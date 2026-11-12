"""User-entered application facts and an offline, illustrative affordability check."""
import datetime as dt
import hashlib
import html
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from . import readydb, renter


FIELDS = {
    "income": ("Gross household income/year",
               "What is your combined gross household income per year? Give a band, an upper limit, "
               "or an exact amount, e.g. <code>£40k-£50k</code> or <code>under £75k</code>.",
               "£40k-£50k"),
    "employment": ("Employment type",
                   "Employment type? Reply employed, full-time, part-time, self-employed, contractor, "
                   "student, retired, unemployed or other.", "employed"),
    "employment_length": ("Time in employment",
                          "How long in this employment? Reply e.g. <code>2 years</code>, "
                          "<code>6 months</code> or <code>not applicable</code>.", "2 years"),
    "references": ("References",
                   "Are references available? Reply yes, no, partial, employer, landlord or both.", "both"),
    "guarantor": ("Guarantor available", "Do you have a guarantor? Reply yes or no.", "yes"),
    "guarantor_income": ("Guarantor gross income/year",
                         "Guarantor's gross annual income? Give a band, upper limit or exact amount.",
                         "£40k-£50k"),
    "cash": ("Cash for deposit and one month's rent",
             "How much cash is available for the deposit and one month's rent? Reply with an amount in £.",
             "£3,000"),
    "flexibility": ("Move-in date flexibility",
                    "Is your move-in date flexible? Reply yes, somewhat or no.", "somewhat"),
    "pets": ("Pets", "Any pets? Reply no, yes, cat, cats, dog or dogs.", "no"),
    "tenancy": ("Tenancy length wanted",
                "How long a tenancy do you want? Reply e.g. <code>12 months</code> or "
                "<code>2 years</code>.", "12 months"),
    "documents": ("Documents ready",
                  "Are your application documents ready? Reply yes, no or partial.", "partial"),
}
SHARED_FIELDS = {"references", "pets"}
CHOICES = {
    "employment": {
        "employed": "employed", "employee": "employed",
        "full-time": "full-time", "full time": "full-time", "employed full-time": "full-time",
        "employed full time": "full-time", "part-time": "part-time", "part time": "part-time",
        "employed part-time": "part-time", "employed part time": "part-time",
        "self-employed": "self-employed", "self employed": "self-employed",
        "contractor": "contractor", "student": "student", "retired": "retired",
        "unemployed": "unemployed", "other": "other",
    },
    "references": {
        "yes": "yes", "available": "yes", "no": "no", "not yet": "no",
        "not available": "no", "partial": "partial", "partly": "partial",
        "partly available": "partial", "employer": "employer", "landlord": "landlord",
        "both": "both", "employer and landlord": "both",
        "employer and previous landlord": "employer_previous_landlord",
    },
    "guarantor": {"yes": "yes", "no": "no"},
    "flexibility": {
        "yes": "yes", "flexible": "yes", "somewhat": "somewhat",
        "somewhat flexible": "somewhat", "no": "no", "not flexible": "no",
    },
    "pets": {
        "no": "none", "none": "none", "no pets": "none", "yes": "yes",
        "cat": "cat", "cats": "cats", "dog": "dog", "dogs": "dogs",
        "one cat": "one cat", "1 cat": "one cat", "one indoor cat": "one cat",
        "one dog": "one dog", "1 dog": "one dog",
    },
    "documents": {"yes": "yes", "ready": "yes", "no": "no", "not yet": "no",
                  "partial": "partial", "partly": "partial"},
}
DISPLAY = {
    "references": {"yes": "available", "no": "not available", "partial": "partly available",
                   "employer": "employer", "landlord": "landlord", "both": "employer and landlord",
                   "employer_previous_landlord": "employer and previous landlord"},
    "flexibility": {"yes": "flexible", "somewhat": "somewhat flexible", "no": "not flexible"},
    "documents": {"yes": "ready", "no": "not ready", "partial": "partly ready"},
}
MONEY = r"£?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?\s*[kK]?"
MAX_AMOUNT = Decimal("1000000000")
EMPLOYMENT_LENGTH_FOR = readydb.EMPLOYMENT_LENGTH_FOR


@dataclass(frozen=True)
class Band:
    low: Decimal
    high: Decimal | None
    high_exclusive: bool
    text: str


@dataclass(frozen=True)
class Pack:
    values: dict
    invalid: set
    movein: tuple | None
    invalid_movein: bool
    ambiguous_movein: bool


