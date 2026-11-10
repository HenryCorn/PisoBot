import argparse
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from requests import RequestException

from . import (archive, cache, collage, config, epc, costs, d1, enrich, extract, intelligence, mapgen, models,
               prefs, ranking, renter, rental_text, taste, telegram, tracker, users)
from .sources import lq, nhg, onthemarket, openrent, otm_rent, rightmove, sharetobuy, spareroom, zoopla
from .sources import detail_fields, private_rent_costs

SOURCES = {"stb": sharetobuy, "lq": lq, "nhg": nhg, "otm": onthemarket, "rm": rightmove,
           "otmr": otm_rent, "zp": zoopla, "orent": openrent, "sr": spareroom,
           # Rental schemes from the housing-association sources (London Living Rent, Rent to Buy)
           "stbr": SimpleNamespace(fetch=sharetobuy.fetch_rent, photos=sharetobuy.photos,
                                   RENT_REFRESH_MINUTES=sharetobuy.RENT_REFRESH_MINUTES),
           "lqr": SimpleNamespace(fetch=lq.fetch_rent, RENT_REFRESH_MINUTES=lq.RENT_REFRESH_MINUTES)}
# Sources the VPS watcher may scan (all fetched directly, never through Decodo)
LOCAL_RENTAL_SOURCES = ["rm", "otmr", "orent", "stbr", "lqr", "sr"]
# Private rentals cost Decodo requests, so they only run on demand (/rentals in Telegram -> --rentals)
RENTAL_SOURCES = ["rm", "otmr", "zp"]
DEFAULT_SOURCES = ["stb", "lq", "nhg", "otm"]
STATE_DIR = os.environ.get("PISOBOT_STATE_DIR") or os.path.join(os.path.dirname(__file__), "..", "state")
SEEN_PATH = os.path.join(STATE_DIR, "seen.json")


