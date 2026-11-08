"""Per-person search profiles for the VPS bot (STORAGE=local).

The owner is TELEGRAM_CHAT_ID and keeps the original database and seen.json. Other people are
admitted by Telegram username (TELEGRAM_INVITE_USERNAMES, up to MAX_USERS in total) and get
their own SQLite database and seen file under PISOBOT_STATE_DIR/users/<chat_id>/, so saved,
hidden, feedback, alerts and baselines are fully separate.

Search parameters live in the primary database's ``users`` table as JSON. A run applies them by
temporarily overriding the matching ``config`` attributes (``criteria``); portals are fetched
once per cycle with the union of everyone's parameters (``fetch_params``) and then filtered per
person, so a second person costs no extra portal requests.
"""
import contextlib
import contextvars
import datetime as dt
import html
import json
import math
import os
import re
from dataclasses import dataclass, field

from . import config, localdb

CURRENT = contextvars.ContextVar("pisobot_user", default=None)


@dataclass(frozen=True)
class Param:
    attr: str
    kind: type
    low: float
    high: float
    label: str
    unit: str = ""
    choices: tuple = ()


PARAMS = {
    "budget": Param("MAX_MONTHLY_COST", float, 300, 10000, "Max monthly cost (rent + bills)", "£/mo"),
    "beds": Param("MIN_BEDROOMS", int, 1, 5, "Min bedrooms"),
    "commute": Param("MAX_TRAVEL_MINUTES", int, 10, 120, "Max commute (weekday 08:30)", "min"),
    "crime": Param("MAX_CRIME_SCORE", int, 10, 2000, "Max safety crimes within 500 m / month"),
    "bills": Param("RENT_BILLS_ESTIMATE", float, 0, 1500, "Bills estimate when not included", "£/mo"),
    "minrent": Param("RENT_MIN", float, 0, 10000, "Min advertised rent (filters out rooms)", "£/mo"),
    "area": Param("RENT_MIN_AREA_M2", float, 0, 300, "Min floor area, when published", "m²"),
    "radius": Param("RENT_SEARCH_MILES", float, 1, 15, "Portal search radius round Oxford Circus", "miles",
                    choices=(1, 3, 5, 10, 15)),
}
DESTINATION = ("TARGET_NAME", "TARGET_LAT", "TARGET_LNG")
DEFAULTS = {p.attr: getattr(config, p.attr) for p in PARAMS.values()}
DEFAULTS.update({attr: getattr(config, attr) for attr in DESTINATION})
LONDON = (51.25, 51.75, -0.55, 0.35)   # lat/lng box for destinations


@dataclass
class User:
    chat_id: str
    name: str = ""
    owner: bool = False
    params: dict = field(default_factory=dict)

    @property
    def directory(self):
        return None if self.owner else localdb.state_dir() / "users" / re.sub(r"[^0-9-]", "", self.chat_id)

    @property
    def label(self):
        return self.name or self.chat_id


def enabled():
    """Several people only with local storage; the cloud Worker remains single-chat."""
    return localdb.storage() == "local"


def owner_chat():
    return os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def invited():
    return {u.strip().lstrip("@").lower() for u in os.environ.get("TELEGRAM_INVITE_USERNAMES", "").split(",")
            if u.strip()}


def max_users():
    return int(os.environ.get("MAX_USERS") or 2)


# ---------- context ----------

def current():
    return CURRENT.get()


def current_chat():
    user = CURRENT.get()
    return user.chat_id if user and user.chat_id else None


@contextlib.contextmanager
def use(user):
    """Route storage (SQLite, seen.json) and Telegram sends to this person."""
    token = CURRENT.set(user)
    db = localdb.DATABASE.set(None if user is None or user.owner else str(user.directory / "pisobot.db"))
    try:
        yield user
    finally:
        localdb.DATABASE.reset(db)
        CURRENT.reset(token)


def seen_path(default):
    user = CURRENT.get()
    if user is None or user.owner:
        return default
    return str(user.directory / "seen.json")


def scoped(name):
    """Per-person key for process-wide caches (cache.json)."""
    user = CURRENT.get()
    return name if user is None or user.owner else f"{name}:{user.chat_id}"


# ---------- registry (always the primary database) ----------

def _primary(sql, *params):
    with use(None):
        return localdb.query(sql, *params)


def _row_user(row, owner_id):
    try:
        params = json.loads(row.get("params") or "{}")
    except ValueError:
        params = {}
    return User(str(row["chat_id"]), row.get("name") or "", str(row["chat_id"]) == owner_id,
                params if isinstance(params, dict) else {})


def owner():
    chat = owner_chat()
    if enabled() and chat:
        rows = _primary("SELECT * FROM users WHERE chat_id = ?", chat)
        if rows:
            return _row_user(rows[0], chat)
    return User(chat, "", True, {})


def get(chat_id):
    chat_id = str(chat_id)
    if chat_id == owner_chat():
        return owner()
    if not enabled():
        return None
    rows = _primary("SELECT * FROM users WHERE chat_id = ?", chat_id)
    return _row_user(rows[0], owner_chat()) if rows else None


