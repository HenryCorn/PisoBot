"""Work package 3 (VPS, STORAGE=local): a person's renter profile, the enquiry draft built from it,
how a home's available date fits their move-in window, and the urgency tier of a new match.

The profile and the urgent switch are stored by tracker (renter table), never in the person's search
settings, so editing them does not re-baseline the search. The bot never sends anything to an agent:
the enquiry is a draft the person copies into the portal or their own email.
"""
import datetime as dt
import html
import logging
import math
import os
import re

from . import archive, config, ranking, tracker

LOG = logging.getLogger(__name__)
# Round seven keeps the incomplete feature unavailable regardless of deployment configuration.
# Re-enable only after the alert-path, replay and privacy contract has been independently accepted.
READY_PACK_APPROVED = False
FIELDS = {   # name: (label, example)
    "name": ("Name", "Alex Smith"),
    "movein": ("Move-in", "1 Dec, 1/12-15/12, December, late December or asap"),
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
PART = {"early": (1, 10), "beginning of": (1, 10), "start of": (1, 10), "mid": (11, 20),
        "middle of": (11, 20), "late": (21, 31), "end of": (21, 31)}   # day ranges of a month part


def ready_enabled():
    return READY_PACK_APPROVED and os.environ.get("READY_PACK") == "1"


def field_name(word):
    word = word.lower().strip()
    return word if word in FIELDS else ALIASES.get(word)


def profile():
    return {name: tracker.value(f"profile.{name}") for name in FIELDS}


def set_field(name, raw, today=None, *, warnings=None):
    """Store one profile fact; 'clear' removes it. Returns the stored text. The move-in answer is
    normalised to 'YYYY-MM-DD..YYYY-MM-DD' (ValueError if it cannot be read)."""
    raw = " ".join(raw.split())[:300]
    if ready_enabled() and name == "job":
        from . import readydb
        try:
            readydb.invalidate_tenure()
        except Exception:
            LOG.warning("Could not invalidate readiness tenure for a job change")
            if warnings is not None:
                warnings.append("readiness tenure could not be invalidated; re-enter it before relying on it")
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
    m = re.fullmatch(r"(?:(?:the )?(early|beginning of|start of|mid|middle of|late|end of)[ -])?"
                     r"([a-z]{3})[a-z]*(?: (\d{4}))?", text)
    if m and m[2] in MONTHS:
        month, year = MONTHS.index(m[2]) + 1, int(m[3] or today.year)
        first, last = PART.get(m[1], (1, 31))

        def span(year):
            days = (dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)).day
            return dt.date(year, month, first), dt.date(year, month, min(last, days))
        if not m[3] and span(year)[1] < today:
            year += 1
        return span(year)[1 if end else 0]
    raise ValueError(text)


def parse_window(text, today=None):
    """'asap', '1 Dec', '1/12-15/12', '2026-12-01..2027-01-15', 'December', 'late December' (21st to
    month end; early = 1-10, mid = 11-20) -> (start, end) dates."""
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
                         "<code>December</code>, <code>late December</code> or <code>asap</code>") from None
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


def movein_needs_confirmation(span):
    """A stored 30-day span may be the approximation used for an ASAP answer."""
    return bool(span and (span[1] - span[0]).days == 30)


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

_NAME_WORD = r"[^\W\d_]+(?:[-'’][^\W\d_]+)*['’]?"
_UNSAFE_DRAFT_WORDS = re.compile(
    r"\b(?:offer(?:ed|ing|s)?|bid(?:ding|s)?|pay(?:ment|ments|ing|s)?|paid|"
    r"advance(?:d)?|upfront|rent(?:al|s|ing)?|month(?:s|ly)?|pound(?:s)?|"
    r"above|over|extra|additional|more|higher|double|exceed(?:s|ed|ing)?|"
    r"advertised|asking|cost|price|cash|deposit|i|we|my|our|will|would|can|could|shall)\b", re.I)