@contextmanager
def run_lock(name="run"):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, f"{name}.lock"), "a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"Another PisoBot {name} is already running") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load_seen():
    try:
        with open(users.seen_path(SEEN_PATH), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        if os.environ.get("STORAGE") == "local":
            raise
        return {}


def save_seen(seen):
    path = users.seen_path(SEEN_PATH)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as f:
            temporary = f.name
            json.dump(seen, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _private_rental(l):
    """Costed as a private let (rent + estimated bills); scheme rents on the sale path are not."""
    return l.tenure == "rent" and l.bills is not None


def _cost_reject(l, early=False):
    """None when the money limits pass, else the funnel reason. Private rentals: advertised rent
    against the rent cap ("rent_cap"), then, only if the person set one, rent + bills against the
    all-in cap ("all_in_cap"). early=True is the cheap check before the details page is fetched,
    when bills are not final: rent alone is a lower bound, so it can only reject safely."""
    if not costs.compute(l):
        return "cost"
    if not _private_rental(l):
        return "cost" if l.monthly_cost > config.MAX_MONTHLY_COST else None
    if l.rent > config.MAX_RENT:
        return "rent_cap"
    if config.MAX_ALL_IN is not None and (l.rent if early else l.monthly_cost) > config.MAX_ALL_IN:
        return "all_in_cap"
    return None


def _post_details_reject(l):
    """Rental preferences that need the details page (a published area, furnishing)."""
    if l.tenure != "rent":
        return None
    l.studio = l.studio or rental_text.is_studio(f"{l.address}. {l.description}")
    if l.studio and config.STUDIO_MIN_AREA is None:
        return "studio"
    if l.studio and config.STUDIO_MIN_AREA:      # >0: studios only with a published area at least this big
        if not l.floor_area_m2 or l.floor_area_m2 < config.STUDIO_MIN_AREA:
            return "studio_area"
    if config.FURNISHED != "any":
        state = l.furnishing or rental_text.furnishing(l.description)
        if (config.FURNISHED, state) in (("furnished", "unfurnished"), ("unfurnished", "furnished")):
            return "furnishing"      # only a stated opposite rejects; not stated keeps the advert
    return None


def _district_reject(l):
    if l.tenure != "rent" or not (config.AREAS_EXCLUDE or config.AREAS_INCLUDE):
        return None
    if models.in_districts(l.outcode, config.AREAS_EXCLUDE):
        return "area_excluded"
    if config.AREAS_INCLUDE and not models.in_districts(l.outcode, config.AREAS_INCLUDE):
        return "area_outside"
    return None


def staged(l):
    """Private rentals on the VPS are judged coordinates-first: commute and crime before any details
    page is fetched. Everything else (the cloud path included) keeps the original order."""
    return archive.enabled() and l.tenure == "rent" and l.bills is not None


def passes_basic(l, sweep=False, stage=None, defer_model=False):
    """stage None: every check in the original order. "cheap": checks needing no request (beds,
    keywords, location, distance, rent). "details": the details page, then the final cost.
    sweep=True: the advert comes from the deep sweep, so the person's radius is not a filter here
    (the commute limit is); only the swept area is."""
    if stage in (None, "cheap"):
        studio = l.tenure == "rent" and (l.studio or rental_text.is_studio(f"{l.address}. {l.description}"))
        if studio:
            l.studio = True
            if config.STUDIO_MIN_AREA is None:
                return "studio"
        if (max(l.bedrooms, 1) if studio else l.bedrooms) < config.MIN_BEDROOMS:
            return "beds"
        if archive.enabled() and _private_rental(l) and l.rent is not None and l.rent < config.RENT_MIN:
            return "min_rent"
        by_rules = l.excluded_by_keywords()
        if by_rules and not (extract.enabled() or extract._default is not None):
            return "keywords"          # no language-model layer: the rules decide, exactly as before
        if l.lat is None or l.lng is None:
            return "no location"
        why = _district_reject(l)
        if why:
            return why
        distance = (_km(l.lat, l.lng, users.DEFAULTS["TARGET_LAT"], users.DEFAULTS["TARGET_LNG"])
                    if users.current() is not None and l.tenure == "rent" else enrich.distance_km(l.lat, l.lng))
        max_dist = (config.DEEP_SWEEP_MILES * 1.609344 if sweep else config.SEARCH_RADIUS_KM)
        if distance > max_dist:
            return "too far"
        refine = getattr(SOURCES.get(l.source), "refine", None)
        if stage == "cheap" or refine:
            # cheap gate first so only plausible listings cost a details-page fetch
            why = _cost_reject(copy.deepcopy(l), early=True)
            if why:
                return why
    if stage == "cheap":
        return None
    refine = getattr(SOURCES.get(l.source), "refine", None)
    if refine:
        if not refine(l):
            return ("enrichment" if archive.enabled() and getattr(l, "detail_unavailable", False)
                    else "details")
    for key, value in detail_fields(l.description).items():
        if getattr(l, key) is None:
            setattr(l, key, value)
    why = _post_details_reject(l) or _cost_reject(l)
    if why:
        return why
    if l.tenure == "rent" and rental_text.explicit_room_let(l.description):
        return "keywords"  # an explicit room/share is a hard unit-type dealbreaker, never an AI rescue
    if not defer_model and extract.keyword_verdict(l, l.excluded_by_keywords()):
        return "keywords"
    return None


def zone_prefilter(l):
    """Legacy no-op hook. Zone estimates may ORDER future lookups, never hard-filter them.
    Every advert still goes on to exact TfL judging; zone.py is deliberately not imported here."""
    return True


def passes_area(l, deadline=None, clock=time.monotonic):
    if not zone_prefilter(l):
        return "travel"
    j = enrich.journey(l.lat, l.lng)
    if not j:
        return "travel"
    # set before the limit is applied so a rejection still records the minutes it was judged on
    l.travel_minutes, l.route, l.station, l.walk_minutes = j["minutes"], j["route"], j["station"], j["walk"]
    l.changes = j.get("changes", 0)
    # Personal limits on the same journey, before the crime lookup (a further request) is spent
    if config.MAX_WALK_MINUTES is not None and (l.walk_minutes or 0) > config.MAX_WALK_MINUTES:
        return "walk"
    if config.MAX_CHANGES is not None and l.changes > config.MAX_CHANGES:
        return "changes"
    if j["minutes"] > config.MAX_TRAVEL_MINUTES:
        return "travel"
    if deadline is not None and clock() >= deadline:
        return "deferred"
    l.crime_score = enrich.crime_score(l.lat, l.lng)
    if l.crime_score > config.MAX_CRIME_SCORE:
        return "crime"
    return None


def is_duplicate(l, records):
    """Legacy sale heuristic only; rental price/location cannot establish listing identity."""
    if l.tenure == "rent":
        return False
    price = l.share_price or l.rent or 0
    for a in records:
        if a.get("beds") != l.bedrooms or not a.get("lat") or not price:
            continue
        if abs(a.get("price", 0) - price) <= 0.02 * price and _km(a["lat"], a["lng"], l.lat, l.lng) <= 0.3:
            return True
    return False


def _km(lat1, lng1, lat2, lng2):
    return math.hypot((lat1 - lat2) * 111.2, (lng1 - lng2) * 111.2 * math.cos(math.radians(lat1)))


def _now():
    return dt.datetime.now(ZoneInfo("Europe/London")).strftime("%Y-%m-%dT%H:%M")


def report_due(p, now=None):
    """Catch up the latest UK report slot if GitHub missed its dedicated cron."""
    now = now or dt.datetime.now(ZoneInfo("Europe/London"))
    slots = [now.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in ((6, 30), (18, 0))]
    due = [slot for slot in slots if slot <= now]
    return bool(due and p.get("last_report", "") < max(due).strftime("%Y-%m-%dT%H:%M"))


def _record(l, today, alerted, prev=None):
    prev = prev or {}
    record = {"alerted": alerted, "cost": round(l.monthly_cost), "first": prev.get("first", _now()),
              "last": today, "url": l.url, "beds": l.bedrooms, "price": l.share_price or l.rent,
              "lat": l.lat, "lng": l.lng, "address": l.address}
    if l.tenure == "rent":
        record["rent"] = l.rent   # price drops compare advertised rent: bills estimates vary per listing
    if prev.get("delivery_intent") is not None:
        record["delivery_intent"] = prev["delivery_intent"]  # never invent a newer delivery receipt
    return record


def price_dropped(l, prev):
    """Alert-worthy cut since the stored record. Rentals compare the advertised rent, never the
    all-in cost; a record without a stored rent cannot alert."""
    if l.tenure == "rent":
        old = prev.get("rent")
        return old is not None and l.rent is not None and old - l.rent >= config.PRICE_DROP_ALERT
    return prev.get("cost", 0) - l.monthly_cost >= config.PRICE_DROP_ALERT


def _ensure_photos(l):
    loader = getattr(SOURCES.get(l.source), "photos", None)
    if loader and len(l.images) < 3:
        try:
            loaded = loader(l)
            if loaded:
                l.images = list(dict.fromkeys([*l.images, *loaded]))
        except (RequestException, ValueError) as ex:
            print(f"Gallery unavailable for {l.key}: {ex}", file=sys.stderr)


def send_report(top, saved, rest, title, flags_of, preview_dir=None, map_url=None):
    messages = telegram.report_messages(top, rest, saved, title, flags_of, map_url=map_url)
    if preview_dir:
        os.makedirs(preview_dir, exist_ok=True)
    preview = []
    for i, (text, kb) in enumerate(messages):
        jpeg, l, shown = None, None, []
        if 1 <= i <= len(top):
            l = top[i - 1]
            _ensure_photos(l)
            jpeg = collage.build(l, sources=shown)
            print(f"card #{i}: {l.key}")
            if not jpeg:
                text += "\n📷 Header unavailable · use Open listing to view the media"
        if preview_dir:
            image = ""
            if jpeg:
                filename = f"listing-{i}.jpg"
                with open(os.path.join(preview_dir, filename), "wb") as f:
                    f.write(jpeg)
                image = f'<img src="{filename}" alt="Listing {i} map and property" style="max-width:100%"><br>\n'
            preview.append(image + text.replace("\n", "<br>\n"))
        else:
            result = telegram.send_card(jpeg, text, kb, f"🏠 Listing #{i}")
            if l is not None:
                telegram.remember_photos(l.key, result, shown if jpeg else [])
            telegram.throttle()
    if preview_dir:
        with open(os.path.join(preview_dir, "report.html"), "w", encoding="utf-8") as f:
            f.write("\n<hr>\n".join(preview))
        print(f"report preview written to {preview_dir}")


def locked():
    """The run lock for local storage (one pipeline per state directory), else a no-op."""
    return run_lock() if os.environ.get("STORAGE") == "local" else contextlib.nullcontext()


def fetch_sources(selected, stop_event=None):
    """Every selected portal once: (listings, failed source names)."""
    listings, failures = [], []
    for name in selected:
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("PisoBot is stopping")
        try:
            got = SOURCES[name].fetch()
            print(f"[{name}] {len(got)} listings")
            listings += got
        except Exception:
            failures.append(name)
            print(f"[{name}] FAILED\n{traceback.format_exc()}", file=sys.stderr)
    return listings, failures


def _save_funnel(funnel, live, rentals):
    if live and rentals and os.environ.get("STORAGE") == "local":
        d1.query("INSERT INTO rental_funnel (created_at, stats) VALUES (?, ?)",
                 dt.datetime.now(dt.timezone.utc).isoformat(), json.dumps(funnel, sort_keys=True))


def run(selected, dry_run, verbose, report=False, preview_dir=None, scheduled=False, rentals=False,
        watch=False, baseline=False, stop_event=None, prefetched=None):
    args = (selected, dry_run, verbose, report, preview_dir, scheduled, rentals, watch, baseline, stop_event,
            prefetched)
    if os.environ.get("STORAGE") == "local":
        with run_lock():
            if rentals and users.current() is None:
                person = users.owner()
                if prefetched is None:
                    for source in selected:
                        cache.put("rent_search", source, {})
                    with users.criteria(users.fetch_params(person)):
                        prefetched = fetch_sources(selected, stop_event)
                    if not dry_run:
                        archive_adverts(prefetched[0])
                with users.use(person), users.criteria(person.params):
                    return _run(*args[:-1], prefetched=prefetched)
            return _run(*args)
    return _run(*args)


def archive_adverts(listings, via="head", sweep_id=None):
    """Remember every rental a local fetch returned (no network). A failure here never stops a scan."""
    if archive.enabled() and listings:
        try:
            return archive.upsert([l for l in listings if l.tenure == "rent"], via, sweep_id)
        except Exception as ex:
            print(f"archive upsert failed: {type(ex).__name__}: {ex}", file=sys.stderr)


def settings_key(person=None):
    """Effective defaults are immutable across the polling/pipeline threads. Version policy too."""
    person = person if person is not None else users.current()
    person = person or users.User(users.owner_chat(), owner=True)
    policy = {"version": "wp2-hardening-2", "criteria": users.effective(person),
              "sweep_miles": config.DEEP_SWEEP_MILES}
    return hashlib.sha1(json.dumps(policy, sort_keys=True).encode()).hexdigest()[:12]


def new_alert_budget():
    """One mutable allowance per person per watcher cycle, shared with every sweep batch."""
    return {"remaining": config.MAX_ALERTS_PER_RUN, "attempts": config.MAX_ALERTS_PER_RUN}


def _verdict_row(l, why):
    all_in = l.monthly_cost if l.monthly_cost is not None else (
        l.rent + l.bills if l.rent is not None and l.bills is not None else None)
    return (l.key, why or "matched", l.rent, all_in, l.travel_minutes, l.crime_score)


def _set_urgency(l):
    """WP3: mark a new rental "top" (⚡ on the card; loud at night only with /set urgent on). Best
    effort: a failure only means no marker, never a lost alert."""
    try:
        l.urgency = renter.tier(l, users.current_chat() or users.owner_chat() or "owner")
    except Exception as ex:
        print(f"urgency tier failed: {type(ex).__name__}", file=sys.stderr)


def _send_change(l, reason):
    """Send a price-drop or saved-home change card unless this person was already told about the same
    archived rent change by another path (a /pipeline notice). Returns "sent", "told" (record it as
    seen, send nothing) or "busy" (another path is sending it now: leave it for the next run)."""
    if l.tenure == "rent" and l.rent is not None and users.enabled() and archive.enabled():
        return tracker.once(l.key, "rent", l.rent, lambda: telegram.send_listing(l, reason))
    telegram.send_listing(l, reason)
    return "sent"


def _run(selected, dry_run, verbose, report=False, preview_dir=None, scheduled=False, rentals=False,
         watch=False, baseline=False, stop_event=None, prefetched=None, sweep=False,
         sweep_keys=(), alert_budget=None, deadline=None, clock=time.monotonic):
    """prefetched: (listings, failures) shared by several people's runs in one cycle; each run
    works on its own copy so per-person refinement and costs never leak between people.
    sweep: the listings come from the market archive (deep sweep), not a portal fetch; the person's
    radius does not filter them, and the run makes no funnel record, no "watch is live" message and
    does not fail on enrichment errors (they are retried). sweep_keys preserves that provenance
    for report extras. alert_budget is shared by the person\'s whole cycle. deadline yields between
    adverts/lookup stages; it cannot cancel an already-running HTTP/image operation."""
    def check_stop():
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("PisoBot is stopping")

    check_stop()
    alert_budget = alert_budget if alert_budget is not None else new_alert_budget()
    sweep_keys = set(sweep_keys)
    processed = []
    local_rentals = archive.enabled() and rentals
    person_id = users.current_chat() or users.owner_chat() or "owner"
    settings = settings_key() if archive.enabled() else None
    seen = load_seen()
    seen_before = copy.deepcopy(seen)
    p = prefs.load()
    live = not dry_run and not preview_dir
    alerted = [v for v in seen.values() if v.get("alerted")]
    if prefetched is None:
        listings, failures = fetch_sources(selected, stop_event)
    else:
        listings, failures = copy.deepcopy(prefetched[0]), list(prefetched[1])
        for l in listings:
            if l.tenure == "rent":
                private_rent_costs(l)   # this person's bills estimate
    funnel = {"mode": "baseline" if baseline else "watch" if watch else "report",
              "filtering_finished": False, "source_failures": failures, "sources": {}}

    def source_stats(source):
        alias = f"{source}r" if source in ("stb", "lq") else source
        name = alias if alias in selected else source
        return funnel["sources"].setdefault(name, {
            "fetched": 0, "rejected": {}, "matched": 0, "hidden": 0})

    def reject(source, reason):
        counts = source_stats(source)["rejected"]
        counts[reason] = counts.get(reason, 0) + 1
        rejected[reason] = rejected.get(reason, 0) + 1

    for source in selected:
        source_stats(source)
    for listing in listings:
        source_stats(listing.source)["fetched"] += 1
    if failures and len(failures) == len(selected):
        _save_funnel(funnel, live, rentals)
        raise RuntimeError("Every source failed; report delivery is incomplete and will be retried")

    try:
        if not sweep:
            enrich.geocode(listings)
    except Exception as ex:
        print(f"geocoding failed: {ex}", file=sys.stderr)
        funnel["geocoding_error"] = type(ex).__name__

    # Distinct rental IDs are not proof of the same flat, even at identical map pins/prices.
    matches, kept, rejected, enrich_failures = [], [], {}, 0
    deferred_failures = 0
    rental_keys = set()
    verdicts = []
    try:
        for l in listings:
            check_stop()
            if deadline is not None and clock() >= deadline:
                break
            processed.append(l.key)
            from_sweep = sweep or l.key in sweep_keys
            if l.tenure == "rent" and l.key in rental_keys:
                reject(l.source, "duplicate_id")
                continue
            if l.tenure == "rent":
                rental_keys.add(l.key)
            if local_rentals and prefs.is_hidden(p, l):
                # Cancel structurally hidden work before expensive lookups, including
                # an old delivery obligation; this is suppression, not a send receipt.
                reject(l.source, "hidden")
                source_stats(l.source)["hidden"] += 1
                verdicts.append(_verdict_row(l, "hidden"))
                continue
            if local_rentals and live:
                blocked = archive.retry_block(person_id, l.key, settings)
                if blocked:
                    reject(l.source, blocked)
                    deferred_failures += 1
                    continue
            order = staged(l)
            if order:
                why = passes_basic(l, sweep=from_sweep, stage="cheap", defer_model=True)
            elif from_sweep:
                why = passes_basic(l, sweep=True, defer_model=True)
            else:
                why = passes_basic(l, defer_model=True)
            if not why:
                if local_rentals and live:
                    archive.begin_lookup(person_id, l.key, settings, baseline=baseline)
                try:
                    why = (passes_area(l, deadline=deadline, clock=clock) if deadline is not None
                           else passes_area(l))
                except Exception as ex:  # API hiccup: try again next run
                    print(f"enrich failed for {l.key}: {type(ex).__name__}", file=sys.stderr)
                    enrich_failures += 1
                    reject(l.source, "enrichment")
                    verdicts.append(_verdict_row(l, "enrichment"))
                    continue
            if not why and order:
                if deadline is not None and clock() >= deadline:
                    why = "deferred"  # classified, isolated work; never hog every future slice
                else:
                    try:
                        why = passes_basic(l, sweep=from_sweep, stage="details", defer_model=True)
                    except Exception as ex:
                        if not local_rentals:
                            raise
                        print(f"details failed for {l.key}: {type(ex).__name__}", file=sys.stderr)
                        why = "enrichment"
                if why == "enrichment":
                    enrich_failures += 1
            if not why and extract.keyword_verdict(l, l.excluded_by_keywords()):
                why = "keywords"
            verdicts.append(_verdict_row(l, why))
            if why:
                reject(l.source, why)
                if verbose:
                    cost = round(l.monthly_cost) if l.monthly_cost else None
                    print(f"  - {why:13} {l.key} {l.address[:60]} {cost}")
                continue
            if l.tenure != "rent" and is_duplicate(l, kept):
                reject(l.source, "duplicate")
                continue
            if l.tenure != "rent":
                kept.append(_record(l, "", alerted=True))
            epc.annotate(l)          # G2: certificate facts; adds labels only, never rejects (fails open)
            matches.append(l)
            stats = source_stats(l.source)
            stats["matched"] += 1
            stats["hidden"] += int(prefs.is_hidden(p, l))
        funnel["filtering_finished"] = True
    finally:
        if not sweep:
            _save_funnel(funnel, live, rentals)
        if live and archive.enabled() and verdicts:
            try:
                # Durable delivery intent BEFORE publish/send/seen I/O. A match is not an ack.
                notice_keys = {r[0] for r in verdicts if r[0] not in seen or (
                    r[2] is not None and seen[r[0]].get("rent") is not None
                    and seen[r[0]].get("alerted") and seen[r[0]]["rent"] - r[2] >= config.PRICE_DROP_ALERT)}
                intents = [(r[0], "unsent", *r[2:]) if r[1] == "matched" and r[0] in notice_keys
                           else r for r in verdicts]
                suppressed = {r[0] for r in verdicts if r[1] == "matched" and r[0] not in notice_keys}
                archive.record_verdicts(person_id, intents, settings, baseline=baseline,
                                        notice_keys=notice_keys, suppressed_keys=suppressed)
            except Exception as ex:
                print(f"archive verdicts failed: {type(ex).__name__}: {ex}", file=sys.stderr)
                if rentals:
                    raise  # do not deliver local rentals without durable retry state
    print(f"rejected: {rejected}")
    print(f"funnel: {json.dumps(funnel, sort_keys=True)}")
    if baseline and not sweep and (failures or (enrich_failures and not local_rentals)):
        raise RuntimeError("Initial rental baseline incomplete; retrying without sending listing alerts")
    enrich.assign_crime_percentiles(matches)
    epc.annotate_values(listings)    # G2: rent per m2 against the same scan

    # 2. Saved/hidden choices (recorded instantly by the Worker) apply to these matches
    today = dt.date.today().isoformat()
    known = {k: {"lat": v.get("lat"), "lng": v.get("lng"), "url": v.get("url"),
                 "address": v.get("address", ""), "cost": v.get("cost")} for k, v in seen.items()}
    known.update({l.key: {"lat": l.lat, "lng": l.lng, "url": l.url, "address": l.address,
                          "cost": round(l.monthly_cost)} for l in matches})
    if live:
        prefs.publish(known)
    hidden = [l for l in matches if prefs.is_hidden(p, l)]
    matches = [l for l in matches if not prefs.is_hidden(p, l)]
    fresh = sum(1 for l in matches if l.key not in seen)   # not yet recorded for this person
    print(f"current matches: {len(matches)} (+{len(hidden)} hidden)")

    # 3. Intelligence & Taste preference matching
    taste_engine = taste.TasteProfile(
        p.get("saved", {}), p.get("hidden", []), p.get("hidden_dev", []), seen, p.get("feedback", [])
    )
    for l in matches:
        l.red_flags = intelligence.detect_red_flags(l.description)
        l.vibes = intelligence.detect_vibes(l.description)
        l.livability_tags = intelligence.nearby_green_spaces(l.lat, l.lng)
        l.rent_savings = intelligence.opportunity_cost_savings(l)
        l.deal_tag = intelligence.deal_hunter_signals(l, seen.get(l.key))
        l.match_score, l.match_reasons = taste_engine.match(l)

    # 4. saved homes: price changes and disappearances
    by_key = {l.key: l for l in matches}
    fetched_keys = {l.key for l in listings}
    saved_changes, gone = [], []
    for key, info in list(p["saved"].items()):
        l = by_key.get(key)
        if l:
            info["missing"] = 0
            info.pop("unseen", None)
            if l.tenure == "rent":
                # advertised rent, not all-in: an unchanged advert must not alert when bills move
                if info.get("rent") is None:
                    info["rent"], info["cost"] = l.rent, round(l.monthly_cost)
                elif abs(info["rent"] - l.rent) >= config.PRICE_DROP_ALERT:
                    saved_changes.append(l)
                    info["rent"], info["cost"] = l.rent, round(l.monthly_cost)
            elif abs((info.get("cost") or 0) - l.monthly_cost) >= config.PRICE_DROP_ALERT:
                saved_changes.append(l)
                info["cost"] = round(l.monthly_cost)
        elif key in fetched_keys:
            info["missing"] = 0   # still advertised, just outside the filters now
        elif not watch and os.environ.get("STORAGE") != "local" and key.split(":")[0] in selected \
                and key.split(":")[0] not in failures:
            info["missing"] = info.get("missing", 0) + 1
            if info["missing"] >= 2:
                gone.append(info)
                del p["saved"][key]

    # 4. what's new or cheaper since last time
    to_send = []
    for l in matches:
        prev = seen.get(l.key)
        if l.key in p["saved"] and not watch:
            pass
        elif prev is None:
            if l.tenure == "rent" or not is_duplicate(l, alerted):
                to_send.append((l, "new"))
        elif prev.get("alerted") and price_dropped(l, prev):
            to_send.append((l, "drop"))
        if prev and live:
            prev["last"] = today
            if l.tenure == "rent" and l.rent is not None and (prev.get("rent") is None or l.rent > prev["rent"]):
                prev["rent"] = l.rent   # first sight of this record's rent, or the advert went up
    f_map = archive.delivery_failures(person_id, [x[0].key for x in to_send]) if (local_rentals and live) else {}
    to_send.sort(key=lambda x: (f_map.get(x[0].key, 0) > 0, ranking.score(x[0])))

    # 🆕 = first seen since the last report (older records only have a date: treat as reported).
    # On-demand rentals reports keep their own "last report" so they never stand in for 06:30/18:00.
    if not p.get("last_report") and seen:
        p["last_report"] = max(v.get("first", "") for v in seen.values())[:10] + "T23:59"
    since = (cache.get("meta", users.scoped("last_rentals_report")) or "") if rentals else p.get("last_report", "")
    new_keys = {l.key for l in matches if l.key not in seen or seen[l.key].get("first", "") > since}
    saved_keys = set(p["saved"])

    def flags_of(l):
        return " ".join(f for f, on in (("🆕", l.key in new_keys), ("❤️", l.key in saved_keys)) if on)

    top = ranking.best(matches)
    if os.environ.get("STORAGE") != "local":
        try:
            mapgen.write(matches, top, saved_keys, new_keys, rentals=rentals)
        except Exception:
            print(f"map page failed\n{traceback.format_exc()}", file=sys.stderr)

    # Silence is per-advert provenance, never a global permission to consume a live intent.
    # Baseline failures retain this membership even after the person's source goes live.
    silent = (archive.silent_keys(person_id, [l.key for l in matches], baseline=baseline)
              if local_rentals and live else {l.key for l in matches} if baseline else set())
    baseline_fresh = sum(l.key in silent and l.key not in seen for l in matches)
    to_send = [(l, reason) for l, reason in to_send if l.key not in silent]
    if live and silent:
        for l in matches:
            if l.key in silent:
                seen[l.key] = _record(l, today, alerted=True, prev=seen.get(l.key))
        save_seen(seen)
    if baseline and live and not sweep:
        telegram.send_text(f"Rental watch is live: {len(silent)} current matches recorded. "
                           "Only new matches and price drops will alert; /report shows them all.")
    if watch:
        saved_changes, gone = [], []

    # 5. report (first run, twice-daily schedule, or --report, which /report dispatches) ...
    report_waiting = set()
    visible = None
    first_run = not seen and not rentals
    if not watch and (first_run or report or rentals or preview_dir):
        saved = sorted((l for l in matches if l.key in saved_keys), key=lambda l: l.monthly_cost)
        rest = [l for l in matches if l not in top and l.key not in saved_keys]
        n_new = len([l for l in matches if l.key in new_keys])
        now = dt.datetime.now(ZoneInfo("Europe/London"))
        edition = "Morning" if now.hour < 12 else "Evening"
        heading = "👋 PisoBot is live" if first_run else f"🏠 PisoBot · {edition} report"
        if rentals:
            heading = "🔑 PisoBot · Private rentals"
        title = (f"<b>{heading}</b>\n<i>{now:%a %d %b · %H:%M %Z}</i>\n\n"
                 f"<b>{len(matches)} homes</b> · 🆕 {n_new} new · ❤️ {len(saved)} saved")
        if failures:
            title += f"\n⚠️ Partial results: unavailable sources {telegram.e(', '.join(failures))}"
        changes = {l.key: l for l, reason in to_send if reason == "drop"}
        changes.update({l.key: l for l in saved_changes})
        compact = scheduled and not first_run and n_new == 0
        report_top, report_saved, report_rest = top, saved, rest
        if compact:
            icon = "🌅" if edition == "Morning" else "🌙"
            title = f"{icon} {edition} report: 0 new homes · {len(matches)} tracked"
            if changes:
                title += f" · {len(changes)} price updates"
            if failures:
                title += f"\n⚠️ Partial results: unavailable sources {telegram.e(', '.join(failures))}"
            report_top = ranking.best(list(changes.values()))
            report_saved = []
            report_rest = [l for l in changes.values() if l not in report_top]
        # Local rental digests visibly include top cards, every saved home, and only the
        # first RENTALS_SHOWN alternatives. Do not acknowledge undelivered queue entries
        # merely because they were candidates for a truncated digest (no live local map).
        visible = None
        if archive.enabled() and rentals:
            alternatives = sorted((l for l in report_rest if l.tenure == "rent"), key=ranking.score)
            visible = {l.key for l in report_top + report_saved + alternatives[:telegram.RENTALS_SHOWN]}
            report_waiting = {l.key for l, _ in to_send if l.key not in visible}
        print(f"report: {len(matches)} matches")
        if not dry_run or preview_dir:
            if compact and not changes and not preview_dir:
                telegram.send_text(title, silent=True)
                print("Sent silent no-new summary; unchanged listing cards suppressed")
            else:
                send_report(report_top, report_saved, report_rest, title, flags_of, preview_dir,
                            map_url=mapgen.url(rentals) if config.MAP_URL else None)
        reported_keys = {l.key for l in report_top}
        saved_changes = [l for l in saved_changes if l.key not in reported_keys]
        if live:
            for l in matches:
                if visible is None or l.key in visible:
                    seen[l.key] = _record(l, today, alerted=True, prev=seen.get(l.key))
                    if local_rentals:
                        token = archive.intent_token(person_id, l.key)
                        if token:
                            seen[l.key]["delivery_intent"] = token
            if rentals:
                cache.put("meta", users.scoped("last_rentals_report"), _now())
            else:
                p["last_report"] = _now()
        to_send = []

    # 6. ... otherwise individual alerts
    print(f"matches to alert: {len(to_send)} (sending up to {config.MAX_ALERTS_PER_RUN})")
    delivered = set()
    delivery_errors = []
    for l, reason in to_send:
        attempts_left = alert_budget.get("attempts", alert_budget.get("remaining", config.MAX_ALERTS_PER_RUN))
        if attempts_left <= 0:
            break
        check_stop()
        if deadline is not None and clock() >= deadline:
            break
        print(f"  + [{reason}] £{l.monthly_cost:,.0f} {l.travel_minutes}min crime={l.crime_score} "
              f"{l.bedrooms}b {l.address[:60]} {l.url}")
        if not live:
            continue
        _ensure_photos(l)
        if reason == "new" and archive.enabled():
            _set_urgency(l)
        alert_budget["attempts"] = attempts_left - 1
        try:
            if reason == "new":
                telegram.send_listing(l, reason)
                send_state = "sent"
            else:
                send_state = _send_change(l, reason)
                if send_state in ("busy", "rejected"):
                    continue
        except RuntimeError as ex:
            if not (watch and rentals and os.environ.get("STORAGE") == "local"):
                raise
            print(f"Listing delivery failed for {l.key}: {telegram.delivery_failure_label(ex)}",
                  file=sys.stderr)
            delivery_errors.append(ex)
            if local_rentals:
                archive.record_delivery_failure(person_id, l.key, settings)
            telegram.throttle()
            continue
        if send_state == "sent":
            alert_budget["remaining"] -= 1
        seen[l.key] = _record(l, today, alerted=True, prev=seen.get(l.key))
        if local_rentals:
            token = archive.intent_token(person_id, l.key)
            if token:
                seen[l.key]["delivery_intent"] = token
        if watch or local_rentals:
            save_seen(seen)
        if local_rentals:
            archive.record_verdicts(person_id, [_verdict_row(l, None)], settings, acknowledged=True)
        delivered.add(l.key)
        if send_state == "sent":
            telegram.throttle()
    if live:
        for l in saved_changes:
            _ensure_photos(l)
            if _send_change(l, "saved") == "sent":
                telegram.throttle()
        for info in gone:
            telegram.send_text(f'❤️ <b>Saved home no longer listed</b>\n<a href="{info.get("url")}">'
                               f'{telegram.e(info.get("address") or "listing")}</a>')
            telegram.throttle()

    cache.save()
    if live:
        save_seen(seen)
        prefs.save(p)
        if archive.enabled() and verdicts:
            waiting = ({l.key for l, _ in to_send} - delivered) | report_waiting
            settled = [r for r in verdicts if r[1] == "matched" and r[0] not in waiting]
            if settled:
                # Only a receipt for THIS obligation may recover an interrupted DB ack.
                # Same price / an old silent baseline is never evidence this notice was sent.
                acknowledged = delivered | (visible or set())
                acknowledged |= {l.key for l in matches
                                 if seen_before.get(l.key, {}).get("delivery_intent") is not None
                                 and seen_before[l.key]["delivery_intent"] == archive.intent_token(person_id, l.key)}
                receipts = [r for r in settled if r[0] in acknowledged]
                bookkeeping = [r for r in settled if r[0] not in acknowledged]
                if receipts:
                    archive.record_verdicts(person_id, receipts, settings, acknowledged=True)
                if bookkeeping:
                    archive.record_verdicts(person_id, bookkeeping, settings)
    result = {"matches": len(matches), "failures": failures, "enrich_failures": enrich_failures,
              "complete": not failures and (local_rentals or not enrich_failures), "fresh": fresh,
              "baseline_fresh": baseline_fresh, "degraded": enrich_failures + deferred_failures,
              "processed": processed, "sent": len(delivered)}
    if delivery_errors:
        # The persisted batch can yield to other inventory without hiding the cycle error.
        # Only send-phase failures receive this marker; failed storage/evaluation never does.
        delivery_errors[0].pisobot_delivery_result = result
        raise delivery_errors[0] from None
    return result


def main():
    ap = argparse.ArgumentParser(description="London shared-ownership alert bot")
    ap.add_argument("--sources", help="comma list: " + ",".join(SOURCES))
    ap.add_argument("--rentals", action="store_true",
                    help="search private rentals only (" + ",".join(RENTAL_SOURCES) + ") and send their report")
    ap.add_argument("--dry-run", action="store_true", help="print matches, don't send or update state")
    ap.add_argument("--report", action="store_true", help="send the full report (top picks + list)")
    ap.add_argument("--restart", action="store_true", help="clear hidden choices and run a fresh report")
    ap.add_argument("--reset", action="store_true", help="factory reset all preferences and state, then run report")
    ap.add_argument("--scheduled", action="store_true", help="send the UK report if its slot is due (else exit)")
    ap.add_argument("--preview", metavar="DIR", help="write the report to DIR instead of sending it")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    rental_defaults = config.WATCH_SOURCES.split(",") if os.environ.get("STORAGE") == "local" else RENTAL_SOURCES
    selected = ([s.strip() for s in a.sources.split(",") if s.strip()] if a.sources is not None
                else rental_defaults if a.rentals else DEFAULT_SOURCES)
    if not selected or any(s not in SOURCES for s in selected):
        ap.error("Select at least one registered source")
    if a.rentals:
        if a.restart or a.reset or a.scheduled:
            ap.error("--rentals cannot be combined with --restart, --reset or --scheduled")
        if any(s not in {*RENTAL_SOURCES, *LOCAL_RENTAL_SOURCES} for s in selected):
            ap.error("--rentals requires rental sources")
        run(selected, a.dry_run, a.verbose, report=True, preview_dir=a.preview, rentals=True)
    elif a.restart:
        prefs.restart(full=False)
        save_seen({})
        run(selected, dry_run=False, verbose=a.verbose, report=True)
    elif a.reset:
        prefs.restart(full=True)
        save_seen({})
        run(selected, dry_run=False, verbose=a.verbose, report=True)
    elif a.scheduled:
        if a.dry_run or a.preview or a.report:
            ap.error("--scheduled cannot be combined with other run modes")
        due = report_due(prefs.load())
        print(f"Scheduled check: report due={due}")
        if due:
            run(selected, False, a.verbose, report=True, scheduled=True)
    else:
        run(selected, a.dry_run, a.verbose, a.report, a.preview)


if __name__ == "__main__":
    main()
