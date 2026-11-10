#!/usr/bin/env python3
"""Measure how often an advert can be matched to an Energy Performance Certificate, and how right it is.

    python scripts/eval_epc.py --cache /path/to/state/cache.json [--limit 40] [--db /tmp/epc-eval.db]

Reads the rental adverts the bot has cached (state/cache.json, namespace rent_search; opened read-only,
never written) and keeps those that carry a full postcode (in the address text, or the 'full_postcode' field).
Without EPC_API_TOKEN in the environment (only its presence is checked; the value is never printed) it prints
how many adverts qualify and stops. With a token it matches each one (live requests, paced, capped at
EPC_DAILY_LIMIT, cached in a scratch SQLite file, never the bot's own database) and prints:
  * match rate: exact / range / none, with the reasons for none
  * for adverts that state their own area: the certificate area minus the advert area (exact matches: error;
    range matches: whether the advert's area falls inside the range)
  * requests spent
Register for the token at https://get-energy-performance-data.communities.gov.uk (GOV.UK One Login), then
export EPC_API_TOKEN=... and set STORAGE=local.
"""
import argparse
import collections
import datetime as dt
import json
import os
import statistics
import sys
import tempfile
from dataclasses import fields

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pisobot import epc  # noqa: E402
from pisobot.models import Listing  # noqa: E402

DEFAULT_DB = os.path.join(tempfile.gettempdir(), "pisobot-eval-epc.db")


def load_listings(path):
    names = {f.name for f in fields(Listing)}
    with open(path, "rb") as f:
        data = json.load(f)
    out = {}
    for entry in (data.get("rent_search") or {}).values():
        if isinstance(entry, dict):
            for item in entry.get("listings") or []:
                l = Listing(**{k: v for k, v in item.items() if k in names})
                if epc.postcode_of(l):
                    out.setdefault(l.key, l)
    return list(out.values())


def evaluate(listings, client, out=print, today=None):
    results = []
    for l in listings:
        try:
            results.append((l, epc.match(l, epc.postcode_of(l), client, today)))
        except epc.Unavailable as ex:
            out(f"  stopped: {ex}")
            break
    counts = collections.Counter(r["match"] for _, r in results)
    n = len(results)
    out(f"\nMatched {n} adverts: " + ", ".join(f"{k} {counts[k]} ({100 * counts[k] / n:.0f}%)"
                                               for k in ("exact", "range", "none")) if n else "\nNothing matched")
    reasons = collections.Counter(r.get("reason", "")[:60] for _, r in results if r["match"] == "none")
    for reason, c in reasons.most_common():
        out(f"  none: {reason} x{c}")
    errors, inside, outside = [], 0, 0
    for l, r in results:
        stated = l.floor_area_m2
        if not stated:
            continue
        if r["match"] == "exact":
            errors.append(r["low"] - stated)
        elif r["match"] == "range":
            if r["low"] - 1 <= stated <= r["high"] + 1:
                inside += 1
            else:
                outside += 1
    if errors:
        out(f"\nExact matches with a stated area: {len(errors)}; certificate minus advert m²: "
            f"median {statistics.median(errors):+.1f}, mean absolute {statistics.mean(abs(e) for e in errors):.1f}, "
            f"worst {max(errors, key=abs):+.1f}")
    out(f"Range matches with a stated area: {inside} inside the range, {outside} outside")
    return results


def main(argv=None, env=None, client=None, out=print):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=os.path.join(ROOT, "state", "cache.json"))
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args(argv)
    env = os.environ if env is None else env
    listings = load_listings(args.cache)[:args.limit]
    out(f"{len(listings)} cached adverts carry a full postcode (limit {args.limit})")
    if not listings:
        return 1
    if client is None and not (env.get("EPC_API_TOKEN") or env.get("EPC_TOKEN") or "").strip():
        out("EPC_API_TOKEN is not set: the certificate half of the evaluation was NOT run. "
            "Register at https://get-energy-performance-data.communities.gov.uk, export EPC_API_TOKEN and rerun.")
        return 0
    client = client or epc.Epc(store=epc.Store(args.db))
    evaluate(listings, client, out)
    out(f"Requests spent this run: {client.requests_made}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
