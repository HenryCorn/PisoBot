"""Cached, additive place facts from OpenStreetMap and TfL."""
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import requests

from . import cache

TFL_URL = "https://api.tfl.gov.uk/StopPoint"
NOISE_SERVICES = (
    ("https://environment.data.gov.uk/spatialdata/road-noise-all-metrics-england-round-4/wms",
     "Road_Noise_Lnight_England_Round_4_All", "road_lnight_db"),
    ("https://environment.data.gov.uk/spatialdata/noise-data/wms",
     "Rail_Noise_Lnight_England_Round_4_All", "rail_lnight_db"),
)
CACHE_TTL_SECONDS = 90 * 24 * 60 * 60
CACHE_NAMESPACE = "place_facts_v2"
LOCAL_INDEX_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                "state", "place-index.sqlite3")
LOCAL_ROAD_RADIUS_M = 800
LOCAL_AMENITY_RADIUS_M = 600
MAJOR_ROAD_LABEL_M = 200
RAILWAY_LABEL_M = 50
QUIET_ROAD_CLEAR_M = 300
QUIET_RAIL_CLEAR_M = 100
LOW_NOISE_MAX_DB = 45
MODERATE_NOISE_MAX_DB = 55
_tfl_lock = threading.Lock()
_tfl_next = 0.0
_noise_lock = threading.Lock()
_noise_next = 0.0
_MAIN_ROAD_CLASSES = {"motorway", "trunk", "primary"}
_AMENITY_CATEGORIES = ("supermarket", "park", "gym", "cafe", "pharmacy")
_RAIL_MODES = {"tube", "overground", "elizabeth-line", "dlr", "national-rail"}
_RAIL_LINE_IDS = {
    "bakerloo", "central", "circle", "district", "dlr", "elizabeth", "elizabeth-line",
    "hammersmith-city", "jubilee", "metropolitan", "northern", "overground", "piccadilly",
    "victoria", "waterloo-city",
}


def _cell(lat, lng):
    return f"{lat:.3f},{lng:.3f}"


def _distance_m(lat1, lng1, lat2, lng2):
    x = (lng2 - lng1) * 111320 * math.cos(math.radians((lat1 + lat2) / 2))
    y = (lat2 - lat1) * 111320
    return math.hypot(x, y)


def _segment_distance_m(lat, lng, lat1, lon1, lat2, lon2):
    cos_lat = math.cos(math.radians(lat))
    x1, y1 = (lon1 - lng) * 111320 * cos_lat, (lat1 - lat) * 111320
    x2, y2 = (lon2 - lng) * 111320 * cos_lat, (lat2 - lat) * 111320
    dx, dy = x2 - x1, y2 - y1
    length = dx * dx + dy * dy
    t = 0 if not length else min(1, max(0, -(x1 * dx + y1 * dy) / length))
    return math.hypot(x1 + t * dx, y1 + t * dy)


def _index_grid_bounds(db, lat, lng, radius_m):
    meta = dict(db.execute("SELECT key, value FROM meta"))
    cell_size = float(meta["grid_cell_size_m"])
    lat_origin = float(meta["grid_lat_origin"])
    lon_scale = float(meta["grid_lon_scale"])
    x = lng * lon_scale
    y = (lat - lat_origin) * 111320
    span = math.ceil(radius_m / cell_size)
    return (math.floor(x / cell_size) - span, math.floor(x / cell_size) + span,
            math.floor(y / cell_size) - span, math.floor(y / cell_size) + span)


