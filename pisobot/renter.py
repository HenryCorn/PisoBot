"""Work package 3 (VPS, STORAGE=local): a person's renter profile, the enquiry draft built from it,
how a home's available date fits their move-in window, and the urgency tier of a new match.

The profile and the urgent switch are stored by tracker (renter table), never in the person's search
settings, so editing them does not re-baseline the search. The bot never sends anything to an agent:
the enquiry is a draft the person copies into the portal or their own email.
"""
import datetime as dt
import html
import math
import re

from . import archive, config, ranking, tracker

FIELDS = {   # name: (label, example)
    "name": ("Name", "Alex Smith"),
    "movein": ("Move-in", "1 Dec, 1/12-15/12, December or asap"),
    "job": ("Occupation", "nurse at St Thomas', full time"),
    "household": ("Household", "couple, no children"),
    "pets": ("Pets", "none"),
    "smoker": ("Smoker", "no"),
    "references": ("References", "employer and previous landlord"),
    "notes": ("Anything else", "can pay 6 months upfront"),
}
ALIASES = {"move": "movein", "move-in": "movein", "move_in": "movein", "occupation": "job", "work": "job",
           "pet": "pets", "smoking": "smoker", "refs": "references", "reference": "references",
           "other": "notes", "note": "notes", "else": "notes"}
PORTALS = {"rm": "Rightmove", "zp": "Zoopla", "orent": "OpenRent", "sr": "SpareRoom", "otmr": "OnTheMarket",
           "otm": "OnTheMarket"}
TOP_HOURS = 3          # first listed within this many hours: top tier
TOP_SHARE = 0.2        # ... or a score in the best fifth of the person's recent matches
TOP_DAYS = 14
TOP_MIN_SAMPLE = 10    # fewer recent matches than this: only the "just listed" rule applies
NEAR_DAYS = 14         # available this close to the move-in window still counts as a near fit
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def field_name(word):
    word = word.lower().strip()
    return word if word in FIELDS else ALIASES.get(word)


def profile():
    return {name: tracker.value(f"profile.{name}") for name in FIELDS}


def set_field(name, raw, today=None):
    """Store one profile fact; 'clear' removes it. Returns the stored text. The move-in answer is
    normalised to 'YYYY-MM-DD..YYYY-MM-DD' (ValueError if it cannot be read)."""
    raw = " ".join(raw.split())[:300]
    if raw.lower() == "clear" or not raw:
        tracker.drop(f"profile.{name}")
        return None
    if name == "movein":
        raw = "..".join(d.isoformat() for d in parse_window(raw, today))
    tracker.put(f"profile.{name}", raw)
    return raw


def _today():
    return dt.datetime.now(tracker.LONDON).date()


def _date(text, today, end=False):
    text = text.strip().lower().rstrip(".")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return dt.date.fromisoformat(text)
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", text)
    if m:
        year = int(m[3]) + (2000 if m[3] and int(m[3]) < 100 else 0) if m[3] else today.year
        day = dt.date(year, int(m[2]), int(m[1]))
        return day if m[3] or day >= today else day.replace(year=year + 1)
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? ([a-z]{3})[a-z]*(?: (\d{4}))?", text)
    if m and m[2] in MONTHS:
        day = dt.date(int(m[3] or today.year), MONTHS.index(m[2]) + 1, int(m[1]))
        return day if m[3] or day >= today else day.replace(year=day.year + 1)
    m = re.fullmatch(r"(?:early |mid |late |end of )?([a-z]{3})[a-z]*(?: (\d{4}))?", text)
    if m and m[1] in MONTHS:
        month, year = MONTHS.index(m[1]) + 1, int(m[2] or today.year)
        if not m[2] and (year, month) < (today.year, today.month):
            year += 1
        last = (dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)).day
        return dt.date(year, month, last if end else 1)
    raise ValueError(text)


