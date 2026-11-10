"""Deep sweep (VPS): a full, resumable crawl of Rightmove and OnTheMarket lettings.

The 15-minute head poll reads the newest few pages inside a small circle. The sweep reads
everything in DEEP_SWEEP_MILES for rent from RENT_MIN up to 110% of the highest rent cap, in price
bands: a band that reports more results than the portal will page through is halved (Rightmove
serves 1,008 results per query and then HTTP 200 without results). Work is done in short slices
inside the watcher cycle (DEEP_SWEEP_STEP_SECONDS) and resumes next cycle from the state stored in
the sweeps row, so it never holds up the head poll for more than about two minutes.

Adverts go into the archive; each person's search then judges the ones their settings have not
judged yet, with the commute limit (not their radius) as the filter. A person's first completed
sweep records its matches silently and sends one summary; later sweeps alert normally.
"""
import json
import logging
import time

from requests import RequestException

from . import archive, config, d1, main as pipeline, prefs, telegram, tracker, users
from .sources import otm_rent, rightmove

log = logging.getLogger(__name__)
PAGERS = {"rm": rightmove, "otmr": otm_rent}
NAMES = {"rm": "Rightmove", "otmr": "OnTheMarket"}
MAX_ERRORS = 3          # consecutive failed requests before a sweep is closed as incomplete
EVALUATE_BATCH = 25     # archived adverts judged per pass (one _run each)


def enabled(sources):
    return config.DEEP_SWEEP_HOURS > 0 and archive.enabled() and any(s in PAGERS for s in sources)


def swept(sources):
    return [s for s in sources if s in PAGERS]


# ---------- scope and bands ----------

def scope_for(people):
    """What a sweep covers: the widest of everyone's searches (rent up to 110% of the highest cap)."""
    values = [users.effective(p, live=True) for p in people]
    return {"miles": float(config.DEEP_SWEEP_MILES),
            "min_rent": int(min(v["minrent"] for v in values)),
            "max_rent": int(max(v["rent"] for v in values) * config.DEEP_SWEEP_RENT_MARGIN),
            "min_beds": int(min(v["beds"] for v in values)),
            "centre": [users.DEFAULTS["TARGET_LAT"], users.DEFAULTS["TARGET_LNG"]]}


def initial_state(scope):
    return {"queue": [[scope["min_rent"], scope["max_rent"]]], "current": None, "errors": 0,
            "truncated": 0, "splits": 0, "bands": 0}


def due(source, hours=None, at=None):
    """True when no sweep of this source has started within DEEP_SWEEP_HOURS."""
    hours = config.DEEP_SWEEP_HOURS if hours is None else hours
    import datetime as dt
    with archive._db() as db:
        row = db.execute("SELECT started_at FROM sweeps WHERE source = ? ORDER BY id DESC LIMIT 1", (source,)).fetchone()
    if row is None:
        return True
    now = dt.datetime.fromisoformat(at) if at else dt.datetime.now(dt.timezone.utc)
    return now - dt.datetime.fromisoformat(row["started_at"]) >= dt.timedelta(hours=hours)


# ---------- crawling ----------

def _split(state, band):
    mid = (band["low"] + band["high"]) // 2
    state["queue"][:0] = [[band["low"], mid], [mid + 1, band["high"]]]
    state["current"], state["splits"] = None, state["splits"] + 1


def _truncated(state, band):
    if not band.get("flagged"):
        band["flagged"] = True
        state["truncated"] += 1


