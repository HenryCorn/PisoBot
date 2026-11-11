"""Incremental G4 cache jobs; the delivery process only publishes completed rows."""
import hashlib
import html
import json
import logging
import re
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from . import models, optional, place
from .optional import store

log = logging.getLogger(__name__)
AREA_TTL_SECONDS = 30 * 24 * 60 * 60
ADVERT_TTL_SECONDS = AREA_TTL_SECONDS
AREA_SAMPLE_LIMIT = 20
MIN_SAMPLE = 5
ARCHIVE_LIMIT = store.MAX_ROWS - 1
SNAPSHOT_REFRESH_SECONDS = 60
SNAPSHOT_MAX_AGE_SECONDS = 24 * 60 * 60
LOCAL_BATCH_SIZE = 100
LOCAL_SECONDS = 15
LOCAL_RETRY_SECONDS = 3600
PROVIDER_SECONDS = 12
WRITE_RESERVE_SECONDS = 2
ARCHIVE_COLUMNS = ("key", "address", "lat", "lng", "rent", "bedrooms", "last_seen", "crime_score")
ARCHIVE_SQL = f"""
WITH recent AS (
    SELECT key, address, lat, lng, rent, bedrooms, last_seen
    FROM adverts WHERE status = 'active' AND lat IS NOT NULL AND lng IS NOT NULL
    ORDER BY last_seen DESC, key LIMIT {ARCHIVE_LIMIT}
), crime AS (
    SELECT key, MAX(crime_score) AS crime_score
    FROM verdicts WHERE outcome = 'matched' GROUP BY key
)
SELECT a.key, a.address, a.lat, a.lng, a.rent, a.bedrooms, a.last_seen, c.crime_score
FROM recent AS a LEFT JOIN crime AS c ON c.key = a.key
ORDER BY a.last_seen DESC, a.key
"""
_POSTCODE = re.compile(
    r"(?<![A-Z0-9])(?P<out>[A-Z]{1,2}\d{1,2}[A-Z]?)"
    r"(?:\s*(?P<in>\d[ABD-HJLNP-UW-Z]{2}))?(?![A-Z0-9])")
_SUBDISTRICTS = {"EC1", "EC2", "EC3", "EC4", "WC1", "WC2", "W1", "SW1"}
_PROVIDER_COLUMNS = (
    "cell", "lat", "lng", "queued_at", "transit_at", "transit", "road_at", "road",
    "rail_at", "rail", "transit_retry", "road_retry", "rail_retry",
)
PROVIDER_SQL = f"""
SELECT q.cell, q.lat, q.lng, q.queued_at, t.fetched_at AS transit_at, t.value AS transit,
       r.fetched_at AS road_at, r.value AS road, l.fetched_at AS rail_at, l.value AS rail,
       tf.retry_after AS transit_retry, rf.retry_after AS road_retry, lf.retry_after AS rail_retry
FROM place_refresh_queue AS q
LEFT JOIN place_transit_answers AS t ON t.cell = q.cell
LEFT JOIN place_noise_answers AS r ON r.cell = q.cell AND r.layer = ?
LEFT JOIN place_noise_answers AS l ON l.cell = q.cell AND l.layer = ?
LEFT JOIN place_transit_failures AS tf ON tf.cell = q.cell
LEFT JOIN place_noise_failures AS rf ON rf.cell = q.cell AND rf.layer = ?
LEFT JOIN place_noise_failures AS lf ON lf.cell = q.cell AND lf.layer = ?
ORDER BY q.queued_at, q.cell LIMIT {ARCHIVE_LIMIT}
"""


def _district(outcode):
    known = models.normalise_district(outcode)
    if known:
        return known
    if outcode == "E1W":
        return "E1"
    if outcode[-1:].isalpha() and outcode[:-1] in _SUBDISTRICTS:
        return outcode[:-1]
    return None


