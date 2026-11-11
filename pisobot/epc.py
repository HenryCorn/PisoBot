"""Floor area, energy band and running cost from Energy Performance Certificates (G2).

OFF unless EPC_API_TOKEN (or EPC_TOKEN) is set (and STORAGE=local, where the SQLite cache lives). On any error the
advert is left exactly as it was: nothing here rejects, hides or reorders a flat.

Data source: the official "Get energy performance of buildings data" service
(https://api.get-energy-performance-data.communities.gov.uk, GOV.UK One Login account, bearer token).
It replaces the older epc.opendatacommunities.org API, whose docs now redirect there. Two request
kinds are used:
    GET /api/domestic/search?postcode=N7+6DJ&page_size=5000   summaries only (address, band, date, uprn)
    GET /api/certificate?certificate_number=....              one certificate: area, rooms, heating, costs
so a postcode costs 1 search plus one request per certificate that has to be read. Certificates are cached
for EPC_CACHE_DAYS, requests are capped per UTC day and nothing is ever guessed: a result is "exact"
(the advert's flat/house matches one certificate), "range" (several certificates of a compatible size agree
closely) or "none".

Address data in certificates is personal data with licence conditions (see the G2 report). Only fields needed
for matching are stored, for at most EPC_CACHE_DAYS, and rows past that age are deleted.
"""
import contextlib
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import statistics
import threading
import time

from . import budget, config, factsdb

log = logging.getLogger(__name__)

API = "https://api.get-energy-performance-data.communities.gov.uk"
CACHE_DAYS = config._env("EPC_CACHE_DAYS", 30)
DAILY_LIMIT = config._env("EPC_DAILY_LIMIT", 1500, int)         # API requests per UTC day (search + fetch)
MAX_CERTS_PER_POSTCODE = config._env("EPC_MAX_CERTS", 60, int)  # certificates read to compare one postcode
PAUSE_SECONDS = config._env("EPC_PAUSE_SECONDS", 0.25)
TIMEOUT = 20
BUSY_SECONDS = 0.25              # every write in the optional post-alert step waits at most this long, then is skipped
MAX_RESPONSE_BYTES = 5_000_000   # an API response larger than this is refused (read streamed, never buffered whole)
MAX_CERT_AGE_YEARS = 12          # a certificate is valid for ten years; older ones are not trusted
RANGE_MIN_CANDIDATES = 2
RANGE_TOLERANCE_ABS = 5.0        # m2: spread (max - min) allowed ...
RANGE_TOLERANCE_REL = 0.12       # ... or this share of the median, whichever is larger
MIN_COMPARABLES = 8              # adverts with an area needed before a percentile is shown

DDL = """
CREATE TABLE IF NOT EXISTS epc_search (
    postcode TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, records TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS epc_cert (
    number TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, record TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS epc_usage (
    day TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS epc_area_facts (
    key TEXT PRIMARY KEY, outcode TEXT NOT NULL, bedrooms INTEGER NOT NULL, rent REAL NOT NULL,
    area REAL NOT NULL, at TEXT NOT NULL);
"""
RETENTION_TABLES = (("epc_search", "fetched_at"), ("epc_cert", "fetched_at"), ("epc_area_facts", "at"), ("epc_usage", "day"))
PURGE_EVERY = dt.timedelta(hours=1)
AREA_FACT_DAYS = 14              # comparables are portal-stated adverts seen in the last two weeks

_default = None
_lock = threading.Lock()


# ---------------------------------------------------------------- switches

def token():
    return (os.environ.get("EPC_API_TOKEN") or os.environ.get("EPC_TOKEN") or "").strip()


def enabled():
    """Only with credentials present, local storage, and not switched off. The value is never read here
    beyond being non-empty, and never logged."""
    return (bool(token()) and os.environ.get("STORAGE") == "local"
            and os.environ.get("EPC_DISABLE", "") not in ("1", "true", "yes"))


def default():
    global _default
    with _lock:
        if _default is None:
            _default = Epc()
        return _default


def set_default(epc):
    """Install an Epc (tests use fakes). None resets to the lazy default."""
    global _default
    with _lock:
        _default = epc


