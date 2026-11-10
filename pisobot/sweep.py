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

from . import archive, config, d1, main as pipeline, prefs, telegram, users
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


def covers(scope, needed):
    """A completed generation must actually cover this person's current search."""
    return (scope["min_rent"] <= needed["min_rent"] and scope["max_rent"] >= needed["max_rent"]
            and scope["min_beds"] <= needed["min_beds"] and scope["miles"] >= needed["miles"]
            and scope["centre"] == needed["centre"])


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
    required = scope_for(people)
    sweep = archive.running_sweep(source)
    if sweep is not None and not covers(sweep["scope"], required):
        archive.finish_sweep(sweep["id"], False, "scope expanded; starting a new generation")
        sweep = None
    if sweep is None:
        last = archive.last_sweep(source)
        if not due(source) and (last is None or covers(last["scope"], required)):
            return None
        scope = required
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
                reserved = archive.reserve_request(sweep_id, state, config.DEEP_SWEEP_MAX_REQUESTS)
                if reserved is None:
                    finished = (False, "request budget already exhausted")
                    break
                requests = reserved
                try:
                    result = pager.band_page(band["low"], band["high"], band["page"])
                except (RequestException, ValueError) as ex:
                    state["errors"] += 1
                    log.warning("Deep sweep %s request failed (%s)", source, type(ex).__name__)
                    if state["errors"] >= MAX_ERRORS:
                        finished = (False, f"{state['errors']} consecutive request failures ({type(ex).__name__})")
                    break
                # Strict persistence: never advance/claim coverage when a page was not archived.
                archive.upsert(result["listings"], "sweep", sweep_id)
                state["errors"] = 0
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
        notify_unseen(unseen, deadline=deadline, clock=clock, stop_event=stop_event)
    return sweep_id


