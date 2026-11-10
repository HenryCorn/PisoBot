"""Offline station-based commute estimates; deliberately NOT used by the rental pipeline.

Build with ``python -m pisobot.zone build --dest 51.5152,-0.1419 --name oxford-circus``.
All network traffic is to api.tfl.gov.uk, at <= 40 starts/minute (no automatic HTTP
retries). Successful station journeys, including explicit no-route answers, are
checkpointed after every call. Failed calls are retried on the next build. Snapshots
expire after 30 days; a resumed build uses the original travel date until then.

Walking model fixed before validation: 4.8 km/h, straight-line distance * 1.25
for street detours, nearest 5 physical station groups. This is NOT a lower bound:
buses, barriers, timetables and station access can make it wrong in either direction.
Missing nearby journeys, stale data and points outside the build radius fail open
in could_reach. Optional National Rail omissions are reported, not concealed.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import tempfile
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import requests

API = "https://api.tfl.gov.uk"
DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "zone"
MODES = ("tube", "dlr", "overground", "elizabeth-line", "national-rail")
# Approximate outer Greater London rectangle, NOT a borough boundary polygon.
LONDON_BBOX = (51.28, -0.51, 51.70, 0.34)  # south, west, north, east
BUILD_RADIUS_KM = 20.0
WALK_KMH = 4.8
WALK_DETOUR = 1.25
NEAREST_STATIONS = 5
REFRESH_DAYS = 30
SCHEMA = 1


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _point(lat, lon):
    if (type(lat) not in (int, float) or type(lon) not in (int, float)
            or not math.isfinite(lat) or not math.isfinite(lon)
            or not -90 <= lat <= 90 or not -180 <= lon <= 180):
        raise ValueError("Invalid latitude/longitude")
    return float(lat), float(lon)


def distance_km(lat, lon, other_lat, other_lon):
    """Haversine (rather than a projection tied to Oxford Circus)."""
    a, b = math.radians(lat), math.radians(other_lat)
    h = (math.sin((b - a) / 2) ** 2
         + math.cos(a) * math.cos(b) * math.sin(math.radians(other_lon - lon) / 2) ** 2)
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, h)))


def walk_minutes(km):
    return km * WALK_DETOUR / WALK_KMH * 60


def next_weekday(today=None):
    """Same next-weekday, 08:30 departure convention as enrich.journey()."""
    day = (today or dt.datetime.now(ZoneInfo("Europe/London")).date()) + dt.timedelta(days=1)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day.strftime("%Y%m%d")


def _fresh(timestamp, now=None):
    try:
        age = (now or _now()) - dt.datetime.fromisoformat(timestamp)
        return dt.timedelta(0) <= age < dt.timedelta(days=REFRESH_DAYS)
    except (TypeError, ValueError):
        return False


def _read(path):
    try:
        with Path(path).open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None


def _write(path, data):
    """Atomic checkpoints; do not corrupt an earlier snapshot on interruption."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".zone-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class TflError(RuntimeError):
    """Only safe error codes, never a request URL containing an API key."""


class TflClient:
    def __init__(self, *, session=None, rpm=40, clock=time.monotonic, sleep=time.sleep):
        if not 0 < rpm <= 40:
            raise ValueError("Request rate must be > 0 and <= 40/minute")
        self.session = session or requests.Session()
        self.interval = 60 / rpm + 0.01  # no burst, including at a minute boundary
        self.clock, self.sleep = clock, sleep
        self.last_start = None
        self.calls = self.failed = 0

    def get(self, path, **params):
        if not path.startswith(("/StopPoint/Mode/", "/Journey/JourneyResults/")):
            raise ValueError("Only the station and journey endpoints are allowed")
        if self.last_start is not None:
            wait = self.interval - (self.clock() - self.last_start)
            if wait > 0:
                self.sleep(wait)
        self.last_start = self.clock()
        self.calls += 1
        key = os.environ.get("TFL_APP_KEY")
        if key:
            params["app_key"] = key
        try:
            response = self.session.get(API + path, params=params, timeout=40,
                                        allow_redirects=False)
            if response.status_code != 200:
                # 429 is retried only by a later build, not in an unpaced HTTP retry.
                raise TflError(f"http_{response.status_code}")
            data = response.json()
            if not isinstance(data, dict):
                raise TflError("invalid_json_object")
            return data
        except TflError:
            self.failed += 1
            raise
        except (requests.RequestException, ValueError):
            self.failed += 1
            raise TflError("transport_or_json_error") from None


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    lat: float
    lon: float
    modes: tuple

    @classmethod
    def from_dict(cls, data):
        lat, lon = _point(data["lat"], data["lon"])
        return cls(data["id"], data["name"], lat, lon, tuple(data["modes"]))

    def to_dict(self):
        return {"id": self.id, "name": self.name, "lat": self.lat, "lon": self.lon,
                "modes": list(self.modes)}