# ---------------------------------------------------------------- address parsing

POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)
FLAT_RE = re.compile(r"\b(?:flat|apartment|apt|unit|suite)\.?\s*#?\s*([a-z]?\d+[a-z]?|[a-z])\b", re.I)
NUMBER_RE = re.compile(r"^(\d+[a-z]?(?:\s*-\s*\d+[a-z]?)?)\s+(.+)$", re.I)     # '12-14' stays a distinct token
FLAT_LIKE = re.compile(r"\b(?:flat|apartment|apt|maisonette|studio|penthouse)\b", re.I)
GENERIC = {"london", "greater london", "england", "uk", "united kingdom"}


def norm_postcode(value):
    """'n7 6dj' / 'N76DJ' -> 'N7 6DJ'; '' when it is not a full postcode."""
    m = POSTCODE_RE.search(str(value or "").upper())
    return f"{m.group(1)} {m.group(2)}" if m else ""


def _clean(text):
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", text.lower())).strip()


def parse_address(text, post_town=None):
    """{'flat', 'number', 'street', 'building'} from a display address. Every field may be None.
    'Flat 3, 12 Hornsey Road, London N7 6DJ' -> flat '3', number '12', street 'hornsey road'.
    A building name ('Kestrel House') is used only when there is no street number."""
    s = POSTCODE_RE.sub(" ", str(text or ""))
    flat = None
    m = FLAT_RE.search(s)
    if m:
        flat = m.group(1).lower()
        s = s[:m.start()] + "," + s[m.end():]
    town = (post_town or "").strip().lower()
    segments = [seg.strip() for seg in s.split(",")]
    segments = [seg for seg in segments if seg and seg.lower() not in GENERIC and seg.lower() != town
                and not re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?", seg.strip(), re.I)]
    if not flat and segments and re.fullmatch(r"\d+[a-z]?", segments[0], re.I) and len(segments) > 1 \
            and NUMBER_RE.match(segments[1]):
        flat = segments.pop(0).lower()       # "3, 12 Hornsey Road": the lone leading number is the flat
    number = street = building = None
    for i, seg in enumerate(segments):
        m = NUMBER_RE.match(seg)
        if m:
            number, street = re.sub(r"\s+", "", m.group(1).lower()), _clean(m.group(2))
            if i > 0 and not building:
                building = _clean(segments[0])
            break
    if number is None and segments:
        building = _clean(segments[0])
    return {"flat": flat, "number": number, "street": street, "building": building if not number else None}


def _building_key(parsed):
    if parsed["number"] and parsed["street"]:
        return ("n", parsed["number"], parsed["street"])
    if parsed["building"] and len(parsed["building"]) >= 4:
        return ("b", parsed["building"])
    return None


def cert_address(summary):
    return ", ".join(str(summary.get(k) or "") for k in ("addressLine1", "addressLine2", "addressLine3")
                     if summary.get(k))


# ---------------------------------------------------------------- storage

class Store:
    """The EPC tables in the feature's OWN database file, facts.db (or an explicit file, for tests and the evaluation)."""

    def __init__(self, path=None):
        self.path = path

    @contextlib.contextmanager
    def _db(self):
        with factsdb.connect(DDL, self.path) as db:
            yield db

    @staticmethod
    def _fresh(stamp, now):
        try:
            return now - dt.datetime.fromisoformat(stamp) < dt.timedelta(days=CACHE_DAYS)
        except (TypeError, ValueError):
            return False

    def search_get(self, postcode, now):
        with self._db() as db:
            row = db.execute("SELECT fetched_at, records FROM epc_search WHERE postcode = ?", (postcode,)).fetchone()
        return json.loads(row["records"]) if row and self._fresh(row["fetched_at"], now) else None

    def search_put(self, postcode, records, now):
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO epc_search VALUES (?, ?, ?)",
                       (postcode, now.isoformat(), json.dumps(records, sort_keys=True)))

    def cert_get(self, number, now):
        with self._db() as db:
            row = db.execute("SELECT fetched_at, record FROM epc_cert WHERE number = ?", (number,)).fetchone()
        return json.loads(row["record"]) if row and self._fresh(row["fetched_at"], now) else None

    def cert_put(self, number, record, now):
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO epc_cert VALUES (?, ?, ?)",
                       (number, now.isoformat(), json.dumps(record, sort_keys=True)))

    def purge(self, now):
        """Delete every row older than the retention window; {table: rows deleted}."""
        cutoff = (now - dt.timedelta(days=CACHE_DAYS)).isoformat()
        deleted = {}
        with self._db() as db:
            for table, column in RETENTION_TABLES:
                deleted[table] = db.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,)).rowcount
        return deleted

    def requests_today(self, day):
        with self._db() as db:
            row = db.execute("SELECT requests FROM epc_usage WHERE day = ?", (day,)).fetchone()
        return row["requests"] if row else 0

    def reserve(self, day, limit):
        """Atomically count one request against `day`'s cap immediately before it is sent; False when the cap
        is used up. The check and the increment are one UPDATE, so two callers can never both take the last one."""
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO epc_usage (day, requests, failures) VALUES (?, 0, 0)", (day,))
            return db.execute("UPDATE epc_usage SET requests = requests + 1 WHERE day = ? AND requests < ?",
                              (day, limit)).rowcount == 1

    def count_failure(self, day):
        with contextlib.suppress(Exception):
            with self._db() as db:
                db.execute("UPDATE epc_usage SET failures = failures + 1 WHERE day = ?", (day,))

    def note_area(self, key, outcode, bedrooms, rent, area, now):
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO epc_area_facts VALUES (?, ?, ?, ?, ?, ?)",
                       (key, outcode, bedrooms, rent, area, now.isoformat()))

    def area_peers(self, outcode, bedrooms, now, exclude_key=None):
        since = (now - dt.timedelta(days=AREA_FACT_DAYS)).isoformat()
        with self._db() as db:
            rows = db.execute("SELECT key, rent, area FROM epc_area_facts WHERE outcode = ? AND bedrooms = ? "
                              "AND at >= ? AND key != ?", (outcode, bedrooms, since, exclude_key or "")).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- API client

