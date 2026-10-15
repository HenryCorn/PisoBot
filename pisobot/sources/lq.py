"""lqhomes.com - public WordPress JSON: /wp-json/custom/developments and
/wp-json/custom/development/{id}/properties."""
import math
import re
import sys
from html import unescape

from requests import RequestException

from .. import cache, config, net
from ..models import Listing
from . import detail_fields, lease_remaining, money, text

BASE = "https://lqhomes.com/wp-json/custom"
PLOT_TYPES = {
    "shared_ownership": ("Shared Ownership", "sale"),
    "london_living_rent": ("London Living Rent", "rent"),
    "rent_to_buy": ("Rent to Buy", "rent"),
}


def _km(lat, lng):
    return math.hypot((lat - config.TARGET_LAT) * 111.2,
                      (lng - config.TARGET_LNG) * 111.2 * math.cos(math.radians(lat)))


def _page_details(html):
    main = re.search(r"<main\b[^>]*>(.*?)</main>", html, re.S)
    if not main:
        raise ValueError("Property page has no main content")
    blob = text(main[1])
    d = {**detail_fields(blob), "description": blob}
    slider = re.search(r'c-floorplan-slider\b(.*?)</section>', main[1], re.S)
    image = re.search(r'data-bg="([^"]+)"', slider[1]) if slider else None
    if image:
        d["floorplan"] = unescape(image[1])
    return d


def refine(listing):
    d = cache.get("lq_detail", listing.url)
    if d is None:
        try:
            d = _page_details(net.get(listing.url).text)
        except (RequestException, ValueError) as ex:
            print(f"L&Q optional details unavailable for {listing.id}: {type(ex).__name__}", file=sys.stderr)
            return not (isinstance(ex, RequestException) and ex.response is not None
                        and ex.response.status_code in (404, 410))
        cache.put("lq_detail", listing.url, d)
    listing.description += " " + d["description"]
    for key, value in d.items():
        if key != "description" and getattr(listing, key) is None:
            setattr(listing, key, value)
    return True


def fetch():
    out = []
    for dev in net.get(f"{BASE}/developments").json():
        loc = dev.get("development_location") or {}
        try:
            lat, lng = float(loc["lat"]), float(loc["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        if not dev.get("site_live") or not dev.get("plots_available") or _km(lat, lng) > config.SEARCH_RADIUS_KM:
            continue
        dev_name = unescape(dev["name"]).strip()
        dev_url = dev["url"].rstrip("/")
        for p in net.get(f"{BASE}/development/{dev['id']}/properties").json():
            a = p.get("acf") or {}
            kind = PLOT_TYPES.get(a.get("plot_type"))
            if not kind or a.get("plot_availability") != "available" or a.get("plot_retirement"):
                continue
            beds = a.get("plot_bedrooms") or "0"
            scheme, tenure = kind
            building = a.get("building_name") or ""
            title = p["post_title"] if building in p["post_title"] else f"{building} {p['post_title']}"
            l = Listing(
                source="lq", id=str(p["ID"]), url=f"{dev_url}/properties/{p['post_name']}/",
                address=f"{title.strip()}, {dev_name}, {loc.get('address', '')}".strip(", "),
                bedrooms=int(beds) if str(beds).isdigit() else 0,
                scheme=scheme, tenure=tenure, lat=lat, lng=lng,
                description=text(f"{beds} {a.get('plot_tagline') or ''} {a.get('plot_description') or ''} "
                                 f"{a.get('plot_special_features') or ''}"),
            )
            if tenure == "rent":
                l.rent = money(a.get("plot_monthly_rent"))
                l.service_charge = 0
            else:
                l.share_price = money(a.get("plot_share_price"))
                l.share_pct = money(a.get("plot_min_share"))
                l.full_value = money(a.get("plot_price"))
                l.rent = money(a.get("plot_monthly_rent"))
                l.service_charge = money(a.get("plot_service_charge"))
                l.min_deposit = money(a.get("plot_minimum_deposit"))
                l.lease_years = lease_remaining(a.get("plot_length_of_lease"))
            band = (a.get("plot_council_tax_band") or "").strip().upper()
            l.council_tax = band if len(band) == 1 else None
            l.floor = a.get("plot_floor") or None
            images = [a.get("plot_featured_image"), a.get("hero_image")]
            images += a.get("image_grid_gallery") or []
            images += (a.get("plot_image_grid") or {}).get("image_grid_gallery") or []
            l.images = list(dict.fromkeys(img["url"] for img in images
                                         if isinstance(img, dict) and img.get("url")))
            l.image = l.images[0] if l.images else None
            for key, value in detail_fields(l.description).items():
                if getattr(l, key) is None:
                    setattr(l, key, value)
            out.append(l)
    return out
