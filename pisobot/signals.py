"""Opportunity signals (G3): flats likely to face less competition or to be negotiable.

These are HYPOTHESES. Signals add a label to a card; they never change which flats match, never hide or
reorder anything, and the card shows at most two. OFF unless SIGNALS=1. Every signal shown and what the person
then did (save / hide) is logged to SQLite so the hypotheses can be checked after a few weeks (report()).

Each detector is a pure function  detector(listing, ...) -> Signal | None  using only the listing, the other
adverts in the same scan and, where noted, a history of that advert (see History below).

Rules of law kept in the wording: since 1 May 2026 landlords and agents in England may not accept offers
above the advertised rent. An offer BELOW asking is lawful; a suggested figure is therefore only ever a
clearly labelled idea, never advice, and never above asking.
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

from . import localdb, rental_text

log = logging.getLogger(__name__)

STALE_DAYS = 21
LONG_STALE_DAYS = 42
FEW_PHOTOS = 4
SHORT_DESCRIPTION = 250         # characters of advert text (portal summary plus details) that count as thin
STUDIO_TOO_BIG_M2 = 38.0        # a "studio" bigger than this is probably a one-bedroom filed under the wrong type
TWO_BED_TOO_SMALL_M2 = 42.0     # a two-bedroom smaller than this deserves a second look
SAME_FLAT_METRES = 30
SAME_FLAT_AREA_TOLERANCE = 0.01
SAME_FLAT_TEXT_SIMILARITY = 0.5
MIN_PRICE_GAP = 25              # £/month between two adverts before one is called cheaper
BACK_WINDOW_DAYS = 30          # a let-agreed or gone mark older than this is no longer news
BACK_AFTER_DAYS = 3             # absent at least this long (or marked let agreed) before "back on the market"
MAX_SHOWN = 2
PRIORITY = ("back_on_market", "same_flat_two_prices", "reduced_stale", "under_presented")
STRENGTHS = {1: "weak", 2: "medium", 3: "strong"}


@dataclasses.dataclass(frozen=True)
class Signal:
    name: str           # one of PRIORITY
    label: str          # short card text
    evidence: str       # why, in plain words, checkable by the reader
    strength: int       # 1 weak .. 3 strong
    idea: str = ""      # optional labelled negotiation idea (reduced_stale only)

    def to_dict(self):
        return dataclasses.asdict(self)


def enabled():
    return os.environ.get("SIGNALS", "") in ("1", "true", "yes")


def _date(value):
    try:
        return dt.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _money(value):
    return f"£{value:,.0f}"


# ------------------------------------------------------------ history interface
#
# History rows for one advert, oldest first, as plain dicts (the archive of PR #17 will provide them):
#   {"at": "2026-10-01", "state": "listed" | "let_agreed" | "gone", "rent": 1500.0 or None}
# "listed" = seen on a portal; "let_agreed" = the portal showed it as let agreed / under offer;
# "gone" = absent from a complete scan. A history provider is any callable  history(key) -> list of rows.

def history_rows(history, listing):
    if history is None:
        return []
    try:
        rows = history(listing.key) or []
    except Exception as ex:                      # a broken archive must never break an alert
        log.warning("signals: history failed (%s)", type(ex).__name__)
        return []
    return sorted((r for r in rows if isinstance(r, dict) and _date(r.get("at"))), key=lambda r: r["at"])


# ------------------------------------------------------------ detectors

def under_presented(l):
    """Few photos, no floorplan, a very short advert, or a type/size that contradicts itself. Two plain
    shortfalls, or any contradiction, are needed: a missing floorplan alone is too common to mean anything."""
    findings, contradiction = [], []
    photos = len(l.images) if l.images else (1 if l.image else 0)
    if photos < FEW_PHOTOS:
        findings.append(f"{photos} photo{'s' if photos != 1 else ''}")
    if not l.floorplan:
        findings.append("no floorplan")
    if len(l.description or "") < SHORT_DESCRIPTION:
        findings.append(f"{len(l.description or '')}-character description")
    area = l.floor_area_m2
    studio = l.studio or rental_text.is_studio(l.description or "")
    if area and studio and l.bedrooms >= 1 and area >= STUDIO_TOO_BIG_M2 and re.search(
            r"\b(?:1|one)[- ]bed(?:room)?\b|\bseparate (?:bed)?room\b", l.description or "", re.I):
        contradiction.append(f"called a studio but {area:g} m² and described with a bedroom")
    elif area and l.bedrooms >= 2 and area < TWO_BED_TOO_SMALL_M2:
        contradiction.append(f"{l.bedrooms} bedrooms in only {area:g} m²")
    if contradiction:
        return Signal("under_presented", "May be mis-described", "; ".join(contradiction + findings), 3)
    if len(findings) >= 2:
        return Signal("under_presented", "Thin advert, fewer people may look",
                      ", ".join(findings), 3 if len(findings) >= 3 else 2)
    return None


def _cuts(rows):
    """Rent cuts in a history: [(from, to)], oldest first."""
    cuts, last = [], None
    for r in rows:
        rent = r.get("rent")
        if rent and last and rent < last:
            cuts.append((last, rent))
        if rent:
            last = rent
    return cuts


def reduced_stale(l, rows=(), today=None):
    """Portal says reduced, or the advert has been up more than 21 days; with history, the cuts so far."""
    today = today or dt.date.today()
    listed = _date(l.first_listed)
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
    if days is not None and days > STALE_DAYS:
        evidence.append(f"first listed {days} days ago")
    if cuts:
        total = cuts[0][0] - cuts[-1][1]
        evidence.append(f"{len(cuts)} cut{'s' if len(cuts) != 1 else ''} seen, {_money(total)} off in total "
                        f"({_money(cuts[0][0])} to {_money(cuts[-1][1])})")
    score = int(reduced) + int(stale) + int(bool(cuts)) + int(bool(days and days > LONG_STALE_DAYS))
    idea = ""
    if l.rent and (stale or reduced or cuts):
        pct = 5 if (days or 0) > LONG_STALE_DAYS or len(cuts) >= 2 else 3
        idea = (f"Idea, not advice: an offer a little under the {_money(l.rent)} asking "
                f"(for example {pct}% under, about {_money(round(l.rent * (1 - pct / 100), -1))}) is lawful if you "
                f"want to try; offers above asking are not allowed.")
    return Signal("reduced_stale", "Reduced or long on the market" if reduced or cuts else "Long on the market",
                  "; ".join(evidence), min(3, max(1, score)), idea)


def back_on_market(l, rows=(), today=None):
    """Seen before as let agreed, or gone for a while, and now listed again. Needs history rows."""
    today = today or dt.date.today()
    rows = list(rows)
    for i in range(len(rows) - 1, -1, -1):
        row = rows[i]
        if row.get("state") in ("let_agreed", "gone"):
            then = _date(row["at"])
            gap = (today - then).days
            if gap > BACK_WINDOW_DAYS:
                break
            if row["state"] == "let_agreed" or gap >= BACK_AFTER_DAYS:
                what = "marked let agreed" if row["state"] == "let_agreed" else "absent from the portal"
                return Signal("back_on_market", "Back on the market",
                              f"{what} on {row['at'][:10]}, listed again now"
                              + (" - a deal may have fallen through" if row["state"] == "let_agreed" else ""),
                              3 if row["state"] == "let_agreed" else 2)
            break
    return None


def _metres(a, b):
    if None in (a.lat, a.lng, b.lat, b.lng):
        return None
    return math.hypot((a.lat - b.lat) * 111_200, (a.lng - b.lng) * 111_200 * math.cos(math.radians(a.lat)))


def _words(text):
    return set(re.findall(r"[a-z]{4,}", (text or "").lower()))


def _photo_ids(l):
    return {u.rsplit("/", 1)[-1].split("?")[0] for u in (l.images or []) if u and "/" in u}


def same_flat_two_prices(l, scan):
    """Another advert in this scan within ~30 m with the same bedrooms and (a shared photo, or a near-identical
    area and similar text) but a different rent. Identical flats in one block also fit the second test, so it
    needs an area on both adverts and similar text; a shared photo is the strong evidence."""
    best = None
    for o in scan:
        if o is l or o.key == l.key or o.tenure != "rent" or o.bedrooms != l.bedrooms or not o.rent or not l.rent:
            continue
        if abs(o.rent - l.rent) < MIN_PRICE_GAP:
            continue
        d = _metres(l, o)
        if d is None or d > SAME_FLAT_METRES:
            continue
        shared = _photo_ids(l) & _photo_ids(o)
        strength = 0
        if shared:
            strength = 3
        elif l.floor_area_m2 and o.floor_area_m2 and \
                abs(l.floor_area_m2 - o.floor_area_m2) <= SAME_FLAT_AREA_TOLERANCE * l.floor_area_m2:
            a, b = _words(l.description), _words(o.description)
            if a and b and len(a & b) / len(a | b) >= SAME_FLAT_TEXT_SIMILARITY:
                strength = 2
        if strength and (best is None or strength > best[0]):
            best = (strength, o, d, bool(shared))
    if not best:
        return None
    strength, o, d, photos = best
    why = f"{'shares a photo' if photos else 'same area and similar text'}, {d:.0f} m apart, " \
          f"{o.bedrooms or 'studio'} bed, {o.source}:{o.id}"
    gap = abs(l.rent - o.rent)
    if o.rent < l.rent:
        agent = f" ({o.agent})" if o.agent else ""
        return Signal("same_flat_two_prices", f"Probably also listed {_money(gap)}/mo cheaper",
                      f"{why}; cheaper advert{agent}: {o.url}", strength)
    return Signal("same_flat_two_prices", "Cheapest of 2 adverts for this flat",
                  f"{why}; the other asks {_money(gap)}/mo more: {o.url}", strength)


# ------------------------------------------------------------ selection and card

def compute(l, scan=(), history=None, today=None):
    """All signals for one private rental, strongest first, at most MAX_SHOWN. Never raises."""
    if l.tenure != "rent":
        return []
    rows = history_rows(history, l)
    found = []
    for detect in (lambda: back_on_market(l, rows, today), lambda: same_flat_two_prices(l, scan),
                   lambda: reduced_stale(l, rows, today), lambda: under_presented(l)):
        try:
            s = detect()
        except Exception as ex:
            log.warning("signals: detector failed (%s)", type(ex).__name__)
            continue
        if s:
            found.append(s)
    found.sort(key=lambda s: (-s.strength, PRIORITY.index(s.name)))
    return found[:MAX_SHOWN]


def annotate(listings, history=None, today=None):
    """Set .signals on each private rental (list of dicts, so the card and logs need no import of this module).
    Never changes any other field. Off unless enabled."""
    if not enabled():
        return
    for l in listings:
        try:
            l.signals = [s.to_dict() for s in compute(l, listings, history, today)]
        except Exception as ex:
            log.warning("signals: failed (%s)", type(ex).__name__)


def card_line(l):
    """Plain-text card line ('' when nothing). Labels only; the evidence is in the log and on /whatif later."""
    signals = getattr(l, "signals", None) or []
    return " · ".join(f"🔎 {s['label']}" for s in signals)


def card_ideas(l):
    return [s["idea"] for s in getattr(l, "signals", None) or [] if s.get("idea")]


# ------------------------------------------------------------ hypothesis log

DDL = """
CREATE TABLE IF NOT EXISTS signal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, signal TEXT NOT NULL, strength INTEGER NOT NULL,
    evidence TEXT NOT NULL, rent REAL, shown_at TEXT NOT NULL, action TEXT, action_at TEXT);
