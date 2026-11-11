#!/usr/bin/env python3
"""Measure how well the floorplan reader (pisobot/floorplan.py) recovers the total area, against adverts where
the portal ALSO states a floor area.

  # 1. gather a sample (live: at most --max-requests paced requests to rightmove.co.uk; FETCH_CHAIN=plain)
  FETCH_CHAIN=plain python scripts/eval_floorplan.py gather --out sample.json [--max-requests 14] [--pages 0,24]

  # 2. read the plans with the model (live: at most --limit images, spend checked against the ledger)
  python scripts/eval_floorplan.py run --sample sample.json --env-file ~/.config/pisobot/anthropic.env \
         --ledger path/to/ai-test-budget.json [--limit 40] [--images-dir DIR] [--fixtures OUT.json]

gather: search pages give numberOfFloorplans and displaySize (the portal's stated size); a details page per
chosen advert gives the floorplan URLs and sizings. Adverts with both a floorplan and a stated size are chosen
first (they are the ground truth), then other adverts with a floorplan.

run: downloads each plan once, asks the model, prints how many plans printed a total, how many were read, the
error against the portal's area (every miss with the printed text), what was refused and why, and the measured
tokens and cost per image. The API key is read from --env-file inside this process only (never printed, logged
or passed as an argument). Model answers are cached in a scratch SQLite file so a rerun does not pay twice.
The ledger (JSON) is read before the run: the run stops before the remaining allowance would be exceeded, and
the measured spend is appended afterwards.
"""
import argparse
import contextlib
import datetime as dt
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pisobot import extract, floorplan  # noqa: E402

SQFT = floorplan.SQFT_TO_SQM
DEFAULT_DB = os.path.join(tempfile.gettempdir(), "pisobot-eval-floorplan.db")
WORST_INPUT_TOKENS = 8000              # reserved per request before it is sent: an image of at most 1600 px (about 3,400 tokens, 4,400 with
                                       # the newer tokenizer) plus the instructions and schema; the output reserve is the real request maximum
MISS_ABS, MISS_REL = 2.0, 0.05         # an error beyond max(2 m2, 5%) is listed as a miss


# ---------------------------------------------------------------- gather

def parse_display_size(text):
    """'277 sq. ft.' / '52 sq. m.' -> square metres, else None."""
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*sq\.?\s*(ft|m)", str(text or ""), re.I)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    return round(value * SQFT, 1) if m.group(2).lower() == "ft" else round(value, 1)


