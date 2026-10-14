import datetime as dt
import html
import json
import os
import time
from zoneinfo import ZoneInfo

from . import config, net, ranking

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
    return " ⭐" if l.tenure == "rent" or (l.deposit or 0) <= config.PREFERRED_DEPOSIT else ""


def finance_lines(l):
    """Cost breakdown + what to ask the bank for."""
    est = f" <i>(est. {', '.join(l.estimated)})</i>" if l.estimated else ""
    if l.tenure == "rent":
        return [f"💷 <b>£{l.monthly_cost:,.0f}/mo rent</b>{est}{_star(l)}"]
    min_dep = f" (min £{l.min_deposit:,.0f})" if l.min_deposit and l.min_deposit < l.deposit else ""
    share = f"{l.share_pct:.0f}% share " if l.share_pct else "share "
    full = f" of £{l.full_value:,.0f}" if l.full_value else ""
    return [
        f"💷 <b>£{l.monthly_cost:,.0f}/mo</b> = mortgage £{l.mortgage:,.0f} + rent £{l.rent:,.0f}"
        f" + service £{l.service_charge:,.0f}{est}",
        f"🏦 Mortgage needed <b>£{l.loan:,.0f}</b> over {config.MORTGAGE_YEARS} yrs @ {config.MORTGAGE_RATE:g}%",
        f"💰 Deposit <b>£{l.deposit:,.0f}</b>{min_dep}{_star(l)} · {share}£{l.share_price:,.0f}{full}",
    ]


def facts_line(l):
    bits = [f"{l.bedrooms} bed", e(l.scheme)]
    if l.floor:
        bits.append(f"floor {e(str(l.floor))}")
    if l.lease_years:
        bits.append(f"lease {l.lease_years} yrs")
    if l.council_tax:
        bits.append(f"CT band {l.council_tax}")
    return " · ".join(bits)


def commute_line(l):
    walk = f" · {l.walk_minutes}′ walk to {e(l.station)}" if l.station and l.walk_minutes else (
        f" · from {e(l.station)}" if l.station else "")
    route = f"\n    {e(l.route)}" if l.route else ""
    return f"🚇 <b>{l.travel_minutes} min</b> to {config.TARGET_NAME}{walk}{route}"


def format_listing(l, reason="new"):
    head = {"drop": "📉 <b>Price drop</b>\n", "saved": "❤️ <b>Saved listing changed</b>\n"}.get(reason, "")
    lines = [f"{head}🏠 <b>{e(l.address)}</b> ({l.source})", facts_line(l)]
    lines += finance_lines(l)
    lines.append(commute_line(l))
    lines.append(f"🛡 crime score {l.crime_score}")
    if l.warnings:
        lines.append("⚠️ " + " · ".join(l.warnings))
    return "\n".join(lines)


def listing_keyboard(l):
    sid = l.short_id
    return {"inline_keyboard": [
        [{"text": "❤️ Save", "callback_data": f"s:{sid}"},
         {"text": "👎 Hide", "callback_data": f"h:{sid}"},
         {"text": "🔕 Hide building", "callback_data": f"d:{sid}"}],
        [{"text": "🔗 Open listing", "url": l.url}],
    ]}


# ---------- senders ----------

def send_listing(l, reason="new"):
    text = format_listing(l, reason)
    kb = listing_keyboard(l)
    if l.image and len(text) <= 1024:
        try:
            return _call("sendPhoto", photo=l.image, caption=text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    return _call("sendMessage", text=text, parse_mode="HTML", disable_web_page_preview="true", reply_markup=kb)


def send_text(text, reply_markup=None):
    data = {"text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if reply_markup:
        data["reply_markup"] = reply_markup
    return _call("sendMessage", **data)


def send_photo_bytes(jpeg, caption=""):
    return _call("sendPhoto", files={"photo": ("top.jpg", jpeg, "image/jpeg")},
                 caption=caption, parse_mode="HTML")


def answer_callback(callback_id, text):
    try:
        _call("answerCallbackQuery", callback_query_id=callback_id, text=text)
    except Exception:
        pass  # callbacks older than a few minutes can't be answered; the action still applies


def mark_message(chat_id, message_id, label):
    """Replace a listing's buttons with its new status."""
    try:
        _call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
              reply_markup={"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]})
    except Exception:
        pass


def get_updates(offset):
    return _call("getUpdates", offset=offset, timeout=0,
                 allowed_updates=json.dumps(["message", "callback_query"]))["result"]


# ---------- report ----------

def _top_line(rank, l, flags):
    if l.tenure == "sale":
        finance = (f"💰 Deposit <b>£{l.deposit:,.0f}</b>{_star(l)} · "
                   f"mortgage £{l.loan:,.0f}/{config.MORTGAGE_YEARS}y")
    else:
        finance = "🔑 Rent · no mortgage needed"
    est = " (est.)" if l.estimated else ""
    route = f"\n🚇 {e(l.route[:160])}" if l.route else ""
    warn = f"\n⚠️ {e(' · '.join(l.warnings))}" if l.warnings else ""
    return (f'<b>#{rank}</b> {flags} <a href="{e(l.url)}">{e(l.address[:90])}</a>\n'
            f"💷 <b>£{l.monthly_cost:,.0f}/mo</b>{est} · 🛏 {l.bedrooms} bed · "
            f"⏱ <b>{l.travel_minutes} min</b>\n"
            f"{finance}\n🛡 Crime {l.crime_score} · {e(l.scheme[:60])} · {e(l.source)}{route}{warn}")


def _short_line(l, flags):
    est = " (est.)" if l.estimated else ""
    return (f'<a href="{e(l.url)}"><b>{e(l.address[:70])}</b></a> {flags}\n'
            f'💷 £{l.monthly_cost:,.0f}/mo{est} · 🛏 {l.bedrooms} bed · '
            f'⏱ {l.travel_minutes} min · 🛡 {l.crime_score}').rstrip()


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
    """Summary, three ranked cards per message with local buttons, then grouped alternatives."""
    matches = {l.key: l for l in top + saved + rest}
    if matches:
        cheapest = min(matches.values(), key=lambda l: l.monthly_cost)
        fastest = min(matches.values(), key=lambda l: l.travel_minutes)
        title += (f"\n💷 From <b>£{cheapest.monthly_cost:,.0f}/mo</b> · "
                  f"⏱ Fastest <b>{fastest.travel_minutes} min</b>")
    else:
        title += "\nNo homes matched your filters this time."
    title += (f'\n\n<a href="{e(config.MAP_URL)}">🗺 Browse photos, filter and compare all homes</a>\n'
              "🆕 New since last report · ❤️ Saved · ⭐ Low deposit · est. = estimated costs")
    out = [(title, None)]
    for start in range(0, len(top), 3):
        batch = top[start:start + 3]
        blocks = [f"🏆 <b>Top picks {start + 1}–{start + len(batch)} of {len(top)}</b>"]
        buttons = []
        for rank, l in enumerate(batch, start + 1):
            blocks.append(_top_line(rank, l, flags_of(l)))
            buttons.append([
                {"text": f"🔗 #{rank}", "url": l.url},
                {"text": f"❤️ #{rank}", "callback_data": f"s:{l.short_id}"},
                {"text": f"👎 #{rank}", "callback_data": f"h:{l.short_id}"},
            ])
        out.append(("\n\n".join(blocks), {"inline_keyboard": buttons}))

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
