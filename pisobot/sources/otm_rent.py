"""onthemarket.com lettings - same __NEXT_DATA__ layout as the shared-ownership search
(onthemarket.py), 30 per page. Search cards carry rent, beds, location and photos; details
pages are read only for listings that pass the cheap filters, to catch rooms and short lets."""
import json
import re
import sys

from requests import RequestException

from .. import cache, config, net
from ..models import Listing
from . import PRIVATE_RENT, apply_rent_details, cached_search, money, private_rent_costs, text
from .onthemarket import BASE, _photo_urls, _property_data

SHARED = re.compile(r"\b(?:room|flat\s*share|house\s*share|shared\s+(?:flat|house|accommodation))\b", re.I)


def _rent(price):
    """'£1,540 pcm (£355 pw)' / '£355 pw' -> monthly £."""
    price = str(price or "")
    pcm = re.search(r"£\s*([\d,]+(?:\.\d+)?)\s*pcm", price, re.I)
    if pcm:
        return money(pcm[1])
    pw = re.search(r"£\s*([\d,]+(?:\.\d+)?)\s*pw", price, re.I)
    return round(money(pw[1]) * 52 / 12, 2) if pw else money(price)


def _listing(x):
    title = x.get("property-title", "")
    if x.get("ad?") or not x.get("id") or SHARED.search(f"{title} {x.get('humanised-property-type', '')}"):
        return None
    rent = _rent(x.get("price"))
    if not rent or rent < config.RENT_MIN:
        return None
    loc = x.get("location") or {}
    images = [img["default"] for img in x.get("images") or [] if img.get("default")]
    feats = " ".join(x.get("features") or [])
    l = Listing(
        source="otmr", id=str(x["id"]), url=BASE + x.get("details-url", f"/details/{x['id']}/"),
        address=x.get("address", ""), bedrooms=int(x.get("bedrooms") or 0),
        scheme=PRIVATE_RENT, tenure="rent", rent=rent,
        lat=loc.get("lat"), lng=loc.get("lon"),
        image=images[0] if images else None, images=images,
        description=text(f"{title}. {feats} {x.get('summary', '')}"),
    )
    return private_rent_costs(l)


def search():
    out, seen = [], set()
    for page in range(1, config.RENT_MAX_PAGES + 1):
        params = {"radius": float(config.RENT_SEARCH_MILES), "min-bedrooms": config.MIN_BEDROOMS,
                  "max-price": int(config.MAX_MONTHLY_COST)}
        if page > 1:
            params["page"] = page
        # flats only: drops house shares server-side (499 -> 309 results: fewer proxied pages)
        html = net.page(f"{BASE}/to-rent/flats-apartments/oxford-circus-station/", params=params)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            raise ValueError("OnTheMarket page has no __NEXT_DATA__ (blocked or layout changed)")
        results = json.loads(m.group(1))["props"]["pageProps"]["initialReduxState"]["results"]
        for x in results.get("list") or []:
            if x.get("id") in seen:
                continue
            seen.add(x.get("id"))
            l = _listing(x)
            if l:
                out.append(l)
        if not (results.get("paginationControls") or {}).get("next"):
            break
    return out


def fetch():
    return cached_search("otmr", search)


def refine(listing):
    d = cache.get("otmr_detail", listing.id)
    if d is None:
        try:
            p = _property_data(net.page(listing.url))
        except (RequestException, ValueError) as ex:
            print(f"OnTheMarket rental detail unavailable for {listing.id}: {type(ex).__name__}", file=sys.stderr)
            return False
        if str(p.get("id")) != listing.id:
            print(f"OnTheMarket rental detail has no matching property ID: {listing.id}", file=sys.stderr)
            return False
        lets = " ".join((p.get("lettingDetails") or {}).get("items") or [])
        feats = " ".join(f.get("feature", "") if isinstance(f, dict) else str(f) for f in p.get("features") or [])
        plans = _photo_urls({"images": p.get("floorplans") or []})
        area = money(p.get("minimumAreaSqM")) or (money(p.get("minimumAreaSqFt")) or 0) * 0.09290304 or None
        deposit = re.search(r"deposit:?\s*£\s*([\d,]+(?:\.\d+)?)", lets, re.I)
        d = {"description": text(f"{'Students only. ' if p.get('student') else ''}{lets}. {feats} "
                                 f"{p.get('description', '')}"),
             "deposit": money(deposit[1]) if deposit else None,
             "floor_area_m2": round(area, 2) if area else None, "floorplan": plans[0] if plans else None}
        cache.put("otmr_detail", listing.id, d)
    return apply_rent_details(listing, d)
