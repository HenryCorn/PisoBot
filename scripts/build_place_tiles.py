#!/usr/bin/env python3
"""Build pisobot/data/place_tiles.json from a Greater London OpenStreetMap extract (offline).

    pip install osmium
    python scripts/build_place_tiles.py path/to/greater-london-latest.osm.pbf

Keeps only supermarkets, convenience shops, gyms and cinemas as (lat, lng, name). Areas use the
centre of their bounding box. Data (c) OpenStreetMap contributors, ODbL 1.0.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import osmium

OUT = Path(__file__).resolve().parents[1] / "pisobot" / "data" / "place_tiles.json"


def category(tags):
    if tags.get("shop") == "supermarket":
        return "supermarket"
    if tags.get("shop") == "convenience":
        return "convenience"
    if tags.get("amenity") in ("gym", "fitness_centre") or tags.get("leisure") == "fitness_centre":
        return "gym"
    if tags.get("amenity") == "cinema":
        return "cinema"
    return None


class Handler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.points = {"supermarket": [], "convenience": [], "gym": [], "cinema": []}

    def add(self, cat, lat, lng, name):
        self.points[cat].append([round(lat, 5), round(lng, 5), (name or "")[:40]])

    def node(self, n):
        cat = category(n.tags)
        if cat and n.location.valid():
            self.add(cat, n.location.lat, n.location.lon, n.tags.get("name"))

    def area(self, a):
        cat = category(a.tags)
        if not cat:
            return
        lats, lngs = [], []
        for ring in a.outer_rings():
            for node in ring:
                lats.append(node.lat)
                lngs.append(node.lon)
        if lats:
            self.add(cat, (min(lats) + max(lats)) / 2, (min(lngs) + max(lngs)) / 2, a.tags.get("name"))


def main(path):
    handler = Handler()
    handler.apply_file(path, locations=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": {"built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "source": Path(path).name,
                        "license": "OpenStreetMap data (c) OpenStreetMap contributors, ODbL 1.0",
                        "counts": {k: len(v) for k, v in handler.points.items()}},
               "points": handler.points}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(payload["meta"]["counts"], OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
