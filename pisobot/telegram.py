import datetime as dt
import html
import json
import os
import re
import time
from zoneinfo import ZoneInfo

from requests import RequestException

from . import collage, config, net, ranking, users
from .sources import PRIVATE_RENT

API = "https://api.telegram.org/bot{token}/{method}"
e = html.escape


def _env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set (add it as a GitHub Actions secret)")
    return value


def quiet_now():
    start, end = config.QUIET_HOURS
    h = dt.datetime.now(ZoneInfo("Europe/London")).hour
    return h >= start or h < end


def _call(method, files=None, **data):
    url = API.format(token=_env("TELEGRAM_BOT_TOKEN"), method=method)
    if method.startswith("send"):
        data.setdefault("chat_id", users.current_chat() or _env("TELEGRAM_CHAT_ID"))
        if quiet_now():
            data["disable_notification"] = "true"
    if isinstance(data.get("reply_markup"), dict):
        data["reply_markup"] = json.dumps(data["reply_markup"])
    try:
        r = net.session.post(url, data=data, files=files, timeout=60, verify=net.VERIFY)
    except RequestException as ex:
        raise RuntimeError(f"Telegram {method} request failed ({type(ex).__name__})") from None
    try:
        res = r.json()
    except ValueError:
        res = {}
    if not res.get("ok"):
        # Telegram's description says what's wrong (e.g. "chat not found", "Unauthorized")
        raise RuntimeError(f"Telegram {method} failed ({r.status_code}): {res.get('description')}")
    return res


def throttle():
    time.sleep(1.2)  # Telegram allows ~1 message/second per chat


# ---------- formatting helpers ----------
# One card answers, top to bottom: where is it · what does it cost all-in · how is the commute ·
# how is the money made up · what is good or worrying about it. One emoji at most per line, as a
# line marker; "~" marks an estimated amount (explained in /help).

PORTALS = {"rm": "Rightmove", "otmr": "OnTheMarket", "otm": "OnTheMarket", "orent": "OpenRent",
           "sr": "SpareRoom", "zp": "Zoopla", "stb": "ShareToBuy", "lq": "L&Q", "nhg": "NHG"}
_MARKER = re.compile(r"^[^\w£~(]+\s*")


def _star(l):
    return " ⭐" if l.tenure == "rent" or (l.deposit is not None and l.deposit <= config.PREFERRED_DEPOSIT) else ""


def _money(value, compact=False):
    if value is None:
        return "TBC"
    if compact and value >= 1000:
        return "£" + f"{value / 1000:,.1f}".rstrip("0").rstrip(".") + "k"
    return f"£{value:,.0f}"


def _n(count, word):
    return f"{count} {word}" + ("" if count == 1 else "s")


def _plain(tag):
    """'🌿 Balcony' -> 'Balcony': features read as words; the line's marker carries the icon."""
    return _MARKER.sub("", tag).strip()


def _date(iso):
    try:
        day = dt.date.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return "now" if day <= dt.date.today() else f"{day.day}\u00a0{day:%b}"   # never split "1 Dec"


def _amount(l, value, component):
    return ("~" if component in l.estimated and value is not None else "") + _money(value)


def headline(l):
    """All-in monthly cost and size: the line a reader compares across cards."""
    size = f" · {l.floor_area_m2:g} m²" if l.floor_area_m2 else ""
    extra = ""
    if l.tenure == "sale":
        extra = f" · {_money(l.price_per_m2)}/m²" if l.price_per_m2 else ""
    elif l.scheme != PRIVATE_RENT:
        extra = f" · {e(l.scheme)}, income caps apply"
    return f"<b>{_money(l.monthly_cost)}/mo</b> · {l.bedrooms} bed{size}{extra}"


def finance_lines(l):
    """How the monthly cost and the upfront money are made up."""
    if l.tenure == "rent":
        if l.bills == 0:
            bills = "bills included"
        elif "council tax" in l.estimated:
            bills = f"{_amount(l, l.bills, 'council tax')} council tax (other bills included)"
        elif l.bills is not None:
            bills = f"{_amount(l, l.bills, 'bills')} bills"
        else:
            bills = f"SC {_amount(l, l.service_charge, 'service charge')}"
        when = _date(l.available)
        move_in = f" · available {when}" if when else ""
        deposit = f" · {_money(l.deposit)} deposit" if l.deposit else ""
        return [f"Rent {_amount(l, l.rent, 'rent')} + {bills}{deposit}{move_in}"]
    share = f"{'~' if 'share %' in l.estimated else ''}{l.share_pct:g}%" if l.share_pct else "Share TBC"
    return [
        f"Mortgage {_money(l.mortgage)} + rent {_amount(l, l.rent, 'rent')} + service "
        f"{_amount(l, l.service_charge, 'service charge')}",
        f"{share} of {_money(l.full_value, True)} ({_money(l.share_price, True)}) · "
        f"{_money(l.deposit, True)} deposit{_star(l)}",
    ]


