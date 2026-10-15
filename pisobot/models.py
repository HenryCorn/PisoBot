import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

from . import config

LONDON_AREAS = {
    "E1": "Whitechapel / Stepney", "E2": "Bethnal Green", "E3": "Bow", "E4": "Chingford",
    "E5": "Clapton", "E6": "East Ham", "E7": "Forest Gate", "E8": "Dalston / Hackney",
    "E9": "Homerton / Hackney Wick", "E10": "Leyton", "E11": "Leytonstone", "E12": "Manor Park",
    "E13": "Plaistow", "E14": "Poplar / Isle of Dogs", "E15": "Stratford",
    "E16": "Canning Town / Custom House", "E17": "Walthamstow", "E18": "South Woodford",
    "E20": "Olympic Park", "EC1": "Clerkenwell", "EC2": "Moorgate",
    "EC3": "City / Aldgate", "EC4": "Blackfriars",
    "N1": "Islington / Hoxton", "N1C": "King's Cross", "N2": "East Finchley", "N3": "Finchley",
    "N4": "Finsbury Park", "N5": "Highbury", "N6": "Highgate", "N7": "Holloway",
    "N8": "Hornsey / Crouch End", "N9": "Edmonton", "N10": "Muswell Hill",
    "N11": "New Southgate", "N12": "North Finchley", "N13": "Palmers Green", "N14": "Southgate",
    "N15": "Seven Sisters / Tottenham", "N16": "Stoke Newington", "N17": "Tottenham",
    "N18": "Upper Edmonton", "N19": "Archway", "N20": "Whetstone", "N21": "Winchmore Hill", "N22": "Wood Green",
    "NW1": "Camden / Marylebone", "NW2": "Cricklewood", "NW3": "Hampstead", "NW4": "Hendon",
    "NW5": "Kentish Town", "NW6": "Kilburn / West Hampstead", "NW7": "Mill Hill",
    "NW8": "St John's Wood", "NW9": "Colindale", "NW10": "Willesden", "NW11": "Golders Green",
    "SE1": "Waterloo / Bermondsey", "SE2": "Abbey Wood", "SE3": "Blackheath", "SE4": "Brockley",
    "SE5": "Camberwell", "SE6": "Catford", "SE7": "Charlton", "SE8": "Deptford", "SE9": "Eltham",
    "SE10": "Greenwich", "SE11": "Kennington", "SE12": "Lee", "SE13": "Lewisham",
    "SE14": "New Cross", "SE15": "Peckham", "SE16": "Rotherhithe / Surrey Quays", "SE17": "Walworth",
    "SE18": "Woolwich / Plumstead", "SE19": "Crystal Palace", "SE20": "Penge", "SE21": "Dulwich",
    "SE22": "East Dulwich", "SE23": "Forest Hill", "SE24": "Herne Hill",
    "SE25": "South Norwood", "SE26": "Sydenham", "SE27": "West Norwood", "SE28": "Thamesmead",
    "SW1": "Westminster", "SW2": "Brixton", "SW3": "Chelsea", "SW4": "Clapham", "SW5": "Earl's Court",
    "SW6": "Fulham", "SW7": "South Kensington", "SW8": "Vauxhall", "SW9": "Stockwell",
    "SW10": "West Brompton", "SW11": "Battersea", "SW12": "Balham", "SW13": "Barnes",
    "SW14": "Mortlake / East Sheen", "SW15": "Putney", "SW16": "Streatham", "SW17": "Tooting",
    "SW18": "Wandsworth", "SW19": "Wimbledon", "SW20": "Raynes Park",
    "W1": "West End", "W2": "Paddington / Bayswater", "W3": "Acton", "W4": "Chiswick",
    "W5": "Ealing", "W6": "Hammersmith", "W7": "Hanwell", "W8": "Kensington", "W9": "Maida Vale",
    "W10": "North Kensington", "W11": "Notting Hill", "W12": "Shepherd's Bush",
    "W13": "West Ealing", "W14": "West Kensington", "WC1": "Bloomsbury", "WC2": "Covent Garden / Holborn",
    "HA0": "Wembley", "HA1": "Harrow", "HA9": "Wembley Park", "IG1": "Ilford", "IG2": "Gants Hill",
    "IG3": "Seven Kings", "IG4": "Redbridge", "IG5": "Clayhall", "IG6": "Barkingside", "IG11": "Barking",
    "TW1": "Twickenham", "TW3": "Hounslow", "TW7": "Isleworth", "TW8": "Brentford", "TW9": "Richmond",
    "UB1": "Southall", "UB2": "Southall", "UB3": "Hayes", "UB5": "Northolt", "UB6": "Greenford",
    "BR1": "Bromley", "BR3": "Beckenham", "CR0": "Croydon", "CR4": "Mitcham",
    "EN1": "Enfield", "EN2": "Enfield Chase", "KT1": "Kingston", "SM1": "Sutton", "SM4": "Morden",
}


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

    # filled by intelligence / taste
    changes: int = 0
    match_score: Optional[int] = None
    match_reasons: list = field(default_factory=list)
    deal_tag: Optional[str] = None
    rent_savings: Optional[int] = None
    red_flags: list = field(default_factory=list)
    vibes: list = field(default_factory=list)
    livability_tags: list = field(default_factory=list)

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
    def commute_friction(self):
        """Quality-weighted commute: penalty for tube/rail changes and excessive walking."""
        if self.travel_minutes is None:
            return None
        walk_penalty = max(0, (self.walk_minutes or 0) - 8) * 0.4
        changes_penalty = (self.changes or 0) * 6.0
        return self.travel_minutes + walk_penalty + changes_penalty

    @property
    def warnings(self):
        w = []
        if (self.service_charge or 0) > config.HIGH_SERVICE_CHARGE:
            w.append("High SC" + (" (est.)" if "service charge" in self.estimated else ""))
        if self.lease_years is not None and self.lease_years < config.SHORT_LEASE_YEARS:
            w.append(f"short lease {self.lease_years} yrs")
        for flag in self.red_flags:
            if flag not in w:
                w.append(flag)
        return w

    @property
    def price_per_m2(self):
        return self.full_value / self.floor_area_m2 if self.full_value and self.floor_area_m2 else None

    @property
    def feature_tags(self):
        blob = self.description
        tags = []
        for pattern, label in ((r"\bbalcony\b", "🌿 Balcony"), (r"\bterrace\b", "🌿 Terrace"),
                                (r"\bparking\b", "🅿️ Parking"), (r"\b(?:lift|elevator)\b", "🛗 Lift")):
            for match in re.finditer(pattern, blob, re.I):
                before = blob[max(0, match.start() - 40):match.start()]
                after = blob[match.end():match.end() + 35]
                if not re.search(r"\b(?:no|without|not)\b[^.;!?]{0,35}$", before, re.I) \
                        and not re.match(r"\s+(?:is\s+)?(?:not|unavailable|excluded)\b", after, re.I):
                    tags.append(label)
                    break
        if re.search(r"\b(?:EWS1(?:\s+(?:form|certificate|rating))?\s*[:\-]?\s*"
                     r"(?:available|approved|compliant|passed|provided|A[12]|B1)|"
                     r"(?:valid|satisfactory|approved|compliant|[AB]1\s+rated)\s+EWS1)\b", blob, re.I) \
                and not re.search(r"\bEWS1[^.;!?]{0,30}\b(?:not|pending|required|awaiting|B2)\b"
                                  r"|\bB2\s+(?:rated\s+)?EWS1\b"
                                  r"|\b(?:no|without)\s+(?:(?:valid|approved|satisfactory|compliant)\s+)?EWS1\b",
                                  blob, re.I):
            tags.append("✅ EWS1")
        if re.search(r"\bEWS1[^.;!?]{0,30}\bB2\b|\bB2\s+(?:rated\s+)?EWS1\b", blob, re.I):
            tags.append("⚠️ EWS1 B2")
        if re.search(r"\bcladding\b", blob, re.I):
            tags.append("⚠️ Cladding mentioned")
        if re.search(r"\b(?:no\s+pets|pets\s+(?:are\s+)?not\s+(?:allowed|permitted))\b", blob, re.I):
            tags.append("⚠️ Pets not allowed")
        for v in self.vibes:
            if v not in tags:
                tags.append(v)
        for lt in self.livability_tags:
            if lt not in tags:
                tags.append(lt)
        return tags

    @property
    def postcode(self):
        m = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})?\b", self.address.upper())
        return m.group(0).replace(" ", "") if m else ""

    @property
    def postcode_full(self):
        m = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", self.address.upper())
        return f"{m.group(1)} {m.group(2)}" if m else ""

    @property
    def outcode(self):
        m = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?)(?:\s*\d[A-Z]{2})?\b", self.address.upper())
        return m.group(1) if m else ""

    @property
    def area_name(self):
        return LONDON_AREAS.get(self.outcode) or LONDON_AREAS.get(re.sub(r"[A-Z]$", "", self.outcode), "")

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
            "area": self.area_name, "features": self.feature_tags,
            "match_score": self.match_score, "deal_tag": self.deal_tag,
            "savings": self.rent_savings,
        }
