import datetime as dt
import math
import os
from bisect import bisect_left, bisect_right
from collections import Counter

from . import cache, config, net

SAFETY_CATEGORIES = {
    "violent-crime", "robbery", "burglary", "theft-from-the-person", "possession-of-weapons",
    "criminal-damage-arson", "drugs", "public-order",
}


def distance_km(lat, lng):
    return math.hypot((lat - config.TARGET_LAT) * 111.2,
                      (lng - config.TARGET_LNG) * 111.2 * math.cos(math.radians(lat)))


def _next_weekday():
    d = dt.date.today() + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d.strftime("%Y%m%d")


STATION_SUFFIXES = (" Underground Station", " Rail Station", " DLR Station", " Station")


def _leg_label(leg):
    mode = leg["mode"]["id"]
    name = ((leg.get("routeOptions") or [{}])[0].get("name") or "").strip()
    if mode == "tube":
        return f"{name} line" if name else "Tube"
    if mode == "overground":
        return f"Overground ({name})" if name else "Overground"
    if mode == "elizabeth-line":
        return "Elizabeth line"
    if mode == "dlr":
        return "DLR"
    if mode == "bus":
        return f"bus {name}".strip()
    if mode == "national-rail":
        return "National Rail"
    return name or mode


def _station(leg):
    s = leg.get("departurePoint", {}).get("commonName", "")
    for suffix in STATION_SUFFIXES:
        s = s.replace(suffix, "")
    return s


def _choose_journey(options):
    """Fastest returned option meeting personal walk/change limits, if one exists."""
    if not options:
        return None
    walk, changes = getattr(config, "MAX_WALK_MINUTES", None), getattr(config, "MAX_CHANGES", None)
    feasible = [j for j in options if (walk is None or j["walk"] <= walk)
                and (changes is None or j["changes"] <= changes)]
    # Return the fastest otherwise so the caller can report the actual failed limit.
    return min(feasible or options, key=lambda j: j["minutes"])


def journey(lat, lng):
    """Fastest returned weekday 08:30 option satisfying personal walk/change limits.
    Cache all returned options by origin/destination; never share a person's selected route."""
    key = f"{lat:.3f},{lng:.3f}->{config.TARGET_LAT},{config.TARGET_LNG}"
    limited = (getattr(config, "MAX_WALK_MINUTES", None) is not None
               or getattr(config, "MAX_CHANGES", None) is not None)
    options = cache.get("journey_options_v1", key)
    if limited and options is not None:
        return _choose_journey(options)
    hit = cache.get("journey", key)
    if not limited and hit is not None:
        return hit or None
    params = {"date": _next_weekday(), "time": "0830", "timeIs": "Departing"}
    if os.environ.get("TFL_APP_KEY"):
        params["app_key"] = os.environ["TFL_APP_KEY"]
    r = net.get(f"https://api.tfl.gov.uk/Journey/JourneyResults/{lat:.5f},{lng:.5f}/to/"
                f"{config.TARGET_LAT},{config.TARGET_LNG}", params=params)
    journeys = r.json().get("journeys") or []
    options = []
    for j in journeys:
        legs = j.get("legs") or []
        ride = [l for l in legs if l["mode"]["id"] not in ("walking", "cycle")]
        first_walk = 0
        for leg in legs:
            if leg["mode"]["id"] != "walking":
                break
            first_walk += leg["duration"]
        options.append({
            "minutes": j["duration"],
            "route": " → ".join(_leg_label(l) for l in ride) or "walk",
            "station": _station(ride[0]) if ride else None,
            "walk": first_walk,
            "changes": max(len(ride) - 1, 0),
        })
    cache.put("journey_options_v1", key, options)
    # Legacy/default consumers must always see the unconstrained fastest option.
    cache.put("journey", key, min(options, key=lambda j: j["minutes"]) if options else {})
    return _choose_journey(options)


_latest_month = None


def _crime_month():
    global _latest_month
    if _latest_month is None:
        _latest_month = net.get("https://data.police.uk/api/crimes-street-dates").json()[0]["date"]
    return _latest_month


def crime_score(lat, lng):
    """Safety-relevant street crimes in the latest published month within CRIME_RADIUS_M."""
    month = _crime_month()
    key = f"{lat:.3f},{lng:.3f}:{month}:{config.CRIME_RADIUS_M}"
    hit = cache.get("crime", key)
    if hit is not None:
        return hit
    r_m = config.CRIME_RADIUS_M
    poly = ":".join(
        f"{lat + r_m * math.cos(2 * math.pi * k / 8) / 111320:.6f},"
        f"{lng + r_m * math.sin(2 * math.pi * k / 8) / (111320 * math.cos(math.radians(lat))):.6f}"
        for k in range(8))
    crimes = net.get("https://data.police.uk/api/crimes-street/all-crime",
                     params={"poly": poly, "date": month}).json()
    counts = Counter(c["category"] for c in crimes)
    score = sum(v for k, v in counts.items() if k in SAFETY_CATEGORIES)
    cache.put("crime", key, score)
    return score


def assign_crime_percentiles(listings):
    """Mid-rank against distinct cached London search locations, not only accepted homes."""
    if not listings:
        return
    for l in listings:
        l.crime_percentile = None
    suffix = f":{_crime_month()}:{config.CRIME_RADIUS_M}"
    reference = sorted(value for key, value in cache.items("crime") if key.endswith(suffix))
    if len(reference) < 20:
        print(f"Crime bands unavailable: only {len(reference)} London search locations for this month")
        return
    for l in listings:
        if l.crime_score is not None:
            l.crime_percentile = 100 * (bisect_left(reference, l.crime_score) +
                                        bisect_right(reference, l.crime_score)) / (2 * len(reference))
    print(f"Crime bands: {_crime_month()}, {len(reference)} distinct London search locations (sample tertiles)")


def geocode(listings):
    """Fill missing coordinates from full postcodes via postcodes.io (cached)."""
    todo = sorted({l.postcode_full for l in listings
                   if l.lat is None and l.postcode_full and cache.get("postcode", l.postcode_full) is None})
    for i in range(0, len(todo), 100):
        r = net.post("https://api.postcodes.io/postcodes", json={"postcodes": todo[i:i + 100]}).json()
        for item in r.get("result", []):
            res = item.get("result")
            cache.put("postcode", item["query"], [res["latitude"], res["longitude"]] if res else [])
    for l in listings:
        if l.lat is None and l.postcode_full:
            ll = cache.get("postcode", l.postcode_full) or []
            if ll:
                l.lat, l.lng = ll
