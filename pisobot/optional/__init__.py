"""The foundation every OPTIONAL feature is built on (G0). It contains no feature, only the seams.

Why it exists: three independent reviews of three different optional features found the same five classes of defect, each
re-invented per feature: (a) optional code between Telegram accepting a card and the seen receipt, so an exception there lost the
receipt and repeated the card; (b) optional writes to the primary database whose transaction delayed the main process's
acknowledgements (a short busy timeout bounds waiting, not holding); (c) optional work that claimed to run after the cycle but ran
under the run lock or before another person's alert; (d) an extra card line pushing a caption past Telegram's limit so one card became
two calls; (e) "off" not being identical to the release. This package removes those classes once:

  HOOKS (all in this module; the release's files only call them)
    after_ack(listing, person, reply, context)   after the acknowledgement; an in-memory append; never raises; no I/O, no logging
    after_cycle(dry_run)                         after the watcher released the run lock; hands the items to the features, each isolated,
                                                 inside a wall-clock budget; never raises; never in a dry run
    card_lines(lines, listing)                   lets features offer an extra card line with a priority; added only while the card stays ONE
                                                 Telegram call (measured the way telegram.send_card and tgtext measure); a provider that is
                                                 slower than its deadline yields no line
  STORAGE (store.py)   the features' own SQLite file (optional.db), created lazily by the first enabled feature that writes; every write is one
                       short transaction that is INTERRUPTED if it runs too long (so a lock is never held long); a read-only accessor for the
                       primary database; optional code has no write access to the primary database at all
  RUNNER (runner.py)   one supervised background child for work that needs the network: single-flight by an exclusive lock file, its own budget,
                       killed if overdue at the start and end of every cycle and by its own timer, never holding the run lock, never started
                       without queued work
  REGISTRY             features register (flag, loader); a feature whose environment flag is not "1" is never imported. With nothing enabled:
                       nothing is imported that does work, no file or table is created, no process is started, and the cycle is the release's.

Importing this package does no work.
"""
import os
import threading
import time

BUFFER_LIMIT = 500             # items waiting in memory for after_cycle (bounded; the overflow is counted, never silently grown)
CYCLE_BUDGET_SECONDS = 2.0     # all features' after_cycle together
CARD_BUDGET_SECONDS = 0.05     # all features' card_line providers together, per card
SLOW_PENALTY_SECONDS = 60      # a provider that missed its deadline is skipped for this long
CAPTION_UNITS = 1024           # as telegram.send_card: a photo caption, measured on the raw HTML text
_registry = []                 # [(flag, loader)]
_loaded = {}                   # flag -> feature or None (failed to load)
_items = []                    # after_ack items for the next after_cycle
_pending = {}                  # feature name -> items it did not finish
_slow_until = {}
_lock = threading.Lock()
dropped = 0                    # items refused because the buffer was full (a feature may report it)


def register(flag, loader):
    """Declare a feature: it is loaded (the loader called, its module imported) only when the environment variable `flag` is "1"."""
    _registry.append((flag, loader))


def reset():
    """Forget everything (tests)."""
    global dropped
    with _lock:
        _registry.clear(), _loaded.clear(), _items.clear(), _pending.clear(), _slow_until.clear()
        dropped = 0


def enabled_any():
    """Is any registered feature switched on? Environment checks only: no import, no I/O, cannot raise."""
    return any(os.environ.get(flag) == "1" for flag, _ in _registry)


def features():
    """The enabled features, loaded once. A feature that fails to load is skipped (it never takes the process down)."""
    out = []
    for flag, loader in list(_registry):
        if os.environ.get(flag) != "1":
            continue
        if flag not in _loaded:
            try:
                _loaded[flag] = loader()
            except Exception:
                _loaded[flag] = None
        if _loaded[flag] is not None:
            out.append(_loaded[flag])
    return out


def message_id(reply):
    """The Telegram message id inside a send's reply (what tracker.record_shown reads), or None. Never raises."""
    try:
        value = reply["result"]["message_id"]
        return value if type(value) is int else None
    except Exception:
        return None


# ---------------------------------------------------------------- the hook after the acknowledgement

def after_ack(listing, person, reply, context):
    """Called by `_run` once per alert, AFTER its receipt and acknowledgement. An append to a bounded in-memory list and nothing else:
    no I/O, no logging, no import, no lock; it can never raise (a failure here is swallowed, because it must not cost a receipt)."""
    global dropped
    try:
        if not _registry or not enabled_any():
            return
        if len(_items) >= BUFFER_LIMIT:
            dropped += 1
            return
        _items.append({"listing": listing, "person": person, "reply": reply, "context": context, "at": time.time()})
    except Exception:
        pass


