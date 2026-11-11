"""Independent G1 review findings (ported reproductions). The defects the reviewer demonstrated are now asserted fixed;
the controls it found sound are asserted to stay sound. Offline: fakes only, no network, no keys."""
import collections
import datetime as dt
import multiprocessing
import queue
import sqlite3
import subprocess
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from pisobot import archive, config, d1, localdb, main, sweep, telegram, timing, users, watch
from pisobot.sources import otm_rent, rightmove

from test_fast_poll import FastBase
from test_users import FRIEND, OWNER, home

ROOT = Path(__file__).resolve().parents[1]
RIGHTMOVE_HEAD = rightmove.fetch_head


class ReviewBase(FastBase):
    pass


# ---------------------------------------------------------------- finding 1: telemetry stays out of the delivery order

class DeliveryOrderTests(ReviewBase):
    def test_timing_is_captured_in_memory_and_written_only_after_the_receipt_and_acknowledgement(self):
        self.baseline()
        self.full = [home(1), home(2)]
        events = []
        real_save, real_verdicts = main.save_seen, archive.record_verdicts
        real_capture, real_write = timing.capture, timing.write

        def save(seen):
            if "rm:2" in seen:
                events.append("receipt")
            return real_save(seen)

        def verdicts(*a, **kw):
            if kw.get("acknowledged"):
                events.append("ack")
            return real_verdicts(*a, **kw)

        def capture(*a, **kw):
            events.append("capture")
            return real_capture(*a, **kw)

        def write(rows, *a, **kw):
            events.append("write")
            return real_write(rows, *a, **kw)

        self.during_send = lambda: events.append("telegram")
        with patch.object(main, "save_seen", side_effect=save), patch.object(archive, "record_verdicts", side_effect=verdicts), \
                patch.object(timing, "capture", side_effect=capture), patch.object(timing, "write", side_effect=write):
            self.watcher.cycle()
        first_write = events.index("write")
        self.assertLess(events.index("telegram"), events.index("capture"))        # taken the instant Telegram accepted
        self.assertLess(events.index("capture"), events.index("receipt"))         # but with no I/O
        self.assertLess(events.index("receipt"), events.index("ack"))             # the release's order is untouched
        self.assertLess(events.index("ack"), first_write)                         # telemetry only afterwards
        self.assertEqual(timing.report()["rm"]["alerts"], 2)       # one per person

    def _contention_crash(self, release_control=False):
        """Telegram accepts a card, another writer holds the database, the sender is killed while waiting."""
        self.baseline()
        self.full = [home(1), home(2)]
        with timing._db():
            pass                                                    # schema exists: creation is not what blocks
        ctx = multiprocessing.get_context("fork")
        accepted, waiting = ctx.Event(), ctx.Event()
        held = []
        real_verdicts = archive.record_verdicts
        release_module = None
        if release_control:
            try:
                source = subprocess.check_output(["git", "-C", str(ROOT), "show", "origin/release-archive-pipeline:pisobot/main.py"],
                                                 text=True, stderr=subprocess.DEVNULL)
            except Exception:
                self.skipTest("release main.py not available from git")
            release_module = types.ModuleType("pisobot._review_release_main")
            release_module.__file__ = str(ROOT / "pisobot" / "main.py")
            release_module.__package__ = "pisobot"
            exec(compile(source, release_module.__file__, "exec"), release_module.__dict__)
            for attr in ("STATE_DIR", "SEEN_PATH", "passes_basic", "passes_area", "_ensure_photos"):
                setattr(release_module, attr, getattr(main, attr))

        def send(l, reason):
            if users.current_chat() == OWNER and l.key == "rm:2":
                db = sqlite3.connect(localdb.state_dir() / "pisobot.db")
                db.execute("BEGIN IMMEDIATE")                       # another local writer takes the lock
                held.append(db)
                accepted.set()

        def verdicts(*a, **kw):
            if kw.get("acknowledged"):
                waiting.set()                                       # the contested step is the existing acknowledgement
            return real_verdicts(*a, **kw)

        def child():
            with patch.object(config, "FAST_POLL_MINUTES", 0), patch.object(telegram, "send_listing", side_effect=send), \
                    patch.object(archive, "record_verdicts", side_effect=verdicts):
                if release_module is not None:
                    with patch.object(watch, "pipeline", release_module):
                        self.watcher.cycle()
                else:
                    self.watcher.cycle()

        process = ctx.Process(target=child)
        process.start()
        try:
            self.assertTrue(accepted.wait(5), "child never reached fake Telegram acceptance")
            self.assertTrue(waiting.wait(5), "child never reached the contested DB operation")
            time.sleep(0.15)
            self.assertTrue(process.is_alive(), "expected SQLite to be waiting")
            checkpointed = "rm:2" in main.load_seen()
        finally:
            if process.is_alive():
                process.terminate()
            process.join(5)
        self.assertFalse(process.is_alive())
        watch.Watcher(["rm"]).cycle()
        return checkpointed, self.counts()[(OWNER, "rm:2", "new")]

    def test_a_writer_holding_the_database_then_a_kill_does_not_resend(self):
        self.assertEqual(self._contention_crash(), (True, 0))      # was (False, 1): a duplicate the release does not have

    def test_the_release_control_behaves_the_same_way(self):
        self.assertEqual(self._contention_crash(release_control=True), (True, 0))

    def test_telemetry_waits_at_most_a_fraction_of_a_second_for_a_contended_writer(self):
        with timing._db():
            pass
        db = sqlite3.connect(localdb.state_dir() / "pisobot.db")
        self.addCleanup(db.close)
        db.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        written = timing.write([timing.capture(home(2), "new", "head")])
        elapsed = time.monotonic() - started
        db.rollback()
        self.assertEqual(written, 0)
        self.assertLess(elapsed, 1.5)                               # the database's own busy timeout is 30 s
        self.assertGreater(localdb.BUSY_TIMEOUT_MS / 1000, 10)

    def test_a_dead_telemetry_database_does_not_fail_or_lose_an_accepted_card(self):
        self.baseline()
        self.full = [home(1), home(2)]
        with patch.object(config, "FAST_POLL_MINUTES", 0), \
                patch.object(timing, "_db", side_effect=sqlite3.OperationalError("fixture telemetry unavailable")):
            self.watcher.cycle()
        self.assertEqual(set(self.counts().values()), {1})
        self.assertIn("rm:2", main.load_seen())