def local_osm_facts(lat, lng, index_path=None):
    """Read nearby OSM features from the optional, standard-library SQLite index."""
    path = Path(index_path or LOCAL_INDEX_PATH)
    if not path.is_file():
        return None
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        x1, x2, y1, y2 = _index_grid_bounds(db, lat, lng, LOCAL_ROAD_RADIUS_M)
        rows = db.execute("""
            SELECT DISTINCT f.id, f.kind, f.class, f.name, f.osm_id,
                   f.lat1, f.lon1, f.lat2, f.lon2, f.lat, f.lon
            FROM grid_cells AS g
            JOIN features AS f ON f.id = g.feature_id
            WHERE g.cell_x BETWEEN ? AND ? AND g.cell_y BETWEEN ? AND ?
        """, (x1, x2, y1, y2))
        roads, main_roads, railways = [], [], []
        amenities = {category: [] for category in _AMENITY_CATEGORIES}
        seen_ids = set()
        for feature_id, kind, feature_class, name, osm_id, lat1, lon1, lat2, lon2, point_lat, point_lon in rows:
            if feature_id in seen_ids:
                continue
            seen_ids.add(feature_id)
            if kind == "amenity":
                distance = round(_distance_m(lat, lng, point_lat, point_lon))
                if distance > LOCAL_AMENITY_RADIUS_M:
                    continue
                category = feature_class
                item_name = name or category.title()
                item = {"name": item_name, "distance_m": distance, "_osm_id": osm_id}
                duplicate = next((old for old in amenities[category]
                                  if ((name and old["_name"].casefold() == name.casefold()
                                       and _distance_m(lat, lng, point_lat, point_lon) < 50)
                                      or (not name and old["_osm_id"] == osm_id))), None)
                if duplicate:
                    if distance < duplicate["distance_m"]:
                        duplicate.update(item)
                else:
                    item["_name"] = name
                    amenities[category].append(item)
                continue

            distance = _segment_distance_m(lat, lng, lat1, lon1, lat2, lon2)
            if kind == "road" and distance <= LOCAL_ROAD_RADIUS_M:
                item = {"class": feature_class, "distance_m": round(distance)}
                roads.append(item)
                if feature_class in _MAIN_ROAD_CLASSES:
                    main_roads.append(item)
            elif kind == "railway" and distance <= LOCAL_ROAD_RADIUS_M:
                railways.append({"distance_m": round(distance)})

    nearest = lambda values: min(values, key=lambda item: item["distance_m"]) if values else None
    clean_amenities = {
        category: {
            "count": len(items),
            "nearest": ({key: value for key, value in nearest(items).items()
                         if not key.startswith("_")} if items else None),
        }
        for category, items in amenities.items()
    }
    return {
        "street": nearest(roads),
        "main_road": nearest(main_roads),
        "railway": nearest(railways),
        "amenities": clean_amenities,
    }


def _tfl_url(lat, lng):
    return (f"{TFL_URL}?lat={lat:.6f}&lon={lng:.6f}&radius=800"
            "&stopTypes=NaptanPublicBusCoachTram,NaptanMetroStation,NaptanRailStation")


def parse_tfl(data):
    """Count distinct rail lines and bus routes represented by nearby TfL stops."""
    rail_lines, bus_routes = set(), set()
    for stop in data.get("stopPoints", []):
        distance = stop.get("distance")
        if not isinstance(distance, (int, float)):
            continue
        modes = set(stop.get("modes") or [])
        lines = stop.get("lines") or []
        if distance <= 800:
            for line in lines:
                line_id = (line.get("id") or line.get("name") or "").strip().lower()
                line_modes = set(line.get("modes") or [])
                if line_id and ((line_modes & _RAIL_MODES) or line_id in _RAIL_LINE_IDS
                                or (modes & _RAIL_MODES and "bus" not in modes)):
                    rail_lines.add(line_id)
        if distance <= 300 and "bus" in modes:
            for line in lines:
                route = (line.get("id") or line.get("name") or "").strip().lower()
                if route and route not in _RAIL_LINE_IDS:
                    bus_routes.add(route)
    return {"rail_lines": len(rail_lines), "bus_routes": len(bus_routes)}


def _tfl(lat, lng):
    global _tfl_next
    with _tfl_lock:
        delay = _tfl_next - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        try:
            response = requests.get(_tfl_url(lat, lng), timeout=30)
            response.raise_for_status()
            return response.json()
        finally:
            _tfl_next = time.monotonic() + 4


def _noise_value(text):
    match = re.search(r"\bGRAY_INDEX\s*=\s*([0-9]+(?:\.[0-9]+)?)", text)
    if not match:
        return None
    value = float(match.group(1))
    return round(value, 1) if value > 0 else None


def _noise_point(url, layer, lat, lng):
    global _noise_next
    with _noise_lock:
        delay = _noise_next - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        try:
            delta = 0.00005
            response = requests.get(url, params={
                "service": "WMS", "version": "1.1.1", "request": "GetFeatureInfo",
                "layers": layer, "query_layers": layer, "srs": "EPSG:4326",
                "bbox": f"{lng - delta},{lat - delta},{lng + delta},{lat + delta}",
                "width": 1, "height": 1, "x": 0, "y": 0, "info_format": "text/plain",
                "feature_count": 1,
            }, timeout=30)
            response.raise_for_status()
            return _noise_value(response.text)
        finally:
            _noise_next = time.monotonic() + 4


def _noise(lat, lng):
    values = {}
    for url, layer, key in NOISE_SERVICES:
        values[key] = _noise_point(url, layer, lat, lng)
    return values


def facts(lat, lng, now=None):
    """Return locally indexed OSM and live TfL/Defra facts, cached for 90 days."""
    if lat is None or lng is None:
        return None
    try:
        index_version = os.stat(LOCAL_INDEX_PATH).st_mtime_ns
    except FileNotFoundError:
        return None
    now = time.time() if now is None else now
    key = f"{_cell(lat, lng)}:{index_version}"
    hit = cache.get(CACHE_NAMESPACE, key)
    if hit and now - hit.get("fetched_at", 0) < CACHE_TTL_SECONDS:
        return hit.get("facts")

    osm = local_osm_facts(lat, lng)
    if osm is None:
        return None
    transit = parse_tfl(_tfl(lat, lng))
    noise = _noise(lat, lng)
    result = {**osm, "transit": transit, "noise": noise}
    cache.put(CACHE_NAMESPACE, key, {"fetched_at": now, "facts": result})
    return result


