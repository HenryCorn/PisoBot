"""Cached, additive place facts from OpenStreetMap and TfL."""
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from uuid import uuid4

import requests

TFL_URL = "https://api.tfl.gov.uk/StopPoint"
NOISE_SERVICES = (
    ("https://environment.data.gov.uk/spatialdata/road-noise-all-metrics-england-round-4/wms",
     "Road_Noise_Lnight_England_Round_4_All", "road_lnight_db"),
    ("https://environment.data.gov.uk/spatialdata/noise-data/wms",
     "Rail_Noise_Lnight_England_Round_4_All", "rail_lnight_db"),
)
CACHE_TTL_SECONDS = 90 * 24 * 60 * 60
LOCAL_INDEX_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                "state", "place-index.sqlite3")
LOCAL_ROAD_RADIUS_M = 800
LOCAL_AMENITY_RADIUS_M = 2000
AREA_AMENITY_RADIUS_M = 600
MAJOR_ROAD_LABEL_M = 200
RAILWAY_LABEL_M = 100
CARD_MAIN_ROAD_M = 50
CARD_SURFACE_RAIL_M = 60
QUIET_ROAD_CLEAR_M = 300
QUIET_RAIL_CLEAR_M = 100
LOW_NOISE_MAX_DB = 45
MODERATE_NOISE_MAX_DB = 55
LOCAL_SQLITE_BUSY_TIMEOUT_MS = 100
NOISE_REQUEST_INTERVAL_SECONDS = 10
NOISE_RATE_LIMIT_BACKOFF_SECONDS = 24 * 60 * 60
NOISE_FAILURE_RETRY_SECONDS = 24 * 60 * 60
REQUEST_LEASE_SECONDS = 120
CACHE_VERSION = 2
INDEX_POINT_SECONDS = 1.0
INDEX_MAX_ROWS = 100000
PLACE_SCHEMA = """
CREATE TABLE IF NOT EXISTS place_meta (key TEXT PRIMARY KEY, value REAL NOT NULL);
CREATE TABLE IF NOT EXISTS place_noise_answers (
    cell TEXT NOT NULL, layer TEXT NOT NULL, value REAL, fetched_at REAL NOT NULL,
    PRIMARY KEY (cell, layer)
);
CREATE TABLE IF NOT EXISTS place_noise_state (key TEXT PRIMARY KEY, value REAL NOT NULL);
CREATE TABLE IF NOT EXISTS place_noise_failures (
    cell TEXT NOT NULL, layer TEXT NOT NULL, retry_after REAL NOT NULL, PRIMARY KEY (cell, layer)
);
CREATE TABLE IF NOT EXISTS place_transit_answers (
    cell TEXT PRIMARY KEY, fetched_at REAL NOT NULL, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS place_transit_failures (cell TEXT PRIMARY KEY, retry_after REAL NOT NULL);
CREATE TABLE IF NOT EXISTS place_request_claims (
    key TEXT PRIMARY KEY, token TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS place_refresh_queue (
    cell TEXT PRIMARY KEY, lat REAL NOT NULL, lng REAL NOT NULL, queued_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS place_advert_facts (
    key TEXT PRIMARY KEY, lat REAL NOT NULL, lng REAL NOT NULL,
    index_mtime REAL NOT NULL, updated_at REAL NOT NULL, retry_after REAL NOT NULL, value TEXT
);
CREATE TABLE IF NOT EXISTS place_areas (
    area TEXT PRIMARY KEY, generated_at REAL NOT NULL, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS place_card_lines (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
_NOISE_UNAVAILABLE = object()
_tfl_lock = threading.Lock()
_tfl_next = 0.0
_logger = logging.getLogger(__name__)
_MAIN_ROAD_CLASSES = {"motorway", "trunk", "primary"}
_AMENITY_CATEGORIES = (
    "supermarket", "convenience", "gym", "cinema", "park", "cafe", "pub",
    "restaurant", "shop", "pharmacy",
)
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


def _readonly_db(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=LOCAL_SQLITE_BUSY_TIMEOUT_MS / 1000)
    db.execute(f"PRAGMA busy_timeout = {LOCAL_SQLITE_BUSY_TIMEOUT_MS}")
    return db


def local_osm_facts(lat, lng, index_path=None, *, db=None, deadline=None):
    """Read nearby OSM features from the optional, standard-library SQLite index."""
    if db is None:
        path = Path(index_path or LOCAL_INDEX_PATH)
        if not path.is_file():
            return None
        with closing(_readonly_db(path)) as connection:
            return local_osm_facts(lat, lng, db=connection, deadline=deadline)
    end = min(deadline, time.monotonic() + INDEX_POINT_SECONDS) if deadline is not None else None
    if end is not None and time.monotonic() >= end:
        raise TimeoutError("Place index deadline reached")
    db.set_progress_handler(lambda: int(end is not None and time.monotonic() >= end), 1000)
    rows = None
    try:
        x1, x2, y1, y2 = _index_grid_bounds(db, lat, lng, max(LOCAL_ROAD_RADIUS_M, LOCAL_AMENITY_RADIUS_M))
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
        for row_number, row in enumerate(rows):
            if row_number >= INDEX_MAX_ROWS or (end is not None and time.monotonic() >= end):
                raise TimeoutError("Place index read exceeded its budget")
            feature_id, kind, feature_class, name, osm_id, lat1, lon1, lat2, lon2, point_lat, point_lon = row
            if feature_id in seen_ids:
                continue
            seen_ids.add(feature_id)
            if kind == "amenity":
                distance = round(_distance_m(lat, lng, point_lat, point_lon))
                if distance > LOCAL_AMENITY_RADIUS_M:
                    continue
                category = feature_class
                if category not in amenities:
                    continue
                item_name = name or category.title()
                item = {"name": item_name, "distance_m": distance, "_osm_id": osm_id,
                        "_name": name or "", "_lat": point_lat, "_lng": point_lon}
                duplicate = next((old for old in amenities[category]
                                  if (old["_osm_id"] == osm_id
                                      or (name and old["_name"].casefold() == name.casefold()
                                          and _distance_m(old["_lat"], old["_lng"], point_lat, point_lon) < 50))), None)
                if duplicate:
                    if distance < duplicate["distance_m"]:
                        duplicate.update(item)
                else:
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
    finally:
        if rows is not None:
            rows.close()
        db.set_progress_handler(None, 0)

    nearest = lambda values: min(values, key=lambda item: item["distance_m"]) if values else None
    clean_amenities = {
        category: {
            "count": sum(item["distance_m"] <= AREA_AMENITY_RADIUS_M for item in items),
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


def local_osm_batch(points, deadline, index_path=None):
    """One read-only connection for a bounded batch; failed points remain unknown."""
    results = {}
    if time.monotonic() >= deadline:
        return results
    with closing(_readonly_db(index_path or LOCAL_INDEX_PATH)) as db:
        for key, lat, lng in points:
            if time.monotonic() >= deadline:
                break
            try:
                results[key] = local_osm_facts(lat, lng, db=db, deadline=deadline)
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
                if time.monotonic() >= deadline:
                    break
                _logger.warning("Place index point unavailable (%s)", type(error).__name__)
                results[key] = None
    return results


def _tfl_url(lat, lng):
    return (f"{TFL_URL}?lat={lat:.6f}&lon={lng:.6f}&radius=800"
            "&stopTypes=NaptanPublicBusCoachTram,NaptanMetroStation,NaptanRailStation")


def parse_tfl(data):
    """Count distinct rail lines and bus routes represented by nearby TfL stops."""
    if not isinstance(data, dict) or not isinstance(data.get("stopPoints"), list):
        raise ValueError("Invalid TfL StopPoint response")
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


class RefreshDeferred(Exception):
    """The supervised step has no budget for another provider operation."""


def _request_timeout(deadline, delay=0):
    left = 30 if deadline is None else deadline - time.monotonic() - max(0, delay)
    if left < 1:
        raise RefreshDeferred("Place provider deadline reached")
    # Separate connect/read waits share the remaining budget; the child watchdog
    # remains the hard limit for stalled sockets or an uncooperative provider.
    return min(10.0, left / 2)


def _tfl(lat, lng, deadline=None):
    global _tfl_next
    with _tfl_lock:
        delay = _tfl_next - time.monotonic()
        _request_timeout(deadline, delay)
        if delay > 0:
            time.sleep(delay)
        try:
            response = requests.get(_tfl_url(lat, lng), timeout=_request_timeout(deadline))
            response.raise_for_status()
            return response.json()
        finally:
            _tfl_next = time.monotonic() + 4


def _noise_value(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or any(re.search(r"<\s*ServiceException|exception|error", line, re.I) for line in lines):
        raise requests.RequestException("Invalid Defra GetFeatureInfo response")
    values = [line for line in lines if line.startswith("GRAY_INDEX")]
    if not values:
        if all(re.fullmatch(r"-+|Results for FeatureType '[^<>]+':", line) for line in lines):
            return None
        raise requests.RequestException("Invalid Defra GetFeatureInfo response")
    match = re.fullmatch(r"GRAY_INDEX\s*=\s*([0-9]+(?:\.[0-9]+)?)", values[0]) if len(values) == 1 else None
    if not match or any(
            line not in values and not re.fullmatch(r"-+|Results for FeatureType '[^<>]+':", line)
            for line in lines):
        raise requests.RequestException("Invalid Defra GetFeatureInfo response")
    value = float(match.group(1))
    if not math.isfinite(value):
        raise requests.RequestException("Non-finite Defra noise value")
    return round(value, 1) if value > 0 else None


class NoiseRateLimited(requests.HTTPError):
    """Defra returned 429; callers should stop noise lookups for this run."""


def _store():
    from .optional import store
    return store


def _ensure_cache():
    rows = _store().read("SELECT value FROM place_meta WHERE key = 'schema'")
    if rows and rows[0].get("value") == CACHE_VERSION:
        return
    # Undated noise and old advert/area shapes cannot be reused. Preserve all
    # provider backoff/lease tables so an upgrade cannot reset a rate-limit pause.
    migration = """
        DROP TABLE IF EXISTS place_noise_answers;
        DROP TABLE IF EXISTS place_advert_facts;
        DROP TABLE IF EXISTS place_areas;
        DROP TABLE IF EXISTS place_refresh_queue;
        DROP TABLE IF EXISTS place_card_lines;
    """
    if not _store().write(migration + PLACE_SCHEMA, [
            ("INSERT INTO place_meta (key, value) VALUES ('schema', ?) "
             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (CACHE_VERSION,))]):
        raise OSError("Optional place cache is unavailable")


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


def _claim_request(key, now, delay=0, spacing=0):
    token = uuid4().hex
    expires = now + delay + REQUEST_LEASE_SECONDS
    statements = [
        ("INSERT INTO place_request_claims (key, token, expires_at) "
         "VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET token=excluded.token, expires_at=excluded.expires_at "
         "WHERE place_request_claims.expires_at <= ?",
         (key, token, expires, now)),
    ]
    if spacing:
        statements.append((
            "INSERT INTO place_noise_state (key, value) "
            "SELECT 'next_request_at', ? WHERE changes() = 1 "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (now + delay + spacing,),
        ))
    if not _store().write("", statements):
        return None
    rows = _store().read("SELECT token FROM place_request_claims WHERE key = ?", (key,))
    return token if rows and rows[0].get("token") == token else None


def _release_request(key, token, cell, layer, *, value=None, failure=None, blocked_until=None, transit=None):
    statements = [
        ("DELETE FROM place_request_claims WHERE key = ? AND token = ?", (key, token)),
    ]
    if blocked_until is not None:
        statements.extend((
            ("INSERT INTO place_noise_state (key, value) VALUES ('blocked_until', ?) "
             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (blocked_until,)),
            ("INSERT INTO place_noise_state (key, value) VALUES ('next_request_at', ?) "
             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (blocked_until,)),
        ))
    elif layer is not None and failure is None:
        statements.extend((
            ("INSERT INTO place_noise_answers (cell, layer, value, fetched_at) VALUES (?, ?, ?, ?) "
             "ON CONFLICT(cell, layer) DO UPDATE SET value=excluded.value, fetched_at=excluded.fetched_at",
             (cell, layer, value, time.time())),
            ("DELETE FROM place_noise_failures WHERE cell = ? AND layer = ?", (cell, layer)),
            ("INSERT INTO place_noise_state (key, value) VALUES ('next_request_at', ?) "
             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
             (time.time() + NOISE_REQUEST_INTERVAL_SECONDS,)),
        ))
    elif layer is not None:
        statements.append((
            "INSERT INTO place_noise_failures (cell, layer, retry_after) VALUES (?, ?, ?) "
            "ON CONFLICT(cell, layer) DO UPDATE SET retry_after=excluded.retry_after",
            (cell, layer, time.time() + NOISE_FAILURE_RETRY_SECONDS),
        ))
        statements.append((
            "INSERT INTO place_noise_state (key, value) VALUES ('next_request_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (time.time() + NOISE_REQUEST_INTERVAL_SECONDS,),
        ))
    elif transit is not None:
        if failure is None:
            statements.extend((
                ("INSERT INTO place_transit_answers (cell, fetched_at, value) VALUES (?, ?, ?) "
                 "ON CONFLICT(cell) DO UPDATE SET fetched_at=excluded.fetched_at, value=excluded.value",
                 (cell, time.time(), json.dumps(transit, sort_keys=True))),
                ("DELETE FROM place_transit_failures WHERE cell = ?", (cell,)),
            ))
        else:
            statements.append((
                "INSERT INTO place_transit_failures (cell, retry_after) VALUES (?, ?) "
                "ON CONFLICT(cell) DO UPDATE SET retry_after=excluded.retry_after",
                (cell, time.time() + NOISE_FAILURE_RETRY_SECONDS),
            ))
    if not _store().write("", statements):
        _logger.warning("Place facts optional cache write failed")


def _noise_point(url, layer, lat, lng, deadline=None):
    _request_timeout(deadline)
    _ensure_cache()
    cell = _noise_db_cell(lat, lng)
    now = time.time()
    cached = _store().read(
        "SELECT value, fetched_at FROM place_noise_answers WHERE cell = ? AND layer = ?", (cell, layer))
    if (cached and fresh(cached[0].get("fetched_at"), now, CACHE_TTL_SECONDS)
            and _valid_noise_value(cached[0].get("value"))):
        return cached[0].get("value")
    blocked = _store().read("SELECT value FROM place_noise_state WHERE key = 'blocked_until'")
    if blocked and number(blocked[0].get("value")) and blocked[0]["value"] > now:
        return _NOISE_UNAVAILABLE
    failed = _store().read(
        "SELECT retry_after FROM place_noise_failures WHERE cell = ? AND layer = ?", (cell, layer))
    if failed and number(failed[0].get("retry_after")) and failed[0]["retry_after"] > now:
        return _NOISE_UNAVAILABLE
    next_request = _store().read("SELECT value FROM place_noise_state WHERE key = 'next_request_at'")
    delay = (max(0, next_request[0]["value"] - now)
             if next_request and number(next_request[0].get("value")) else 0)
    _request_timeout(deadline, delay)
    token = _claim_request("defra", now, delay, NOISE_REQUEST_INTERVAL_SECONDS)
    if token is None:
        return _NOISE_UNAVAILABLE
    value, failure, blocked_until = None, None, None
    try:
        if delay:
            time.sleep(delay)
        delta = 0.00005
        response = requests.get(url, params={
            "service": "WMS", "version": "1.1.1", "request": "GetFeatureInfo",
            "layers": layer, "query_layers": layer, "srs": "EPSG:4326",
            "bbox": f"{lng - delta},{lat - delta},{lng + delta},{lat + delta}",
            "width": 1, "height": 1, "x": 0, "y": 0, "info_format": "text/plain",
            "feature_count": 1,
        }, timeout=_request_timeout(deadline))
        if response.status_code == 429:
            retry_after = _retry_after_seconds(response.headers.get("Retry-After"), time.time())
            blocked_until = time.time() + max(NOISE_RATE_LIMIT_BACKOFF_SECONDS, retry_after or 0)
            failure = NoiseRateLimited(
                f"Defra returned HTTP 429; noise lookups paused until "
                f"{datetime.fromtimestamp(blocked_until, timezone.utc).isoformat()}")
        else:
            response.raise_for_status()
            value = _noise_value(response.text)
    except RefreshDeferred:
        if not _store().write("", [
                ("DELETE FROM place_request_claims WHERE key = ? AND token = ?", ("defra", token))]):
            _logger.warning("Place provider lease release failed")
        raise
    except (requests.RequestException, ValueError, TypeError) as error:
        failure = error
    _release_request("defra", token, cell, layer, value=value, failure=failure, blocked_until=blocked_until)
    if failure is not None:
        raise failure
    return value


def noise_backoff_active():
    """Return whether a persisted Defra rate-limit pause is still in effect."""
    row = _store().read("SELECT value FROM place_noise_state WHERE key = 'blocked_until'")
    return bool(row and number(row[0].get("value")) and row[0]["value"] > time.time())


def _noise(lat, lng, deadline=None):
    _request_timeout(deadline)
    values = {}
    cell = _noise_db_cell(lat, lng)
    values = {row["layer"]: row["value"] for row in _store().read(
        "SELECT layer, value, fetched_at FROM place_noise_answers WHERE cell = ?", (cell,))
        if fresh(row.get("fetched_at"), time.time(), CACHE_TTL_SECONDS) and _valid_noise_value(row.get("value"))}
    for url, layer, key in NOISE_SERVICES:
        if layer in values:
            values[key] = values.pop(layer)
            continue
        try:
            value = _noise_point(url, layer, lat, lng, deadline=deadline)
            if value is not _NOISE_UNAVAILABLE:
                values[key] = value
        except NoiseRateLimited as error:
            _logger.warning("Place facts Defra rate-limited; stopping noise lookups: %s", error)
            break
        except requests.RequestException as error:
            _logger.warning("Place facts Defra lookup failed for %s: %s", key, error)
    return values


def cached_facts(lat, lng, now=None):
    """Read local/cache facts on the optional worker, never on the card-delivery path."""
    if lat is None or lng is None:
        return None
    now = time.time() if now is None else now
    try:
        osm = local_osm_facts(lat, lng)
    except (OSError, sqlite3.Error) as error:
        _logger.warning("Place facts local OSM read failed: %s", error)
        return None
    if osm is None:
        return None
    result = {**osm}
    try:
        cell = _cell(lat, lng)
        transit = _store().read(
            "SELECT fetched_at, value FROM place_transit_answers WHERE cell = ?", (cell,))
        if transit and fresh(transit[0].get("fetched_at"), now, CACHE_TTL_SECONDS):
            try:
                value = json.loads(transit[0]["value"])
                if _valid_transit(value):
                    result["transit"] = value
            except (ValueError, TypeError, KeyError) as error:
                _logger.warning("Place facts local TfL cache is invalid: %s", type(error).__name__)
        layers = {row["layer"]: row["value"] for row in _store().read(
            "SELECT layer, value, fetched_at FROM place_noise_answers WHERE cell = ?", (cell,))
            if fresh(row.get("fetched_at"), now, CACHE_TTL_SECONDS)}
        noise = {}
        for layer, key in ((NOISE_SERVICES[0][1], NOISE_SERVICES[0][2]),
                           (NOISE_SERVICES[1][1], NOISE_SERVICES[1][2])):
            if layer in layers and _valid_noise_value(layers[layer]):
                noise[key] = layers[layer]
        if noise:
            result["noise"] = noise
    except Exception as error:
        _logger.warning("Place facts local cache read failed: %s", type(error).__name__)
    if not _valid_facts(result):
        _logger.warning("Place facts local cache record has an invalid schema")
        return None
    return result


def _refresh_transit(lat, lng, now=None, deadline=None):
    _request_timeout(deadline)
    _ensure_cache()
    cell = _cell(lat, lng)
    now = time.time() if now is None else now
    cached = _store().read(
        "SELECT fetched_at, value FROM place_transit_answers WHERE cell = ?", (cell,))
    if cached and fresh(cached[0].get("fetched_at"), now, CACHE_TTL_SECONDS):
        try:
            if _valid_transit(json.loads(cached[0]["value"])):
                return
        except (ValueError, TypeError, KeyError) as error:
            _logger.warning("Place TfL cache is invalid (%s)", type(error).__name__)
    failed = _store().read("SELECT retry_after FROM place_transit_failures WHERE cell = ?", (cell,))
    if failed and number(failed[0].get("retry_after")) and failed[0]["retry_after"] > now:
        return
    _request_timeout(deadline)
    token = _claim_request(f"tfl:{cell}", now)
    if token is None:
        return
    failure, value = None, None
    try:
        value = parse_tfl(_tfl(lat, lng, deadline=deadline))
    except RefreshDeferred:
        if not _store().write("", [
                ("DELETE FROM place_request_claims WHERE key = ? AND token = ?", (f"tfl:{cell}", token))]):
            _logger.warning("Place provider lease release failed")
        raise
    except (requests.RequestException, ValueError, TypeError, AttributeError) as error:
        failure = error
    _release_request(f"tfl:{cell}", token, cell, None, failure=failure,
                     transit=value if failure is None else {})
    if failure is not None:
        _logger.warning("Place facts TfL lookup failed: %s", failure)


def refresh_network(lat, lng, now=None, deadline=None):
    """Refresh provider caches only in the foundation's supervised optional child."""
    if lat is None or lng is None or not Path(LOCAL_INDEX_PATH).is_file():
        return
    _request_timeout(deadline)
    _refresh_transit(lat, lng, now=now, deadline=deadline)
    _noise(lat, lng, deadline=deadline)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def fresh(timestamp, now, ttl):
    return number(timestamp) and 0 <= now - timestamp < ttl


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


