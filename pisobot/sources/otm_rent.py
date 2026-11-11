"""onthemarket.com lettings - same __NEXT_DATA__ layout as the shared-ownership search
(onthemarket.py), 30 per page. Search cards carry rent, beds, location and photos; details
pages are read only for listings that pass the cheap filters, to catch rooms and short lets."""
import datetime as dt
import os
import json
import re
import sys

from requests import RequestException

from .. import cache, config, net, rental_text
from ..models import Listing
from . import (PRIVATE_RENT, apply_rent_details, cached_search, min_bedrooms, money, private_rent_costs,
               rent_from_text, studios_wanted, text)
from .onthemarket import BASE, _photo_urls, _property_data

SHARED = re.compile(r"\b(?:room|flat\s*share|house\s*share|shared\s+(?:flat|house|accommodation))\b", re.I)
STUDIO = re.compile(r"\bstudio\b", re.I)


def _today():
    return dt.date.today()


def _apply_age(l, value):
    """'days-since-added-reduced' is text such as 'Added today', 'Reduced yesterday',
    'Reduced < 14 days' or 'Added > 14 days' (a bare day count is accepted too). Exact dates are
    stored absolute so cached searches stay right; vague phrases are kept verbatim and dated by
    when they were read, because they must not be shown once that reading is old."""
    value = " ".join(str(value if value is not None else "").split())
    today = _today()
    if value.isdigit():
        l.update_reason = "added_or_reduced"
        l.update_date = (today - dt.timedelta(days=int(value))).isoformat()
        return
    m = re.match(r"(added|reduced)\b\s*(.*)$", value, re.I)
    if not m:
        return
    l.update_reason = m[1].lower()
    when = m[2].lower()
    if when in ("today", "yesterday"):
        l.update_date = (today - dt.timedelta(days=int(when == "yesterday"))).isoformat()
    elif when:
        l.update_text, l.update_text_on = value, today.isoformat()


def _listing(x):
    title = x.get("property-title", "")
    studio = bool(STUDIO.search(x.get("humanised-property-type") or "") or rental_text.is_studio(title))
    if studio and not studios_wanted():
        return None
    if (x.get("ad?") or not x.get("id") or SHARED.search(x.get("humanised-property-type") or "")
            or rental_text.classify(title, rental_text.exclude_keywords())[0]
            or re.match(r"^(?:(?:double|single|en[- ]suite)\s+)?room\b", title, re.I)):
        return None
    rent = rent_from_text(x.get("price"))
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
    l.studio = studio
    _apply_age(l, x.get("days-since-added-reduced"))
    l.bathrooms = int(x["bathrooms"]) if str(x.get("bathrooms") or "").isdigit() else None
    agent = x.get("agent")
    if isinstance(agent, dict):
        agent = next((agent[k] for k in ("name", "display-name", "branch-name", "branch") if agent.get(k)), None)
    l.agent = " ".join(str(agent).split()) or None if agent else None
    deposit = re.search(r"deposit[^£\d]{0,30}£\s*([\d,]+(?:\.\d+)?)", str(x.get("fees-description") or ""), re.I)
    l.advert_deposit = money(deposit[1]) if deposit else None
    return private_rent_costs(l)


def search():
    out, seen = [], set()
    for page in range(1, config.RENT_MAX_PAGES + 1):
        # newest first (listing update date): the default "recommended" order hid new adverts behind page 3
        params = {"radius": float(config.RENT_SEARCH_MILES), "min-bedrooms": min_bedrooms(),
                  "max-price": int(config.MAX_RENT), "sort-field": "update_date"}
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


PAGE_SIZE = 30
# Observed (2026-11-10, radius 10, max-price 1600, sort-field update_date): totalResults is on normal
# pages (1655 for the whole query); page 34 returns 30 items with paginationControls.next false even
# though more results exist; pages 50 and 56 return an empty list, totalResults None and no pagination
# keys. So one query serves 34 pages (about 1,000 results) and then just says there is no next page.
MAX_PAGES = 34
REACHABLE = 1000        # a band reporting more than this is split before it is read


def _total(results):
    """The result count, from totalResults or a total-count key of paginationControls; None if absent."""
    candidates = [results.get("totalResults")]
    candidates += [v for k, v in (results.get("paginationControls") or {}).items() if "total" in str(k).lower()]
    for value in candidates:
        if value is not None and str(value).replace(",", "").isdigit():
            return int(str(value).replace(",", ""))
    return None


def band_page(low, high, page):
    """One page of a deep-sweep price band: {"listings", "raw" (items on the page before filtering),
    "total" (None if the page does not say), "more" (the portal links a next page)}. A missing
    next link is NOT proof the band is finished: see MAX_PAGES."""
    params = {"radius": float(config.RENT_SEARCH_MILES), "min-bedrooms": config.MIN_BEDROOMS,
              "min-price": int(low), "max-price": int(high), "sort-field": "update_date"}
    if page > 0:
        params["page"] = page + 1
    html = net.page(f"{BASE}/to-rent/flats-apartments/oxford-circus-station/", params=params)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("OnTheMarket page has no __NEXT_DATA__ (blocked or layout changed)")
    results = json.loads(m.group(1))["props"]["pageProps"]["initialReduxState"]["results"]
    rows = results.get("list") or []
    return {"listings": [l for x in rows if (l := _listing(x))], "raw": len(rows), "total": _total(results),
            "more": bool((results.get("paginationControls") or {}).get("next"))}


def fetch():
    return cached_search("otmr", search)


def refine(listing):
    listing.detail_unavailable = False  # ephemeral retry signal, never a market fact
    d = cache.get("otmr_detail", listing.id)
    if d is None:
        try:
            p = _property_data(net.page(listing.url))
        except (RequestException, ValueError) as ex:
            print(f"OnTheMarket rental detail unavailable for {listing.id}: {type(ex).__name__}", file=sys.stderr)
            response = getattr(ex, "response", None)
            listing.detail_unavailable = response is None or response.status_code not in (404, 410)
            return False
        if str(p.get("id")) != listing.id:
            print(f"OnTheMarket rental detail has no matching property ID: {listing.id}", file=sys.stderr)
            listing.detail_unavailable = True
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
        if os.environ.get("ALERT_TIMING") == "1" and isinstance(p.get("exclusive"), bool):
            d["exclusive"] = p["exclusive"]                 # off: the cached detail is exactly the release's
        cache.put("otmr_detail", listing.id, d)
    return apply_rent_details(listing, d)