def district_for_address(value):
    text = str(value or "").upper()
    for candidate in reversed(list(_POSTCODE.finditer(text))):
        district = _district(candidate["out"])
        if (district and (candidate["in"] or not re.search(
                r"\b(?:FLAT|UNIT|APARTMENT|APT|BLOCK|SUITE)\s*$", text[:candidate.start()]))):
            return district
    return None


def _coordinates(row):
    lat, lng = row.get("lat"), row.get("lng")
    if (not place.number(lat) or not place.number(lng)
            or not -90 <= lat <= 90 or not -180 <= lng <= 180):
        return None
    return float(lat), float(lng)


def _completed_read(sql, columns, params=(), *, primary=False):
    # G0 returns [] both on failure and for no rows. A completion row is part of
    # this same bounded SELECT, so even a valid empty result is unambiguous.
    query = (f"WITH result AS ({sql}) SELECT *, 0 AS _complete FROM result "
             f"UNION ALL SELECT {', '.join('NULL' for _ in columns)}, 1")
    rows = (store.read_primary if primary else store.read)(query, params)
    if not rows or not any(row.get("_complete") == 1 for row in rows):
        raise OSError("bounded archive read unavailable" if primary else "bounded optional cache read unavailable")
    return [{name: row.get(name) for name in columns} for row in rows if row.get("_complete") == 0]


def _load_rows():
    try:
        rows = _completed_read(ARCHIVE_SQL, ARCHIVE_COLUMNS, primary=True)
    except OSError:
        log.warning("Place archive read failed, timed out or is unavailable; retaining unexpired cache")
        return None
    good = [row for row in rows if isinstance(row.get("key"), str)
            and 0 < len(row["key"]) <= 200 and _coordinates(row)]
    if len(good) != len(rows):
        log.warning("Place archive omitted %d malformed coordinate records", len(rows) - len(good))
    return good


def _cache_rows(table, columns):
    return _completed_read(
        f"SELECT {', '.join(columns)} FROM {table} LIMIT {ARCHIVE_LIMIT}", columns)


def _json(value, validator, label):
    try:
        if not isinstance(value, str) or len(value) > 20000:
            raise ValueError("invalid cache value size")
        decoded = json.loads(value)
        if validator(decoded):
            return decoded
    except (ValueError, TypeError, KeyError):
        pass
    log.warning("Place %s cache record is invalid; omitting and rebuilding", label)
    return None


def _valid_osm(value):
    return (place._valid_facts(value)
            and {"street", "main_road", "railway", "amenities"}.issubset(value))


def _write_batches(statements, deadline):
    for start in range(0, len(statements), store.MAX_STATEMENTS):
        if time.monotonic() >= deadline:
            raise place.RefreshDeferred("Place cache batch deadline reached")
        if not store.write("", statements[start:start + store.MAX_STATEMENTS]):
            raise OSError("Place cache transaction failed")


def _delete_keys(table, column, keys):
    keys = list(keys)
    return [(f"DELETE FROM {table} WHERE {column} IN ({','.join('?' for _ in batch)})", tuple(batch))
            for start in range(0, len(keys), 200) if (batch := keys[start:start + 200])]


def _provider_rows():
    road, rail = place.NOISE_SERVICES[0][1], place.NOISE_SERVICES[1][1]
    return {row["cell"]: row for row in _completed_read(
        PROVIDER_SQL, _PROVIDER_COLUMNS, (road, rail, road, rail)) if isinstance(row.get("cell"), str)}


