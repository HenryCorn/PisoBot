"""Read-only, per-person saved-home dashboard."""
import argparse
import datetime as dt
import html
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time

from . import localdb

LOG = logging.getLogger(__name__)
STALE_AFTER = dt.timedelta(hours=24)
MAX_HTML_BYTES = 1_000_000
MAX_SAVED = 250
_jobs = queue.Queue(maxsize=1)
_worker_lock = threading.Lock()
_worker_started = False


class DashboardError(ValueError):
    pass


def enabled():
    return os.environ.get("DASHBOARD") == "1"


def _exists(db, schema, table):
    if schema not in {row[1] for row in db.execute("PRAGMA database_list")}:
        return False
    return db.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _rows(db, schema, sql, params=()):
    return [dict(row) for row in db.execute(f"{sql}", params)]


def _iso(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _shown(value):
    if value is None:
        return "—"
    if isinstance(value, float) and not math.isfinite(value):
        return "—"
    return html.escape(str(value), quote=True)


def _money(value):
    try:
        number = float(value)
        return f"£{number:,.0f}" if math.isfinite(number) else "—"
    except (TypeError, ValueError, OverflowError):
        return "—"


def _safe_url(value):
    value = str(value or "")
    return value if value.startswith(("https://", "http://")) else ""


def _status(ad, now):
    if not ad:
        return "unknown", "Not checked: no archive record for this advert."
    last_seen = _iso(ad.get("last_seen"))
    stamp = last_seen.strftime("%Y-%m-%d %H:%M UTC") if last_seen else "unknown time"
    if ad.get("status") == "unseen" and int(ad.get("missed") or 0) >= 2:
        return "unavailable", f"Not found in {int(ad['missed'])} complete sweeps since {stamp}."
    age = now - last_seen if last_seen else None
    if ad.get("status") == "active" and age is not None and dt.timedelta(0) <= age <= STALE_AFTER:
        return "available", f"Seen on the portal at {stamp}."
    if ad.get("status") == "unseen":
        return "unknown", f"Not found in {int(ad.get('missed') or 0)} complete sweep(s) since {stamp}; confirmation needs 2."
    return "unknown", f"Last checked at {stamp}; evidence is stale after 24 hours."


def _chart(title, body):
    return f'<section class="chart"><h2>{title}</h2>{body}</section>'


def _svg_lines(series, label, width=680, height=240):
    values = [point[1] for points in series for point in points if point[1] is not None]
    if not values:
        return '<p class="muted">Not enough archived data yet.</p>'
    lo, hi = min(values), max(values)
    hi = hi if hi > lo else lo + 1
    colors = ("#2563eb", "#e11d48", "#059669", "#9333ea", "#d97706", "#0891b2")
    paths = []
    for i, points in enumerate(series):
        coords = []
        for x, y in points:
            if y is None:
                continue
            px = 40 + (width - 60) * x
            py = 18 + (height - 50) * (1 - (y - lo) / (hi - lo))
            coords.append(f"{px:.1f},{py:.1f}")
        if coords:
            paths.append(f'<polyline points="{" ".join(coords)}" fill="none" stroke="{colors[i % len(colors)]}" stroke-width="2"/>')
    return (f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(label, quote=True)}">'
            f'<text x="4" y="24">{_money(hi)}</text><text x="4" y="{height - 8}">{_money(lo)}</text>'
            f'{"".join(paths)}</svg>')


