"""Free-tier property intelligence: red-flag traps, lifestyle vibes, deal-hunting history,
opportunity-cost savings vs private rent, and park/green-space proximity."""
import datetime as dt
import math
import re

# Benchmark median monthly private rents (Greater London Authority / ONS rental data)
BOROUGH_MEDIAN_RENTS = {
    "Barking and Dagenham": {1: 1250, 2: 1500, 3: 1750},
    "Barnet": {1: 1450, 2: 1800, 3: 2200},
    "Bexley": {1: 1150, 2: 1400, 3: 1650},
    "Brent": {1: 1550, 2: 1950, 3: 2400},
    "Bromley": {1: 1250, 2: 1550, 3: 1850},
    "Camden": {1: 2000, 2: 2600, 3: 3400},
    "City of London": {1: 2200, 2: 2900, 3: 3800},
    "Croydon": {1: 1200, 2: 1500, 3: 1800},
    "Ealing": {1: 1500, 2: 1850, 3: 2250},
    "Enfield": {1: 1300, 2: 1600, 3: 1950},
    "Greenwich": {1: 1550, 2: 1900, 3: 2300},
    "Hackney": {1: 1800, 2: 2300, 3: 2850},
    "Hammersmith and Fulham": {1: 1950, 2: 2500, 3: 3200},
    "Haringey": {1: 1550, 2: 1950, 3: 2400},
    "Harrow": {1: 1350, 2: 1650, 3: 2000},
    "Havering": {1: 1200, 2: 1450, 3: 1750},
    "Hillingdon": {1: 1300, 2: 1600, 3: 1950},
    "Hounslow": {1: 1400, 2: 1750, 3: 2150},
    "Islington": {1: 1900, 2: 2450, 3: 3100},
    "Kensington and Chelsea": {1: 2400, 2: 3200, 3: 4200},
    "Kingston upon Thames": {1: 1400, 2: 1750, 3: 2150},
    "Lambeth": {1: 1750, 2: 2250, 3: 2800},
    "Lewisham": {1: 1450, 2: 1800, 3: 2200},
    "Merton": {1: 1450, 2: 1800, 3: 2250},
    "Newham": {1: 1600, 2: 1950, 3: 2350},
    "Redbridge": {1: 1300, 2: 1600, 3: 1950},
    "Richmond upon Thames": {1: 1650, 2: 2100, 3: 2600},
    "Southwark": {1: 1850, 2: 2350, 3: 2950},
    "Sutton": {1: 1200, 2: 1500, 3: 1800},
    "Tower Hamlets": {1: 1950, 2: 2450, 3: 3000},
    "Waltham Forest": {1: 1450, 2: 1750, 3: 2150},
    "Wandsworth": {1: 1800, 2: 2300, 3: 2850},
    "Westminster": {1: 2400, 2: 3200, 3: 4200},
}

OUTCODE_TO_BOROUGH = {
    "E1": "Tower Hamlets", "E2": "Tower Hamlets", "E3": "Tower Hamlets", "E5": "Hackney",
    "E6": "Newham", "E7": "Newham", "E8": "Hackney", "E9": "Hackney", "E13": "Newham",
    "E14": "Tower Hamlets", "E15": "Newham", "E16": "Newham", "E17": "Waltham Forest",
    "E20": "Newham", "EC1": "Islington", "EC2": "City of London", "EC3": "City of London",
    "EC4": "City of London", "N1": "Islington", "N4": "Haringey", "N5": "Islington",
    "N7": "Islington", "N8": "Haringey", "N15": "Haringey", "N16": "Hackney", "N17": "Haringey",
    "NW1": "Camden", "NW3": "Camden", "NW5": "Camden", "NW6": "Camden", "NW8": "Westminster",
    "NW10": "Brent", "SE1": "Southwark", "SE5": "Southwark", "SE8": "Lewisham", "SE10": "Greenwich",
    "SE13": "Lewisham", "SE14": "Lewisham", "SE15": "Southwark", "SE16": "Southwark",
    "SE17": "Southwark", "SE18": "Greenwich", "SW1": "Westminster", "SW4": "Lambeth",
    "SW8": "Lambeth", "SW9": "Lambeth", "SW11": "Wandsworth", "W1": "Westminster",
    "W2": "Westminster", "W6": "Hammersmith and Fulham", "W12": "Hammersmith and Fulham",
    "W14": "Hammersmith and Fulham", "WC1": "Camden", "WC2": "Westminster",
}

# Major London parks and commons (lat, lng, name)
MAJOR_PARKS = [
    (51.5430, -0.0160, "Olympic Park"),
    (51.5360, -0.0380, "Victoria Park"),
    (51.4760, 0.0000, "Greenwich Park"),
    (51.5310, -0.1570, "Regent's Park"),
    (51.5070, -0.1690, "Hyde Park"),
    (51.4790, -0.1570, "Battersea Park"),
    (51.5600, -0.1650, "Hampstead Heath"),
    (51.4840, -0.0820, "Burgess Park"),
    (51.5410, -0.0570, "London Fields"),
    (51.5590, -0.0860, "Clissold Park"),
    (51.5710, -0.1060, "Finsbury Park"),
    (51.5030, 0.0350, "Thames Barrier Park"),
    (51.4900, -0.0090, "Millwall Park"),
    (51.4950, -0.0560, "Southwark Park"),
    (51.4510, -0.1110, "Brockwell Park"),
    (51.4580, -0.1480, "Clapham Common"),
    (51.5490, -0.1010, "Highbury Fields"),
    (51.5830, -0.0560, "Walthamstow Wetlands"),
]