def _money(raw):
    text = str(raw).strip()
    if not re.fullmatch(MONEY, text):
        raise ValueError("Use an amount in pounds, e.g. £3,000 or £40k")
    number = text.replace("£", "").replace(",", "").strip().lower()
    multiplier = Decimal(1000) if number.endswith("k") else Decimal(1)
    if multiplier != 1:
        number = number[:-1].strip()
    try:
        amount = Decimal(number) * multiplier
    except InvalidOperation:
        raise ValueError("Use an amount in pounds, e.g. £3,000 or £40k") from None
    if not amount.is_finite() or amount < 0 or amount > MAX_AMOUNT:
        raise ValueError("Use an amount between £0 and £1 billion")
    return amount


def money_text(amount):
    if amount == amount.to_integral_value():
        return f"£{int(amount):,}"
    return f"£{amount:,.2f}"


def income_band(raw):
    text = " ".join(str(raw).strip().split())
    match = re.fullmatch(rf"(?:under|less than|<)\s*({MONEY})", text, re.I)
    if match:
        high = _money(match[1])
        if high <= 0:
            raise ValueError("An income upper limit must be above £0")
        return Band(Decimal(0), high, True, f"under {money_text(high)}")
    match = re.fullmatch(rf"(?:up to|at most|<=|≤)\s*({MONEY})", text, re.I)
    if match:
        high = _money(match[1])
        return Band(Decimal(0), high, False, f"up to {money_text(high)}")
    match = (re.fullmatch(rf"(?:at least|over|>=|≥)\s*({MONEY})", text, re.I)
             or re.fullmatch(rf"({MONEY})\s*\+", text, re.I))
    if match:
        low = _money(match[1])
        return Band(low, None, False, f"at least {money_text(low)}")
    match = re.fullmatch(rf"({MONEY})\s*(?:-|–|—|\bto\b)\s*({MONEY})", text, re.I)
    if match:
        low, high = _money(match[1]), _money(match[2])
        if high < low:
            raise ValueError("The income band must start at or below its upper limit")
        return Band(low, high, False, f"{money_text(low)}–{money_text(high)}")
    try:
        amount = _money(text)
    except ValueError:
        raise ValueError("Use an annual income band like £40k-£50k, under £75k, or an exact amount") from None
    return Band(amount, amount, False, money_text(amount))


def _length(raw, allow_na=False):
    text = " ".join(str(raw).lower().split())
    if allow_na and text in ("n/a", "na", "not applicable"):
        return "n/a"
    match = re.fullmatch(r"(\d{1,4})\s*(months?|mos?|years?|yrs?)", text)
    if not match:
        raise ValueError("Use a duration such as 6 months or 2 years")
    months = int(match[1]) * (12 if match[2].startswith(("year", "yr")) else 1)
    if months > 1200 or (not allow_na and months == 0):
        raise ValueError("Use a duration of at most 100 years" if allow_na
                         else "Use a tenancy duration from 1 month to 100 years")
    return months


def _parse(name, raw):
    if name in ("income", "guarantor_income"):
        return income_band(raw)
    if name == "cash":
        return _money(raw)
    if name in ("employment_length", "tenancy"):
        return _length(raw, allow_na=name == "employment_length")
    value = CHOICES[name].get(" ".join(str(raw).lower().split()))
    if value is None:
        raise ValueError(f"Unrecognised answer for {FIELDS[name][0].lower()}; use the choices in the question")
    return value


def _key(name):
    return name if name in SHARED_FIELDS else f"ready_{name}"


def value_text(name, value):
    if isinstance(value, Band):
        return value.text
    if name == "cash":
        return money_text(value)
    if name in ("employment_length", "tenancy"):
        return "not applicable" if value == "n/a" else f"{value} month{'s' if value != 1 else ''}"
    return DISPLAY.get(name, {}).get(value, value)


def _employment_context(employment, job):
    # Tied to this process: a later run may have changed the job with the pack off, unseen.
    fingerprint = hashlib.sha256(job.encode("utf-8")).hexdigest() if job else None
    return json.dumps([employment, fingerprint, readydb.tenure_session()], separators=(",", ":"))


def _store(name, text):
    if text is None:
        if readydb.value(f"profile.{_key(name)}") is not None:
            readydb.drop(f"profile.{_key(name)}")
            readydb.discard_outboxes()
    else:
        readydb.put(f"profile.{_key(name)}", text)


