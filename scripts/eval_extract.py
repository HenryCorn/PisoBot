#!/usr/bin/env python3
"""Measure the keyword rules against the language-model reader on real cached adverts.

    python scripts/eval_extract.py --cache /path/to/state/cache.json [--sample 60] [--seed 7]

Reads the rental adverts the bot has cached (state/cache.json, namespace rent_search; opened
read-only, never written), runs the current keyword rules over ALL of them, and prints how many each
rule rejects. If ANTHROPIC_API_KEY is set (its presence is checked; the value is never printed) it
then runs the model on a sample that contains every advert the rules reject plus a random remainder
up to --sample, and prints the agreement table, every disagreement with the model's quoted phrase,
and the measured tokens and cost per advert. Without a key it says so and stops after the rules part.

Model results are cached in a scratch SQLite file (--db, default in the temp directory), so a rerun
does not pay for adverts already read. This script never touches the bot's own database.
"""
import argparse
import collections
import json
import os
import random
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pisobot import extract  # noqa: E402
from dataclasses import fields  # noqa: E402

from pisobot.models import Listing  # noqa: E402

DEFAULT_DB = os.path.join(tempfile.gettempdir(), "pisobot-eval-extract.db")
DAILY_ADVERTS = 300       # new adverts per day, for the cost projection


def restore(item):
    names = {f.name for f in fields(Listing)}
    return Listing(**{k: v for k, v in item.items() if k in names})


def load_listings(path):
    """Every cached rental advert that has a description, once per key. The file is opened read-only."""
    with open(path, "rb") as f:
        data = json.load(f)
    out = {}
    for name, entry in (data.get("rent_search") or {}).items():
        if not isinstance(entry, dict):
            continue
        for item in entry.get("listings") or []:
            if item.get("description"):
                listing = restore(item)
                out.setdefault(listing.key, listing)
    return list(out.values())


def rules_verdict(listing):
    """(rejected keywords, warning keywords) from the current rules."""
    rejected, warnings = listing._rental_keywords()
    return rejected, warnings


def snippet(listing, word, width=60):
    text = " ".join(listing.description.split())
    i = text.lower().find(word.lower().split()[0]) if word else -1
    i = max(0, i - width // 2) if i >= 0 else 0
    return text[i:i + width + 30]


def rules_report(listings, out=print):
    by_source = collections.defaultdict(lambda: collections.Counter())
    words = collections.Counter()
    rejected_listings = []
    for l in listings:
        rejected, warnings = rules_verdict(l)
        state = "rejected" if rejected else "warned" if warnings else "kept"
        by_source[l.source][state] += 1
        by_source[l.source]["total"] += 1
        for w in rejected:
            words[w] += 1
        if rejected:
            rejected_listings.append((l, rejected))
    out(f"Rules over {len(listings)} cached adverts")
    out(f"{'source':8}{'total':>7}{'rejected':>10}{'warned':>8}{'kept':>7}")
    for source, c in sorted(by_source.items()):
        out(f"{source:8}{c['total']:>7}{c['rejected']:>10}{c['warned']:>8}{c['kept']:>7}")
    out("Rejecting keywords: " + (", ".join(f"{w} x{n}" for w, n in words.most_common()) or "none"))
    for l, rejected in rejected_listings:
        out(f"  rules reject {l.key:12} [{', '.join(rejected)}] ...{snippet(l, rejected[0])}...")
    return rejected_listings


def choose_sample(listings, rejected_keys, size, seed):
    """Every rules-rejected advert, then a random remainder of the others up to `size`."""
    chosen = [l for l in listings if l.key in rejected_keys]
    others = sorted((l for l in listings if l.key not in rejected_keys), key=lambda l: l.key)
    random.Random(seed).shuffle(others)
    return chosen + others[:max(0, size - len(chosen))]


def stored_usage(path, key):
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT input_tokens, output_tokens, cache_read_tokens, cache_write_tokens "
                         "FROM extractions WHERE hash = ?", (key,)).fetchone()
    return dict(zip(("input", "output", "cache_read", "cache_write"), row)) if row else None


