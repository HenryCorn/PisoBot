"""Entry point of the supervised child: python -m pisobot.optional.step  (see runner.py)."""
import logging
import os
import sys
import time

from . import bounded, features
from . import runner

log = logging.getLogger(__name__)
MEMORY_LIMIT_BYTES = 1_500_000_000


def main():
    import fcntl
    import signal
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.environ.get("STORAGE") != "local":
        return 0
    from . import store
    from .. import registry_loader  # noqa: F401  (imports the shipped features' registrations)
    active = [f for f in features() if hasattr(f, "step")]
    if not active or not store.exists():
        return 0
    lock = open(store.path().with_suffix(".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("an optional step is already running; exiting")
        return 0
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (runner.STEP_SECONDS + 20, runner.STEP_SECONDS + 20))
    except Exception as ex:
        if os.environ.get("OPTIONAL_ALLOW_NO_LIMIT") != "1":
            log.error("optional step refused: could not set its resource limits (%s)", type(ex).__name__)
            return 3
        log.warning("optional step running WITHOUT resource limits (OPTIONAL_ALLOW_NO_LIMIT=1)")
    signal.alarm(int(runner.STEP_SECONDS + runner.KILL_GRACE_SECONDS - 3))      # the child's own watchdog: default SIGALRM ends it
    deadline = time.monotonic() + runner.STEP_SECONDS
    for feature in active:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            if feature.has_work():
                bounded(lambda f=feature: f.step(deadline), left)
        except Exception as ex:
            log.warning("optional feature %s failed (%s)", getattr(feature, "name", "?"), type(ex).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
