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
    return f"""[out:json][timeout:25];
(
  nwr(around:800,{lat:.6f},{lng:.6f})[highway~"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service)$"];
)->.roads;
(
  nwr(around:800,{lat:.6f},{lng:.6f})[railway~"^(rail|light_rail|tram|subway)$"];
)->.rail;
(
  nwr(around:600,{lat:.6f},{lng:.6f})[shop=supermarket];
  nwr(around:600,{lat:.6f},{lng:.6f})[leisure~"^(park|garden|nature_reserve|common|fitness_centre)$"];
  nwr(around:600,{lat:.6f},{lng:.6f})[landuse~"^(recreation_ground|village_green)$"];
  nwr(around:600,{lat:.6f},{lng:.6f})[amenity~"^(gym|fitness_centre|cafe|pharmacy)$"];
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
                timeout=35,
            )
            response.raise_for_status()
            return response.json()
        finally:
            _overpass_next = time.monotonic() + 4


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


def street_label(facts):
    if not facts:
        return ""
    main = facts.get("main_road")
    rail = facts.get("railway")
    street = facts.get("street")
    if main and main["distance_m"] <= 50:
        return "On a main road"
    if rail and rail["distance_m"] <= 50:
        return "Beside a railway"
    if street and street["class"] in ("residential", "living_street", "unclassified", "service") \
            and (not main or main["distance_m"] > 500) and (not rail or rail["distance_m"] > 500):
        return "Quiet residential street"
    return ""


def card_line(facts):
    """One compact, HTML-safe-at-the-caller card line; empty when no facts are available."""
    if not facts:
        return ""
    parts = []
    street = street_label(facts)
    if street:
        parts.append(street)
    transit = facts.get("transit") or {}
    if transit.get("rail_lines") or transit.get("bus_routes"):
        lines, routes = transit.get("rail_lines", 0), transit.get("bus_routes", 0)
        parts.append(f"{lines} line{'s' if lines != 1 else ''} + {routes} bus route{'s' if routes != 1 else ''}")
    noise = facts.get("noise") or {}
    levels = [f"{source} {noise[key]:g} dB" for source, key in
              (("road", "road_lnight_db"), ("rail", "rail_lnight_db")) if noise.get(key) is not None]
    if levels:
        parts.append("Lnight " + ", ".join(levels))
    labels = {"supermarket": "shop", "park": "park", "gym": "gym", "cafe": "cafe",
              "pharmacy": "pharmacy"}
    plurals = {"supermarket": "shops", "park": "parks", "gym": "gyms", "cafe": "cafes",
               "pharmacy": "pharmacies"}
    for category, label in labels.items():
        data = (facts.get("amenities") or {}).get(category) or {}
        count, nearest = data.get("count", 0), data.get("nearest")
        if not count:
            continue
        if nearest:
            name = nearest.get("name", label.title())
            nearby = f"{name} ~{nearest['distance_m']} m"
            parts.append(nearby if count == 1 else f"{plurals[category]} {count} ({nearby})")
        else:
            parts.append(f"{plurals[category]} {count}")
    return "🏘 " + " · ".join(parts) if parts else ""