# ---------------------------------------------------------------- finding 4: retention

class RetentionTests(ReviewBase):
    def test_rows_older_than_thirty_days_are_deleted_on_write_and_by_the_cycle(self):
        now = dt.datetime.now(dt.timezone.utc)
        old = now - dt.timedelta(days=400)
        timing.write([timing.capture(home(i + 10), "new", sent_at=old) for i in range(12)], now=old)
        timing.write([timing.capture(home(2), "new", sent_at=now)], now=now)
        with timing._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM alert_timing").fetchone()[0], 1)       # was 13
        timing.write([timing.capture(home(3), "new", sent_at=now - dt.timedelta(days=31))], now=now - dt.timedelta(days=31))
        self.assertEqual(timing.purge_expired(now), 1)
        with timing._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM alert_timing").fetchone()[0], 1)

    def test_purge_expired_touches_nothing_when_there_is_no_table(self):
        self.assertEqual(timing.purge_expired(), 0)
        db = Path(localdb.state_dir()) / "pisobot.db"
        if db.exists():
            with sqlite3.connect(db) as conn:
                self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'alert_timing'").fetchone())

    def test_a_full_cycle_runs_the_retention(self):
        self.baseline()
        with patch.object(timing, "purge_expired") as purge:
            self.watcher.cycle()
        purge.assert_called()


# ---------------------------------------------------------------- findings 2, 3, 5: scheduling

