"""sharetobuy.com - server-rendered search results; cards carry data-* attributes and the
page embeds a JSON map of property coordinates (data-map-properties)."""
import json
import re
import sys
from html import unescape

from requests import RequestException

from .. import cache, config, net
from ..models import Listing
from . import detail_fields, money, text

BASE = "https://www.sharetobuy.com"

# schemeType ids from the search form
SCHEMES = {"2": "Shared Ownership", "1": "SO Resale", "13": "London Living Rent", "17": "Rent to Buy"}
SEARCHES = [
    # (rentOrBuy, scheme ids, maxMonthlyCost slack - costs are recomputed with our deposit)
    ("3", ["2", "1"], config.MAX_MONTHLY_COST + 300),
    ("2", ["13", "17"], config.MAX_MONTHLY_COST),
]
MAX_PAGES = 15


def _params(rent_or_buy, schemes, max_cost):
    p = [
        ("location", config.TARGET_NAME), ("lat", config.TARGET_LAT), ("lon", config.TARGET_LNG),
        ("radius", round(config.SEARCH_RADIUS_KM / 1.609)),  # whole miles; decimals return nothing
        ("minBedrooms", config.MIN_BEDROOMS), ("maxMonthlyCost", int(max_cost)),
        ("status[]", "1"), ("rentOrBuy", rent_or_buy),
    ]
    if rent_or_buy == "3":
        p.append(("maxDeposit", int(config.MAX_DEPOSIT)))
    p += [("schemeType[]", s) for s in schemes]
    return p


def _coords(html):
    m = re.search(r'data-map-properties="([^"]*)"', html)
    out = {}
    if m:
        data = json.loads(unescape(m.group(1))) or {}
        for pt in data.values():
            for pid in pt["data"]["propertyIds"]:
                out[str(pid)] = (float(pt["lat"]), float(pt["lng"]))
    return out


def _cards(html):
    starts = [m.start() for m in re.finditer(r'<div class="property-card ', html)]
    for i, s in enumerate(starts):
        yield html[s: starts[i + 1] if i + 1 < len(starts) else s + 40000]


def _attr(card, name):
    m = re.search(rf'data-{name}="([^"]*)"', card)
    return m.group(1) if m else None


def _photo_urls(html, pid):
    page = html.split(f'id="property-{pid}"', 1)
    if len(page) != 2:
        raise ValueError(f"Property {pid} not found on details page")
    gallery = re.search(r'card-images-main-image\b(.*?)card-images-secondary-image', page[1], re.S)
    if not gallery:
        return []
    return list(dict.fromkeys(unescape(url) for url in re.findall(
        r'data-src-original="(https://sih\.cdn\.sharetobuy\.com/[^"]+)"', gallery.group(1))))


def photos(listing):
    urls = cache.get("stb_photos", listing.id)
    if urls is None:
        urls = _photo_urls(net.get(listing.url).text, listing.id)
        cache.put("stb_photos", listing.id, urls)
    return urls


def _details(pid, card_cost):
    """Cost breakdown from the property page (cached while the card's cost is unchanged)."""
    ck = f"{pid}:{card_cost}"
    hit = cache.get("stb_detail3", ck)
    if hit:
        return hit
    try:
        html = net.get(f"{BASE}/properties/{pid}/").text
        if f'id="property-{pid}"' not in html:
            raise ValueError("Property ID missing from details page")
        t = text(html)
    except (RequestException, ValueError) as ex:
        print(f"ShareToBuy detail unavailable for {pid}: {type(ex).__name__}", file=sys.stderr)
        return {}
    f = lambda pat: money(m.group(1)) if (m := re.search(pat, t, re.I)) else None
    s = lambda pat: m.group(1) if (m := re.search(pat, t, re.I)) else None
    lease = f(r"Lease Length\s*(\d[\d,]*)")
    d = {
        "share_price": f(r"£\s*([\d,]+)\s*share price"),
        "share_pct": f(r"([\d.]+)\s*%\s*share"),
        "min_deposit": f(r"£\s*([\d,]+)\s*min deposit"),
        "rent": f(r"£\s*([\d,]+(?:\.\d+)?)\s*rent\b"),
        "service_charge": f(r"£\s*([\d,]+(?:\.\d+)?)\s*service charge"),
        "full_value": f(r"Full value of property\s*£\s*([\d,]+)"),
        "lease_years": int(lease) if lease else None,
        "council_tax": s(r"Council Tax Band\s*([A-H])\b"),
        "floor": s(r"\bFloor\s*(Ground|Lower ground|Basement|\d+(?:st|nd|rd|th))\b"),
    }
    description = re.search(r'<div\s+id="description-content".*?(?=<div class="property-information-and-downloads|\Z)',
                            html, re.S)
    d["description"] = text(description[0]) if description else ""
    d.update(detail_fields(d["description"]))
    plan = re.search(r'<a\b[^>]*class="[^"]*\bdownload-floor-plan\b[^"]*"[^>]*>', html, re.S)
    href = re.search(r'href="([^"]+)"', plan[0]) if plan else None
    d["floorplan"] = unescape(href[1]) if href else None
    cache.put("stb_photos", pid, _photo_urls(html, pid))
    cache.put("stb_detail3", ck, d)
    return d


def fetch():
    listings = {}
    for rent_or_buy, schemes, max_cost in SEARCHES:
        params = _params(rent_or_buy, schemes, max_cost)
        coords = {}
        for page in range(1, MAX_PAGES + 1):
            path = "/properties/" if page == 1 else f"/properties/page/{page}/"
            html = net.get(BASE + path, params=params).text
            coords.update(_coords(html))
            cards = list(_cards(html))
            if not cards:
                break
            for c in cards:
                pid = _attr(c, "property-id")
                if not pid or f"stb:{pid}" in listings:
                    continue
                status = re.search(r'card-status-text">([^<]*)', c)
                if status and status.group(1).strip().lower() != "available":
                    continue
                title = re.search(r'property-details-title.*?<a[^>]*>(.*?)</a>', c, re.S)
                desc = re.search(r'property-details-description[^>]*>(.*?)</div>', c, re.S)
                img = re.search(r'(?:data-src-original|src)="(https://sih\.cdn\.sharetobuy\.com/[^"]+)"', c)
                scheme_id = _attr(c, "scheme-type") or ""
                lat, lng = coords.get(pid, (None, None))
                l = Listing(
                    source="stb", id=pid, url=f"{BASE}/properties/{pid}/",
                    address=(text(title.group(1)) if title else "") or f"ShareToBuy #{pid}",
                    bedrooms=int(_attr(c, "bedrooms") or 0),
                    scheme=SCHEMES.get(scheme_id, f"scheme {scheme_id}"),
                    tenure="rent" if rent_or_buy == "2" else "sale",
                    min_deposit=money(_attr(c, "min-deposit")),
                    lat=lat, lng=lng, image=img.group(1) if img else None,
                    description=text(desc.group(1)) if desc else "",
                )
                card_cost = money(_attr(c, "monthly-cost"))
                if l.tenure == "rent":
                    l.rent = card_cost
                    l.service_charge = 0
                else:
                    for k, v in _details(pid, card_cost).items():
                        if v is not None:
                            setattr(l, k, v)
                listings[l.key] = l
            if f"/properties/page/{page + 1}/" not in html:
                break
    return list(listings.values())