def search_items(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("no __NEXT_DATA__")
    return json.loads(m.group(1))["props"]["pageProps"]["searchResults"].get("properties") or []


def choose(items, budget, prefer_no_size=False, exclude=()):
    """Adverts with a floorplan, those with a displayed size first (or, for a blind sample, without one first)."""
    with_plan = [x for x in items if (x.get("numberOfFloorplans") or 0) > 0 and x.get("id") and str(x["id"]) not in exclude]
    with_plan.sort(key=lambda x: (1 if parse_display_size(x.get("displaySize")) else 0) if prefer_no_size
                   else (0 if parse_display_size(x.get("displaySize")) else 1))
    return with_plan[:budget]


def gather(out, max_requests=14, pages=(0, 24), page_fn=None, details_fn=None, out_fn=print, location=None,
           radius=5.0, prefer_no_size=False, exclude=()):
    from pisobot.sources import rightmove
    from pisobot import net
    page_fn = page_fn or (lambda index: net.page(f"{rightmove.BASE}/property-to-rent/find.html", params={
        "locationIdentifier": location or rightmove.LOCATION, "radius": radius, "minBedrooms": 1, "maxPrice": 1600,
        "dontShow": "houseShare,student,retirement", "sortType": 6, "index": index}))
    details_fn = details_fn or (lambda item: rightmove._property_data(net.page(f"{rightmove.BASE}/properties/{item['id']}")))
    used, items, seen = 0, [], set()
    for index in pages:
        if used >= max_requests:
            break
        used += 1
        for x in search_items(page_fn(index)):
            if x.get("id") not in seen:
                seen.add(x.get("id"))
                items.append(x)
    candidates = choose(items, max_requests - used, prefer_no_size, set(map(str, exclude)))
    out_fn(f"search pages: {used}; adverts seen {len(items)}; with a floorplan "
           f"{sum((x.get('numberOfFloorplans') or 0) > 0 for x in items)}; "
           f"with floorplan and a displayed size {sum(bool(parse_display_size(x.get('displaySize'))) for x in items if (x.get('numberOfFloorplans') or 0) > 0)}")
    sample = []
    for x in candidates:
        used += 1
        try:
            p = details_fn(x)
        except Exception as ex:
            out_fn(f"  details failed for {x.get('id')}: {type(ex).__name__}")
            continue
        sqm = next((s.get("minimumSize") for s in p.get("sizings") or [] if s.get("unit") == "sqm"), None)
        urls = [f["url"] for f in p.get("floorplans") or [] if isinstance(f, dict) and f.get("url")]
        sample.append({"id": str(x["id"]), "beds": x.get("bedrooms"), "stated_sqm": sqm,
                       "display_size": x.get("displaySize") or "", "floorplans": urls})
    out_fn(f"requests used: {used}; sample {len(sample)} adverts, {sum(len(s['floorplans']) for s in sample)} plans, "
           f"{sum(1 for s in sample if s['stated_sqm'])} with a stated area")
    with open(out, "w") as f:
        json.dump({"gathered": dt.date.today().isoformat(), "requests": used, "adverts": sample}, f, indent=1)
    return sample


# ---------------------------------------------------------------- synthetic refusal cases

SYNTHETIC = {
    "room_dimensions_only": ["BEDROOM 3.2m x 4.1m", "LIVING ROOM 5.0m x 3.6m", "KITCHEN 2.4m x 3.0m", "BATHROOM 2.0m x 1.8m"],
    "includes_garage": ["BEDROOM 3.2m x 4.1m", "LIVING ROOM 5.0m x 3.6m", "Total area 62 sq m (667 sq ft)",
                        "including garage and store"],
    "per_floor_no_total": ["GROUND FLOOR", "approx. 30 sq m (323 sq ft)", "FIRST FLOOR", "approx. 28 sq m (301 sq ft)"],
    "units_disagree": ["BEDROOM 3.2m x 4.1m", "LIVING ROOM 5.0m x 3.6m", "Total area 52 sq m (700 sq ft)"],
}


def synthetic_image(lines):
    """A plain drawn plan: a box with the given printed lines. Not a real advert."""
    import io
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (900, 700), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((60, 60, 840, 520), outline="black", width=8)
    d.line((460, 60, 460, 520), fill="black", width=6)
    try:
        font = ImageFont.load_default(size=28)
    except TypeError:
        font = ImageFont.load_default()
    for i, line in enumerate(lines):
        d.text((90, 90 + i * 70), line, fill="black", font=font)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def add_synthetic(sample):
    sample = dict(sample)
    sample["adverts"] = list(sample["adverts"]) + [
        {"id": f"synthetic:{name}", "beds": 1, "stated_sqm": None, "display_size": "", "floorplans": [f"synthetic:{name}"],
         "expect": "no_read"} for name in SYNTHETIC]
    return sample


def synthetic_fetcher(real):
    return lambda url: synthetic_image(SYNTHETIC[url.split(":", 1)[1]]) if url.startswith("synthetic:") else real(url)


# ---------------------------------------------------------------- ledger

def load_ledger(path):
    with open(path) as f:
        return json.load(f)


def remaining_allowance(ledger):
    """Conservative: ceiling minus everything recorded as spent minus the outstanding reservation."""
    return (ledger["user_authorized_ceiling_usd"] - ledger.get("total_all_evaluations_estimated_usd", 0)
            - ledger.get("new_tests_reserved_usd", 0))


def _write(path, ledger):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2)
    os.replace(tmp, path)