_OCCUPATIONS = {
    "accountant", "administrator", "analyst", "architect", "artist", "chef", "civil servant",
    "consultant", "designer", "doctor", "driver", "engineer", "journalist", "lawyer", "manager",
    "nurse", "pharmacist", "researcher", "scientist", "social worker", "software developer",
    "software engineer", "student", "teacher",
}
_HOUSEHOLDS = {
    "one person", "one adult", "single adult", "two people", "two adults", "three adults",
    "couple", "a couple", "couple, no children", "couple with no children",
    "two adults, no children", "couple, one child", "couple, two children", "family",
    "me and my partner",
}
_STREET_TYPES = (
    "street|st|road|rd|avenue|ave|lane|ln|drive|dr|court|close|place|square|gardens|"
    "way|terrace|row|mews|crescent|grove|park|walk|hill"
)


def _safe_name(raw):
    if not isinstance(raw, str):
        return None
    value = " ".join(raw.split())
    if (len(value) > 80 or _UNSAFE_DRAFT_WORDS.search(value) or
            not re.fullmatch(rf"{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,3}}", value)):
        return None
    return value


def _safe_occupation(raw):
    if not isinstance(raw, str):
        return None
    value = " ".join(raw.split())
    if len(value) > 120 or _UNSAFE_DRAFT_WORDS.search(value):
        return None
    role, comma, hours = value.partition(",")
    hours = hours.strip()
    if comma and hours.lower() not in ("full time", "full-time", "part time", "part-time"):
        return None
    parts = re.split(r"\s+at\s+", role.strip(), maxsplit=1, flags=re.I)
    if (len(parts) == 2 and
            not re.fullmatch(rf"{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,3}}", parts[1])):
        return None
    if parts[0].lower() not in _OCCUPATIONS:
        return None
    # The employer is free text; copy only the recognised role and hours.
    return parts[0] + (f", {hours}" if hours else "")


def _safe_household(raw):
    if not isinstance(raw, str):
        return None
    value = " ".join(raw.split())
    return value if value.lower() in _HOUSEHOLDS else None


def _safe_address(raw):
    if not isinstance(raw, str) or len(raw) > 200:
        return None
    parts = [" ".join(part.split()) for part in raw.split(",")]
    pattern = (rf"(?:\d{{1,4}}[a-z]?(?:-\d{{1,4}}[a-z]?)?\s+)?"
               rf"{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,4}}\s+(?:{_STREET_TYPES})\.?")
    streets = units = 0
    for part in parts:
        if re.fullmatch(r"(?:flat|apartment|unit|studio)\s+[a-z0-9-]{1,6}", part, re.I):
            units += 1
        elif not _UNSAFE_DRAFT_WORDS.search(part) and re.fullmatch(pattern, part, re.I):
            streets += 1
        elif part.lower() != "london" and not re.fullmatch(r"[a-z]{1,2}\d[a-z\d]?\s*\d[a-z]{2}", part, re.I):
            return None
    return ", ".join(parts) if streets == 1 and units <= 1 else None


def _safe_smoker(raw):
    if not isinstance(raw, str):
        return None
    return {"yes": "yes", "smoker": "yes", "no": "no", "non-smoker": "no",
            "non smoker": "no", "nonsmoker": "no"}.get(" ".join(raw.lower().split()))


def _safe_availability(raw):
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if text in ("now", "immediately", "available now"):
        return "available immediately"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return "available from " + dt.date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _ready_details(facts, listing):
    return {"name": _safe_name(facts["name"]), "job": _safe_occupation(facts["job"]),
            "household": _safe_household(facts["household"]), "smoker": _safe_smoker(facts["smoker"]),
            "address": _safe_address(listing.get("address"))}


def _portal(key):
    return PORTALS.get(str(key).split(":", 1)[0], "your website")


def draft(listing, today=None):
    """(html, missing) for the Enquiry text button. listing: the ``listings`` row ({"key", "address",
    "url"}) plus, when archived, "available". The draft is plain text in <pre> so Telegram shows a
    copy button; facts missing from the profile become [placeholders]."""
    today = today or _today()
    if ready_enabled():
        try:
            return _draft(listing, today, readiness=True)
        except Exception:
            LOG.warning("Application readiness draft unavailable; using a generic enquiry")
            return ("⚠️ Application readiness is unavailable; this generic draft contains no profile details. "
                    "Try /ready again.\n✍️ <b>Enquiry draft</b>\n"
                    "<pre>Hello,\n\nI'm interested in your advertised rental. Is it still available, "
                    "and when can I view it?\n\nMany thanks,\n[your name]</pre>\n"
                    "<i>Copy it yourself; PisoBot never contacts agents.</i>", ["name"])
    return _draft(listing, today)