def _people_turn(people, name):
    """Primary-DB cursor, regardless of the caller's current person/database."""
    people = list(people)
    row = users._primary("SELECT value FROM meta WHERE name = ?", name)
    turn = int(row[0]["value"]) % len(people) if row and people else 0
    if people:
        users._primary("INSERT INTO meta(name,value) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                       name, str((turn + 1) % len(people)))
    return people[turn:] + people[:turn]


def notify_unseen(keys=None, deadline=None, clock=time.monotonic, stop_event=None):
    """Drain durable, per-person transition events; one failed chat must not block another.

    At least once: a crash after Telegram accepts but before the DB ack may duplicate a message.
    """
    if deadline is not None and clock() >= deadline:
        return
    people = _people_turn(users.everyone(), "unseen_turn")
    for index, person in enumerate(people):
        person_deadline = (None if deadline is None else
                           min(deadline, clock() + max(0, deadline - clock()) / (len(people) - index)))
        try:
            with users.use(person):
                p = prefs.load()
                for event in archive.pending_unseen(person.chat_id):
                    if stop_event is not None and stop_event.is_set():
                        raise InterruptedError("PisoBot is stopping")
                    if person_deadline is not None and clock() >= person_deadline:
                        break
                    if keys is not None and event["key"] not in keys:
                        continue
                    info = p["saved"].get(event["key"])
                    if info is not None and event["status"] == "unseen":
                        telegram.send_text(f'❤️ <b>Saved home no longer found in the search</b>\n'
                                           f'<a href="{info.get("url")}">{telegram.e(info.get("address") or "listing")}</a>\n'
                                           "<i>It may have been let, withdrawn or re-priced above your range.</i>")
                        info["unseen"] = True
                        prefs.save(p)
                        telegram.throttle()
                    archive.acknowledge_unseen(person.chat_id, event["id"])
        except InterruptedError:
            raise
        except Exception as ex:
            log.error("Unseen notification failed for %s (%s); retained for retry", person.label, type(ex).__name__)


# ---------- judging archived adverts for each person ----------

def _meta(name):
    rows = d1.query("SELECT value FROM meta WHERE name = ?", name)
    return rows[0]["value"] if rows else None


def _put_meta(name, value):
    d1.query("INSERT INTO meta (name, value) VALUES (?, ?) "
             "ON CONFLICT(name) DO UPDATE SET value = excluded.value", name, str(value))


def judge(person, sources, budget_seconds, stop_event=None, verbose=False, clock=time.monotonic,
          alert_budget=None):
    """Run this person's search over archived adverts their current settings have not judged yet.
    A source whose first-sweep baseline is not established judges silently; once everything archived
    is judged and a sweep of it has completed, the baseline is set. Returns {source: matches found
    during that baseline} for baselines completed in this call."""
    sources = swept(sources)
    settings = pipeline.settings_key(person)
    needed = scope_for([person])
    alert_budget = alert_budget if alert_budget is not None else pipeline.new_alert_budget()
    deadline = clock() + budget_seconds
    skip, finished = set(), {}
    with users.use(person), users.criteria(person.params):
        turn = int(_meta("sweep_judge_turn") or 0) % len(sources) if sources else 0
        sources = sources[turn:] + sources[:turn]
        if sources:
            _put_meta("sweep_judge_turn", (turn + 1) % len(sources))
        for index, source in enumerate(sources):
            source_deadline = min(deadline, clock() + max(0, deadline - clock()) / (len(sources) - index))
            quiet = _meta(f"sweep_base:{source}") != settings
            if quiet and _meta(f"sweep_generation:{source}") != settings:
                _put_meta(f"sweep_generation:{source}", settings)
                _put_meta(f"sweep_found:{source}", 0)
            drained = False
            while clock() < source_deadline:
                if stop_event is not None and stop_event.is_set():
                    raise InterruptedError("PisoBot is stopping")
                if not quiet and alert_budget["remaining"] <= 0:
                    break
                batch = archive.pending(person.chat_id, [source], settings, EVALUATE_BATCH, skip,
                                        exclude_intents=quiet and alert_budget["remaining"] <= 0)
                if not batch:
                    # Classified retries/intents have their OWN silence/delivery lifecycle.
                    # Only unattempted inventory blocks this source generation from going live.
                    drained = not archive.pending(person.chat_id, [source], settings, 1, drainage=True)
                    break
                result = pipeline._run([source], False, verbose, rentals=True, watch=True, baseline=quiet,
                                       stop_event=stop_event, prefetched=(batch, []), sweep=True,
                                       alert_budget=alert_budget, deadline=source_deadline, clock=clock)
                skip.update(result.get("processed", [l.key for l in batch]))
                if quiet:
                    _put_meta(f"sweep_found:{source}", int(_meta(f"sweep_found:{source}") or 0) + result.get("baseline_fresh", result["fresh"]))
            if quiet:
                # The last attempted member may have consumed the slice. Its classified retry
                # must not keep the generation quiet merely because the loop hit its deadline.
                drained = not archive.pending(person.chat_id, [source], settings, 1, drainage=True)
            complete = archive.last_sweep(source, complete=True)
            running = archive.running_sweep(source)
            generation_complete = (complete is not None and covers(complete["scope"], needed)
                                   and (running is None or running["id"] < complete["id"]))
            if quiet and drained and generation_complete:
                _put_meta(f"sweep_base_generation:{source}", complete["id"])
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

def step(sources, people, stop_event=None, verbose=False, clock=time.monotonic, alert_budgets=None):
    """Called once per watcher cycle after the head poll: crawl, then judge, within about
    DEEP_SWEEP_STEP_SECONDS in total."""
    sources = swept(sources)
    half = config.DEEP_SWEEP_STEP_SECONDS / 2
    deadline = clock() + config.DEEP_SWEEP_STEP_SECONDS
    for source in sources:
        if clock() >= deadline:
            break
        try:
            crawl(source, people, min(half / len(sources), deadline - clock()), stop_event, clock)
        except InterruptedError:
            raise
        except Exception as ex:
            log.error("Deep sweep of %s failed (%s)", source, type(ex).__name__)
    # Reserve time for old durable notices BEFORE judgement can consume the whole slice.
    notify_unseen(deadline=min(deadline, clock() + config.DEEP_SWEEP_STEP_SECONDS * 0.1),
                  clock=clock, stop_event=stop_event)
    for person in _people_turn(people, "sweep_people_turn"):
        if not person.owner and users.get(person.chat_id) is None:
            continue
        try:
            budget = alert_budgets.get(person.chat_id) if alert_budgets is not None else None
            seconds = max(0, min(half / len(people), deadline - clock()))
            summarise(person, judge(person, sources, seconds, stop_event, verbose, clock,
                                    alert_budget=budget))
        except InterruptedError:
            raise
        except Exception as ex:
            log.error("Deep sweep judging failed for %s (%s)", person.label, type(ex).__name__)
    # Retry durable events even when no new sweep finished; never let a backlog hold the poll.
    notify_unseen(deadline=deadline, clock=clock, stop_event=stop_event)