@contextlib.contextmanager
def _locked(path):
    """One writer at a time across processes: every read-modify-write of the ledger holds this lock."""
    import fcntl
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def reserve_spend(path, amount):
    """Atomically reserve `amount` dollars against the ledger's ceiling (the total already includes earlier reservations).
    False, and nothing written, when it would exceed the ceiling."""
    with _locked(path):
        ledger = load_ledger(path)
        total = ledger.get("total_all_evaluations_estimated_usd", 0)
        if total + ledger.get("new_tests_reserved_usd", 0) + amount > ledger["user_authorized_ceiling_usd"] + 1e-12:
            return False
        ledger["total_all_evaluations_estimated_usd"] = round(total + amount, 8)
        _write(path, ledger)
        return True


def adjust_spend(path, delta, detail=None):
    """Add `delta` dollars to the ledger total, atomically under the lock, re-reading it first. Settles a reservation to the
    measured cost afterwards (a crash in between leaves the reservation in place)."""
    with _locked(path):
        ledger = load_ledger(path)
        ledger["total_all_evaluations_estimated_usd"] = round(ledger.get("total_all_evaluations_estimated_usd", 0) + delta, 8)
        if detail is not None:
            ledger["g6_floorplan_evaluation"] = {**ledger.get("g6_floorplan_evaluation", {}), **detail,
                                                 "note": "measured token usage x official claude-haiku-5-5 prices; not an invoice"}
        _write(path, ledger)


def append_spend(path, spend, detail):
    adjust_spend(path, spend, {**detail, "estimated_usd": round(spend, 8)})


def worst_case_cost(model):
    """What one request can cost at most: the input reserve plus the output limit the request actually carries."""
    from pisobot import config
    return extract.cost_usd(WORST_INPUT_TOKENS, config.EXTRACT_MAX_TOKENS, model=model)


def load_key(env_file):
    """Reads ANTHROPIC_API_KEY (or ANTHROPIC_KEY) from a KEY=value file into this process's environment only."""
    with open(os.path.expanduser(env_file)) as f:
        for line in f:
            name, _, value = line.strip().partition("=")
            if name.replace("export ", "") in ("ANTHROPIC_API_KEY", "ANTHROPIC_KEY") and value.strip().strip("'\""):
                os.environ["ANTHROPIC_API_KEY"] = value.strip().strip("'\"")
                return True
    return False


# ---------------------------------------------------------------- run