def _render_charts(flats, market, history, now):
    bedroom_groups = {}
    for flat in flats:
        if flat["beds"] is not None:
            bedroom_groups.setdefault(flat["beds"], []).append(flat["rent"])
    distribution = []
    for beds in sorted(bedroom_groups):
        rents = [r for r in market.get(beds, []) if r is not None]
        saved = [r for r in bedroom_groups[beds] if r is not None]
        if rents:
            low, high = min(rents), max(rents)
            span = max(high - low, 1)
            markers = "".join(
                f'<circle cx="{40 + 580 * (value - low) / span:.1f}" cy="18" r="6">'
                f'<title>Saved {_money(value)}</title></circle>' for value in saved)
            distribution.append(
                f'<div><b>{_shown(beds)} bed</b> · market {_money(low)}–{_money(high)} (n={len(rents)})'
                f'<svg viewBox="0 0 640 42" role="img" aria-label="{_shown(beds)} bedroom rent range">'
                f'<line x1="40" y1="18" x2="620" y2="18" stroke="#667085" stroke-width="8"/>'
                f'{markers}<text x="35" y="40">{_money(low)}</text><text x="565" y="40">{_money(high)}</text></svg>'
                f'<small>Saved rents: {", ".join(_money(v) for v in saved) or "—"}</small></div>')
    rent_chart = "".join(distribution) or '<p class="muted">No comparable archived rents.</p>'

    commutes = [(flat["commute"], flat["rent"], flat["address"]) for flat in flats
                if flat["commute"] is not None and flat["rent"] is not None]
    if commutes:
        xs = [p[0] for p in commutes]
        ys = [p[1] for p in commutes]
        xmin, xmax = min(xs), max(xs) or min(xs) + 1
        ymin, ymax = min(ys), max(ys) or min(ys) + 1
        points = "".join(
            f'<circle cx="{40 + 610 * (x - xmin) / (xmax - xmin or 1):.1f}" '
            f'cy="{18 + 180 * (1 - (y - ymin) / (ymax - ymin or 1)):.1f}" r="5"><title>'
            f'{_shown(address)} · {_money(y)} · {x} min</title></circle>'
            for x, y, address in commutes)
        scatter = (f'<svg viewBox="0 0 680 240" role="img" aria-label="Saved flat rent versus commute">'
                   f'<text x="40" y="232">{xmin} min</text><text x="580" y="232">{xmax} min</text>'
                   f'{points}</svg>')
    else:
        scatter = '<p class="muted">No saved flats have both rent and commute data.</p>'

    changed = [entry for entry in history
               if len({point[1] for point in entry["points"] if point[1] is not None}) > 1]
    line_series = []
    for entry in changed:
        prices = [point[1] for point in entry["points"] if point[1] is not None]
        if not prices:
            continue
        line_series.append([(i / max(1, len(prices) - 1), v)
                            for i, v in enumerate(prices)])
    if line_series:
        history_svg = _svg_lines(line_series, "Price history for saved flats")
        legend = "".join(f'<span>■ {_shown(entry["address"])}</span>' for entry in changed)
        history_html = history_svg + f'<div class="legend">{legend}</div>'
    else:
        history_html = '<p class="muted">No saved flats have recorded price changes.</p>'

    weeks = {}
    for flat in flats:
        first = _iso(flat["first_seen"])
        if first:
            monday = (first.date() - dt.timedelta(days=first.weekday())).isoformat()
            weeks[monday] = weeks.get(monday, 0) + 1
    week_values = sorted(weeks.items())
    max_week = max(weeks.values(), default=1)
    week_html = ('<div class="barlist">' + "".join(
        f'<div><span>{_shown(week)}</span><b class="weekbar" style="width:{max(5, 100 * count / max_week):.1f}%">{count}</b></div>'
        for week, count in week_values) + '</div>' if week_values else
        '<p class="muted">No first-seen dates are available.</p>')
    return (_chart("Saved rents vs archived market (same bedroom count)", rent_chart)
            + _chart("Saved rent vs commute", scatter)
            + _chart("Price history · changed saved flats", history_html)
            + _chart("Saved flats first seen per week", week_html))


