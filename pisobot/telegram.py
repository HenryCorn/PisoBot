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

def _k(v):
    return f"£{v / 1000:,.0f}k" if v >= 10000 else f"£{v:,.0f}"


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
        money = (f"£{l.monthly_cost:,.0f}/mo · dep {_k(l.deposit)}{_star(l)} · "
                 f"loan {_k(l.loan)}/{config.MORTGAGE_YEARS}y")
    else:
        money = f"£{l.monthly_cost:,.0f}/mo rent{_star(l)}"
    est = " (est.)" if l.estimated else ""
    route = f" · {e(l.route)}" if l.route else ""
    warn = f"\n    ⚠️ {' · '.join(l.warnings)}" if l.warnings else ""
    return (f"<b>{rank}. {money}{est}</b> {flags}\n"
            f"    {l.bedrooms} bed · {l.travel_minutes} min{route} · 🛡{l.crime_score}\n"
            f'    <a href="{e(l.url)}">{e(l.address[:70])}</a> ({l.source}){warn}')


def _short_line(l, flags):
    loan = f" · loan {_k(l.loan)}" if l.loan else ""
    return (f'£{l.monthly_cost:,.0f} · {l.bedrooms}b · {l.travel_minutes}′ · 🛡{l.crime_score}{loan} · '
            f'<a href="{e(l.url)}">{e(l.address[:55])}</a> ({l.source}) {flags}').rstrip()


def _group_line(group, flags_of):
    costs = sorted(l.monthly_cost for l in group)
    beds = sorted({l.bedrooms for l in group})
    bed_txt = "/".join(f"{b}" for b in beds) + "b"
    first = min(group, key=lambda l: l.monthly_cost)
    links = " ".join(f'<a href="{e(l.url)}">[{i + 1}]</a>' for i, l in enumerate(
        sorted(group, key=lambda l: l.monthly_cost)))
    flags = " ".join(sorted({f for l in group for f in flags_of(l).split()}))
    return (f"£{costs[0]:,.0f}–{costs[-1]:,.0f} · {bed_txt} · {first.travel_minutes}′ · 🛡{first.crime_score} · "
            f"<b>{e(first.address[:45])}</b> — {len(group)} homes: {links} {flags}").rstrip()


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
    """[(text, reply_markup)] - top picks numbered like the collage (with Save/Hide buttons),
    saved listings, then the rest grouped by development."""
    top_blocks = [title, ""] + [_top_line(i + 1, l, flags_of(l)) for i, l in enumerate(top)]
    buttons, row = [], []
    for i, l in enumerate(top):
        row += [{"text": f"❤️ {i + 1}", "callback_data": f"s:{l.short_id}"},
                {"text": f"👎 {i + 1}", "callback_data": f"h:{l.short_id}"}]
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    out = [(c, None) for c in _chunk(top_blocks)]
    if out and buttons:
        out[-1] = (out[-1][0], {"inline_keyboard": buttons})

    blocks = []
    if saved:
        blocks += [f"<b>❤️ Saved ({len(saved)})</b>"] + [_short_line(l, flags_of(l)) for l in saved] + [""]
    if rest:
        groups = ranking.group_by_development(rest)
        blocks.append(f"<b>Other {len(rest)} matches</b> in {len(groups)} places (cheapest first)")
        for g in groups:
            blocks.append(_short_line(g[0], flags_of(g[0])) if len(g) == 1 else _group_line(g, flags_of))
    out += [(c, None) for c in _chunk(blocks)]
    return out
