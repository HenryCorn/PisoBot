"""Three distinct photo thumbnails per listing, combined into one Telegram card image."""
import base64
import io
import json
import re
import sys

from PIL import Image, ImageDraw, ImageFont
from requests import RequestException

from . import net

TILE_W, TILE_H = 420, 280


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
    except (RequestException, OSError, ValueError, Image.DecompressionBombError) as ex:
        print(f"Thumbnail unavailable: {type(ex).__name__}", file=sys.stderr)
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


def build(listing):
    """Return a three-panel JPEG, or None if no photo is usable. Never repeat a photo."""
    urls = list(dict.fromkeys(small_url(url) for url in (listing.images or [listing.image]) if url))
    thumbs = [_thumb(url) for url in urls[:3]]
    if not any(thumbs):
        return None
    thumbs += [None] * (3 - len(thumbs))
    sheet = Image.new("RGB", (3 * TILE_W, TILE_H), (210, 210, 205))
    draw = ImageDraw.Draw(sheet)
    for i, t in enumerate(thumbs):
        x = i * TILE_W
        if t:
            sheet.paste(t, (x, 0))
        else:
            draw.text((x + TILE_W // 2, TILE_H // 2), "Photo unavailable", fill=(100, 100, 100),
                      font=_font(22), anchor="mm")
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=70, optimize=True)
    return out.getvalue()
