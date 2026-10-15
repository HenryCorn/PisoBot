"""Search criteria. Every value can be overridden with an environment variable of the same name."""
import os


def _env(name, default, cast=float):
    raw = os.environ.get(name)
    return cast(raw) if raw not in (None, "") else default


# Target: Oxford Circus station
TARGET_NAME = "Oxford Circus"
TARGET_LAT = 51.5152
TARGET_LNG = -0.1419

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

# "Best" ranking for the report: each term is scaled 0-1 against its cap, lower is better
RANK_WEIGHTS = {"cost": 0.45, "travel": 0.30, "crime": 0.25}
RANK_DEPOSIT_PENALTY = 0.10     # deposit above PREFERRED_DEPOSIT
RANK_ESTIMATE_PENALTY = 0.05    # some costs were estimated, not published
RANK_EXTRA_BED_BONUS = 0.05     # per bedroom above MIN_BEDROOMS
REPORT_TOP_N = _env("REPORT_TOP_N", 10, int)
REPORT_MAX_PER_DEVELOPMENT = 2  # keep the top 10 varied (no 10 plots of one building)

MAX_ALERTS_PER_RUN = _env("MAX_ALERTS_PER_RUN", 40, int)
PRICE_DROP_ALERT = _env("PRICE_DROP_ALERT", 25)     # re-alert when monthly cost falls by this much

# Warnings shown on listings
HIGH_SERVICE_CHARGE = 250       # £/month; strictly greater triggers a warning
SHORT_LEASE_YEARS = 90

# Notifications between these UK hours are delivered silently
QUIET_HOURS = (22, 7)

# Interactive map published with GitHub Pages from /docs
MAP_URL = os.environ.get("MAP_URL", "https://henrycorn.github.io/PisoBot/")

EXCLUDE_KEYWORDS = [
    "studio", "over 55", "over-55", "over 55s", "older person", "retirement",
    "room in", "flat share", "flatshare", "house share", "houseshare", "auction",
    "coming soon", "under offer", "sold stc", "sstc", "reserved",
    "sheltered", "care staff", "extra care", "warden", "over 60", "aged 60", "aged 55",
]
