import html
import os
import time

from . import config, net

API = "https://api.telegram.org/bot{token}/{method}"


def _env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set (add it as a GitHub Actions secret)")
    return value


def _call(method, **data):
    url = API.format(token=_env("TELEGRAM_BOT_TOKEN"), method=method)
    r = net.session.post(url, data=data, timeout=30, verify=net.VERIFY)
    try:
        res = r.json()
    except ValueError:
        res = {}
    if not res.get("ok"):
        # Telegram's description says what's wrong (e.g. "chat not found", "Unauthorized")
        raise RuntimeError(f"Telegram {method} failed ({r.status_code}): {res.get('description')}")
    return res


def format_listing(l, reason="new"):
    e = html.escape
    est = f" <i>(est. {', '.join(l.estimated)})</i>" if l.estimated else ""
    head = "📉 <b>Price drop</b>\n" if reason == "drop" else ""
    star = " ⭐" if l.tenure == "rent" or (l.deposit or 0) <= config.PREFERRED_DEPOSIT else ""
    lines = [
        f"{head}🏠 <b>{e(l.address)}</b>",
        f"{l.bedrooms} bed · {e(l.scheme)} · {e(l.source.upper())}",
        f"💷 <b>£{l.monthly_cost:,.0f}/mo</b>{est}",
    ]
    if l.tenure == "sale":
        share = f"{l.share_pct:.0f}% " if l.share_pct else ""
        full = f" of £{l.full_value:,.0f}" if l.full_value else ""
        lines.append(f"   mortgage £{l.mortgage:,.0f} + rent £{l.rent:,.0f} + service £{l.service_charge:,.0f}")
        lines.append(f"   {share}share £{l.share_price:,.0f}{full}")
        lines.append(f"   deposit £{l.deposit:,.0f}{star}")
    else:
        lines.append(f"   rent £{l.rent:,.0f} pcm{star}")
    lines.append(f"🚇 {l.travel_minutes} min to {config.TARGET_NAME} · 🛡 crime score {l.crime_score}")
    lines.append(f'<a href="{e(l.url)}">Open listing</a>')
    return "\n".join(lines)


def send_listing(l, reason="new"):
    chat = _env("TELEGRAM_CHAT_ID")
    caption = format_listing(l, reason)
    if l.image:
        try:
            res = _call("sendPhoto", chat_id=chat, photo=l.image, caption=caption, parse_mode="HTML")
            if res.get("ok"):
                return
        except Exception:
            pass
    _call("sendMessage", chat_id=chat, text=caption, parse_mode="HTML", disable_web_page_preview="false")


def send_text(text):
    _call("sendMessage", chat_id=_env("TELEGRAM_CHAT_ID"), text=text, parse_mode="HTML",
          disable_web_page_preview="true")


def throttle():
    time.sleep(1.2)  # Telegram allows ~1 message/second per chat


def digest(listings, limit=3800):
    """Compact one-line-per-listing summary split into Telegram-sized messages."""
    e = html.escape
    lines = [
        f'£{l.monthly_cost:,.0f} · {l.bedrooms}b · {l.travel_minutes}′ · 🛡{l.crime_score} · '
        f'<a href="{e(l.url)}">{e(l.address[:55])}</a> ({l.source})'
        for l in listings
    ]
    header = (f"👋 <b>PisoBot is live.</b> {len(listings)} current matches "
              f"(≤£{config.MAX_MONTHLY_COST:,.0f}/mo, ≤{config.MAX_TRAVEL_MINUTES} min to "
              f"{config.TARGET_NAME}), cheapest first. From now on you'll get one alert per new listing.\n")
    chunks, cur = [], header
    for line in lines:
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks
