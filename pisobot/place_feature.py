"""Published neighbourhood facts for the optional G4 feature."""
import html
import json
import logging
import math
import re
import statistics
import time
from pathlib import Path

from . import models, optional, place
from .optional import store

log = logging.getLogger(__name__)
AREA_TTL_SECONDS = 30 * 24 * 60 * 60
ADVERT_TTL_SECONDS = AREA_TTL_SECONDS
AREA_SAMPLE_LIMIT = 20
SNAPSHOT_REFRESH_SECONDS = 60
ARCHIVE_SQL = """
SELECT a.key, a.address, a.lat, a.lng, a.rent, a.bedrooms,
       (SELECT MAX(v.crime_score) FROM verdicts AS v
        WHERE v.key = a.key AND v.outcome = 'matched') AS crime_score
FROM adverts AS a
WHERE a.status = 'active' AND a.lat IS NOT NULL AND a.lng IS NOT NULL
ORDER BY a.last_seen DESC
LIMIT 5000
"""


def district_for_address(value):
    match = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(?:\d[A-Z]{2})?\b",
                      str(value or "").upper())
    return models.normalise_district(match.group(1)) if match else None


def _coordinates(row):
    lat, lng = row.get("lat"), row.get("lng")
    if (type(lat) not in (int, float) or type(lng) not in (int, float)
            or not math.isfinite(lat) or not math.isfinite(lng)
            or not -90 <= lat <= 90 or not -180 <= lng <= 180):
        return None
    return float(lat), float(lng)


def _load_rows():
    try:
        return store.read_primary(ARCHIVE_SQL)
    except Exception as error:
        log.warning("Place facts archive read failed: %s", type(error).__name__)
        return []


def _optional_rows(sql, params=()):
    return store.read(sql, params)


def _cache_osm(key, lat, lng, index_mtime, now):
    cached = _optional_rows(
        "SELECT index_mtime, updated_at, value FROM place_advert_facts WHERE key = ?", (key,))
    if cached:
        record = cached[0]
        if (record.get("index_mtime") == index_mtime
                and 0 <= now - record.get("updated_at", 0) < ADVERT_TTL_SECONDS):
            try:
                facts = json.loads(record["value"])
                if place._valid_facts(facts):
                    return facts
            except (ValueError, TypeError, KeyError) as error:
                log.warning("Place facts cache record is invalid: %s", type(error).__name__)
    facts = place.local_osm_facts(lat, lng)
    if facts is None or not place._valid_facts(facts):
        return None
    if not store.write(place.PLACE_SCHEMA, [
            ("INSERT INTO place_advert_facts (key, index_mtime, updated_at, value) VALUES (?, ?, ?, ?) "
             "ON CONFLICT(key) DO UPDATE SET index_mtime=excluded.index_mtime, "
             "updated_at=excluded.updated_at, value=excluded.value",
             (key, index_mtime, now, json.dumps(facts, ensure_ascii=False, sort_keys=True))) ]):
        log.warning("Place facts local snapshot cache write failed")
    return facts


def _provider_facts(lat, lng, now):
    result = {}
    cell = place._cell(lat, lng)
    transit = _optional_rows(
        "SELECT fetched_at, value FROM place_transit_answers WHERE cell = ?", (cell,))
    if transit and 0 <= now - transit[0].get("fetched_at", 0) < place.CACHE_TTL_SECONDS:
        try:
            value = json.loads(transit[0]["value"])
            if place._valid_transit(value):
                result["transit"] = value
        except (ValueError, TypeError, KeyError) as error:
            log.warning("Place facts cached TfL record is invalid: %s", type(error).__name__)
    noise = _optional_rows(
        "SELECT layer, value FROM place_noise_answers WHERE cell = ?", (cell,))
    values = {row["layer"]: row["value"] for row in noise}
    mapped = {}
    for layer, key in ((place.NOISE_SERVICES[0][1], place.NOISE_SERVICES[0][2]),
                       (place.NOISE_SERVICES[1][1], place.NOISE_SERVICES[1][2])):
        if layer in values and place._valid_noise_value(values[layer]):
            mapped[key] = values[layer]
    if mapped:
        result["noise"] = mapped
    return result


