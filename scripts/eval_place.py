#!/usr/bin/env python3
"""Print local amenity tiles and postcode-district overviews for cached advert coordinates."""
import argparse
import json
import os
import sys

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
    import statistics
    from pisobot import models, place
    from pisobot.place_feature import _overview_text

    sample = [(row, facts[i]) for i, row in enumerate(rows) if facts[i] is not None]
    n = len(sample)
    green = sum(
        bool((data.get("amenities", {}).get("park") or {}).get("nearest")
             and data["amenities"]["park"]["nearest"]["distance_m"] <= place.AREA_AMENITY_RADIUS_M)
        for _, data in sample)
    poi = {}
    for label, categories in (
            ("cafes", ("cafe",)), ("pubs", ("pub",)), ("restaurants", ("restaurant",)),
            ("shops", ("supermarket", "convenience", "shop", "pharmacy"))):
        counts = [sum((data.get("amenities", {}).get(cat) or {}).get("count", 0) for cat in categories)
                  for _, data in sample]
        poi[label] = round(statistics.mean(counts), 1) if counts else None
    roads = [bool(data.get("main_road") and data["main_road"]["distance_m"] <= 200)
             for _, data in sample]
    rails = [bool(data.get("railway") and data["railway"]["distance_m"] <= 100)
             for _, data in sample]
    rents = [float(row["rent"]) for row, _ in sample
             if row.get("bedrooms") == 1 and type(row.get("rent")) in (int, float) and row["rent"] > 0]
    crime = [row["crime_score"] for row, _ in sample
             if type(row.get("crime_score")) is int and row["crime_score"] >= 0]
    crime_median = statistics.median(crime) if crime else None
    comparison = [record["crime_median"] for record in areas.values()
                  if record.get("crime_median") is not None]
    record = {
        "area": area, "area_name": models.LONDON_AREAS.get(area, area), "sample_size": n,
        "green_space_share_pct": round(100 * green / n) if n else None,
        "poi_density_per_location": poi,
        "main_road_within_200m_pct": round(100 * sum(roads) / n) if n else None,
        "surface_rail_within_100m_pct": round(100 * sum(rails) / n) if n else None,
        "tfl_rail_lines_mean": None,
        "one_bed_median_rent": statistics.median(rents) if rents else None,
        "one_bed_advert_count": len(rents),
        "market_source": "selected cached adverts",
        "crime_median": crime_median,
        "crime_comparison_median": statistics.median(comparison) if len(comparison) >= 3 else None,
        "crime_comparison_area_count": len(comparison) if len(comparison) >= 3 else 0,
    }
    record["text"] = _overview_text(record)
    return record


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
