"""The supervised background step for optional work that needs the network (G0).

A feature that needs HTTP or a model has `has_work()` and `step(deadline)`; its worker thread keeps a `work` flag. The watcher starts ONE
child (`python -m pisobot.optional.step`) after a cycle, only when an enabled feature's flag says work is queued. Everything here is
non-raising and bounded; the watcher's result or exception is never replaced by anything in this module.
  * the child is its own session and process group; it is killed as a GROUP, so descendants die with it;
  * single flight: the child takes an exclusive flock on optional.lock BEFORE it loads any feature;
  * budget: the child's own SIGALRM, a cancellable timer in the watcher, and a check at the start and end of every cycle;
  * ownership: a child that was killed but not yet confirmed dead stays tracked and is re-checked, never forgotten;
  * it never holds the run lock, never runs in a dry run, and is never started without queued work.
Importing this module does no work.
"""
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

STEP_SECONDS = 45
KILL_GRACE_SECONDS = 15
CONFIRM_SECONDS = 1.0          # the longest the watcher waits to confirm one kill


class Runner:
    def __init__(self):
        self.proc, self.started, self.timer, self.dying = None, 0.0, None, []

    @staticmethod
    def has_work():
        from . import enabled_any, workers
        if not enabled_any():
            return False
        return any(w.work and w.feature is not None for w in workers())

    def maybe_start(self, popen=subprocess.Popen):
        """Start the child if an enabled feature has queued work and none is running. Never raises."""
        try:
            from . import enabled_any
            if not enabled_any() or self.reap() == "running" or not self.has_work():
                return False
            proc = popen([sys.executable, "-m", "pisobot.optional.step"], stdin=subprocess.DEVNULL, start_new_session=True,
                         cwd=str(Path(__file__).resolve().parents[2]))
            self.proc, self.started = proc, time.monotonic()
            self.timer = threading.Timer(STEP_SECONDS + KILL_GRACE_SECONDS, self._kill_group, args=(proc,))
            self.timer.daemon = True
            self.timer.start()
            return True
        except Exception:
            return False

    @staticmethod
    def _kill_group(proc):
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    proc.kill()

    def _confirm(self, proc):
        """Kill the group and wait a bounded time for the exit; a child not confirmed dead stays in `dying` and is retried."""
        self._kill_group(proc)
        try:
            proc.wait(timeout=CONFIRM_SECONDS)
            return True
        except Exception:
            self.dying.append(proc)
            return False

    def reap(self):
        """Collect a finished child, kill an overdue one, retry the ones not yet confirmed dead: "running" or None. Never raises."""
        try:
            self.dying = [p for p in self.dying if p.poll() is None and not self._retry(p)]
            proc = self.proc
            if proc is None:
                return None
            if proc.poll() is not None:
                self._drop_timer()
                self.proc = None
                return None
            if time.monotonic() - self.started > STEP_SECONDS + KILL_GRACE_SECONDS:
                self._drop_timer()
                self.proc = None
                self._confirm(proc)
                return None
            return "running"
        except Exception:
            return None

    def _retry(self, proc):
        self._kill_group(proc)
        try:
            proc.wait(timeout=0.05)
            return True
        except Exception:
            return False

    def _drop_timer(self):
        timer, self.timer = self.timer, None
        if timer is not None:
            timer.cancel()

    def stop(self):
        """Shutdown: cancel the timer, kill the child's whole group and reap it (bounded). Never raises."""
        try:
            self._drop_timer()
            proc, self.proc = self.proc, None
            for p in [*( [proc] if proc is not None else []), *self.dying]:
                self._kill_group(p)
                with contextlib.suppress(Exception):
                    p.wait(timeout=CONFIRM_SECONDS)
            self.dying = []
        except Exception:
            pass


RUNNER = Runner()
