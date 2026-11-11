#!/usr/bin/env python3
"""Print local amenity tiles and postcode-district overviews for cached advert coordinates."""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def load_listings(path):
    with open(path, "rb") as source:
        cache = json.load(source)
    seen, listings = set(), []
    for entry in (cache.get("rent_search") or {}).values():
        if not isinstance(entry, dict):
            continue
        for listing in entry.get("listings") or []:
            if not isinstance(listing, dict):
                continue
            lat, lng = listing.get("lat"), listing.get("lng")
            if type(lat) not in (int, float) or type(lng) not in (int, float):
                continue
            point = (round(lat, 6), round(lng, 6))
            if point not in seen:
                seen.add(point)
                listings.append({**listing, "lat": point[0], "lng": point[1]})
    return listings


def _overview(area, rows, facts, areas):
    from pisobot.place_feature import _area_record

    prepared = [{**row, "key": f"eval:{index}"} for index, row in enumerate(rows)]
    fact_cache = {row["key"]: facts[index] for index, row in enumerate(prepared) if facts[index] is not None}
    comparison = [record["crime_median"] for record in areas.values() if record.get("crime_median") is not None]
    return _area_record(area, prepared, fact_cache, {}, comparison, time.time(),
                        market_source="selected cached adverts")


def main(argv=None, out=print):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", default=os.path.join(ROOT, "state", "cache.json"))
    parser.add_argument("--index", default=os.path.join(ROOT, "state", "place-index.sqlite3"))
    parser.add_argument("--sample", type=int, default=20)
    args = parser.parse_args(argv)
    if args.sample < 1:
        parser.error("--sample must be positive")
    try:
        listings = load_listings(args.cache)
    except (OSError, ValueError, TypeError) as error:
        out(f"Could not read advert cache ({type(error).__name__}).")
        return 2
    if not listings:
        out("No cached rental advert coordinates found.")
        return 1

    from pisobot import place
    from pisobot import models
    from pisobot.place_feature import district_for_address

    listings = [listing for listing in listings if district_for_address(listing.get("address"))][:args.sample]
    if not listings:
        out("No cached coordinates with a supported postcode district.")
        return 1
    facts = []
    by_area = {}
    for listing in listings:
        try:
            value = place.local_osm_facts(listing["lat"], listing["lng"], args.index)
        except (OSError, ValueError, TypeError):
            value = None
        facts.append(value)
        area = district_for_address(listing.get("address"))
        if area:
            by_area.setdefault(area, []).append((listing, value))
    summary_inputs = {
        area: ( [row for row, _ in group], [value for _, value in group])
        for area, group in by_area.items()
    }
    initial = {
        area: _overview(area, rows, values, {})
        for area, (rows, values) in summary_inputs.items()
    }
    summaries = {
        area: _overview(area, rows, values, initial)
        for area, (rows, values) in summary_inputs.items()
    }
    out(f"Place tiles and postcode-district overviews for {len(listings)} cached coordinates")
    for index, listing in enumerate(listings, 1):
        tile = place.amenities_line(facts[index - 1]) or "No requested amenity within 2 km"
        area = district_for_address(listing.get("address"))
        summary = summaries.get(area)
        out(f"{index:02d}. {tile}")
        if summary:
            out(f"    Area: {area} {models.LONDON_AREAS.get(area, area)} — {summary['text']}")
        else:
            out("    Area overview unavailable: address has no supported postcode district.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