def crawl(source, people, budget_seconds, stop_event=None, clock=time.monotonic):
    """Advance this source's sweep for up to budget_seconds (always at least one request).
    Starts a sweep when one is due. Returns the finished sweep's id, or None while it continues."""
    sweep = archive.running_sweep(source)
    if sweep is None:
        if not due(source):
            return None
        scope = scope_for(people)
        archive.start_sweep(source, scope, initial_state(scope))
        sweep = archive.running_sweep(source)
        log.info("Deep sweep of %s started: %s", NAMES[source], scope)
    sweep_id, scope, state, requests = sweep["id"], sweep["scope"], sweep["state"], sweep["requests"]
    pager = PAGERS[source]
    deadline = clock() + budget_seconds
    finished = None          # (complete, note) once the sweep is over
    criteria = {"rent": scope["max_rent"], "minrent": scope["min_rent"], "beds": scope["min_beds"],
                "radius": scope["miles"]}
    try:
        with users.criteria(criteria):
            first = True
            while first or clock() < deadline:
                first = False
                if stop_event is not None and stop_event.is_set():
                    break
                if state["current"] is None:
                    if not state["queue"]:
                        finished = (state["truncated"] == 0, "")
                        break
                    low, high = state["queue"].pop(0)
                    state["current"] = {"low": low, "high": high, "page": 0}
                if requests >= config.DEEP_SWEEP_MAX_REQUESTS:
                    finished = (False, f"stopped at the {config.DEEP_SWEEP_MAX_REQUESTS}-request limit")
                    break
                band = state["current"]
                requests += 1
                try:
                    result = pager.band_page(band["low"], band["high"], band["page"])
                except (RequestException, ValueError) as ex:
                    state["errors"] += 1
                    log.warning("Deep sweep %s request failed (%s)", source, type(ex).__name__)
                    if state["errors"] >= MAX_ERRORS:
                        finished = (False, f"{state['errors']} consecutive request failures ({type(ex).__name__})")
                    break
                state["errors"] = 0
                pipeline.archive_adverts(result["listings"], "sweep", sweep_id)
                raw = result.get("raw", len(result["listings"]))
                if band["page"] == 0:
                    band["total"], band["read"] = result["total"], 0
                band["read"] = band.get("read", 0) + raw
                total = band.get("total")
                if band["page"] == 0 and total is not None and total > pager.REACHABLE:
                    # more results than the portal will page through: halve the price band
                    if band["low"] < band["high"]:
                        _split(state, band)
                        continue
                    _truncated(state, band)       # a one-pound band: read what is reachable, report the gap
                last_page = band["page"] + 1 >= pager.MAX_PAGES
                ended = not result["more"] or raw == 0 or last_page
                if not ended:
                    band["page"] += 1
                    continue
                # A missing next link, a missing total or an empty page is not proof the band was read:
                # only the adverts read reaching the band's own total is (or, with no total anywhere,
                # a natural end before the page limit).
                if total is not None:
                    short = band["read"] < total
                else:
                    short = last_page or (raw == 0 and band["page"] > 0)
                if short:
                    if band["low"] < band["high"]:
                        _split(state, band)
                        continue
                    _truncated(state, band)
                state["current"], state["bands"] = None, state["bands"] + 1
    finally:
        archive.save_sweep(sweep_id, state, requests)
    if finished is None:
        return None
    complete, note = finished
    if not complete and not note:
        note = f"{state['truncated']} price band(s) too large to page through"
    unseen = archive.finish_sweep(sweep_id, complete, note)
    log.info("Deep sweep of %s %s after %s requests%s", NAMES[source], "complete" if complete else "incomplete",
             requests, f" ({note})" if note else "")
    if unseen:
        notify_unseen(unseen)
    return sweep_id


def notify_unseen(keys):
    """A saved home that two complete sweeps could not find. Absence is all the evidence there is, so
    the message says only that: it may have been let, withdrawn or re-priced above the swept range.
    With per-person storage the message shares one acknowledgement with the /pipeline change notice
    for the same archived transition (tracker.once), whichever path gets there first."""
    for person in users.everyone():
        with users.use(person):
            p = prefs.load()
            changed = False
            try:
                for key in keys:
                    info = p["saved"].get(key)
                    if info is None or info.get("unseen"):
                        continue
                    text = (f'❤️ <b>Saved home no longer found in the search</b>\n<a href="{info.get("url")}">'
                            f'{telegram.e(info.get("address") or "listing")}</a>\n'
                            "<i>It may have been let, withdrawn or re-priced above your range.</i>")
                    if users.enabled() and archive.enabled():
                        state = tracker.once(key, "status", "unseen", lambda: telegram.send_text(text))
                    else:
                        telegram.send_text(text)
                        state = "sent"
                    if state == "busy":
                        continue         # the pipeline is telling it right now; it acknowledges for both
                    info["unseen"] = True    # keep the saved entry; just never repeat the message
                    changed = True
                    if state == "sent":
                        telegram.throttle()
            finally:
                if changed:
                    prefs.save(p)


