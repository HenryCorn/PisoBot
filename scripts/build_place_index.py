#!/usr/bin/env python3
"""Download the Greater London OSM extract and build a compact local place index."""
import argparse
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from urllib.request import Request, urlopen

import osmium


ROOT = Path(__file__).resolve().parents[1]
EXTRACT_URL = "https://download.geofabrik.de/europe/united-kingdom/england/greater-london-latest.osm.pbf"
USER_AGENT = "PisoBot place index builder/1.0 (rental search project)"
DEFAULT_EXTRACT = ROOT / "state" / "greater-london-latest.osm.pbf"
DEFAULT_OUTPUT = ROOT / "state" / "place-index.sqlite3"
CELL_SIZE_M = 250
GRID_LAT_ORIGIN = 51.5
GRID_LON_SCALE = 111320 * math.cos(math.radians(GRID_LAT_ORIGIN))

ROAD_CLASSES = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
    "residential", "living_street", "service", "pedestrian",
}
RAIL_CLASSES = {
    "rail", "light_rail", "tram", "subway", "narrow_gauge", "monorail",
    "funicular", "miniature", "preserved",
}
RAIL_EXCLUDED = {"disused", "abandoned", "construction", "proposed"}
GREEN_LEISURE = {"park", "garden", "nature_reserve", "common", "green_space"}
GREEN_LANDUSE = {"recreation_ground", "village_green"}


def _amenity_category(tags):
    if tags.get("shop") == "supermarket":
        return "supermarket"
    if tags.get("leisure") in GREEN_LEISURE or tags.get("landuse") in GREEN_LANDUSE:
        return "park"
    if tags.get("amenity") in ("gym", "fitness_centre") or tags.get("leisure") == "fitness_centre":
        return "gym"
    if tags.get("amenity") in ("cafe", "pharmacy"):
        return tags["amenity"]
    return None


def _grid_xy(lat, lon):
    x = lon * GRID_LON_SCALE
    y = (lat - GRID_LAT_ORIGIN) * 111320
    return math.floor(x / CELL_SIZE_M), math.floor(y / CELL_SIZE_M)


def _feature_cells(feature):
    if feature["kind"] == "amenity":
        return {_grid_xy(feature["lat"], feature["lon"])}
    x1, y1 = _grid_xy(feature["lat1"], feature["lon1"])
    x2, y2 = _grid_xy(feature["lat2"], feature["lon2"])
    return {(x, y) for x in range(min(x1, x2), max(x1, x2) + 1)
            for y in range(min(y1, y2), max(y1, y2) + 1)}


class _IndexWriter:
    def __init__(self, db):
        self.db = db
        self.next_id = 1
        self.features = []
        self.cells = []
        self.counts = {"road": 0, "railway": 0, "amenity": 0}

    def add(self, kind, feature_class, name, osm_id, *, lat1=None, lon1=None,
            lat2=None, lon2=None, lat=None, lon=None):
        feature_id = self.next_id
        self.next_id += 1
        feature = {
            "kind": kind, "class": feature_class, "name": name, "osm_id": osm_id,
            "lat1": lat1, "lon1": lon1, "lat2": lat2, "lon2": lon2, "lat": lat, "lon": lon,
        }
        self.features.append((
            feature_id, kind, feature_class, name, osm_id,
            lat1, lon1, lat2, lon2, lat, lon,
        ))
        self.cells.extend((x, y, feature_id) for x, y in _feature_cells(feature))
        self.counts[kind] += 1
        if len(self.features) >= 20000:
            self.flush()

    def flush(self):
        if not self.features:
            return
        self.db.executemany(
            "INSERT INTO features VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self.features,
        )
        self.db.executemany("INSERT OR IGNORE INTO grid_cells VALUES (?, ?, ?)", self.cells)
        self.features.clear()
        self.cells.clear()


class _PlaceHandler(osmium.SimpleHandler):
    def __init__(self, writer):
        super().__init__()
        self.writer = writer

    @staticmethod
    def _coords(obj):
        points = []
        for node in obj.nodes:
            location = node.location
            if location.valid():
                points.append((location.lat, location.lon))
            else:
                points.append(None)
        return points

    def _add_lines(self, kind, feature_class, osm_id, points):
        for first, second in zip(points, points[1:]):
            if first is None or second is None:
                continue
            self.writer.add(kind, feature_class, None, osm_id,
                            lat1=first[0], lon1=first[1], lat2=second[0], lon2=second[1])

    def node(self, node):
        if not node.location.valid():
            return
        tags = dict(node.tags)
        category = _amenity_category(tags)
        if category:
            name = " ".join((tags.get("name") or tags.get("brand") or "").split())
            self.writer.add("amenity", category, name or category.title(), f"node/{node.id}",
                            lat=node.location.lat, lon=node.location.lon)

    def way(self, way):
        tags = dict(way.tags)
        points = self._coords(way)
        if not points:
            return
        osm_id = f"way/{way.id}"
        highway = tags.get("highway")
        if highway in ROAD_CLASSES:
            self._add_lines("road", highway, osm_id, points)
        railway = tags.get("railway")
        lifecycle = any(
            tags.get(key, "").lower() not in ("", "no", "false", "0")
            for key in ("disused", "abandoned", "disused:railway", "abandoned:railway")
        )
        underground = tags.get("tunnel", "").lower() not in ("", "no", "false", "0")
        if (railway in RAIL_CLASSES and railway not in RAIL_EXCLUDED
                and not lifecycle and not underground):
            self._add_lines("railway", railway, osm_id, points)

    def area(self, area):
        tags = dict(area.tags)
        category = _amenity_category(tags)
        if not category:
            return
        points = []
        for ring in area.outer_rings():
            for node in ring:
                if node.location.valid():
                    points.append((node.location.lat, node.location.lon))
        if not points:
            return
        lat = sum(point[0] for point in points) / len(points)
        lon = sum(point[1] for point in points) / len(points)
        name = " ".join((tags.get("name") or tags.get("brand") or "").split())
        origin = "way" if area.from_way() else "relation"
        self.writer.add("amenity", category, name or category.title(),
                        f"{origin}/{area.orig_id()}", lat=lat, lon=lon)