def _provider_record(row, now, noise_state):
    facts, due = {}, []
    transit = None
    if place.fresh(row.get("transit_at"), now, place.CACHE_TTL_SECONDS):
        transit = _json(row.get("transit"), place._valid_transit, "TfL")
    if transit is not None:
        facts["transit"] = transit
        due.append(row["transit_at"] + place.CACHE_TTL_SECONDS)
    else:
        retry = row.get("transit_retry")
        due.append(max(now, retry) if place.number(retry) else now)
    noise = {}
    for prefix, key in (("road", "road_lnight_db"), ("rail", "rail_lnight_db")):
        timestamp, value = row.get(prefix + "_at"), row.get(prefix)
        if place.fresh(timestamp, now, place.CACHE_TTL_SECONDS) and place._valid_noise_value(value):
            noise[key] = value
            due.append(timestamp + place.CACHE_TTL_SECONDS)
        else:
            if place.fresh(timestamp, now, place.CACHE_TTL_SECONDS) and not place._valid_noise_value(value):
                log.warning("Place noise cache record is invalid; omitting and refreshing")
            waits = [row.get(prefix + "_retry"), noise_state.get("blocked_until"),
                     noise_state.get("next_request_at")]
            due.append(max([now] + [value for value in waits if place.number(value)]))
    if noise:
        facts["noise"] = noise
    return facts, min(due)


def _locations(rows):
    locations = {}
    for row in rows:
        coords = _coordinates(row)
        if coords:
            locations.setdefault(tuple(round(value, 5) for value in coords), []).append(row)
    return locations


def _crime_values(rows):
    values = []
    for group in _locations(rows).values():
        scores = [row["crime_score"] for row in group
                  if type(row.get("crime_score")) is int and row["crime_score"] >= 0]
        if scores:
            values.append(statistics.median(scores))
    return values


def _area_record(area, rows, fact_cache, providers, comparison, now, market_source="active archive-window adverts"):
    locations = _locations(rows)
    available = [group[0] for _, group in sorted(locations.items()) if group[0]["key"] in fact_cache]
    if len(available) > AREA_SAMPLE_LIMIT:
        available = [available[round(i * (len(available) - 1) / (AREA_SAMPLE_LIMIT - 1))]
                     for i in range(AREA_SAMPLE_LIMIT)]
    facts = [fact_cache[row["key"]] for row in available]
    n = len(facts)
    provider_samples = [providers.get(place._cell(*_coordinates(row)), {}) for row in available]
    green = sum(bool((f.get("amenities", {}).get("park") or {}).get("nearest")
                     and f["amenities"]["park"]["nearest"]["distance_m"] <= place.AREA_AMENITY_RADIUS_M)
                for f in facts)
    density = {}
    for label, categories in (("cafes", ("cafe",)), ("pubs", ("pub",)), ("restaurants", ("restaurant",)),
                              ("shops", ("supermarket", "convenience", "shop", "pharmacy"))):
        density[label] = (round(sum((f.get("amenities", {}).get(cat) or {}).get("count", 0)
                                    for f in facts for cat in categories) / n, 1) if n else None)
    transit = [p["transit"] for p in provider_samples if "transit" in p]
    noise = [value for p in provider_samples if (value := place._night_noise(p)) is not None]
    rents = [float(row["rent"]) for row in rows if row.get("bedrooms") == 1
             and place.number(row.get("rent")) and row["rent"] > 0]
    crime = _crime_values(rows)
    record = {
        "version": place.CACHE_VERSION, "area": area, "area_name": models.LONDON_AREAS[area],
        "generated_at": now, "unit": "UK postcode district", "sample_size": n,
        "location_count": len(locations), "advert_count": len(rows),
        "green_space_share_pct": round(100 * green / n) if n else None,
        "green_space_definition": ("sample proximity to mapped green-space representative points, including gardens "
                                   "and recreation grounds; not public-park access or land-area coverage"),
        "poi_density_per_location": density,
        "main_road_within_200m_pct": (round(100 * sum(bool(f.get("main_road")
                                                   and f["main_road"]["distance_m"] <= 200) for f in facts) / n) if n else None),
        "surface_rail_within_100m_pct": (round(100 * sum(bool(f.get("railway")
                                                      and f["railway"]["distance_m"] <= 100) for f in facts) / n) if n else None),
        "tfl_sample_size": len(transit),
        "tfl_rail_lines_mean": (round(statistics.mean(p["rail_lines"] for p in transit), 1)
                               if len(transit) >= MIN_SAMPLE else None),
        "tfl_bus_routes_mean": (round(statistics.mean(p["bus_routes"] for p in transit), 1)
                               if len(transit) >= MIN_SAMPLE else None),
        "noise_sample_size": len(noise),
        "median_lnight_db": round(statistics.median(noise), 1) if len(noise) >= MIN_SAMPLE else None,
        "one_bed_advert_count": len(rents),
        "one_bed_median_rent": round(statistics.median(rents), 2) if len(rents) >= MIN_SAMPLE else None,
        "market_source": market_source,
        "crime_sample_size": len(crime),
        "crime_median": statistics.median(crime) if len(crime) >= MIN_SAMPLE else None,
        "crime_comparison_median": statistics.median(comparison) if len(comparison) >= 3 else None,
        "crime_comparison_area_count": len(comparison) if len(comparison) >= 3 else 0,
        "comparison_base": ("median of districts with at least five distinct matched-advert score locations; "
                            "already filtered by users' crime caps, not London-wide or population-adjusted"),
    }
    inputs = [(row["key"], _coordinates(row), row.get("rent"), row.get("bedrooms"), row.get("crime_score"))
              for row in rows]
    record["inputs"] = hashlib.sha256(json.dumps(
        [inputs, facts, provider_samples, comparison], sort_keys=True).encode()).hexdigest()
    record["text"] = _overview_text(record)
    return record