def facts_line(l):
    """Tenure facts that change the decision; None when there is nothing to add."""
    if l.tenure == "rent":
        if "location" in l.estimated or "approximate" in (l.description or "").lower():
            return "📍 Approximate location: commute and crime are estimates"
        return None
    lease = "Lease TBC" if l.lease_years is None else f"{l.lease_years}-year lease"
    if l.lease_years is not None and l.lease_years < config.SHORT_LEASE_YEARS:
        lease = f"⚠️ Short lease: {l.lease_years} years"
    return f"{lease} · {e(l.scheme[:40])}"


def commute_line(l):
    minutes = f"{l.travel_minutes} min" if l.travel_minutes is not None else "Commute TBC"
    target = e(config.TARGET_NAME)
    if not l.route or l.route == "walk":
        return f"🚶 <b>{minutes}</b> walk to {target}" if l.route == "walk" else f"🚇 <b>{minutes}</b> to {target}"
    start = f" from {e(l.station)}" if l.station else ""
    walk = f", {l.walk_minutes} min walk" if l.walk_minutes is not None else ""
    return f"🚇 <b>{minutes}</b> to {target} · {e(l.route[:120])}{start}{walk}"


def crime_label(l):
    if l.crime_score is None:
        return "⚪ Crime TBC"
    if l.crime_percentile is None:
        return f"⚪ {l.crime_score} crimes/mo nearby"
    band = "🟢 Low" if l.crime_percentile <= 100 / 3 else "🟡 Med" if l.crime_percentile <= 200 / 3 else "🔴 High"
    return f"{band} crime"


def _signals(l):
    """(good things, warnings) as plain phrases."""
    good, bad = [], []
    for tag in l.feature_tags:
        (bad if tag.startswith("⚠️") else good).append(_plain(tag))
    if getattr(l, "deal_tag", None):
        good.append(_plain(l.deal_tag))
    if getattr(l, "rent_savings", None):
        good.append(f"~£{l.rent_savings}/mo below local median rent" if l.tenure == "rent"
                    else f"saves ~£{l.rent_savings}/mo vs renting")
    if (l.service_charge or 0) > config.HIGH_SERVICE_CHARGE:
        bad.append("High service charge" + (" (est.)" if "service charge" in l.estimated else ""))
    bad += [_plain(f) for f in getattr(l, "red_flags", []) or [] if _plain(f) not in bad]
    return [e(g) for g in dict.fromkeys(good)], [e(b) for b in dict.fromkeys(bad)]


def format_listing(l, reason="new", rank=None, flags=""):
    previous = getattr(l, "previous_cost", None)
    drop = (f"📉 <b>Price drop: {_money(previous)} → {_money(l.monthly_cost)}/mo</b>\n"
            if previous else "📉 <b>Price drop</b>\n")
    head = {"drop": drop, "saved": "❤️ <b>Saved home changed</b>\n",
            "new": "" if rank else "🆕 <b>New match</b>\n"}.get(reason, "")
    street = e(l.address.split(",")[0][:90])
    place = e(" ".join(p for p in (l.area_name, l.outcode) if p))
    title = (f"#{rank} · " if rank else "") + f'<a href="{e(l.url)}">{street}</a>'
    lines = [head + f"<b>{title}</b>" + (f" · {place}" if place else "") + (f" {e(flags)}" if flags else "")]
    lines.append(headline(l))
    lines.append(commute_line(l))
    lines += finance_lines(l)
    facts = facts_line(l)
    if facts:
        lines.append(facts)
    good, bad = _signals(l)
    lines.append(" · ".join([crime_label(l), *good]))
    if bad:
        lines.append("⚠️ " + " · ".join(bad))
    if getattr(l, "match_score", None) and l.match_score >= 70:
        why = f" · {e(l.match_reasons[0])}" if getattr(l, "match_reasons", None) else ""
        lines.append(f"✨ {l.match_score}% match{why}")
    return "\n".join(lines)


def listing_keyboard(l, saved=False):
    sid = l.short_id
    portal = PORTALS.get(l.source)
    return {"inline_keyboard": [
        [{"text": f"Open on {portal}" if portal else "Open listing", "url": l.url}],
        [{"text": "🩶 Saved" if saved else "❤️ Save", "callback_data": f"s:{sid}"},
         {"text": "👎 Hide", "callback_data": f"h:{sid}"},
         {"text": "🔕 Hide building", "callback_data": f"d:{sid}"}],
    ]}


# ---------- senders ----------

def send_listing(l, reason="new"):
    text = format_listing(l, reason)
    kb = listing_keyboard(l, saved=reason == "saved")
    jpeg = collage.build(l)   # without one the card is sent as text; the button opens the photos
    return send_card(jpeg, text, kb, "🏠 " + e(l.address[:90]))


def send_card(jpeg, text, keyboard, short_caption):
    if jpeg and len(text.encode("utf-16-le")) // 2 <= 1024:
        return send_photo_bytes(jpeg, text, keyboard)
    if jpeg:
        send_photo_bytes(jpeg, short_caption)
        throttle()
    return send_text(text, keyboard)