class Unavailable(Exception):
    """Daily cap reached, rate-limited or the service failed: the advert is left without EPC facts."""


def _number(value):
    """A number that the API may send plain or as {'value': n, 'quantity': ...}."""
    if isinstance(value, dict):
        value = value.get("value")
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _text(value):
    if isinstance(value, dict):
        value = value.get("value")
    return str(value).strip() if value not in (None, "") else None


def parse_certificate(payload, number=None):
    """The fields we keep from one certificate (the fetch response, wrapped in 'data' or not)."""
    d = payload.get("data", payload) if isinstance(payload, dict) else {}
    if not isinstance(d, dict):
        return None
    area = _number(d.get("total_floor_area"))
    heating = d.get("main_heating")
    heating = _text(heating[0].get("description")) if isinstance(heating, list) and heating \
        and isinstance(heating[0], dict) else _text(heating)
    costs = [_number(d.get(k)) for k in ("heating_cost_current", "hot_water_cost_current", "lighting_cost_current")]
    label = str(d.get("dwelling_type") or "").lower()
    if label:
        flat = "flat" in label or "maisonette" in label
    else:        # RdSAP property_type codes: 0 house, 1 bungalow, 2 flat, 3 maisonette (from a sample, unverified)
        flat = {0: False, 1: False, 2: True, 3: True}.get(_number(d.get("property_type")))
    return {
        "number": number or _text(d.get("certificate_number")),
        "area": area if area and 8 <= area <= 1000 else None,
        "rooms": _number(d.get("habitable_room_count")),
        "band": _text(d.get("current_energy_efficiency_band")),
        "heating": heating,
        "annual_cost": round(sum(costs), 2) if all(c is not None for c in costs) else None,
        "date": _text(d.get("registration_date")) or _text(d.get("inspection_date")),
        "flat": flat,
        "address": ", ".join(str(d.get(k) or "") for k in ("address_line_1", "address_line_2", "address_line_3")
                              if d.get(k)),
    }


