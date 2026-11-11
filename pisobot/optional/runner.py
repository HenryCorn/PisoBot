"""The supervised background step for optional work that needs the network (G0).

A feature that needs HTTP or a model has `has_work()` (a cheap read-only check) and `step(deadline)`. The watcher starts ONE child process
(`python -m pisobot.optional.step`) after a cycle's after_cycle, only when an enabled feature reports queued work:
  * single flight: the child takes an exclusive non-blocking lock on optional.lock and exits if it is held;
  * its own budget: a SIGALRM watchdog inside the child, a daemon timer in the watcher, and a check at the start and end of every cycle all
    kill it when it is overdue, so a long cycle cannot stop the supervision;
  * it never holds the run lock, never runs in a dry run, and refuses to run if its memory/CPU limits cannot be set (unless explicitly opted out);
  * with nothing enabled or nothing queued, no process is started.
Importing this module does no work.
"""
import contextlib
import subprocess
import sys
import threading
import time
from pathlib import Path

STEP_SECONDS = 45
KILL_GRACE_SECONDS = 15


class Runner:
    def __init__(self):
        self.proc, self.started = None, 0.0

    def has_work(self):
        from . import features
        for feature in features():
            try:
                if hasattr(feature, "step") and feature.has_work():
                    return True
            except Exception:
                continue
        return False

    def maybe_start(self, popen=subprocess.Popen):
        """Start the child if an enabled feature has queued work and none is running. Never raises."""
        try:
            from . import enabled_any
            if not enabled_any() or self.reap() == "running" or not self.has_work():
                return False
            proc = popen([sys.executable, "-m", "pisobot.optional.step"], stdin=subprocess.DEVNULL,
                         cwd=str(Path(__file__).resolve().parents[2]))
            self.proc, self.started = proc, time.monotonic()
            timer = threading.Timer(STEP_SECONDS + KILL_GRACE_SECONDS, self._kill, args=(proc,))
            timer.daemon = True            # its own timer: kills the child whatever the watcher is doing (a long cycle, a sweep)
            timer.start()
            return True
        except Exception:
            return False

    @staticmethod
    def _kill(proc):
        if proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.kill()

    def reap(self):
        """Collect a finished child or kill an overdue one: "running" or None. Called at the start and end of every cycle."""
        proc = self.proc
        if proc is None:
            return None
        if proc.poll() is not None:
            self.proc = None
            return None
        if time.monotonic() - self.started > STEP_SECONDS + KILL_GRACE_SECONDS:
            self._kill(proc)
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
            self.proc = None
            return None
        return "running"

    def stop(self):
        if self.proc is not None:
            self._kill(self.proc)
            self.proc = None


RUNNER = Runner()