# ---------------------------------------------------------------- the hook after the cycle

def bounded(fn, seconds):
    """Run fn() in a daemon thread and wait at most `seconds`: (finished, result). On a timeout the thread is abandoned (it cannot hold
    anything the cycle needs) and the caller moves on; an exception inside fn counts as not finished."""
    box = {}

    def work():
        try:
            box["result"] = fn()
            box["done"] = True
        except Exception:
            box["done"] = False

    thread = threading.Thread(target=work, name="optional", daemon=True)
    thread.start()
    thread.join(max(0.0, seconds))
    return bool(box.get("done")), box.get("result")


def after_cycle(dry_run=False, budget=None, clock=time.monotonic):
    """Called by the watcher AFTER it has released the run lock and after every person's sends. Hands the collected items to each enabled
    feature (`feature.after_cycle(items, deadline)`: local, bounded work on the feature's own storage) inside one wall-clock budget; every
    feature is isolated; what a feature does not finish waits for the next cycle (bounded). Never raises; does nothing in a dry run."""
    try:
        if dry_run:
            with _lock:
                _items.clear()
            return 0
        with _lock:
            fresh, _items[:] = list(_items), []
        active = features() if _registry and enabled_any() else []
        if not active or not (fresh or any(_pending.values())):
            return 0
        deadline = clock() + (CYCLE_BUDGET_SECONDS if budget is None else budget)
        handled = 0
        for feature in active:
            name = getattr(feature, "name", repr(feature))
            batch = _pending.pop(name, []) + fresh
            left = deadline - clock()
            if left <= 0:
                _pending[name] = batch[-BUFFER_LIMIT:]
                continue
            finished, result = bounded(lambda f=feature, b=batch, d=deadline: f.after_cycle(b, d), left)
            if finished and not result:
                handled += len(batch)
            else:                                   # not finished (or the feature asks to keep them): try again next cycle
                keep = batch if not finished or result is True else list(result or [])
                _pending[name] = keep[-BUFFER_LIMIT:]
        return handled
    except Exception:
        return 0


# ---------------------------------------------------------------- card lines

def _units(text):
    return len(text.encode("utf-16-le")) // 2


def card_lines(lines, listing):
    """Offer enabled features the chance to add ONE extra line each to a card. `lines` is the card's list of lines (returned unchanged
    when no feature is enabled, or none offers anything). Providers (`feature.card_line(listing)` -> (priority, text) or None) must be
    pure in-memory or a bounded local read: they run with a hard deadline, and one that exceeds it yields no line. Lines are added by
    priority (highest first) only while the card stays ONE Telegram call, measured as the release measures it: a card whose text fits a
    photo caption (raw UTF-16 units <= 1024) must still fit; a card that already goes out as photo + text message must keep its text
    within one message (tgtext.visible <= 4096). The lowest priorities are the ones dropped."""
    try:
        if not _registry or not enabled_any():
            return lines
        now = time.monotonic()
        active = [f for f in features() if hasattr(f, "card_line") and _slow_until.get(getattr(f, "name", ""), 0) <= now]
        if not active:
            return lines
        offers = []

        def collect():
            for f in active:
                try:
                    offer = f.card_line(listing)
                except Exception:
                    continue
                if offer:
                    offers.append((offer[0], getattr(f, "name", ""), offer[1]))

        thread = threading.Thread(target=collect, name="optional-card", daemon=True)
        thread.start()
        thread.join(CARD_BUDGET_SECONDS)
        if thread.is_alive():                                             # too slow: whatever finished is used, the rest are skipped for a while
            for f in active:
                _slow_until[getattr(f, "name", "")] = time.monotonic() + SLOW_PENALTY_SECONDS
        offers = sorted((o for o in list(offers) if isinstance(o[2], str) and o[2].strip()), key=lambda o: -o[0])
        if not offers:
            return lines
        from .. import tgtext
        base = "\n".join(lines)
        within = (lambda text: _units(text) <= CAPTION_UNITS) if _units(base) <= CAPTION_UNITS else \
            (lambda text: tgtext.visible(text) <= tgtext.MESSAGE)
        if not within(base):
            return lines                                                   # already beyond one message: add nothing
        chosen = []
        for _, _, text in offers:
            if within("\n".join([*lines, *chosen, text])):
                chosen.append(text)
        return [*lines, *chosen] if chosen else lines
    except Exception:
        return lines


from .runner import RUNNER  # noqa: E402,F401  (the class is defined; nothing runs until a feature has queued work)
