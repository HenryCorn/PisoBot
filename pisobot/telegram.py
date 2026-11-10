import datetime as dt
import html
import json
import os
import time
from zoneinfo import ZoneInfo

from requests import RequestException

from . import collage, config, net, ranking, tgtext, users
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


def _call(method, files=None, loud=False, **data):
    """loud=True (a top match while the person has /set urgent on) keeps the notification sound
    during quiet hours; every other message is silent then."""
    url = API.format(token=_env("TELEGRAM_BOT_TOKEN"), method=method)
    if method.startswith("send"):
        data.setdefault("chat_id", users.current_chat() or _env("TELEGRAM_CHAT_ID"))
        if quiet_now() and not loud:
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
        error = RuntimeError(f"Telegram {method} failed ({r.status_code}): {res.get('description')}")
        # 400/403: Telegram rejected this request for good (too long, chat blocked...); retrying is futile
        error.permanent = (res.get("error_code") or r.status_code) in (400, 403)
        raise error from None
    return res


def throttle():
    time.sleep(1.2)  # Telegram allows ~1 message/second per chat


# ---------- formatting helpers ----------

def _star(l):
    return " ⭐" if l.tenure == "rent" or (l.deposit is not None and l.deposit <= config.PREFERRED_DEPOSIT) else ""


def _money(value, compact=False):
    if value is None:
        return "TBC"
    if compact and value >= 1000:
        return "£" + f"{value / 1000:,.1f}".rstrip("0").rstrip(".") + "k"
    return f"£{value:,.0f}"


def rental_all_in(l):
    """'~£1,812 all-in': rent plus bills, with ~ while any part is estimated."""
    return f"{'~' if l.estimated and l.monthly_cost is not None else ''}{_money(l.monthly_cost)} all-in"


def rental_bills_detail(l):
    """'council tax B £131 · utilities ~£131'; names what is estimated or included."""
    if not l.bills and not l.council_tax_monthly and not l.utilities_monthly:
        return "bills included" if "bills" not in l.estimated else "bills ~£0 (estimate)"
    parts = []
    if l.council_tax_monthly:
        parts.append(f"{l.council_tax_label or 'council tax'} {_money(l.council_tax_monthly)}")
    else:
        parts.append("council tax incl.")
    if l.utilities_monthly:
        parts.append(f"utilities {'~' if 'bills' in l.estimated else ''}{_money(l.utilities_monthly)}")
    else:   # an estimate that came to £0 is not the same as bills being included in the rent
        parts.append("utilities ~£0 (estimate)" if "bills" in l.estimated else "utilities incl.")
    return " · ".join(parts)


def finance_lines(l):
    def amount(value, component):
        return ("~" if component in l.estimated and value is not None else "") + _money(value)
    total = f"💷 <b>{_money(l.monthly_cost)}/mo</b>"
    sc = amount(l.service_charge, "service charge")
    rent = amount(l.rent, "rent")
    if l.tenure == "rent" and l.bills is not None:
        move_in = f" · 📅 from {l.available}" if l.available else ""
        return [f"💷 <b>{_money(l.rent)} rent</b> · {rental_all_in(l)} <i>({rental_bills_detail(l)})</i>",
                f"🔑 {e(l.scheme)} · 💰 <b>{_money(l.deposit, True)} dep</b>{move_in}"]
    if l.tenure == "rent":
        return [f"{total} <i>(R: {rent} · SC: {sc})</i>", "🔑 Rent · no mortgage needed"]
    share = f"{'~' if 'share %' in l.estimated else ''}{l.share_pct:g}%" if l.share_pct else "Share TBC"
    return [
        f"{total} <i>(M: {_money(l.mortgage)} · R: {rent} · SC: {sc})</i>",
        f"🏷️ <b>{share} of {_money(l.full_value, True)}</b> ({_money(l.share_price, True)})"
        f" · 💰 <b>{_money(l.deposit, True)} dep</b>{_star(l)}",
    ]


def _today():
    return dt.date.today()


