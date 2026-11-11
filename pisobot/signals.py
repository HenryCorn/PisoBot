"""Opportunity signals (G3): flats likely to face less competition or to be negotiable.

These are HYPOTHESES. A signal adds a label to a card; it never changes which flats match, hides or reorders anything, and the
card shows at most two. OFF unless SIGNALS=1: with it off nothing here runs, no table is created and the release's behaviour is
unchanged. Every signal shown, and what the person then did (save / hide), is logged so the hypotheses can be checked after a few
weeks (report()); the log is written AFTER the send sequence (Telegram accepted, seen receipt, acknowledgement) and never inside it.

Send-path rule (nothing optional may do network or slow work before an alert is sent): signals are LOCAL computation. Every read
goes through ArchiveReader, a read-only SQLite connection (mode=ro, query_only) with a 0.25 s busy timeout, opened once per batch
of cards; annotate() stops after SEND_BUDGET_SECONDS in total and wraps everything, so any error or contention simply yields no
signal and nothing is ever raised into the send path. No detector performs a network call.

Where a detector needs a floor area it uses ONLY an area the portal itself states (stated_area); see the marked extension point.

How the release records an advert's life, and which transitions count as "back on the market" (archive.py): `observations` gets
a row (key, at, rent, status) at the first sighting ("active"), whenever the rent changes ("active"), when an in-scope advert has
been missing from UNSEEN_AFTER (2) consecutive COMPLETE sweeps ("unseen"), and when an unseen advert is seen again ("active": upsert
inserts a row when the stored status was not active). The release does not record "let agreed" or "under offer" at all. So an advert
is treated as BACK ON THE MARKET when its history holds an "unseen" row followed by a later "active" row (a reactivation) within
BACK_WINDOW_DAYS, and it is active now. That means: absent from the portal's complete result for at least two sweeps, now listed again.
It is NOT evidence that a let fell through; the label says only what is known.
"""
import contextlib
import dataclasses
import datetime as dt
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from urllib.parse import quote

from . import localdb, rental_text

log = logging.getLogger(__name__)

STALE_DAYS = 21
LONG_STALE_DAYS = 42
FEW_PHOTOS = 4
SHORT_DESCRIPTION = 250
STUDIO_TOO_BIG_M2 = 38.0
TWO_BED_TOO_SMALL_M2 = 42.0
SAME_FLAT_METRES = 30
SAME_FLAT_AREA_TOLERANCE = 0.01
SAME_FLAT_TEXT_SIMILARITY = 0.5
MIN_PRICE_GAP = 25
BACK_WINDOW_DAYS = 30
MAX_SHOWN = 2
PRIORITY = ("back_on_market", "same_flat_two_prices", "reduced_stale", "under_presented")
STRENGTH_NAMES = {1: "weak", 2: "medium", 3: "strong"}
BUSY_SECONDS = 0.25
SEND_BUDGET_SECONDS = 0.15          # all signals for one batch of cards, before the sends (local reads only)
LOG_RETENTION_DAYS = 90
SEARCHED_SOURCES = ("rm", "otmr", "orent", "sr")
MAX_NEIGHBOURS = 50


@dataclasses.dataclass(frozen=True)
class Signal:
    name: str
    label: str
    evidence: str
    strength: int       # 1 weak .. 3 strong
    idea: str = ""      # optional labelled negotiation idea (reduced_stale only)

    def to_dict(self):
        return dataclasses.asdict(self)


def enabled():
    return os.environ.get("SIGNALS", "") in ("1", "true", "yes") and os.environ.get("STORAGE") == "local"


def _date(value):
    try:
        return dt.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _money(value):
    return f"£{value:,.0f}"


# ---------------------------------------------------------------- the one place an area comes from

def stated_area(listing):
    """The floor area the PORTAL states for this advert (m2), or None.

    EXTENSION POINT: areas from other sources (an energy certificate, a floorplan reading) may be added here LATER, but only
    labelled as such and never mixed silently with a portal figure; nothing else in this module reads an area."""
    area = getattr(listing, "floor_area_m2", None)
    return area if area else None


# ---------------------------------------------------------------- local, read-only archive access