def parse_window(text, today=None):
    """'asap', '1 Dec', '1/12-15/12', '2026-12-01..2027-01-15', 'December' -> (start, end) dates."""
    today = today or _today()
    text = text.strip().lower()
    if text in ("asap", "now", "immediately", "anytime", "any time"):
        return today, today + dt.timedelta(days=30)
    parts = re.split(r"\s*(?:\.\.|–|—|\bto\b|\buntil\b| - )\s*", text)
    if len(parts) == 1 and re.fullmatch(r"\d{1,2}/\d{1,2}(?:/\d{2,4})?-\d{1,2}/\d{1,2}(?:/\d{2,4})?", text):
        parts = text.split("-")
    try:
        if len(parts) == 1:
            start, end = _date(parts[0], today), _date(parts[0], today, end=True)
        elif len(parts) == 2:
            start, end = _date(parts[0], today), _date(parts[1], today, end=True)
        else:
            raise ValueError
    except (ValueError, IndexError):
        raise ValueError("I couldn't read that move-in date; try <code>1 Dec</code>, <code>1/12-15/12</code>, "
                         "<code>December</code> or <code>asap</code>") from None
    if end < start:
        raise ValueError("The window ends before it starts")
    return start, end


def window(today=None):
    raw = tracker.value("profile.movein")
    if not raw:
        return None
    try:
        start, end = (dt.date.fromisoformat(part) for part in raw.split(".."))
    except ValueError:
        return None
    return start, end


def _available(text, today):
    text = str(text or "").strip().lower()
    if text in ("now", "immediately", "available now"):
        return today
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _day(d):
    return f"{d.day} {d.strftime('%b')}"


def show_window(start, end):
    return _day(start) if start == end else f"{_day(start)}–{_day(end)}"


def fit(available, today=None):
    """(symbol, text) for the move-in fit of a home available on ``available`` (ISO date), or None
    when the person has no move-in window yet."""
    today = today or _today()
    span = window(today)
    if span is None:
        return None
    start, end = span
    want = show_window(start, end)
    day = _available(available, today)
    if day is None:
        return "❔", f"available date not given (you: {want})"
    # A home that is already free fits an "as soon as possible" window that starts today
    day = max(day, today)
    if start <= day <= end:
        return "✅", f"available {_day(day)} fits your move-in ({want})"
    gap = (start - day).days if day < start else (day - end).days
    side = "before" if day < start else "after"
    mark = "🟡" if gap <= NEAR_DAYS else "⚠️"
    return mark, f"available {_day(day)}, {gap} day{'s' * (gap != 1)} {side} your move-in ({want})"


def fit_line(available):
    """The card's move-in line, or "" when the person has no move-in window."""
    try:
        found = fit(available)
    except Exception:
        return ""
    return f"{found[0]} {found[1]}" if found else ""


# ---------- enquiry draft ----------

def _portal(key):
    return PORTALS.get(str(key).split(":", 1)[0], "your website")


