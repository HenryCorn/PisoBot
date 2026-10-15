import datetime as dt
import html
import json
import os
import time
from zoneinfo import ZoneInfo

from . import collage, config, net, ranking

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
        data.setdefault("chat_id", _env("TELEGRAM_CHAT_ID"))
        if quiet_now():
            data["disable_notification"] = "true"
    if isinstance(data.get("reply_markup"), dict):
        data["reply_markup"] = json.dumps(data["reply_markup"])
    r = net.session.post(url, data=data, files=files, timeout=60, verify=net.VERIFY)
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

def _star(l):
    return " ⭐" if l.tenure == "rent" or (l.deposit is not None and l.deposit <= config.PREFERRED_DEPOSIT) else ""


def _money(value, compact=False):
    if value is None:
        return "TBC"
    if compact and value >= 1000:
        return "£" + f"{value / 1000:,.1f}".rstrip("0").rstrip(".") + "k"
    return f"£{value:,.0f}"


def finance_lines(l):
    def amount(value, component):
        return ("~" if component in l.estimated and value is not None else "") + _money(value)
    total = f"💷 <b>{_money(l.monthly_cost)}/mo</b>"
    sc = amount(l.service_charge, "service charge")
    rent = amount(l.rent, "rent")
    if l.tenure == "rent":
        return [f"{total} <i>(R: {rent} · SC: {sc})</i>", "🔑 Rent · no mortgage needed"]
    share = f"{'~' if 'share %' in l.estimated else ''}{l.share_pct:g}%" if l.share_pct else "Share TBC"
    return [
        f"{total} <i>(M: {_money(l.mortgage)} · R: {rent} · SC: {sc})</i>",
        f"🏷️ <b>{share} of {_money(l.full_value, True)}</b> ({_money(l.share_price, True)})"
        f" · 💰 <b>{_money(l.deposit, True)} dep</b>{_star(l)}",
    ]


def facts_line(l):
    area = f" ({l.floor_area_m2:g} m²" if l.floor_area_m2 else ""
    if area:
        area += (f" · {_money(l.price_per_m2)}/m²" if l.price_per_m2 else "") + ")"
    lease = "📜 Lease: TBC" if l.lease_years is None else f"📜 <b>{l.lease_years}y lease</b>"
    if l.lease_years is not None and l.lease_years < config.SHORT_LEASE_YEARS:
        lease += " ⚠️"
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
    title = [f"#{rank}"] if rank else []
    if l.area_name or l.outcode:
        title.append(e(f"{l.area_name} ({l.outcode})" if l.area_name else l.outcode))
    title.append(f'<a href="{e(l.url)}">{e(l.address.split(",")[0][:90])}</a>')
    lines = [head + "<b>" + " · ".join(title) + (" " + e(flags) if flags else "") + "</b>"]
    lines += finance_lines(l)
    lines.append(facts_line(l))
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
        tags.append(f"💡 Saves ~£{l.rent_savings}/mo vs rent")
    if (l.service_charge or 0) > config.HIGH_SERVICE_CHARGE:
        tags.append("⚠️ High SC" + (" (est.)" if "service charge" in l.estimated else ""))
    lines.append(" · ".join(tags))
    if l.estimated:
        lines.append("<i>est. " + e(", ".join(l.estimated)) + "</i>")
    return "\n".join(lines)


def listing_keyboard(l, saved=False):
    sid = l.short_id
    return {"inline_keyboard": [
        [{"text": "🩶 Saved" if saved else "❤️ Save", "callback_data": f"s:{sid}"},
         {"text": "👎 Hide", "callback_data": f"h:{sid}"},
         {"text": "🔕 Hide building", "callback_data": f"d:{sid}"}],
        [{"text": "🔗 Open listing", "url": l.url}],
    ]}


# ---------- senders ----------

def send_listing(l, reason="new"):
    text = format_listing(l, reason)
    kb = listing_keyboard(l, saved=reason == "saved")
    jpeg = collage.build(l)
    if not jpeg:
        text += "\n📷 Header unavailable · use Open listing to view the media"
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
    est = " (est.)" if l.estimated else ""
    return (f'<a href="{e(l.url)}"><b>{e(l.address[:70])}</b></a> {flags}\n'
            f'💷 £{l.monthly_cost:,.0f}/mo{est} · 🛏 {l.bedrooms} bed · '
            f'⏱ {l.travel_minutes} min · 🛡 {crime_label(l)}').rstrip()


def _group_line(group, flags_of):
    costs = sorted(l.monthly_cost for l in group)
    beds = sorted({l.bedrooms for l in group})
    bed_txt = "/".join(f"{b}" for b in beds) + " bed"
    first = min(group, key=lambda l: l.monthly_cost)
    flags = " ".join(sorted({f for l in group for f in flags_of(l).split()}))
    est = " · includes estimates" if any(l.estimated for l in group) else ""
    return (f"<b>{e(first.address[:70])}</b> · {len(group)} homes {flags}\n"
            f"💷 £{costs[0]:,.0f}–£{costs[-1]:,.0f}/mo · 🛏 {bed_txt}{est}\n"
            f'<a href="{e(first.url)}">Open cheapest home</a> · '
            f'<a href="{e(config.MAP_URL)}">Compare all on map</a>').rstrip()


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


def report_messages(top, rest, saved, title, flags_of):
    """Summary, one ranked card per listing with local buttons, then grouped alternatives."""
    matches = {l.key: l for l in top + saved + rest}
    if matches:
        cheapest = min(matches.values(), key=lambda l: l.monthly_cost)
        fastest = min(matches.values(), key=lambda l: l.travel_minutes)
        title += (f"\n💷 From <b>£{cheapest.monthly_cost:,.0f}/mo</b> · "
                  f"⏱ Fastest <b>{fastest.travel_minutes} min</b>")
    else:
        title += "\nNo homes matched your filters this time."
    title += f'\n\n<a href="{e(config.MAP_URL)}">🗺 Browse photos, filter and compare all homes</a>'
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
    if rest:
        groups = ranking.group_by_development(rest)
        blocks.append(f"🏘 <b>Other {len(rest)} homes · {len(groups)} places</b>\n"
                      f"Showing the {min(5, len(groups))} cheapest places\n")
        for g in groups[:5]:
            blocks.append((_short_line(g[0], flags_of(g[0])) if len(g) == 1 else _group_line(g, flags_of)) + "\n")
        blocks.append(f'<a href="{e(config.MAP_URL)}">🗺 Browse all {len(rest)} other homes on the map</a>')
    out += [(c, None) for c in _chunk(blocks)]
    return out
