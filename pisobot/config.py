"""Search criteria. Every value can be overridden with an environment variable of the same name."""
import os


def _env(name, default, cast=float):
    raw = os.environ.get(name)
    return cast(raw) if raw not in (None, "") else default


# Target: Shoreditch High Street station
TARGET_NAME = "Shoreditch High St"
TARGET_LAT = 51.5233
TARGET_LNG = -0.0752

# Hard filters
MAX_MONTHLY_COST = _env("MAX_MONTHLY_COST", 1600)   # mortgage + rent + service charge
MAX_DEPOSIT = _env("MAX_DEPOSIT", 20000)
PREFERRED_DEPOSIT = _env("PREFERRED_DEPOSIT", 10000)
MIN_BEDROOMS = _env("MIN_BEDROOMS", 1, int)
MAX_TRAVEL_MINUTES = _env("MAX_TRAVEL_MINUTES", 40, int)
SEARCH_RADIUS_KM = _env("SEARCH_RADIUS_KM", 12)     # cheap pre-filter before calling TfL

# "Safety" crimes (violence, robbery, burglary, theft from person, weapons, arson/damage,
# drugs, public order) per month within CRIME_RADIUS_M. London town centres are 150-250,
# quiet residential pockets 30-90.
MAX_CRIME_SCORE = _env("MAX_CRIME_SCORE", 120, int)
CRIME_RADIUS_M = _env("CRIME_RADIUS_M", 500, int)

# Mortgage assumptions used to recompute monthly costs consistently across sources
MORTGAGE_RATE = _env("MORTGAGE_RATE", 5.0)          # % per year
MORTGAGE_YEARS = _env("MORTGAGE_YEARS", 30, int)
# Used only when a source does not publish rent / service charge
SO_RENT_RATE = _env("SO_RENT_RATE", 2.75)           # % per year on the unowned share
DEFAULT_SERVICE_CHARGE = _env("DEFAULT_SERVICE_CHARGE", 250)
# Conservative full-value floor when a listing doesn't state its share %
MIN_FULL_VALUE_BY_BEDS = {1: 350000, 2: 450000, 3: 550000}

MAX_ALERTS_PER_RUN = _env("MAX_ALERTS_PER_RUN", 40, int)
PRICE_DROP_ALERT = _env("PRICE_DROP_ALERT", 25)     # re-alert when monthly cost falls by this much

EXCLUDE_KEYWORDS = [
    "studio", "over 55", "over-55", "over 55s", "older person", "retirement",
    "room in", "flat share", "flatshare", "house share", "houseshare", "auction",
    "coming soon", "under offer", "sold stc", "sstc", "reserved",
    "sheltered", "care staff", "extra care", "warden", "over 60", "aged 60", "aged 55",
]