def detect_red_flags(text):
    """Identifies common property traps, auction conditions, and hidden dealbreakers."""
    blob = text or ""
    flags = []
    patterns = [
        (r"\b(?:cash\s+buyers?\s+only|unmortgageable|for\s+sale\s+by\s+auction|"
         r"modern\s+method\s+of\s+auction|auction\s+sale)\b", "⚠️ Auction / Cash only"),
        (r"\b(?:above\s+(?:a\s+)?(?:takeaway|shop|commercial|restaurant|pub|bar|retail))\b",
         "⚠️ Above commercial"),
        (r"\b(?:(?:needs|in\s+need\s+of)\s+(?:full\s+)?(?:modernis[ea]tion|refurbishment)|"
         r"needs\s+tlc|structural\s+movement|subsidence|underpinning|damp\s+issues?)\b", "⚠️ Needs refurbishment"),
        (r"\b(?:doubling\s+ground\s+rent|escalating\s+ground\s+rent)\b", "⚠️ Escalating ground rent"),
        (r"\b(?:waking\s+watch|remediation\s+costs?\s+payable)\b", "⚠️ Waking watch / remediation"),
        (r"\b(?:sold\s+with\s+tenant|sitting\s+tenant|shared\s+(?:kitchen|bathroom))\b",
         "⚠️ Tenant / shared facilities"),
    ]
    for pattern, label in patterns:
        if re.search(pattern, blob, re.I):
            flags.append(label)
    return flags


def detect_vibes(text):
    """Semantic lifestyle matching: bright, modern, leafy, spacious, waterside."""
    blob = text or ""
    vibes = []
    categories = [
        (r"\b(?:sun[\s-]drenched|bathed\s+in\s+light|abundan(?:ce|t)\s+(?:of\s+)?natural\s+light|"
         r"floor[\s-]to[\s-]ceiling\s+windows?|dual[\s-]aspect|bright\s+and\s+airy)\b", "☀️ Bright & Airy"),
        (r"\b(?:quiet\s+residential|peaceful|leafy|tree[\s-]lined|residential\s+cul[\s-]de[\s-]sac)\b",
         "🌿 Quiet & Leafy"),
        (r"\b(?:high[\s-]spec(?:ification)?|modern\s+kitchen|integrated\s+appliances|"
         r"contemporary\s+finish|recently\s+refurbished|sleek|quartz)\b", "✨ Modern finish"),
        (r"\b(?:generous\s+(?:proportions|size|double)|spacious\s+open|work\s+from\s+home|"
         r"dedicated\s+workspace|ample\s+storage)\b", "📐 Spacious"),
        (r"\b(?:canal[\s-]side|river[\s-]side|dock[\s-]side|waterside|panoramic\s+views?|"
         r"stunning\s+views?|overlooking\s+(?:the\s+)?(?:river|canal|dock|water))\b", "🌊 Waterside / Views"),
    ]
    for pattern, label in categories:
        if re.search(pattern, blob, re.I):
            vibes.append(label)
    return vibes


def opportunity_cost_savings(listing):
    """Compares total monthly cost to the local borough median private rent for the same bed count."""
    if not listing.monthly_cost:
        return None
    borough = OUTCODE_TO_BOROUGH.get(listing.outcode)
    beds = max(1, min(listing.bedrooms or 1, 3))
    median = None
    if borough and borough in BOROUGH_MEDIAN_RENTS:
        median = BOROUGH_MEDIAN_RENTS[borough].get(beds)
    if not median:
        # Fallback by postal region
        out = listing.outcode or ""
        if out.startswith(("EC", "WC")):
            median = {1: 2100, 2: 2700, 3: 3500}.get(beds, 2100)
        elif out.startswith(("E", "N", "NW", "SE", "SW", "W")):
            median = {1: 1700, 2: 2150, 3: 2700}.get(beds, 1700)
        else:
            median = {1: 1350, 2: 1650, 3: 2000}.get(beds, 1350)
    savings = round(median - listing.monthly_cost)
    return savings if savings > 0 else None


def nearby_green_spaces(lat, lng, max_dist_m=600):
    """Returns nearest major London park within max_dist_m if applicable."""
    if lat is None or lng is None:
        return []
    nearby = []
    for p_lat, p_lng, name in MAJOR_PARKS:
        # Haversine distance approximation for London latitude (~51.5)
        dy = (lat - p_lat) * 111200
        dx = (lng - p_lng) * 111200 * math.cos(math.radians(lat))
        dist = math.hypot(dx, dy)
        if dist <= max_dist_m:
            nearby.append(f"🌳 {int(round(dist, -1))}m to {name}")
    return nearby[:1]


def deal_hunter_signals(listing, seen_record, now=None):
    """Calculates days on market and price drop momentum for negotiation leverage."""
    now = now or dt.datetime.now()
    if not seen_record:
        return None
    first_iso = seen_record.get("first")
    if not first_iso:
        return None
    try:
        first_dt = dt.datetime.fromisoformat(first_iso[:16])
        days = (now - first_dt).days
    except (ValueError, TypeError):
        return None
    prev_cost = seen_record.get("cost")
    if prev_cost and listing.monthly_cost and (prev_cost - listing.monthly_cost) >= 30:
        drop = round(prev_cost - listing.monthly_cost)
        return f"🏷️ Price cut (-£{drop}/mo)"
    if days >= 45:
        return f"⏳ {days}d on market (Negotiation potential)"
    return None