def send_text(text, reply_markup=None, silent=False):
    data = {"text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if reply_markup:
        data["reply_markup"] = reply_markup
    if silent:
        data["disable_notification"] = "true"
    return _call("sendMessage", **data)


def send_photo_bytes(jpeg, caption="", reply_markup=None):
    return _call("sendPhoto", files={"photo": ("top.jpg", jpeg, "image/jpeg")},
                 caption=caption, parse_mode="HTML", reply_markup=reply_markup)


# ---------- report ----------

def _short_line(l, flags):
    """Two lines per home in the report lists: where, then the comparable numbers."""
    place = e(" ".join(p for p in (l.area_name, l.outcode) if p))
    scheme = f" · {e(l.scheme)}" if l.tenure == "rent" and l.scheme != PRIVATE_RENT else ""
    minutes = f"{l.travel_minutes} min" if l.travel_minutes is not None else "commute TBC"
    return (f'<a href="{e(l.url)}"><b>{e(l.address.split(",")[0][:60])}</b></a>'
            + (f" · {place}" if place else "") + (f" {flags}" if flags else "") + "\n"
            + f"{_money(l.monthly_cost)}/mo · {l.bedrooms} bed · {minutes} · {crime_label(l)}{scheme}")


def _group_line(group, flags_of, map_url=None):
    costs = sorted(l.monthly_cost for l in group)
    beds = sorted({l.bedrooms for l in group})
    first = min(group, key=lambda l: l.monthly_cost)
    flags = " ".join(sorted({f for l in group for f in flags_of(l).split()}))
    compare = (f' · <a href="{e(map_url or config.MAP_URL)}">compare on map</a>'
               if map_url or config.MAP_URL else "")
    return (f"<b>{e(first.address.split(',')[0][:60])}</b> · {len(group)} homes {flags}".rstrip() + "\n"
            f"{_money(costs[0])}–{_money(costs[-1])}/mo · {'/'.join(map(str, beds))} bed\n"
            f'<a href="{e(first.url)}">Open the cheapest</a>{compare}')


def _chunk(blocks, limit=3800):
    chunks, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += b + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


RENTALS_SHOWN = 8


def report_messages(top, rest, saved, title, flags_of, map_url=None):
    """Summary, one ranked card per listing with local buttons, then grouped alternatives."""
    map_url = map_url or config.MAP_URL
    matches = {l.key: l for l in top + saved + rest}
    if matches:
        cheapest = min(matches.values(), key=lambda l: l.monthly_cost)
        fastest = min(matches.values(), key=lambda l: l.travel_minutes)
        title += (f"\nCheapest <b>{_money(cheapest.monthly_cost)}/mo</b> · "
                  f"fastest commute <b>{fastest.travel_minutes} min</b>")
    else:
        title += "\nNothing matches your search right now. /settings shows it; widen budget, commute or radius with /set."
    if map_url:
        title += f'\n\n<a href="{e(map_url)}">🗺 Compare every home on the map</a>'
    out = [(title, None)]
    saved_keys = {l.key for l in saved}
    for rank, l in enumerate(top, 1):
        out.append((format_listing(l, rank=rank, flags=flags_of(l)), listing_keyboard(l, saved=l.key in saved_keys)))

    top_keys = {l.key for l in top}
    saved = [l for l in saved if l.key not in top_keys]
    blocks = []
    if saved:
        blocks += [f"<b>More saved homes ({len(saved)})</b>"]
        blocks += [_short_line(l, flags_of(l)) + "\n" for l in saved]
    # Private rentals cost more than most shared-ownership homes, so they get their own section
    # instead of never reaching "the cheapest places" below.
    rentals = sorted((l for l in rest if l.tenure == "rent"), key=ranking.score)
    rest = [l for l in rest if l.tenure != "rent"]
    if rentals:
        kind = "private rentals" if all(l.scheme == PRIVATE_RENT for l in rentals) else "rentals"
        shown = min(RENTALS_SHOWN, len(rentals))
        kind = kind if len(rentals) > 1 else kind[:-1]
        blocks.append(f"<b>{len(rentals)} more {kind}</b>\n"
                      + (f"Best {shown} shown · " if shown < len(rentals) else "") + "prices include bills\n")
        blocks += [_short_line(l, flags_of(l)) + "\n" for l in rentals[:RENTALS_SHOWN]]
    if rest:
        groups = ranking.group_by_development(rest)
        shown = min(5, len(groups))
        blocks.append(f"<b>{len(rest)} more homes in {_n(len(groups), 'place')}</b>\n"
                      + (f"The {shown} cheapest\n" if shown < len(groups) else "Cheapest first\n"))
        for g in groups[:5]:
            blocks.append((_short_line(g[0], flags_of(g[0])) if len(g) == 1 else _group_line(g, flags_of, map_url)) + "\n")
        if map_url:
            blocks.append(f'<a href="{e(map_url)}">🗺 See all {len(rest)} on the map</a>')
    out += [(c, None) for c in _chunk(blocks)]
    return out