def age_text(l, today=None):
    """The portal's own listing age: 'Added today', 'Reduced yesterday', 'Added 3 days ago'."""
    today = today or _today()
    when = l.update_date if l.update_reason else None
    verb = {"added": "Added", "reduced": "Reduced", "added_or_reduced": "Added/reduced"}.get(l.update_reason)
    if not when and l.update_text and l.update_text_on == today.isoformat():
        return l.update_text      # the portal's own vague phrase, only while it was read today
    if not when:
        when, verb = l.first_listed, "Added"
    try:
        days = (today - dt.date.fromisoformat(when)).days
    except (TypeError, ValueError):
        return ""
    if days < 0:
        return ""
    return f"{verb} " + ("today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago")


def portal_line(l, today=None):
    """Age, letting agent and the agent's published number (plain text, never a call button)."""
    parts = [f"🕒 {age_text(l, today)}" if age_text(l, today) else "", f"🏢 {e(l.agent[:60])}" if l.agent else "",
             f"☎️ {e(l.agent_phone[:24])}" if l.agent_phone else ""]
    return " · ".join(p for p in parts if p)


def move_in_line(l):
    """WP3 move-in fit against the person's /profile movein window (VPS only; "" otherwise)."""
    if l.tenure != "rent" or not users.enabled():
        return ""
    from . import renter
    return renter.fit_line(l.available)


def facts_line(l):
    area = f" ({l.floor_area_m2:g} m²" if l.floor_area_m2 else ""
    if area:
        area += (f" · {_money(l.price_per_m2)}/m²" if l.price_per_m2 else "") + ")"
    lease = "📜 Lease: TBC" if l.lease_years is None else f"📜 <b>{l.lease_years}y lease</b>"
    if l.lease_years is not None and l.lease_years < config.SHORT_LEASE_YEARS:
        lease += " ⚠️"
    if l.tenure == "rent":   # a tenant doesn't take on the lease
        note = "" if l.scheme == PRIVATE_RENT else " · ℹ️ income caps apply"
        if "location" in l.estimated:
            note += " · Approx. location; commute/crime estimated"
        return f"🛏️ <b>{l.bedrooms} bed</b>{area} · {e(l.scheme)} ({e(l.source)}){note}"
    return f"🛏️ <b>{l.bedrooms} bed</b>{area} · {lease} · {e(l.scheme[:40])} ({e(l.source)})"


def commute_line(l):
    walk = f"🚶{l.walk_minutes}m" if l.walk_minutes is not None else "🚶TBC"
    first = f"{walk} {l.station or 'boarding point TBC'}"
    route = f" → {l.route[:160]}" if l.route else ""
    minutes = f"{l.travel_minutes}m" if l.travel_minutes is not None else "TBC"
    return f"🚇 <b>{minutes}</b> to {e(config.TARGET_NAME)} <i>({e(first + route)})</i>"


def crime_label(l):
    if l.crime_score is None:
        return "⚪ TBC"
    if l.crime_percentile is None:
        return f"⚪ Unrated ({l.crime_score})"
    band = "🟢 Low" if l.crime_percentile <= 100 / 3 else "🟡 Med" if l.crime_percentile <= 200 / 3 else "🔴 High"
    return f"{band} ({l.crime_score})"


def format_listing(l, reason="new", rank=None, flags=""):
    head = {"drop": "📉 <b>Price drop</b>\n", "saved": "❤️ <b>Saved listing changed</b>\n"}.get(reason, "")
    if getattr(l, "urgency", None) == "top":
        head = "⚡ <b>Top match</b> · act fast\n" + head
    title = [f"#{rank}"] if rank else []
    if l.area_name or l.outcode:
        title.append(e(f"{l.area_name} ({l.outcode})" if l.area_name else l.outcode))
    title.append(f'<a href="{e(l.url)}">{e(l.address.split(",")[0][:90])}</a>')
    lines = [head + "<b>" + " · ".join(title) + (" " + e(flags) if flags else "") + "</b>"]
    lines += finance_lines(l)
    lines.append(facts_line(l))
    if portal_line(l):
        lines.append(portal_line(l))
    if move_in_line(l):
        lines.append(move_in_line(l))
    lines.append(commute_line(l))
    tags = []
    if getattr(l, "match_score", None) and l.match_score >= 70:
        reason_txt = f" ({l.match_reasons[0]})" if getattr(l, "match_reasons", None) else ""
        tags.append(f"✨ <b>{l.match_score}% Match</b>{reason_txt}")
    tags.append("🛡️ Crime: " + crime_label(l))
    tags += l.feature_tags
    if getattr(l, "deal_tag", None):
        tags.append(l.deal_tag)
    if getattr(l, "rent_savings", None):
        tags.append(f"💡 ~£{l.rent_savings}/mo below local median rent" if l.scheme == PRIVATE_RENT
                    else f"💡 Saves ~£{l.rent_savings}/mo vs rent")
    if (l.service_charge or 0) > config.HIGH_SERVICE_CHARGE:
        tags.append("⚠️ High SC" + (" (est.)" if "service charge" in l.estimated else ""))
    lines.append(" · ".join(tags))
    if l.estimated:
        lines.append("<i>est. " + e(", ".join(l.estimated)) + "</i>")
    return "\n".join(lines)