class ArchiveReader:
    """Read-only access to the market archive: a `mode=ro`, `query_only` connection with a short busy timeout. Any failure
    raises; callers wrap it so a failure means no signal. `path=None` is the live primary database."""

    def __init__(self, path=None):
        self.path, self.db = path, None

    def __enter__(self):
        path = self.path
        if path is None:
            token = localdb.DATABASE.set(None)
            try:
                path = localdb.database_path()
            finally:
                localdb.DATABASE.reset(token)
        if not os.path.exists(path):
            raise FileNotFoundError("no archive database")
        self.db = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=BUSY_SECONDS)
        self.db.row_factory = sqlite3.Row
        self.db.execute(f"PRAGMA busy_timeout = {int(BUSY_SECONDS * 1000)}")
        self.db.execute("PRAGMA query_only = 1")
        return self

    def __exit__(self, *exc):
        with contextlib.suppress(Exception):
            self.db.close()
        self.db = None

    def history(self, key):
        """[{'at', 'rent', 'status'}] oldest first: the release's observations for this advert (one indexed lookup)."""
        return [dict(r) for r in self.db.execute(
            "SELECT at, rent, status FROM observations WHERE key = ? ORDER BY id", (key,))]

    def first_listed(self, key):
        row = self.db.execute("SELECT portal_first_listed FROM adverts WHERE key = ?", (key,)).fetchone()
        return row["portal_first_listed"] if row else None

    def neighbours(self, listing, metres=SAME_FLAT_METRES):
        """Currently ACTIVE archived adverts with the same bedroom count within `metres` of this one (not this advert), as
        listings. Per source the existing (source, status) index narrows the rows; the box test and LIMIT bound the rest."""
        if listing.lat is None or listing.lng is None:
            return []
        from .sources import restore
        dlat = metres / 111_200
        dlng = metres / (111_200 * max(0.2, math.cos(math.radians(listing.lat))))
        found = []
        for source in SEARCHED_SOURCES:
            for r in self.db.execute(
                    "SELECT key, url, rent, bedrooms, lat, lng, data FROM adverts WHERE source = ? AND status = 'active' "
                    "AND bedrooms = ? AND lat BETWEEN ? AND ? AND lng BETWEEN ? AND ? AND key != ? LIMIT ?",
                    (source, listing.bedrooms, listing.lat - dlat, listing.lat + dlat, listing.lng - dlng, listing.lng + dlng,
                     listing.key, MAX_NEIGHBOURS)):
                try:
                    data = json.loads(r["data"])
                    data.update(url=r["url"], rent=r["rent"], bedrooms=r["bedrooms"], lat=r["lat"], lng=r["lng"])
                    found.append(restore(data))
                except Exception:
                    continue
        return found


# ---------------------------------------------------------------- detectors (pure)

def under_presented(l):
    """Few photos, no floorplan, a very short advert, or a type/size that contradicts itself (using only a portal-stated area).
    Two plain shortfalls, or any contradiction, are needed: a missing floorplan alone is too common to mean anything."""
    findings, contradiction = [], []
    photos = len(l.images) if l.images else (1 if l.image else 0)
    if photos < FEW_PHOTOS:
        findings.append(f"{photos} photo{'s' if photos != 1 else ''}")
    if not l.floorplan:
        findings.append("no floorplan")
    if len(l.description or "") < SHORT_DESCRIPTION:
        findings.append(f"{len(l.description or '')}-character description")
    area = stated_area(l)
    studio = l.studio or rental_text.is_studio(l.description or "")
    if area and studio and l.bedrooms >= 1 and area >= STUDIO_TOO_BIG_M2 and re.search(
            r"\b(?:1|one)[- ]bed(?:room)?\b|\bseparate (?:bed)?room\b", l.description or "", re.I):
        contradiction.append(f"called a studio but {area:g} m² (portal) and described with a bedroom")
    elif area and l.bedrooms >= 2 and area < TWO_BED_TOO_SMALL_M2:
        contradiction.append(f"{l.bedrooms} bedrooms in only {area:g} m² (portal)")
    if contradiction:
        return Signal("under_presented", "May be mis-described", "; ".join(contradiction + findings), 3)
    if len(findings) >= 2:
        return Signal("under_presented", "Thin advert, fewer people may look",
                      ", ".join(findings), 3 if len(findings) >= 3 else 2)
    return None


