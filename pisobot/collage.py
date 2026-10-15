"""1200x500 Telegram header: fitted location map and an uncropped floorplan or hero."""
import base64
import io
import json
import math
import os
import re
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps
from requests import RequestException

from . import config, net

WIDTH, HEIGHT, MAP_W = 1200, 500, 480
PHOTO_W = WIDTH - MAP_W
GALLERY_H = 280
DIVIDER = 4
TILE_CACHE = Path(__file__).resolve().parents[1] / ".cache" / "map-tiles"


def small_url(url, floorplan=False, width=PHOTO_W, height=HEIGHT):
    """ShareToBuy serves images through a resizer whose options are base64 JSON in the path."""
    m = re.match(r"(https://sih\.cdn\.sharetobuy\.com/)([A-Za-z0-9+/=]+)$", url or "")
    if not m:
        return url
    try:
        spec = json.loads(base64.b64decode(m.group(2)))
        spec["edits"] = {"resize": {"fit": "inside" if floorplan else "cover",
                                  "width": width, "height": height}}
        return m.group(1) + base64.b64encode(json.dumps(spec, separators=(",", ":")).encode()).decode()
    except ValueError:
        return url


def _is_floorplan(url):
    label = url or ""
    if label.startswith("https://sih.cdn.sharetobuy.com/"):
        try:
            label = json.loads(base64.b64decode(label.rsplit("/", 1)[1])).get("key", label)
        except ValueError:
            pass
    return bool(re.search(r"floor[\s_-]*plan", label, re.I) or
                (re.search(r"(?:^|[/_-])plan(?:[._-]|\d|$)", label, re.I)
                 and not re.search(r"open[-_ ]plan", label, re.I)))


def _thumb(url, floorplan=False):
    if not url:
        return None
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(net.get(
            small_url(url, floorplan), retries=1).content))).convert("RGBA")
        background = Image.new("RGBA", img.size, "white")
        img = Image.alpha_composite(background, img).convert("RGB")
    except (RequestException, OSError, ValueError, Image.DecompressionBombError) as ex:
        print(f"Thumbnail unavailable: {type(ex).__name__}", file=sys.stderr)
        return None
    if not floorplan:
        return ImageOps.fit(img, (PHOTO_W, HEIGHT), Image.Resampling.LANCZOS)
    fitted = ImageOps.contain(img, (PHOTO_W - 32, HEIGHT - 72), Image.Resampling.LANCZOS)
    panel = Image.new("RGB", (PHOTO_W, HEIGHT), "white")
    panel.paste(fitted, ((PHOTO_W - fitted.width) // 2, 48 + (HEIGHT - 72 - fitted.height) // 2))
    return panel


def _gallery_thumb(url, target_size):
    if not url:
        return None
    try:
        tw, th = target_size
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(net.get(
            small_url(url, floorplan=False, width=tw, height=th), retries=1).content))).convert("RGBA")
        background = Image.new("RGBA", img.size, "white")
        img = Image.alpha_composite(background, img).convert("RGB")
        return ImageOps.fit(img, target_size, Image.Resampling.LANCZOS)
    except (RequestException, OSError, ValueError, Image.DecompressionBombError) as ex:
        print(f"Gallery thumbnail unavailable: {type(ex).__name__}", file=sys.stderr)
        return None


FONTS = ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "arialbd.ttf", "arial.ttf"]


def _font(size):
    # Pillow's bundled font has no "£", so prefer a system TrueType font
    for name in FONTS:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _mercator(lat, lng):
    if not math.isfinite(lat) or not math.isfinite(lng) or not -85 <= lat <= 85 or not -180 <= lng <= 180:
        raise ValueError("Invalid map coordinates")
    return (lng + 180) / 360, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2


def _map_view(listing):
    points = [_mercator(config.TARGET_LAT, config.TARGET_LNG), _mercator(listing.lat, listing.lng)]
    for zoom in range(15, -1, -1):
        scale = 256 * 2 ** zoom
        xs, ys = [[p[i] * scale for p in points] for i in (0, 1)]
        if max(xs) - min(xs) <= MAP_W - 128 and max(ys) - min(ys) <= HEIGHT - 200:
            break
    origin = (math.floor(sum(xs) / 2 - MAP_W / 2), math.floor(sum(ys) / 2 - (HEIGHT - 72) / 2))
    return zoom, origin, [(x - origin[0], y - origin[1]) for x, y in zip(xs, ys)]