def _market_rows(rows):
    grouped = {}
    for row in rows:
        district = district_for_address(row.get("address"))
        if district:
            grouped.setdefault(district, []).append(row)
    return grouped


def _area_record(area, rows, all_area_crime, index_mtime, now):
    by_location = {}
    for row in rows:
        coords = _coordinates(row)
        if coords:
            by_location.setdefault((round(coords[0], 5), round(coords[1], 5)), (row, coords))
    samples = list(by_location.values())[:AREA_SAMPLE_LIMIT]
    osm_facts = []
    provider_records = []
    for row, (lat, lng) in samples:
        facts = _cache_osm(str(row.get("key") or ""), lat, lng, index_mtime, now)
        if facts is None:
            continue
        osm_facts.append(facts)
        provider_records.append(_provider_facts(lat, lng, now))

    sample_n = len(osm_facts)
    near_green = sum(
        bool((facts.get("amenities", {}).get("park") or {}).get("nearest")
             and facts["amenities"]["park"]["nearest"]["distance_m"] <= place.AREA_AMENITY_RADIUS_M)
        for facts in osm_facts)
    count_averages = {}
    for key, categories in (
            ("cafes", ("cafe",)), ("pubs", ("pub",)), ("restaurants", ("restaurant",)),
            ("shops", ("supermarket", "convenience", "shop", "pharmacy"))):
        count_averages[key] = (
            round(sum((facts.get("amenities", {}).get(cat) or {}).get("count", 0)
                      for facts in osm_facts for cat in categories) / sample_n, 1)
            if sample_n else None)

    main_road_pct = (
        round(100 * sum(bool(f.get("main_road") and f["main_road"]["distance_m"] <= 200)
                        for f in osm_facts) / sample_n)
        if sample_n else None)
    rail_pct = (
        round(100 * sum(bool(f.get("railway") and f["railway"]["distance_m"] <= 100)
                        for f in osm_facts) / sample_n)
        if sample_n else None)
    rail_lines = [p["transit"]["rail_lines"] for p in provider_records if "transit" in p]
    bus_routes = [p["transit"]["bus_routes"] for p in provider_records if "transit" in p]
    noise_values = []
    for provider in provider_records:
        noise = provider.get("noise") or {}
        if all(key in noise for key in ("road_lnight_db", "rail_lnight_db")):
            values = [value for value in noise.values() if value is not None]
            if values:
                noise_values.append(max(values))

    one_bed_rents = [
        float(row["rent"]) for row in rows
        if row.get("bedrooms") == 1 and type(row.get("rent")) in (int, float)
        and math.isfinite(row["rent"]) and row["rent"] > 0
    ]
    crime_values = [
        int(row["crime_score"]) for row in rows
        if type(row.get("crime_score")) is int and row["crime_score"] >= 0
    ]
    crime_median = statistics.median(crime_values) if crime_values else None
    comparison_values = [value for value in all_area_crime if value is not None]
    comparison_median = statistics.median(comparison_values) if len(comparison_values) >= 3 else None
    record = {
        "area": area,
        "area_name": models.LONDON_AREAS.get(area, area),
        "generated_at": now,
        "unit": "UK postcode district",
        "sample_size": sample_n,
        "green_space_share_pct": round(100 * near_green / sample_n) if sample_n else None,
        "green_space_definition": "share of sampled advert locations with mapped green space within 600 m",
        "poi_density_per_location": count_averages,
        "main_road_within_200m_pct": main_road_pct,
        "surface_rail_within_100m_pct": rail_pct,
        "tfl_sample_size": len(rail_lines),
        "tfl_rail_lines_mean": round(statistics.mean(rail_lines), 1) if rail_lines else None,
        "tfl_bus_routes_mean": round(statistics.mean(bus_routes), 1) if bus_routes else None,
        "median_lnight_db": round(statistics.median(noise_values), 1) if noise_values else None,
        "noise_sample_size": len(noise_values),
        "one_bed_median_rent": round(statistics.median(one_bed_rents), 2) if one_bed_rents else None,
        "one_bed_advert_count": len(one_bed_rents),
        "market_source": "active archive adverts",
        "crime_median": crime_median,
        "crime_sample_size": len(crime_values),
        "crime_comparison_median": comparison_median,
        "crime_comparison_area_count": len(comparison_values) if comparison_median is not None else 0,
        "comparison_base": ("median of available cached matched-advert scores in the postcode districts "
                            "seen in this archive; not London-wide or population-adjusted"),
    }
    record["text"] = _overview_text(record)
    return record


