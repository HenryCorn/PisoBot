"""Writes docs/index.html: an interactive map + sortable list of all current matches,
served by GitHub Pages (Settings -> Pages -> Deploy from branch: main, /docs)."""
import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

from . import collage, config, ranking

PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "index.html")


def write(matches, top, saved_keys, new_keys):
    rank = {l.key: i + 1 for i, l in enumerate(top)}
    rows = []
    for l in sorted(matches, key=ranking.score):
        d = l.to_dict()
        d.update(score=round(ranking.score(l), 3), rank=rank.get(l.key), saved=l.key in saved_keys,
                 new=l.key in new_keys, image=collage.small_url(l.image) if l.image else None)
        rows.append(d)
    meta = {
        "updated": dt.datetime.now(ZoneInfo("Europe/London")).strftime("%d %b %Y, %H:%M"),
        "target": {"name": config.TARGET_NAME, "lat": config.TARGET_LAT, "lng": config.TARGET_LNG},
        "max_cost": config.MAX_MONTHLY_COST, "max_travel": config.MAX_TRAVEL_MINUTES,
        "max_crime": config.MAX_CRIME_SCORE, "years": config.MORTGAGE_YEARS, "rate": config.MORTGAGE_RATE,
    }
    page = TEMPLATE.replace("__DATA__", json.dumps(rows).replace("</", "<\\/")) \
                   .replace("__META__", json.dumps(meta))
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH, "w", encoding="utf-8") as f:
        f.write(page)


