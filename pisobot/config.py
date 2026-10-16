"""Search criteria. Every value can be overridden with an environment variable of the same name."""
import os


def _load_dotenv(path=os.path.join(os.path.dirname(__file__), "..", ".env")):
    """Local secrets (e.g. DECODO_AUTH_TOKEN) from a git-ignored .env; real env vars win."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        name, sep, value = line.strip().partition("=")
        if sep and name and not name.startswith("#"):
            os.environ.setdefault(name.strip(), value.strip().strip("'\""))


_load_dotenv()


def _env(name, default, cast=float):
    raw = os.environ.get(name)
    return cast(raw) if raw not in (None, "") else default


# Target: Oxford Circus station
TARGET_NAME = "Oxford Circus"
TARGET_LAT = 51.5152
TARGET_LNG = -0.1419

# Hard filters
MAX_MONTHLY_COST = _env("MAX_MONTHLY_COST", 1600)   # mortgage + rent + service charge (private rent: + bills)
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

# Private rentals (Rightmove / OnTheMarket / Zoopla lettings) run only on demand: /rentals in
# Telegram (main.py --rentals). Portals block cloud runners, so they go through Decodo when
# DECODO_AUTH_TOKEN is set (~25 pages per search plus details pages for new candidates; the free
# plan is 2,000 requests). A repeat /rentals within RENT_REFRESH_HOURS reuses the last search.
RENT_BILLS_ESTIMATE = _env("RENT_BILLS_ESTIMATE", 280)  # council tax + energy + water + broadband
RENT_COUNCIL_TAX_ESTIMATE = _env("RENT_COUNCIL_TAX_ESTIMATE", 150)  # "bills included except council tax"
RENT_MIN = _env("RENT_MIN", 1000)   # cheaper "1 bed" lets in zones 1-3 are rooms in shared houses
RENT_MIN_AREA_M2 = _env("RENT_MIN_AREA_M2", 30)  # London Plan 1-bed minimum is 37 m²; smaller = room/bedsit
RENT_DEPOSIT_WEEKS = 5                                  # Tenant Fees Act cap below £50k/yr rent
RENT_SEARCH_MILES = _env("RENT_SEARCH_MILES", 5)       # portals accept 1/3/5/10/15 miles
RENT_REFRESH_HOURS = _env("RENT_REFRESH_HOURS", 2)
RENT_MAX_PAGES = _env("RENT_MAX_PAGES", 20, int)

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
    "short let", "short-let", "holiday let", "students only", "student accommodation",
    "room only", "shared house", "shared flat", "multiple occupation", "room to rent", "double room",
    "single room", "sharing with", "shared kitchen", "shared bathroom", "communal bathroom",
    "short term let",
    "sheltered", "care staff", "extra care", "warden", "over 60", "aged 60", "aged 55",
]