def _cuts(rows):
    """Rent cuts in a history of active observations: [(from, to)], oldest first."""
    cuts, last = [], None
    for r in rows:
        if r.get("status") not in (None, "active"):
            continue
        rent = r.get("rent")
        if rent and last and rent < last:
            cuts.append((last, rent))
        if rent:
            last = rent
    return cuts


def reduced_stale(l, rows=(), today=None, first_listed=None):
    """Portal says reduced, or first listed (the portal's own date) more than 21 days ago, or the archive saw rent cuts."""
    today = today or dt.date.today()
    listed = _date(l.first_listed) or _date(first_listed)
    days = (today - listed).days if listed else None
    stale = days is not None and days > STALE_DAYS
    reduced = l.update_reason == "reduced"
    cuts = _cuts(rows)
    if not (stale or reduced or cuts):
        return None
    evidence = []
    if reduced:
        when = _date(l.update_date)
        evidence.append("portal says reduced" + (f" {(today - when).days} days ago" if when else ""))
    if stale:
        evidence.append(f"first listed {days} days ago")
    if cuts:
        total = cuts[0][0] - cuts[-1][1]
        evidence.append(f"{len(cuts)} cut{'s' if len(cuts) != 1 else ''} seen, {_money(total)} off in total "
                        f"({_money(cuts[0][0])} to {_money(cuts[-1][1])})")
    score = int(reduced) + int(stale) + int(bool(cuts)) + int(bool(days and days > LONG_STALE_DAYS))
    idea = ""
    if l.rent:
        pct = 5 if (days or 0) > LONG_STALE_DAYS or len(cuts) >= 2 else 3
        idea = (f"Idea, not advice: an offer a little under the {_money(l.rent)} asking "
                f"(for example {pct}% under, about {_money(round(l.rent * (1 - pct / 100), -1))}) is lawful if you "
                f"want to try; offers above asking are not allowed.")
    return Signal("reduced_stale", "Reduced or long on the market" if reduced or cuts else "Long on the market",
                  "; ".join(evidence), min(3, max(1, score)), idea)


def back_on_market(l, rows=(), today=None):
    """The advert's archive history holds an 'unseen' observation (absent from two or more complete sweeps) followed by a later
    'active' one (seen again), the return within BACK_WINDOW_DAYS. See the module docstring for exactly what this does and
    does not mean; the release records no 'let agreed' state."""
    today = today or dt.date.today()
    rows = list(rows)
    for i in range(len(rows) - 1, 0, -1):
        back = rows[i]
        if back.get("status") != "active":
            continue
        gone = next((r for r in reversed(rows[:i]) if r.get("status") == "unseen"), None)
        if gone is None:
            return None
        returned, left = _date(back.get("at")), _date(gone.get("at"))
        if returned is None or left is None or (today - returned).days > BACK_WINDOW_DAYS:
            return None
        absent = (returned - left).days
        return Signal("back_on_market", "Back on the market",
                      f"marked unseen on {left} (absent from 2+ complete sweeps), listed again on {returned}"
                      + (f"; away at least {absent} days" if absent >= 1 else ""),
                      3 if absent >= 7 else 2)
    return None


def _metres(a, b):
    if None in (a.lat, a.lng, b.lat, b.lng):
        return None
    return math.hypot((a.lat - b.lat) * 111_200, (a.lng - b.lng) * 111_200 * math.cos(math.radians(a.lat)))


def _words(text):
    return set(re.findall(r"[a-z]{4,}", (text or "").lower()))


def _photo_ids(l):
    return {u.rsplit("/", 1)[-1].split("?")[0] for u in (l.images or []) if u and "/" in u}