class Epc:
    """Reads certificates through a cache. http(url, params, headers) -> (status, json) is injectable."""

    def __init__(self, http=None, store=None, daily_limit=None, now=None, sleep=time.sleep):
        self.http = http or self._http
        self.store = store or Store()
        self.daily_limit = DAILY_LIMIT if daily_limit is None else daily_limit
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.sleep = sleep
        self.blocked_until = 0.0
        self.requests_made = 0
        self._last_purge = None

    @staticmethod
    def _http(url, params, headers):
        import requests          # plain client: no scraping proxy is needed or wanted for an official API
        started = time.monotonic()
        r = requests.get(url, params=params, headers=headers, timeout=budget.timeout(TIMEOUT), stream=True)
        try:
            chunks, size = [], 0
            for chunk in r.iter_content(65536):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ValueError("response too large")
                if budget.expired() or time.monotonic() - started > TIMEOUT * 2:
                    raise TimeoutError("response too slow")
                chunks.append(chunk)
        finally:
            r.close()
        try:
            body = json.loads(b"".join(chunks))
        except ValueError:
            body = None
        return r.status_code, body

    def _get(self, path, params):
        if time.monotonic() < self.blocked_until:
            raise Unavailable("rate limited")
        if budget.expired():
            raise Unavailable("time budget")
        if self.requests_made:
            self.sleep(min(PAUSE_SECONDS, max(0.0, budget.time_left() or PAUSE_SECONDS)))
        day = self._now().date().isoformat()                 # the day of the request actually sent, not of an earlier step
        if not self.store.reserve(day, self.daily_limit):
            raise Unavailable("daily cap")
        self.requests_made += 1
        try:
            status, body = self.http(API + path, params,
                                     {"Accept": "application/json", "Authorization": f"Bearer {token()}"})
        except Exception as ex:
            self.store.count_failure(day)
            raise Unavailable(type(ex).__name__)
        if status >= 400 and status != 404:
            self.store.count_failure(day)
        if status == 404:
            return None
        if status == 429:
            self.blocked_until = time.monotonic() + 300
            raise Unavailable("429")
        if status != 200 or not isinstance(body, dict):
            raise Unavailable(f"http {status}")
        return body

    def summaries(self, postcode):
        """Latest certificate per address at a postcode (cached). List of summary dicts."""
        now = self._now()
        self.maybe_purge(now)
        cached = self.store.search_get(postcode, now)
        if cached is not None:
            return cached
        rows, page = [], 1
        while page <= 3:
            body = self._get("/api/domestic/search", {"postcode": postcode, "page_size": 5000, "current_page": page})
            if body is None:
                break
            rows += [r for r in body.get("data") or [] if isinstance(r, dict)]
            if not (body.get("pagination") or {}).get("nextPage"):
                break
            page += 1
        latest = {}
        for r in rows:
            key = str(r.get("uprn") or "") or _clean(cert_address(r))
            if key and (key not in latest or str(r.get("registrationDate") or "") > str(latest[key].get("registrationDate") or "")):
                latest[key] = r
        keep = [{k: r.get(k) for k in ("certificateNumber", "addressLine1", "addressLine2", "addressLine3",
                                       "postTown", "currentEnergyEfficiencyBand", "registrationDate", "uprn")}
                for r in latest.values() if r.get("certificateNumber")]
        self.store.search_put(postcode, keep, now)
        return keep

    def maybe_purge(self, now):
        """Retention: delete rows past the window at most hourly, whatever the cache hit rate. A failed purge is
        retried on the next call (the clock only advances on success)."""
        if self._last_purge is not None and now - self._last_purge < PURGE_EVERY:
            return
        try:
            deleted = self.store.purge(now)
        except Exception as ex:
            log.warning("epc: retention purge failed (%s); will retry", type(ex).__name__)
            return
        self._last_purge = now
        if any(deleted.values()):
            log.info("epc: retention purge deleted %s", deleted)

    def certificate(self, number):
        now = self._now()
        self.maybe_purge(now)
        cached = self.store.cert_get(number, now)
        if cached is not None:
            return cached
        body = self._get("/api/certificate", {"certificate_number": number})
        record = parse_certificate(body, number) if body else None
        if record is None:
            raise Unavailable("certificate unreadable")
        self.store.cert_put(number, record, now)
        return record


# ---------------------------------------------------------------- matching

