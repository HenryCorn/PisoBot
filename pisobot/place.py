"""Cached, additive place facts from OpenStreetMap and TfL."""
import math
import re
import threading
import time

import requests

from . import cache, config

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
TFL_URL = "https://api.tfl.gov.uk/StopPoint"
NOISE_SERVICES = (
    ("https://environment.data.gov.uk/spatialdata/road-noise-all-metrics-england-round-4/wms",
     "Road_Noise_Lnight_England_Round_4_All", "road_lnight_db"),
    ("https://environment.data.gov.uk/spatialdata/noise-data/wms",
     "Rail_Noise_Lnight_England_Round_4_All", "rail_lnight_db"),
)
OVERPASS_UA = "PisoBot place facts/1.0 (open-source rental search project)"
CACHE_TTL_SECONDS = 90 * 24 * 60 * 60
OVERPASS_GAP_SECONDS = 10
OVERPASS_ROAD_RADIUS_M = 400
AMENITY_RADIUS_M = 600
MAJOR_ROAD_LABEL_M = 200
RAILWAY_LABEL_M = 50
QUIET_ROAD_CLEAR_M = 300
QUIET_RAIL_CLEAR_M = 100
LOW_NOISE_MAX_DB = 45
MODERATE_NOISE_MAX_DB = 55
_overpass_lock = threading.Lock()
_overpass_next = 0.0
_tfl_lock = threading.Lock()
_tfl_next = 0.0
_noise_lock = threading.Lock()
_noise_next = 0.0
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


def _query(lat, lng):
    return f"""[out:json][timeout:12];
(
  nwr(around:{OVERPASS_ROAD_RADIUS_M},{lat:.6f},{lng:.6f})[highway~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service)$"];
)->.roads;
(
  nwr(around:{OVERPASS_ROAD_RADIUS_M},{lat:.6f},{lng:.6f})[railway~"^(rail|light_rail|tram|subway)$"];
)->.rail;
(
  nwr(around:{AMENITY_RADIUS_M},{lat:.6f},{lng:.6f})[shop=supermarket];
  nwr(around:{AMENITY_RADIUS_M},{lat:.6f},{lng:.6f})[leisure~"^(park|garden|nature_reserve|common|fitness_centre)$"];
  nwr(around:{AMENITY_RADIUS_M},{lat:.6f},{lng:.6f})[landuse~"^(recreation_ground|village_green)$"];
  nwr(around:{AMENITY_RADIUS_M},{lat:.6f},{lng:.6f})[amenity~"^(gym|fitness_centre|cafe|pharmacy)$"];
)->.amenities;
.roads out geom;
.rail out geom;
.amenities out center;"""


def _element_point(element):
    point = element.get("center") or element
    lat, lng = point.get("lat"), point.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        return lat, lng
    return None


def _geometry_distance(element, lat, lng):
    geometry = element.get("geometry") or []
    if not geometry:
        point = _element_point(element)
        return _distance_m(lat, lng, *point) if point else None
    points = [(p["lat"], p["lon"]) for p in geometry]
    if len(points) == 1:
        return _distance_m(lat, lng, *points[0])

    def xy(point):
        return ((point[1] - lng) * 111320 * math.cos(math.radians(lat)),
                (point[0] - lat) * 111320)

    coords = [xy(point) for point in points]
    best = float("inf")
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        dx, dy = x2 - x1, y2 - y1
        length = dx * dx + dy * dy
        t = 0 if not length else min(1, max(0, -(x1 * dx + y1 * dy) / length))
        best = min(best, math.hypot(x1 + t * dx, y1 + t * dy))
    return best


