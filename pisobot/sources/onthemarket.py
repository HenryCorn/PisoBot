"""onthemarket.com - Next.js page; results live in __NEXT_DATA__ under
props.pageProps.initialReduxState.results.list. Mostly estate-agent resales. Listings
quote either the share price or the full value, so anything above SHARE_PRICE_CEILING is
treated as a full value and skipped (the share-price version is usually listed too)."""
import json
import re
import sys

from requests import RequestException

from .. import cache, config, net
from ..models import Listing
from . import detail_fields, money, text

BASE = "https://www.onthemarket.com"
SHARE_PRICE_CEILING = 250000
MAX_PAGES = 30


def _property_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    return json.loads(m.group(1))["props"]["pageProps"]["initialReduxState"].get("property", {}) if m else {}


def _photo_urls(property):
    return list(dict.fromkeys(img.get("largeUrl") or img.get("default") or img.get("url")
                             for img in property.get("images") or []
                             if isinstance(img, dict) and img.get("isImage", True)
                             and (img.get("largeUrl") or img.get("default") or img.get("url"))))


def photos(listing):
    urls = cache.get("otm_photos", listing.id)
    if urls is None:
        property = _property_data(net.get(listing.url).text)
        if str(property.get("id")) != listing.id:
            raise ValueError(f"Property {listing.id} not found on details page")
        urls = _photo_urls(property)
        cache.put("otm_photos", listing.id, urls)
    return urls


def fetch():
    out, seen = [], set()
    radius_mi = round(config.SEARCH_RADIUS_KM / 1.609, 1)
    for page in range(1, MAX_PAGES + 1):
        params = {"radius": radius_mi, "shared-ownership": "true", "min-bedrooms": config.MIN_BEDROOMS,
                  "max-price": SHARE_PRICE_CEILING}
        if page > 1:
            params["page"] = page
        html = net.get(f"{BASE}/for-sale/property/oxford-circus/", params=params).text
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            break
        results = json.loads(m.group(1))["props"]["pageProps"]["initialReduxState"]["results"]
        for x in results.get("list") or []:
            if x.get("ad?") or not x.get("id") or x["id"] in seen:
                continue
            seen.add(x["id"])
            price = money(x.get("price"))
            if not price or price > SHARE_PRICE_CEILING:
                continue
            feats = " ".join(x.get("features") or [])
            blob = f"{x.get('property-title', '')} {feats} {x.get('summary', '')}"
            pct = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%\s*(?:share|ownership|stake)", blob, re.I)
            full = re.search(r"(?:full(?:\s+market)?\s+value|FMV|100%)[^£]{0,30}£\s*([\d,]+)", blob, re.I)
            loc = x.get("location") or {}
            imgs = x.get("images") or []
            l = Listing(
                source="otm", id=str(x["id"]), url=BASE + x.get("details-url", f"/details/{x['id']}/"),
                address=x.get("address", ""), bedrooms=int(x.get("bedrooms") or 0),
                scheme="Shared Ownership (agent)", share_price=price,
                share_pct=float(pct.group(1)) if pct else None,
                full_value=money(full.group(1)) if full else None,
                lat=loc.get("lat"), lng=loc.get("lon"),
                image=imgs[0].get("default") if imgs else None,
                images=[img["default"] for img in imgs if img.get("default")], description=blob,
            )
            out.append(l)
        nxt = (results.get("paginationControls") or {}).get("next")
        if not nxt:
            break
    return out


def refine(listing):
    """Pull features + description from the details page (cached) so keyword exclusions and
    share % / full value parsing see the whole advert. Returns False to drop the listing."""
    d = cache.get("otm_detail2", listing.id)
    if d is None:
        try:
            html = net.get(listing.url).text
        except RequestException as ex:
            print(f"OnTheMarket detail unavailable for {listing.id}: {type(ex).__name__}", file=sys.stderr)
            return False
        p = _property_data(html)
        if str(p.get("id")) != listing.id:
            print(f"OnTheMarket detail has no matching property ID: {listing.id}", file=sys.stderr)
            return False
        cache.put("otm_photos", listing.id, _photo_urls(p))
        feats = " ".join(f.get("feature", "") if isinstance(f, dict) else str(f) for f in p.get("features") or [])
        description = text(f"{feats} {p.get('description', '')}")
        plans = _photo_urls({"images": p.get("floorplans") or []})
        d = {**detail_fields(description), "description": description,
             "floorplan": plans[0] if plans else None}
        area = money(p.get("minimumAreaSqM"))
        feet = money(p.get("minimumAreaSqFt"))
        if area or feet:
            d["floor_area_m2"] = area or round(feet * 0.09290304, 2)
        cache.put("otm_detail2", listing.id, d)
    listing.description = f"{listing.description} {d['description']}"
    for key, value in d.items():
        if key != "description" and value is not None:
            setattr(listing, key, value)
    blob = listing.description
    pct = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%\s*(?:shared?|ownership|stake)", blob, re.I)
    full = re.search(r"(?:full(?:\s+market)?\s+value|FMV|100%)[^£]{0,30}£\s*([\d,]+)", blob, re.I)
    if pct:
        listing.share_pct = float(pct.group(1))
    if full:
        listing.full_value = money(full.group(1))
    band = re.search(r"council tax band:?\s*([A-H])\b", blob, re.I)
    if band:
        listing.council_tax = band.group(1).upper()
    return True
