import argparse
import copy
import datetime as dt
import json
import math
import os
import sys
import traceback
from zoneinfo import ZoneInfo

from requests import RequestException

from . import cache, collage, config, costs, enrich, mapgen, prefs, ranking, telegram
from .sources import lq, nhg, onthemarket, sharetobuy
from .sources import detail_fields

SOURCES = {"stb": sharetobuy, "lq": lq, "nhg": nhg, "otm": onthemarket}
SEEN_PATH = os.path.join(os.path.dirname(__file__), "..", "state", "seen.json")


def load_seen():
    try:
        with open(SEEN_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen):
    os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=1, sort_keys=True)


def _cost_ok(l):
    return costs.compute(l) and l.monthly_cost <= config.MAX_MONTHLY_COST


def passes_basic(l):
    if l.bedrooms < config.MIN_BEDROOMS or l.excluded_by_keywords():
        return "beds/keywords"
    if l.lat is None or l.lng is None:
        return "no location"
    if enrich.distance_km(l.lat, l.lng) > config.SEARCH_RADIUS_KM:
        return "too far"
    refine = getattr(SOURCES[l.source], "refine", None)
    if refine:
        # cheap gate first so only plausible listings cost a details-page fetch
        if not _cost_ok(copy.deepcopy(l)):
            return "cost"
        if not refine(l):
            return "details"
        if l.excluded_by_keywords():
            return "beds/keywords"
    for key, value in detail_fields(l.description).items():
        if getattr(l, key) is None:
            setattr(l, key, value)
    if not _cost_ok(l):
        return "cost"
    return None


def passes_area(l):
    j = enrich.journey(l.lat, l.lng)
    if not j or j["minutes"] > config.MAX_TRAVEL_MINUTES:
        return "travel"
    l.travel_minutes, l.route, l.station, l.walk_minutes = j["minutes"], j["route"], j["station"], j["walk"]
    l.crime_score = enrich.crime_score(l.lat, l.lng)
    if l.crime_score > config.MAX_CRIME_SCORE:
        return "crime"
    return None


def is_duplicate(l, records):
    """Same flat on another portal: same beds, price within 2%, within ~300 m."""
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
    return {"alerted": alerted, "cost": round(l.monthly_cost), "first": prev.get("first", _now()),
            "last": today, "url": l.url, "beds": l.bedrooms, "price": l.share_price or l.rent,
            "lat": l.lat, "lng": l.lng, "address": l.address}


def _ensure_photos(l):
    loader = getattr(SOURCES.get(l.source), "photos", None)
    if loader and len(l.images) < 3:
        try:
            loaded = loader(l)
            if loaded:
                l.images = list(dict.fromkeys([*l.images, *loaded]))
        except (RequestException, ValueError) as ex:
            print(f"Gallery unavailable for {l.key}: {ex}", file=sys.stderr)


def send_report(top, saved, rest, title, flags_of, preview_dir=None):
    messages = telegram.report_messages(top, rest, saved, title, flags_of)
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