def set_field(name, raw):
    """Validate and atomically save readiness facts outside the alert database."""
    if name not in FIELDS:
        raise ValueError("Unknown readiness field")
    if name in SHARED_FIELDS:
        # One value in every mode: the renter profile, which only /profile changes.
        raise ValueError(f"{FIELDS[name][0]} comes from your renter profile; use /profile {name}")
    text = " ".join(raw.split())
    with readydb.transaction():
        if text.lower() == "clear":
            if name == "guarantor":
                _store("guarantor_income", None)
            elif name == "employment":
                _store("employment_length", None)
                readydb.drop(EMPLOYMENT_LENGTH_FOR)
            _store(name, None)
            if name == "employment_length":
                readydb.drop(EMPLOYMENT_LENGTH_FOR)
            return None
        if name == "guarantor_income" and readydb.value("profile.ready_guarantor") != "yes":
            raise ValueError("Set guarantor to yes before entering their income")
        employment = None
        if name == "employment_length":
            previous = readydb.value(f"profile.{_key('employment')}")
            try:
                employment = _parse("employment", previous or "")
            except ValueError:
                raise ValueError("Set employment before entering its length") from None
        value = _parse(name, text)
        stored = value.text if isinstance(value, Band) else (
            f"{value} months" if name in ("employment_length", "tenancy") and value != "n/a"
            else str(value))
        if name == "cash":
            stored = money_text(value)
        if name == "guarantor" and value == "no":
            _store("guarantor_income", None)
        if name == "employment":
            previous = readydb.value(f"profile.{_key(name)}")
            try:
                changed = not previous or _parse(name, previous) != value
            except ValueError:
                changed = True
            if changed:
                _store("employment_length", None)
                readydb.drop(EMPLOYMENT_LENGTH_FOR)
        _store(name, stored)
        if name == "employment_length":
            job = readydb.profile(renter.FIELDS)["job"]
            readydb.put(EMPLOYMENT_LENGTH_FOR, _employment_context(employment, job))
        return value


def load(facts=None):
    with readydb.transaction():
        return _load(facts)


def _load(facts):
    facts = readydb.profile(renter.FIELDS) if facts is None else facts
    values, invalid = {}, set()
    for name in FIELDS:
        raw = facts.get(name) if name in SHARED_FIELDS else readydb.value(f"profile.{_key(name)}")
        if not raw:
            values[name] = None
            continue
        try:
            values[name] = _parse(name, raw)
        except ValueError:
            values[name] = None
            invalid.add(name)
    # A duration counts only if confirmed in this process for this employment and job.
    if (values["employment_length"] is not None and
            (values["employment"] is None or
             readydb.value(EMPLOYMENT_LENGTH_FOR) !=
             _employment_context(values["employment"], facts.get("job")))):
        values["employment_length"] = None
        invalid.add("employment_length")
        readydb.drop(EMPLOYMENT_LENGTH_FOR)
    span = None
    if facts.get("movein"):
        try:
            start, end = facts["movein"].split("..")
            span = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        except (ValueError, TypeError):
            pass
    if span and span[1] < span[0]:
        span = None
    invalid_movein = bool(facts.get("movein") and not span)
    ambiguous_movein = renter.movein_needs_confirmation(span)
    return Pack(values, invalid, None if ambiguous_movein else span, invalid_movein, ambiguous_movein)


def missing(pack, after=None):
    names = list(FIELDS)
    start = names.index(after) + 1 if after in names else 0
    return [name for name in names[start:]
            if pack.values[name] is None and name not in SHARED_FIELDS
            and (name != "employment_length" or pack.values["employment"] is not None)
            and (name != "guarantor_income" or pack.values["guarantor"] == "yes")]


def movein_text(span):
    start, end = span
    return start.isoformat() if start == end else f"{start.isoformat()} to {end.isoformat()}"


def rent_amount(raw):
    if type(raw) not in (str, int, float, Decimal):
        raise ValueError("Give the advertised monthly rent in pounds")
    try:
        amount = _money(raw)
    except ValueError:
        raise ValueError("Give a valid advertised monthly rent, e.g. £1,600") from None
    if amount <= 0:
        raise ValueError("The advertised monthly rent must be above £0")
    return amount


def affordability(pack, rent=None, archived=False):
    rule = ("Rule: gross annual household income ≥ 30 × advertised monthly rent "
            "(2.5 × monthly gross); some agents use 3 ×. Agents vary.")
    try:
        amount = rent_amount(rent)
    except ValueError:
        return (rule + "\nAdvertised monthly rent is unavailable; cannot assess affordability or "
                "whether a guarantor may be needed. Use <code>/ready rent 1600</code> or tap "
                "Enquiry text on a home's card.")
    source = ("Last archived advertised rent" if archived else "Your entered advertised rent")
    threshold = amount * 30
    result = (f"{source}: {money_text(amount)}/month; confirm the current advert. "
              f"30 × rent = {money_text(threshold)} gross/year.")
    band = pack.values["income"]
    if band is None:
        result += " Income is not provided or not recognised, so the test and guarantor need are unknown."
    elif band.low >= threshold:
        result += (" Your band meets this 30 × rule; it does not indicate a guarantor is needed. "
                   "An agent may apply a different test.")
    elif band.high is not None and (band.high < threshold or (
            band.high == threshold and band.high_exclusive)):
        result += (" Your band is below this 30 × rule; a guarantor may be needed. "
                   "Ask the agent about its requirements.")
    else:
        result += (" Your band crosses this threshold, so the result and guarantor need are unknown. "
                   "Enter a narrower band or exact income to check.")
    return rule + "\n" + result


