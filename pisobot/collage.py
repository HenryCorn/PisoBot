"""One low-res JPEG grid of the top listings: photo, rank number and price/commute label."""
import base64
import io
import json
import re

from PIL import Image, ImageDraw, ImageFont

from . import net

TILE_W, TILE_H, LABEL_H, COLS = 300, 200, 34, 5
LABEL_FONT, BADGE_FONT = 17, 21
BG, FG, BADGE = (245, 245, 242), (25, 25, 25), (214, 51, 108)


def small_url(url):
    """ShareToBuy serves images through a resizer whose options are base64 JSON in the path."""
    m = re.match(r"(https://sih\.cdn\.sharetobuy\.com/)([A-Za-z0-9+/=]+)$", url or "")
    if not m:
        return url
    try:
        spec = json.loads(base64.b64decode(m.group(2)))
        spec["edits"] = {"resize": {"fit": "cover", "width": TILE_W, "height": TILE_H}}
        return m.group(1) + base64.b64encode(json.dumps(spec, separators=(",", ":")).encode()).decode()
    except ValueError:
        return url


def _thumb(url):
    if not url:
        return None
    try:
        img = Image.open(io.BytesIO(net.get(small_url(url), retries=1).content)).convert("RGB")
    except Exception:
        return None
    # centre-crop to the tile's aspect ratio
    ratio = TILE_W / TILE_H
    w, h = img.size
    if w / h > ratio:
        nw = int(h * ratio)
        img = img.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    else:
        nh = int(w / ratio)
        img = img.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    return img.resize((TILE_W, TILE_H), Image.LANCZOS)


FONTS = ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "arialbd.ttf", "arial.ttf"]


def _font(size):
    # Pillow's bundled font has no "£", so prefer a system TrueType font
    for name in FONTS:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def build(listings):
    """Return JPEG bytes, or None if no listing has a usable photo."""
    thumbs = [_thumb(l.image) for l in listings]
    if not any(thumbs):
        return None
    rows = (len(listings) + COLS - 1) // COLS
    sheet = Image.new("RGB", (COLS * TILE_W, rows * (TILE_H + LABEL_H)), BG)
    draw = ImageDraw.Draw(sheet)
    big, small = _font(BADGE_FONT), _font(LABEL_FONT)
    for i, (l, t) in enumerate(zip(listings, thumbs)):
        x, y = (i % COLS) * TILE_W, (i // COLS) * (TILE_H + LABEL_H)
        if t:
            sheet.paste(t, (x, y))
        else:
            draw.rectangle((x, y, x + TILE_W - 1, y + TILE_H - 1), fill=(210, 210, 205))
            draw.text((x + TILE_W // 2, y + TILE_H // 2), "no photo", fill=(120, 120, 120),
                      font=small, anchor="mm")
        draw.ellipse((x + 8, y + 8, x + 44, y + 44), fill=BADGE)
        draw.text((x + 26, y + 26), str(i + 1), fill="white", font=big, anchor="mm")
        label = f"£{l.monthly_cost:,.0f}/mo · {l.bedrooms}b · {l.travel_minutes} min"
        draw.text((x + 8, y + TILE_H + LABEL_H // 2), label, fill=FG, font=small, anchor="lm")
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=70, optimize=True)
    return out.getvalue()
