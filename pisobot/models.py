import re
from dataclasses import dataclass, field
from typing import Optional

from . import config


@dataclass
class Listing:
    source: str
    id: str
    url: str
    address: str
    bedrooms: int
    scheme: str                       # e.g. "Shared Ownership", "SO Resale", "London Living Rent"
    tenure: str = "sale"              # "sale" or "rent"
    share_price: Optional[float] = None
    share_pct: Optional[float] = None
    full_value: Optional[float] = None
    rent: Optional[float] = None      # monthly rent (on the unowned share, or LLR rent)
    service_charge: Optional[float] = None
    min_deposit: Optional[float] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    image: Optional[str] = None
    description: str = ""
    estimated: list = field(default_factory=list)   # which cost components were estimated

    # filled by costs.compute / enrich
    deposit: Optional[float] = None
    mortgage: Optional[float] = None
    monthly_cost: Optional[float] = None
    travel_minutes: Optional[int] = None
    crime_score: Optional[int] = None

    @property
    def key(self):
        return f"{self.source}:{self.id}"

    @property
    def postcode(self):
        m = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})?\b", self.address.upper())
        return m.group(0).replace(" ", "") if m else ""

    @property
    def postcode_full(self):
        m = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", self.address.upper())
        return f"{m.group(1)} {m.group(2)}" if m else ""

    @property
    def dedupe_key(self):
        """Same flat listed on several portals -> same key (postcode + beds + share price)."""
        price = round((self.share_price or self.rent or 0) / 500)
        return f"{self.postcode}|{self.bedrooms}|{price}"

    def excluded_by_keywords(self):
        text = f"{self.address} {self.scheme} {self.description}".lower()
        return any(k in text for k in config.EXCLUDE_KEYWORDS)
