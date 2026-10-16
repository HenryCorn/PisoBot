import re
import datetime as dt
from dataclasses import asdict, fields
from html import unescape

from .. import cache, config
from ..models import Listing


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
    """Bills (estimated unless the advert says they are included) and the capped deposit."""
    listing.service_charge = 0   # the landlord pays it; adverts that mention one must not add it
    listing.estimated = [x for x in listing.estimated if x not in ("bills", "council tax")]
    blob = listing.description
    included = re.search(r"\b(?:all\s+)?bills\s+(?:are\s+)?inc(?:luded|l\.?|lusive)\b|\b(?:inclusive\s+of|including)\s+(?:all\s+)?bills\b",
                         blob, re.I)
    excluded = re.search(r"\bbills\s+(?:are\s+)?(?:not\s+included|excluded|extra)\b|\b(?:excluding|exclusive\s+of|plus)\s+bills\b",
                         blob, re.I)
    if included and not excluded:
        listing.bills = 0
        if re.search(r"\b(?:except|excluding|exc\.?|not\s+including|plus)\s+(?:the\s+)?council\s+tax\b", blob, re.I):
            listing.bills = config.RENT_COUNCIL_TAX_ESTIMATE
            listing.estimated.append("council tax")
    else:
        listing.bills = config.RENT_BILLS_ESTIMATE
        listing.estimated.append("bills")
    if listing.rent:
        listing.min_deposit = round(listing.rent * 12 / 52 * config.RENT_DEPOSIT_WEEKS)
    return listing


# A room in someone's flat or house, even when the portal files it as "1 bedroom flat"
ROOM_LET = re.compile(r"\bshar(?:e|ed|ing)\b[^.!?]{0,40}\b(?:kitchen|bathroom|house|flat|accommodation|living|lounge)"
                      r"|\ben[\s-]?suite\s+room\b|\bco-?living\b", re.I)


def apply_rent_details(listing, details):
    """Merge a lettings details page (see the sources' refine); False drops rooms and bedsits."""
    listing.description = f"{listing.description} {details['description']}"
    for key in ("floor_area_m2", "floorplan", "available"):
        if details.get(key):
            setattr(listing, key, details[key])
    private_rent_costs(listing)
    if details.get("deposit"):
        listing.min_deposit = details["deposit"]
    if ROOM_LET.search(listing.description):
        return False
    return not (listing.floor_area_m2 and listing.floor_area_m2 < config.RENT_MIN_AREA_M2)


def cached_search(name, search):
    """Run an expensive (proxied) portal search at most every RENT_REFRESH_HOURS; in between,
    the last results are rebuilt from the repo cache so hourly runs cost no proxy requests."""
    hit = cache.get("rent_search", name) or {}
    try:
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(hit.get("at", ""))
    except ValueError:
        age = None
    if age is not None and age < dt.timedelta(hours=config.RENT_REFRESH_HOURS):
        names = {f.name for f in fields(Listing)}
        return [Listing(**{k: v for k, v in d.items() if k in names}) for d in hit["listings"]]
    listings = search()
    keep = ("source", "id", "url", "address", "bedrooms", "scheme", "tenure", "rent", "service_charge",
            "bills", "available", "min_deposit", "lat", "lng", "image", "images", "floorplan",
            "floor_area_m2", "description", "estimated")
    cache.put("rent_search", name, {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "listings": [{**{k: v for k, v in asdict(l).items() if k in keep},
                      "images": l.images[:6], "description": l.description[:1500]} for l in listings],
    })
    return listings
