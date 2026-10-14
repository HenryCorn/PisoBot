"""nhghomes.com - server-rendered plot cards on /find-a-home. Only the share price is
published statically, so rent and service charge are estimated. Coordinates come from
the postcode (enrich.geocode)."""
import re

from .. import enrich, net
from ..models import Listing
from . import money, text

BASE = "https://nhghomes.com"
MAX_PAGES = 30


def fetch():
    cards = []
    for page in range(1, MAX_PAGES + 1):
        html = net.get(f"{BASE}/find-a-home", params={"ownership": "Shared", "page": page}).text
        found = html.split('<div class="plotcard">')[1:]
        cards += found
        if not found or f'data-page="{page + 1}"' not in html:
            break

    out = []
    for c in cards:
        tag = re.search(r'plotcard-tag">([^<]*)', c)
        if not tag or tag.group(1).strip().lower() != "available":
            continue
        if "Shared ownership" not in c:
            continue
        href = re.search(r'href="(/developments/[^"]+)"', c)
        addr = re.search(r'plotcard-address">(.*?)</p>', c, re.S)
        beds = re.search(r"(\d+)\s*Bedroom", c)
        price = re.search(r"Shared ownership\s*</p>\s*<p>([^<]+)", c)
        img = re.search(r'<img src="([^"]+)"', c)
        if not (href and price):
            continue
        l = Listing(
            source="nhg", id=href.group(1).rsplit("/", 1)[-1], url=BASE + href.group(1),
            address=text(addr.group(1)) if addr else "", bedrooms=int(beds.group(1)) if beds else 0,
            scheme="Shared Ownership", share_price=money(price.group(1)),
            image=BASE + img.group(1) if img and img.group(1).startswith("/") else None,
            description=text(c)[:300],
        )
        out.append(l)

    enrich.geocode(out)
    return out