def everyone():
    """Owner first, then admitted people in joining order."""
    people = [owner()]
    if enabled():
        rows = _primary("SELECT * FROM users WHERE chat_id != ? ORDER BY created_at, chat_id", owner_chat())
        people += [_row_user(r, owner_chat()) for r in rows][:max(0, max_users() - 1)]
    return people


def register(chat_id, name, username):
    """Admit an invited username's private chat; returns the User or None if not allowed."""
    chat_id = str(chat_id)
    if not enabled() or chat_id == owner_chat():
        return None
    if (username or "").lower() not in invited() or len(everyone()) >= max_users():
        return None
    _primary("INSERT INTO users (chat_id, name, username, params, created_at) VALUES (?, ?, ?, '{}', ?) "
             "ON CONFLICT(chat_id) DO NOTHING", chat_id, name or username, username,
             dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    return get(chat_id)


def remove(chat_id):
    _primary("DELETE FROM users WHERE chat_id = ? AND chat_id != ?", str(chat_id), owner_chat())


def save_params(user, params):
    _primary("INSERT INTO users (chat_id, name, username, params, created_at) VALUES (?, ?, '', ?, ?) "
             "ON CONFLICT(chat_id) DO UPDATE SET params = excluded.params",
             user.chat_id, user.name, json.dumps(params, sort_keys=True),
             dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    user.params = params


# ---------- parameters ----------

def parse(name, raw):
    """Validated value for /set <name> <raw>; raises ValueError with a user-facing message."""
    spec = PARAMS[name]
    cleaned = re.sub(r"[£,\s]|(?:mins?|minutes|miles?|m2|m²|sqm|pcm|/mo)$", "", raw.strip().lower())
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(f"{spec.label}: expected a number") from None
    if not math.isfinite(value) or not spec.low <= value <= spec.high:
        raise ValueError(f"{spec.label}: between {spec.low:g} and {spec.high:g}")
    if spec.choices:
        value = min(spec.choices, key=lambda c: (abs(c - value), c))
    return int(round(value)) if spec.kind is int else value


def effective(user, live=False):
    """Every parameter's value for this person, falling back to the service defaults
    (live=True: to ``config`` as it is now, for the pipeline thread outside any person's run)."""
    base = {spec.attr: getattr(config, spec.attr) for spec in PARAMS.values()} if live else DEFAULTS
    out = {name: user.params.get(name, base[spec.attr]) for name, spec in PARAMS.items()}
    dest = user.params.get("destination") or {}
    out["destination"] = {"name": dest.get("name", DEFAULTS["TARGET_NAME"]),
                          "lat": dest.get("lat", DEFAULTS["TARGET_LAT"]),
                          "lng": dest.get("lng", DEFAULTS["TARGET_LNG"])}
    return out


def overrides(params):
    """config attribute -> value for the parameters a person has explicitly set."""
    out = {PARAMS[name].attr: params[name] for name in PARAMS if name in params}
    dest = params.get("destination")
    if isinstance(dest, dict) and all(k in dest for k in ("name", "lat", "lng")):
        out.update(TARGET_NAME=dest["name"], TARGET_LAT=dest["lat"], TARGET_LNG=dest["lng"])
    return out


def fetch_params(people):
    """Widest portal query that covers everyone: highest budget/radius, lowest beds/min rent.
    Never a destination: portal searches stay centred on Oxford Circus."""
    values = [effective(u, live=True) for u in people]
    if not values:
        return {}
    return {"budget": max(v["budget"] for v in values), "radius": max(v["radius"] for v in values),
            "beds": min(v["beds"] for v in values), "minrent": min(v["minrent"] for v in values)}


@contextlib.contextmanager
def criteria(params):
    """Apply a person's parameters to ``config`` for the duration of their run. Only the
    pipeline thread does this; the Telegram thread reads DEFAULTS, never ``config``."""
    changes = overrides(params or {})
    previous = {attr: getattr(config, attr) for attr in changes}
    try:
        for attr, value in changes.items():
            setattr(config, attr, value)
        yield
    finally:
        for attr, value in previous.items():
            setattr(config, attr, value)


def describe(user):
    values = effective(user)
    lines = []
    for name, spec in PARAMS.items():
        value = values[name]
        shown = f"£{value:,.0f}" if spec.unit.startswith("£") else f"{value:g}"
        unit = "" if spec.unit.startswith("£") else f" {spec.unit}".rstrip()
        mark = "" if name in user.params else " <i>(default)</i>"
        lines.append(f"• <b>{name}</b> {shown}{unit}{mark} — {spec.label}")
    dest = values["destination"]
    mark = "" if user.params.get("destination") else " <i>(default)</i>"
    lines.append(f"• <b>destination</b> {html.escape(str(dest['name']))}{mark} — commute target")
    return "\n".join(lines)


def in_london(lat, lng):
    return LONDON[0] <= lat <= LONDON[1] and LONDON[2] <= lng <= LONDON[3]
