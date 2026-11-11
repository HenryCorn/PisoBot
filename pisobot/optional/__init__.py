"""The foundation every OPTIONAL feature is built on (G0, round 2). It contains no feature, only the seams.

THE RULE: nothing a feature supplies (its loader, name, repr, has_work, after_cycle, step, iterators it returns) ever executes on the
delivery thread or on the watcher thread. Feature code runs only in
  * one dedicated worker thread per feature (loads the feature, delivers it batches, asks it has_work), or
  * the supervised background child (runner.py / step.py).
The delivery and watcher threads only (a) append plain data to a bounded list, (b) hand that data to the workers' inboxes (no waiting,
no feature call), (c) read an already PUBLISHED immutable snapshot of card lines. A feature that blocks forever therefore blocks only its
own worker: it is never started a second time (single flight per feature), its inbox is bounded and aged, and what is lost is counted.

  after_ack(listing, person, reply, context)  after the acknowledgement; builds an immutable Item of plain scalars; never raises
  after_cycle(dry_run)                        after the run lock was released; moves items to the workers' inboxes; instant; never raises
  extend_card(text, listing, has_photo)       called by telegram.send_listing with the FINAL card text and the real media outcome; adds a
                                              published line only if the Telegram call plan (kind and number of calls, exactly as
                                              send_card/send_text decide it) is unchanged
  register(flag, loader, templates=())       register a worker and, optionally, declarative fetch-time templates at process startup
  publish_lines(flag, {advert key: [(priority, text)]})   publish card lines from a feature worker (validated, escaped, bounded, atomic)
  publish_templates(flag, [template])         replace a feature's templates from its worker. See "Fetch-time facts" below.

Fetch-time facts. A fact known only when the advert is fetched ("Listed 40 min ago") cannot be published per advert before its first
card: the feature never sees the advert in time, and waiting for it would delay the alert. So nothing is handed to the feature and
nothing is waited for. The hand-over at fetch is the listing's own plain scalar fields, filled in by the portal parser. A feature
registers declarative templates (field name, kind, text with one "{}", bounds, priority) as plain data at startup, before its worker
loads. The template is therefore available even for the first card. When the card is
built, extend_card reads that one scalar straight from the listing's instance dictionary (no property, no descriptor, exact builtin
types only), renders it with the pure functions below and offers the result exactly like a published line (same call-plan rule).
Lines and templates are rendered only while their feature flag is "1". No feature callback, no I/O, no lock, no wait; any problem yields
no line.

Delivery semantics of after_cycle: at least once. A batch is a tuple of Items (each has a unique `seq`); the feature returns None (all
settled), True (keep all) or the Items/seqs to keep. A kept or failed item is offered again (at most MAX_ATTEMPTS times, at most
MAX_AGE_SECONDS old); every loss is counted in `lost` by category. Features must be idempotent per `seq`. The `at` of an Item is taken at
the hook, a few milliseconds after Telegram accepted the card.

Termination: "never raises" means ordinary exceptions. KeyboardInterrupt/SystemExit are never swallowed here (the receipts are already
durable and swallowing termination would stop the service from stopping).

Importing this package does no work. With nothing enabled nothing is imported that does work, no file or table exists, no thread or
process is started, and the cycle is the release's.
"""
import dataclasses
import datetime
import html
import itertools
import os
import re
import threading
import time
from collections import deque

BUFFER_LIMIT = 500            # items waiting in the main process, and in each feature's inbox
MAX_AGE_SECONDS = 6 * 3600    # an item older than this is dropped (counted) rather than retried
MAX_ATTEMPTS = 3
STUCK_SECONDS = 30            # a worker inside one feature call for longer is reported as stuck (it is never started twice)
LINE_MAX = 160
SNAPSHOT_MAX = 2000           # adverts with published lines, per feature
PUBLISHERS_MAX = 20          # bound the total snapshot as well as each feature's contribution
CARD_OFFERS_MAX = 20
MAX_CARD_CHARS = 8192        # skip optional lines on exceptionally large release cards
FIELDS = ("key", "source", "tenure", "rent", "address", "url", "first_listed_at", "exclusive", "bedrooms")
SCALARS = (str, int, float, bool, type(None))