def _overview_text(record):
    poi = record["poi_density_per_location"]
    if record["sample_size"]:
        density = ", ".join(
            f"{value:g} {label}" for key, label in
            (("cafes", "cafes"), ("pubs", "pubs"), ("restaurants", "restaurants"), ("shops", "shops"))
            if (value := poi.get(key)) is not None)
        green = (f"Mapped green space is within 600 m of {record['green_space_share_pct']}% "
                 f"of {record['sample_size']} sampled advert locations"
                 if record["green_space_share_pct"] is not None else
                 f"Local OSM coverage is based on {record['sample_size']} sampled advert locations")
        sentence1 = green + (f"; typical nearby counts within 600 m are {density}." if density
                             else "; nearby venue counts are not available.")
    else:
        sentence1 = "Local OSM coverage is not available for sampled adverts in this district."

    access = []
    if record["main_road_within_200m_pct"] is not None:
        access.append(f"main roads within 200 m at {record['main_road_within_200m_pct']}% of samples")
    if record["surface_rail_within_100m_pct"] is not None:
        access.append(f"surface rail within 100 m at {record['surface_rail_within_100m_pct']}%")
    if record["tfl_rail_lines_mean"] is not None:
        access.append(f"cached TfL average {record['tfl_rail_lines_mean']:g} rail lines")
    if record.get("median_lnight_db") is not None:
        access.append(f"cached Defra median Lnight {record['median_lnight_db']:g} dB "
                      f"from {record.get('noise_sample_size', 0)} samples")
    if record["one_bed_median_rent"] is not None:
        access.append(f"1-bed advertised median £{record['one_bed_median_rent']:,.0f}/mo "
                      f"from {record['one_bed_advert_count']} "
                      f"{record.get('market_source', 'active adverts')}")
    sentence2 = ("; ".join(access).capitalize() + "." if access
                 else "Road, rail, TfL and rent comparisons are not available in the local archive.")

    comparison = record["crime_comparison_median"]
    score = record["crime_median"]
    if score is not None and comparison is not None:
        relation = ("above" if score > comparison * 1.1 else
                    "below" if score < comparison * 0.9 else "around")
        sentence3 = (f"Cached police-crime scores are {relation} the sampled-area median "
                     f"({comparison:g}) across {record['crime_comparison_area_count']} districts; "
                     "this is not a London-wide or population-adjusted risk measure.")
    elif score is not None:
        sentence3 = ("Cached matched-advert police-crime scores are available, but fewer than three "
                     "sampled districts support a comparison.")
    else:
        sentence3 = "No cached matched-advert police-crime score is available for comparison."
    return " ".join((sentence1, sentence2, sentence3))


def _save_area(record):
    return store.write(place.PLACE_SCHEMA, [
        ("INSERT INTO place_areas (area, generated_at, value) VALUES (?, ?, ?) "
         "ON CONFLICT(area) DO UPDATE SET generated_at=excluded.generated_at, value=excluded.value",
         (record["area"], record["generated_at"], json.dumps(record, ensure_ascii=False, sort_keys=True)))
    ])