def _valid_noise_value(value):
    return value is None or (
        type(value) in (int, float) and math.isfinite(value) and value >= 0)


def _valid_transit(value):
    return isinstance(value, dict) and all(
        type(value.get(key)) is int and value[key] >= 0
        for key in ("rail_lines", "bus_routes"))


def _valid_facts(value):
    if not isinstance(value, dict):
        return False
    for key in ("street", "main_road", "railway"):
        item = value.get(key)
        if item is not None and (
                not isinstance(item, dict)
                or type(item.get("distance_m")) not in (int, float)
                or not math.isfinite(item["distance_m"]) or item["distance_m"] < 0
                or (key != "railway" and not isinstance(item.get("class"), str))):
            return False
    if "transit" in value and not _valid_transit(value["transit"]):
        return False
    noise = value.get("noise", {})
    if not isinstance(noise, dict) or any(
            key not in ("road_lnight_db", "rail_lnight_db") or not _valid_noise_value(item)
            for key, item in noise.items()):
        return False
    amenities = value.get("amenities", {})
    if not isinstance(amenities, dict):
        return False
    for item in amenities.values():
        if not isinstance(item, dict):
            return False
        if "count" in item and (type(item["count"]) is not int or item["count"] < 0):
            return False
        nearest = item.get("nearest")
        if nearest is not None and (
                not isinstance(nearest, dict)
                or not isinstance(nearest.get("name"), str)
                or type(nearest.get("distance_m")) not in (int, float)
                or not math.isfinite(nearest["distance_m"]) or nearest["distance_m"] < 0):
            return False
    return True