def same_flat_two_prices(l, candidates):
    """Another currently active advert (from the archive) within ~30 m with the same bedrooms, a different rent, and corroboration:
    a shared photo file name (strong), or a near-identical PORTAL-stated area on both plus similar text (medium)."""
    best = None
    for o in candidates:
        if o.key == l.key or o.tenure != "rent" or o.bedrooms != l.bedrooms or not o.rent or not l.rent:
            continue
        if abs(o.rent - l.rent) < MIN_PRICE_GAP:
            continue
        d = _metres(l, o)
        if d is None or d > SAME_FLAT_METRES:
            continue
        strength = 0
        if _photo_ids(l) & _photo_ids(o):
            strength = 3
        else:
            a, b = stated_area(l), stated_area(o)
            if a and b and abs(a - b) <= SAME_FLAT_AREA_TOLERANCE * a:
                x, y = _words(l.description), _words(o.description)
                if x and y and len(x & y) / len(x | y) >= SAME_FLAT_TEXT_SIMILARITY:
                    strength = 2
        if strength and (best is None or strength > best[0]):
            best = (strength, o, d, strength == 3)
    if not best:
        return None
    strength, o, d, photos = best
    why = f"{'shares a photo' if photos else 'same area and similar text'}, {d:.0f} m apart, {o.bedrooms or 'studio'} bed, {o.source}:{o.id}"
    gap = abs(l.rent - o.rent)
    if o.rent < l.rent:
        agent = f" ({o.agent})" if o.agent else ""
        return Signal("same_flat_two_prices", f"Probably also listed {_money(gap)}/mo cheaper",
                      f"{why}; cheaper advert{agent}: {o.url}", strength)
    return Signal("same_flat_two_prices", "Cheapest of 2 adverts for this flat",
                  f"{why}; the other asks {_money(gap)}/mo more: {o.url}", strength)


# ---------------------------------------------------------------- selection

def compute(l, reader=None, today=None):
    """Signals for one private rental, strongest first, at most MAX_SHOWN (what a card would carry)."""
    return compute_all(l, reader, today)[:MAX_SHOWN]


def compute_all(l, reader=None, today=None):
    """Every signal that fires for one private rental, strongest first (the evaluation script counts these). `reader` (an open
    ArchiveReader) supplies history and neighbours; without one only the archive-free detectors run. Each detector is isolated:
    a failure yields no signal."""
    if l.tenure != "rent":
        return []
    rows, listed, neighbours = [], None, []
    if reader is not None:
        with contextlib.suppress(Exception):
            rows = reader.history(l.key)
            listed = reader.first_listed(l.key)
        with contextlib.suppress(Exception):
            neighbours = reader.neighbours(l)
    found = []
    for detect in (lambda: back_on_market(l, rows, today), lambda: same_flat_two_prices(l, neighbours),
                   lambda: reduced_stale(l, rows, today, listed), lambda: under_presented(l)):
        try:
            s = detect()
        except Exception as ex:
            log.warning("signals: detector failed (%s)", type(ex).__name__)
            continue
        if s:
            found.append(s)
    found.sort(key=lambda s: (-s.strength, PRIORITY.index(s.name)))
    return found


def annotate(listings, budget=SEND_BUDGET_SECONDS, today=None, clock=time.monotonic, reader_factory=None):
    """Set .signals on each private rental about to be shown on a card. Local reads only, one read-only connection for the batch,
    at most `budget` seconds in total (cards after that simply get none), and NOTHING is ever raised: any error or contention
    yields no signal. Returns the number of listings that got at least one signal."""
    if not enabled():
        return 0
    got = 0
    try:
        deadline = clock() + budget
        with (reader_factory or ArchiveReader)() as reader:
            for l in listings:
                if clock() >= deadline:
                    break
                try:
                    l.signals = [s.to_dict() for s in compute(l, reader, today)]
                    got += bool(l.signals)
                except Exception as ex:
                    log.warning("signals: failed for a card (%s)", type(ex).__name__)
    except Exception as ex:
        log.info("signals: unavailable (%s); cards go out without them", type(ex).__name__)
    return got


def card_line(l):
    """Plain-text card line ('' when nothing). Labels only; the evidence is in the log."""
    signals = getattr(l, "signals", None) or []
    return " · ".join(f"🔎 {s['label']}" for s in signals)