def _overview_text(record):
    n, total = record["sample_size"], record["location_count"]
    if n < MIN_SAMPLE:
        first = (f"Only {n} of {total} distinct advert locations have local OSM facts; "
                 f"fewer than {MIN_SAMPLE} is too sparse for area statistics.")
    else:
        density = ", ".join(f"{value:g} {label}" for label, value in record["poi_density_per_location"].items()
                            if value is not None)
        first = (f"Non-random sample of {n} of {total} advert locations, not representative of the district: "
                 f"mapped green-space points within 600 m at {record['green_space_share_pct']}%; "
                 f"mean nearby counts are {density}.")
    access = []
    if n >= MIN_SAMPLE:
        access.extend((f"main roads within 200 m at {record['main_road_within_200m_pct']}%",
                       f"surface rail within 100 m at {record['surface_rail_within_100m_pct']}%"))
    if record["tfl_rail_lines_mean"] is not None:
        access.append(f"cached TfL mean {record['tfl_rail_lines_mean']:g} rail lines "
                      f"from {record['tfl_sample_size']} locations")
    if record["median_lnight_db"] is not None:
        access.append(f"cached Defra median Lnight {record['median_lnight_db']:g} dB "
                      f"from {record['noise_sample_size']} locations")
    if record["one_bed_median_rent"] is not None:
        access.append(f"1-bed advertised median £{record['one_bed_median_rent']:,.0f}/mo "
                      f"from {record['one_bed_advert_count']} {record['market_source']}")
    else:
        access.append(f"only {record['one_bed_advert_count']} one-bed {record['market_source']}; no rent median")
    second = ("Cached observations: " + "; ".join(access) + "; mapped green includes gardens and recreation "
              "grounds, and point proximity is not park access or land-area coverage.")
    score, median = record["crime_median"], record["crime_comparison_median"]
    if score is not None and median is not None:
        relation = "above" if score > median * 1.1 else "below" if score < median * 0.9 else "around"
        third = (f"Crime scores from {record['crime_sample_size']} distinct matched-advert locations are {relation} "
                 f"the median ({median:g}) of {record['crime_comparison_area_count']} eligible sampled districts; ")
    else:
        third = (f"No crime comparison: at least {MIN_SAMPLE} scored locations per district and three districts "
                 "are required; ")
    third += "matched adverts already pass users' crime caps, so this is selection-biased, not London-wide or population-adjusted risk."
    return " ".join((first, second, third))