def checklist(pack, rent=None, archived=False, selected=None):
    lines = ["<b>📋 Application readiness</b> <i>(your private profile)</i>"]
    if selected:
        lines.append("Last selected home: " + html.escape(selected[:100]))
    for name, (label, _, _) in FIELDS.items():
        if name == "employment_length":
            label = employment_length_label(pack).capitalize()
        value = pack.values[name]
        if name == "employment_length" and pack.values["employment"] is None:
            shown = "answer employment first"
        elif name == "guarantor_income" and pack.values["guarantor"] != "yes":
            shown = "not applicable" if pack.values["guarantor"] == "no" else "answer guarantor first"
        elif value is not None:
            shown = value_text(name, value)
        elif name == "employment_length" and name in pack.invalid:
            shown = "unconfirmed for this job or status; re-enter"
        elif name in SHARED_FIELDS:
            shown = "unrecognised; update it in /profile" if name in pack.invalid else "not set; use /profile"
        else:
            shown = "unrecognised; re-enter" if name in pack.invalid else "missing"
        command = "profile" if name in SHARED_FIELDS else "ready"
        lines.append(f"{label}: {html.escape(shown)}  <code>/{command} {name}</code>")
    movein = (movein_text(pack.movein) if pack.movein else
              "needs confirmation; a 30-day window may be an estimate" if pack.ambiguous_movein else
              "unrecognised; update /profile movein" if pack.invalid_movein else "not set; /profile movein")
    lines.append(f"Move-in date from /profile: {html.escape(movein)}")
    lines.append("\n" + affordability(pack, rent, archived=archived))
    lines.append("\nReply <code>skip</code> to leave an answer unknown; /cancel stops. "
                 "Use <code>/ready field</code> to change one answer, or <code>clear</code> to remove it. "
                 "Confirm the actual deposit with the agent.")
    return "\n".join(lines)


def employment_length_label(pack):
    return {"unemployed": "time unemployed", "student": "time as a student",
            "retired": "time retired", "other": "time in current status"}.get(
                pack.values["employment"], "time in employment")


def _known(pack):
    labels = {
        "income": "gross annual household income",
        "employment": "employment",
        "employment_length": employment_length_label(pack),
        "references": "references",
        "guarantor": "guarantor available",
        "guarantor_income": "guarantor gross annual income",
        "cash": "cash available for deposit and one month's rent",
        "flexibility": "move-in date",
        "pets": "pets",
        "tenancy": "tenancy length wanted",
        "documents": "documents",
    }
    return [(labels[name], value_text(name, pack.values[name])) for name in FIELDS
            if pack.values[name] is not None
            and (name != "guarantor_income" or pack.values["guarantor"] == "yes")]


def application_summary(pack):
    facts = _known(pack)
    if pack.movein:
        facts.append(("move-in window", movein_text(pack.movein)))
    elif pack.ambiguous_movein:
        facts.append(("move-in window", "[confirm move-in date]"))
    if not facts:
        return "Application summary: [add the details you wish to share]."
    return "Application summary: " + "; ".join(f"{label}: {value}" for label, value in facts) + "."


def extras(pack, rent, details):
    script = ("Hello, I'm calling about your advertised property. Is it still available? "
              "When can I view it, and what documents and affordability checks do you require? Thank you.")
    fields = [f"Property: {details['address'] or '[selected property]'}",
              f"Name: {details['name'] or '[your name]'}", "Email: [your email]",
              "Phone: [your phone number]", f"Household: {details['household'] or '[who will live there]'}",
              f"Occupation: {details['job'] or '[if applicable]'}"]
    known = dict(_known(pack))
    for label in ("gross annual household income", "employment", employment_length_label(pack), "references",
                  "guarantor available", "guarantor gross annual income",
                  "cash available for deposit and one month's rent", "move-in date", "pets",
                  "tenancy length wanted", "documents"):
        if label == "guarantor gross annual income" and pack.values["guarantor"] != "yes":
            continue
        fields.append(f"{label.capitalize()}: {known.get(label, '[not supplied]')}")
    movein = (movein_text(pack.movein) if pack.movein else
              "[confirm move-in date]" if pack.ambiguous_movein else "[not supplied]")
    fields.append("Move-in window: " + movein)
    return ("\n\n<b>Call-first script</b>\n<pre>" + html.escape(script, quote=False)
            + "</pre>\n\n<b>Portal form fields</b>\n<pre>" + html.escape("\n".join(fields), quote=False)
            + "</pre>\n\n<b>Affordability for this home</b>\n" + affordability(pack, rent, archived=True))