def draft(listing, today=None):
    """(html, missing) for the Enquiry text button. listing: the ``listings`` row ({"key", "address",
    "url"}) plus, when archived, "available". The draft is plain text in <pre> so Telegram shows a
    copy button; facts missing from the profile become [placeholders]."""
    today = today or _today()
    facts = profile()
    missing = [FIELDS[name][0].lower() for name in ("name", "movein", "job", "household") if not facts[name]]
    address = (listing.get("address") or "the property").split(",")[0].strip()
    span = window(today)
    day = _available(listing.get("available"), today)
    lines = ["Hello,", "",
             f"I'm interested in renting {address}, which I saw on {_portal(listing.get('key'))}. "
             "Is it still available?"]
    if span:
        want = show_window(*span)
        if day:
            lines.append(f"I see it is available from {_day(max(day, today))}; I'm looking to move in {want}.")
        else:
            lines.append(f"Could you confirm the date it is available from? I'm looking to move in {want}.")
    else:
        lines.append("Could you confirm the date it is available from? I'm looking to move in [move-in date].")
    about = []
    if facts["job"]:
        about.append(f"I work as {facts['job']}" if not facts["job"].lower().startswith(("i ", "a ", "an "))
                     else facts["job"])
    else:
        about.append("I work as [occupation]")
    about.append(f"Household: {facts['household'] or '[who will live there]'}")
    if facts["pets"]:
        about.append(f"Pets: {facts['pets']}")
    if facts["smoker"]:
        about.append(f"Smoker: {facts['smoker']}")
    if facts["references"]:
        about.append(f"References: {facts['references']}")
    if facts["notes"]:
        about.append(facts["notes"])
    lines += ["", "A little about me:", *[f"- {item}" for item in about], "",
              "Could you let me know your earliest available viewing slots? I'm flexible and can view at "
              "short notice.", "", "Many thanks,", facts["name"] or "[your name]"]
    text = "\n".join(lines)
    out = (f"✍️ <b>Enquiry draft</b> · {html.escape(address[:60])}\n<pre>{html.escape(text, quote=False)}</pre>\n"
           "<i>Copy it into the portal's contact form or your email; PisoBot never contacts agents.</i>")
    if missing:
        out += f"\nAdd {', '.join(missing)} with /profile to fill the gaps."
    return out, missing


def show_profile():
    facts = profile()
    lines = ["<b>👤 Renter profile</b> <i>(used for enquiry drafts and move-in fit)</i>"]
    for name, (label, example) in FIELDS.items():
        current = facts[name]
        if name == "movein" and current:
            span = window()
            current = show_window(*span) if span else current
        shown = html.escape(current) if current else f"<i>not set</i> (e.g. {html.escape(example)})"
        lines.append(f"{label}: {shown}  <code>/profile {name}</code>")
    lines.append("\nSet one with <code>/profile name Alex Smith</code>; <code>/profile pets clear</code> removes it.")
    return "\n".join(lines)


# ---------- urgency ----------

def urgent_on():
    return tracker.value("urgent") == "on"


def set_urgent(on):
    tracker.put("urgent", "on" if on else "off")


def _numbers(all_in, travel, crime, scale):
    w = config.RANK_WEIGHTS
    return (w["cost"] * all_in / scale + w["travel"] * (travel or 0) / config.MAX_TRAVEL_MINUTES
            + w["crime"] * (crime or 0) / config.MAX_CRIME_SCORE)


def tier(l, chat_id, at=None):
    """"top" when the home was first listed within TOP_HOURS, or its numbers (all-in cost, commute,
    crime, weighted as ranking.score) are in the best fifth of this person's matched verdicts over
    the last TOP_DAYS days; else None. Taste and red-flag adjustments are left out because verdicts
    do not store them, so the comparison is like for like."""
    at = at or dt.datetime.now(dt.timezone.utc)
    if l.tenure != "rent":
        return None
    if l.first_listed_at:
        try:
            listed = dt.datetime.fromisoformat(l.first_listed_at)
            if dt.timedelta(0) <= at - listed <= dt.timedelta(hours=TOP_HOURS):
                return "top"
        except (TypeError, ValueError):
            pass
    if l.monthly_cost is None or not archive.enabled():
        return None
    scale = ranking.cost_scale(l)
    scores = sorted(_numbers(v["all_in"], v["travel_minutes"], v["crime_score"], scale)
                    for v in archive.verdicts_since(chat_id, TOP_DAYS, at.isoformat())
                    if v["outcome"] in ("matched", "unsent") and v["all_in"] is not None)
    if len(scores) < TOP_MIN_SAMPLE:
        return None
    cut = scores[max(0, math.ceil(len(scores) * TOP_SHARE) - 1)]
    return "top" if _numbers(l.monthly_cost, l.travel_minutes, l.crime_score, scale) <= cut + 1e-9 else None