def _tile(zoom, x, y):
    path = TILE_CACHE / f"{zoom}-{x}-{y}.png"
    if path.exists() and time.time() - path.stat().st_mtime < 7 * 86400:
        return Image.open(path).convert("RGB")
    url = os.environ.get("MAP_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
    content = net.get(url.format(z=zoom, x=x, y=y), retries=1,
                      headers={"User-Agent": "PisoBot/1.0 (+https://github.com/HenryCorn/PisoBot)"}).content
    image = Image.open(io.BytesIO(content)).convert("RGB")
    if image.size != (256, 256):
        raise ValueError("Map tile is not 256x256")
    TILE_CACHE.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")
    (TILE_CACHE.parent / "map-tiles.updated").write_text("updated", encoding="ascii")
    return image


def _map(listing):
    if listing.lat is None or listing.lng is None:
        print(f"Map unavailable for {listing.key}: missing coordinates", file=sys.stderr)
        return None
    try:
        zoom, (ox, oy), points = _map_view(listing)
        image = Image.new("RGB", (MAP_W, HEIGHT), "white")
        for x in range(ox // 256, (ox + MAP_W - 1) // 256 + 1):
            for y in range(oy // 256, (oy + HEIGHT - 1) // 256 + 1):
                image.paste(_tile(zoom, x, y), (x * 256 - ox, y * 256 - oy))
    except (RequestException, OSError, ValueError, Image.DecompressionBombError) as ex:
        print(f"Map unavailable for {listing.key}: {type(ex).__name__}", file=sys.stderr)
        return None
    draw = ImageDraw.Draw(image)
    draw.line(points, fill="white", width=10)
    draw.line(points, fill=(74, 82, 102), width=5)
    pin_points = points
    if math.dist(*points) < 44:
        pin_points = [(points[0][0] - 24, points[0][1]), (points[1][0] + 24, points[1][1])]
        for point, pin in zip(points, pin_points):
            draw.line([point, pin], fill=(74, 82, 102), width=3)
    for (x, y), label, color in zip(pin_points, ("A", "B"), ((35, 80, 170), (197, 46, 65))):
        draw.ellipse((x - 20, y - 20, x + 20, y + 20), fill=color, outline="white", width=3)
        draw.text((x, y), label, fill="white", font=_font(22), anchor="mm")
    draw.rectangle((0, 0, MAP_W, 35), fill="white")
    draw.text((12, 17), f"A {config.TARGET_NAME}  /  B Listing", fill=(25, 35, 55), font=_font(19), anchor="lm")
    if getattr(listing, "travel_minutes", None) is not None:
        draw.text((MAP_W - 14, 17), f"{listing.travel_minutes} min commute", fill=(25, 35, 55),
                  font=_font(17), anchor="rm")
    draw.rectangle((0, HEIGHT - 61, MAP_W, HEIGHT), fill="white")
    draw.text((12, HEIGHT - 43), "North up / straight line, not a route", fill=(55, 65, 80),
              font=_font(17), anchor="lm")
    draw.text((12, HEIGHT - 18), "© OpenStreetMap contributors", fill=(55, 65, 80), font=_font(17), anchor="lm")
    return image


def build(listing):
    urls = list(dict.fromkeys(url for url in [listing.image, *listing.images] if url))
    plan = listing.floorplan or next((url for url in urls if _is_floorplan(url)), None)
    photo = _thumb(plan, floorplan=True) if plan else None
    kind = "floorplan" if photo else "unavailable"
    used_top_url = plan if photo else None
    if photo is None:
        for url in urls[:5]:
            if url != plan and not _is_floorplan(url):
                photo = _thumb(url)
                if photo is not None:
                    kind = "hero photo"
                    used_top_url = url
                    break
    location = _map(listing)
    print(f"header {listing.key}: map={'ready' if location is not None else 'unavailable'}, media={kind}")
    if photo is None and location is None:
        return None

    # Bottom gallery row of up to 3 listing photos (excluding floorplan and top hero photo)
    gallery_candidates = [
        u for u in urls
        if u != plan and not _is_floorplan(u) and u != used_top_url
    ]
    gallery_slots = {
        3: [(0, 397), (401, 398), (803, 397)],
        2: [(0, 598), (602, 598)],
        1: [(0, WIDTH)],
    }
    target_w = 398 if len(gallery_candidates) >= 3 else (598 if len(gallery_candidates) == 2 else WIDTH)
    gallery_thumbs = []
    for u in gallery_candidates:
        gt = _gallery_thumb(u, (target_w, GALLERY_H))
        if gt is not None:
            gallery_thumbs.append(gt)
            if len(gallery_thumbs) == 3:
                break

    n_thumbs = len(gallery_thumbs)
    slots = gallery_slots.get(n_thumbs, [])
    if n_thumbs in (1, 2) and target_w == 398:
        gallery_thumbs = [
            ImageOps.fit(t, (w, GALLERY_H), Image.Resampling.LANCZOS)
            for (x, w), t in zip(slots, gallery_thumbs)
        ]

    has_gallery = n_thumbs > 0
    total_height = HEIGHT + (DIVIDER + GALLERY_H if has_gallery else 0)
    sheet = Image.new("RGB", (WIDTH, total_height), (235, 237, 240))
    draw = ImageDraw.Draw(sheet)

    for panel, x, width, label in ((location, 0, MAP_W, "Map unavailable"),
                                   (photo, MAP_W, PHOTO_W, "Property image unavailable")):
        if panel is not None:
            sheet.paste(panel, (x, 0))
        else:
            draw.text((x + width // 2, HEIGHT // 2), label, fill=(85, 95, 110), font=_font(24), anchor="mm")

    if photo is not None:
        label = kind.upper()
        font = _font(16)
        bbox = draw.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        bx, by = MAP_W + 14, 12
        bw, bh = tw + 24, max(th + 14, 28)
        draw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=bh // 2,
                               fill="white", outline=(203, 213, 225), width=1)
        draw.text((bx + 12, by + bh // 2), label, fill=(30, 41, 59), font=font, anchor="lm")

    draw.line((MAP_W, 0, MAP_W, HEIGHT), fill="white", width=4)

    if has_gallery:
        gy = HEIGHT + DIVIDER
        draw.line((0, HEIGHT + 2, WIDTH, HEIGHT + 2), fill="white", width=DIVIDER)
        for (gx, gw), gt in zip(slots, gallery_thumbs):
            sheet.paste(gt, (gx, gy))
            draw.rectangle((gx, gy, gx + gw - 1, gy + GALLERY_H - 1),
                           outline=(226, 232, 240), width=1)
        if n_thumbs == 3:
            draw.line((399, gy, 399, total_height), fill="white", width=DIVIDER)
            draw.line((801, gy, 801, total_height), fill="white", width=DIVIDER)
        elif n_thumbs == 2:
            draw.line((600, gy, 600, total_height), fill="white", width=DIVIDER)

    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()