_registry = []                # [(flag, loader)]
_workers = {}                 # flag -> Worker
_items = []                   # after_ack items awaiting after_cycle
_lock = threading.Lock()
_worker_lock = threading.Lock()
_lines = {}                   # feature flag -> {key: ((priority, text), ...)}
_snapshot = {}                # key -> ((priority, text, flag), ...)  published atomically, never mutated
_template_sets = {}           # feature flag -> (Template, ...)
_templates = ()               # every published Template; replaced atomically, never mutated
TEMPLATES_PER_FEATURE = 5
TEMPLATES_MAX = 20
TEMPLATE_KINDS = ("age", "value")
MAX_AGE_TEMPLATE_SECONDS = 7 * 86400
CLOCK_SKEW_SECONDS = 60       # a portal clock slightly ahead of ours still reads as "under 1 min"
lost = {"overflow": 0, "evicted": 0, "expired": 0, "failed": 0, "malformed": 0, "contended": 0}   # every loss, by category
_seq = [0]


def register(flag, loader, *, templates=()):
    """Declare a worker and its optional startup templates. The loader runs only on its worker when `flag` is "1".

    Startup templates must be a plain list/tuple of plain dictionaries, not feature callbacks or iterators.
    """
    if type(flag) is not str or not 0 < len(flag) <= 80:
        lost["malformed"] += 1
        return
    _registry.append((flag, loader))
    if type(templates) not in (list, tuple):
        lost["malformed"] += 1
    elif templates:
        try:
            publish_templates(flag, templates)
        except Exception:
            lost["malformed"] += 1


def reset():
    """Forget everything and stop the workers (tests)."""
    global _snapshot, _templates
    with _lock:
        workers = list(_workers.values())
        _registry.clear(), _workers.clear(), _items.clear(), _lines.clear(), _template_sets.clear()
        _snapshot, _templates = {}, ()
        for key in lost:
            lost[key] = 0
    for worker in workers:
        worker.stop()


def enabled_any():
    """Is any registered feature switched on? Environment checks only: no import, no I/O, cannot raise."""
    return any(os.environ.get(flag) == "1" for flag, _ in _registry)


@dataclasses.dataclass(frozen=True)
class Item:
    """One accepted alert, as plain immutable data (what a feature receives; never the live listing)."""
    seq: int
    at: float
    person: str
    message_id: object
    reason: str
    state: str
    via: str
    data: tuple

    def get(self, name, default=None):
        for key, value in self.data:
            if key == name:
                return value
        return default


def message_id(reply):
    """The Telegram message id inside a send's reply, or None. Never raises."""
    try:
        value = reply["result"]["message_id"]
        return value if type(value) is int else None
    except Exception:
        return None


def _scalar(value):
    return value if type(value) in SCALARS and (type(value) is not str or len(value) <= 500) else None


# ---------------------------------------------------------------- the hook after the acknowledgement

def after_ack(listing, person, reply, context):
    """Called by `_run` once per alert, AFTER its receipt and acknowledgement. Copies a few scalars into an immutable Item and appends it
    to a bounded in-memory list: no I/O, no logging, no import, no lock, no feature code. Failures are swallowed and counted."""
    try:
        if not _registry or not enabled_any():
            return
        if len(_items) >= BUFFER_LIMIT:
            lost["overflow"] += 1
            return
        _seq[0] += 1
        ctx = context if isinstance(context, dict) else {}
        _items.append(Item(
            _seq[0], time.time(), str(person), message_id(reply), str(ctx.get("reason", "")), str(ctx.get("state", "")),
            str(ctx.get("via", "head")), tuple((name, _scalar(getattr(listing, name, None))) for name in FIELDS)))
    except Exception:
        try:
            lost["malformed"] += 1
        except Exception:
            pass


# ---------------------------------------------------------------- the workers (feature code runs here and nowhere on the pipeline)