def noise_band(decibels):
    """Use the highest mapped Lnight value: low <45, moderate 45-55, high >55 dB."""
    if decibels is None:
        return None
    if decibels < LOW_NOISE_MAX_DB:
        return "low"
    if decibels <= MODERATE_NOISE_MAX_DB:
        return "moderate"
    return "high"


def _night_noise(facts):
    values = [value for value in (facts.get("noise") or {}).values()
              if isinstance(value, (int, float))]
    return max(values) if values else None


def street_label(facts):
    """Major=OSM motorway/trunk/primary within 200 m; rail within 50 m.

    "Quiet residential street" additionally requires a residential-type road, all
    available modelled Lnight values below 45 dB, major roads beyond 300 m and rail beyond 100 m.
    """
    if not facts:
        return ""
    main = facts.get("main_road")
    rail = facts.get("railway")
    street = facts.get("street")
    nearby = []
    if main and main["distance_m"] <= MAJOR_ROAD_LABEL_M:
        nearby.append((main["distance_m"], "Busy road nearby"))
    if rail and rail["distance_m"] <= RAILWAY_LABEL_M:
        nearby.append((rail["distance_m"], "Railway nearby"))
    if nearby:
        return min(nearby)[1]
    if street and street["class"] in ("residential", "living_street", "unclassified", "service"):
        noise = noise_band(_night_noise(facts))
        if (noise == "low" and (not main or main["distance_m"] > QUIET_ROAD_CLEAR_M)
                and (not rail or rail["distance_m"] > QUIET_RAIL_CLEAR_M)):
            return "Quiet residential street"
        return "Residential street"
    if street and street["class"] in ("secondary", "tertiary"):
        return f"{street['class'].title()} road"
    return ""


def card_line(facts):
    """A prioritized place summary capped at 90 characters."""
    if not facts:
        return ""
    parts = []
    street = street_label(facts)
    if street:
        parts.append(street)
    band = noise_band(_night_noise(facts))
    if band:
        parts.append(f"night noise {band}")
    transit = facts.get("transit") or {}
    lines = transit.get("rail_lines", 0)
    if lines:
        parts.append(f"{lines} rail/tube line{'s' if lines != 1 else ''}")
    nearby = []
    for category, label in (("supermarket", "shop"), ("park", "park")):
        data = (facts.get("amenities") or {}).get(category) or {}
        nearest = data.get("nearest")
        if nearest:
            name = nearest.get("name", label.title()).strip()[:12]
            nearby.append(f"{name} ~{nearest['distance_m']}m")
    text = "🏘 " + " · ".join(parts)
    for item in nearby:
        candidate = text + " · " + item
        if len(candidate) <= 90:
            text = candidate
    return text if parts else ""


def detail_lines(facts):
    """Full facts for a future expanded card; distances are approximate within the cached cell."""
    if not facts:
        return []
    lines = []
    street = facts.get("street")
    main = facts.get("main_road")
    rail = facts.get("railway")
    if street:
        lines.append(f"Nearest street: {street['class']} (~{street['distance_m']} m)")
    lines.append(f"Nearest main road (motorway/trunk/primary): "
                 + (f"{main['class']} (~{main['distance_m']} m)" if main else
                    f"not found within {LOCAL_ROAD_RADIUS_M} m"))
    lines.append("Nearest railway: " + (f"~{rail['distance_m']} m" if rail else
                                       f"not found within {LOCAL_ROAD_RADIUS_M} m"))
    amenities = facts.get("amenities") or {}
    for category in ("supermarket", "park", "gym", "cafe", "pharmacy"):
        info = amenities.get(category) or {}
        nearest = info.get("nearest")
        distance = f"; nearest {nearest['name']} (~{nearest['distance_m']} m)" if nearest else ""
        lines.append(f"{category.title()}: {info.get('count', 0)} within "
                     f"{LOCAL_AMENITY_RADIUS_M} m{distance}")
    transit = facts.get("transit") or {}
    lines.append(f"TfL: {transit.get('rail_lines', 0)} distinct rail/tube lines within 800 m; "
                 f"{transit.get('bus_routes', 0)} bus routes within 300 m")
    noise = facts.get("noise") or {}
    road, railway = noise.get("road_lnight_db"), noise.get("rail_lnight_db")
    lines.append("Defra Round 4 Lnight (2021 model year, 4 m receptor, 23:00–07:00): "
                 f"road {f'{road:g} dB' if road is not None else 'no value'}; "
                 f"rail {f'{railway:g} dB' if railway is not None else 'no value'}")
    return lines