def street_label(facts):
    """Major=OSM motorway/trunk/primary within 200 m; rail within 100 m.

    "Quiet residential street" additionally requires a residential-type road, all
    available modelled Lnight values below 45 dB, major roads beyond 300 m and rail beyond 100 m.
    """
    if not facts or not _valid_facts(facts):
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
    """Put modelled night noise first; mention nearby roads/rail only at close range."""
    if not facts or not _valid_facts(facts):
        return ""
    parts = []
    band = noise_band(_night_noise(facts))
    if band:
        parts.append(f"Night noise {band}")
        main = facts.get("main_road")
        if main and main["distance_m"] <= CARD_MAIN_ROAD_M:
            parts.append(f"{main['class'].title()} road ~{round(main['distance_m'])}m")
        rail = facts.get("railway")
        if rail and rail["distance_m"] <= CARD_SURFACE_RAIL_M:
            parts.append(f"Railway ~{round(rail['distance_m'])}m")
    else:
        street = street_label(facts)
        if street:
            parts.append(street)
    transit = facts.get("transit") or {}
    lines = transit.get("rail_lines", 0)
    if lines:
        parts.append(f"{lines} rail/tube line{'s' if lines != 1 else ''}")
    text = "🏘 " + " · ".join(parts) if parts else ""
    return text if len(text) <= 90 else ""