def listing_keyboard(l, saved=False):
    sid = l.short_id
    rows = [
        [{"text": "🩶 Saved" if saved else "❤️ Save", "callback_data": f"s:{sid}"},
         {"text": "👎 Hide", "callback_data": f"h:{sid}"},
         {"text": "🔕 Hide building", "callback_data": f"d:{sid}"}],
    ]
    if users.enabled() and l.tenure == "rent":
        rows += action_rows(l.key, sid)
    rows.append([{"text": "🔗 Open listing", "url": l.url}])
    return {"inline_keyboard": rows}


def action_rows(key, sid):
    """WP3 rows on a rental card (VPS only): the pipeline stage row, then enquiry draft and photos."""
    from . import tracker
    try:
        stage = tracker.stage_of(key)
    except Exception:
        stage = "new"
    first = (tracker.card_row(stage, sid) if stage in tracker.STAGES
             else [{"text": "✉️ Enquired", "callback_data": f"e:{sid}"}])
    return [first, [{"text": "✍️ Enquiry text", "callback_data": f"q:{sid}"},
                    {"text": "📷 More photos", "callback_data": f"m:{sid}"}]]


# ---------- senders ----------

def _loud(l):
    """A top match is loud during quiet hours only when its person turned /set urgent on."""
    if getattr(l, "urgency", None) != "top" or not users.enabled():
        return False
    from . import renter
    try:
        return renter.urgent_on()
    except Exception:
        return False


def send_listing(l, reason="new"):
    text = format_listing(l, reason)
    kb = listing_keyboard(l, saved=reason == "saved")
    shown = []
    jpeg = collage.build(l, sources=shown)
    if not jpeg:
        text += "\n📷 Header unavailable · use Open listing to view the media"
    result = send_card(jpeg, text, kb, "🏠 " + e(l.address[:90]), loud=_loud(l))
    remember_photos(l.key, result, shown if jpeg else [])
    return result


def remember_photos(key, result, urls):
    """Record which listing photos the card just sent (``result``: its sendMessage/sendPhoto reply)
    showed, so its 📷 More photos button sends exactly the others. Best effort: a card is never
    held back by this."""
    if not users.enabled():
        return
    try:
        message_id = result["result"]["message_id"]
        if type(message_id) is int:
            from . import tracker
            tracker.record_shown(key, message_id, urls)
    except Exception:
        pass


def send_card(jpeg, text, keyboard, short_caption, loud=False):
    extra = {"loud": True} if loud else {}
    if jpeg and len(text.encode("utf-16-le")) // 2 <= 1024:
        return send_photo_bytes(jpeg, text, keyboard, **extra)
    if jpeg:
        send_photo_bytes(jpeg, short_caption, **extra)
        throttle()
    return send_text(text, keyboard, **extra)