class SchedulingTests(ReviewBase):
    def _serve(self, horizon=86400, fast_duration=0, request_report=False, real_fast=False):
        clock, events, stop = {"t": 0.0}, [], threading.Event()
        w = self.watcher
        run_fast = w.fast_cycle

        def get(timeout=None):
            if not w.pending.empty():
                return queue.Queue.get(w.pending, block=False)
            clock["t"] += timeout or 0
            if clock["t"] >= horizon:
                stop.set()
                return "rentals", None
            raise queue.Empty

        def full(kind="rentals", *a, **kw):
            events.append((kind, clock["t"]))
            if fast_duration and len([e for e in events if e[0] != "fast"]) >= 2:
                stop.set()
            return {"matches": 0}

        def fast(stop_event=None, deadline=None, *a, **kw):
            events.append(("fast", clock["t"]))
            if request_report:
                self.assertTrue(w.request_run("report", FRIEND))
            if real_fast:
                run_fast(stop, deadline=deadline)
            else:
                clock["t"] += fast_duration

        def slow_area(l, **kw):
            clock["t"] += fast_duration / (len(self.head) * len(users.everyone()))
            return None

        with patch.object(w.pending, "get", side_effect=get), patch.object(w, "cycle", side_effect=full), \
                patch.object(w, "fast_cycle", side_effect=fast), patch.object(main, "passes_area", side_effect=slow_area), \
                patch.object(watch.time, "monotonic", lambda: clock["t"]), patch.object(watch.random, "uniform", return_value=1), \
                patch.object(config, "WATCH_INTERVAL_MIN", 15):
            w.serve(stop, lambda *a: stop.wait())
        return events

    def test_a_long_fast_poll_cannot_delay_the_due_full_cycle(self):
        self.baseline()
        self.head = [home(i) for i in range(2, 10)]
        events = self._serve(fast_duration=320, real_fast=True)     # 20 simulated seconds per lookup, 16 lookups if unbounded
        self.assertEqual(events[0], ("rentals", 0))
        self.assertEqual(events[1], ("fast", 300))
        self.assertEqual(events[-1][0], "rentals")
        self.assertLessEqual(events[-1][1], 905)                   # was 1500: ten minutes late
        self.assertTrue(all(t < 900 - config.FAST_POLL_MARGIN_SECONDS for kind, t in events if kind == "fast"))
        self.assertGreater(len(self.sent), 0)                      # it did useful work before its deadline

    def test_a_queued_manual_report_is_not_held_up_by_a_fast_poll(self):
        self.baseline()
        self.head = [home(i) for i in range(2, 10)]
        events = self._serve(fast_duration=1200, request_report=True, real_fast=True)
        self.assertEqual([e[0] for e in events][:3], ["rentals", "fast", "report"])
        self.assertLess(events[2][1], 400)                         # was 1500
        self.assertEqual(self.sent, [])                            # the poll yielded before judging any advert

    def test_the_deadline_is_passed_into_the_run_and_stops_between_adverts(self):
        self.baseline()
        self.head = [home(i) for i in range(2, 8)]
        clock = {"t": 1000.0}
        def slow(l, **kw):
            clock["t"] += 40
        with patch.object(watch.time, "monotonic", lambda: clock["t"]), patch.object(main, "passes_area", side_effect=slow):
            self.watcher.fast_cycle(deadline=1100)
        self.assertEqual(len(self.sent), 1)                        # judging stopped at the deadline (minus the send reserve); what was judged was sent
        self.assertEqual({c for c, k, r in self.sent}, {OWNER})    # the second person was not reached: left for the next poll or scan

    def test_the_hard_cap_applies_even_without_a_caller_deadline(self):
        self.baseline()
        self.head = [home(i) for i in range(2, 12)]
        clock = {"t": 0.0}
        def slow(l, **kw):
            clock["t"] += 30
        with patch.object(watch.time, "monotonic", lambda: clock["t"]), patch.object(main, "passes_area", side_effect=slow), \
                patch.object(config, "FAST_POLL_MAX_SECONDS", 90):
            self.watcher.fast_cycle()
        self.assertLess(len(self.sent), 2 * 10)

    def test_a_partial_failure_backs_the_poll_off(self):
        self.baseline()
        fake_otm = types.SimpleNamespace(fetch_head=lambda: [], fetch=lambda: [])
        self.fetch_head.side_effect = RuntimeError("fixture portal unavailable")
        with patch.dict(main.SOURCES, {"otmr": fake_otm}):
            w = watch.Watcher(["rm", "otmr"])
            w.fast_cycle()
            self.assertEqual(w.fast_failures, 1)                    # was 0: the other portal's success erased it
            w.fast_cycle()
            self.assertEqual(w.fast_failures, 2)

    def test_a_failed_group_is_not_erased_by_a_later_successful_group(self):
        self.baseline()
        users.save_params(users.owner(), {"rent": 1500})

        def fetch():
            if config.MAX_RENT == 1500:
                raise RuntimeError("fixture group unavailable")
            return []

        self.fetch_head.side_effect = fetch
        w = watch.Watcher(["rm"])
        for expected in (1, 2, 3):
            w.fast_cycle()
            self.assertEqual(w.fast_failures, expected)
        with patch.object(watch.random, "uniform", return_value=1):
            self.assertGreater(w.fast_delay(), 300)

    def test_a_clean_poll_resets_the_backoff(self):
        self.baseline()
        self.fetch_head.side_effect = RuntimeError("down")
        self.watcher.fast_cycle()
        self.assertEqual(self.watcher.fast_failures, 1)
        self.fetch_head.side_effect = lambda: [home(1)]
        self.watcher.fast_cycle()
        self.assertEqual(self.watcher.fast_failures, 0)

    def test_total_failure_backoff_is_bounded_and_not_a_tight_loop(self):
        self.baseline()
        self.fetch_head.side_effect = RuntimeError("fixture all portals unavailable")
        delays = []
        with patch.object(watch.random, "uniform", return_value=1):
            for _ in range(8):
                self.watcher.fast_cycle()
                delays.append(self.watcher.fast_delay())
        self.assertEqual(delays, [600, 900, 900, 900, 900, 900, 900, 900])

    def test_the_real_schedule_is_192_polls_not_288(self):
        events = self._serve()
        counts = collections.Counter(kind for kind, _ in events)
        self.assertEqual(counts, {"rentals": 96, "fast": 192})
        searches = {"fast": counts["fast"] * 2, "full": counts["rentals"] * 6}      # 3 pages x 2 portals per scan
        self.assertEqual((searches["fast"], searches["full"], sum(searches.values())), (384, 576, 960))
        self.assertAlmostEqual(searches["fast"] / searches["full"], 2 / 3, places=2)          # +67%, not +100%

    def test_source_search_request_counts_and_fast_only_lookups(self):
        import json
        html = '<script id="__NEXT_DATA__">' + json.dumps({"props": {"pageProps": {
            "initialReduxState": {"results": {"list": [], "paginationControls": {"next": True}}}}}}) + "</script>"
        calls = []
        with patch.object(otm_rent.net, "page", side_effect=lambda url, **kw: (calls.append(url), html)[1]), \
                patch.object(rightmove.net, "page", side_effect=lambda url, **kw: (calls.append(url), html)[1]), \
                patch.object(rightmove, "_results", return_value={"properties": [], "resultCount": 500}), \
                patch.object(config, "RENT_MAX_PAGES", 3):
            rightmove.search()
            otm_rent.search()
            full = len(calls)
            RIGHTMOVE_HEAD()
            otm_rent.fetch_head()
            fast = len(calls) - full
        self.assertEqual((full, fast), (6, 2))
        self.baseline()
        self.head = [home(2)]
        with patch.object(main, "passes_area", return_value=None) as area:
            self.watcher.fast_cycle()
            before_full = area.call_count
            self.full = [home(1)]
            self.watcher.cycle()
        self.assertEqual(before_full, 2)                            # an advert only a poll sees is looked up by both people
        self.assertEqual(area.call_count, 4)                        # a later scan that no longer sees it adds only the advert it still sees


