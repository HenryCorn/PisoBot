"""Cached, additive place facts from OpenStreetMap and TfL."""
import math
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
CACHE_NAMESPACE = "place_facts_v3"
LOCAL_INDEX_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                "state", "place-index.sqlite3")
NOISE_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                "state", "place-noise.sqlite3")
LOCAL_ROAD_RADIUS_M = 800
LOCAL_AMENITY_RADIUS_M = 600
MAJOR_ROAD_LABEL_M = 200
RAILWAY_LABEL_M = 100
QUIET_ROAD_CLEAR_M = 300
QUIET_RAIL_CLEAR_M = 100
LOW_NOISE_MAX_DB = 45
MODERATE_NOISE_MAX_DB = 55
NOISE_REQUEST_INTERVAL_SECONDS = 10
NOISE_RATE_LIMIT_BACKOFF_SECONDS = 24 * 60 * 60
NOISE_FAILURE_RETRY_SECONDS = 24 * 60 * 60
_NOISE_UNAVAILABLE = object()
_tfl_lock = threading.Lock()
_tfl_next = 0.0
_noise_lock = threading.Lock()
_logger = logging.getLogger(__name__)
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


class NoiseRateLimited(requests.HTTPError):
    """Defra returned 429; callers should stop noise lookups for this run."""


def _noise_database():
    path = Path(NOISE_CACHE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=60)
    db.execute("""
        CREATE TABLE IF NOT EXISTS noise_answers (
            cell TEXT NOT NULL,
            layer TEXT NOT NULL,
            value REAL,
            PRIMARY KEY (cell, layer)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS noise_state (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS noise_failures (
            cell TEXT NOT NULL,
            layer TEXT NOT NULL,
            retry_after REAL NOT NULL,
            PRIMARY KEY (cell, layer)
        )
    """)
    return db


def _noise_db_cell(lat, lng):
    return _cell(lat, lng)