TEMPLATE = r"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PisoBot map</title>
<meta name="robots" content="noindex">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>
:root{--bg:#f6f5f2;--panel:#fff;--ink:#1b1b1b;--muted:#6b6b6b;--line:#e3e1dc;--accent:#d6336c;--good:#2f9e44;--mid:#f08c00;--bad:#e03131;--chip:#f1efe9}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141414;--panel:#1e1e1e;--ink:#eee;--muted:#9a9a9a;--line:#333;--chip:#2a2a2a}}
:root[data-theme="dark"]{--bg:#141414;--panel:#1e1e1e;--ink:#eee;--muted:#9a9a9a;--line:#333;--chip:#2a2a2a}
*{box-sizing:border-box}
body{margin:0;font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:12px 16px;border-bottom:1px solid var(--line);background:var(--panel);display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}
h1{font-size:18px;margin:0}
.muted{color:var(--muted)}
.filters{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line)}
.filters label{display:flex;gap:6px;align-items:center;white-space:nowrap}
.filters input[type=range]{width:110px}
select,button{font:inherit;color:var(--ink);background:var(--chip);border:1px solid var(--line);border-radius:6px;padding:4px 8px}
main{display:grid;grid-template-columns:minmax(0,1fr) 440px;height:calc(100vh - 110px)}
#map{height:100%}
#list{overflow:auto;border-left:1px solid var(--line)}
.card{display:grid;grid-template-columns:120px 1fr;gap:10px;padding:10px 12px;border-bottom:1px solid var(--line);cursor:pointer;background:var(--panel)}
.card:hover,.card.sel{background:var(--chip)}
.card img,.ph{width:120px;height:80px;object-fit:cover;border-radius:6px;background:var(--chip)}
.cost{font-weight:700;font-size:15px}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.chip{background:var(--chip);border-radius:10px;padding:1px 7px;font-size:12px}
.warn{color:var(--bad)}
.rank{display:inline-block;background:var(--accent);color:#fff;border-radius:50%;width:20px;height:20px;text-align:center;font-size:12px;line-height:20px;margin-right:4px}
.pop img{width:240px;height:150px;object-fit:cover;border-radius:6px}
.pop table{border-collapse:collapse;margin:6px 0}.pop td{padding:1px 6px 1px 0}
.pin{border-radius:50%;border:2px solid #fff;box-shadow:0 0 3px rgba(0,0,0,.5);color:#fff;font:700 11px/18px system-ui;text-align:center}
a{color:var(--accent)}
@media (max-width:800px){main{grid-template-columns:1fr;grid-template-rows:45vh auto;height:auto}#map{height:45vh}#list{border-left:0}}
</style>
</head>
<body>
<header><h1>🏠 PisoBot</h1><span class="muted" id="summary"></span></header>
<div class="filters">
  <label>Max £/mo <input type="range" id="fCost" step="25"><b id="vCost"></b></label>
  <label>Max min <input type="range" id="fTravel" step="1"><b id="vTravel"></b></label>
  <label>Max crime <input type="range" id="fCrime" step="5"><b id="vCrime"></b></label>
  <label>Beds <select id="fBeds"><option value="1">1+</option><option value="2">2+</option><option value="3">3+</option></select></label>
  <label><input type="checkbox" id="fEst"> hide estimated</label>
  <label><input type="checkbox" id="fDep"> deposit ≤ £10k</label>
  <label>Sort <select id="sort"><option value="score">Best</option><option value="cost">Cost</option><option value="travel">Commute</option><option value="crime">Crime</option><option value="loan">Loan</option></select></label>
</div>
<main><div id="map"></div><div id="list"></div></main>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
const DATA=__DATA__, META=__META__;
const $=id=>document.getElementById(id), gbp=v=>v==null?'–':'£'+Math.round(v).toLocaleString('en-GB');
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
$('summary').textContent=`${DATA.length} homes ≤ ${gbp(META.max_cost)}/mo, ≤ ${META.max_travel} min to ${META.target.name} · updated ${META.updated}`;
for(const [id,max,val] of [['fCost',META.max_cost,META.max_cost],['fTravel',META.max_travel,META.max_travel],['fCrime',META.max_crime,META.max_crime]]){
  const el=$(id); el.min=id==='fCost'?500:id==='fTravel'?10:0; el.max=max; el.value=val;}
const map=L.map('map').setView([META.target.lat,META.target.lng],12);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:18,attribution:'© OpenStreetMap'}).addTo(map);
L.marker([META.target.lat,META.target.lng],{title:META.target.name}).addTo(map).bindPopup('🎯 '+META.target.name);
const layer=L.layerGroup().addTo(map); const markers={};
const sc=DATA.map(d=>d.score).sort((a,b)=>a-b), q1=sc[Math.floor(sc.length/3)]??0, q2=sc[Math.floor(2*sc.length/3)]??0;
const colour=s=>s<=q1?'var(--good)':s<=q2?'var(--mid)':'var(--bad)';  // thirds of today's matches
function popup(d){
  const fin=d.tenure==='sale'
    ?`<tr><td>Monthly</td><td><b>${gbp(d.cost)}</b> = mortgage ${gbp(d.mortgage)} + rent ${gbp(d.rent)} + service ${gbp(d.service)}</td></tr>
      <tr><td>Mortgage</td><td><b>${gbp(d.loan)}</b> over ${META.years} yrs @ ${META.rate}%</td></tr>
      <tr><td>Deposit</td><td><b>${gbp(d.deposit)}</b> · ${d.share_pct?Math.round(d.share_pct)+'% ':''}share ${gbp(d.share_price)}${d.full_value?' of '+gbp(d.full_value):''}</td></tr>`
    :`<tr><td>Rent</td><td><b>${gbp(d.cost)}</b>/mo</td></tr>`;
  return `<div class="pop">${d.image?`<img src="${esc(d.image)}" loading="lazy">`:''}
    <div><b>${d.rank?'#'+d.rank+' ':''}${esc(d.address)}</b></div>
    <div class="muted">${d.beds} bed · ${esc(d.scheme)} · ${d.source}${d.lease?' · lease '+d.lease+' yrs':''}${d.council_tax?' · CT '+d.council_tax:''}</div>
    <table>${fin}<tr><td>Commute</td><td><b>${d.travel} min</b>${d.route?' · '+esc(d.route):''}</td></tr>
    <tr><td>Crime</td><td>${d.crime}</td></tr></table>
    ${d.estimated.length?`<div class="muted">Estimated: ${esc(d.estimated.join(', '))}</div>`:''}
    ${d.warnings.length?`<div class="warn">⚠️ ${esc(d.warnings.join(' · '))}</div>`:''}
    <a href="${esc(d.url)}" target="_blank" rel="noopener">Open listing ↗</a></div>`;
}
function card(d){
  const chips=[`${d.beds} bed`,`${d.travel} min`,`🛡 ${d.crime}`];
  if(d.loan) chips.push('loan '+gbp(d.loan)); if(d.tenure==='sale') chips.push('dep '+gbp(d.deposit));
  if(d.new) chips.push('🆕'); if(d.saved) chips.push('❤️'); if(d.estimated.length) chips.push('est.');
  return `<div class="card" data-k="${esc(d.key)}">${d.image?`<img src="${esc(d.image)}" loading="lazy" alt="">`:'<div class="ph"></div>'}
    <div><div class="cost">${d.rank?`<span class="rank">${d.rank}</span>`:''}${gbp(d.cost)}/mo</div>
    <div>${esc(d.address)}</div><div class="muted">${esc(d.route||'')}</div>
    <div class="chips">${chips.map(c=>`<span class="chip">${esc(c)}</span>`).join('')}</div>
    ${d.warnings.length?`<div class="warn">⚠️ ${esc(d.warnings.join(' · '))}</div>`:''}</div></div>`;
}
function render(){
  const maxCost=+$('fCost').value, maxT=+$('fTravel').value, maxC=+$('fCrime').value, beds=+$('fBeds').value;
  $('vCost').textContent=gbp(maxCost); $('vTravel').textContent=maxT; $('vCrime').textContent=maxC;
  const key=$('sort').value;
  const rows=DATA.filter(d=>d.cost<=maxCost&&d.travel<=maxT&&d.crime<=maxC&&d.beds>=beds
      &&!($('fEst').checked&&d.estimated.length)&&!($('fDep').checked&&d.tenure==='sale'&&d.deposit>10000))
    .sort((a,b)=>(a[key]??1e9)-(b[key]??1e9));
  layer.clearLayers();
  for(const d of rows){
    const size=d.rank?20:14;
    const icon=L.divIcon({className:'',iconSize:[size,size],html:`<div class="pin" style="width:${size}px;height:${size}px;background:${colour(d.score)}">${d.rank||''}</div>`});
    markers[d.key]=L.marker([d.lat,d.lng],{icon,zIndexOffset:d.rank?1000-d.rank:0}).bindPopup(popup(d),{maxWidth:280}).addTo(layer);
  }
  $('list').innerHTML=rows.map(card).join('')||'<p style="padding:16px">No homes match these filters.</p>';
}
$('list').addEventListener('click',ev=>{const c=ev.target.closest('.card'); if(!c) return;
  const m=markers[c.dataset.k]; if(m){map.setView(m.getLatLng(),15); m.openPopup();}
  document.querySelectorAll('.card.sel').forEach(x=>x.classList.remove('sel')); c.classList.add('sel');});
for(const id of ['fCost','fTravel','fCrime','fBeds','fEst','fDep','sort']) $(id).addEventListener('input',render);
render();
</script>
</body>
</html>
"""
