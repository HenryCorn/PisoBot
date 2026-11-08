"""OpenRent's public search, without JavaScript execution or challenge bypass.

Schema verified against /properties-to-rent/london and /properties-to-rent/oxford-circus:
the HTML embeds parallel numeric arrays for ALL results, not just its 20 cards.
Price/bed filters are client-side. We apply them locally, sort dateFirstListedMs
newest first, then GET /search/propertiesbyid?ids=... in the site's 20-ID batches.
The anonymous site's first-24-hour exclusive-access window is respected.
RENT_MAX_PAGES bounds metadata batches, not the initial index request; truncation
is reported, never represented as a complete inventory. Details are read only
by refine(), after the caller's cheap filters, and cached for a day unless the
search card's rent or bills change.
"""
import datetime as dt
import json
import math
import re
import sys
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from requests import RequestException

from .. import cache, config, net
from ..models import Listing
from . import (PRIVATE_RENT, ROOM_LET, apply_rent_details, cached_search, detail_fields,
               money, private_rent_costs, text)

BASE = "https://www.openrent.co.uk"
PAGE_SIZE = 20
EXCLUDED = re.compile(
    r"\bstudio\b|\b(?:room|bedsit)\s+(?:in|to\s+(?:rent|let))\b"
    r"|\b(?:house|flat)[ -]?share\b|\bshared\s+(?:house|flat|accommodation)\b"
    r"|\b(?:students?[\s-]+only|only\s+(?:for\s+)?students?|student\s+accommodation)\b"
    r"|\bretirement\b|\bsheltered\s+(?:housing|accommodation)\b"
    r"|\b(?:over[\s-]+|aged\s+)(?:55|60)\b", re.I)
ARRAYS = ("PROPERTYIDS", "PROPERTYLISTLATITUDES", "PROPERTYLISTLONGITUDES", "prices",
          "bedrooms", "isshared", "isstudio", "nonStudents", "islivelistBool",
          "dateFirstListedMs", "propertyTypes", "bills")


def _array(html, name):
    match = re.search(r"\bvar\s+" + name + r"\s*=\s*(\[.*?\])\s*;", html, re.S)
    if not match:
        raise ValueError(f"OpenRent search missing {name} (blocked or layout changed)")
    try:
        values = json.loads(re.sub(r",\s*]", "]", match[1]))
    except ValueError as ex:
        raise ValueError(f"OpenRent invalid {name}") from ex
    if not all(type(v) in (int, float) and math.isfinite(v) for v in values):
        raise ValueError(f"OpenRent invalid numeric array {name}")
    return values


def _km(lat, lng, target_lat, target_lng):
    a = (math.sin(math.radians(lat - target_lat) / 2) ** 2
         + math.cos(math.radians(lat)) * math.cos(math.radians(target_lat))
         * math.sin(math.radians(lng - target_lng) / 2) ** 2)
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, a)))


def _index(html):
    count = re.search(r"\bvar\s+NUMBEROFPROPERTIES\s*=\s*(\d+)\s*;", html)
    if not count:
        raise ValueError("OpenRent search has no result count (blocked or layout changed)")
    arrays = {name: _array(html, name) for name in ARRAYS}
    if any(len(values) != int(count[1]) for values in arrays.values()):
        raise ValueError("OpenRent search has incomplete property arrays")
    centre = {}
    for name in ("SEARCHLAT", "SEARCHLNG", "SEARCHRADIUS"):
        values = re.findall(r"\b" + name + r"\s*=\s*(-?\d+(?:\.\d+)?)\s*;", html)
        if not values:
            raise ValueError(f"OpenRent search missing {name}")
        centre[name] = float(values[-1])
    if (_km(centre["SEARCHLAT"], centre["SEARCHLNG"], config.TARGET_LAT, config.TARGET_LNG)
            + config.RENT_SEARCH_MILES * 1.609344 > centre["SEARCHRADIUS"] + 0.001):
        raise ValueError("OpenRent returned a search area that does not cover the requested radius")
    # These are literal assignments in the site's public anonymous bootstrap.
    # Take the most restrictive window instead of evaluating its JavaScript.
    windows = re.findall(r"\blockedForHours\s*=\s*(\d+)\s*;", html)
    if not windows:
        raise ValueError("OpenRent search missing exclusive-access window")
    locked_hours = max(map(int, windows))
    out, seen = [], set()
    now_ms = time.time() * 1000
    for i, identifier in enumerate(arrays["PROPERTYIDS"]):
        row = {name: values[i] for name, values in arrays.items()}
        if identifier <= 0 or int(identifier) != identifier:
            raise ValueError("OpenRent invalid property ID")
        if not -90 <= row["PROPERTYLISTLATITUDES"] <= 90 or not -180 <= row["PROPERTYLISTLONGITUDES"] <= 180:
            raise ValueError("OpenRent invalid coordinates")
        if any(row[key] not in (0, 1) for key in ("isshared", "isstudio", "nonStudents", "islivelistBool", "bills")):
            raise ValueError("OpenRent invalid property flags")
        if (identifier in seen or not row["islivelistBool"] or row["isshared"] or row["isstudio"]
                or not row["nonStudents"] or row["propertyTypes"] == 3
                or row["bedrooms"] < max(1, config.MIN_BEDROOMS)
                or not config.RENT_MIN <= row["prices"] <= config.MAX_MONTHLY_COST
                or now_ms - row["dateFirstListedMs"] < locked_hours * 3600000
                or _km(row["PROPERTYLISTLATITUDES"], row["PROPERTYLISTLONGITUDES"],
                       config.TARGET_LAT, config.TARGET_LNG) > config.RENT_SEARCH_MILES * 1.609344):
            continue
        seen.add(identifier)
        out.append(row)
    return sorted(out, key=lambda row: row["dateFirstListedMs"], reverse=True)


