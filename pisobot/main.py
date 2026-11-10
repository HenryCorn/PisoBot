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
import traceback
from types import SimpleNamespace
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from requests import RequestException

from . import (archive, cache, collage, config, costs, d1, enrich, intelligence, mapgen, prefs, ranking, renter, taste,
               telegram, users)
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


def staged(l):
    """Private rentals on the VPS are judged coordinates-first: commute and crime before any details
    page is fetched. Everything else (the cloud path included) keeps the original order."""
    return archive.enabled() and l.tenure == "rent" and l.bills is not None


def passes_basic(l, sweep=False, stage=None):
    """stage None: every check in the original order. "cheap": checks needing no request (beds,
    keywords, location, distance, rent). "details": the details page, then the final cost.
    sweep=True: the advert comes from the deep sweep, so the person's radius is not a filter here
    (the commute limit is); only the swept area is."""
    if stage in (None, "cheap"):
        if l.bedrooms < config.MIN_BEDROOMS:
            return "beds"
        if l.excluded_by_keywords():
            return "keywords"
        if l.lat is None or l.lng is None:
            return "no location"
        distance = (_km(l.lat, l.lng, users.DEFAULTS["TARGET_LAT"], users.DEFAULTS["TARGET_LNG"])
                    if users.current() is not None and l.tenure == "rent" else enrich.distance_km(l.lat, l.lng))
        if distance > (config.DEEP_SWEEP_MILES * 1.609344 if sweep else config.SEARCH_RADIUS_KM):
            return "too far"
        refine = getattr(SOURCES[l.source], "refine", None)
        if stage == "cheap" or refine:
            # cheap gate first so only plausible listings cost a details-page fetch
            why = _cost_reject(copy.deepcopy(l), early=True)
            if why:
                return why
    if stage == "cheap":
        return None
    refine = getattr(SOURCES[l.source], "refine", None)
    if refine:
        if not refine(l):
            return "details"
        if l.excluded_by_keywords():
            return "keywords"
    for key, value in detail_fields(l.description).items():
        if getattr(l, key) is None:
            setattr(l, key, value)
    return _cost_reject(l)


def zone_prefilter(l):
    """HOOK(zone): a cheap commute-zone prefilter that runs before the TfL call in passes_area.
    When pisobot/zone.py (could_reach) exists, return could_reach(l.lat, l.lng, <this person's
    destination>, config.MAX_TRAVEL_MINUTES) here; False rejects as "travel" without a TfL request.
    Until then every advert goes on to TfL."""
    return True


def passes_area(l):
    if not zone_prefilter(l):
        return "travel"
    j = enrich.journey(l.lat, l.lng)
    if not j:
        return "travel"
    # set before the limit is applied so a rejection still records the minutes it was judged on
    l.travel_minutes, l.route, l.station, l.walk_minutes = j["minutes"], j["route"], j["station"], j["walk"]
    l.changes = j.get("changes", 0)
    if j["minutes"] > config.MAX_TRAVEL_MINUTES:
        return "travel"
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
        jpeg = None
        if 1 <= i <= len(top):
            l = top[i - 1]
            _ensure_photos(l)
            jpeg = collage.build(l)
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
            telegram.send_card(jpeg, text, kb, f"🏠 Listing #{i}")
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
    """Short fingerprint of a person's search settings: verdicts are only current for these."""
    person = person if person is not None else users.current()
    return hashlib.sha1(json.dumps(person.params if person else {}, sort_keys=True).encode()).hexdigest()[:12]


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