def _area_line(record):
    if not record:
        return ""
    details = []
    green = record.get("green_space_share_pct")
    if type(green) in (int, float) and math.isfinite(green) and 0 <= green <= 100:
        details.append(f"parks within 600 m at {green:g}% of samples")
    transit = record.get("tfl_rail_lines_mean")
    if type(transit) in (int, float) and math.isfinite(transit) and transit >= 0:
        details.append(f"TfL avg {transit:g} lines")
    crime, baseline = record.get("crime_median"), record.get("crime_comparison_median")
    if (type(crime) in (int, float) and math.isfinite(crime) and crime >= 0
            and type(baseline) in (int, float) and math.isfinite(baseline) and baseline >= 0):
        if crime > baseline * 1.1:
            details.append("above sampled-area crime median")
        elif crime < baseline * 0.9:
            details.append("below sampled-area crime median")
    if not details:
        return ""
    area, name = record.get("area"), record.get("area_name")
    if not isinstance(area, str) or not isinstance(name, str):
        return ""
    return f"Area: {area} {name} — " + " · ".join(details)


class PlaceFeature:
    """Runs only in the G0 feature worker and supervised child, never in the alert path."""

    def __init__(self):
        self._last_scan = 0.0
        self._dirty = True
        self._last_areas = {}

    def _rebuild(self):
        index = Path(place.LOCAL_INDEX_PATH)
        if not index.is_file():
            return
        index_mtime = index.stat().st_mtime
        now = time.time()
        rows = _load_rows()
        if not rows:
            return
        by_area = _market_rows(rows)
        by_area = {area: group for area, group in by_area.items()}
        existing = {
            row["area"]: row for row in _optional_rows(
                "SELECT area, generated_at, value FROM place_areas")
            if isinstance(row.get("area"), str)
        }
        due = {
            area: group for area, group in by_area.items()
            if (area not in existing or not isinstance(existing[area].get("generated_at"), (int, float))
                or not 0 <= now - existing[area]["generated_at"] < AREA_TTL_SECONDS)
        }
        crimes = []
        for group in by_area.values():
            values = [int(row["crime_score"]) for row in group
                      if type(row.get("crime_score")) is int and row["crime_score"] >= 0]
            crimes.append(statistics.median(values) if values else None)
        updates = {}
        for area, group in due.items():
            record = _area_record(area, group, crimes, index_mtime, now)
            if _save_area(record):
                updates[area] = record
            else:
                log.warning("Place area overview cache write failed for %s", area)

        fact_cache = {
            row["key"]: row for row in _optional_rows(
                "SELECT key, index_mtime, updated_at, value FROM place_advert_facts "
                "ORDER BY updated_at DESC LIMIT 2000")
            if isinstance(row.get("key"), str)
        }
        lines = {}
        for row in rows[:optional.SNAPSHOT_MAX]:
            key = row.get("key")
            coords = _coordinates(row)
            if not isinstance(key, str) or not coords:
                continue
            facts = None
            cached = fact_cache.get(key)
            if (cached and cached.get("index_mtime") == index_mtime
                    and 0 <= now - cached.get("updated_at", 0) < ADVERT_TTL_SECONDS):
                try:
                    value = json.loads(cached["value"])
                    if place._valid_facts(value):
                        facts = value
                except (ValueError, TypeError, KeyError) as error:
                    log.warning("Place facts advert cache is invalid: %s", type(error).__name__)
            if facts is None:
                facts = _cache_osm(key, *coords, index_mtime, now)
            if facts is None:
                continue
            current = {**facts, **_provider_facts(*coords, now)}
            offers = []
            tile = place.amenities_line(current)
            if tile:
                offers.append((100, tile))
            label = place.card_line(current)
            if label:
                offers.append((50, label))
            district = district_for_address(row.get("address"))
            if district:
                area_row = updates.get(district) or existing.get(district)
                if area_row:
                    try:
                        record = (area_row if "text" in area_row else json.loads(area_row["value"]))
                    except (ValueError, TypeError, KeyError):
                        record = None
                    line = _area_line(record)
                    if line:
                        offers.append((10, line))
            if offers:
                lines[key] = offers
            cell = place._cell(*coords)
            if _needs_refresh(cell, now):
                store.write(place.PLACE_SCHEMA, [
                    ("INSERT INTO place_refresh_queue (cell, lat, lng, queued_at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(cell) DO UPDATE SET lat=excluded.lat, lng=excluded.lng",
                     (cell, coords[0], coords[1], now))
                ])
        optional.publish_lines("place-facts", lines)
        self._last_areas = {area: (updates.get(area) or _decode_area(existing.get(area)))
                            for area in by_area}
        self._last_scan = time.monotonic()
        self._dirty = False

    def has_work(self):
        if self._dirty or time.monotonic() - self._last_scan >= SNAPSHOT_REFRESH_SECONDS:
            try:
                self._rebuild()
            except Exception as error:
                log.warning("Place facts snapshot refresh failed: %s", type(error).__name__)
                self._last_scan = time.monotonic()
                self._dirty = False
        return bool(self._due_queue())

    def after_cycle(self, _items):
        self._dirty = True
        self._rebuild()
        return None

    def _due_queue(self):
        rows = _optional_rows("SELECT cell, lat, lng FROM place_refresh_queue ORDER BY queued_at LIMIT 100")
        due = []
        now = time.time()
        for row in rows:
            if _needs_refresh(row["cell"], now):
                due.append(row)
        return due

    def step(self, deadline):
        for row in self._due_queue():
            if time.monotonic() >= deadline:
                return
            try:
                place.refresh_network(row["lat"], row["lng"])
                if not _needs_refresh(row["cell"], time.time()):
                    store.write(place.PLACE_SCHEMA, [
                        ("DELETE FROM place_refresh_queue WHERE cell = ?", (row["cell"],))
                    ])
            except Exception as error:
                log.warning("Place facts refresh failed: %s", type(error).__name__)