def _too_old(date, today):
    try:
        return (today - dt.date.fromisoformat(str(date)[:10])).days > MAX_CERT_AGE_YEARS * 365
    except ValueError:
        return False


def compatible_rooms(bedrooms, studio=False):
    """Habitable-room counts a flat with this many bedrooms can have: its bedrooms plus one living room
    (a studio counts one room). Kitchens and bathrooms are not habitable rooms in an EPC."""
    return {1} if studio or bedrooms == 0 else {bedrooms + 1}


def exact_compatible(listing, cert, advert_flat_like):
    """Is the certificate of the same kind of home as the advert? Explicit mismatches (a house for a flat, a flat
    for an advert that reads as a house, a room count far from the bedrooms) veto the exact label."""
    if advert_flat_like and cert.get("flat") is False:
        return False
    if not advert_flat_like and cert.get("flat") is True:
        return False
    rooms = cert.get("rooms")
    beds = max(listing.bedrooms, 1) if getattr(listing, "studio", False) or listing.bedrooms == 0 else listing.bedrooms
    if rooms is not None and not (max(1, beds) <= rooms <= beds + 2):
        return False
    return True


def range_compatible(cert, advert_flat_like):
    """The same dwelling-type evidence as the exact path (address AND description): a flat advert is compared only with
    flats (never houses), an advert that reads as a house only with houses."""
    if advert_flat_like:
        return cert.get("flat") is not False
    return cert.get("flat") is not True


def match(listing, postcode, epc, today=None):
    """Result dict: {'match': 'exact'|'range'|'none', ...}. Raises Unavailable when the service cannot answer."""
    today = today or dt.date.today()
    summaries = [s for s in epc.summaries(postcode) if not _too_old(s.get("registrationDate"), today)]
    if not summaries:
        return {"match": "none", "reason": "no certificates at this postcode", "postcode": postcode}
    advert = parse_address(listing.address)
    akey = _building_key(advert)
    parsed = [(s, parse_address(cert_address(s), s.get("postTown"))) for s in summaries]
    building = [(s, p) for s, p in parsed if akey and _building_key(p) == akey]

    # 1. exact: the same flat in the same building, or the only dwelling at a house number. The address is not
    # enough: the certificate's dwelling type and room count must also fit the advert (a whole house is never
    # an exact match for a flat), and an advert that names no flat in a building is never matched to one flat.
    advert_flat_like = bool(advert["flat"] or FLAT_LIKE.search(f"{listing.address} {listing.description[:300]}"))
    exact = None
    if building:
        if advert["flat"]:
            hits = [s for s, p in building if p["flat"] == advert["flat"]]
        elif not advert_flat_like and len(building) == 1 and not building[0][1]["flat"]:
            hits = [building[0][0]]
        else:
            hits = []
        if len(hits) == 1:
            exact = hits[0]
    if exact:
        cert = epc.certificate(exact["certificateNumber"])
        if cert.get("area") and exact_compatible(listing, cert, advert_flat_like):
            return {"match": "exact", "low": cert["area"], "high": cert["area"], "band": cert.get("band"),
                    "heating": cert.get("heating"), "annual_cost": cert.get("annual_cost"),
                    "date": cert.get("date"), "n": 1, "postcode": postcode}

    # 2. range: certificates of a compatible size in the same building (if it has several) or postcode
    pool = [s for s, _ in building] if len(building) >= RANGE_MIN_CANDIDATES else [s for s, _ in parsed]
    if len(pool) > epc_cap():
        return {"match": "none", "reason": f"{len(pool)} certificates at this postcode: too many to compare",
                "postcode": postcode}
    rooms = compatible_rooms(listing.bedrooms, getattr(listing, "studio", False))
    certs = []
    for s in pool:
        c = epc.certificate(s["certificateNumber"])
        if c.get("area") and c.get("rooms") in rooms and range_compatible(c, advert_flat_like):
            certs.append(c)
    if len(certs) < RANGE_MIN_CANDIDATES:
        return {"match": "none", "reason": "no unique certificate and too few of a compatible size",
                "postcode": postcode}
    areas = sorted(c["area"] for c in certs)
    spread = areas[-1] - areas[0]
    if spread > max(RANGE_TOLERANCE_ABS, RANGE_TOLERANCE_REL * statistics.median(areas)):
        return {"match": "none", "reason": f"compatible certificates disagree ({areas[0]:g}-{areas[-1]:g} m²)",
                "postcode": postcode}
    bands = {c.get("band") for c in certs}
    dates = sorted(c["date"] for c in certs if c.get("date"))
    return {"match": "range", "low": areas[0], "high": areas[-1], "n": len(certs),
            "band": bands.pop() if len(bands) == 1 else None, "heating": None, "annual_cost": None,
            "date": dates[-1] if dates else None, "postcode": postcode}