def _valid_area(record, now):
    if (not isinstance(record, dict) or record.get("version") != place.CACHE_VERSION
            or not isinstance(record.get("area"), str) or record["area"] not in models.LONDON_AREAS
            or not place.fresh(record.get("generated_at"), now, AREA_TTL_SECONDS)
            or not isinstance(record.get("inputs"), str) or len(record["inputs"]) != 64
            or not isinstance(record.get("market_source"), str) or len(record["market_source"]) > 80):
        return False
    for key in ("sample_size", "location_count", "advert_count", "tfl_sample_size", "noise_sample_size",
                "one_bed_advert_count", "crime_sample_size", "crime_comparison_area_count"):
        if type(record.get(key)) is not int or not 0 <= record[key] <= ARCHIVE_LIMIT:
            return False
    if record["sample_size"] > min(AREA_SAMPLE_LIMIT, record["location_count"]):
        return False
    if (record["tfl_sample_size"] > record["sample_size"] or record["noise_sample_size"] > record["sample_size"]
            or record["crime_sample_size"] > record["location_count"]
            or record["location_count"] > record["advert_count"]):
        return False
    for key in ("green_space_share_pct", "main_road_within_200m_pct", "surface_rail_within_100m_pct",
                "tfl_rail_lines_mean", "tfl_bus_routes_mean", "median_lnight_db", "one_bed_median_rent",
                "crime_median", "crime_comparison_median"):
        if key not in record or (record[key] is not None and
                                 (not place.number(record[key]) or record[key] < 0)):
            return False
        if key.endswith("_pct") and record[key] is not None and record[key] > 100:
            return False
        if key.endswith("_pct") and record["sample_size"] and record[key] is None:
            return False
    for key, count, minimum in (("tfl_rail_lines_mean", "tfl_sample_size", MIN_SAMPLE),
                                ("tfl_bus_routes_mean", "tfl_sample_size", MIN_SAMPLE),
                                ("median_lnight_db", "noise_sample_size", MIN_SAMPLE),
                                ("one_bed_median_rent", "one_bed_advert_count", MIN_SAMPLE),
                                ("crime_median", "crime_sample_size", MIN_SAMPLE),
                                ("crime_comparison_median", "crime_comparison_area_count", 3)):
        if record[key] is not None and record[count] < minimum:
            return False
    density = record.get("poi_density_per_location")
    return isinstance(density, dict) and set(density) == {"cafes", "pubs", "restaurants", "shops"} and all(
        value is None or (place.number(value) and value >= 0) for value in density.values())


def _decode_area(row, now=None):
    now = time.time() if now is None else now
    if not isinstance(row, dict) or not place.fresh(row.get("generated_at"), now, AREA_TTL_SECONDS):
        return None
    record = _json(row.get("value"), lambda value: _valid_area(value, now), "area")
    if record is None or record["generated_at"] != row["generated_at"] or record["area"] != row.get("area"):
        return None
    record["area_name"] = models.LONDON_AREAS[record["area"]]
    record["text"] = _overview_text(record)
    return record


def _area_line(record):
    if not _valid_area(record, time.time()):
        return ""
    day = datetime.fromtimestamp(record["generated_at"], timezone.utc).strftime("%d %b")
    sample = (f"only {record['sample_size']} locations, too sparse" if record["sample_size"] < MIN_SAMPLE
              else f"{record['sample_size']} advert locations, biased sample")
    return f"Area: {record['area']} {record['area_name']} — {sample} ({day}); /area {record['area']}"


def _valid_lines(value):
    return isinstance(value, list) and len(value) <= 3 and all(
        isinstance(offer, list) and len(offer) == 2 and type(offer[0]) is int
        and isinstance(offer[1], str) and 0 < len(offer[1]) <= optional.LINE_MAX for offer in value)