# ---------- judging archived adverts for each person ----------

def _meta(name):
    rows = d1.query("SELECT value FROM meta WHERE name = ?", name)
    return rows[0]["value"] if rows else None


def _put_meta(name, value):
    d1.query("INSERT INTO meta (name, value) VALUES (?, ?) "
             "ON CONFLICT(name) DO UPDATE SET value = excluded.value", name, str(value))


def judge(person, sources, budget_seconds, stop_event=None, verbose=False, clock=time.monotonic):
    """Run this person's search over archived adverts their current settings have not judged yet.
    A source whose first-sweep baseline is not established judges silently; once everything archived
    is judged and a sweep of it has completed, the baseline is set. Returns {source: matches found
    during that baseline} for baselines completed in this call."""
    sources = swept(sources)
    settings = pipeline.settings_key(person)
    deadline = clock() + budget_seconds
    skip, finished = set(), {}
    with users.use(person), users.criteria(person.params):
        for source in sources:
            quiet = _meta(f"sweep_base:{source}") != settings
            drained = False
            while clock() < deadline:
                if stop_event is not None and stop_event.is_set():
                    raise InterruptedError("PisoBot is stopping")
                batch = archive.pending(person.chat_id, [source], settings, EVALUATE_BATCH, skip)
                if not batch:
                    drained = True
                    break
                skip.update(l.key for l in batch)
                result = pipeline._run([source], False, verbose, rentals=True, watch=True, baseline=quiet,
                                       stop_event=stop_event, prefetched=(batch, []), sweep=True)
                if quiet:
                    _put_meta(f"sweep_found:{source}", int(_meta(f"sweep_found:{source}") or 0) + result["fresh"])
            if quiet and drained and archive.last_sweep(source, complete=True) is not None:
                finished[source] = int(_meta(f"sweep_found:{source}") or 0)
                _put_meta(f"sweep_base:{source}", settings)
                _put_meta(f"sweep_found:{source}", 0)
    return finished


def summarise(person, finished):
    """One message per person for the first completed sweep(s) that found matches."""
    count = sum(finished.values())
    if count:
        names = " and ".join(NAMES[s] for s in sorted(finished) if finished[s]) or "the portals"
        with users.use(person):
            telegram.send_text(f"🔭 The deep sweep of {names} found {count} more existing matches than the "
                               "regular scan had shown. They are recorded without alerts; /report lists "
                               "your current matches. Newly listed homes will alert as usual.")


# ---------- one watcher slice ----------

def step(sources, people, stop_event=None, verbose=False, clock=time.monotonic):
    """Called once per watcher cycle after the head poll: crawl, then judge, within about
    DEEP_SWEEP_STEP_SECONDS in total."""
    sources = swept(sources)
    half = config.DEEP_SWEEP_STEP_SECONDS / 2
    for source in sources:
        try:
            crawl(source, people, half / len(sources), stop_event, clock)
        except InterruptedError:
            raise
        except Exception as ex:
            log.error("Deep sweep of %s failed (%s)", source, type(ex).__name__)
    for person in people:
        if not person.owner and users.get(person.chat_id) is None:
            continue
        try:
            summarise(person, judge(person, sources, half / len(people), stop_event, verbose, clock))
        except InterruptedError:
            raise
        except Exception as ex:
            log.error("Deep sweep judging failed for %s (%s)", person.label, type(ex).__name__)
