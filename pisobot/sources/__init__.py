import re
import math
import datetime as dt
from dataclasses import asdict, fields
from html import unescape

from .. import cache, config, council_tax
from ..models import Listing
from ..rental_text import ROOM_LET, is_room_let


def money(s):
    """'£1,403' / '&pound; 97,500' -> 1403.0"""
    if s is None:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", unescape(str(s)))
    return float(m.group(0).replace(",", "")) if m else None


def text(html):
    html = re.sub(r"<(script|style|svg)[^>]*>.*?</\1>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def lease_remaining(value):
    m = re.search(r"(\d{2,4})(?:\s*(?:years?|yrs?))?(?:\s+(?:lease\s+)?(?:from|commencing|starting)\b[^.;]{0,45}?\b((?:19|20)\d{2}))?",
                  str(value or ""), re.I)
    if not m:
        return None
    return max(0, int(m[1]) - (dt.date.today().year - int(m[2]) if m[2] else 0))


def detail_fields(blob):
    """Only explicit advert facts; absence stays unknown rather than becoming zero."""
    blob = text(blob)
    fields = {}
    for name, label in (("rent", r"rent"), ("service_charge", r"service\s+charges?")):
        label = rf"(?:(?:monthly|annual|yearly|weekly)\s+)?{label}"
        amount = r"£\s*([\d,]+(?:\.\d+)?)"
        unit = r"(?:per\s+(?:calendar\s+)?(?:month|annum|year|week)|p\.?c\.?m\.?|p\.?[amw]\.?|monthly|annually|yearly|weekly|/(?:month|year|week))"
        period = rf"(?:\s*\(?\s*{unit}\s*\)?)?"
        match = re.search(rf"\b{label}{period}\s*[:\-]?\s*{amount}{period}", blob, re.I)
        if not match:
            match = re.search(rf"{amount}{period}\s+{label}\b", blob, re.I)
        if match:
            value, phrase = money(match[1]), match[0].lower()
            if re.search(r"annum|annual|year|(?<![a-z])p\.?a\.?(?![a-z])", phrase):
                value /= 12
            elif re.search(r"week|(?<![a-z])p\.?w\.?(?![a-z])", phrase):
                value *= 52 / 12
            fields[name] = value
    share = re.search(r"\b(\d{1,3}(?:\.\d+)?)\s*%\s*(?:share|ownership|stake)", blob, re.I)
    full = re.search(r"\b(?:full(?:\s+market)?\s+value(?:\s+of\s+property)?|FMV|100%)\s*[:\-]?\s*£\s*([\d,]+(?:\.\d+)?)",
                     blob, re.I)
    if share and 0 < float(share[1]) <= 100:
        fields["share_pct"] = float(share[1])
    if full:
        fields["full_value"] = money(full[1])
    start = r"(?:from|commencing|starting)\b[^.;]{0,45}?\b(?:19|20)\d{2}"
    lease = re.search(rf"\blease(?:hold)?(?:\s+(?:length|remaining))?\s*[:\-]?\s*"
                      rf"(\d{{2,4}}(?:\s*years?)?(?:\s+lease)?(?:\s+{start})?)"
                      rf"|(\d{{2,4}}\s*years?\s+(?:remaining|unexpired|lease)(?:\s+{start})?)", blob, re.I)
    if lease:
        fields["lease_years"] = lease_remaining(lease[1] or lease[2])
    area = re.search(r"\b(?:total(?:\s+(?:gross|internal|floor))*(?:\s+area)?|(?:gross|internal|floor)\s+area|size)"
                     r"\s*[:\-=]?\s*(?:approx(?:\.|imately)?\s*)?([\d,.]+)\s*"
                     r"(m²|m2|sq\.?\s*m(?:etres?)?|square\s+metres?|sq\.?\s*ft|ft²|square\s+feet)\b",
                     blob, re.I)
    if area:
        value = money(area[1])
        if re.search(r"ft|feet", area[2], re.I):
            value *= 0.09290304
        if 0 < value < 10000:
            fields["floor_area_m2"] = round(value, 2)
    return fields


PRIVATE_RENT = "Private rent"


def monthly_rent(amount, frequency):
    """Portal rent in £/month; weekly and yearly prices are converted."""
    if not amount:
        return None
    frequency = str(frequency or "monthly").lower()
    if frequency.startswith("week"):
        return round(amount * 52 / 12, 2)
    if frequency.startswith(("year", "annual")):
        return round(amount / 12, 2)
    return float(amount)


def rent_from_text(price):
    """'£1,540 pcm (£355 pw)' / '£355 pw' -> monthly £."""
    price = str(price or "")
    pcm = re.search(r"£\s*([\d,]+(?:\.\d+)?)\s*pcm", price, re.I)
    if pcm:
        return money(pcm[1])
    pw = re.search(r"£\s*([\d,]+(?:\.\d+)?)\s*pw", price, re.I)
    return round(money(pw[1]) * 52 / 12, 2) if pw else money(price)


def private_rent_costs(listing):
    """Bills and the capped deposit. Bills = council tax (the advert's band at the borough's 2026-27
    Band D, London average and band C when unknown) + utilities (RENT_BILLS_ESTIMATE); either is 0
    when the advert says it is included. Estimated parts stay labelled in ``estimated``."""
    listing.service_charge = 0   # the landlord pays it; adverts that mention one must not add it
    listing.estimated = [x for x in listing.estimated if x not in ("bills", "council tax")]
    blob = listing.description
    included = re.search(r"\b(?:all\s+)?bills\s+(?:are\s+)?inc(?:luded|l\.?|lusive)\b|\b(?:inclusive\s+of|including)\s+(?:all\s+)?bills\b",
                         blob, re.I)
    excluded = re.search(r"\bbills\s+(?:are\s+)?(?:not\s+included|excluded|extra)\b|\b(?:excluding|exclusive\s+of|plus)\s+bills\b",
                         blob, re.I)
    except_tax = re.search(r"\b(?:except|excluding|exc\.?|not\s+including|plus)\s+(?:the\s+)?council\s+tax\b", blob, re.I)
    all_included = bool(included and not excluded and not except_tax)
    tax_included = all_included or (listing.council_tax_included is True)
    band = listing.council_tax_band or council_tax.band_from_text(blob)
    utilities, tax, label = 0, 0, ""
    if included and not excluded:
        pass                                  # utilities in the rent
    else:
        utilities = config.RENT_BILLS_ESTIMATE
        listing.estimated.append("bills")
    if not tax_included:
        tax, label, exact = council_tax.monthly(
            band, council_tax.borough(listing.address, listing.outcode))
        if not exact and "bills" not in listing.estimated:
            listing.estimated.append("council tax")
    listing.council_tax_monthly, listing.utilities_monthly = tax, utilities
    listing.council_tax_label = label
    listing.bills = tax + utilities
    if listing.advert_deposit:
        listing.min_deposit = listing.advert_deposit
    elif listing.rent:
        listing.min_deposit = round(listing.rent * 12 / 52 * config.RENT_DEPOSIT_WEEKS)
    return listing


def iso_date(value):
    """'2026-11-09T14:03:12Z' / '2026-11-09' -> '2026-11-09', else None."""
    try:
        return dt.date.fromisoformat(str(value or "")[:10]).isoformat()
    except ValueError:
        return None


def update_kind(reason):
    """A portal's listing-update reason as "added" / "reduced" (None when unrecognised)."""
    reason = str(reason or "").lower()
    if "reduc" in reason:
        return "reduced"
    return "added" if "new" in reason or "add" in reason else None


def apply_rent_details(listing, details):
    """Merge a lettings details page (see the sources' refine); False drops rooms and bedsits."""
    listing.description = f"{listing.description} {details['description']}"
    for key in ("floor_area_m2", "floorplan", "available", "council_tax_band", "agent_phone"):
        if details.get(key):
            setattr(listing, key, details[key])
    if details.get("council_tax_included") is not None:
        listing.council_tax_included = details["council_tax_included"]
    private_rent_costs(listing)
    if details.get("deposit"):
        listing.min_deposit = details["deposit"]
    if is_room_let(listing.description):
        return False
    return not (listing.floor_area_m2 and listing.floor_area_m2 < config.RENT_MIN_AREA_M2)


def rental_filters():
    return (config.TARGET_NAME, config.TARGET_LAT, config.TARGET_LNG, config.RENT_SEARCH_MILES,
            config.RENT_MIN, config.MAX_RENT, config.MIN_BEDROOMS)


def rental_in_scope(listing):
    if (listing.rent is None or not config.RENT_MIN <= listing.rent <= config.MAX_RENT
            or listing.bedrooms < max(1, config.MIN_BEDROOMS)
            or listing.lat is None or listing.lng is None):
        return False
    distance = math.hypot((listing.lat - config.TARGET_LAT) * 111.2,
                          (listing.lng - config.TARGET_LNG) * 111.2 * math.cos(math.radians(listing.lat)))
    return distance <= config.RENT_SEARCH_MILES * 1.609344


CACHE_FIELDS = ("source", "id", "url", "address", "bedrooms", "scheme", "tenure", "rent", "service_charge",
            "bills", "available", "min_deposit", "lat", "lng", "image", "images", "floorplan",
            "floor_area_m2", "description", "estimated", "council_tax", "floor", "lease_years",
            "council_tax_band", "council_tax_included", "council_tax_monthly", "utilities_monthly",
            "council_tax_label", "first_listed", "update_reason", "update_date", "update_text", "update_text_on",
            "bathrooms", "agent", "agent_phone", "first_listed_at",
            "advert_deposit")


def snapshot(listing, max_images=6):
    """The stored facts of a rental listing (search cache and market archive), JSON-safe. The search
    cache keeps 6 photos; the archive keeps more so "More photos" has something to send."""
    data = {k: v for k, v in asdict(listing).items() if k in CACHE_FIELDS}
    return {**data, "images": data.get("images", [])[:max_images],
            "description": (data.get("description") or "")[:1500]}


def phone_number(value):
    """A published UK phone number as plain text ('020 7123 4567'), else None. Defensive: portals'
    contact fields are not verified against stored pages, so anything unusual is dropped."""
    value = " ".join(str(value or "").split())
    digits = re.sub(r"\D", "", value)
    if not re.fullmatch(r"\+?[\d ()\-]{9,24}", value) or not 10 <= len(digits) <= 13:
        return None
    return value


def restore(data):
    """A Listing from a snapshot, ignoring fields this version no longer has."""
    names = {f.name for f in fields(Listing)}
    return Listing(**{k: v for k, v in data.items() if k in names})


def cached_search(name, search, minutes=None):
    """Run a portal search at most every RENT_REFRESH_MINUTES; in between,
    the last results are rebuilt from the repo cache so hourly runs cost no proxy requests."""
    hit = cache.get("rent_search", name) or {}
    try:
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(hit.get("at", ""))
    except ValueError:
        age = None
    if minutes is None:
        minutes = getattr(config, "RENT_REFRESH_MINUTES", config.RENT_REFRESH_HOURS * 60)
    if age is not None and age < dt.timedelta(minutes=minutes):
        return [restore(d) for d in hit["listings"]]
    listings = search()
    cache.put("rent_search", name, {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "listings": [snapshot(l) for l in listings],
    })
    return listings