def _run(selected, dry_run, verbose, report=False, preview_dir=None, scheduled=False, rentals=False,
         watch=False, baseline=False, stop_event=None, prefetched=None, sweep=False):
    """prefetched: (listings, failures) shared by several people's runs in one cycle; each run
    works on its own copy so per-person refinement and costs never leak between people.
    sweep: the listings come from the market archive (deep sweep), not a portal fetch; the person's
    radius does not filter them, and the run makes no funnel record, no "watch is live" message and
    does not fail on enrichment errors (they are retried)."""
    def check_stop():
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("PisoBot is stopping")

    check_stop()
    seen = load_seen()
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
    rental_keys = set()
    verdicts = []
    try:
        for l in listings:
            check_stop()
            if l.tenure == "rent" and l.key in rental_keys:
                reject(l.source, "duplicate_id")
                continue
            if l.tenure == "rent":
                rental_keys.add(l.key)
            order = staged(l)
            if order:
                why = passes_basic(l, sweep=sweep, stage="cheap")
            elif sweep:
                why = passes_basic(l, sweep=True)
            else:
                why = passes_basic(l)
            if not why:
                try:
                    why = passes_area(l)
                except Exception as ex:  # API hiccup: try again next run
                    print(f"enrich failed for {l.key}: {type(ex).__name__}", file=sys.stderr)
                    enrich_failures += 1
                    reject(l.source, "enrichment")
                    verdicts.append(_verdict_row(l, "enrichment"))
                    continue
            if not why and order:
                why = passes_basic(l, sweep=sweep, stage="details")
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
                archive.record_verdicts(users.current_chat() or users.owner_chat() or "owner", verdicts,
                                        settings_key())
            except Exception as ex:
                print(f"archive verdicts failed: {type(ex).__name__}: {ex}", file=sys.stderr)
    print(f"rejected: {rejected}")
    print(f"funnel: {json.dumps(funnel, sort_keys=True)}")
    if baseline and not sweep and (failures or enrich_failures):
        raise RuntimeError("Initial rental baseline incomplete; retrying without sending listing alerts")
    enrich.assign_crime_percentiles(matches)

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
    to_send.sort(key=lambda x: ranking.score(x[0]))

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

    if baseline:
        to_send = []
        if live:
            for l in matches:
                seen[l.key] = _record(l, today, alerted=True, prev=seen.get(l.key))
            save_seen(seen)
            if not sweep:
                telegram.send_text(f"Rental watch is live: {len(matches)} current matches recorded. "
                                   "Only new matches and price drops will alert; /report shows them all.")
    if watch:
        saved_changes, gone = [], []

    # 5. report (first run, twice-daily schedule, or --report, which /report dispatches) ...
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
                seen[l.key] = _record(l, today, alerted=True, prev=seen.get(l.key))
            if rentals:
                cache.put("meta", users.scoped("last_rentals_report"), _now())
            else:
                p["last_report"] = _now()
        to_send = []

    # 6. ... otherwise individual alerts
    print(f"matches to alert: {len(to_send)} (sending up to {config.MAX_ALERTS_PER_RUN})")
    for l, reason in to_send[: config.MAX_ALERTS_PER_RUN]:
        check_stop()
        print(f"  + [{reason}] £{l.monthly_cost:,.0f} {l.travel_minutes}min crime={l.crime_score} "
              f"{l.bedrooms}b {l.address[:60]} {l.url}")
        if not live:
            continue
        _ensure_photos(l)
        if reason == "new" and archive.enabled():
            _set_urgency(l)
        telegram.send_listing(l, reason)
        seen[l.key] = _record(l, today, alerted=True, prev=seen.get(l.key))
        if watch:
            save_seen(seen)
        telegram.throttle()
    if sweep and live and archive.enabled() and len(to_send) > config.MAX_ALERTS_PER_RUN:
        # Matches whose alert hit the cap are judged again next time rather than lost
        try:
            archive.record_verdicts(users.current_chat() or users.owner_chat() or "owner",
                                    [_verdict_row(l, "unsent") for l, _ in to_send[config.MAX_ALERTS_PER_RUN:]],
                                    settings_key())
        except Exception as ex:
            print(f"archive verdicts failed: {type(ex).__name__}: {ex}", file=sys.stderr)
    if live:
        for l in saved_changes:
            _ensure_photos(l)
            telegram.send_listing(l, "saved")
            telegram.throttle()
        for info in gone:
            telegram.send_text(f'❤️ <b>Saved home no longer listed</b>\n<a href="{info.get("url")}">'
                               f'{telegram.e(info.get("address") or "listing")}</a>')
            telegram.throttle()

    cache.save()
    if live:
        save_seen(seen)
        prefs.save(p)
    return {"matches": len(matches), "failures": failures, "enrich_failures": enrich_failures,
            "complete": not failures and not enrich_failures, "fresh": fresh}


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
