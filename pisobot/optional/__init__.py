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
  publish_lines(feature, {advert key: [(priority, text)]})   how a feature publishes card lines (validated, escaped, bounded, atomic)

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
import html
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
FIELDS = ("key", "source", "tenure", "rent", "address", "url", "first_listed_at", "exclusive", "bedrooms",
          "lat", "lng", "crime_score")
SCALARS = (str, int, float, bool, type(None))

_registry = []                # [(flag, loader)]
_workers = {}                 # flag -> Worker
_items = []                   # after_ack items awaiting after_cycle
_lock = threading.Lock()
_lines = {}                   # feature name -> {key: ((priority, text), ...)}
_snapshot = {}                # key -> ((priority, text), ...)  published, replaced atomically, never mutated
lost = {"overflow": 0, "evicted": 0, "expired": 0, "failed": 0, "malformed": 0}   # every loss, by category
_seq = [0]


def register(flag, loader):
    """Declare a feature: the loader is called (on the feature's own worker thread) only when the environment variable `flag` is "1"."""
    if not any(existing == flag for existing, _ in _registry):
        _registry.append((flag, loader))


def reset():
    """Forget everything and stop the workers (tests)."""
    global _snapshot
    with _lock:
        workers = list(_workers.values())
        _registry.clear(), _workers.clear(), _items.clear(), _lines.clear()
        _snapshot = {}
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
    return value if isinstance(value, SCALARS) and (not isinstance(value, str) or len(value) <= 500) else None


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
        self.busy_since = None

    # -- called from the watcher thread: data only, never waits, never calls feature code
    def offer(self, items):
        with self.lock:
            for item in items:
                self.inbox.append([item, 0])
            while len(self.inbox) > BUFFER_LIMIT:
                self.inbox.popleft()
                lost["evicted"] += 1
        self.wake.set()

    def stop(self):
        self.halt.set()
        self.wake.set()

    def status(self):
        busy = self.busy_since
        return {"flag": self.flag, "loaded": self.feature is not None, "failed": self.failed, "waiting": len(self.inbox),
                "stuck": busy is not None and time.monotonic() - busy > STUCK_SECONDS}

    # -- the worker thread
    def run(self):
        try:
            self.feature = self.loader()
            if self.feature is None:
                raise ValueError("no feature")
        except Exception:
            self.failed = True
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

    def _refresh(self):
        try:
            self.work = bool(self.feature.has_work()) if hasattr(self.feature, "step") else False
        except Exception:
            self.work = False

    def _take(self):
        now = time.time()
        with self.lock:
            taken, self.inbox = list(self.inbox), deque()
        fresh = []
        for entry in taken:
            if now - entry[0].at > MAX_AGE_SECONDS:
                lost["expired"] += 1
            else:
                fresh.append(entry)
        return fresh

    def _deliver(self, entries):
        items = tuple(e[0] for e in entries)
        self.busy_since = time.monotonic()
        try:
            result = self.feature.after_cycle(items)
            ok = True
        except Exception:
            result, ok = None, False
        finally:
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
        if retry:
            with self.lock:
                self.inbox.extendleft(reversed(retry))
                while len(self.inbox) > BUFFER_LIMIT:
                    self.inbox.pop()
                    lost["evicted"] += 1


def _ensure_workers():
    """Create and start one worker per enabled registration (no feature code runs on the calling thread). Idempotent."""
    for flag, loader in list(_registry):
        if os.environ.get(flag) == "1" and flag not in _workers:
            with _lock:
                if flag in _workers:
                    continue
                worker = Worker(flag, loader)
                _workers[flag] = worker
            worker.start()


def start():
    """Start the workers at process start (serve). Never raises; with nothing enabled it starts nothing."""
    try:
        if os.environ.get("PLACE_FACTS") == "1":
            from .. import registry_loader
            registry_loader.register()
        if _registry and enabled_any():
            _ensure_workers()
    except Exception:
        pass


def workers():
    return list(_workers.values())


def health():
    """What /status may show: loss counts and per-feature state. Reads only."""
    try:
        return {"lost": dict(lost), "workers": [w.status() for w in workers()]}
    except Exception:
        return {"lost": {}, "workers": []}


# ---------------------------------------------------------------- the hook after the cycle

def after_cycle(dry_run=False):
    """Called after the run lock was released. Moves the collected items to each enabled feature's inbox and wakes its worker: instant,
    no feature call, no waiting. Nothing in a dry run. Never raises."""
    try:
        with _lock:
            fresh = list(_items)
            _items.clear()
        if dry_run or not _registry or not enabled_any():
            return 0
        _ensure_workers()
        for worker in workers():
            if os.environ.get(worker.flag) == "1":
                worker.offer(fresh)
        return len(fresh)
    except Exception:
        return 0


# ---------------------------------------------------------------- card lines (published snapshot; no feature code on the delivery thread)

def publish_lines(feature, mapping):
    """Called by a feature (from its own thread) to publish its card lines: {advert key: [(priority, text), ...]}. Validated and escaped
    here; the whole mapping replaces that feature's previous one, atomically. Returns the number of adverts published."""
    global _snapshot
    clean = {}
    for key, offers in list(mapping.items())[:SNAPSHOT_MAX]:
        if not isinstance(key, str) or not 0 < len(key) <= 200:
            continue
        good = []
        for offer in list(offers)[:5]:
            try:
                priority, text = offer
                if type(priority) is int and isinstance(text, str):
                    text = re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()
                    if text and len(text) <= LINE_MAX:
                        good.append((priority, html.escape(text, quote=False)))
            except Exception:
                continue
        if good:
            clean[key] = tuple(sorted(good, key=lambda o: -o[0]))
    with _lock:
        _lines[str(feature)[:40]] = clean
        merged = {}
        for per_feature in _lines.values():
            for key, offers in per_feature.items():
                merged[key] = merged.get(key, ()) + offers
        _snapshot = {k: tuple(sorted(v, key=lambda o: -o[0])) for k, v in merged.items()}
    return len(clean)


def plan(has_photo, text):
    """The Telegram call plan of a card, decided exactly as telegram.send_card / send_text decide it: (kind, number of calls)."""
    from .. import tgtext
    if has_photo and len(text.encode("utf-16-le")) // 2 <= tgtext.CAPTION:
        return ("photo-caption", 1)
    pieces = len(tgtext.split_html(text))
    return ("photo-then-text", 1 + pieces) if has_photo else ("text", pieces)


def extend_card(text, listing, has_photo):
    """Add published lines to the FINAL card text (media outcome known), highest priority first, each only while the call plan stays
    exactly what it was without it. Reads the published snapshot only; calls no feature code; returns `text` unchanged on any problem."""
    try:
        offers = _snapshot.get(getattr(listing, "key", None))
        if not offers:
            return text
        base = plan(has_photo, text)
        for _, line in offers:
            candidate = text + "\n" + line
            if plan(has_photo, candidate) == base:
                text = candidate
        return text
    except Exception:
        return text


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
