"""spareroom.co.uk whole-property lets ("flats to rent"), from the server-rendered list at
/flats-to-rent/london (newest-ish first, ~11 per page, /pageN). robots.txt allows these pages for
every user agent but disallows SpareRoom's filter parameters, so budget/bed filtering is local.

Cards carry data-listing-* attributes (id, type, title, neighbourhood, outcode, property type,
rooms, advertiser role, headline rent + period). Advert pages need JavaScript, so there is no
refine(): coordinates come from a full postcode quoted in the card text when there is one, else
the outcode centroid (postcodes.io) and the card says the location is approximate, as are its
commute and crime figures (an outcode spans roughly 1-3 km)."""
import datetime as dt
import math
import re
from html import unescape
from urllib.parse import urljoin

from .. import cache, config, net
from ..models import Listing
from . import (PRIVATE_RENT, is_room_let, cached_search, money, monthly_rent, private_rent_costs,
               rental_filters, text)

BASE = "https://www.spareroom.co.uk"
LIST = f"{BASE}/flats-to-rent/london"
OUTCODE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$")
APPROXIMATE = "Location approximate (postcode district centre)."


def _km(lat, lng):
    return ((lat - config.TARGET_LAT) ** 2 * 111.2 ** 2
            + ((lng - config.TARGET_LNG) * 111.2 * 0.6225) ** 2) ** 0.5


def _coords(namespace, code):
    """postcodes.io centroid of a full postcode ("postcode", shared with enrich.geocode) or a
    district ("outcode"), cached; an empty list caches "unknown"."""
    hit = cache.get(namespace, code)
    if hit is None:
        path = "postcodes" if namespace == "postcode" else "outcodes"
        r = net.session.get(f"https://api.postcodes.io/{path}/{code}", timeout=20, verify=net.VERIFY)
        if r.status_code == 404:
            hit = []
        else:
            r.raise_for_status()
            result = r.json().get("result")
            if not isinstance(result, dict):
                raise ValueError("postcodes.io returned no coordinate result")
            hit = [result.get("latitude"), result.get("longitude")]
            if (not all(type(v) in (int, float) and math.isfinite(v) for v in hit)
                    or not -90 <= hit[0] <= 90 or not -180 <= hit[1] <= 180):
                raise ValueError("postcodes.io returned invalid coordinates")
        cache.put(namespace, code, hit)
    return tuple(hit) if hit else (None, None)


def _available(value):
    """'20th Nov 2026' -> '2026-11-20'; 'now' or anything else -> None."""
    match = re.match(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3})[a-z]*\s+(\d{4})", (value or "").strip())
    try:
        return dt.datetime.strptime(" ".join(match.groups()), "%d %b %Y").date().isoformat() if match else None
    except ValueError:
        return None


def _cards(html):
    starts = [m.start() for m in re.finditer(r'<li\s+class="listing-result"', html)]
    for i, start in enumerate(starts):
        block = html[start:starts[i + 1] if i + 1 < len(starts) else len(html)]
        attrs = {k: unescape(v) for k, v in re.findall(r'data-listing-([a-z-]+)="([^"]*)"',
                                                         block[:block.find(">", block.find("data-listing-id"))])}
        yield attrs, block


def _listing(attrs, block):
    if attrs.get("type") != "flatstorent" or not attrs.get("id", "").isdigit():
        return None
    title = attrs.get("title", "")
    kind = attrs.get("property-type", "").lower()
    try:
        beds = int(attrs.get("rooms-in-property") or 0)
    except ValueError:
        return None
    period = attrs.get("ad-headline-rate-period", "pcm").lower()
    rent = monthly_rent(money(attrs.get("ad-headline-rate")), "weekly" if period == "pw" else "monthly")
    outcode = attrs.get("postcode", "").upper().replace(" ", "")
    if (kind == "studio" or beds < max(1, config.MIN_BEDROOMS) or not rent
            or not config.RENT_MIN <= rent <= config.MAX_MONTHLY_COST or not OUTCODE.match(outcode)
            or re.search(r"\b(?:studio|bedsit)\b|\broom\s+(?:in|to\s+(?:rent|let))\b"
                         r"|\b(?:double|single)\s+room\b", title, re.I)):
        return None
    snippet = re.search(r'class="listing-card__short_description"[^>]*>(.*?)</', block, re.S)
    description = text(f"{title}. {kind} {beds} bed. {snippet[1] if snippet else ''} "
                       f"Advertiser: {attrs.get('advertiser-role', '')}.")
    if is_room_let(description):
        return None
    # Landlords often quote the full postcode in the text; otherwise use the district centre
    full = re.search(rf"\b{outcode}\s*(\d[A-Z]{{2}})\b", description.upper())
    postcode = f"{outcode} {full[1]}" if full else ""
    lat, lng = _coords("postcode", postcode) if postcode else (None, None)
    exact = lat is not None
    if not exact:
        lat, lng = _coords("outcode", outcode)
    if lat is None or _km(lat, lng) > config.RENT_SEARCH_MILES * 1.609344:
        return None
    link = re.search(r'class="listing-card__link"\s+href="([^"]+)"', block)
    url = urljoin(BASE, unescape(link[1]).split("?")[0]) if link else f"{LIST}/{attrs['id']}"
    image = re.search(r"src='(//photos\d*\.spareroom\.co\.uk/[^'?]+)", block)
    images = [f"https:{image[1]}"] if image else []
    neighbourhood = attrs.get("neighbourhood", "")
    available = re.search(r'listing-card__availability"[^>]*>\s*-?\s*Available\s+([^<]+)<', block)
    return private_rent_costs(Listing(
        source="sr", id=attrs["id"], url=url, address=(f"{neighbourhood}, " if neighbourhood and outcode not in neighbourhood else "")
        + f"London {postcode or outcode}",
        bedrooms=beds, scheme=PRIVATE_RENT, tenure="rent", rent=rent, lat=lat, lng=lng,
        image=images[0] if images else None, images=images,
        available=_available(available[1]) if available else None,
        estimated=[] if exact else ["location"],
        description=description if exact else f"{description} {APPROXIMATE}"))


def search():
    if config.RENT_MAX_PAGES < 1:
        raise ValueError("RENT_MAX_PAGES must be positive")
    out, seen = [], set()
    for page in range(1, config.RENT_MAX_PAGES + 1):
        html = net.page(LIST if page == 1 else f"{LIST}/page{page}")
        cards = list(_cards(html))
        if not cards:
            raise ValueError("SpareRoom list has no listing cards (blocked or layout changed)")
        for attrs, block in cards:
            if attrs.get("id") in seen:
                continue
            seen.add(attrs.get("id"))
            listing = _listing(attrs, block)
            if listing:
                out.append(listing)
        if f"/flats-to-rent/london/page{page + 1}" not in html:
            break
    return out


def fetch():
    filters = [*rental_filters(), config.RENT_MAX_PAGES]
    hit = cache.get("rent_search", "sr") or {}
    if hit.get("filters") != filters:
        cache.put("rent_search", "sr", {**hit, "at": ""})
    listings = cached_search("sr", search)
    cache.put("rent_search", "sr", {**cache.get("rent_search", "sr"), "filters": filters})
    return listings
