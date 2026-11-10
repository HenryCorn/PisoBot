"""Council tax for private rentals: the borough's Band D times the statutory band ratio.

Source: Ministry of Housing, Communities and Local Government, "Council Tax levels set by local
authorities in England 2026 to 2027" (accredited official statistics, published 25 March 2026),
https://www.gov.uk/government/statistics/council-tax-levels-set-by-local-authorities-in-england-2026-to-2027
- Table 9, "Area council tax for a dwelling occupied by two adults by band", column Band D: the
  billing authority's charge plus the GLA precept (and any parish precept), £ per year.
- Table 7, "London area" column, 2026 to 2027: £2,068.47, the official London average Band D.
Year of the figures: 2026-27. Refresh both from the March 2027 release.
Every London billing authority in Table 9 is listed; a borough that cannot be identified from an
advert is not guessed and gets the labelled London-wide figure instead.
"""
import re

SOURCE = "MHCLG Council Tax levels set by local authorities in England 2026 to 2027, Tables 7 and 9"
YEAR = "2026-27"

BAND_D = {
    "Barking and Dagenham": 2198.51,
    "Barnet": 2132.6,
    "Bexley": 2366.36,
    "Brent": 2235.27,
    "Bromley": 2140.04,
    "Camden": 2207.55,
    "City of London": 1329.56,
    "Croydon": 2599.91,
    "Ealing": 2138.53,
    "Enfield": 2267.67,
    "Greenwich": 2107.86,
    "Hackney": 2060.3,
    "Hammersmith and Fulham": 1519.51,
    "Haringey": 2313.78,
    "Harrow": 2511.07,
    "Havering": 2424.66,
    "Hillingdon": 2045.46,
    "Hounslow": 2185.56,
    "Islington": 2108.15,
    "Kensington and Chelsea": 1666.65,
    "Kingston upon Thames": 2609.2,
    "Lambeth": 2047.11,
    "Lewisham": 2237.33,
    "Merton": 2146.76,
    "Newham": 1944.23,
    "Redbridge": 2294.58,
    "Richmond upon Thames": 2486.1,
    "Southwark": 1967.26,
    "Sutton": 2378.64,
    "Tower Hamlets": 1837.78,
    "Waltham Forest": 2386.96,
    "Wandsworth": 1028.21,
    "Westminster": 1049.55,
}
LONDON_BAND_D = 2068.47      # Table 7, London area average Band D
FALLBACK_BAND = "C"

# Statutory ratios to Band D (Local Government Finance Act 1992, s.5)
BAND_RATIO = {"A": 6 / 9, "B": 7 / 9, "C": 8 / 9, "D": 1.0, "E": 11 / 9, "F": 13 / 9, "G": 15 / 9, "H": 18 / 9}

# Words in an advert address that name a borough unambiguously (longest first when matching)
_ALIASES = {"Hammersmith": "Hammersmith and Fulham", "Fulham": "Hammersmith and Fulham",
            "Kensington": "Kensington and Chelsea", "Chelsea": "Kensington and Chelsea",
            "Richmond": "Richmond upon Thames", "Kingston": "Kingston upon Thames",
            "Barking": "Barking and Dagenham", "Dagenham": "Barking and Dagenham"}


def band_from_text(text):
    """'Council Tax Band: C' / 'council tax band is C' in an advert, else None."""
    m = re.search(r"\bcouncil\s+tax\s+band\s*(?:is|:|-|=)?\s*([A-H])\b", text or "", re.I)
    return m[1].upper() if m else None


def borough(address, outcode=""):
    """A borough named by a whole comma-separated part of the address ("..., Hackney, London E5",
    never a street such as "Camden Road"), else fixed by the outcode, else None (never a guess)."""
    from .intelligence import OUTCODE_TO_BOROUGH
    lookup = {name.lower(): name for name in BAND_D}
    lookup.update({alias.lower(): name for alias, name in _ALIASES.items()})
    for part in str(address or "").split(","):
        part = re.sub(r"^(?:the\s+)?(?:royal\s+)?(?:london\s+)?borough\s+of\s+", "", part.strip().lower())
        if part in lookup:
            return lookup[part]
    named = OUTCODE_TO_BOROUGH.get(outcode)
    return named if named in BAND_D else None


def monthly(band, borough_name):
    """(£ per month, label, exact). Unknown band or borough falls back to the London-wide
    average, band C when no band is known; the label says what was assumed."""
    band = band.upper() if isinstance(band, str) and band.upper() in BAND_RATIO else None
    band_d = BAND_D.get(borough_name)
    exact = band is not None and band_d is not None
    used = band or FALLBACK_BAND
    amount = (band_d if band_d is not None else LONDON_BAND_D) * BAND_RATIO[used] / 12
    if exact:
        label = f"council tax {used}"
    elif band:
        label = f"council tax {used}, London average"
    else:
        label = f"council tax ~{used}, London average"
    return round(amount), label, exact
