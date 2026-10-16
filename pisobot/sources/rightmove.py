"""rightmove.co.uk lettings - Next.js page; results live in __NEXT_DATA__ under
props.pageProps.searchResults (24 per page, paged with ?index=). The search page already has
rent, beds, coordinates, type and move-in date; details pages (window.__PAGE_MODEL) are read
only for listings that pass the cheap filters, to catch rooms, short lets and real deposits."""
import datetime as dt
import json
import re
import sys

from requests import RequestException

from .. import cache, config, net
from ..models import Listing
from . import PRIVATE_RENT, apply_rent_details, cached_search, monthly_rent, private_rent_costs, text

BASE = "https://www.rightmove.co.uk"
LOCATION = "STATION^6953"          # Oxford Circus Station
PAGE_SIZE = 24                     # plus one promoted slot that may repeat or be out of area
SHARED = {"flat share", "house share", "room"}


def _listing(x):
    if x.get("students") or x.get("auction") or str(x.get("propertySubType", "")).lower() in SHARED:
        return None
    price = x.get("price") or {}
    rent = monthly_rent(price.get("amount"), price.get("frequency"))
    loc = x.get("location") or {}
    if not rent or rent < config.RENT_MIN or not x.get("id"):
        return None
    media = (x.get("customer") or {}).get("mediaServerUrl") or "https://media.rightmove.co.uk"
    images = [f"{media}/{img['url']}" if img.get("url") else img.get("srcUrl")
              for img in x.get("images") or [] if img.get("url") or img.get("srcUrl")]
    l = Listing(
        source="rm", id=str(x["id"]), url=f"{BASE}/properties/{x['id']}",
        address=x.get("displayAddress", ""), bedrooms=int(x.get("bedrooms") or 0),
        scheme=PRIVATE_RENT, tenure="rent", rent=rent,
        lat=loc.get("latitude"), lng=loc.get("longitude"),
        image=images[0] if images else None, images=images,
        description=text(f"{x.get('propertySubType', '')}. {x.get('summary', '')}"),
        available=(x.get("letAvailableDate") or "")[:10] or None,
    )
    return private_rent_costs(l)


def search():
    out, seen = [], set()
    for page in range(config.RENT_MAX_PAGES):
        html = net.page(f"{BASE}/property-to-rent/find.html", params={
            "locationIdentifier": LOCATION, "radius": float(config.RENT_SEARCH_MILES),
            "minBedrooms": config.MIN_BEDROOMS, "maxPrice": int(config.MAX_MONTHLY_COST),
            "dontShow": "houseShare,student,retirement",   # 319 -> 114 results: fewer proxied pages
            "sortType": 6, "index": page * PAGE_SIZE})
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            raise ValueError("Rightmove page has no __NEXT_DATA__ (blocked or layout changed)")
        results = json.loads(m.group(1))["props"]["pageProps"]["searchResults"]
        for x in results.get("properties") or []:
            if x.get("id") in seen:
                continue
            seen.add(x.get("id"))
            l = _listing(x)
            if l:
                out.append(l)
        if (page + 1) * PAGE_SIZE >= int(results.get("resultCount", "0").replace(",", "") or 0):
            break
    return out


def fetch():
    return cached_search("rm", search)


def _property_data(html):
    """window.__PAGE_MODEL holds {"data": "<flattened JSON>"}: a list whose objects and arrays
    reference other entries by index."""
    i = html.find("window.__PAGE_MODEL")
    if i < 0:
        return {}
    model, _ = json.JSONDecoder().raw_decode(html, html.find("{", i))
    if not isinstance(model.get("data"), str):
        return model.get("propertyData") or {}
    flat = json.loads(model["data"])

    def node(index, depth=0):
        if not isinstance(index, int) or not 0 <= index < len(flat) or depth > 40:
            return None
        value = flat[index]
        if isinstance(value, dict):
            return {k: node(v, depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            return [node(v, depth + 1) for v in value]
        return value
    return (node(0) or {}).get("propertyData") or {}


def _details(p):
    lettings = p.get("lettings") or {}
    sqm = next((s.get("minimumSize") for s in p.get("sizings") or [] if s.get("unit") == "sqm"), None)
    try:
        available = dt.datetime.strptime(lettings.get("letAvailableDate") or "", "%d/%m/%Y").date().isoformat()
    except ValueError:
        available = None
    features = " ".join(f for f in p.get("keyFeatures") or [] if isinstance(f, str))
    plans = [f.get("url") for f in p.get("floorplans") or [] if isinstance(f, dict) and f.get("url")]
    return {"description": text(f"{lettings.get('letType') or ''} let. {lettings.get('furnishType') or ''}. "
                                f"{features} {(p.get('text') or {}).get('description', '')}"),
            "floor_area_m2": sqm or None, "floorplan": plans[0] if plans else None,
            "deposit": lettings.get("deposit") or None, "available": available}


def refine(listing):
    d = cache.get("rm_detail", listing.id)
    if d is None:
        try:
            p = _property_data(net.page(listing.url))
        except (RequestException, ValueError) as ex:
            print(f"Rightmove detail unavailable for {listing.id}: {type(ex).__name__}", file=sys.stderr)
            return False
        if str(p.get("id")) != listing.id:
            print(f"Rightmove detail has no matching property ID: {listing.id}", file=sys.stderr)
            return False
        d = _details(p)
        cache.put("rm_detail", listing.id, d)
    return apply_rent_details(listing, d)