def run(selected, dry_run, verbose, report=False, preview_dir=None, scheduled=False):
    seen = load_seen()
    p = prefs.load()
    live = not dry_run and not preview_dir
    alerted = [v for v in seen.values() if v.get("alerted")]
    listings, failures = [], []
    for name in selected:
        try:
            got = SOURCES[name].fetch()
            print(f"[{name}] {len(got)} listings")
            listings += got
        except Exception:
            failures.append(name)
            print(f"[{name}] FAILED\n{traceback.format_exc()}", file=sys.stderr)
    if failures and len(failures) == len(selected):
        raise RuntimeError("Every source failed; report delivery is incomplete and will be retried")

    try:
        enrich.geocode(listings)
    except Exception as ex:
        print(f"geocoding failed: {ex}", file=sys.stderr)

    # 1. everything that currently fits the criteria, one entry per physical flat
    matches, kept, rejected = [], [], {}
    for l in listings:
        why = passes_basic(l)
        if not why:
            try:
                why = passes_area(l)
            except Exception as ex:  # API hiccup: try again next run
                print(f"enrich failed for {l.key}: {ex}", file=sys.stderr)
                continue
        if why:
            rejected[why] = rejected.get(why, 0) + 1
            if verbose:
                cost = round(l.monthly_cost) if l.monthly_cost else None
                print(f"  - {why:13} {l.key} {l.address[:60]} {cost}")
            continue
        rec = _record(l, "", alerted=True)
        if is_duplicate(l, kept):
            continue
        kept.append(rec)
        matches.append(l)
    print(f"rejected: {rejected}")
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
    print(f"current matches: {len(matches)} (+{len(hidden)} hidden)")

    # 3. saved homes: price changes and disappearances
    by_key = {l.key: l for l in matches}
    fetched_keys = {l.key for l in listings}
    saved_changes, gone = [], []
    for key, info in list(p["saved"].items()):
        l = by_key.get(key)
        if l:
            info["missing"] = 0
            if abs((info.get("cost") or 0) - l.monthly_cost) >= config.PRICE_DROP_ALERT:
                saved_changes.append(l)
                info["cost"] = round(l.monthly_cost)
        elif key in fetched_keys:
            info["missing"] = 0   # still advertised, just outside the filters now
        elif key.split(":")[0] not in failures:
            info["missing"] = info.get("missing", 0) + 1
            if info["missing"] >= 2:
                gone.append(info)
                del p["saved"][key]

    # 4. what's new or cheaper since last time
    to_send = []
    for l in matches:
        prev = seen.get(l.key)
        if l.key in p["saved"]:
            pass
        elif prev is None:
            if not is_duplicate(l, alerted):
                to_send.append((l, "new"))
        elif prev.get("alerted") and prev.get("cost", 0) - l.monthly_cost >= config.PRICE_DROP_ALERT:
            to_send.append((l, "drop"))
        if prev and live:
            prev["last"] = today
    to_send.sort(key=lambda x: ranking.score(x[0]))

    # 🆕 = first seen since the last report (older records only have a date: treat as reported)
    if not p.get("last_report") and seen:
        p["last_report"] = max(v.get("first", "") for v in seen.values())[:10] + "T23:59"
    new_keys = {l.key for l in matches
                if l.key not in seen or seen[l.key].get("first", "") > p.get("last_report", "")}
    saved_keys = set(p["saved"])

    def flags_of(l):
        return " ".join(f for f, on in (("🆕", l.key in new_keys), ("❤️", l.key in saved_keys)) if on)

    top = ranking.best(matches)
    try:
        mapgen.write(matches, top, saved_keys, new_keys)
    except Exception:
        print(f"map page failed\n{traceback.format_exc()}", file=sys.stderr)

    # 5. report (first run, twice-daily schedule, or --report, which /report dispatches) ...
    first_run = not seen
    if first_run or report or preview_dir:
        saved = sorted((l for l in matches if l.key in saved_keys), key=lambda l: l.monthly_cost)
        rest = [l for l in matches if l not in top and l.key not in saved_keys]
        n_new = len([l for l in matches if l.key in new_keys])
        now = dt.datetime.now(ZoneInfo("Europe/London"))
        edition = "Morning" if now.hour < 12 else "Evening"
        heading = "👋 PisoBot is live" if first_run else f"🏠 PisoBot · {edition} report"
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
                send_report(report_top, report_saved, report_rest, title, flags_of, preview_dir)
        reported_keys = {l.key for l in report_top}
        saved_changes = [l for l in saved_changes if l.key not in reported_keys]
        if live:
            for l in matches:
                seen[l.key] = _record(l, today, alerted=True, prev=seen.get(l.key))
            p["last_report"] = _now()
        to_send = []

    # 6. ... otherwise individual alerts
    print(f"matches to alert: {len(to_send)} (sending up to {config.MAX_ALERTS_PER_RUN})")
    for l, reason in to_send[: config.MAX_ALERTS_PER_RUN]:
        print(f"  + [{reason}] £{l.monthly_cost:,.0f} {l.travel_minutes}min crime={l.crime_score} "
              f"{l.bedrooms}b {l.address[:60]} {l.url}")
        if not live:
            continue
        _ensure_photos(l)
        telegram.send_listing(l, reason)
        telegram.throttle()
        seen[l.key] = _record(l, today, alerted=True, prev=seen.get(l.key))
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


def main():
    ap = argparse.ArgumentParser(description="London shared-ownership alert bot")
    ap.add_argument("--sources", default=",".join(SOURCES), help="comma list: " + ",".join(SOURCES))
    ap.add_argument("--dry-run", action="store_true", help="print matches, don't send or update state")
    ap.add_argument("--report", action="store_true", help="send the full report (top picks + list)")
    ap.add_argument("--scheduled", action="store_true", help="send the UK report if its slot is due (else exit)")
    ap.add_argument("--preview", metavar="DIR", help="write the report to DIR instead of sending it")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    selected = [s for s in a.sources.split(",") if s]
    if a.scheduled:
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
