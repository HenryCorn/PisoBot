"""rightmove.co.uk lettings - Next.js page; results live in __NEXT_DATA__ under
props.pageProps.searchResults (24 per page, paged with ?index=). The search page already has
rent, beds, coordinates, type and move-in date; details pages (window.__PAGE_MODEL) are read
only for listings that pass the cheap filters, to catch rooms, short lets and real deposits."""
import datetime as dt
import json
import re
import sys

from requests import RequestException

from .. import cache, config, net, rental_text
from ..models import Listing
from . import (PRIVATE_RENT, apply_rent_details, cached_search, iso_date, min_bedrooms, monthly_rent,
               phone_number, private_rent_costs, studios_wanted, text, update_kind)

BASE = "https://www.rightmove.co.uk"
LOCATION = "STATION^6953"          # Oxford Circus Station
PAGE_SIZE = 24                     # plus one promoted slot that may repeat or be out of area
MAX_INDEX = 1008                   # 42 pages: from this index on Rightmove answers 200 without results
SHARED = {"flat share", "house share", "room"}
STUDIO_TYPES = {"studio", "studio flat", "studio apartment"}


def _listing(x):
    subtype = str(x.get("propertySubType", "")).lower()
    studio = subtype in STUDIO_TYPES
    if x.get("students") or x.get("auction") or subtype in SHARED or (studio and not studios_wanted()):
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
    l.studio = studio
    l.first_listed = iso_date(x.get("firstVisibleDate"))
    l.first_listed_at = _timestamp(x.get("firstVisibleDate"))
    update = x.get("listingUpdate") or {}
    l.update_reason = update_kind(update.get("listingUpdateReason") or update.get("reason"))
    l.update_date = iso_date(update.get("listingUpdateDate") or update.get("date")) if l.update_reason else None
    l.bathrooms = int(x["bathrooms"]) if str(x.get("bathrooms") or "").isdigit() else None
    branch = re.sub(r"^\s*by\s+", "", str(x.get("formattedBranchName") or ""), flags=re.I).strip()
    l.agent = branch or None
    return private_rent_costs(l)


def _timestamp(value):
    """'2026-11-09T14:03:12Z' -> '2026-11-09T14:03:12+00:00' (UTC); a bare date or junk -> None."""
    value = str(value or "")
    if "T" not in value:
        return None
    try:
        when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    return when.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _contact_phone(p):
    """The branch number from propertyData.contactInfo.telephoneNumbers, when published. The shape
    comes from the spec, not a stored page, so every level is checked and anything odd is dropped."""
    contact = p.get("contactInfo")
    numbers = contact.get("telephoneNumbers") if isinstance(contact, dict) else None
    if not isinstance(numbers, dict):
        return None
    for name in ("localNumber", "internationalNumber"):
        number = phone_number(numbers.get(name))
        if number:
            return number
    return None


def _params(index, **extra):
    return {"locationIdentifier": LOCATION, "radius": float(config.RENT_SEARCH_MILES),
            "minBedrooms": min_bedrooms(), "maxPrice": int(config.MAX_RENT),
            "dontShow": "houseShare,student,retirement",   # 319 -> 114 results: fewer proxied pages
            "sortType": 6, "index": index, **extra}


def _results(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    return json.loads(m.group(1))["props"]["pageProps"]["searchResults"] if m else None


def _total(results):
    return int(str(results.get("resultCount", "0")).replace(",", "") or 0)


def search():
    out, seen = [], set()
    for page in range(config.RENT_MAX_PAGES):
        html = net.page(f"{BASE}/property-to-rent/find.html", params=_params(page * PAGE_SIZE))
        results = _results(html)
        if results is None:
            if page * PAGE_SIZE >= MAX_INDEX:
                # Past the last reachable page Rightmove serves a 200 "couldn't find the place" page
                print(f"Rightmove: no results page at index {page * PAGE_SIZE}; end of the reachable results",
                      file=sys.stderr)
                break
            raise ValueError("Rightmove page has no __NEXT_DATA__ (blocked or layout changed)")
        for x in results.get("properties") or []:
            if x.get("id") in seen:
                continue
            seen.add(x.get("id"))
            l = _listing(x)
            if l:
                out.append(l)
        if (page + 1) * PAGE_SIZE >= _total(results):
            break
    return out


REACHABLE = MAX_INDEX     # results one query can page through
MAX_PAGES = MAX_INDEX // PAGE_SIZE   # 42 pages


def band_page(low, high, page):
    """One page of a deep-sweep price band (minPrice..maxPrice inclusive, whole pounds):
    {"listings", "raw" (items on the page before filtering), "total" (all results in the band),
    "more" (another reachable page follows)}."""
    index = page * PAGE_SIZE
    results = _results(net.page(f"{BASE}/property-to-rent/find.html",
                                params=_params(index, minPrice=int(low), maxPrice=int(high))))
    if results is None:
        if index >= MAX_INDEX:
            return {"listings": [], "raw": 0, "total": None, "more": False}
        raise ValueError("Rightmove page has no __NEXT_DATA__ (blocked or layout changed)")
    total = _total(results)
    properties = results.get("properties") or []
    listings = [l for x in properties if (l := _listing(x))]
    return {"listings": listings, "raw": len(properties), "total": total,
            "more": (page + 1) * PAGE_SIZE < min(total, MAX_INDEX)}


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
    living = p.get("livingCosts") or {}
    addr = p.get("address") if isinstance(p.get("address"), dict) else {}
    postcode = f"{addr.get('outcode') or ''} {addr.get('incode') or ''}".strip()
    band = str(living.get("councilTaxBand") or "").strip().upper()
    return {"description": text(f"{lettings.get('letType') or ''} let. {lettings.get('furnishType') or ''}. "
                                f"{features} {(p.get('text') or {}).get('description', '')}"),
            "postcode": postcode if len(postcode.split()) == 2 else None,
            "floor_area_m2": sqm or None, "floorplan": plans[0] if plans else None,
            "deposit": lettings.get("deposit") or None, "available": available,
            "furnishing": rental_text.furnishing(lettings.get("furnishType") or ""),
            "council_tax_band": band if band in "ABCDEFGH" and len(band) == 1 else None,
            "agent_phone": _contact_phone(p),
            "council_tax_included": living["councilTaxIncluded"]
            if isinstance(living.get("councilTaxIncluded"), bool) else None}


def refine(listing):
    listing.detail_unavailable = False  # ephemeral retry signal, never a market fact
    d = cache.get("rm_detail", listing.id)
    if d is None or "council_tax_band" not in d:   # entries cached before the band was captured
        try:
            p = _property_data(net.page(listing.url))
        except (RequestException, ValueError) as ex:
            print(f"Rightmove detail unavailable for {listing.id}: {type(ex).__name__}", file=sys.stderr)
            response = getattr(ex, "response", None)
            listing.detail_unavailable = response is None or response.status_code not in (404, 410)
            return False
        if str(p.get("id")) != listing.id:
            print(f"Rightmove detail has no matching property ID: {listing.id}", file=sys.stderr)
            listing.detail_unavailable = True
            return False
        d = _details(p)
        cache.put("rm_detail", listing.id, d)
    return apply_rent_details(listing, d)
