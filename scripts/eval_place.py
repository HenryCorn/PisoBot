#!/usr/bin/env python3
"""Evaluate place facts for cached rental adverts with coordinates.

    python scripts/eval_place.py --cache state/cache.json [--sample 20]

Only the selected latitude/longitude pairs are sent to OpenStreetMap Overpass and TfL.
The cache file is opened read-only; results are stored in the normal PisoBot cache.
"""
import argparse
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["PISOBOT_STATE_DIR"] = os.path.join(tempfile.gettempdir(), "pisobot-place-eval-cache-v2")

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
    args = parser.parse_args(argv)
    if args.sample < 1:
        parser.error("--sample must be positive")
    try:
        coordinates = load_coordinates(args.cache)[:args.sample]
    except (OSError, ValueError) as error:
        out(f"Could not read advert coordinates: {error}")
        return 2
    if not coordinates:
        out("No cached rental advert coordinates found.")
        return 1
    out(f"Place facts for {len(coordinates)} cached advert coordinates")
    for index, (lat, lng) in enumerate(coordinates):
        try:
            out(f"{index + 1:02d}. {place.card_line(facts_for(lat, lng)) or 'No mapped place facts'}")
            cache.save()
        except Exception as error:
            out(f"{index + 1:02d}. Place facts unavailable ({type(error).__name__})")
        if index + 1 < len(coordinates):
            sleep(4)
    return 0


if __name__ == "__main__":
    sys.exit(main())
