"""Entry point of the supervised child: python -m pisobot.optional.step  (see runner.py).

Order matters: take the single-flight lock, set the limits, arm the watchdog and the parent-death check FIRST, and only then load features."""
import logging
import os
import sys
import threading
import time

from . import runner

log = logging.getLogger(__name__)
MEMORY_LIMIT_BYTES = 1_500_000_000
MEMORY_CHECK_SECONDS = 0.25


def open_lock(path):
    """Open the single-flight lock file WITHOUT following a link and WITHOUT truncating, and prove it is a plain, singly-linked regular
    file before anything is done with it. Raises OSError/ValueError otherwise (the caller then exits without touching anything)."""
    import stat
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("optional.lock must be a plain, unlinked file")
        return os.fdopen(fd, "r+")
    except BaseException:
        os.close(fd)
        raise


def resident_bytes():
    """This process's peak resident memory in bytes (ru_maxrss is bytes on macOS, kilobytes elsewhere)."""
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def limit_resources():
    """CPU time is always limited. Memory: the address-space limit where the platform accepts it (Linux); where it does not (macOS
    rejects RLIMIT_AS), a watchdog checks peak resident memory. That polling fallback is best effort, not a hard memory limit: a burst
    can exceed it before the watchdog runs. Returns the mechanism, or raises when neither can be arranged."""
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (runner.STEP_SECONDS + 20, runner.STEP_SECONDS + 20))
    resident_bytes()                                                              # the fallback must work on this platform
    try:
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
        return "address-space"
    except (ValueError, OSError):
        return "resident-watchdog"


def bounded(fn, seconds):
    box = {}

    def work():
        try:
            box["result"] = fn()
        except Exception:
            box["error"] = True

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(max(0.0, seconds))
    return not thread.is_alive() and "error" not in box, box.get("result")


def load_features():
    """Load every enabled feature (in this child only)."""
    from . import _registry
    from .. import registry_loader  # noqa: F401  (registers the shipped features)
    out = []
    for flag, loader in list(_registry):
        if os.environ.get(flag) == "1":
            try:
                feature = loader()
                if feature is not None and hasattr(feature, "step"):
                    out.append(feature)
            except Exception as ex:
                log.warning("optional feature %s failed to load (%s)", flag, type(ex).__name__)
    return out


def main():
    import fcntl
    import signal
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.environ.get("STORAGE") != "local":
        return 0
    from . import store
    try:
        lock = open_lock(store.path().with_suffix(".lock"))
    except (OSError, ValueError) as ex:
        log.error("optional step refused: its lock file is not a plain file (%s)", type(ex).__name__)
        return 5
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("an optional step is already running; exiting")
        return 0
    try:
        log.info("optional step limits: cpu, memory by %s", limit_resources())
    except Exception as ex:
        if os.environ.get("OPTIONAL_ALLOW_NO_LIMIT") != "1":
            log.error("optional step refused: could not set its resource limits (%s)", type(ex).__name__)
            return 3
        log.warning("optional step running WITHOUT resource limits (OPTIONAL_ALLOW_NO_LIMIT=1)")
    signal.alarm(int(runner.STEP_SECONDS + runner.KILL_GRACE_SECONDS - 3))      # the child's own watchdog (default action ends it)
    parent = os.getppid()

    def watchdog():
        while True:
            time.sleep(MEMORY_CHECK_SECONDS)
            if os.getppid() != parent:
                os._exit(4)                                                       # the supervisor died: do not outlive it
            try:
                if resident_bytes() > MEMORY_LIMIT_BYTES:
                    os._exit(6)                                                   # over the memory ceiling (every platform)
            except Exception:
                pass
    threading.Thread(target=watchdog, daemon=True).start()
    deadline = time.monotonic() + runner.STEP_SECONDS
    for feature in load_features():
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            ok, has = bounded(feature.has_work, min(left, 5))
            if ok and has:
                bounded(lambda f=feature: f.step(deadline), deadline - time.monotonic())
        except Exception as ex:
            log.warning("optional feature failed (%s)", type(ex).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