def card_ideas(l):
    return [s["idea"] for s in getattr(l, "signals", None) or [] if s.get("idea")]


# ---------------------------------------------------------------- hypothesis log (written AFTER the send sequence)

DDL = """
CREATE TABLE IF NOT EXISTS signal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, key TEXT NOT NULL, signal TEXT NOT NULL,
    strength INTEGER NOT NULL, evidence TEXT NOT NULL, rent REAL, shown_at TEXT NOT NULL, via TEXT NOT NULL,
    action TEXT, action_at TEXT);
CREATE INDEX IF NOT EXISTS signal_log_key ON signal_log (chat_id, key);
"""
_buffer, _buffer_lock = [], threading.Lock()
BUFFER_LIMIT = 1000


@contextlib.contextmanager
def _db(path=None):
    token = localdb.DATABASE.set(None)
    try:
        path = path or localdb.database_path()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with contextlib.closing(sqlite3.connect(path, timeout=BUSY_SECONDS)) as db:
            db.row_factory = sqlite3.Row
            db.execute(f"PRAGMA busy_timeout = {int(BUSY_SECONDS * 1000)}")
            db.executescript(DDL)
            with db:
                yield db
    finally:
        localdb.DATABASE.reset(token)


def capture(l, chat_id, via, now=None):
    """The in-memory log rows for the signals on a card that was just accepted by Telegram. NO I/O."""
    signals = getattr(l, "signals", None) or []
    stamp = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
    return [{"chat_id": str(chat_id), "key": l.key, "signal": s["name"], "strength": s["strength"], "evidence": s["evidence"],
             "rent": l.rent, "shown_at": stamp, "via": via} for s in signals]


def defer(rows):
    """Park captured rows in memory until flush(); bounded, never raises."""
    if rows:
        with _buffer_lock:
            _buffer.extend(rows)
            del _buffer[:-BUFFER_LIMIT]


def flush(path=None, now=None):
    """Write and clear the parked rows after the run lock is released (short busy timeout, skipped on contention) and apply
    retention. Never raises; returns rows written."""
    with _buffer_lock:
        rows, _buffer[:] = list(_buffer), []
    if not rows or not (enabled() or path):
        return 0
    try:
        now = now or dt.datetime.now(dt.timezone.utc)
        with _db(path) as db:
            db.executemany("INSERT INTO signal_log (chat_id, key, signal, strength, evidence, rent, shown_at, via) "
                           "VALUES (:chat_id, :key, :signal, :strength, :evidence, :rent, :shown_at, :via)", rows)
            db.execute("DELETE FROM signal_log WHERE shown_at < ?",
                       ((now - dt.timedelta(days=LOG_RETENTION_DAYS)).isoformat(timespec="seconds"),))
        return len(rows)
    except Exception as ex:
        log.warning("signals: log not written (%s)", type(ex).__name__)
        return 0


def log_action(chat_id, key, action, now=None, path=None):
    """What the person then did ('saved' or 'hidden'): the latest shown rows of that advert for that person without one. Best effort."""
    if not (enabled() or path):
        return
    try:
        stamp = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
        with _db(path) as db:
            db.execute("UPDATE signal_log SET action = ?, action_at = ? WHERE chat_id = ? AND key = ? AND action IS NULL",
                       ({"s": "saved", "h": "hidden", "d": "hidden"}.get(action, action), stamp, str(chat_id), key))
    except Exception as ex:
        log.warning("signals: action not logged (%s)", type(ex).__name__)


def report(path=None):
    """{signal: {'shown', 'saved', 'hidden', 'no_action', 'save_rate'}}: compare each hypothesis with the base rate of saves."""
    out = {}
    with _db(path) as db:
        for r in db.execute("SELECT signal, action, COUNT(DISTINCT chat_id || key) n FROM signal_log GROUP BY signal, action"):
            row = out.setdefault(r["signal"], {"shown": 0, "saved": 0, "hidden": 0, "no_action": 0})
            row["shown"] += r["n"]
            row[r["action"] or "no_action"] += r["n"]
    for row in out.values():
        row["save_rate"] = round(row["saved"] / row["shown"], 3) if row["shown"] else None
    return out