class Worker(threading.Thread):
    """Everything about one feature: loads it, delivers batches one at a time, refreshes its has_work flag. Single flight by construction."""

    def __init__(self, flag, loader):
        super().__init__(name=f"optional-{flag}", daemon=True)
        self.flag, self.loader = flag, loader
        self.feature, self.failed, self.work = None, False, False
        self.inbox, self.lock, self.wake, self.halt = deque(), threading.Lock(), threading.Event(), threading.Event()
        self.busy_since, self.inflight = None, 0

    # -- called from the watcher thread: data only, never waits, never calls feature code
    def offer(self, items):
        if not self.lock.acquire(blocking=False):
            lost["contended"] += len(items)
            return
        try:
            if self.failed or self.halt.is_set():       # nobody will ever take them
                lost["failed"] += len(items)
                return
            for item in items:
                self.inbox.append([item, 0])
            while len(self.inbox) > BUFFER_LIMIT:
                self.inbox.popleft()
                lost["evicted"] += 1
        finally:
            self.lock.release()
        self.wake.set()

    def stop(self):
        self.halt.set()
        self.wake.set()

    def status(self):
        with self.lock:
            waiting, inflight, failed, busy = len(self.inbox), self.inflight, self.failed, self.busy_since
        return {"flag": self.flag, "loaded": self.feature is not None, "failed": failed, "waiting": waiting,
                "inflight": inflight,                   # taken from the inbox, the feature has not settled them yet
                "stuck": busy is not None and time.monotonic() - busy > STUCK_SECONDS}

    # -- the worker thread
    def run(self):
        try:
            try:
                self.feature = self.loader()
                if self.feature is None:
                    raise ValueError("no feature")
            except Exception:
                return
            self._refresh()
            while not self.halt.is_set():
                self.wake.wait(5.0)
                self.wake.clear()
                if self.halt.is_set():
                    break
                batch = self._take()
                if batch:
                    self._deliver(batch)
                self._refresh()
        finally:
            with self.lock:
                self.failed = True
                lost["failed"] += len(self.inbox) + self.inflight
                self.inbox.clear()
                self.inflight = 0

    def _refresh(self):
        with self.lock:
            self.busy_since = time.monotonic()
        try:
            self.work = bool(self.feature.has_work()) if hasattr(self.feature, "step") else False
        except Exception:
            self.work = False
        finally:
            with self.lock:
                self.busy_since = None

    def _take(self):
        now = time.time()
        with self.lock:
            taken, self.inbox = list(self.inbox), deque()
            self.inflight = len(taken)                  # same step as leaving the inbox: never in neither
        fresh = []
        for entry in taken:
            if now - entry[0].at > MAX_AGE_SECONDS:
                lost["expired"] += 1
            else:
                fresh.append(entry)
        self.inflight = len(fresh)
        return fresh

    def _deliver(self, entries):
        items = tuple(e[0] for e in entries)
        with self.lock:
            self.inflight = len(items)
            self.busy_since = time.monotonic()
        try:
            result = self.feature.after_cycle(items)
            ok = True
        except Exception:
            result, ok = None, False
        finally:
            with self.lock:
                self.busy_since = None
        try:
            if ok and result is True:
                keep = {e[0].seq for e in entries}
            elif ok and result is not None:
                keep = {getattr(x, "seq", x) for x in result}
            else:
                keep = None
        except Exception:
            keep, ok = None, False
        retry = []
        for entry in entries:
            wanted = (keep is not None and entry[0].seq in keep) or (keep is None and not ok)
            if not wanted:
                continue
            entry[1] += 1
            if entry[1] >= MAX_ATTEMPTS:
                lost["failed"] += 1
            else:
                retry.append(entry)
        with self.lock:                                  # one step: an item is always counted in the inbox or in flight
            self.inbox.extendleft(reversed(retry))
            while len(self.inbox) > BUFFER_LIMIT:
                self.inbox.pop()
                lost["evicted"] += 1
            self.inflight = 0


def _ensure_workers():
    """Create and start one worker per enabled registration (no feature code runs on the calling thread). Idempotent."""
    for flag, loader in list(_registry):
        if os.environ.get(flag) == "1" and flag not in _workers:
            if not _worker_lock.acquire(blocking=False):
                continue
            try:
                if flag in _workers:
                    continue
                worker = Worker(flag, loader)
                try:
                    worker.start()
                except Exception:
                    worker.failed = True
                _workers[flag] = worker
            finally:
                _worker_lock.release()


def start():
    """Start the workers at process start (serve). Never raises; with nothing enabled it starts nothing."""
    try:
        if _registry and enabled_any():
            _ensure_workers()
    except Exception:
        pass


def workers():
    return list(_workers.values())


def health():
    """What /status may show: loss counts and per-feature state. Reads only."""
    try:
        from . import store
        return {"lost": dict(lost), "workers": [w.status() for w in workers()], "storage": store.last_error[0]}
    except Exception:
        return {"lost": {}, "workers": []}


# ---------------------------------------------------------------- the hook after the cycle

