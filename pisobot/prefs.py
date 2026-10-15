"""User choices made in Telegram (Save / Hide buttons, commands), stored in Cloudflare D1.
The Worker in worker/ writes them the moment a button is tapped; runs read them here and write
back only what the run itself changed (saved-home costs, disappearances, report time)."""
import copy
import hashlib
import json
import sys

from . import d1


def load():
    p = {"saved": {}, "hidden": [], "hidden_dev": [], "feedback": []}
    if not d1.configured():
        print("D1 is not configured: running without saved/hidden choices", file=sys.stderr)
    else:
        p["saved"] = {r["key"]: json.loads(r["info"]) for r in d1.query("SELECT key, info FROM saved")}
        p["hidden"] = [r["key"] for r in d1.query("SELECT key FROM hidden")]
        p["hidden_dev"] = [[r["lat"], r["lng"]] for r in d1.query("SELECT lat, lng FROM hidden_dev")]
        p.update({r["name"]: r["value"] for r in d1.query("SELECT name, value FROM meta")})
        try:
            p["feedback"] = d1.query("SELECT key, action, reason FROM feedback")
        except Exception:
            p["feedback"] = []
    _snapshot(p)
    return p


def restart(full=False):
    """Clears preferences in D1. full=True also clears saved homes and feedback."""
    if not d1.configured():
        return
    d1.query("DELETE FROM hidden")
    d1.query("DELETE FROM hidden_dev")
    d1.query("DELETE FROM meta WHERE name = 'last_report'")
    if full:
        d1.query("DELETE FROM saved")
        d1.query("DELETE FROM feedback")


def _snapshot(p):
    # what this p was loaded with, so save() writes only the run's own changes and never
    # overwrites taps the Worker recorded while the run was busy
    p["_loaded"] = copy.deepcopy({"saved": p["saved"], "last_report": p.get("last_report")})


def save(p):
    if not d1.configured():
        return
    loaded = p.get("_loaded", {})
    before = loaded.get("saved", {})
    for key in before.keys() - p["saved"].keys():
        d1.query("DELETE FROM saved WHERE key = ?", key)
    for key, info in p["saved"].items():
        if before.get(key) != info:
            # UPDATE, not upsert: a home unsaved via the Worker mid-run must stay unsaved
            d1.query("UPDATE saved SET info = ? WHERE key = ?", json.dumps(info), key)
    if p.get("last_report") and p["last_report"] != loaded.get("last_report"):
        d1.query("INSERT INTO meta (name, value) VALUES ('last_report', ?) "
                 "ON CONFLICT(name) DO UPDATE SET value = excluded.value", p["last_report"])
    _snapshot(p)


def publish(known, chunk=200):
    """Let the Worker resolve a card's button (short id) to its listing.
    known: {key: {"lat", "lng", "address", "url", "cost"}}."""
    if not d1.configured():
        return
    rows = [{"sid": sid(key), "key": key, **{f: info.get(f) for f in ("lat", "lng", "url", "address", "cost")}}
            for key, info in known.items()]
    columns = ("sid", "key", "lat", "lng", "url", "address", "cost")
    extract = ", ".join(f"json_extract(value, '$.{c}')" for c in columns)
    update = ", ".join(f"{c} = excluded.{c}" for c in columns[1:])
    # one statement per chunk: json_each expands the rows server-side ("WHERE true" lets
    # SQLite parse the upsert after a SELECT)
    for start in range(0, len(rows), chunk):
        d1.query(f"INSERT INTO listings ({', '.join(columns)}) SELECT {extract} FROM json_each(?) "
                 f"WHERE true ON CONFLICT(sid) DO UPDATE SET {update}", json.dumps(rows[start:start + chunk]))


def sid(key):
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def is_hidden(p, l):
    if l.key in p["hidden"]:
        return True
    return any(abs(l.lat - lat) < 0.0015 and abs(l.lng - lng) < 0.0025 for lat, lng in p["hidden_dev"])
