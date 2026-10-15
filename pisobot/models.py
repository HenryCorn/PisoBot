import hashlib
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
    images: list[str] = field(default_factory=list)
    floorplan: Optional[str] = None
    floor_area_m2: Optional[float] = None
    description: str = ""
    estimated: list = field(default_factory=list)   # which cost components were estimated
    lease_years: Optional[int] = None
    council_tax: Optional[str] = None
    floor: Optional[str] = None

    # filled by costs.compute / enrich
    deposit: Optional[float] = None
    mortgage: Optional[float] = None
    monthly_cost: Optional[float] = None
    travel_minutes: Optional[int] = None
    route: Optional[str] = None       # e.g. "Overground → Elizabeth line"
    station: Optional[str] = None     # first station boarded
    walk_minutes: Optional[int] = None
    crime_score: Optional[int] = None
    crime_percentile: Optional[float] = None

    @property
    def key(self):
        return f"{self.source}:{self.id}"

    @property
    def short_id(self):
        """Stable id that fits in Telegram callback_data (64 bytes)."""
        return hashlib.sha1(self.key.encode()).hexdigest()[:10]

    @property
    def loan(self):
        """Mortgage amount to request: share price minus deposit."""
        if self.tenure != "sale" or self.share_price is None or self.deposit is None:
            return None
        return max(self.share_price - self.deposit, 0)

    @property
    def warnings(self):
        w = []
        if (self.service_charge or 0) > config.HIGH_SERVICE_CHARGE:
            w.append("High SC" + (" (est.)" if "service charge" in self.estimated else ""))
        if self.lease_years is not None and self.lease_years < config.SHORT_LEASE_YEARS:
            w.append(f"short lease {self.lease_years} yrs")
        return w

    @property
    def price_per_m2(self):
        return self.full_value / self.floor_area_m2 if self.full_value and self.floor_area_m2 else None

    @property
    def postcode(self):
        m = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})?\b", self.address.upper())
        return m.group(0).replace(" ", "") if m else ""

    @property
    def postcode_full(self):
        m = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", self.address.upper())
        return f"{m.group(1)} {m.group(2)}" if m else ""

    def excluded_by_keywords(self):
        text = f"{self.address} {self.scheme} {self.description}".lower()
        return any(k in text for k in config.EXCLUDE_KEYWORDS)

    def to_dict(self):
        """Plain data for the map page."""
        return {
            "key": self.key, "source": self.source, "url": self.url, "address": self.address,
            "beds": self.bedrooms, "scheme": self.scheme, "tenure": self.tenure,
            "cost": round(self.monthly_cost), "mortgage": round(self.mortgage or 0),
            "rent": round(self.rent or 0), "service": round(self.service_charge or 0),
            "share_price": self.share_price, "share_pct": self.share_pct, "full_value": self.full_value,
            "deposit": self.deposit, "loan": self.loan, "travel": self.travel_minutes,
            "route": self.route, "station": self.station, "crime": self.crime_score,
            "lat": self.lat, "lng": self.lng, "image": self.image, "estimated": self.estimated,
            "lease": self.lease_years, "council_tax": self.council_tax, "floor": self.floor,
            "warnings": self.warnings,
            "floorplan": self.floorplan, "area_m2": self.floor_area_m2, "price_per_m2": self.price_per_m2,
            "walk": self.walk_minutes, "crime_percentile": self.crime_percentile,
        }
