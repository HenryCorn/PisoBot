"""Offline preview of the Telegram rental report as a phone would show it.

    .venv/bin/python scripts/preview_report.py /tmp/pisobot-preview [--no-images]

Builds sample listings (no portal requests), renders the same messages and keyboards the bot
sends (telegram.report_messages / format_listing), and writes index.html that mimics a Telegram
chat at phone width, with the real collage headers unless --no-images (those fetch map tiles and
photos). Open it in a browser, or screenshot it with headless Chrome:

    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \\
        --screenshot=/tmp/pisobot-preview/shot.png --window-size=430,4000 \\
        /tmp/pisobot-preview/index.html
"""
import datetime as dt
import html
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pisobot import collage, telegram  # noqa: E402
from pisobot.models import Listing  # noqa: E402
from pisobot.sources import private_rent_costs  # noqa: E402

PHOTO = "https://photos2.spareroom.co.uk/images/flatshare/listings/unmodified/{}.jpg"


def sample():
    def rent(id, address, beds, rent, lat, lng, minutes, route, station, walk, changes, crime, pct,
             description, source="rm", photos=(), area=None, available=None, scheme="Private rent"):
        l = Listing(source, id, f"https://www.rightmove.co.uk/properties/{id}", address, beds, scheme,
                    tenure="rent", rent=rent, lat=lat, lng=lng, description=description,
                    images=[PHOTO.format(p) for p in photos], floor_area_m2=area, available=available)
        private_rent_costs(l)
        l.deposit = l.min_deposit
        l.monthly_cost = l.rent + l.bills
        l.travel_minutes, l.route, l.station, l.walk_minutes, l.changes = minutes, route, station, walk, changes
        l.crime_score, l.crime_percentile = crime, pct
        return l

    homes = [
        rent("161234567", "Elgin Avenue, Maida Vale, London W9 2NR", 1, 1325, 51.5262, -0.1925, 24,
             "Bakerloo line", "Maida Vale", 6, 0, 64, 28,
             "One bedroom flat. Bright and airy, dual aspect reception, modern kitchen. Balcony. Lift.",
             photos=("10/20/102040008", "10/21/102112368", "10/21/102109879"), area=46.5,
             available="2026-12-01"),
        rent("161299881", "Brecknock Road, London N7", 1, 1295, 51.5523, -0.1336, 31,
             "Northern line → Victoria line", "Tufnell Park", 9, 1, 98, 55,
             "1 bed flat, bills included except council tax. Recently refurbished.",
             photos=("95/70/95709255", "10/20/102099923")),
        rent("700001", "Kennington Lane, London SE11 5QY", 2, 1350, 51.4880, -0.1110, 27,
             "Northern line", "Kennington", 7, 0, 112, 71,
             "Two bedroom flat above shop. No pets. Unfurnished.", source="orent",
             photos=("97/00/97005236",)),
        rent("787654", "West Parkside, North Greenwich, SE10 0JT", 1, 1290, 51.4941, 0.0089, 38,
             "Jubilee line", "North Greenwich", 11, 0, 41, 12,
             "London Living Rent apartment.", source="stb", scheme="London Living Rent"),
    ]
    homes[0].match_score, homes[0].match_reasons = 82, ["near saved home (0.8km)"]
    homes[0].deal_tag = "🏷️ Price cut (-£50/mo)"
    homes[1].rent_savings = 255
    return homes


def to_html(text):
    """Telegram's HTML subset rendered close to how the app shows it."""
    text = text.replace("\n", "<br>")
    text = re.sub(r"<blockquote( expandable)?>", '<blockquote class="q">', text)
    return text


def keyboard_html(kb):
    if not kb:
        return ""
    rows = []
    for row in kb["inline_keyboard"]:
        rows.append('<div class="kbrow">' + "".join(
            f'<span class="btn">{html.escape(b["text"])}</span>' for b in row) + "</div>")
    return '<div class="kb">' + "".join(rows) + "</div>"


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/pisobot-preview")
    images = "--no-images" not in sys.argv
    out.mkdir(parents=True, exist_ok=True)
    homes = sample()
    saved = {homes[1].key}
    new = {homes[0].key, homes[2].key}

    def flags_of(l):
        return " ".join(f for f, on in (("🆕", l.key in new), ("❤️", l.key in saved)) if on)

    now = dt.datetime(2026, 11, 9, 18, 0)
    title = (f"<b>🔑 PisoBot · Private rentals</b>\n<i>{now:%a %d %b · %H:%M} GMT</i>\n\n"
             f"<b>{len(homes)} homes</b> · 🆕 {len(new)} new · ❤️ {len(saved)} saved")
    top, rest = homes[:3], homes[3:]
    messages = telegram.report_messages(top, rest, [homes[1]], title, flags_of, map_url="")
    alerts = [(telegram.format_listing(homes[0], "new"), telegram.listing_keyboard(homes[0])),
              (telegram.format_listing(homes[2], "drop"), telegram.listing_keyboard(homes[2]))]
    homes[2].previous_cost = 1680
    alerts[1] = (telegram.format_listing(homes[2], "drop"), alerts[1][1])
    bubbles = []
    for i, (text, kb) in enumerate([*messages, *alerts]):
        listing = top[i - 1] if 1 <= i <= len(top) else (homes[0] if i == len(messages) else
                                                          homes[2] if i == len(messages) + 1 else None)
        image = ""
        if listing is not None and images:
            jpeg = collage.build(listing)
            if jpeg:
                (out / f"card-{i}.jpg").write_bytes(jpeg)
                image = f'<img src="card-{i}.jpg">'
        caption = len(text.encode("utf-16-le")) // 2
        warn = f'<div class="meta">caption {caption}/1024{" ⚠️ too long: sent as separate text" if caption > 1024 else ""}</div>' if listing else ""
        bubbles.append(f'<div class="bubble">{image}<div class="text">{to_html(text)}</div>{warn}</div>'
                       f'{keyboard_html(kb)}')
        if i == len(messages) - 1:
            bubbles.append('<div class="sep">— later: instant alerts —</div>')
    page = f"""<!doctype html><meta charset="utf-8"><title>PisoBot preview</title>
<style>
body {{ margin:0; background:#8fb3c9; font:15px/1.38 -apple-system, "SF Pro Text", Roboto, sans-serif; }}
.chat {{ max-width:400px; margin:0; padding:12px 8px 40px; }}
.bubble {{ background:#fff; border-radius:14px; margin:8px 0 2px; overflow:hidden; box-shadow:0 1px 1px rgba(0,0,0,.15); }}
.bubble img {{ display:block; width:100%; }}
.text {{ padding:7px 11px 9px; color:#000; word-wrap:break-word; }}
.text a {{ color:#2a7ab8; text-decoration:none; }}
.q {{ margin:4px 0; padding:2px 8px; border-left:3px solid #2a7ab8; background:#eef5fb; border-radius:4px; }}
.meta {{ font-size:11px; color:#8a9; padding:0 11px 6px; }}
.kb {{ margin:2px 0 6px; }}
.kbrow {{ display:flex; gap:3px; margin-top:3px; }}
.btn {{ flex:1; text-align:center; background:rgba(0,0,0,.22); color:#fff; border-radius:8px; padding:9px 4px; font-size:14px; font-weight:500; }}
.sep {{ text-align:center; color:#fff; font-size:13px; margin:18px 0 6px; }}
</style><div class="chat">{"".join(bubbles)}</div>"""
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"preview written to {out / 'index.html'}")


if __name__ == "__main__":
    os.environ.setdefault("STORAGE", "cloud")
    main()