# ---------------------------------------------------------------- controls the review found sound

class ControlTests(ReviewBase):
    def test_mid_batch_failure_restart_then_full_is_exactly_once(self):
        self.baseline()
        self.head = [home(i) for i in (2, 3, 4)]
        failed = []

        def send(l, reason):
            if users.current_chat() == OWNER and l.key == "rm:3" and not failed:
                failed.append(l.key)
                raise RuntimeError("Telegram sendMessage failed (500): fixture")
            return self._send(l, reason)

        with patch.object(telegram, "send_listing", side_effect=send):
            self.watcher.fast_cycle()
            self.assertNotIn("rm:3", main.load_seen())
            self.assertEqual(archive.work_counts(OWNER)["intent"], 1)
            self.full = [home(i) for i in (1, 2, 3, 4)]
            watch.Watcher(["rm"]).cycle()
        self.assertEqual(set(self.counts()), {(c, f"rm:{i}", "new") for c in (OWNER, FRIEND) for i in (2, 3, 4)})
        self.assertEqual(set(self.counts().values()), {1})

    def test_settings_change_preserves_a_failed_fast_intent(self):
        self.baseline()
        self.head = [home(2)]

        def send(l, reason):
            if users.current_chat() == FRIEND:
                raise RuntimeError("Telegram sendMessage failed (500): fixture")
            self._send(l, reason)

        with patch.object(telegram, "send_listing", side_effect=send):
            self.watcher.fast_cycle()
        friend = users.get(FRIEND)
        users.save_params(friend, {**friend.params, "rent": 1400})
        with users.use(friend):
            d1.query("DELETE FROM meta WHERE name = 'watch_initialized'")
        self.full = [home(1), home(2)]
        watch.Watcher(["rm"]).cycle()
        self.assertEqual(self.counts(), collections.Counter({(OWNER, "rm:2", "new"): 1, (FRIEND, "rm:2", "new"): 1}))

    def test_pending_baseline_waits_and_price_drop_has_the_same_fence(self):
        self.head = [home(1), home(2)]
        self.assertEqual(self.watcher.fast_cycle(), 0)
        self.assertEqual(self.sent, [])
        self.assertFalse(main.load_seen())
        self.baseline()
        self.head = [home(2, 1500)]
        self.watcher.fast_cycle()
        self.head = [home(2, 1400)]
        self.watcher.fast_cycle()
        self.full = [home(1), home(2, 1400)]
        watch.Watcher(["rm"]).cycle()
        watch.Watcher(["rm"]).fast_cycle()
        self.assertEqual(self.counts(), collections.Counter({
            (c, "rm:2", reason): 1 for c in (OWNER, FRIEND) for reason in ("new", "drop")}))

    def test_full_and_sweep_share_a_budget_and_fast_is_additional(self):
        self.baseline()
        self.head = [home(i) for i in range(10, 16)]
        with patch.object(config, "FAST_POLL_MAX_ALERTS", 2), patch.object(config, "MAX_ALERTS_PER_RUN", 3):
            self.assertEqual(self.watcher.fast_cycle(), 4)
            self.assertEqual(self.watcher.fast_cycle(), 0)
            self.full = [home(1)] + self.head
            self.watcher.cycle()
            self.assertEqual(len(self.sent), 10)
            self.watcher.fast_cycle()
            self.assertEqual(len(self.sent), 12)
        self.assertEqual(set(self.counts().values()), {1})

    def test_a_quiet_sweep_delivers_fast_overflow_under_the_same_lock(self):
        self.baseline()
        self.head = [home(2), home(3)]
        with patch.object(config, "FAST_POLL_MAX_ALERTS", 1):
            self.watcher.fast_cycle()
        self.assertEqual(archive.work_counts(OWNER)["intent"], 1)
        self.full = [home(1)]
        nested = []

        def step(sources, people, *a, alert_budgets=None, **kw):
            for person in people:
                sweep.judge(person, sources, 10, clock=lambda: 0, alert_budget=alert_budgets[person.chat_id])
            return {"complete": True}

        self.during_send = lambda: nested.append(self.watcher.fast_cycle())
        with patch.object(sweep, "enabled", return_value=True), patch.object(sweep, "step", side_effect=step):
            self.watcher.cycle()
        self.assertEqual(nested, [None, None])
        self.assertEqual(self.counts(), collections.Counter({(c, f"rm:{i}", "new"): 1 for c in (OWNER, FRIEND) for i in (2, 3)}))

    def test_fast_skips_a_manual_report_lock(self):
        self.baseline()
        self.full = [home(1), home(2)]
        nested = []
        with patch.object(main, "send_report", side_effect=lambda *a, **kw: nested.append(self.watcher.fast_cycle())):
            self.watcher.cycle(kind="report")
        self.assertEqual(nested, [None])

    def test_the_inherited_limitation_with_the_sweep_off_is_real_and_documented(self):
        """Not fixed here (it is the release's head-only drainage): fast-created work that leaves page 1 stays pending."""
        self.baseline()
        self.head = [home(2), home(3)]
        with patch.object(config, "FAST_POLL_MAX_ALERTS", 1):
            self.watcher.fast_cycle()
        self.head = self.full = [home(1)]
        for _ in range(3):
            self.watcher.cycle()
            self.watcher.fast_cycle()
        self.assertNotIn((OWNER, "rm:3", "new"), self.sent)
        self.assertEqual(archive.work_counts(OWNER)["intent"], 1)
        self.assertEqual([l.key for l in archive.pending(OWNER, ["rm"], main.settings_key(users.owner()), 10)], ["rm:3"])

    def test_timezone_date_only_and_future_timestamps(self):
        self.assertEqual(timing._minutes("2026-07-01T10:00:00+00:00", "2026-07-01T10:00:00+01:00"), 60)
        self.assertIsNone(timing._minutes("2026-07-01T10:00:00+00:00", "2026-07-01"))
        now = dt.datetime.now(dt.timezone.utc)
        for ts in ("2026-11-11", "2026-11-11T03:00:00", (now + dt.timedelta(minutes=1)).isoformat()):
            l = home()
            l.first_listed_at = ts
            self.assertEqual(telegram.listed_ago(l, now), "")


if __name__ == "__main__":
    unittest.main()
