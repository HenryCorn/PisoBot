#!/usr/bin/env python3
"""How often would each opportunity signal fire on real data? (G3)

    python scripts/eval_signals.py /path/to/COPY-of-pisobot.db [--today 2026-11-12] [--examples 10]

Give it a COPY of a production archive database (it is opened read-only, `mode=ro` + `query_only`, and never written; do not point
it at the live file). For every currently ACTIVE advert in the archive it runs the same detectors the bot would, with the same
archive reads (observation history, portal first-listed date, neighbouring active adverts), and prints, per signal, how many
adverts it fires on, its strength mix, and ten examples (key, url, label, evidence), so thresholds can be judged on real data.
It also prints the data coverage that limits each signal (adverts with a history, a recorded 'unseen' episode, a portal date,
a portal-stated area, photos). Nothing is sent, fetched or stored.
"""
import argparse
import collections
import datetime as dt
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pisobot import signals  # noqa: E402
from pisobot.sources import restore  # noqa: E402


def active_listings(reader):
    """Every active archived advert as a listing (the archive's own latest facts), in key order."""
    for r in reader.db.execute("SELECT key, url, rent, bedrooms, lat, lng, portal_first_listed, data FROM adverts "
                               "WHERE status = 'active' ORDER BY key"):
        try:
            data = json.loads(r["data"])
            data.update(url=r["url"], rent=r["rent"], bedrooms=r["bedrooms"], lat=r["lat"], lng=r["lng"])
            if r["portal_first_listed"] and not data.get("first_listed"):
                data["first_listed"] = r["portal_first_listed"]
            yield restore(data)
        except Exception:
            continue


def evaluate(path, today=None, examples=10, out=print):
    today = today or dt.date.today()
    fired, strengths, shown = collections.Counter(), collections.defaultdict(collections.Counter), collections.defaultdict(list)
    coverage = collections.Counter()
    total = with_card_signal = 0
    started = time.monotonic()
    with signals.ArchiveReader(path) as reader:
        counts = {t: reader.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("adverts", "observations")}
        for l in active_listings(reader):
            if l.tenure != "rent":
                continue
            total += 1
            rows = reader.history(l.key)
            coverage["with a rent observation history"] += len(rows) > 1
            coverage["with an 'unseen' episode recorded"] += any(r["status"] == "unseen" for r in rows)
            coverage["with a portal first-listed date"] += bool(l.first_listed)
            coverage["with a portal-stated area"] += bool(signals.stated_area(l))
            coverage["with 4 or more photos"] += len(l.images or []) >= signals.FEW_PHOTOS
            coverage["with a floorplan"] += bool(l.floorplan)
            found = signals.compute_all(l, reader, today)
            with_card_signal += bool(found[:signals.MAX_SHOWN])
            for s in found:
                fired[s.name] += 1
                strengths[s.name][s.strength] += 1
                if len(shown[s.name]) < examples:
                    shown[s.name].append((l.key, l.url, s.label, s.evidence))
    elapsed = time.monotonic() - started
    out(f"Archive copy: {counts['adverts']:,} adverts, {counts['observations']:,} observations; {total:,} active private rentals evaluated "
        f"in {elapsed:.1f} s ({1000 * elapsed / max(1, total):.1f} ms each, all reads included); as of {today}")
    out("\nData coverage (what limits each signal)")
    for name, n in coverage.items():
        out(f"  {n:>6,} ({100 * n / max(1, total):4.1f}%)  {name}")
    out(f"\nA card would carry at least one signal on {with_card_signal:,} of {total:,} adverts ({100 * with_card_signal / max(1, total):.1f}%)"
        f" (at most {signals.MAX_SHOWN} shown)")
    for name in signals.PRIORITY:
        n = fired[name]
        mix = ", ".join(f"{signals.STRENGTH_NAMES.get(k, k)} {v}" for k, v in sorted(strengths[name].items()))
        out(f"\n== {name}: fires on {n:,} adverts ({100 * n / max(1, total):.1f}%)" + (f"  [{mix}]" if n else ""))
        for key, url, label, evidence in shown[name]:
            out(f"   {key:14} {label}: {evidence}\n   {'':14} {url}")
    return {"total": total, "fired": dict(fired)}


def main(argv=None, out=print):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", help="path to a COPY of a production archive database (opened read-only)")
    ap.add_argument("--today", help="evaluate as of this date (YYYY-MM-DD)")
    ap.add_argument("--examples", type=int, default=10)
    args = ap.parse_args(argv)
    if not os.path.exists(args.db):
        out(f"no such file: {args.db}")
        return 2
    evaluate(args.db, dt.date.fromisoformat(args.today) if args.today else None, args.examples, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
