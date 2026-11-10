#!/usr/bin/env python3
"""Evaluate place facts for cached rental adverts with coordinates.

    python scripts/eval_place.py --cache state/cache.json [--sample 20] [--interval 10]

Only the selected latitude/longitude pairs are sent to Overpass, TfL and Defra's point services.
The input cache is opened read-only; evaluation results use an isolated temporary cache.
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["PISOBOT_STATE_DIR"] = "/private/tmp/pisobot-place-eval-round2"

from pisobot import cache, place  # noqa: E402


def load_coordinates(path):
    with open(path, "rb") as f:
        data = json.load(f)
    seen, coordinates = set(), []
    for entry in (data.get("rent_search") or {}).values():
        if not isinstance(entry, dict):
            continue
        for listing in entry.get("listings") or []:
            lat, lng = listing.get("lat"), listing.get("lng")
            if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
                continue
            key = (round(lat, 6), round(lng, 6))
            if key not in seen:
                seen.add(key)
                coordinates.append(key)
    return coordinates


def main(argv=None, out=print, facts_for=place.facts, sleep=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache", default=os.path.join(ROOT, "state", "cache.json"))
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--interval", type=float, default=10,
                        help="minimum seconds between location evaluations (default: 10)")
    args = parser.parse_args(argv)
    if args.sample < 1 or args.interval < 10:
        parser.error("--sample must be positive and --interval must be at least 10 seconds")
    try:
        coordinates = load_coordinates(args.cache)[:args.sample]
    except (OSError, ValueError) as error:
        out(f"Could not read advert coordinates: {error}")
        return 2
    if not coordinates:
        out("No cached rental advert coordinates found.")
        return 1
    out(f"Place facts for {len(coordinates)} cached advert coordinates")
    failures = 0
    for index, (lat, lng) in enumerate(coordinates):
        try:
            facts = facts_for(lat, lng)
            out(f"{index + 1:02d}. {place.card_line(facts) or 'No mapped place facts'}")
            main_road = facts.get("main_road") if facts else None
            road_text = (f"{main_road['class']} ~{main_road['distance_m']}m"
                         if main_road else f"none within {place.OVERPASS_ROAD_RADIUS_M}m")
            noise = (facts.get("noise") or {}) if facts else {}
            road_noise = noise.get("road_lnight_db")
            rail_noise = noise.get("rail_lnight_db")
            road_value = f"{road_noise:g} dB" if road_noise is not None else "no value"
            rail_value = f"{rail_noise:g} dB" if rail_noise is not None else "no value"
            out(f"    main road: {road_text}; Defra Lnight 2021/4m: road {road_value}, rail {rail_value}")
            cache.save()
        except Exception as error:
            failures += 1
            out(f"{index + 1:02d}. Place facts unavailable ({type(error).__name__}: {error})")
        if index + 1 < len(coordinates):
            sleep(args.interval)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