def parse_overpass(data, lat, lng):
    """Reduce a combined Overpass response to stable, compact facts."""
    roads, main_roads, railways = [], [], []
    amenities = {name: [] for name in ("supermarket", "park", "gym", "cafe", "pharmacy")}
    for element in data.get("elements", []):
        tags = element.get("tags") or {}
        point = _element_point(element)
        highway = tags.get("highway")
        if highway:
            distance = _geometry_distance(element, lat, lng)
            if distance is None:
                continue
            road = {"class": highway, "distance_m": round(distance)}
            roads.append(road)
            if highway in ("motorway", "trunk", "primary"):
                main_roads.append(road)
        railway = tags.get("railway")
        if railway in ("rail", "light_rail", "tram", "subway"):
            distance = _geometry_distance(element, lat, lng)
            if distance is not None:
                railways.append({"distance_m": round(distance)})
        if point is None:
            continue
        distance = round(_distance_m(lat, lng, *point))
        if tags.get("shop") == "supermarket":
            category = "supermarket"
        elif tags.get("leisure") in ("park", "garden", "nature_reserve", "common") \
                or tags.get("landuse") in ("recreation_ground", "village_green"):
            category = "park"
        elif tags.get("amenity") in ("gym", "fitness_centre") or tags.get("leisure") == "fitness_centre":
            category = "gym"
        elif tags.get("amenity") in ("cafe", "pharmacy"):
            category = tags["amenity"]
        else:
            continue
        if distance <= 600:
            name = " ".join((tags.get("name") or tags.get("brand") or "").split())
            if category == "park" and element.get("type") == "way" and not name:
                continue
            item = {"name": name or category.title(), "distance_m": distance, "_point": point}
            duplicate = next((old for old in amenities[category]
                              if (name and old["_name"].casefold() == name.casefold()
                                  and (category == "park" or
                                       _distance_m(*old["_point"], *point) < 50))
                              or (not name and old["_id"] == (element.get("type"), element.get("id")))),
                             None)
            if duplicate:
                if distance < duplicate["distance_m"]:
                    duplicate.update(item)
            else:
                item["_name"] = name
                item["_id"] = (element.get("type"), element.get("id"))
                amenities[category].append(item)

    nearest = lambda values: min(values, key=lambda item: item["distance_m"]) if values else None
    clean_amenities = {}
    for kind, items in amenities.items():
        closest = nearest(items)
        clean_amenities[kind] = {
            "count": len(items),
            "nearest": ({key: value for key, value in closest.items() if not key.startswith("_")}
                        if closest else None),
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


def _overpass(lat, lng):
    global _overpass_next
    with _overpass_lock:
        delay = _overpass_next - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        try:
            response = requests.post(
                OVERPASS_URL, data={"data": _query(lat, lng)},
                headers={"User-Agent": OVERPASS_UA, "Accept": "application/json"},
                timeout=18,
            )
            response.raise_for_status()
            return response.json()
        finally:
            _overpass_next = time.monotonic() + OVERPASS_GAP_SECONDS


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
    """Return OSM/TfL facts, cached for 90 days per approximately 100 m cell."""
    if lat is None or lng is None:
        return None
    now = time.time() if now is None else now
    key = _cell(lat, lng)
    hit = cache.get("place_facts_v1", key)
    if hit and now - hit.get("fetched_at", 0) < CACHE_TTL_SECONDS:
        return hit.get("facts")

    osm = parse_overpass(_overpass(lat, lng), lat, lng)
    transit = parse_tfl(_tfl(lat, lng))
    noise = _noise(lat, lng)
    result = {**osm, "transit": transit, "noise": noise}
    cache.put("place_facts_v1", key, {"fetched_at": now, "facts": result})
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
                    f"not found within {OVERPASS_ROAD_RADIUS_M} m"))
    lines.append("Nearest railway: " + (f"~{rail['distance_m']} m" if rail else
                                       f"not found within {OVERPASS_ROAD_RADIUS_M} m"))
    amenities = facts.get("amenities") or {}
    for category in ("supermarket", "park", "gym", "cafe", "pharmacy"):
        info = amenities.get(category) or {}
        nearest = info.get("nearest")
        distance = f"; nearest {nearest['name']} (~{nearest['distance_m']} m)" if nearest else ""
        lines.append(f"{category.title()}: {info.get('count', 0)} within {AMENITY_RADIUS_M} m{distance}")
    transit = facts.get("transit") or {}
    lines.append(f"TfL: {transit.get('rail_lines', 0)} distinct rail/tube lines within 800 m; "
                 f"{transit.get('bus_routes', 0)} bus routes within 300 m")
    noise = facts.get("noise") or {}
    road, railway = noise.get("road_lnight_db"), noise.get("rail_lnight_db")
    lines.append("Defra Round 4 Lnight (2021 model year, 4 m receptor, 23:00–07:00): "
                 f"road {f'{road:g} dB' if road is not None else 'no value'}; "
                 f"rail {f'{railway:g} dB' if railway is not None else 'no value'}")
    return lines