def model_report(sample, rejected_keys, extractor, db_path, out=print):
    rows, failed = [], []
    for l in sample:
        text = extract.build_input(l)
        ex = extractor.extract_text(text)
        if ex is None:
            failed.append(l.key)
            continue
        usage = stored_usage(db_path, extract.input_hash(text, extractor.model)) or {}
        rows.append((l, l.key in rejected_keys, ex, usage))
    out(f"\nModel ({extractor.model}) read {len(rows)} of {len(sample)} sampled adverts"
        + (f"; failed or skipped: {', '.join(failed)}" if failed else ""))

    table = collections.Counter((ex.kind, rules) for _, rules, ex, _ in rows)
    out(f"\nAgreement table (rows: model kind; columns: rules verdict)")
    out(f"{'model kind':16}{'rules reject':>14}{'rules keep':>12}")
    for kind in extract.KINDS:
        if table[(kind, True)] or table[(kind, False)]:
            out(f"{kind:16}{table[(kind, True)]:>14}{table[(kind, False)]:>12}")
    out(f"{'total':16}{sum(v for (k, r), v in table.items() if r):>14}{sum(v for (k, r), v in table.items() if not r):>12}")
    conf = collections.Counter((ex.kind, ex.confidence) for _, _, ex, _ in rows)
    out("Confidence: " + ", ".join(f"{k}/{c} x{n}" for (k, c), n in sorted(conf.items())))

    out("\nDisagreements (rules vs model), with the model's quoted phrase and what the policy would do")
    count = 0
    for l, rules, ex, _ in rows:
        drop = ex.kind in extract.REJECT_KINDS or ex.kind == "studio"
        if (rules and ex.kind == "whole_flat") or (not rules and drop):
            count += 1
            decision = extract.adjust(ex, rules)
            out(f"  {l.key:12} rules={'reject' if rules else 'keep':6} model={ex.kind}/{ex.confidence} "
                f"-> {decision.outcome} (final: {'reject' if decision.reject else 'keep'})")
            out(f"      quote: {ex.evidence or '(none)'}")
            out(f"      summary: {ex.summary}")
    if not count:
        out("  none")
    flags = [(l, f) for l, _, ex, _ in rows for f in ex.red_flags]
    out(f"\nRed flags raised: {len(flags)}")
    for l, f in flags:
        out(f"  {l.key:12} {f['flag']}: {f['phrase']}")

    measured = [u for _, _, _, u in rows if u]
    if measured:
        n = len(measured)
        mean = {k: sum(u[k] or 0 for u in measured) / n for k in ("input", "output", "cache_read", "cache_write")}
        per = [extract.cost_usd(u["input"] or 0, u["output"] or 0, u["cache_read"] or 0, u["cache_write"] or 0)
               for u in measured]
        out(f"\nMeasured over {n} adverts (tokens per advert, mean): input {mean['input']:.0f} uncached, "
            f"cache read {mean['cache_read']:.0f}, cache write {mean['cache_write']:.0f}, output {mean['output']:.0f}")
        out(f"Cost per advert: mean ${sum(per) / n:.5f}, max ${max(per):.5f}; total ${sum(per):.4f}")
        out(f"Projected at {DAILY_ADVERTS} new adverts a day: ${sum(per) / n * DAILY_ADVERTS:.3f} a day, "
            f"${sum(per) / n * DAILY_ADVERTS * 30:.2f} a month")
    return rows


def main(argv=None, env=None, client=None, out=print):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=os.path.join(ROOT, "state", "cache.json"))
    ap.add_argument("--sample", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--db", default=DEFAULT_DB, help="scratch SQLite file for model results")
    args = ap.parse_args(argv)
    env = os.environ if env is None else env

    listings = load_listings(args.cache)
    if not listings:
        out(f"No cached rental adverts with descriptions in {args.cache}")
        return 1
    rejected = rules_report(listings, out)
    rejected_keys = {l.key for l, _ in rejected}

    if client is None and not env.get("ANTHROPIC_API_KEY", "").strip():
        out("\nANTHROPIC_API_KEY is not set: the model half of the evaluation was NOT run. "
            "Set it and rerun this command to get the agreement table, disagreements and cost.")
        return 0
    if client is None:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            out("\nANTHROPIC_API_KEY is set but the 'anthropic' package is not installed "
                "(pip install -r requirements-extract.txt): the model half was NOT run.")
            return 2
    sample = choose_sample(listings, rejected_keys, args.sample, args.seed)
    out(f"\nSample: {len(sample)} adverts = {len(rejected_keys)} the rules reject + "
        f"{len(sample) - len(rejected_keys)} random others (seed {args.seed})")
    extractor = extract.Extractor(client=client, store=extract.Store(args.db), daily_limit=len(sample) + 10)
    model_report(sample, rejected_keys, extractor, args.db, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