def after_cycle(dry_run=False):
    """Called after the run lock was released. Moves the collected items to each enabled feature's inbox and wakes its worker: instant,
    no feature call, no waiting. Nothing in a dry run. Never raises."""
    try:
        fresh = list(_items)
        _items.clear()
        if dry_run or not _registry or not enabled_any():
            return 0
        _ensure_workers()
        for flag in dict.fromkeys(flag for flag, _ in _registry):
            if os.environ.get(flag) == "1":
                worker = _workers.get(flag)
                if worker is None:
                    lost["failed"] += len(fresh)
                    continue
                worker.offer(fresh)
        return len(fresh)
    except Exception:
        return 0


# ---------------------------------------------------------------- card lines (published snapshot; no feature code on the delivery thread)

def publish_lines(flag, mapping):
    """Publish {advert key: [(priority, text), ...]} from a feature worker, replacing that flag's snapshot atomically."""
    global _snapshot
    if type(flag) is not str or not 0 < len(flag) <= 80:
        lost["malformed"] += 1
        return 0
    clean = {}
    for key, offers in itertools.islice(mapping.items(), SNAPSHOT_MAX):     # never consumes more than the cap, whatever is supplied
        if type(key) is not str or not 0 < len(key) <= 200:
            continue
        good = []
        for offer in itertools.islice(offers, 5):
            try:
                priority, text = offer
                if type(priority) is int and -1_000_000 <= priority <= 1_000_000 and type(text) is str and len(text) <= 4 * LINE_MAX:
                    text = re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()
                    if text and len(text) <= LINE_MAX:
                        good.append((priority, html.escape(text, quote=False)))
            except Exception:
                continue
        if good:
            clean[key] = tuple(sorted(good, key=lambda o: -o[0]))
    with _lock:
        if flag not in _lines and len(_lines) >= PUBLISHERS_MAX:
            lost["malformed"] += 1
            return 0
        _lines[flag] = clean
        merged = {}
        for owner, per_feature in _lines.items():
            for key, offers in per_feature.items():
                merged[key] = merged.get(key, ()) + tuple((priority, text, owner) for priority, text in offers)
        _snapshot = {k: tuple(sorted(v, key=lambda o: -o[0])) for k, v in merged.items()}
    return len(clean)

# ---------------------------------------------------------------- fetch-time facts: declared by a feature, rendered here at send

@dataclasses.dataclass(frozen=True)
class Template:
    """A declared card line about one scalar field of the listing. Plain data; `text` is already HTML-escaped and has exactly one "{}"."""
    priority: int
    field: str
    kind: str            # "age": the field is a timestamp, "{}" becomes "40 min" / "2 h 5 min"; "value": "{}" becomes the scalar itself
    text: str
    max_seconds: int     # "age" only: no line once the fact is older than this
    flag: str            # the feature must still be enabled when the card is sent


def publish_templates(flag, templates):
    """Replace a flag's templates with validated plain data. For a first alert, pass these to register() at startup instead."""
    global _templates
    if type(flag) is not str or not 0 < len(flag) <= 80:
        lost["malformed"] += 1
        return 0
    good = []
    for raw in itertools.islice(templates, TEMPLATES_PER_FEATURE):
        try:
            if type(raw) is not dict:
                continue
            priority, field, kind, text = raw["priority"], raw["field"], raw["kind"], raw["text"]
            limit = raw.get("max_seconds", 0)
            if (type(priority) is not int or not -1_000_000 <= priority <= 1_000_000 or type(field) is not str or type(kind) is not str
                    or type(text) is not str or kind not in TEMPLATE_KINDS):
                continue
            if len(text) > 4 * LINE_MAX or len(field) > 40:
                continue
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", field):
                continue
            text = re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()
            if text.count("{}") != 1 or "{" in text.replace("{}", "") or "}" in text.replace("{}", "") or len(text) > LINE_MAX:
                continue
            if kind == "age" and (type(limit) is not int or not 0 < limit <= MAX_AGE_TEMPLATE_SECONDS):
                continue
            good.append(Template(priority, field, kind, html.escape(text, quote=False), limit if kind == "age" else 0, flag))
        except Exception:
            continue
    with _lock:
        if flag not in _template_sets and len(_template_sets) >= PUBLISHERS_MAX:
            lost["malformed"] += 1
            return 0
        _template_sets[flag] = tuple(good)
        _templates = tuple(t for group in _template_sets.values() for t in group)
    return len(good)