def build(person_id, db_readonly_connection, now):
    """Build one person's dashboard from their read-only database connection.

    The connection may have a read-only `market` database attached. Otherwise archive tables
    are read from the same database, which is convenient for local fixtures and the owner profile.
    """
    now = _iso(now) or dt.datetime.now(dt.timezone.utc)
    market_schema = "market" if _exists(db_readonly_connection, "market", "adverts") else "main"
    if not _exists(db_readonly_connection, "main", "saved"):
        raise DashboardError("Saved-home database is unavailable.")
    saved_rows = _rows(db_readonly_connection, "main", "SELECT key, info FROM saved ORDER BY key LIMIT ?",
                       (MAX_SAVED + 1,))
    if len(saved_rows) > MAX_SAVED:
        raise DashboardError(f"Too many saved homes to render safely (limit {MAX_SAVED}).")
    archived = {}
    observations = {}
    keys = [str(row["key"]) for row in saved_rows]
    marks = ",".join("?" for _ in keys)
    in_keys = f" WHERE key IN ({marks})" if keys else " WHERE 0"
    if keys and _exists(db_readonly_connection, market_schema, "adverts"):
        archived = {r["key"]: r for r in _rows(db_readonly_connection, market_schema,
                                                "SELECT * FROM adverts" + in_keys, keys)}
    if keys and _exists(db_readonly_connection, market_schema, "observations"):
        for row in _rows(db_readonly_connection, market_schema,
                         "SELECT key, at, rent, status FROM observations" + in_keys + " ORDER BY id", keys):
            observations.setdefault(row["key"], []).append(row)
    sweep_times = {}
    if _exists(db_readonly_connection, market_schema, "sweeps"):
        sweep_times = {r["source"]: r["at"] for r in _rows(
            db_readonly_connection, market_schema,
            "SELECT source, MAX(finished_at) AS at FROM sweeps WHERE complete=1 GROUP BY source")}
    verdicts = {}
    if keys and _exists(db_readonly_connection, market_schema, "verdicts"):
        verdicts = {r["key"]: r for r in _rows(
            db_readonly_connection, market_schema,
            "SELECT * FROM verdicts WHERE chat_id=? AND key IN (" + marks + ")",
            (str(person_id), *keys))}
    stages = {}
    if keys and _exists(db_readonly_connection, "main", "pipeline"):
        stages = {r["key"]: r for r in _rows(db_readonly_connection, "main",
                                               "SELECT * FROM pipeline" + in_keys, keys)}
    market_rents = {}
    beds = sorted({ad.get("bedrooms") for ad in archived.values() if ad.get("bedrooms") is not None})
    if beds:
        bed_marks = ",".join("?" for _ in beds)
        for row in _rows(db_readonly_connection, market_schema,
                         f"SELECT bedrooms, rent FROM adverts WHERE bedrooms IN ({bed_marks}) "
                         "AND rent IS NOT NULL AND status='active' LIMIT 20000", beds):
            market_rents.setdefault(row["bedrooms"], []).append(row["rent"])

    flats = []
    price_history = []
    for row in saved_rows:
        try:
            info = json.loads(row["info"])
        except (ValueError, TypeError):
            info = {}
        if not isinstance(info, dict):
            info = {}
        key = str(row["key"])
        ad = archived.get(key)
        verdict = verdicts.get(key) or {}
        pipeline = stages.get(key) or {}
        try:
            data = json.loads(ad.get("data") or "{}") if ad else {}
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        status, evidence = _status(ad, now)
        if ad is None:
            source = key.split(":", 1)[0]
            swept_at = _iso(sweep_times.get(source))
            evidence = (f"No archive record; {source} last completed a sweep at "
                        f"{swept_at.strftime('%Y-%m-%d %H:%M UTC')}."
                        if swept_at else f"No archive record; {source} has no completed sweep recorded.")
        rent = ad.get("rent") if ad else info.get("rent")
        total = verdict.get("all_in") or info.get("cost") or (rent if rent is not None else None)
        beds = ad.get("bedrooms") if ad else info.get("bedrooms")
        area = data.get("floor_area_m2", info.get("floor_area_m2"))
        address = info.get("address") or (ad.get("address") if ad else None) or pipeline.get("address") or key
        url = _safe_url(info.get("url") or (ad.get("url") if ad else None) or pipeline.get("url"))
        lat = info.get("lat") if info.get("lat") is not None else (ad.get("lat") if ad else None)
        lng = info.get("lng") if info.get("lng") is not None else (ad.get("lng") if ad else None)
        first_seen = ad.get("first_seen") if ad else None
        seen = _iso(first_seen)
        days = max(0, (now.date() - seen.date()).days) if seen else None
        stage = pipeline.get("stage") or "saved"
        image = _safe_url(data.get("image"))
        per_m2 = (float(rent) / float(area)) if rent is not None and area else None
        flat = {
            "key": key, "status": status, "evidence": evidence, "rent": rent, "total": total,
            "beds": beds, "area": area, "per_m2": per_m2,
            "commute": verdict.get("travel_minutes"), "first_seen": first_seen,
            "days": days, "stage": stage, "url": url, "address": address, "image": image,
            "lat": lat, "lng": lng, "floor": data.get("floor"),
            "status_label": {"available": "Available", "unavailable": "Let agreed / removed",
                             "unknown": "Unknown"}[status],
        }
        flats.append(flat)
        history = observations.get(key, [])
        if history:
            price_history.append({"address": address, "points": [(r["at"], r["rent"]) for r in history]})

    counts = {name: sum(flat["status"] == name for flat in flats)
              for name in ("available", "unavailable", "unknown")}
    registry_schema = market_schema if _exists(db_readonly_connection, market_schema, "users") else "main"
    person = str(person_id)
    if _exists(db_readonly_connection, registry_schema, "users"):
        person_rows = _rows(db_readonly_connection, registry_schema,
                            "SELECT name FROM users WHERE chat_id=?", (person,))
        if person_rows and person_rows[0].get("name"):
            person = person_rows[0]["name"]
    count_text = (f'<span>{len(flats)} saved</span><span>{counts["available"]} available</span>'
                  f'<span>{counts["unavailable"]} no longer available</span>'
                  f'<span>{counts["unknown"]} unknown</span>')
    cards = []
    for flat in flats:
        url = html.escape(flat["url"], quote=True)
        photo = (f'<img src="{html.escape(flat["image"], quote=True)}" alt="" loading="lazy">'
                 if flat["image"] else "")
        link_start = f'<a href="{url}" rel="noopener noreferrer">' if url else ""
        link_end = "</a>" if url else ""
        commute = f'{_shown(flat["commute"])} min' if flat["commute"] is not None else "—"
        area = f'{_shown(flat["area"])} m²' if flat["area"] is not None else "—"
        per_m2 = _money(flat["per_m2"]) + "/m²/mo" if flat["per_m2"] is not None else "—"
        card_status = flat["status"]
        cards.append(
            f'<article class="flat {card_status}" data-status="{card_status}" '
            f'data-price="{_shown(flat["rent"]) if flat["rent"] is not None else ""}" '
            f'data-commute="{_shown(flat["commute"]) if flat["commute"] is not None else ""}">'
            f'<div class="photo">{photo}</div><div class="details"><div class="heading">'
            f'{link_start}<strong>{_shown(flat["address"])}</strong>{link_end}'
            f'<span class="badge">{flat["status_label"]}</span></div>'
            f'<p class="evidence">{_shown(flat["evidence"])}</p>'
            f'<p>Rent <b>{_money(flat["rent"])}</b> · total monthly {_money(flat["total"])}</p>'
            f'<p>{_shown(flat["beds"])} beds · {area} · {per_m2} · commute {commute}</p>'
            f'<p>First seen {_shown(flat["first_seen"])} · days on market {_shown(flat["days"])} · '
            f'pipeline {_shown(flat["stage"])} · floor {_shown(flat["floor"])}</p>'
            f'{link_start}Open advert ↗{link_end}</div></article>')

    map_pins = [flat for flat in flats if flat["lat"] is not None and flat["lng"] is not None
                and all(type(flat[k]) in (int, float) and math.isfinite(float(flat[k])) for k in ("lat", "lng"))]
    json_pins = json.dumps([
        {"lat": f["lat"], "lng": f["lng"], "status": f["status"], "address": str(f["address"]),
         "rent": _money(f["rent"]), "url": f["url"]}
        for f in map_pins], ensure_ascii=True).replace("</", "<\\/")
    body = (
        '<header><button id="theme" type="button">Toggle light / dark</button><h1>Saved flats</h1><p>For ' + _shown(person) + ' · generated '
        + _shown(now.strftime("%Y-%m-%d %H:%M UTC")) + '</p><div class="counts">' + count_text + '</div>'
        + '<p class="rule">Availability rule: “Available” requires an active archive sighting within '
        '24 hours. Older evidence is Unknown. “Let agreed / removed” means absent in at least 2 '
        'complete in-scope sweeps; absence does not prove it was let. If no source sweep/archive '
        'record exists, status is Unknown. The archive cannot verify a portal\'s explicit let-agreed '
        'label, so it is not inferred.</p></header>'
        + '<main><section><h2>All saved flats</h2><div class="filters">'
        + '<label>Status <select id="status"><option value="all">All</option><option value="available">Available</option>'
        + '<option value="unavailable">No longer available</option><option value="unknown">Unknown</option></select></label>'
        + '<label>Max rent <input id="price" type="number" min="0" placeholder="Any"></label>'
        + '<label>Max commute (min) <input id="commute" type="number" min="0" placeholder="Any"></label>'
        + '<label>Sort <select id="sort"><option value="price">Rent</option><option value="commute">Commute</option>'
        + '<option value="status">Status</option></select></label></div>'
        + '<div id="flats">' + "".join(cards) + '</div></section>'
        + _render_charts(flats, market_rents, price_history, now)
        + '<section class="chart"><h2>Map</h2><p class="muted">Map tile requests disclose the displayed area to the tile server.</p>'
        + '<div id="map"><svg id="map-fallback" viewBox="0 0 600 320" role="img" aria-label="Saved flat location pins"></svg></div>'
        + '</section></main><footer>Market comparisons use the locally archived adverts, not all London rentals.</footer>'
        + '<script>const pins=' + json_pins + ';'
        + 'const colors={available:"#16803c",unavailable:"#777",unknown:"#d97706"};'
        + 'const all=[...document.querySelectorAll(".flat")];'
        + 'document.querySelector("#theme").addEventListener("click",()=>{const r=document.documentElement,'
        + 'current=r.dataset.theme||(matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");'
        + 'r.dataset.theme=current==="dark"?"light":"dark";});'
        + 'function filter(){const s=document.querySelector("#status").value,price=document.querySelector("#price").value,'
        + 'commute=document.querySelector("#commute").value,p=price===""?null:Number(price),'
        + 'c=commute===""?null:Number(commute),sort=document.querySelector("#sort").value,list=document.querySelector("#flats");'
        + 'all.sort((a,b)=>{if(sort==="status")return a.dataset.status.localeCompare(b.dataset.status);'
        + 'const av=a.dataset[sort],bv=b.dataset[sort];if(!av)return bv?1:0;if(!bv)return -1;return Number(av)-Number(bv);});'
        + 'all.forEach(x=>{list.append(x);x.hidden=!( (s==="all"||x.dataset.status===s)&&'
        + '(p===null||(x.dataset.price!==""&&Number(x.dataset.price)<=p))'
        + '&&(c===null||(x.dataset.commute!==""&&Number(x.dataset.commute)<=c)));});}'
        + 'document.querySelectorAll(".filters input,.filters select").forEach(x=>{x.addEventListener("input",filter);'
        + 'x.addEventListener("change",filter);});'
        + 'filter();'
        + 'const fallback=document.querySelector("#map-fallback");'
        + 'function fallbackMap(){const ns="http://www.w3.org/2000/svg";if(!pins.length){const t=document.createElementNS(ns,"text");'
        + 't.setAttribute("x","20");t.setAttribute("y","40");t.textContent="No saved coordinates";fallback.append(t);return;}'
        + 'const xs=pins.map(p=>p.lng),ys=pins.map(p=>p.lat),xmin=Math.min(...xs),xmax=Math.max(...xs)||xmin+1,'
        + 'ymin=Math.min(...ys),ymax=Math.max(...ys)||ymin+1;pins.forEach(p=>{const c=document.createElementNS(ns,"circle"),'
        + 'x=20+560*(p.lng-xmin)/(xmax-xmin||1),y=20+280*(1-(p.lat-ymin)/(ymax-ymin||1)),t=document.createElementNS(ns,"title");'
        + 'c.setAttribute("cx",x);c.setAttribute("cy",y);c.setAttribute("r","7");c.setAttribute("fill",colors[p.status]);'
        + 't.textContent=p.address+" · "+p.rent;c.append(t);fallback.append(c);});}'
        + 'fallbackMap();'
        + 'const css=document.createElement("link");css.rel="stylesheet";css.href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";document.head.append(css);'
        + 'const script=document.createElement("script");script.src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";'
        + 'script.onload=()=>{try{const map=L.map("map").setView([51.51,-0.1],11),'
        + 'tiles=L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:"© OpenStreetMap"}).addTo(map);'
        + 'tiles.on("tileload",()=>{fallback.style.display="none";});'
        + 'tiles.on("tileerror",()=>{fallback.style.display="block";});'
        + 'pins.forEach(p=>{const marker=L.circleMarker([p.lat,p.lng],{color:colors[p.status]}).addTo(map);'
        + 'const box=document.createElement("div"),label=document.createElement("strong");label.textContent=p.address;box.append(label,'
        + 'document.createElement("br"),document.createTextNode(p.rent+" · "));if(p.url){const a=document.createElement("a");'
        + 'a.href=p.url;a.textContent="Open advert";a.rel="noopener noreferrer";box.append(a);}marker.bindPopup(box);});'
        + 'if(pins.length){const bounds=L.latLngBounds(pins.map(p=>[p.lat,p.lng]));map.fitBounds(bounds.pad(.15));}'
        + '}catch(e){fallback.style.display="block";}};document.body.append(script);'
        + '</script>'
    )
    style = """<style>
:root{color-scheme:light dark;--bg:#fff;--fg:#202124;--muted:#667085;--card:#f7f8fa;--line:#d0d5dd}
@media(prefers-color-scheme:dark){:root{--bg:#16181d;--fg:#eef0f4;--muted:#a9b0bc;--card:#22262d;--line:#414752}}
:root[data-theme=light]{color-scheme:light;--bg:#fff;--fg:#202124;--muted:#667085;--card:#f7f8fa;--line:#d0d5dd}
:root[data-theme=dark]{color-scheme:dark;--bg:#16181d;--fg:#eef0f4;--muted:#a9b0bc;--card:#22262d;--line:#414752}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
header,main,footer{max-width:1000px;margin:auto;padding:16px}h1{margin:.2em 0}h2{font-size:1.15rem}
.counts,.filters,.heading{display:flex;flex-wrap:wrap;gap:12px;align-items:center}.counts span,.badge{padding:4px 9px;border-radius:20px;background:var(--card)}
.rule,.muted,.evidence,footer{color:var(--muted)}.flat{display:flex;gap:12px;margin:10px 0;padding:12px;border:1px solid var(--line);border-radius:12px;background:var(--card)}
.flat.unavailable{opacity:.62;filter:grayscale(1)}.photo{width:128px;flex:none}.photo img{width:100%;height:100px;object-fit:cover;border-radius:8px}
.details{min-width:0}.heading{justify-content:space-between}.available .badge{color:#16803c}.unavailable .badge{color:#888}.unknown .badge{color:#d97706}
a{color:#2563eb;overflow-wrap:anywhere}.chart{margin:20px 0;padding:14px;border:1px solid var(--line);border-radius:12px}
.filters label{display:grid;gap:3px}.filters input,.filters select{max-width:180px;padding:7px}
svg{display:block;width:100%;max-height:300px}svg text{fill:var(--fg);font-size:12px}svg circle{stroke:#fff;stroke-width:1}
.barrow,.barlist>div{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid var(--line);padding:6px}
.barlist>div{display:block}.weekbar{display:block;background:#2563eb;color:#fff;text-align:right;padding-right:5px;border-radius:4px}
.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:.85em}.legend span:nth-child(6n+1){color:#2563eb}
#map{height:320px;position:relative;background:var(--card);border-radius:8px}#map-fallback{height:320px;position:absolute;inset:0;z-index:1000;pointer-events:none}
@media(max-width:600px){.flat{gap:8px;padding:9px}.photo{width:88px}.photo img{height:80px}.heading{align-items:flex-start}}
</style>"""
    page = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Saved flats dashboard</title>' + style + '</head><body>' + body + '</body></html>'
    if len(page.encode("utf-8")) > MAX_HTML_BYTES:
        raise DashboardError(f"Dashboard exceeds the {MAX_HTML_BYTES:,}-byte size limit.")
    return page