def _short_name(value, limit=18):
    name = " ".join(str(value).split())
    if len(name) <= limit:
        return name
    words = name.split()
    shortened = ""
    for word in words:
        candidate = f"{shortened} {word}".strip()
        if len(candidate) > limit - 1:
            break
        shortened = candidate
    if shortened:
        return shortened + "…"
    return name[:limit - 1] + "…"


def amenities_line(facts):
    """One bounded tile line; distance is straight-line × 1.3 at 80 m/min."""
    if not facts or not _valid_facts(facts):
        return ""
    parts = []
    for category, emoji in (
            ("supermarket", "🛒"), ("convenience", "🏪"), ("gym", "🏋"),
            ("cinema", "🎬")):
        info = (facts.get("amenities") or {}).get(category) or {}
        nearest = info.get("nearest")
        if not nearest or nearest["distance_m"] > LOCAL_AMENITY_RADIUS_M:
            continue
        distance = nearest["distance_m"]
        minutes = max(1, math.ceil(distance * 1.3 / 80))
        name = _short_name(nearest["name"])
        if name:
            parts.append(f"{emoji} {name} ~{minutes} min")
    return " · ".join(parts)


def detail_lines(facts):
    """Full facts for a future expanded card; distances are approximate within the cached cell."""
    if not facts or not _valid_facts(facts):
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
    for category in _AMENITY_CATEGORIES:
        info = amenities.get(category) or {}
        nearest = info.get("nearest")
        distance = f"; nearest {nearest['name']} (~{nearest['distance_m']} m)" if nearest else ""
        lines.append(f"{category.title()}: {info.get('count', 0)} within "
                     f"{AREA_AMENITY_RADIUS_M} m{distance}")
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