def _draft(listing, today, readiness=False):
    pack = None
    if readiness:
        from . import ready, readydb
        facts = readydb.profile(FIELDS)
        pack = ready.load(facts)
        details = _ready_details(facts, listing)
        facts = {name: None for name in FIELDS}
        facts.update({name: details[name] for name in ("name", "job", "household", "smoker")})
        facts["movein"] = "set" if pack.movein else None
    else:
        facts = profile()
    missing = [FIELDS[name][0].lower() for name in ("name", "movein", "job", "household") if not facts[name]]
    address = ((details["address"] or "[property address]") if readiness else
               (listing.get("address") or "the property").split(",")[0].strip())
    span = pack.movein if readiness else window(today)
    day = None if readiness else _available(listing.get("available"), today)
    lines = ["Hello,", "",
             f"I'm interested in renting {address}, which I saw on {_portal(listing.get('key'))}. "
             "Is it still available?"]
    available = _safe_availability(listing.get("available")) if readiness else None
    if available:
        lines.append(f"The last archived advert said it was {available}; could you confirm?")
    if span:
        want = ready.movein_text(span) if readiness else show_window(*span)
        if day:
            lines.append(f"I see it is available from {_day(max(day, today))}; I'm looking to move in {want}.")
        elif available:
            lines.append(f"I'm looking to move in {want}.")
        else:
            lines.append(f"Could you confirm the date it is available from? I'm looking to move in {want}.")
    else:
        date = "[confirm move-in date]" if readiness and pack.ambiguous_movein else "[move-in date]"
        if available:
            lines.append(f"I'm looking to move in {date}.")
        else:
            lines.append(f"Could you confirm the date it is available from? I'm looking to move in {date}.")
    about = []
    if readiness:
        about.append(f"Occupation: {facts['job'] or '[if applicable]'}")
    elif facts["job"]:
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
    lines += ["", "A little about me:", *[f"- {item}" for item in about]]
    if readiness:
        lines += ["", ready.application_summary(pack)]
    lines += ["", ("Could you let me know your earliest available viewing slots?" if readiness else
                   "Could you let me know your earliest available viewing slots? I'm flexible and can view at "
                   "short notice."), "", "Many thanks,", facts["name"] or "[your name]"]
    text = "\n".join(lines)
    title = "selected property" if readiness and not details["address"] else html.escape(address[:60])
    out = (f"✍️ <b>Enquiry draft</b> · {title}\n<pre>{html.escape(text, quote=False)}</pre>\n"
           "<i>Copy it into the portal's contact form or your email; PisoBot never contacts agents.</i>")
    if readiness:
        out += ("\n<i>Only recognised profile details are included; review and edit all details and "
                "placeholders before sending. "
                "Do not bid above the advertised rent or offer more than one month's rent in advance.</i>"
                + ready.extras(pack, listing.get("ready_rent"), details))
    elif missing:
        out += f"\nAdd {', '.join(missing)} with /profile to fill the gaps."
    return out, missing


def show_profile():
    facts = profile()
    lines = ["<b>👤 Renter profile</b> <i>(used for enquiry drafts and move-in fit)</i>"]
    for name, (label, example) in FIELDS.items():
        if name == "notes" and ready_enabled():
            example = "anything important"
        current = facts[name]
        if name == "movein" and current:
            span = window()
            if ready_enabled() and movein_needs_confirmation(span):
                current = "needs confirmation; a 30-day window may be an estimate"
            else:
                current = show_window(*span) if span else current
        shown = html.escape(current) if current else f"<i>not set</i> (e.g. {html.escape(example)})"
        lines.append(f"{label}: {shown}  <code>/profile {name}</code>")
    lines.append("\nSet one with <code>/profile name Alex Smith</code>; <code>/profile pets clear</code> removes it.")
    if ready_enabled():
        lines.append("Use /ready for application details and affordability. Only recognised profile "
                     "details are included in the draft; review and edit it before sending.")
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
