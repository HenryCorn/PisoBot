import argparse
import copy
import datetime as dt
import json
import math
import os
import sys
import traceback

from . import cache, collage, config, costs, enrich, ranking, telegram
from .sources import lq, nhg, onthemarket, sharetobuy

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
    if not _cost_ok(l):
        return "cost"
    return None


def passes_area(l):
    l.travel_minutes = enrich.travel_minutes(l.lat, l.lng)
    if l.travel_minutes is None or l.travel_minutes > config.MAX_TRAVEL_MINUTES:
        return "travel"
    l.crime_score = enrich.crime_score(l.lat, l.lng)
    if l.crime_score > config.MAX_CRIME_SCORE:
        return "crime"
    return None


def is_duplicate(l, alerted):
    """Same flat on another portal: same beds, price within 2%, within ~300 m."""
    price = l.share_price or l.rent or 0
    for a in alerted:
        if a.get("beds") != l.bedrooms or not a.get("lat") or not price:
            continue
        if abs(a.get("price", 0) - price) <= 0.02 * price and _km(a["lat"], a["lng"], l.lat, l.lng) <= 0.3:
            return True
    return False


def _km(lat1, lng1, lat2, lng2):
    return math.hypot((lat1 - lat2) * 111.2, (lng1 - lng2) * 111.2 * math.cos(math.radians(lat1)))


def _record(l, today, alerted, prev=None):
    prev = prev or {}
    return {"alerted": alerted, "cost": round(l.monthly_cost), "first": prev.get("first", today),
            "last": today, "url": l.url, "beds": l.bedrooms, "price": l.share_price or l.rent,
            "lat": l.lat, "lng": l.lng}


def send_report(matches, title, preview_dir=None):
    top = ranking.best(matches)
    rest = sorted((m for m in matches if m not in top), key=lambda l: l.monthly_cost)
    jpeg = collage.build(top)
    caption = f"🏆 Top {len(top)} of {len(matches)} matches"
    texts = telegram.report_messages(top, rest, title)
    if preview_dir:
        os.makedirs(preview_dir, exist_ok=True)
        if jpeg:
            with open(os.path.join(preview_dir, "top.jpg"), "wb") as f:
                f.write(jpeg)
        with open(os.path.join(preview_dir, "report.html"), "w", encoding="utf-8") as f:
            f.write("\n<hr>\n".join(t.replace("\n", "<br>\n") for t in texts))
        print(f"report preview written to {preview_dir}")
        return
    if jpeg:
        telegram.send_photo_bytes(jpeg, caption)
        telegram.throttle()
    for t in texts:
        telegram.send_text(t)
        telegram.throttle()


def run(selected, dry_run, verbose, report=False, preview_dir=None):
    seen = load_seen()
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
    print(f"current matches: {len(matches)}")

    # 2. what's new or cheaper since last time
    today = dt.date.today().isoformat()
    to_send = []
    for l in matches:
        prev = seen.get(l.key)
        if prev is None:
            if not is_duplicate(l, alerted):
                to_send.append((l, "new"))
        elif prev.get("alerted") and prev.get("cost", 0) - l.monthly_cost >= config.PRICE_DROP_ALERT:
            to_send.append((l, "drop"))
        elif prev:
            prev["last"] = today
    to_send.sort(key=lambda x: ranking.score(x[0]))

    first_run = not seen
    if first_run or report or preview_dir:
        # A report covers everything, so no separate per-listing alerts this run.
        title = ("👋 <b>PisoBot is live.</b> From now on you'll get one alert per new listing."
                 if first_run else f"📋 <b>PisoBot report</b> · {today}")
        print(f"report: {len(matches)} matches")
        if not dry_run or preview_dir:
            send_report(matches, title, preview_dir)
        to_send = []
        if not dry_run and not preview_dir:
            for l in matches:
                seen[l.key] = _record(l, today, alerted=True, prev=seen.get(l.key))

    print(f"matches to alert: {len(to_send)} (sending up to {config.MAX_ALERTS_PER_RUN})")
    for l, reason in to_send[: config.MAX_ALERTS_PER_RUN]:
        print(f"  + [{reason}] £{l.monthly_cost:,.0f} {l.travel_minutes}min crime={l.crime_score} "
              f"{l.bedrooms}b {l.address[:60]} {l.url}")
        if dry_run:
            continue
        telegram.send_listing(l, reason)
        telegram.throttle()
        seen[l.key] = _record(l, today, alerted=True, prev=seen.get(l.key))

    if failures and not dry_run and len(failures) == len(selected):
        telegram.send_text("⚠️ PisoBot: every source failed this run: " + ", ".join(failures))
    cache.save()
    if not dry_run and not preview_dir:
        save_seen(seen)


def main():
    ap = argparse.ArgumentParser(description="London shared-ownership alert bot")
    ap.add_argument("--sources", default=",".join(SOURCES), help="comma list: " + ",".join(SOURCES))
    ap.add_argument("--dry-run", action="store_true", help="print matches, don't send or update seen.json")
    ap.add_argument("--report", action="store_true", help="send the full report (top picks + list)")
    ap.add_argument("--preview", metavar="DIR", help="write the report to DIR instead of sending it")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    run([s for s in a.sources.split(",") if s], a.dry_run, a.verbose, a.report, a.preview)


if __name__ == "__main__":
    main()