CREATE INDEX IF NOT EXISTS signal_log_key ON signal_log (key);
"""


@contextlib.contextmanager
def _db(path=None):
    path = path or localdb.database_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with contextlib.closing(sqlite3.connect(path, timeout=localdb.BUSY_TIMEOUT_MS / 1000)) as db:
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout = {localdb.BUSY_TIMEOUT_MS}")
        db.executescript(DDL)
        with db:
            yield db


def _logging_on():
    return enabled() and os.environ.get("STORAGE") == "local"


def log_shown(l, now=None, path=None):
    """Record every signal on a card that was actually sent. Never raises."""
    signals = getattr(l, "signals", None) or []
    if not signals or not (_logging_on() or path):
        return
    try:
        stamp = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
        with _db(path) as db:
            db.executemany("INSERT INTO signal_log (key, signal, strength, evidence, rent, shown_at) "
                           "VALUES (?, ?, ?, ?, ?, ?)",
                           [(l.key, s["name"], s["strength"], s["evidence"], l.rent, stamp) for s in signals])
    except Exception as ex:
        log.warning("signals: log failed (%s)", type(ex).__name__)


def log_action(key, action, now=None, path=None):
    """Record what the person then did: 'saved' or 'hidden' (the latest shown rows of that advert without one)."""
    if not (_logging_on() or path):
        return
    try:
        stamp = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
        with _db(path) as db:
            db.execute("UPDATE signal_log SET action = ?, action_at = ? WHERE key = ? AND action IS NULL",
                       ({"s": "saved", "h": "hidden", "d": "hidden"}.get(action, action), stamp, key))
    except Exception as ex:
        log.warning("signals: action log failed (%s)", type(ex).__name__)


def report(path=None):
    """{signal: {'shown': n, 'saved': n, 'hidden': n, 'no_action': n, 'save_rate': x}} - the numbers to compare
    each hypothesis against the base rate of saves over all alerts."""
    out = {}
    with _db(path) as db:
        for r in db.execute("SELECT signal, action, COUNT(DISTINCT key) n FROM signal_log GROUP BY signal, action"):
            row = out.setdefault(r["signal"], {"shown": 0, "saved": 0, "hidden": 0, "no_action": 0})
            row["shown"] += r["n"]
            row[r["action"] or "no_action"] += r["n"]
    for row in out.values():
        row["save_rate"] = round(row["saved"] / row["shown"], 3) if row["shown"] else None
    return out