def normalise_stations(payloads):
    """Group entrances/platforms by stationNaptan, merging modes across endpoints.

    Prefer the station node's own position/name; if absent use the mean of unique
    entrance coordinates (not an arbitrary platform). Do not merge nearby stations
    by distance/name: separate rail/Tube NaPTAN groups can have different access.
    Only national-rail points are clipped to the Greater London rectangle.
    """
    groups = {}
    for mode, payload in payloads.items():
        points = payload.get("stopPoints")
        if not isinstance(points, list) or not points:
            raise ValueError(f"No stopPoints for {mode}")
        total = payload.get("total", len(points))
        if total > len(points):
            raise ValueError(f"Truncated stopPoints for {mode}; refusing an incomplete table")
        for p in points:
            if not isinstance(p, dict):
                continue
            try:
                lat, lon = _point(p.get("lat"), p.get("lon"))
            except ValueError:
                continue
            south, west, north, east = LONDON_BBOX
            if mode == "national-rail" and not (south <= lat <= north and west <= lon <= east):
                continue
            identifier = p.get("stationNaptan") or p.get("naptanId") or p.get("id")
            if not identifier or not p.get("commonName"):
                continue
            group = groups.setdefault(identifier, {"nodes": {}, "modes": set()})
            group["nodes"][p.get("id") or p.get("naptanId") or str((lat, lon))] = p
            group["modes"].update(set(p.get("modes") or []) & set(MODES))
            group["modes"].add(mode)
    out = []
    for identifier, group in sorted(groups.items()):
        nodes = group["nodes"]
        parent = nodes.get(identifier)
        if parent:
            lat, lon, name = parent["lat"], parent["lon"], parent["commonName"]
        else:
            coords = {(p["lat"], p["lon"]) for p in nodes.values()}
            lat = statistics.mean(c[0] for c in coords)
            lon = statistics.mean(c[1] for c in coords)
            name = min(p["commonName"] for p in nodes.values())
        out.append(Station(identifier, name, lat, lon, tuple(sorted(group["modes"]))))
    return out


def station_table(client, data_dir=DATA_DIR, refresh=False):
    """One call per mode; each response cached separately to resume partial discovery.

    National Rail is optional if its endpoint is unusable, but omitted coverage and
    errors are explicit in stations.json. The other four modes are required.
    """
    directory = Path(data_dir)
    payloads, errors, mode_stats = {}, {}, {}
    for mode in MODES:
        path = directory / "stops" / f"{mode}.json"
        hit = _read(path)
        if (not refresh and hit and hit.get("error") and _fresh(hit.get("fetched_at"))):
            errors[mode] = hit["error"]
            continue
        if refresh or not hit or not _fresh(hit.get("fetched_at")):
            try:
                payload = client.get(f"/StopPoint/Mode/{mode}")
                normalise_stations({mode: payload})  # check before checkpointing
                hit = {"fetched_at": _now().isoformat(), "payload": payload}
                _write(path, hit)
            except (TflError, ValueError) as error:
                errors[mode] = str(error)
                if mode != "national-rail":
                    raise TflError(f"Required station mode unavailable: {mode}") from None
                _write(path, {"fetched_at": _now().isoformat(), "error": str(error)})
                continue
        payloads[mode] = hit["payload"]
        mode_stats[mode] = {"points": len(hit["payload"]["stopPoints"]),
                            "fetched_at": hit["fetched_at"],
                            "groups": len(normalise_stations({mode: hit["payload"]}))}
    stations = normalise_stations(payloads)
    table = {"schema": SCHEMA, "source": API + "/StopPoint/Mode/<mode>",
             "national_rail_bbox": list(LONDON_BBOX), "modes": mode_stats,
             "errors": errors, "stations": [s.to_dict() for s in stations]}
    _write(directory / "stations.json", table)
    return table


