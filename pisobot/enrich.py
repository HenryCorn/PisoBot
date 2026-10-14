import datetime as dt
import math
import os
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


def travel_minutes(lat, lng):
    """Fastest weekday 08:30 public-transport journey to the target, cached per ~100 m."""
    key = f"{lat:.3f},{lng:.3f}"
    hit = cache.get("travel", key)
    if hit is not None:
        return hit
    params = {"date": _next_weekday(), "time": "0830", "timeIs": "Departing"}
    if os.environ.get("TFL_APP_KEY"):
        params["app_key"] = os.environ["TFL_APP_KEY"]
    r = net.get(f"https://api.tfl.gov.uk/Journey/JourneyResults/{lat:.5f},{lng:.5f}/to/"
                f"{config.TARGET_LAT},{config.TARGET_LNG}", params=params)
    journeys = r.json().get("journeys") or []
    if not journeys:
        return None
    minutes = min(j["duration"] for j in journeys)
    cache.put("travel", key, minutes)
    return minutes


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
