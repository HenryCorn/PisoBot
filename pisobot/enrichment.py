"""Optional facts for the cards (G2 certificate area/band/value, G6 floorplan area) with a HARD time budget.

Everything here is optional: a card is complete without it. So it must never be able to delay a card:
  * it runs only for the adverts about to be shown, in a single worker thread, with a per-advert wall-clock limit
    (ENRICH_ADVERT_SECONDS) and a rolling per-window limit across every call (ENRICH_WINDOW_SECONDS);
  * the worker gets a COPY of the listing; the caller waits at most the budget, and on timeout the card goes out
    without the fact (the stuck worker is abandoned; its network calls are themselves bounded by budget.py, and while
    it is still running no further enrichment is started, so work can never pile up behind it);
  * only on success are the derived fields copied back, so a late worker can never change a card that is being sent;
  * with every feature off, enrich() returns before touching anything: the pipeline is exactly as before.
"""
import concurrent.futures
import contextvars
import copy
import logging
import threading
import time

from . import budget, config

log = logging.getLogger(__name__)

FIELDS = ("floor_area_m2", "area_source", "epc", "value", "floorplan_read", "check_notes")
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="enrich")
_stuck = None                 # the last worker we gave up waiting for
_window = {"start": None, "spent": 0.0}
_lock = threading.Lock()


def providers():
    """The enabled annotators, in the order they must run (a later one may use an earlier one's area)."""
    from . import epc
    fns = []
    try:                       # G6 (floorplan) joins here when that module is present
        from . import floorplan
        if hasattr(floorplan, "annotate") and floorplan.enabled():
            fns.append(floorplan.annotate)
    except ImportError:
        pass
    if epc.enabled():
        fns.append(epc.annotate)
    return fns


def enabled():
    return bool(providers())


def observe(listings):
    """Cheap bookkeeping, no network: remember portal-stated areas as comparables. Off unless enabled."""
    from . import epc
    if epc.enabled() or enabled():
        epc.observe(listings)


def _one(work, fns, deadline):
    from . import epc
    token = budget.DEADLINE.set(deadline)
    try:
        for fn in fns:
            if budget.expired():
                break
            fn(work)
        if work.floor_area_m2 and not budget.expired():
            epc.value_for(work)
    finally:
        budget.DEADLINE.reset(token)
    return work


def enrich(listings, advert_seconds=None, window_seconds=None, clock=time.monotonic):
    """Annotate each listing in place within the budget; returns how many were enriched. Never raises."""
    global _stuck
    try:
        fns = providers()
        if not fns:
            return 0
        advert_seconds = config.ENRICH_ADVERT_SECONDS if advert_seconds is None else advert_seconds
        window_seconds = config.ENRICH_WINDOW_SECONDS if window_seconds is None else window_seconds
        done = 0
        for listing in listings:
            if listing.tenure != "rent":
                continue
            with _lock:
                now = clock()
                if _window["start"] is None or now - _window["start"] >= config.WATCH_INTERVAL_MIN * 60:
                    _window["start"], _window["spent"] = now, 0.0
                left = window_seconds - _window["spent"]
            if left <= 0:
                log.info("enrichment: window budget used up; remaining cards go out without optional facts")
                break
            if _stuck is not None and not _stuck.done():
                log.info("enrichment: an earlier lookup is still running; skipping")
                break
            allowed = min(advert_seconds, left)
            work = copy.copy(listing)
            for name in FIELDS:                       # the worker mutates copies, never the card being sent
                if hasattr(listing, name):
                    setattr(work, name, copy.deepcopy(getattr(listing, name)))
            started = clock()
            future = _pool.submit(contextvars.copy_context().run, _one, work, fns, started + allowed)
            try:
                future.result(timeout=allowed)
            except concurrent.futures.TimeoutError:
                _stuck = future
                log.warning("enrichment: %s exceeded %.0f s; card sent without the optional facts", listing.key, allowed)
            except Exception as ex:
                log.warning("enrichment: %s failed (%s); card unchanged", listing.key, type(ex).__name__)
            else:
                for name in FIELDS:
                    if hasattr(work, name):
                        setattr(listing, name, getattr(work, name))
                done += 1
            finally:
                with _lock:
                    _window["spent"] += clock() - started
        return done
    except Exception as ex:
        log.warning("enrichment failed (%s); cards unchanged", type(ex).__name__)
        return 0


def reset():
    """Forget the window and any abandoned worker (tests)."""
    global _stuck
    with _lock:
        _stuck = None
        _window["start"], _window["spent"] = None, 0.0