def _retry_after_seconds(value, now):
    if not value:
        return None
    try:
        return max(0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0, retry_at.timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return None


def _set_noise_next(db, timestamp):
    db.execute("INSERT OR REPLACE INTO noise_state (key, value) VALUES ('next_request_at', ?)",
               (timestamp,))


def _noise_point(url, layer, lat, lng):
    cell = _noise_db_cell(lat, lng)
    with _noise_lock, closing(_noise_database()) as db:
        db.execute("BEGIN IMMEDIATE")
        cached = db.execute(
            "SELECT value FROM noise_answers WHERE cell = ? AND layer = ?",
            (cell, layer),
        ).fetchone()
        if cached is not None:
            db.commit()
            return cached[0]

        now = time.time()
        blocked = db.execute(
            "SELECT value FROM noise_state WHERE key = 'blocked_until'"
        ).fetchone()
        if blocked and blocked[0] > now:
            db.commit()
            return _NOISE_UNAVAILABLE
        failure = db.execute(
            "SELECT retry_after FROM noise_failures WHERE cell = ? AND layer = ?",
            (cell, layer),
        ).fetchone()
        if failure and failure[0] > now:
            db.commit()
            return _NOISE_UNAVAILABLE
        if failure:
            db.execute("DELETE FROM noise_failures WHERE cell = ? AND layer = ?",
                       (cell, layer))

        next_request = db.execute(
            "SELECT value FROM noise_state WHERE key = 'next_request_at'"
        ).fetchone()
        if next_request and next_request[0] > now:
            time.sleep(next_request[0] - now)

        delta = 0.00005
        try:
            response = requests.get(url, params={
                "service": "WMS", "version": "1.1.1", "request": "GetFeatureInfo",
                "layers": layer, "query_layers": layer, "srs": "EPSG:4326",
                "bbox": f"{lng - delta},{lat - delta},{lng + delta},{lat + delta}",
                "width": 1, "height": 1, "x": 0, "y": 0, "info_format": "text/plain",
                "feature_count": 1,
            }, timeout=30)
            if response.status_code == 429:
                now = time.time()
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"), now)
                blocked_until = now + max(NOISE_RATE_LIMIT_BACKOFF_SECONDS,
                                          retry_after or 0)
                db.execute(
                    "INSERT OR REPLACE INTO noise_state (key, value) VALUES ('blocked_until', ?)",
                    (blocked_until,),
                )
                _set_noise_next(db, blocked_until)
                db.commit()
                raise NoiseRateLimited(
                    f"Defra returned HTTP 429; noise lookups paused until "
                    f"{datetime.fromtimestamp(blocked_until, timezone.utc).isoformat()}"
                )
            response.raise_for_status()
            value = _noise_value(response.text)
            db.execute(
                "INSERT INTO noise_answers (cell, layer, value) VALUES (?, ?, ?)",
                (cell, layer, value),
            )
            _set_noise_next(db, time.time() + NOISE_REQUEST_INTERVAL_SECONDS)
            db.commit()
            return value
        except NoiseRateLimited:
            raise
        except Exception:
            now = time.time()
            db.execute(
                "INSERT OR REPLACE INTO noise_failures (cell, layer, retry_after) "
                "VALUES (?, ?, ?)",
                (cell, layer, now + NOISE_FAILURE_RETRY_SECONDS),
            )
            _set_noise_next(db, now + NOISE_REQUEST_INTERVAL_SECONDS)
            db.commit()
            raise


def noise_backoff_active():
    """Return whether a persisted Defra rate-limit pause is still in effect."""
    with closing(_noise_database()) as db:
        row = db.execute(
            "SELECT value FROM noise_state WHERE key = 'blocked_until'"
        ).fetchone()
    return bool(row and row[0] > time.time())


def _noise(lat, lng):
    values = {}
    cell = _noise_db_cell(lat, lng)
    with closing(_noise_database()) as db:
        values = dict(db.execute(
            "SELECT layer, value FROM noise_answers WHERE cell = ?", (cell,)
        ))
    for url, layer, key in NOISE_SERVICES:
        if layer in values:
            values[key] = values.pop(layer)
            continue
        try:
            value = _noise_point(url, layer, lat, lng)
            if value is not _NOISE_UNAVAILABLE:
                values[key] = value
        except NoiseRateLimited as error:
            _logger.warning("Place facts Defra rate-limited; stopping noise lookups: %s", error)
            break
        except requests.RequestException as error:
            _logger.warning("Place facts Defra lookup failed for %s: %s", key, error)
    return values


def facts(lat, lng, now=None):
    """Return independent OSM, TfL and Defra facts for a matched advert."""
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
        osm = hit["osm"]
        transit = hit.get("transit")
    else:
        osm = local_osm_facts(lat, lng)
        if osm is None:
            return None
        transit = None

    if transit is None:
        try:
            transit = parse_tfl(_tfl(lat, lng))
        except requests.RequestException as error:
            _logger.warning("Place facts TfL lookup failed: %s", error)

    cache.put(CACHE_NAMESPACE, key, {"fetched_at": now, "osm": osm, "transit": transit})
    result = {**osm}
    if transit is not None:
        result["transit"] = transit
    try:
        result["noise"] = _noise(lat, lng)
    except (requests.RequestException, OSError, sqlite3.Error) as error:
        _logger.warning("Place facts Defra cache unavailable: %s", error)
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
    noise = facts.get("noise") or {}
    if any(key not in noise for key in ("road_lnight_db", "rail_lnight_db")):
        return None
    values = [value for value in noise.values() if isinstance(value, (int, float))]
    return max(values) if values else None


def street_label(facts):
    """Major=OSM motorway/trunk/primary within 200 m; rail within 100 m.

    "Quiet residential street" additionally requires a residential-type road, all
    available modelled Lnight values below 45 dB, major roads beyond 300 m and rail beyond 100 m.
    """
    if not facts:
        return ""
    main = facts.get("main_road")
    rail = facts.get("railway")
    street = facts.get("street")
    busy_road = main and main["distance_m"] <= MAJOR_ROAD_LABEL_M
    nearby_rail = rail and rail["distance_m"] <= RAILWAY_LABEL_M
    if busy_road and nearby_rail:
        return "Busy road and railway nearby"
    if busy_road:
        return "Busy road nearby"
    if nearby_rail:
        return "Railway nearby"
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
    transit = facts.get("transit")
    if transit is not None:
        lines.append(f"TfL: {transit.get('rail_lines', 0)} distinct rail/tube lines within 800 m; "
                     f"{transit.get('bus_routes', 0)} bus routes within 300 m")
    noise = facts.get("noise") or {}
    road, railway = noise.get("road_lnight_db"), noise.get("rail_lnight_db")
    reported_noise = []
    if "road_lnight_db" in noise:
        reported_noise.append(f"road {f'{road:g} dB' if road is not None else 'no modelled value'}")
    if "rail_lnight_db" in noise:
        reported_noise.append(
            f"rail {f'{railway:g} dB' if railway is not None else 'no modelled value'}"
        )
    if reported_noise:
        lines.append("Defra Round 4 Lnight (2021 model year, 4 m receptor, 23:00–07:00): "
                     + "; ".join(reported_noise))
    return lines