def epc_cap():
    return MAX_CERTS_PER_POSTCODE


# ---------------------------------------------------------------- the one call the pipeline makes

def postcode_of(listing):
    return norm_postcode(getattr(listing, "full_postcode", None)) or listing.postcode_full


def annotate(listing, epc=None):
    """Attach listing.epc and, when the advert gave no area, an area from an exact certificate. Never raises,
    never rejects. Private rentals with a full postcode only. Runs only when the feature is enabled or a client
    is passed in explicitly (an installed default client is never authority to run: switching the feature off
    takes effect at once)."""
    try:
        if listing.tenure != "rent" or not (enabled() or epc is not None):
            return listing
        postcode = postcode_of(listing)
        if not postcode:
            return listing
        epc = epc or default()
        result = match(listing, postcode, epc)
        listing.epc = result
        advert_area = listing.floor_area_m2
        if result["match"] == "exact" and not advert_area:
            listing.floor_area_m2 = result["low"]
            listing.area_source = "epc"
        elif advert_area:
            listing.area_source = listing.area_source or "advert"
            if result["match"] == "exact":
                result["advert_area"] = advert_area
    except Unavailable as ex:
        log.info("epc: unavailable (%s)", ex)
    except Exception as ex:
        log.warning("epc: failed (%s); advert unchanged", type(ex).__name__)
    return listing


# ---------------------------------------------------------------- value against recent portal-stated adverts

def area_used(listing):
    """An area we would stand behind: from the advert, an exact certificate or a floorplan, never a range."""
    return listing.floor_area_m2 if getattr(listing, "area_source", None) in ("advert", "epc", "floorplan") or \
        (listing.floor_area_m2 and not listing.epc) else None


def value_against(listing, scan):
    """{'per_m2', 'percentile', 'n', 'district', 'beds', 'peers'} - the share of comparable adverts (same bedroom
    count, same postcode district) that this one is roomier than, by area per pound. None when the rent or area is
    missing or fewer than MIN_COMPARABLES others have an area. 'Roomier' = more m2 per £ rent. `scan` holds the
    comparables; the pipeline passes recently seen adverts whose area the PORTAL stated, so no derived area
    (certificate, floorplan) is ever used as a comparable."""
    area = area_used(listing)
    if not listing.rent or not area:
        return None
    district = listing.outcode
    peers = []
    for other in scan:
        if other is listing or other.key == listing.key or other.tenure != "rent" or not other.rent:
            continue
        if other.bedrooms != listing.bedrooms or other.outcode != district:
            continue
        a = area_used(other)
        if a:
            peers.append(a / other.rent)
    if len(peers) < MIN_COMPARABLES:
        return None
    mine = area / listing.rent
    below = sum(p < mine for p in peers) + 0.5 * sum(p == mine for p in peers)
    return {"per_m2": round(listing.rent / area, 1), "percentile": round(100 * below / len(peers)),
            "n": len(peers), "district": district, "beds": listing.bedrooms, "peers": "portal-stated areas"}


def _peer_listing(row, district, bedrooms):
    from types import SimpleNamespace
    return SimpleNamespace(key=row["key"], tenure="rent", rent=row["rent"], bedrooms=bedrooms, outcode=district,
                           floor_area_m2=row["area"], area_source="advert", epc={})