def _photo(url):
    if not isinstance(url, str) or not url:
        return None
    url = urljoin(BASE, url)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.hostname != "imagescdn.openrent.co.uk":
        return None
    return "https://" + parts.netloc + parts.path


def _listing(card, row):
    title = card.get("title")
    rent = card.get("rentPerMonth")
    if (not isinstance(title, str) or not title.strip()
            or type(rent) not in (int, float) or not math.isfinite(rent) or rent <= 0
            or not isinstance(card.get("description"), str)
            or not isinstance(card.get("letAgreed"), bool)
            or not isinstance(card.get("isMultiRoom"), bool)):
        raise ValueError("OpenRent card missing required listing data")
    beds = re.match(r"(\d+)\s+Bed\b", title, re.I)
    description = text(f"{title}. {card['description']}")
    if (card["letAgreed"] or card["isMultiRoom"] or EXCLUDED.search(description) or ROOM_LET.search(description)
            or not config.RENT_MIN <= rent <= config.MAX_MONTHLY_COST):
        return None
    if not beds:
        raise ValueError("OpenRent card has no bedroom count")
    if int(beds[1]) < max(1, config.MIN_BEDROOMS):
        return None
    photos = [card.get("imageUrl")] + (card.get("featuredPhotoUrls") or [])
    images = list(dict.fromkeys(url for value in photos if (url := _photo(value))))
    if row["bills"]:
        description += " Bills included."
    listing = Listing(
        source="orent", id=str(card["id"]), url=f"{BASE}/{card['id']}",
        address=title.split(",", 1)[-1].strip(), bedrooms=int(beds[1]),
        scheme=PRIVATE_RENT, tenure="rent", rent=float(rent),
        lat=row["PROPERTYLISTLATITUDES"], lng=row["PROPERTYLISTLONGITUDES"],
        image=images[0] if images else None, images=images, description=description,
    )
    return private_rent_costs(listing)


def search():
    if config.RENT_MAX_PAGES < 1:
        raise ValueError("RENT_MAX_PAGES must be positive")
    params = {"term": "Oxford Circus", "latn": config.TARGET_LAT, "lngn": config.TARGET_LNG,
              "area": math.ceil(config.RENT_SEARCH_MILES * 1.609344), "searchType": "km",
              "prices_max": int(config.MAX_MONTHLY_COST), "bedrooms_min": max(1, config.MIN_BEDROOMS)}
    html = net.page(f"{BASE}/properties-to-rent/oxford-circus", params=params)
    rows = _index(html)
    limit = config.RENT_MAX_PAGES * PAGE_SIZE
    if len(rows) > limit:
        print(f"OpenRent search truncated: newest {limit} of {len(rows)} eligible properties; "
              "inventory is incomplete (RENT_MAX_PAGES)", file=sys.stderr)
    out = []
    for start in range(0, min(len(rows), limit), PAGE_SIZE):
        batch = rows[start:start + PAGE_SIZE]
        cards = json.loads(net.page(f"{BASE}/search/propertiesbyid",
                                   params=[("ids", row["PROPERTYIDS"]) for row in batch]))
        if not isinstance(cards, list) or not all(isinstance(card, dict) for card in cards):
            raise ValueError("OpenRent card endpoint returned an invalid response")
        by_id = {str(card.get("id")): card for card in cards}
        wanted = {str(row["PROPERTYIDS"]) for row in batch}
        if set(by_id) != wanted or len(by_id) != len(cards):
            raise ValueError("OpenRent card endpoint returned missing, duplicate or unexpected IDs")
        for row in batch:
            listing = _listing(by_id[str(row["PROPERTYIDS"])], row)
            if listing:
                out.append(listing)
    return out