class PlaceFeature:
    """Only step() computes facts; has_work() publishes already-computed bounded rows."""

    def __init__(self):
        self._dirty_at = time.time()
        self._published = None

    def after_cycle(self, _items):
        self._dirty_at = time.time()
        return None

    def has_work(self):
        now = time.time()
        rows = store.read("SELECT key, value FROM place_meta")
        meta = {row["key"]: row["value"] for row in rows
                if isinstance(row.get("key"), str) and place.number(row.get("value"))}
        if len(meta) != len(rows):
            log.warning("Place job state is malformed; scheduling cache repair")
        if meta.get("schema") != place.CACHE_VERSION:
            optional.publish_lines("place-facts", {})
            self._published = None
            return True
        if not place.fresh(meta.get("published_at"), now, SNAPSHOT_MAX_AGE_SECONDS):
            optional.publish_lines("place-facts", {})
            self._published = None
        elif self._published != meta.get("revision"):
            try:
                cached = _cache_rows("place_card_lines", ("key", "value"))
                lines = {}
                for row in cached[:optional.SNAPSHOT_MAX]:
                    if not isinstance(row.get("key"), str):
                        log.warning("Place card cache key is malformed")
                        continue
                    offers = _json(row.get("value"), _valid_lines, "card")
                    if offers:
                        lines[row["key"]] = offers
                optional.publish_lines("place-facts", lines)
                self._published = meta.get("revision")
            except OSError as error:
                log.warning("Place snapshot publication failed (%s)", type(error).__name__)
                optional.publish_lines("place-facts", {})
        if meta.get("retry_at", 0) > now:
            return False
        return (meta.get("scanned_at", 0) < self._dirty_at
                or meta.get("next_run_at", 0) <= now)

    def _rebuild(self, deadline=None, *, refresh=False):
        deadline = time.monotonic() + LOCAL_SECONDS + WRITE_RESERVE_SECONDS if deadline is None else deadline
        if time.monotonic() >= deadline:
            return
        place._ensure_cache()
        now = time.time()
        rows = _load_rows()
        if time.monotonic() >= deadline:
            return
        if rows is None:
            _write_batches([("INSERT INTO place_meta VALUES ('retry_at', ?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (now + SNAPSHOT_REFRESH_SECONDS,))], deadline)
            return
        index = Path(place.LOCAL_INDEX_PATH)
        if not index.is_file():
            log.warning("Place index is unavailable; no local snapshot will be published")
            _write_batches([
                ("DELETE FROM place_card_lines", ()),
                ("INSERT INTO place_meta VALUES ('published_at', 0) "
                 "ON CONFLICT(key) DO UPDATE SET value=0", ()),
                ("INSERT INTO place_meta VALUES ('retry_at', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now + SNAPSHOT_REFRESH_SECONDS,)),
            ], deadline)
            return
        index_mtime = index.stat().st_mtime
        active = {row["key"]: row for row in rows}
        raw_facts = {row["key"]: row for row in _cache_rows(
            "place_advert_facts", ("key", "lat", "lng", "index_mtime", "updated_at", "retry_after", "value"))}
        old_areas = {row["area"]: row for row in _cache_rows("place_areas", ("area", "generated_at", "value"))}
        old_lines = {row["key"]: row["value"] for row in _cache_rows("place_card_lines", ("key", "value"))}
        provider_rows = _provider_rows()
        noise_state = {row["key"]: row["value"] for row in _cache_rows("place_noise_state", ("key", "value"))}
        cells = {}
        groups = {}
        for row in rows:
            coords = _coordinates(row)
            cells.setdefault(place._cell(*coords), coords)
            if district := district_for_address(row.get("address")):
                groups.setdefault(district, []).append(row)

        statements = _delete_keys("place_advert_facts", "key", set(raw_facts) - set(active))
        statements.append(("DELETE FROM place_advert_facts WHERE updated_at <= ? OR updated_at > ? "
                           "OR typeof(updated_at) NOT IN ('integer','real')",
                           (now - ADVERT_TTL_SECONDS, now)))
        statements += _delete_keys("place_areas", "area", set(old_areas) - set(groups))
        removed_cells = set(provider_rows) - set(cells)
        for table in ("place_refresh_queue", "place_transit_answers", "place_noise_answers",
                      "place_transit_failures", "place_noise_failures"):
            statements += _delete_keys(table, "cell", removed_cells)
        for table in ("place_noise_answers", "place_transit_answers"):
            statements.append((f"DELETE FROM {table} WHERE fetched_at <= ? OR fetched_at > ? "
                               "OR typeof(fetched_at) NOT IN ('integer','real')",
                               (now - place.CACHE_TTL_SECONDS, now)))
        for table, column in (("place_noise_failures", "retry_after"), ("place_transit_failures", "retry_after"),
                              ("place_request_claims", "expires_at")):
            statements.append((f"DELETE FROM {table} WHERE {column} <= ? "
                               f"OR typeof({column}) NOT IN ('integer','real')", (now,)))
        for cell, coords in cells.items():
            old = provider_rows.get(cell)
            if old is None or _coordinates(old) != coords:
                statements.append((
                    "INSERT INTO place_refresh_queue VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(cell) DO UPDATE SET lat=excluded.lat, lng=excluded.lng",
                    (cell, *coords, now)))
                provider_rows[cell] = {**(old or {}), "cell": cell, "lat": coords[0], "lng": coords[1],
                                       "queued_at": old.get("queued_at", now) if old else now}
        for table in ("place_noise_answers", "place_transit_answers", "place_noise_failures", "place_transit_failures"):
            statements.append((f"DELETE FROM {table} WHERE cell NOT IN (SELECT cell FROM place_refresh_queue)", ()))
        _write_batches(statements, deadline)
        providers, due = {}, {}
        for cell in cells:
            providers[cell], due[cell] = _provider_record(provider_rows[cell], now, noise_state)
        if refresh and due and min(due.values()) <= now and time.monotonic() + WRITE_RESERVE_SECONDS < deadline:
            cell = min(due, key=due.get)
            try:
                place.refresh_network(*cells[cell], deadline=min(
                    deadline - WRITE_RESERVE_SECONDS, time.monotonic() + PROVIDER_SECONDS))
            except place.RefreshDeferred:
                pass
            if time.monotonic() >= deadline:
                raise place.RefreshDeferred("Place provider used the remaining step budget")
            provider_rows = _provider_rows()
            noise_state = {row["key"]: row["value"] for row in _cache_rows("place_noise_state", ("key", "value"))}
            for cell in cells:
                providers[cell], due[cell] = _provider_record(provider_rows[cell], time.time(), noise_state)

        facts, blocked, pending = {}, {}, []
        for key, row in active.items():
            cached = raw_facts.get(key)
            if (cached and _coordinates(cached) == _coordinates(row)
                    and cached.get("index_mtime") == index_mtime
                    and place.fresh(cached.get("updated_at"), now, ADVERT_TTL_SECONDS)):
                if cached.get("value") is None and place.number(cached.get("retry_after")) and cached["retry_after"] > now:
                    blocked[key] = cached["retry_after"]
                    continue
                value = _json(cached.get("value"), _valid_osm, "advert")
                if value is not None:
                    facts[key] = value
                    continue
            pending.append((key, *_coordinates(row)))
        local_deadline = min(deadline - WRITE_RESERVE_SECONDS, time.monotonic() + LOCAL_SECONDS)
        changed = place.local_osm_batch(pending[:LOCAL_BATCH_SIZE], local_deadline) if pending else {}
        statements = []
        for key, value in changed.items():
            if value is not None and not _valid_osm(value):
                log.warning("Place index returned malformed facts; retrying later")
                value = None
            retry_at = now + LOCAL_RETRY_SECONDS if value is None else 0
            statements.append((
                "INSERT INTO place_advert_facts VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET lat=excluded.lat, lng=excluded.lng, index_mtime=excluded.index_mtime, "
                "updated_at=excluded.updated_at, retry_after=excluded.retry_after, value=excluded.value",
                (key, *_coordinates(active[key]), index_mtime, now, retry_at,
                 json.dumps(value, ensure_ascii=False, sort_keys=True) if value is not None else None)))
            if value is not None:
                facts[key] = value
            else:
                blocked[key] = retry_at
        _write_batches(statements, deadline)

        comparison = [statistics.median(values) for group in groups.values()
                      if len(values := _crime_values(group)) >= MIN_SAMPLE]
        areas, statements = {}, []
        for area, group in groups.items():
            if time.monotonic() >= deadline:
                raise place.RefreshDeferred("Place area batch deadline reached")
            record = _area_record(area, group, facts, providers, comparison, now)
            old = _decode_area(old_areas.get(area), now)
            if old and old["inputs"] == record["inputs"]:
                record = old
            else:
                statements.append((
                    "INSERT INTO place_areas VALUES (?, ?, ?) ON CONFLICT(area) DO UPDATE SET "
                    "generated_at=excluded.generated_at, value=excluded.value",
                    (area, now, json.dumps(record, ensure_ascii=False, sort_keys=True))))
            areas[area] = record
        lines = {}
        for row in rows[:optional.SNAPSHOT_MAX]:
            if time.monotonic() >= deadline:
                raise place.RefreshDeferred("Place card batch deadline reached")
            key = row["key"]
            current = {**facts.get(key, {}), **providers.get(place._cell(*_coordinates(row)), {})}
            offers = [(priority, line) for priority, line in (
                (100, place.amenities_line(current)), (50, place.card_line(current)),
                (10, _area_line(areas.get(district_for_address(row.get("address"))))))
                if line and len(line) <= optional.LINE_MAX]
            if offers:
                value = json.dumps(offers, ensure_ascii=False)
                lines[key] = value
                if old_lines.get(key) != value:
                    statements.append(("INSERT INTO place_card_lines VALUES (?, ?) "
                                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)))
        statements += _delete_keys("place_card_lines", "key", set(old_lines) - set(lines))
        pending_keys = set(active) - set(facts) - set(blocked)
        next_run = min([now + SNAPSHOT_REFRESH_SECONDS] + list(due.values()) + list(blocked.values()))
        if pending_keys:
            next_run = now
        for key, value in (("scanned_at", now), ("published_at", now), ("next_run_at", next_run), ("retry_at", 0)):
            statements.append(("INSERT INTO place_meta VALUES (?, ?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)))
        statements.append(("INSERT INTO place_meta VALUES ('revision', 1) "
                           "ON CONFLICT(key) DO UPDATE SET value=value+1", ()))
        _write_batches(statements, deadline)

    def step(self, deadline):
        try:
            self._rebuild(deadline, refresh=True)
        except place.RefreshDeferred:
            log.info("Place cache step yielded at its deadline; progress is retained")
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
            log.warning("Place cache step failed (%s); optional facts are omitted", type(error).__name__)


def area_reply(query):
    """Read-only cached answer; invalid and expired data never becomes an area claim."""
    candidate = _POSTCODE.fullmatch(str(query or "").strip().upper())
    district = _district(candidate["out"]) if candidate else None
    if district is None:
        return "Use /area followed by a London postcode district or full postcode, for example /area N7."
    rows = store.read("SELECT area, generated_at, value FROM place_areas WHERE area = ?", (district,))
    record = _decode_area(rows[0]) if rows else None
    if record is None:
        return f"No cached overview for {district} yet (or it has expired)."
    stamp = datetime.fromtimestamp(record["generated_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (f"<b>Area: {html.escape(district)} {html.escape(record['area_name'])}</b>\n"
            f"Cached {stamp}; non-random advert sample, not current district-wide conditions.\n"
            f"{html.escape(record['text'], quote=False)}")
