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
import logging
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

STEP_SECONDS = 45
KILL_GRACE_SECONDS = 15
CONFIRM_SECONDS = 1.0          # the longest the watcher waits to confirm one kill
GROUP_CHECK_SECONDS = 0.25
GROUP_CHECK_TIMEOUT = 1.0
GROUP_MEMORY_BYTES = 1_500_000_000
GROUP_PROCESS_LIMIT = 64
GROUP_OUTPUT_LIMIT = 16_384
log = logging.getLogger(__name__)


def group_usage(pgid):
    """Return aggregate RSS and process count for the child's process group."""
    ps = subprocess.Popen(["/bin/ps", "-g", str(pgid), "-o", "pgid=,rss="],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    deadline, output = time.monotonic() + GROUP_CHECK_TIMEOUT, bytearray()
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([ps.stdout], [], [], left)[0]:
                raise TimeoutError("process-group inspection timed out")
            chunk = os.read(ps.stdout.fileno(), min(4096, GROUP_OUTPUT_LIMIT + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > GROUP_OUTPUT_LIMIT:
                raise ValueError("process-group inspection exceeded its output limit")
        if ps.wait(timeout=max(0, deadline - time.monotonic())) not in (0, 1):
            raise OSError("process-group inspection failed")
    finally:
        ps.stdout.close()
        if ps.poll() is None:
            ps.kill()
        try:
            ps.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            log.warning("optional process-group inspector did not exit promptly")
    total, count = 0, 0
    for line in output.splitlines():
        group, rss = line.split()
        if int(group) == pgid:
            total += int(rss) * 1024
            count += 1
    if not count:
        raise ProcessLookupError("process group not found")
    return total, count


def group_alive(proc):
    try:
        os.killpg(proc.pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Runner:
    def __init__(self):
        self.proc, self.started, self.timer, self.dying = None, 0.0, None, []
        self.monitor_stop = None

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
            self.monitor_stop = threading.Event()
            monitor = threading.Thread(target=self._monitor, args=(proc, self.timer, self.monitor_stop), daemon=True)
            monitor.start()
            return True
        except Exception as ex:
            log.warning("optional step could not start or be supervised (%s)", type(ex).__name__)
            if self.proc is not None:
                self._kill_group(self.proc)
            return False

    @staticmethod
    def _kill_group(proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as ex:
            log.warning("optional step group could not be signalled (%s)", type(ex).__name__)
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                proc.kill()

    def _monitor(self, proc, timer, stop):
        terminating = False
        while not stop.wait(GROUP_CHECK_SECONDS):
            try:
                if proc.poll() is not None:
                    terminating = True
                if terminating:
                    if proc.poll() is not None and not group_alive(proc):
                        timer.cancel()
                        return
                    self._kill_group(proc)
                    continue
                memory, count = group_usage(proc.pid)
                if memory > GROUP_MEMORY_BYTES or count > GROUP_PROCESS_LIMIT:
                    log.warning("optional step exceeded its process-group resource limit")
                    terminating = True
                    self._kill_group(proc)
            except Exception as ex:
                if not terminating:
                    log.warning("optional step group supervision failed (%s)", type(ex).__name__)
                terminating = True
                self._kill_group(proc)

    def _confirm(self, proc):
        """Kill the group and wait a bounded time for the exit; a child not confirmed dead stays in `dying` and is retried."""
        if proc.poll() is not None and not group_alive(proc):
            return True
        self._kill_group(proc)
        try:
            proc.wait(timeout=CONFIRM_SECONDS)
            if not group_alive(proc):
                return True
        except Exception:
            pass
        self.dying.append(proc)
        return False

    def reap(self):
        """Poll and kill without waiting. An unconfirmed child remains owned and prevents another child from starting."""
        try:
            self.dying = [p for p in self.dying if not self._retry(p)]
            proc = self.proc
            if proc is None:
                if self.dying:
                    return "running"
                self._drop_timer()
                return None
            if proc.poll() is not None:
                self.proc = None
                if not self._retry(proc):
                    self.dying.append(proc)
                if self.dying:
                    return "running"
                self._drop_timer()
                return None
            if time.monotonic() - self.started > STEP_SECONDS + KILL_GRACE_SECONDS:
                self.proc = None
                self.dying.append(proc)
                self._kill_group(proc)
                return "running"
            return "running"
        except Exception:
            return "running" if self.proc is not None or self.dying else None

    def _retry(self, proc):
        if proc.poll() is not None and not group_alive(proc):
            return True
        self._kill_group(proc)
        return proc.poll() is not None and not group_alive(proc)

    def _drop_timer(self):
        timer, self.timer = self.timer, None
        if timer is not None:
            timer.cancel()
        monitor_stop, self.monitor_stop = self.monitor_stop, None
        if monitor_stop is not None:
            monitor_stop.set()

    def stop(self):
        """Kill and reap the group, retaining supervision if its exit cannot be confirmed. Never raises."""
        try:
            proc, self.proc = self.proc, None
            waiting, self.dying = [*([proc] if proc is not None else []), *self.dying], []
            for p in waiting:
                try:
                    self._confirm(p)                     # retains the group if its exit is not confirmed
                except Exception:
                    self.dying.append(p)
            if not self.dying:
                self._drop_timer()
        except Exception:
            pass


RUNNER = Runner()
