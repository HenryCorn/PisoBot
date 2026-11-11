"""The supervised background step for optional work that needs the network (G0).

A feature that needs HTTP or a model has `has_work()` and `step(deadline)`; its worker thread keeps a `work` flag. The watcher starts ONE
child (`python -m pisobot.optional.step`) after a cycle, only when an enabled feature's flag says work is queued. Everything here is
non-raising and bounded; the watcher's result or exception is never replaced by anything in this module.
  * the child is its own session and process group; signalling targets that group, including its remaining descendants;
  * single flight: the child takes an exclusive flock on optional.lock BEFORE it loads any feature;
  * budget: the child's own SIGALRM, a cancellable timer in the watcher, and a check at the start and end of every cycle;
  * ownership: a child that was killed but not yet confirmed dead stays tracked and is re-checked, never forgotten;
  * it never holds the run lock, never runs in a dry run, and is never started without queued work.
Importing this module does no work.
"""
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
    """Return aggregate RSS, process count and non-zombie count for the child's process group."""
    ps = subprocess.Popen(["/bin/ps", "-g", str(pgid), "-o", "pgid=,rss=,stat="],
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
    total, count, live = 0, 0, 0
    for line in output.splitlines():
        group, rss, state = line.split()
        if int(group) == pgid:
            total += int(rss) * 1024
            count += 1
            live += not state.startswith(b"Z")
    return total, count, live


class ChildGroup:
    """Keep the leader's PID reserved until the group is settled and signalling is disabled."""

    def __init__(self, proc, deadline):
        self.proc, self.deadline = proc, deadline
        self.lock = threading.Lock()
        self.stopping, self.wake, self.done = threading.Event(), threading.Event(), threading.Event()
        self.closed, self.lost = False, False

    def _exited(self):
        if self.proc.returncode is not None:
            raise ChildProcessError("group leader was reaped outside its supervisor")
        # WNOWAIT retains the zombie and its PID until finish() serializes the final reap with all signals.
        return os.waitid(os.P_PID, self.proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None

    def _lost(self):
        if not self.lost:
            self.lost = True
            log.error("optional step leader ownership lost; refusing further group signals")

    def signal(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            if self.closed or self.lost or self.proc is None:
                return
            try:
                self._exited()
            except ChildProcessError:
                self._lost()
                return
            except OSError as ex:
                log.warning("optional step ownership check failed (%s)", type(ex).__name__)
                return
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as ex:
                log.warning("optional step group could not be signalled (%s)", type(ex).__name__)
        finally:
            self.lock.release()

    def request_stop(self):
        self.stopping.set()
        self.wake.set()

    def terminate(self):
        self.request_stop()
        self.signal()

    def finish(self):
        with self.lock:
            if self.closed or self.lost:
                return self.closed
            try:
                if not self._exited():
                    return False
                self.proc.wait(timeout=0)
            except ChildProcessError:
                self._lost()
                return False
            except subprocess.TimeoutExpired:
                return False
            self.closed = True
            self.done.set()
            return True

    def supervise(self, timer):
        terminating = failure_reported = False
        try:
            while not self.done.is_set() and not self.lost:
                try:
                    with self.lock:
                        exited = self._exited()
                    terminating = terminating or exited or self.stopping.is_set() or time.monotonic() >= self.deadline
                    if terminating:
                        self.signal()
                    memory, count, live = group_usage(self.proc.pid)
                    if not count:
                        raise ProcessLookupError("process group inspection returned no members")
                    if terminating and exited and not live:
                        self.finish()
                    elif not terminating:
                        if memory > GROUP_MEMORY_BYTES or count > GROUP_PROCESS_LIMIT:
                            log.warning("optional step exceeded its process-group resource limit")
                            terminating = True
                            self.signal()
                    failure_reported = False
                except ChildProcessError:
                    with self.lock:
                        self._lost()
                except Exception as ex:
                    if not failure_reported:
                        log.warning("optional step group supervision failed (%s)", type(ex).__name__)
                        failure_reported = True
                    terminating = True
                    self.signal()
                self.wake.wait(GROUP_CHECK_SECONDS)
                self.wake.clear()
        except Exception as ex:
            log.error("optional step supervisor stopped unexpectedly (%s)", type(ex).__name__)
            self.terminate()
        finally:
            if self.done.is_set() or self.lost:
                timer.cancel()


class Runner:
    def __init__(self):
        self.proc, self.started, self.timer, self.dying = None, 0.0, None, []
        self.group = None
        self.launch_lock = threading.Lock()

    @staticmethod
    def has_work():
        from . import enabled_any, workers
        if not enabled_any():
            return False
        return any(w.work and w.feature is not None for w in workers())

    def maybe_start(self, popen=subprocess.Popen):
        """Schedule a child launch without waiting for process creation. Never raises."""
        if not self.launch_lock.acquire(blocking=False):
            return False
        try:
            from . import enabled_any, execution_available
            if not execution_available() or not enabled_any() or self.reap() == "running" or not self.has_work():
                return False
            if (not all(hasattr(os, name) for name in ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT"))
                    or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL):
                log.warning("optional step refused: safe child ownership is unavailable")
                return False
            started = time.monotonic()
            group = ChildGroup(None, started + STEP_SECONDS + KILL_GRACE_SECONDS)
            timer = threading.Timer(STEP_SECONDS + KILL_GRACE_SECONDS, group.terminate)
            timer.daemon = True

            def launch_and_supervise():
                try:
                    proc = popen([sys.executable, "-m", "pisobot.optional.step"], stdin=subprocess.DEVNULL,
                                 start_new_session=True, cwd=str(Path(__file__).resolve().parents[2]))
                except Exception as ex:
                    log.warning("optional step could not start (%s)", type(ex).__name__)
                    timer.cancel()
                    with group.lock:
                        group.closed = True
                        group.done.set()
                    return
                with group.lock:
                    group.proc = self.proc = proc
                group.supervise(timer)

            monitor = threading.Thread(target=launch_and_supervise, daemon=True)
            self.started, self.group, self.timer = started, group, timer
            try:
                timer.start()
                monitor.start()
            except Exception:
                timer.cancel()
                group.done.set()
                self.reap()
                raise
            return True
        except Exception as ex:
            log.warning("optional step could not start or be supervised (%s)", type(ex).__name__)
            return False
        finally:
            self.launch_lock.release()

    def reap(self):
        """Check supervisor state without waiting; an unconfirmed group prevents another child from starting."""
        try:
            group = self.group
            if group is None:
                return "running" if self.proc is not None or self.dying else None
            if group.done.is_set():
                self.proc, self.group, self.dying = None, None, []
                if self.timer is not None:
                    self.timer.cancel()
                    self.timer = None
                return None
            if time.monotonic() - self.started > STEP_SECONDS + KILL_GRACE_SECONDS:
                group.request_stop()
                self.proc, self.dying = None, [group.proc] if group.proc is not None else []
            return "running"
        except Exception:
            return "running" if self.group is not None or self.proc is not None or self.dying else None

    def stop(self):
        """Request group termination and wait boundedly; retain any unconfirmed child."""
        try:
            group = self.group
            if group is not None:
                group.terminate()
                if group.done.wait(CONFIRM_SECONDS):
                    self.reap()
                else:
                    self.proc, self.dying = None, [group.proc] if group.proc is not None else []
        except Exception:
            pass


RUNNER = Runner()