def _database(person_id):
    if not re.fullmatch(r"-?\d+", str(person_id)):
        raise DashboardError("Person id must be numeric.")
    owner_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    base = localdb.state_dir()
    path = base / "pisobot.db" if str(person_id) == owner_id else base / "users" / str(person_id) / "pisobot.db"
    if not path.is_file():
        raise DashboardError("Saved-home database was not found.")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    deadline = time.monotonic() + 5
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    primary = base / "pisobot.db"
    try:
        if path.resolve() != primary.resolve() and primary.is_file():
            db.execute("ATTACH DATABASE ? AS market", (primary.as_uri() + "?mode=ro",))
            db.execute("PRAGMA market.query_only=ON")
    except Exception:
        db.close()
        raise
    return db


def render_person(person_id):
    db = _database(person_id)
    try:
        return build(person_id, db, dt.datetime.now(dt.timezone.utc))
    finally:
        db.close()


def _worker():
    while True:
        client, person_id, chat_id = _jobs.get()
        try:
            rendered = render_person(person_id)
            client.send_document(chat_id, rendered.encode("utf-8"), "saved-flats.html")
        except Exception:
            LOG.warning("Dashboard generation or delivery failed")
            try:
                client.call("sendMessage", chat_id=chat_id,
                            text="⚠️ I couldn't build the dashboard. Your saved homes and alerts are unchanged.")
            except Exception:
                LOG.warning("Dashboard failure notice could not be sent")
        finally:
            _jobs.task_done()


def enqueue(client, person_id, chat_id):
    global _worker_started
    if not enabled():
        return False
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, name="saved-dashboard", daemon=True).start()
            _worker_started = True
    try:
        _jobs.put_nowait((client, str(person_id), str(chat_id)))
        return True
    except queue.Full:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build a saved-flats dashboard from local SQLite data.")
    parser.add_argument("--person", required=True, help="numeric Telegram chat id")
    parser.add_argument("--out", required=True, help="output HTML path")
    args = parser.parse_args(argv)
    db = _database(args.person)
    try:
        result = build(args.person, db, dt.datetime.now(dt.timezone.utc))
    finally:
        db.close()
    target = Path(args.out)
    target.write_text(result, encoding="utf-8")


if __name__ == "__main__":
    main()