def _decode_area(row):
    if not row:
        return None
    try:
        record = json.loads(row["value"])
        if (not isinstance(record, dict) or not isinstance(record.get("text"), str)
                or len(record["text"]) > 3000):
            return None
        return record
    except (ValueError, TypeError, KeyError):
        return None


def _needs_refresh(cell, now):
    answers = {row["layer"] for row in _optional_rows(
        "SELECT layer FROM place_noise_answers WHERE cell = ?", (cell,))}
    blocked = _optional_rows("SELECT value FROM place_noise_state WHERE key = 'blocked_until'")
    defra_blocked = bool(blocked and blocked[0].get("value", 0) > now)
    noise_due = not defra_blocked and any(
        layer not in answers and not (
            (failure := _optional_rows(
                "SELECT retry_after FROM place_noise_failures WHERE cell = ? AND layer = ?",
                (cell, layer))) and failure[0].get("retry_after", 0) > now)
        for layer in (place.NOISE_SERVICES[0][1], place.NOISE_SERVICES[1][1]))
    transit = _optional_rows(
        "SELECT fetched_at FROM place_transit_answers WHERE cell = ?", (cell,))
    transit_fresh = bool(transit and 0 <= now - transit[0].get("fetched_at", 0) < place.CACHE_TTL_SECONDS)
    transit_failure = _optional_rows(
        "SELECT retry_after FROM place_transit_failures WHERE cell = ?", (cell,))
    transit_blocked = bool(transit_failure and transit_failure[0].get("retry_after", 0) > now)
    return noise_due or (not transit_fresh and not transit_blocked)


def area_reply(query):
    """A read-only /area response, served only from the optional database cache."""
    text = " ".join(str(query or "").split()).upper()
    district = district_for_address(text)
    if district is None:
        district = models.normalise_district(text.replace(" ", ""))
    if district is None:
        return "Use /area followed by a London postcode district or full postcode, for example /area N7."
    rows = store.read("SELECT generated_at, value FROM place_areas WHERE area = ?", (district,))
    record = _decode_area(rows[0]) if rows else None
    if not record or not isinstance(record.get("text"), str):
        return f"No cached overview for {district} yet."
    name = record.get("area_name")
    if not isinstance(name, str) or len(name) > 80:
        name = district
    return (f"<b>Area: {html.escape(district)} {html.escape(name)}</b>\n"
            f"{html.escape(record['text'], quote=False)}")
