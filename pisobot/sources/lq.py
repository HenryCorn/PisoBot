"""lqhomes.com - public WordPress JSON: /wp-json/custom/developments and
/wp-json/custom/development/{id}/properties."""
import datetime as dt
import math
import re
from html import unescape

from .. import config, net
from ..models import Listing
from . import money

BASE = "https://lqhomes.com/wp-json/custom"
PLOT_TYPES = {
    "shared_ownership": ("Shared Ownership", "sale"),
    "london_living_rent": ("London Living Rent", "rent"),
    "rent_to_buy": ("Rent to Buy", "rent"),
}


def _lease_remaining(text):
    """'999 from May 2004' -> years left today; '125' -> 125."""
    m = re.search(r"(\d{2,4})(?:\D+(\d{4}))?", str(text or ""))
    if not m:
        return None
    years = int(m.group(1))
    if m.group(2):
        years -= dt.date.today().year - int(m.group(2))
    return years


def _km(lat, lng):
    return math.hypot((lat - config.TARGET_LAT) * 111.2,
                      (lng - config.TARGET_LNG) * 111.2 * math.cos(math.radians(lat)))


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
                description=f"{beds} {a.get('plot_tagline') or ''}",
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
                l.lease_years = _lease_remaining(a.get("plot_length_of_lease"))
            band = (a.get("plot_council_tax_band") or "").strip().upper()
            l.council_tax = band if len(band) == 1 else None
            l.floor = a.get("plot_floor") or None
            img = a.get("plot_featured_image")
            if isinstance(img, dict):
                l.image = img.get("url")
            out.append(l)
    return out
