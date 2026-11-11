"""A wall-clock budget for optional work that sits in the alert path (EPC and floorplan enrichment).

enrichment.py sets DEADLINE (a time.monotonic() value) around each advert's optional lookups; every request
those lookups make bounds its own timeout by time_left() and refuses to start when the budget is spent. The
thread that runs them is also abandoned by the caller when the deadline passes, so a slow host can only ever
delay a card by the budget, never hold it up.
"""
import contextvars
import time

DEADLINE = contextvars.ContextVar("pisobot_enrichment_deadline", default=None)


def time_left(clock=time.monotonic):
    """Seconds left in the current budget, or None when no budget is set (ordinary, unbudgeted code)."""
    deadline = DEADLINE.get()
    return None if deadline is None else deadline - clock()


def expired(clock=time.monotonic):
    left = time_left(clock)
    return left is not None and left <= 0


def timeout(default, clock=time.monotonic):
    """A request timeout: `default`, shortened to the time left when a budget is set (never below 0.5 s)."""
    left = time_left(clock)
    return default if left is None else max(0.5, min(default, left))
