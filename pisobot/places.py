"""Nearest supermarket, convenience shop, gym and cinema for a rental card (optional, off by default).

Reads one small precomputed file (pisobot/data/place_tiles.json, built offline by
scripts/build_place_tiles.py from OpenStreetMap, (c) OpenStreetMap contributors, ODbL 1.0). No
network, no database, no background process, no writes. Switched on with PLACE_TILES=1; with it
off nothing here is imported into the alert path's behaviour. Every failure returns "" so a card
is never held back, altered or delayed by this module.
"""
import html
import json
import logging
import math
import os
import re
from pathlib import Path

LOG = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parent / "data" / "place_tiles.json"
MAX_M = 2000          # nothing further than this is shown
DETOUR = 1.3          # straight line -> rough street distance
WALK_M_PER_MIN = 80
CELL = 0.01           # degrees; ~1.1 km north-south, ~0.7 km east-west
ORDER = (("supermarket", "🛒"), ("convenience", "🏪"), ("gym", "🏋"), ("cinema", "🎬"))
NAME_MAX = 14

_state = {"loaded": False, "grid": None}
_CONTROL = re.compile(r"[\x00-\x1f\x7f<>&]")


def enabled():
    return os.environ.get("PLACE_TILES") == "1"


def _load():
    """Index the file once per process; None when missing or malformed (logged once)."""
    if _state["loaded"]:
        return _state["grid"]
    _state["loaded"] = True
    try:
        with open(DATA, encoding="utf-8") as f:
            raw = json.load(f)
        grid = {}
        for category, _ in ORDER:
            for lat, lng, name in raw["points"].get(category, []):
                lat, lng = float(lat), float(lng)
                if not (math.isfinite(lat) and math.isfinite(lng)):
                    continue
                grid.setdefault((category, int(lat // CELL), int(lng // CELL)), []).append(
                    (lat, lng, str(name or "")))
        _state["grid"] = grid
    except Exception as ex:
        LOG.warning("Place tiles unavailable (%s)", type(ex).__name__)
        _state["grid"] = None
    return _state["grid"]


def _metres(lat, lng, lat2, lng2):
    dy = (lat2 - lat) * 111200
    dx = (lng2 - lng) * 111200 * math.cos(math.radians(lat))
    return math.hypot(dx, dy)


def nearest(lat, lng, category):
    """(metres, name) of the closest point of a category within MAX_M, else None."""
    grid = _load()
    if not grid:
        return None
    cy, cx = int(lat // CELL), int(lng // CELL)
    best = None
    for dy in (-2, -1, 0, 1, 2):
        for dx in (-2, -1, 0, 1, 2):
            for p_lat, p_lng, name in grid.get((category, cy + dy, cx + dx), ()):
                d = _metres(lat, lng, p_lat, p_lng)
                if d <= MAX_M and (best is None or d < best[0]):
                    best = (d, name)
    return best


def _label(name):
    name = _CONTROL.sub("", name).strip()
    if len(name) > NAME_MAX:
        name = name[:NAME_MAX - 1].rstrip() + "…"
    return html.escape(name, quote=False)


def line(lat, lng):
    """One HTML-safe card line, or "" when there is nothing to show or anything goes wrong."""
    try:
        if lat is None or lng is None:
            return ""
        lat, lng = float(lat), float(lng)
        if not (math.isfinite(lat) and math.isfinite(lng)):
            return ""
        parts = []
        for category, icon in ORDER:
            found = nearest(lat, lng, category)
            if not found:
                continue
            minutes = max(1, int(round(found[0] * DETOUR / WALK_M_PER_MIN)))
            name = _label(found[1])
            parts.append(f"{icon} {name + ' ' if name else ''}~{minutes} min")
        return "🚶 " + " · ".join(parts) if parts else ""
    except Exception as ex:
        LOG.warning("Place line failed (%s)", type(ex).__name__)
        return ""