def fetch():
    # Reusing a search after expanding its radius/budget would hide new matches.
    filters = [config.TARGET_LAT, config.TARGET_LNG, config.RENT_SEARCH_MILES,
               config.RENT_MIN, config.MAX_MONTHLY_COST, config.MIN_BEDROOMS, config.RENT_MAX_PAGES]
    hit = cache.get("rent_search", "orent") or {}
    if hit.get("filters") != filters:
        cache.put("rent_search", "orent", {**hit, "at": ""})
    listings = cached_search("orent", search)
    cache.put("rent_search", "orent", {**cache.get("rent_search", "orent"), "filters": filters})
    return listings


class _Node:
    def __init__(self, tag="", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def nodes(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.nodes()

    def plain(self):
        if self.tag in ("script", "style", "svg", "button"):
            return ""
        return " ".join(child.plain() if isinstance(child, _Node) else child for child in self.children)


class _Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _available(value):
    if not value:
        return None
    if value.lower() == "today":
        return dt.datetime.now(ZoneInfo("Europe/London")).date().isoformat()
    for fmt in ("%d %B, %Y", "%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError("OpenRent unrecognised availability date")


def _details(html, identifier):
    nodes = list(_Document(html).root.nodes())
    canonical = next((node.attrs.get("href", "") for node in nodes
                      if node.tag == "link" and node.attrs.get("rel") == "canonical"), "")
    if urlsplit(canonical).hostname != "www.openrent.co.uk" or urlsplit(canonical).path.rstrip("/").split("/")[-1] != identifier:
        raise ValueError("OpenRent detail has no matching property ID")
    description = next((text(node.plain()) for node in nodes if node.attrs.get("id") == "descriptionText"), "")
    if not description:
        raise ValueError("OpenRent detail has no description (unavailable, locked or layout changed)")
    cells = {}
    for node in nodes:
        if node.tag == "tr":
            row = [child for child in node.children if isinstance(child, _Node) and child.tag == "td"]
            if len(row) == 2:
                cells[text(row[0].plain())] = row[1]
    rent = money(cells["Rent PCM"].plain()) if "Rent PCM" in cells else None
    if not rent:
        raise ValueError("OpenRent detail has no monthly rent")
    bills = cells.get("Bills Included")
    if bills:
        classes = " ".join(node.attrs.get("class", "") for node in bills.nodes())
        if "text-danger" in classes:
            description += " Bills not included."
        elif "text-success" in classes:
            description += " Bills included."
    images = list(dict.fromkeys(url for node in nodes
                               if node.tag == "a" and "lightbox_item" in node.attrs.get("class", "").split()
                               and (url := _photo(node.attrs.get("href")))))
    return {"description": description, "rent": rent,
            "deposit": money(cells["Deposit"].plain()) if "Deposit" in cells else None,
            "available": _available(text(cells["Available From"].plain())) if "Available From" in cells else None,
            "floor_area_m2": detail_fields(description).get("floor_area_m2"), "images": images}


def _bills_kind(listing):
    """Whether the card says bills are included; not the amount, which is a per-person estimate."""
    return "included" if listing.bills == 0 else "council tax" if "council tax" in listing.estimated else "extra"


def refine(listing):
    hit = cache.get("orent_detail", listing.id) or {}
    if "search_bills_kind" not in hit and "search_bills" in hit:   # entries cached before per-person bills
        hit["search_bills_kind"] = ("included" if hit["search_bills"] == 0
                                    else "council tax" if hit["search_bills"] == config.RENT_COUNCIL_TAX_ESTIMATE
                                    else "extra")
    try:
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(hit.get("at", ""))
    except ValueError:
        age = None
    details = (hit.get("details") if age is not None and age < dt.timedelta(days=1)
               and hit.get("search_rent") == listing.rent and hit.get("search_bills_kind") == _bills_kind(listing)
               else None)
    if details is None:
        try:
            details = _details(net.page(listing.url), listing.id)
        except (RequestException, ValueError) as ex:
            print(f"OpenRent detail unavailable for {listing.id}: {type(ex).__name__}", file=sys.stderr)
            return False
        cache.put("orent_detail", listing.id, {
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "details": details,
            "search_rent": listing.rent, "search_bills_kind": _bills_kind(listing)})
    listing.rent = details["rent"]
    if not config.RENT_MIN <= listing.rent <= config.MAX_MONTHLY_COST:
        return False
    if details["images"]:
        listing.images, listing.image = details["images"], details["images"][0]
    return apply_rent_details(listing, details) and not EXCLUDED.search(listing.description)