def _download_extract(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, pending_name = tempfile.mkstemp(prefix=".greater-london-", suffix=".download", dir=path.parent)
    os.close(fd)
    request = Request(EXTRACT_URL, headers={"User-Agent": USER_AGENT})
    last_modified = None
    try:
        with urlopen(request, timeout=120) as response, open(pending_name, "wb") as target:
            last_modified = response.headers.get("Last-Modified")
            expected = response.headers.get("Content-Length")
            written = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
                written += len(chunk)
            if expected and written != int(expected):
                raise IOError(f"incomplete extract download: received {written} of {expected} bytes")
        if written == 0:
            raise IOError("empty extract download")
        os.replace(pending_name, path)
    finally:
        if os.path.exists(pending_name):
            os.unlink(pending_name)
    return last_modified


def _header_timestamp(path):
    reader = osmium.io.Reader(str(path))
    try:
        header = reader.header()
        return header.get("osmosis_replication_timestamp")
    finally:
        reader.close()


def build(extract_path, output_path, last_modified=None):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".place-index-", suffix=".sqlite3", dir=output_path.parent)
    os.close(fd)
    try:
        db = sqlite3.connect(temporary_name)
        try:
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("PRAGMA synchronous=OFF")
            db.executescript("""
                CREATE TABLE features (
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL,
                    class TEXT,
                    name TEXT,
                    osm_id TEXT NOT NULL,
                    lat1 REAL, lon1 REAL, lat2 REAL, lon2 REAL,
                    lat REAL, lon REAL
                );
                CREATE TABLE grid_cells (
                    cell_x INTEGER NOT NULL,
                    cell_y INTEGER NOT NULL,
                    feature_id INTEGER NOT NULL,
                    PRIMARY KEY (cell_x, cell_y, feature_id)
                ) WITHOUT ROWID;
            """)
            writer = _IndexWriter(db)
            handler = _PlaceHandler(writer)
            handler.apply_file(str(extract_path), locations=True, idx="flex_mem")
            writer.flush()
            counts = {kind: db.execute(
                "SELECT COUNT(*) FROM features WHERE kind = ?", (kind,),
            ).fetchone()[0] for kind in ("road", "railway", "amenity")}
            if not any(counts.values()):
                raise RuntimeError("extract contained no indexable features")
            db.execute("CREATE INDEX grid_cells_by_y_x ON grid_cells(cell_y, cell_x)")
            metadata = {
                "extract_date": (_header_timestamp(extract_path) or last_modified or "unknown"),
                "built_at_utc": datetime.now(timezone.utc).isoformat(),
                "source": EXTRACT_URL,
                "license": "OpenStreetMap data © OpenStreetMap contributors, ODbL 1.0",
                "attribution": "© OpenStreetMap contributors",
                "road_segment_count": str(counts["road"]),
                "railway_segment_count": str(counts["railway"]),
                "amenity_point_count": str(counts["amenity"]),
                "grid_cell_size_m": str(CELL_SIZE_M),
                "grid_lat_origin": str(GRID_LAT_ORIGIN),
                "grid_lon_scale": str(GRID_LON_SCALE),
            }
            db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.executemany("INSERT INTO meta VALUES (?, ?)", metadata.items())
            db.commit()
        finally:
            db.close()
        os.replace(temporary_name, output_path)
        return counts
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extract", type=Path, default=DEFAULT_EXTRACT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--refresh", action="store_true",
                        help="download the latest extract even when a local copy exists")
    args = parser.parse_args(argv)
    started = time.monotonic()
    last_modified = None
    if args.refresh or not args.extract.exists():
        print(f"Downloading {EXTRACT_URL}")
        last_modified = _download_extract(args.extract)
    counts = build(args.extract, args.output, last_modified)
    size = args.output.stat().st_size
    print(
        f"Built {args.output} ({size:,} bytes) in {time.monotonic() - started:.1f} s; "
        f"road segments={counts['road']:,}, railway segments={counts['railway']:,}, "
        f"amenity points={counts['amenity']:,}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