def run(sample, reader, ledger=None, limit=40, out=print, ledger_path=None):
    """Read each plan; `ledger` (a dict) bounds the spend, `ledger_path` (optional) makes the ledger persistent: the worst
    case is reserved in the file before every request and settled to the measured tokens afterwards. Every response the
    API returned is charged (accepted or not), and a request that returned nothing (a timeout) is charged at the worst case."""
    plans = [(a, url) for a in sample["adverts"] for url in a["floorplans"][:1]]
    plans.sort(key=lambda p: 0 if p[0].get("stated_sqm") else 1 if not p[0].get("expect") else 2)   # ground truth first
    plans = plans[:limit]
    allowance = remaining_allowance(ledger) if ledger else None
    guard = worst_case_cost(reader.model)
    spent, rows, stopped = 0.0, [], None
    for advert, url in plans:
        if allowance is not None and spent + guard > allowance:
            stopped = f"stopped before the ledger allowance (${allowance:.4f}) could be exceeded"
            break
        if ledger_path and not reserve_spend(ledger_path, guard):       # one atomic reservation against the shared ceiling
            stopped = "stopped: the ledger ceiling would be exceeded"
            break
        reader.last_usage, unknown_before = None, reader.unknown_billed
        record = reader.read(url, str(advert["id"]))
        got = reader.last_usage                                          # THIS request's own usage, whatever day it was billed on
        din = (got["input"] + got["cache_read"] + got["cache_write"]) if got else 0     # all input at the full price: conservative
        dout = got["output"] if got else 0
        unknown = reader.unknown_billed - unknown_before
        usage = {"input": din, "output": dout, "cache_read": 0, "cache_write": 0} if got else {}
        cost = extract.cost_usd(din, dout, model=reader.model) + unknown * guard
        if ledger_path:
            adjust_spend(ledger_path, cost - guard)                      # settle the reservation to what was measured
        fresh = bool(din or dout or unknown)
        spent += cost
        rows.append((advert, url, record, usage, cost, fresh))
    out(f"\nPlans sent to the model: {len(rows)} of {len(plans)} chosen" + (f"  [{stopped}]" if stopped else ""))
    failed = [a["id"] for a, _, r, _, _, _ in rows if r is None]
    if failed:
        out(f"No answer (image or API failure): {', '.join(failed)}")
    answered = [r for r in rows if r[2] is not None]
    status = {}
    for _, _, rec, _, _, _ in answered:
        status[rec["status"]] = status.get(rec["status"], 0) + 1
    printed = sum(1 for r in answered if r[2]["status"] == "read" or r[2]["status"] == "discarded" and r[2]["reason"]
                  and not r[2]["reason"].startswith("not a floorplan"))
    out(f"Statuses: {status}")
    out(f"Plans on which the model found any printed total figure (read + discarded): {printed} of {len(answered)}")
    out(f"Plans read (total accepted): {status.get('read', 0)} of {len(answered)}")

    truth = [r for r in answered if r[0].get("stated_sqm")]
    out(f"\nGround truth: {len(truth)} plans whose advert states an area on the portal")
    errors, misses = [], []
    for advert, url, rec, usage, cost, fresh in truth:
        stated = advert["stated_sqm"]
        if rec["status"] == "read":
            err = rec["area_sqm"] - stated
            errors.append(err)
            ok = abs(err) <= max(MISS_ABS, MISS_REL * stated)
            out(f"  {advert['id']:>10}  portal {stated:>6.1f}  plan {rec['area_sqm']:>6.1f}  error {err:+6.1f}  "
                f"{'ok' if ok else 'MISS'}   \"{rec['quote'][:80]}\"")
            if not ok:
                misses.append((advert, rec))
        else:
            out(f"  {advert['id']:>10}  portal {stated:>6.1f}  plan not read: {rec['status']} - {rec['reason']}")
    if errors:
        import statistics
        out(f"Read with a stated area: {len(errors)}; error (plan minus portal) median {statistics.median(errors):+.2f} m², "
            f"mean absolute {statistics.mean(abs(e) for e in errors):.2f} m², worst {max(errors, key=abs):+.2f} m²; "
            f"within max(2 m², 5%): {len(errors) - len(misses)} of {len(errors)}")
    out(f"Misses (read but wrong): {len(misses)}")
    for advert, rec in misses:
        out(f"  MISS {advert['id']}: portal {advert['stated_sqm']:g} m², plan read {rec['area_sqm']:g} m² from \"{rec['quote']}\"")

    synth = [r for r in answered if r[0].get("expect") == "no_read"]
    if synth:
        refused = [r for r in synth if r[2]["status"] != "read"]
        out(f"\nSynthetic cases that must NOT be read: refused {len(refused)} of {len(synth)}")
        for advert, _, rec, *_ in synth:
            out(f"  {advert['id']:34} {rec['status']:9} {rec['reason'] or ('READ ' + str(rec['area_sqm']))}")
    out("\nNot read (refusals and discards), with the model's reason; check each against the image:")
    for advert, url, rec, usage, cost, fresh in answered:
        if rec["status"] != "read":
            out(f"  {advert['id']:>10}  {rec['status']:9} {rec['reason']}")
    out("\nBedrooms the plan shows vs the advert:")
    for advert, url, rec, usage, cost, fresh in answered:
        if rec.get("bedrooms") is not None:
            out(f"  {advert['id']:>10}  plan {rec['bedrooms']}  advert {advert.get('beds')}  \"{rec['bedrooms_quote'][:50]}\""
                + ("   <- differs" if rec["bedrooms"] != advert.get("beds") else ""))
    measured = [(u, c) for _, _, r, u, c, f in rows if u and r is not None]
    if measured:
        n = len(measured)
        mean = {k: sum(u.get(k, 0) or 0 for u, _ in measured) / n for k in ("input", "output", "cache_read", "cache_write")}
        out(f"\nMeasured over {n} images (tokens each, mean): input {mean['input']:.0f} uncached, cache read "
            f"{mean['cache_read']:.0f}, cache write {mean['cache_write']:.0f}, output {mean['output']:.0f}")
        costs = [c for _, c in measured]
        out(f"Cost per image: mean ${sum(costs) / n:.5f}, max ${max(costs):.5f}; this run's new spend ${spent:.4f}")
        out(f"Projected at 60 images/day: ${sum(costs) / n * 60:.3f}/day")
    return {"rows": rows, "spent": spent, "n": len(rows), "stopped": stopped}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gather")
    g.add_argument("--out", required=True)
    g.add_argument("--max-requests", type=int, default=14)
    g.add_argument("--pages", default="0,24")
    g.add_argument("--location", help="Rightmove locationIdentifier, e.g. REGION^85428 (default: Oxford Circus station)")
    g.add_argument("--radius", type=float, default=5.0)
    g.add_argument("--prefer-no-size", action="store_true", help="prefer adverts whose card shows no size (blind sample)")
    g.add_argument("--exclude", help="a sample JSON whose advert ids are left out")
    r = sub.add_parser("run")
    r.add_argument("--sample", required=True)
    r.add_argument("--env-file", required=True)
    r.add_argument("--ledger", required=True)
    r.add_argument("--limit", type=int, default=40)
    r.add_argument("--db", default=DEFAULT_DB)
    r.add_argument("--synthetic", action="store_true", help="also send 4 drawn plans that must be refused")
    r.add_argument("--dump", help="write the interpreted records (no images, no addresses) to this JSON file")
    args = ap.parse_args(argv)
    if args.cmd == "gather":
        exclude = [a["id"] for a in json.load(open(args.exclude))["adverts"]] if args.exclude else []
        gather(args.out, args.max_requests, tuple(int(p) for p in args.pages.split(",")), location=args.location,
               radius=args.radius, prefer_no_size=args.prefer_no_size, exclude=exclude)
        return 0
    with open(args.sample) as f:
        sample = json.load(f)
    if not load_key(args.env_file):
        print("no API key found in the env file; nothing was run")
        return 2
    ledger = load_ledger(args.ledger)
    print(f"ledger allowance remaining (conservative): ${remaining_allowance(ledger):.4f}")
    import anthropic
    client = anthropic.Anthropic(timeout=40, max_retries=0)
    if args.synthetic:
        sample = add_synthetic(sample)
    reader = floorplan.Reader(client=client, store=floorplan.Store(args.db), daily_limit=args.limit + 5,
                              fetcher=synthetic_fetcher(floorplan.fetch))
    result = run(sample, reader, ledger, args.limit, ledger_path=args.ledger)
    if args.dump:
        with open(args.dump, "w") as f:
            json.dump([{"id": a["id"], "stated_sqm": a.get("stated_sqm"), "record": r, "usage": u}
                       for a, _, r, u, _, _ in result["rows"]], f, indent=1)
    adjust_spend(args.ledger, 0, {"images": result["n"], "model": reader.model, "date": dt.date.today().isoformat(),
                                  "estimated_usd_this_run": round(result["spent"], 8)})
    print(f"ledger updated per request; this run's measured spend ${result['spent']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
