"""zoopla.co.uk lettings - Next.js app router: results are in the React Server Components stream
(self.__next_f.push chunks) under "regularListingsFormatted", 25 per page (?pn=). Zoopla blocks
plain requests outright, so it only works through Decodo. Search cards often lack size and cut
the summary short, so details pages are read (and cached) for listings that pass the cheap filters."""
import datetime as dt
import json
import re
import sys

from requests import RequestException

from .. import cache, config, net
from ..models import Listing
from . import (PRIVATE_RENT, apply_rent_details, cached_search, detail_fields, private_rent_costs,
               rent_from_text, text)

BASE = "https://www.zoopla.co.uk"
IMAGE = "https://lid.zoocdn.com/u/1024/768/{}"


def _stream(html):
    return "".join(json.loads(f'"{chunk}"') for chunk in
                   re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html))


def _results(html):
    """(listings, last page number) from a search page."""
    stream = _stream(html)
    m = re.search(r'"regularListingsFormatted":\s*', stream)
    if not m:
        raise ValueError("Zoopla page has no listings data (blocked or layout changed)")
    rows, _ = json.JSONDecoder().raw_decode(stream, m.end())
    pages = re.search(r'"pagination":\{[^}]*"pageNumberMax":(\d+)', stream)
    return rows, int(pages[1]) if pages else 1


def _available(value):
    try:
        return dt.datetime.strptime(re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", value or ""), "%d %b %Y").date().isoformat()
    except ValueError:
        return None


def _listing(x):
    rent = rent_from_text(x.get("price"))
    if not x.get("listingId") or not rent or rent < config.RENT_MIN or x.get("underOffer"):
        return None
    beds = next((f.get("content") for f in x.get("features") or [] if f.get("iconId") == "bed"), 0)
    pos = x.get("pos") or {}
    images = [IMAGE.format(name) for name in x.get("gallery") or []]
    tags = " ".join(t.get("content", "") for t in x.get("tags") or [] if isinstance(t, dict))
    l = Listing(
        source="zp", id=str(x["listingId"]),
        url=BASE + ((x.get("listingUris") or {}).get("detail") or f"/to-rent/details/{x['listingId']}/"),
        address=x.get("address", ""), bedrooms=int(beds or 0), scheme=PRIVATE_RENT, tenure="rent", rent=rent,
        lat=pos.get("lat"), lng=pos.get("lng"), image=images[0] if images else None, images=images,
        floor_area_m2=round(x["sizeSqft"] * 0.09290304, 2) if x.get("sizeSqft") else None,
        description=text(f"{x.get('title', '')}. {tags}. {x.get('summaryDescription', '')}"),
        available=_available(x.get("availableFrom")),
    )
    return private_rent_costs(l)


def search():
    out, seen, last = [], set(), 1
    for page in range(1, config.RENT_MAX_PAGES + 1):
        params = {"q": f"{config.TARGET_NAME}, London", "radius": int(config.RENT_SEARCH_MILES),
                  "beds_min": config.MIN_BEDROOMS, "price_max": int(config.MAX_MONTHLY_COST),
                  "price_frequency": "per_month", "is_shared_accommodation": "false",
                  "is_student_accommodation": "false", "is_retirement_home": "false", "search_source": "to-rent"}
        if page > 1:
            params["pn"] = page
        # the canonical path: /to-rent/flats/london/oxford-circus/ redirects here and drops the filters
        rows, last = _results(net.page(f"{BASE}/to-rent/flats/oxford-circus/", params=params))
        for x in rows:
            if x.get("listingId") in seen:
                continue
            seen.add(x.get("listingId"))
            l = _listing(x)
            if l:
                out.append(l)
        if page >= last:
            break
    return out


def fetch():
    return cached_search("zp", search)


def _value(stream, pattern):
    """First JSON value following a key pattern in the stream, or None."""
    m = re.search(pattern, stream)
    return json.JSONDecoder().raw_decode(stream, m.end())[0] if m else None


def _details(stream):
    bullets = (_value(stream, r'"features":\s*(?=\{"bullets")') or {}).get("bullets") or []
    plans = (_value(stream, r'"floorPlan":\s*') or {}).get("image") or []
    description = text(f"{' '.join(b for b in bullets if isinstance(b, str))}. "
                       f"{_value(stream, r'\"detailedDescription\":\s*') or ''}")
    return {"description": description, "floor_area_m2": detail_fields(description).get("floor_area_m2"),
            "floorplan": IMAGE.format(plans[0]["filename"]) if plans and plans[0].get("filename") else None}


def refine(listing):
    d = cache.get("zp_detail", listing.id)
    if d is None:
        try:
            stream = _stream(net.page(listing.url))
        except (RequestException, ValueError) as ex:
            print(f"Zoopla detail unavailable for {listing.id}: {type(ex).__name__}", file=sys.stderr)
            return False
        if f'"listingId":{listing.id}' not in stream:
            print(f"Zoopla detail has no matching listing ID: {listing.id}", file=sys.stderr)
            return False
        d = _details(stream)
        cache.put("zp_detail", listing.id, d)
    return apply_rent_details(listing, d)