def send_text(text, reply_markup=None, silent=False, loud=False):
    """One message, or several when the text is over Telegram's 4,096 visible UTF-16 units (cut at
    line breaks, tags kept balanced; the buttons go on the last). Returns the last reply."""
    pieces = tgtext.split_html(text)
    for i, piece in enumerate(pieces):
        data = {"text": piece, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if reply_markup and i == len(pieces) - 1:
            data["reply_markup"] = reply_markup
        if silent:
            data["disable_notification"] = "true"
        result = _call("sendMessage", **data, **({"loud": True} if loud else {}))
    return result


def send_photo_bytes(jpeg, caption="", reply_markup=None, loud=False):
    extra = {"loud": True} if loud else {}
    return _call("sendPhoto", files={"photo": ("top.jpg", jpeg, "image/jpeg")},
                 caption=caption, parse_mode="HTML", reply_markup=reply_markup, **extra)


# ---------- report ----------

def _short_line(l, flags):
    est = " (est.)" if l.estimated else ""
    money = (f"{_money(l.rent)} rent · {rental_all_in(l)}" if l.tenure == "rent" and l.bills is not None
             else f"£{l.monthly_cost:,.0f}/mo{est}")
    return (f'<a href="{e(l.url)}"><b>{e(l.address[:70])}</b></a> {flags}\n'
            f'💷 {money} · 🛏 {l.bedrooms} bed · '
            f'⏱ {l.travel_minutes} min · 🛡 {crime_label(l)}').rstrip()


def _group_line(group, flags_of, map_url=None):
    costs = sorted(l.monthly_cost for l in group)
    beds = sorted({l.bedrooms for l in group})
    bed_txt = "/".join(f"{b}" for b in beds) + " bed"
    first = min(group, key=lambda l: l.monthly_cost)
    flags = " ".join(sorted({f for l in group for f in flags_of(l).split()}))
    est = " · includes estimates" if any(l.estimated for l in group) else ""
    all_in = " all-in" if all(l.tenure == "rent" and l.bills is not None for l in group) else ""
    compare = (f' · <a href="{e(map_url or config.MAP_URL)}">Compare all on map</a>'
               if map_url or config.MAP_URL else "")
    return (f"<b>{e(first.address[:70])}</b> · {len(group)} homes {flags}\n"
            f"💷 £{costs[0]:,.0f}–£{costs[-1]:,.0f}/mo{all_in} · 🛏 {bed_txt}{est}\n"
            f'<a href="{e(first.url)}">Open cheapest home</a>{compare}').rstrip()


def _chunk(blocks, limit=3800):
    # UTF-16 units of the raw HTML: never less than what Telegram counts, so a chunk always fits
    chunks, cur = [], ""
    for b in blocks:
        if cur and tgtext.units(cur) + tgtext.units(b) + 1 > limit:
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
        all_in = " all-in" if cheapest.tenure == "rent" and cheapest.bills is not None else ""
        title += (f"\n💷 From <b>£{cheapest.monthly_cost:,.0f}/mo</b>{all_in} · "
                  f"⏱ Fastest <b>{fastest.travel_minutes} min</b>")
    else:
        title += "\nNo homes matched your filters this time."
    if map_url:
        title += f'\n\n<a href="{e(map_url)}">🗺 Browse photos, filter and compare all homes</a>'
    out = [(title, None)]
    saved_keys = {l.key for l in saved}
    for rank, l in enumerate(top, 1):
        out.append((format_listing(l, rank=rank, flags=flags_of(l)), listing_keyboard(l, saved=l.key in saved_keys)))

    top_keys = {l.key for l in top}
    saved = [l for l in saved if l.key not in top_keys]
    blocks = []
    if saved:
        blocks += [f"<b>❤️ More saved homes ({len(saved)})</b>"]
        blocks += [_short_line(l, flags_of(l)) + "\n" for l in saved]
    # Private rentals cost more than most shared-ownership homes, so they get their own section
    # instead of never reaching "the cheapest places" below.
    rentals = sorted((l for l in rest if l.tenure == "rent"), key=ranking.score)
    rest = [l for l in rest if l.tenure != "rent"]
    if rentals:
        kind = "Private rentals" if all(l.scheme == PRIVATE_RENT for l in rentals) else "Rentals"
        blocks.append(f"🔑 <b>{kind} · {len(rentals)}</b>\n"
                      f"Rent + bills, best {min(RENTALS_SHOWN, len(rentals))} first\n")
        blocks += [_short_line(l, flags_of(l)) + "\n" for l in rentals[:RENTALS_SHOWN]]
    if rest:
        groups = ranking.group_by_development(rest)
        blocks.append(f"🏘 <b>Other {len(rest)} homes · {len(groups)} places</b>\n"
                      f"Showing the {min(5, len(groups))} cheapest places\n")
        for g in groups[:5]:
            blocks.append((_short_line(g[0], flags_of(g[0])) if len(g) == 1 else _group_line(g, flags_of, map_url)) + "\n")
        blocks.append(f'<a href="{e(map_url)}">🗺 Browse all {len(rest)} other homes on the map</a>')
    out += [(c, None) for c in _chunk(blocks)]
    return out