def _slug(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug or slug == "stations":
        raise ValueError("Choose a destination name other than 'stations'")
    return slug


def build(lat, lon, name, *, data_dir=DATA_DIR, client=None, refresh=False,
          radius_km=BUILD_RADIUS_KM, progress=print):
    """Resume the destination's monthly snapshot, checkpointing each station.

    Coordinate origins and Journey parameters deliberately match enrich.journey(),
    without a mode restriction: the station-to-destination leg may use buses too.
    """
    lat, lon = _point(lat, lon)
    if not math.isfinite(radius_km) or radius_km <= 0:
        raise ValueError("Build radius must be positive")
    client = client or TflClient()
    initial_calls, initial_failed = client.calls, client.failed
    table = station_table(client, data_dir, refresh)
    stations = [s for row in table["stations"] if distance_km(
        (s := Station.from_dict(row)).lat, s.lon, lat, lon) <= radius_km]
    path = Path(data_dir) / f"{_slug(name)}.json"
    data = _read(path)
    dest = {"name": name, "lat": lat, "lon": lon}
    if data and (data.get("destination") != dest or data.get("radius_km") != radius_km):
        raise ValueError("Destination name already belongs to different coordinates/radius")
    if refresh or not data or not _fresh(data.get("started_at")):
        data = {"schema": SCHEMA, "destination": dest, "radius_km": radius_km,
                "started_at": _now().isoformat(), "travel_date": next_weekday(),
                "time": "0830", "time_is": "Departing", "results": {}, "runs": []}
    data["stations"] = [s.to_dict() for s in stations]
    data["station_table_errors"] = table["errors"]
    fingerprint = hashlib.sha256(json.dumps(data["stations"], sort_keys=True).encode()).hexdigest()
    data["station_fingerprint"] = fingerprint
    run = {"started_at": _now().isoformat(), "calls": 0, "failed": 0}
    data["runs"].append(run)
    start_calls, start_failed = client.calls, client.failed
    # Include discovery calls made during this invocation, not only journeys.
    run["discovery_calls"] = start_calls - initial_calls
    run["discovery_failed"] = start_failed - initial_failed
    _write(path, data)
    try:
        for index, station in enumerate(stations, 1):
            hit = data["results"].get(station.id)
            origin = [round(station.lat, 5), round(station.lon, 5)]
            if hit and hit.get("status") in ("ok", "no_route") and hit.get("origin") == origin:
                continue
            result = {"origin": origin, "checked_at": _now().isoformat()}
            try:
                reply = client.get(
                    f"/Journey/JourneyResults/{station.lat:.5f},{station.lon:.5f}/to/{lat},{lon}",
                    date=data["travel_date"], time="0830", timeIs="Departing")
                journeys = reply.get("journeys")
                if not isinstance(journeys, list):
                    raise TflError("missing_journeys")
                if journeys:
                    durations = [j.get("duration") if isinstance(j, dict) else None for j in journeys]
                    if any(type(n) not in (int, float) or not math.isfinite(n) or n < 0 for n in durations):
                        raise TflError("invalid_duration")
                    result.update(status="ok", minutes=min(durations))
                else:
                    result.update(status="no_route", minutes=None)
            except TflError as error:
                result.update(status="error", minutes=None, error=str(error))
                run["failed"] += 1
            data["results"][station.id] = result
            run["calls"] = client.calls - start_calls
            data["updated_at"] = _now().isoformat()
            _write(path, data)
            progress(f"{name}: {index}/{len(stations)} {station.name}: "
                     f"{result.get('minutes') if result['status'] == 'ok' else result['status']}")
    finally:
        run["calls"] = client.calls - start_calls
        run["finished_at"] = _now().isoformat()
        _write(path, data)
    pending = sum(data["results"].get(s.id, {}).get("status") not in ("ok", "no_route") for s in stations)
    progress(f"{name}: {len(stations)} stations; this run {run['calls']} journey calls, "
             f"{run['failed']} failed; {run['discovery_calls']} discovery calls, "
             f"{run['discovery_failed']} failed; "
             f"{pending} pending")
    return ZoneData(data)


@dataclass(frozen=True)
class Estimate:
    minutes: float
    station: Station
    walk_minutes: float


@dataclass(frozen=True)
class PortalZone:
    stations: frozenset
    centre: tuple | None
    radius_km: float | None
    complete: bool
    missing_modes: tuple = ()


class ZoneData:
    def __init__(self, data):
        if data.get("schema") != SCHEMA:
            raise ValueError("Unsupported zone schema")
        self.data = data
        self.stations = tuple(Station.from_dict(row) for row in data["stations"])
        self.lat, self.lon = _point(data["destination"]["lat"], data["destination"]["lon"])

    @property
    def stale(self):
        return not _fresh(self.data.get("started_at"))

    @property
    def complete(self):
        # Optional National Rail coverage is reported separately, not silently
        # treated as a failed station journey. Complete refers to discovered groups.
        return (not self.stale and bool(self.stations)
                and all(self.data["results"].get(s.id, {}).get("status") in ("ok", "no_route")
                        for s in self.stations))


def load(destination, data_dir=DATA_DIR):
    """Pass an already loaded ZoneData in hot loops, or a destination name / JSON path."""
    if isinstance(destination, ZoneData):
        return destination
    path = Path(destination)
    if path.suffix != ".json":
        path = Path(data_dir) / f"{_slug(str(destination))}.json"
    data = _read(path)
    if data is None:
        raise FileNotFoundError(f"No zone data for {destination}; run zone build first")
    return ZoneData(data)


def estimate(lat, lng, destination):
    """Minimum walk + station time over the nearest five station groups, else None.

    A missing journey for one of the nearest groups makes the answer unknown, not
    falsely slow. Explicit TfL no-route results are skipped. No network or cache
    writes occur here. The station is the candidate access station, not a guarantee
    of the exact route's first vehicle (station journeys can themselves start walking).
    """
    lat, lng = _point(lat, lng)
    zone = load(destination)
    nearest = sorted(((distance_km(lat, lng, s.lat, s.lon), s) for s in zone.stations),
                     key=lambda pair: (pair[0], pair[1].id))[:NEAREST_STATIONS]
    candidates = []
    for km, station in nearest:
        result = zone.data["results"].get(station.id, {})
        if result.get("status") not in ("ok", "no_route"):
            return None
        if result["status"] == "ok":
            walk = walk_minutes(km)
            candidates.append(Estimate(walk + result["minutes"], station, walk))
    return min(candidates, key=lambda e: (e.minutes, e.station.id)) if candidates else None


def could_reach(lat, lng, destination, limit=40, margin=5):
    """Recall-biased prefilter, NOT a final gate. Nearby unknown/stale/outside => True.

    Optional mode omissions (e.g. unavailable National Rail) are a documented model
    limitation, so inspect station_table_errors before integrating as a hard gate.
    """
    lat, lng = _point(lat, lng)
    _limits(limit, margin)
    try:
        zone = load(destination)
    except FileNotFoundError:
        return True
    if (zone.stale or not zone.stations
            or distance_km(lat, lng, zone.lat, zone.lon) > zone.data["radius_km"]):
        return True
    result = estimate(lat, lng, zone)
    return result is None or result.minutes <= limit + margin


def _limits(limit, margin):
    if not all(math.isfinite(n) and n >= 0 for n in (limit, margin)):
        raise ValueError("Limit and margin must be finite and non-negative")


def portal_zone(destination, limit=40, margin=5):
    """Station set and a conservative enclosing search circle for the walking model.

    Each included station gets an access disk for its remaining time allowance.
    Triangle inequality encloses those disks; this is NOT a polygon of real streets
    or a guarantee of true reachability. If complete=False do not use the circle as
    a hard search boundary. Stations beyond the build radius have not been tested.
    """
    _limits(limit, margin)
    zone = load(destination)
    included = []
    for station in zone.stations:
        result = zone.data["results"].get(station.id, {})
        if result.get("status") == "ok" and result["minutes"] <= limit + margin:
            included.append((station, (limit + margin - result["minutes"]) * WALK_KMH / 60 / WALK_DETOUR))
    missing_modes = tuple(sorted(zone.data.get("station_table_errors", {})))
    complete = zone.complete and not missing_modes
    if not included:
        return PortalZone(frozenset(), None, None, complete, missing_modes)
    centre = ((min(s.lat for s, _ in included) + max(s.lat for s, _ in included)) / 2,
              (min(s.lon for s, _ in included) + max(s.lon for s, _ in included)) / 2)
    radius = max(distance_km(*centre, s.lat, s.lon) + access for s, access in included)
    return PortalZone(frozenset(s for s, _ in included), centre, radius, complete, missing_modes)


def exact_points(cache_data, lat=51.5152, lon=-0.1419):
    """Assume legacy unqualified keys mean Oxford Circus for the requested comparison.
    That destination is NOT verifiable from such keys. Prefer qualified results when
    both exist: report sample counts without double-weighting the same location.
    TfL empty/no-route cache entries are unknown, not evidence of unreachability.
    """
    points, qualified, skipped = {}, {}, 0
    for key, value in cache_data.get("journey", {}).items():
        try:
            origin, sep, dest = key.partition("->")
            target = tuple(map(float, dest.split(","))) if sep else (51.5152, -0.1419)
            if target != (lat, lon):
                skipped += 1
                continue
            point = _point(*map(float, origin.split(",")))
            minutes = value.get("minutes")
            if type(minutes) not in (int, float) or not math.isfinite(minutes) or minutes < 0:
                skipped += 1
                continue
            (qualified if sep else points)[point] = float(minutes)
        except (TypeError, ValueError, AttributeError):
            skipped += 1
    duplicates = len(points.keys() & qualified.keys())
    points.update(qualified)
    return points, duplicates, skipped


def _validation_metrics(points, zone, output):
    comparisons = [(point, exact, estimate(*point, zone)) for point, exact in sorted(points.items())]
    errors = sorted(abs(result.minutes - exact) for _, exact, result in comparisons if result is not None)
    reachable = sum(exact <= 40 for _, exact, _ in comparisons)
    unreachable = len(comparisons) - reachable
    output(f"Compared: {len(errors)} estimates; {len(points) - len(errors)} unknown; "
           f"{reachable} truly reachable (<=40); {unreachable} unreachable (>40)")
    if errors:
        output(f"Mean absolute error: {statistics.mean(errors):.2f} min")
        output(f"90th-percentile absolute error (nearest rank): {errors[math.ceil(0.9 * len(errors)) - 1]:.2f} min")
    else:
        output("Mean / 90th-percentile absolute error: N/A (no estimates)")
    targets = []
    for margin in (0, 5, 10):
        false_drop = true_drop = 0
        for point, exact, _ in comparisons:
            discarded = not could_reach(*point, zone, 40, margin)
            false_drop += discarded and exact <= 40
            true_drop += discarded and exact > 40
        false_share = false_drop / reachable if reachable else None
        true_share = true_drop / unreachable if unreachable else None
        f = f"{100 * false_share:.2f}%" if false_share is not None else "N/A"
        t = f"{100 * true_share:.2f}%" if true_share is not None else "N/A"
        output(f"Limit 40, margin {margin:2d}: reachable wrongly discarded {false_drop}/{reachable} ({f}); "
               f"unreachable correctly discarded {true_drop}/{unreachable} ({t})")
        if false_share is not None and true_share is not None and false_share < 0.02 and true_share > 0.5:
            targets.append(margin)
    output("Target (<2% false discards and >50% true discards): "
           + (f"MET at margin(s) {', '.join(map(str, targets))}" if targets else "NOT MET"))
    return comparisons


def validate(destination="oxford-circus", cache_path=None, *, output=print):
    """Offline validation, no live door-to-door calls and never writes cache.json."""
    zone = load(destination)
    if cache_path is None:
        # Default works from this dedicated worktree, then from a normal checkout.
        sibling = Path(__file__).resolve().parents[2] / "PisoBot" / "state" / "cache.json"
        cache_path = sibling if sibling.exists() else Path(__file__).resolve().parents[1] / "state" / "cache.json"
    cache_data = _read(cache_path)
    if cache_data is None:
        raise FileNotFoundError("Exact journey cache not found; supply --cache")
    points, duplicates, skipped = exact_points(cache_data, zone.lat, zone.lon)
    output(f"Destination: {zone.data['destination']['name']} ({zone.lat},{zone.lon})")
    output(f"Exact cache: {len(cache_data.get('journey', {}))} entries; {len(points)} unique points; "
           f"{duplicates} duplicate legacy points superseded; {skipped} skipped")
    output(f"Station snapshot: {len(zone.stations)} stations; complete={zone.complete}; stale={zone.stale}; "
           f"travel date={zone.data['travel_date']} 08:30 Departing")
    output(f"Station mode omissions: {json.dumps(zone.data.get('station_table_errors', {}), sort_keys=True)}")
    output(f"Walking model: {WALK_KMH:g} km/h; detour {WALK_DETOUR:g}; nearest {NEAREST_STATIONS} groups")
    output("Requested comparison (legacy-only origins assumed to target Oxford Circus):")
    comparisons = _validation_metrics(points, zone, output)
    legacy = {key: value for key, value in cache_data.get("journey", {}).items() if "->" not in key}
    if legacy:
        windrush = sum(isinstance(value, dict) and
                       str(value.get("route") or "").split(" → ")[-1] == "Overground (Windrush)"
                       for value in legacy.values())
        output(f"Ground-truth warning: {len(legacy)} legacy entries have no destination; "
               f"{windrush} end their recorded route on Overground (Windrush). "
               "Their destination cannot be verified from these summaries.")
        explicit = {"journey": {key: value for key, value in cache_data["journey"].items() if "->" in key}}
        explicit_points, _, _ = exact_points(explicit, zone.lat, zone.lon)
        output("Explicit-destination-only comparison (no model/constants changed):")
        _validation_metrics(explicit_points, zone, output)
    output("Limitations: station-only access misses bus-first routes and street barriers; weekday/snapshot "
           "and departure times differ from the historical cache; rounded pins and separate NaPTAN groups "
           "affect nearest-station selection. No constants fitted to this sample.")
    return comparisons


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    builder = sub.add_parser("build", help="Paced, resumable TfL station journeys")
    builder.add_argument("--dest", required=True, help="LAT,LNG")
    builder.add_argument("--name", required=True)
    builder.add_argument("--data-dir", type=Path, default=DATA_DIR)
    builder.add_argument("--refresh", action="store_true", help="Start a fresh monthly snapshot")
    checker = sub.add_parser("validate", help="Compare against the read-only exact journey cache")
    checker.add_argument("--destination", default="oxford-circus", help="Name or zone JSON path")
    checker.add_argument("--cache", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            lat, lon = _point(*map(float, args.dest.split(",")))
            build(lat, lon, args.name, data_dir=args.data_dir, refresh=args.refresh)
        else:
            validate(args.destination, args.cache)
    except (ValueError, TflError, FileNotFoundError) as error:
        # Errors above contain only sanitised messages, never exception URLs.
        parser.exit(1, f"zone: {error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "zone: interrupted; rerun build to resume\n")


if __name__ == "__main__":
    main()