def note_portal_area(listing, store=None, now=None):
    """Remember the area the PORTAL stated for this advert as a comparable for later ones. Called only from the post-alert step
    (never in the alert path, never in a dry run); derived areas are never recorded. Never raises."""
    try:
        if listing.tenure == "rent" and listing.rent and listing.floor_area_m2 and listing.outcode and \
                getattr(listing, "area_source", None) in (None, "advert") and not getattr(listing, "epc", None):
            (store or default().store).note_area(listing.key, listing.outcode, listing.bedrooms, listing.rent,
                                                 listing.floor_area_m2, now or dt.datetime.now(dt.timezone.utc))
    except Exception as ex:
        log.warning("epc: comparable not recorded (%s)", type(ex).__name__)


def value_for(listing, store=None, now=None):
    """listing.value from recently seen portal-stated adverts. Never raises."""
    try:
        if listing.tenure != "rent" or not listing.floor_area_m2:
            return listing
        store = store or default().store
        now = now or dt.datetime.now(dt.timezone.utc)
        peers = [_peer_listing(r, listing.outcode, listing.bedrooms)
                 for r in store.area_peers(listing.outcode, listing.bedrooms, now, listing.key)]
        listing.value = value_against(listing, peers) or {}
    except Exception as ex:
        log.warning("epc: value failed (%s)", type(ex).__name__)
    return listing


# ---------------------------------------------------------------- retention (independent of use)

def purge_expired(now=None, path=None):
    """Delete certificate/address rows, comparables and usage counters older than the retention window from facts.db. Touches nothing
    when the file or the tables do not exist. Returns {table: rows deleted}; never raises."""
    try:
        target = path or factsdb.path()
        if not os.path.exists(target):
            return {}
        now = now or dt.datetime.now(dt.timezone.utc)
        deleted = {}
        with factsdb.connect(None, target, create=False) as db:
            have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            cutoff = (now - dt.timedelta(days=CACHE_DAYS)).isoformat()
            for table, column in RETENTION_TABLES:
                if table in have:
                    deleted[table] = db.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,)).rowcount
        if any(deleted.values()):
            log.info("epc: retention purge deleted %s", deleted)
        return deleted
    except Exception as ex:
        log.warning("epc: retention purge failed (%s); will retry", type(ex).__name__)
        return {}


# ---------------------------------------------------------------- card line

def _year(date):
    return str(date or "")[:4] or "?"


def card_line(listing, today=None):
    """One plain-text line, or '' when there is nothing to say. Facts are labelled with their origin."""
    today = today or dt.date.today()
    result = getattr(listing, "epc", None) or {}
    value = getattr(listing, "value", None) or {}
    parts = []
    area = listing.floor_area_m2
    source = getattr(listing, "area_source", None)
    if area and source == "epc":
        parts.append(f"{area:g} m² (EPC {_year(result.get('date'))})")
    elif area and source in ("advert", "floorplan"):
        parts.append(f"{area:g} m² ({source})")
        if result.get("match") == "exact" and result.get("advert_area") and \
                abs(result["low"] - result["advert_area"]) > max(4, 0.15 * result["advert_area"]):
            parts.append(f"⚠ EPC says {result['low']:g} m²")
    elif result.get("match") == "range":
        parts.append(f"{result['low']:g}-{result['high']:g} m² (postcode range of {result['n']} EPCs, not this flat)")
    if value:
        parts.append(f"£{value['per_m2']:g}/m²")
        pct = value["percentile"]
        if pct >= 60:
            parts.append(f"roomier than {pct}% of {value['district']} "
                         f"{'studios' if value['beds'] == 0 else str(value['beds']) + '-beds'} ({value['n']} seen)")
        elif pct <= 40:
            parts.append(f"tighter than {100 - pct}% of {value['district']} "
                         f"{'studios' if value['beds'] == 0 else str(value['beds']) + '-beds'} ({value['n']} seen)")
    if result.get("band"):
        parts.append(f"EPC {result['band']}" + ("" if result.get("match") == "exact" else " (postcode)"))
    if result.get("match") == "exact" and result.get("annual_cost"):
        parts.append(f"~£{result['annual_cost'] / 12:.0f}/mo energy (certificate {_year(result.get('date'))}"
                     + (f", {result['heating'][:40]}" if result.get("heating") else "") + ")")
    return "📐 " + " · ".join(parts) if parts else ""