def epoch(value):
    """Seconds since 1970 for an ISO timestamp WITH an offset, or None. Pure, bounded, never raises."""
    try:
        if type(value) is not str or not 10 < len(value) <= 40:
            return None
        when = datetime.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        return when.timestamp() if when.tzinfo is not None else None
    except Exception:
        return None


def age_words(seconds):
    """'under 1 min', '40 min', '2 h', '2 h 5 min', '3 days'. Pure."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "under 1 min"
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        hours, rest = divmod(minutes, 60)
        return f"{hours} h" + (f" {rest} min" if rest else "")
    return f"{minutes // 1440} days"


def render(template, listing, now):
    """The escaped line for this listing, or None. Reads ONE scalar from the listing's instance dictionary (never a property, never a
    feature object), accepts exact builtin types only, and computes with pure code. Never raises."""
    try:
        value = vars(listing).get(template.field)
        if template.kind == "age":
            then = epoch(value)
            if then is None:
                return None
            age = now - then
            if not -CLOCK_SKEW_SECONDS <= age < template.max_seconds:
                return None
            shown = age_words(max(age, 0))
        else:
            if type(value) is str:
                if len(value) > 4 * LINE_MAX:
                    return None
                shown = re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()
            elif type(value) is int and value.bit_length() <= 200:
                shown = str(value)
            elif type(value) is float and value == value and abs(value) < 1e15:
                shown = f"{value:g}"
            else:
                return None
            if not shown or len(shown) > 60:
                return None
            shown = html.escape(shown, quote=False)
        line = template.text.replace("{}", shown)
        return line if len(line) <= LINE_MAX else None
    except Exception:
        return None


def plan(has_photo, text):
    """The Telegram call plan of a card, decided exactly as telegram.send_card / send_text decide it: (kind, number of calls)."""
    from .. import tgtext
    if has_photo and len(text.encode("utf-16-le")) // 2 <= tgtext.CAPTION:
        return ("photo-caption", 1)
    pieces = len(tgtext.split_html(text))
    return ("photo-then-text", 1 + pieces) if has_photo else ("text", pieces)


def extend_card(text, listing, has_photo):
    """Add published lines, and lines rendered from published templates, to the FINAL card text (media outcome known), highest priority
    first, each only while the call plan stays exactly what it was without it. Reads published immutable data and one scalar of the
    listing; calls no feature code; returns `text` unchanged on any problem."""
    original = text
    try:
        if len(text) > MAX_CARD_CHARS:
            return text
        templates = sorted((t for t in _templates if os.environ.get(t.flag) == "1"), key=lambda t: -t.priority)[:TEMPLATES_MAX]
        snapshot = _snapshot
        if not snapshot and not templates:
            return text
        offers = ([(priority, line) for priority, line, flag in snapshot.get(getattr(listing, "key", None), ())
                   if os.environ.get(flag) == "1"] if snapshot else [])
        if templates:
            now = time.time()
            rendered = [(t.priority, line) for t in templates for line in (render(t, listing, now),) if line]
            offers.extend(rendered)
        if not offers:
            return text
        offers = sorted(offers, key=lambda o: -o[0])[:CARD_OFFERS_MAX]
        base, used = plan(has_photo, text), set()
        for _, line in offers:
            if line in used:
                continue
            candidate = text + "\n" + line
            if plan(has_photo, candidate) == base:
                text = candidate
                used.add(line)
        return text if plan(has_photo, text) == base else original
    except Exception:
        return original


# ---------------------------------------------------------------- supervisor entry points used by the watcher

def cycle_start():
    try:
        RUNNER.reap()
    except Exception:
        pass


def cycle_end(dry_run=False):
    try:
        after_cycle(dry_run)
        RUNNER.reap()
    except Exception:
        pass


def maybe_start_step():
    try:
        RUNNER.maybe_start()
    except Exception:
        pass


def shutdown():
    """Bounded and non-raising: stop the workers' loops and terminate and reap the child and its process group."""
    try:
        for worker in workers():
            worker.stop()
        RUNNER.stop()
    except Exception:
        pass


from .runner import RUNNER  # noqa: E402,F401  (the class is defined; nothing runs until a feature has queued work)
from .. import registry_loader  # noqa: E402,F401  (records which features exist; imports none of them)
