"""G1 timing as the first client of the optional foundation (ALERT_TIMING=1), with the reviewer's findings fixed."""
import datetime as dt
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pisobot import archive, cache, main, optional, registry_loader, status, telegram, timing, users
from pisobot.models import Listing
from pisobot.optional import store
from pisobot.sources import PRIVATE_RENT, otm_rent, rightmove
import test_optional
from test_optional import OptionalBase, REAL_SEND_LISTING, REAL_SEND_TEXT, wait_for
from test_portal_facts import rm_property
from test_users import OWNER, home

NOW = dt.datetime.now(dt.timezone.utc)


def iso(minutes_ago):
    return (NOW - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


class TimingBase(OptionalBase):
    def setUp(self):
        super().setUp()
        timing.stats.update(retried=0, written=0, recovery_error="", recovery_more=False)
        timing._SCAN.update(state=None, after=0)
        self.baseline()
        optional.reset()
        optional.register("ALERT_TIMING", lambda: timing.FEATURE, templates=registry_loader.TIMING_TEMPLATES, empty_cycle=True)
        patcher = patch.dict(os.environ, {"ALERT_TIMING": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def rows(self):
        return store.read("SELECT * FROM alert_timing ORDER BY id")

    def settle(self, count=None):
        """Wait for the feature's worker to have written (or kept) everything handed to it."""
        wait_for(lambda: all(w["waiting"] == 0 and w["inflight"] == 0 and not w["cycle_pending"] and not w["stuck"]
                             for w in optional.health()["workers"]) and
                 (count is None or len(self.rows()) >= count))


class StatisticsTests(unittest.TestCase):
    def test_the_median_is_the_conventional_one(self):
        self.assertEqual(timing.median([10, 20]), 15)
        self.assertEqual(timing.median([10, 20, 30, 40]), 25)
        self.assertEqual(timing.median([30, 10, 20]), 20)
        self.assertIsNone(timing.median([]))

    def test_the_90th_percentile_needs_enough_alerts(self):
        rows = [{"source": "rm", "via": "head", "listed_at": iso(60 + i), "first_fetch_at": iso(50), "sent_at": iso(i)}
                for i in range(4)]
        with patch.object(timing.store, "read_checked", return_value=(rows, None)):
            row = timing.report(now=NOW)["rm"]
        self.assertEqual((row["portal_median"], row["portal_p90"]), (60, None))


class RecordingTests(TimingBase):
    def test_one_row_per_accepted_alert_with_the_archives_first_sight(self):
        self.full = [home(1, 1200), home(2, 1250)]
        self.watcher.cycle()
        self.settle(len(self.sent))
        rows = self.rows()
        self.assertEqual(len(rows), len(self.sent))
        self.assertTrue(all(r["via"] == "head" and r["reason"] == "new" for r in rows))
        self.assertTrue(any(r["first_fetch_at"] for r in rows))

    def test_a_partially_failed_cycle_keeps_the_alerts_that_were_accepted(self):
        real = telegram.send_listing

        def flaky(l, reason):
            if l.key == "rm:2" and users.current_chat() == OWNER:
                raise RuntimeError("telegram down")
            return real(l, reason)
        self.full = [home(1, 1200), home(2, 1250)]
        with patch.object(telegram, "send_listing", side_effect=flaky), self.assertRaises(Exception):
            self.watcher.cycle()
        self.settle(1)
        self.assertGreater(len(self.rows()), 0)

    def test_a_contended_database_retries_without_loss_or_duplicates_and_says_so(self):
        store.write(timing.DDL, [])
        holder = sqlite3.connect(store.path())
        holder.execute("BEGIN IMMEDIATE")
        self.addCleanup(holder.close)
        self.full = [home(1, 1200), home(2, 1250)]
        self.watcher.cycle()
        sent = len(self.sent)
        self.assertTrue(wait_for(lambda: timing.stats["retried"] >= sent))
        holder.rollback()
        self.watcher.cycle()                      # nothing new to alert; the worker also retries what was kept
        self.assertTrue(wait_for(lambda: len(self.rows()) == sent, 10))
        time.sleep(0.2)
        self.assertEqual(len(self.rows()), sent)  # no duplicates
        self.assertIn("retried since start", "\n".join(timing.status_lines()))

    def test_a_later_chunk_exception_and_whole_batch_retry_do_not_duplicate_committed_rows(self):
        items = [optional.Item(i, time.time(), OWNER, None, "new", "sent", "head",
                               (("key", f"rm:{i}"), ("source", "rm"))) for i in range(1, timing.BATCH + 3)]
        write, calls = store.write, [0]

        def partial(ddl, statements):
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError("later transaction failed")
            return write(ddl, statements)

        with patch.object(timing, "first_seen", return_value=({}, set())), patch.object(store, "write", side_effect=partial):
            with self.assertRaises(RuntimeError):
                timing.FEATURE.after_cycle(items)
            self.assertEqual(len(self.rows()), timing.BATCH)
            self.assertIsNone(timing.FEATURE.after_cycle(items))
        self.assertEqual(len(self.rows()), len(items))

    def test_failed_first_sight_read_retries_only_affected_events(self):
        items = [optional.Item(i, time.time(), OWNER, None, "new", "sent", "head",
                               (("key", f"rm:{i}"), ("source", "rm"))) for i in (1, 2)]

        def first_read(sql, params):
            if params == ("rm:1",):
                return None, sqlite3.OperationalError("private detail")
            return [{"first_seen": iso(30)}], None

        with patch.object(timing, "recover_receipts"), patch.object(store, "read_primary_checked", side_effect=first_read), \
                self.assertLogs("pisobot.timing", level="WARNING") as logs:
            self.assertEqual(timing.FEATURE.after_cycle(items), [items[0].seq])
        self.assertNotIn("private detail", "\n".join(logs.output))
        self.assertEqual([row["first_fetch_at"] for row in self.rows()], [iso(30)])
        self.assertEqual(timing.stats["retried"], 1)

        with patch.object(timing, "recover_receipts"), \
                patch.object(store, "read_primary_checked", return_value=([{"first_seen": iso(60)}], None)):
            self.assertIsNone(timing.FEATURE.after_cycle([items[0]]))
            self.assertIsNone(timing.FEATURE.after_cycle(items))
        self.assertEqual(sorted(row["first_fetch_at"] for row in self.rows()), sorted([iso(30), iso(60)]))

    def test_an_absent_first_sight_is_reported_as_unavailable(self):
        item = optional.Item(1, time.time(), OWNER, None, "new", "sent", "head",
                             (("key", "rm:missing"), ("source", "rm")))
        with patch.object(timing, "recover_receipts"), patch.object(store, "read_primary_checked", return_value=([], None)):
            self.assertIsNone(timing.FEATURE.after_cycle([item]))
        self.assertIsNone(self.rows()[0]["first_fetch_at"])
        self.assertIn("0 timed; 1 unavailable", "\n".join(timing.status_lines()))

    def test_exclusive_is_not_captured_or_persisted_and_does_not_cause_an_extra_fetch(self):
        def listing(number):
            return Listing("otmr", number, f"https://example.com/{number}", "Home", 1, PRIVATE_RENT,
                           tenure="rent", rent=1400)

        for number, exclusive in (("501", True), ("502", False), ("503", None)):
            details = {"id": number, "description": "A whole flat", "minimumAreaSqM": "50", "exclusive": exclusive}
            with self.subTest(number=number), patch.object(otm_rent.net, "page", return_value="detail") as fetch, \
                    patch.object(otm_rent, "_property_data", return_value=details):
                first, cached = listing(number), listing(number)
                self.assertTrue(otm_rent.refine(first))
                self.assertNotIn("exclusive", vars(first))
                self.assertNotIn("exclusive", cache.get("otmr_detail", number))
                self.assertTrue(otm_rent.refine(cached))
                self.assertNotIn("exclusive", vars(cached))
                fetch.assert_called_once()
        cache.save()
        with open(cache.PATH, encoding="utf-8") as saved:
            persisted = json.load(saved)["otmr_detail"]
        self.assertTrue(all("exclusive" not in entry for entry in persisted.values()))

    def test_an_existing_cached_exclusive_is_not_applied_to_a_listing(self):
        cache.put("otmr_detail", "legacy", {"description": "A whole flat", "exclusive": True})
        listing = Listing("otmr", "legacy", "https://example.com/legacy", "Home", 1, PRIVATE_RENT,
                          tenure="rent", rent=1400)
        with patch.object(otm_rent.net, "page", side_effect=AssertionError("extra detail fetch")):
            self.assertTrue(otm_rent.refine(listing))
        self.assertNotIn("exclusive", vars(listing))

    def test_event_identity_is_stable_for_retries_and_distinct_between_processes(self):
        optional.after_ack(home(1), OWNER, None, {"reason": "new", "state": "sent"})
        item = optional._items[-1]
        with patch.object(timing, "_INSTANCE", "first"):
            first = timing.row_for(item)["event_key"]
            self.assertEqual(first, timing.row_for(item)["event_key"])
        with patch.object(timing, "_INSTANCE", "second"):
            second = timing.row_for(item)["event_key"]
        self.assertNotEqual(first, second)
        self.assertNotIn("rm:1", first)

    def test_ack_hook_does_not_read_receipt_tokens(self):
        class NoReceiptRead(dict):
            def get(self, key, default=None):
                if key == "receipt_token":
                    raise AssertionError("extra receipt work on the alert path")
                return super().get(key, default)

        before = len(optional._items)
        optional.after_ack(home(1), OWNER, None, NoReceiptRead(reason="new", state="sent"))
        self.assertEqual(len(optional._items), before + 1)
        self.assertNotIn("receipt_token", dict(optional._items[-1].data))

    def test_pipeline_passes_no_extra_receipt_context_to_the_existing_hook(self):
        contexts, real = [], optional.after_ack

        def capture(listing, person, reply, context):
            contexts.append(set(context))
            return real(listing, person, reply, context)

        self.full = [home(1, 1200), home(2, 1250)]
        with patch.object(optional, "after_ack", side_effect=capture):
            self.watcher.cycle()
            self.settle()
        self.assertTrue(contexts)
        self.assertTrue(all(keys == {"reason", "state", "via"} for keys in contexts))

    def test_separate_alerts_for_the_same_listing_are_not_deduplicated(self):
        items = [optional.Item(i, time.time(), OWNER, None, "new", "sent", "head",
                               (("key", "rm:1"), ("source", "rm"))) for i in (10001, 10002)]
        timing.FEATURE.after_cycle(items)
        self.assertEqual(len(self.rows()), 2)

    def test_existing_timing_rows_survive_the_idempotency_tables_being_added(self):
        legacy = ("CREATE TABLE IF NOT EXISTS alert_timing (id INTEGER PRIMARY KEY, source TEXT NOT NULL, "
                  "reason TEXT NOT NULL, via TEXT NOT NULL, listed_at TEXT, first_fetch_at TEXT, "
                  "sent_at TEXT NOT NULL, exclusive INTEGER)")
        self.assertTrue(store.write(legacy, [
            ("INSERT INTO alert_timing (source, reason, via, sent_at) VALUES ('rm', 'new', 'head', ?)", (iso(1),))]))
        optional.after_ack(home(1), OWNER, None, {"reason": "new", "state": "sent"})
        item = optional._items[-1]
        timing.FEATURE.after_cycle([item])
        timing.FEATURE.after_cycle([item])
        self.assertEqual(len(self.rows()), 2)

    def test_receipt_ack_recovery_is_recorded_after_the_cycle_without_another_alert(self):
        self.full = [home(1, 1200), home(2, 1250)]
        original, failed, scans = archive.record_verdicts, [None], []
        real_scan = timing.recover_receipts

        def fail_first_ack(chat, rows, *args, **kwargs):
            if chat == OWNER and kwargs.get("acknowledged") and not failed[0]:
                failed[0] = rows[0][0]
                raise RuntimeError("ack interrupted")
            return original(chat, rows, *args, **kwargs)

        def scan():
            with main.run_lock():
                scans.append(len(self.sent))
            return real_scan()

        with patch.object(timing, "recover_receipts", side_effect=scan), \
                patch.object(archive, "record_verdicts", side_effect=fail_first_ack):
            with self.assertRaises(RuntimeError):
                self.watcher.cycle()
            self.settle(1)
        self.assertTrue(failed[0])
        sent = list(self.sent)
        self.assertEqual(len(sent), 2)
        optional.shutdown()
        optional.reset()
        optional.register("ALERT_TIMING", lambda: timing.FEATURE, templates=registry_loader.TIMING_TEMPLATES, empty_cycle=True)
        timing._SCAN.update(state=None, after=0)
        with patch.object(timing, "recover_receipts", side_effect=scan):
            self.watcher.cycle()
            receipt = timing._seen_for(OWNER, OWNER)[failed[0]]["delivery_intent"]
            key = timing.receipt_key(receipt)
            self.assertTrue(wait_for(lambda: store.read(
                "SELECT event_key FROM alert_timing_unmeasured WHERE event_key = ?", (key,)), 5))
            self.settle()
        self.assertEqual(self.sent, sent)
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(scans and all(count == len(sent) for count in scans))
        self.assertIn("without a verifiable timing link", "\n".join(timing.status_lines()))

    def test_a_timed_event_does_not_falsely_correlate_an_unlinked_receipt(self):
        token = "unique-receipt"
        key = timing.receipt_key(token)
        self.assertTrue(store.write(timing.DDL, [
            ("INSERT INTO alert_timing_unmeasured (event_key, source, observed_at) VALUES (?, ?, ?)", (key, "rm", iso(0)))]))
        optional.after_ack(home(1), OWNER, None, {"reason": "new", "state": "sent"})
        timing.FEATURE.after_cycle([optional._items[-1]])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(store.read("SELECT * FROM alert_timing_unmeasured WHERE event_key = ?", (key,))), 1)
        self.assertIn("may be older or overlap timed alerts", "\n".join(timing.status_lines()))

    def test_receipt_recovery_pages_through_bounded_work_without_starvation(self):
        rows = [{"rid": n, "chat_id": OWNER, "key": f"rm:{n}", "intent_id": f"token-{n}", "source": "rm"}
                for n in range(1, 4)]
        seen = {f"rm:{n}": {"delivery_intent": f"token-{n}"} for n in range(1, 4)}

        def read_page(sql, params):
            return [row for row in rows if row["rid"] > params[0]][:params[2]], None

        with patch.object(timing, "RECOVERY_PAGE", 1), patch.object(store, "read_primary_checked", side_effect=read_page), \
                patch.object(timing, "_seen_for", return_value=seen):
            timing.recover_receipts(now=NOW)
            self.assertTrue(timing.stats["recovery_more"])
            timing.recover_receipts(now=NOW)
            timing.recover_receipts(now=NOW)
        self.assertFalse(timing.stats["recovery_more"])
        self.assertEqual(len(store.read("SELECT * FROM alert_timing_unmeasured")), 3)

    def test_receipt_recovery_ignores_a_stale_receipt_from_an_older_obligation(self):
        rows = [{"rid": 1, "chat_id": OWNER, "key": "rm:1", "intent_id": "new-token", "source": "rm"}]
        seen = {"rm:1": {"delivery_intent": "old-token"}}
        with patch.object(store, "read_primary_checked", return_value=(rows, None)), \
                patch.object(timing, "_seen_for", return_value=seen):
            timing.recover_receipts(now=NOW)
        self.assertEqual(store.read("SELECT * FROM alert_timing_unmeasured"), [])

    def test_a_bad_seen_file_does_not_block_other_people_and_is_retried(self):
        rows = [{"rid": 1, "chat_id": "bad", "key": "rm:1", "intent_id": "one", "source": "rm"},
                {"rid": 2, "chat_id": OWNER, "key": "rm:2", "intent_id": "two", "source": "rm"}]
        seen = {"bad": {"rm:1": {"delivery_intent": "one"}}, OWNER: {"rm:2": {"delivery_intent": "two"}}}

        def partial(chat, owner):
            if chat == "bad":
                raise ValueError("seen file too large")
            return seen[chat]

        with patch.object(store, "read_primary_checked", return_value=(rows, None)), \
                patch.object(timing, "_seen_for", side_effect=partial), self.assertLogs("pisobot.timing", level="WARNING"):
            timing.FEATURE.after_cycle(())
        keys = {row["event_key"] for row in store.read("SELECT event_key FROM alert_timing_unmeasured")}
        self.assertEqual(keys, {timing.receipt_key("two")})
        self.assertIn("receipt recovery unavailable", "\n".join(timing.status_lines()))
        with patch.object(store, "read_primary_checked", return_value=(rows, None)), \
                patch.object(timing, "_seen_for", side_effect=lambda chat, owner: seen[chat]):
            timing.FEATURE.after_cycle(())
        keys = {row["event_key"] for row in store.read("SELECT event_key FROM alert_timing_unmeasured")}
        self.assertEqual(keys, {timing.receipt_key("one"), timing.receipt_key("two")})
        self.assertEqual(timing.stats["recovery_error"], "")

    def test_receipt_file_failure_remains_visible_until_a_complete_clean_scan(self):
        rows = [{"rid": 1, "chat_id": "bad", "key": "rm:1", "intent_id": "one", "source": "rm"},
                {"rid": 2, "chat_id": OWNER, "key": "rm:2", "intent_id": "two", "source": "rm"}]
        seen = {"bad": {"rm:1": {"delivery_intent": "one"}}, OWNER: {"rm:2": {"delivery_intent": "two"}}}
        broken = [True]

        def page(sql, params):
            return [row for row in rows if row["rid"] > params[0]][:params[2]], None

        def read_seen(chat, owner):
            if chat == "bad" and broken[0]:
                raise ValueError("unreadable seen")
            return seen[chat]

        with patch.object(timing, "RECOVERY_PAGE", 1), patch.object(store, "read_primary_checked", side_effect=page), \
                patch.object(timing, "_seen_for", side_effect=read_seen), self.assertLogs("pisobot.timing", level="WARNING"):
            timing.FEATURE.after_cycle(())
            self.assertTrue(timing.stats["recovery_error"])
            timing.FEATURE.after_cycle(())
            self.assertTrue(timing.stats["recovery_error"])
            broken[0] = False
            timing.FEATURE.after_cycle(())
            self.assertTrue(timing.stats["recovery_error"])
            timing.FEATURE.after_cycle(())
        self.assertEqual(timing.stats["recovery_error"], "")

    def test_receipt_recovery_failure_is_reported_and_retried(self):
        with patch.object(store, "read_primary_checked", return_value=(None, sqlite3.OperationalError("busy"))), \
                self.assertLogs("pisobot.timing", level="WARNING"):
            timing.FEATURE.after_cycle(())
        self.assertIn("receipt recovery unavailable", "\n".join(timing.status_lines()))
        timing.FEATURE.after_cycle(())
        self.assertEqual(timing.stats["recovery_error"], "")

    def test_dropped_rows_are_visible(self):
        optional.lost["evicted"] = 4
        self.assertIn("evicted 4", "\n".join(timing.status_lines()))

    def test_old_rows_are_deleted_by_a_write(self):
        old = (NOW - dt.timedelta(days=40)).isoformat(timespec="seconds")
        store.write(timing.DDL, [("INSERT INTO alert_timing (source, reason, via, sent_at) VALUES ('rm','new','head',?)", (old,))])
        self.full = [home(1, 1200), home(2, 1250)]
        self.watcher.cycle()
        self.settle(2)
        self.assertTrue(self.rows())
        self.assertTrue(all(r["sent_at"] > old for r in self.rows()))

    def test_the_path_comes_from_the_hook_context(self):
        optional.after_ack(home(1), OWNER, None, {"reason": "new", "state": "sent", "via": "sweep"})
        self.assertEqual(timing.row_for(optional._items[-1])["via"], "sweep")
        optional.after_ack(home(2), OWNER, None, {"reason": "new", "state": "told"})
        self.assertIsNone(timing.row_for(optional._items[-1]))

    def test_the_feature_runs_on_its_own_thread_never_the_pipeline(self):
        threads = set()
        real = timing.Feature.after_cycle
        with patch.object(timing.Feature, "after_cycle", lambda self, items: (threads.add(threading.current_thread()), real(self, items))[1]):
            self.full = [home(1, 1200), home(2, 1250)]
            self.watcher.cycle()
            self.assertTrue(wait_for(lambda: threads))
        self.assertNotIn(threading.current_thread(), threads)


class StatusTests(TimingBase):
    def test_status_reports_isolation_refusal_without_loading_the_feature(self):
        with patch.object(optional, "execution_available", test_optional.REAL_EXECUTION_AVAILABLE), \
                patch.object(status.importlib, "import_module", side_effect=AssertionError("feature imported")):
            self.assertIn(optional.ISOLATION_REFUSAL, status.status_text(now=NOW))
            self.assertIn(optional.ISOLATION_REFUSAL, "\n".join(timing.status_lines(now=NOW)))

    def test_failed_and_stuck_workers_are_visible_without_queued_events(self):
        health = {"workers": [{"flag": "ALERT_TIMING", "waiting": 0, "inflight": 0, "failed": True, "stuck": True}],
                  "lost": {}, "pending": 0, "storage": ""}
        with patch.object(optional, "health", return_value=health):
            text = "\n".join(timing.status_lines(now=NOW))
        self.assertIn("timing worker failed", text)
        self.assertIn("timing worker stuck", text)

    def test_status_import_is_disabled_and_optional_failures_preserve_the_baseline(self):
        with patch.dict(os.environ, {"ALERT_TIMING": "0"}), \
                patch.object(status.importlib, "import_module", side_effect=AssertionError("imported while off")):
            baseline = status.status_text(now=NOW)
        with patch.object(status.importlib, "import_module", side_effect=ImportError("private")), \
                self.assertLogs("pisobot.status", level="WARNING"):
            self.assertEqual(status.status_text(now=NOW), baseline)
        with patch.object(timing, "status_lines", side_effect=RuntimeError("private")), \
                self.assertLogs("pisobot.status", level="WARNING"):
            self.assertEqual(status.status_text(now=NOW), baseline)
        with patch.object(timing, "status_lines", return_value=["partial", object()]), \
                self.assertLogs("pisobot.status", level="WARNING"):
            self.assertEqual(status.status_text(now=NOW), baseline)

    def test_the_status_module_does_not_import_timing_in_a_disabled_process(self):
        code = """
import importlib.abc, os, sys
os.environ['ALERT_TIMING'] = '0'
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'pisobot.timing':
            raise ImportError('timing is unavailable')
sys.meta_path.insert(0, Block())
from pisobot import status
assert 'pisobot.timing' not in sys.modules
"""
        child = subprocess.run([sys.executable, "-c", code], cwd=test_optional.ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 0, child.stderr)

    def test_disabled_and_refused_fresh_processes_match_the_release_trace_and_state(self):
        release = "3be20c6a848171e711567f30b1cdfd580723ac93"
        try:
            snapshot = subprocess.check_output(["git", "archive", release, "pisobot", "worker/schema.sql"], cwd=test_optional.ROOT,
                                               stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            self.skipTest("pinned release is not available locally")
        code = """
import importlib, json, os, sys, types
if os.environ.get("RELEASE_FIXTURE") == "1":
    optional = types.ModuleType("pisobot.optional")
    optional.__path__ = []
    optional.reset = lambda: None
    optional.execution_available = lambda: False
    optional.runner = types.ModuleType("pisobot.optional.runner")
    optional.runner.RUNNER = types.SimpleNamespace(proc=None)
    optional.store = types.ModuleType("pisobot.optional.store")
    sys.modules["pisobot.optional"] = optional
    sys.modules["pisobot.optional.runner"] = optional.runner
    sys.modules["pisobot.optional.store"] = optional.store
from pisobot import main
assert os.path.realpath(main.__file__).startswith(os.path.realpath(os.getcwd()) + os.sep)
from test_optional import OffIsTheReleaseTests
case = OffIsTheReleaseTests("test_the_ordered_trace_and_every_persons_state_are_identical_to_the_releases_with_nothing_enabled")
try:
    case.setUp()
    if os.environ.get("RELEASE_FIXTURE") != "1":
        from pisobot import registry_loader
        importlib.reload(registry_loader)
    trace = case.scenario(False)
finally:
    case.doCleanups()
print(json.dumps(trace, sort_keys=True))
"""
        with tempfile.TemporaryDirectory() as directory:
            with tarfile.open(fileobj=io.BytesIO(snapshot)) as archive_file:
                archive_file.extractall(directory, filter="data")
            for flag in ("0", "1"):
                with self.subTest(ALERT_TIMING=flag):
                    results = []
                    for root, is_release in ((directory, True), (str(test_optional.ROOT), False)):
                        env = dict(os.environ, ALERT_TIMING=flag, PYTHONDONTWRITEBYTECODE="1",
                                   RELEASE_FIXTURE="1" if is_release else "0")
                        env["PYTHONPATH"] = os.pathsep.join((root, str(test_optional.ROOT / "tests"), os.environ.get("PYTHONPATH", "")))
                        child = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, timeout=30)
                        self.assertEqual(child.returncode, 0, child.stderr)
                        results.append(json.loads(child.stdout))
                    self.assertTrue(any(event[0] == "telegram" for event in results[1][0]))
                    self.assertEqual(results[0], results[1])
                    self.assertNotIn("optional.db", results[1][1])

    def test_status_never_creates_the_file_or_the_table(self):
        self.assertEqual(timing.status_lines(), [])
        status.status_text()
        self.assertFalse(store.exists())

    def test_a_missing_timing_table_is_valid_emptiness_and_is_not_created_by_status(self):
        self.assertTrue(store.write("CREATE TABLE IF NOT EXISTS unrelated (id INTEGER)", []))
        self.assertEqual(timing.status_lines(now=NOW), [])
        self.assertEqual(store.read("SELECT name FROM sqlite_master WHERE name LIKE 'alert_timing%'"), [])

    def test_an_acknowledged_event_before_cycle_handoff_is_visible(self):
        optional.after_ack(home(1), OWNER, None, {"reason": "new", "state": "sent"})
        self.assertEqual(optional.health()["pending"], 1)
        self.assertIn("1 awaiting cycle handoff", status.status_text(now=NOW))

    def test_read_failures_are_distinct_from_an_empty_timing_database(self):
        with patch.object(store, "read_checked", return_value=(None, sqlite3.OperationalError("disk I/O error"))), \
                self.assertLogs("pisobot.timing", level="WARNING"):
            self.assertIn("unavailable (timing database read failed)", "\n".join(timing.status_lines(now=NOW)))
        self.assertEqual(timing.status_lines(now=NOW), [])

    def test_more_than_the_read_cap_reports_a_capped_nonempty_sample(self):
        self.assertTrue(store.write(timing.DDL, []))
        with sqlite3.connect(store.path()) as db:
            db.executemany("INSERT INTO alert_timing (source, reason, via, sent_at) VALUES ('rm', 'new', 'head', ?)",
                           [(iso(5),)] * (store.MAX_ROWS + 1))
        result = timing.report(now=NOW)
        self.assertTrue(result.capped)
        self.assertEqual(result["rm"]["alerts"], timing.REPORT_ROWS)
        self.assertIn("results are capped", "\n".join(timing.status_lines(now=NOW)))

    def test_status_shows_the_delay_once_alerts_are_recorded(self):
        l = home(1)
        l.first_listed_at = iso(30)
        optional.after_ack(l, OWNER, None, {"reason": "new", "state": "sent"})
        optional.after_cycle()
        self.settle(1)
        text = "\n".join(timing.status_lines())
        self.assertIn("Alert delay", text)
        self.assertIn("Rightmove: 1 alerts", text)
        self.assertIn("coverage cannot be verified", text)

    def test_an_alert_whose_write_is_in_flight_is_shown_not_hidden(self):
        """Re-review P3: the batch has left the inbox and the write has not settled; /status must still say so."""
        entered, release, real = threading.Event(), threading.Event(), store.write
        self.addCleanup(release.set)

        def slow(*a, **kw):
            entered.set()
            release.wait(5)
            return real(*a, **kw)
        with patch.object(store, "write", side_effect=slow):
            optional.after_ack(home(1), OWNER, None, {"reason": "new", "state": "sent"})
            optional.after_cycle()
            self.assertTrue(entered.wait(5))
            text = status.status_text()
            self.assertIn("Alert delay", text)
            self.assertIn("1 being written now", text)
            release.set()
            self.settle(1)
        self.assertTrue(wait_for(lambda: "being written now" not in "\n".join(timing.status_lines())))


class ListedAgoTests(TimingBase):
    """The "Listed N min ago" line goes through the foundation's fetch-to-send seam: declared once, rendered by the foundation."""

    def card(self, listing, photo=True):
        calls = []
        with patch.object(telegram, "listing_keyboard", return_value={}), patch.object(telegram, "remember_photos"), \
                patch.object(telegram.collage, "build", return_value=b"jpeg" if photo else None), \
                patch.object(telegram, "send_text", REAL_SEND_TEXT), \
                patch.object(telegram, "_call", side_effect=lambda m, **kw: (calls.append((m, kw)), {})[1]):
            REAL_SEND_LISTING(listing)
        return calls

    def text(self, calls):
        return "".join(str(kw.get("caption") or kw.get("text") or "") for _, kw in calls)

    def declared(self):
        self.assertTrue(optional._templates)
        self.assertEqual(optional.workers(), [])

    def test_the_first_alert_of_a_fresh_advert_says_how_long_ago_it_was_listed(self):
        self.declared()
        for photo in (True, False):
            fresh = home(2, 1300)
            fresh.first_listed_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=40, seconds=20)).isoformat()
            calls = self.card(fresh, photo)
            self.assertIn("\n🕒 Listed 40 min ago", self.text(calls), (photo, calls))

    def test_only_under_three_hours_and_only_with_a_real_timestamp(self):
        self.declared()
        for minutes, shown in ((179, "Listed 2 h 59 min ago"), (180, None), (181, None), (60 * 30, None), (-30, None)):
            l = home(2, 1300)
            l.first_listed_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes, seconds=20)).isoformat()
            text = self.text(self.card(l))
            self.assertEqual("Listed" in text, shown is not None, minutes)
            if shown:
                self.assertIn(shown, text)
        l = home(2, 1300)
        self.assertNotIn("Listed", self.text(self.card(l)))                   # the portal gave no timestamp
        l.first_listed_at = "2026-11-11"
        self.assertNotIn("Listed", self.text(self.card(l)))                   # a bare date is not a listing time

    def test_the_line_never_changes_the_number_of_telegram_calls(self):
        l = home(2, 1300)
        l.first_listed_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=40, seconds=20)).isoformat()
        l.station = "S" * 3000
        with patch.dict(os.environ, {"ALERT_TIMING": "0"}):
            before = [m for m, _ in self.card(l)]
        self.declared()
        self.assertEqual([m for m, _ in self.card(l)], before)

    def test_the_feature_declares_data_and_none_of_its_code_runs_when_a_card_is_built(self):
        self.declared()
        self.assertEqual([(t.field, t.kind, t.max_seconds) for t in optional._templates], [("first_listed_at", "age", 3 * 3600)])
        fresh = home(2, 1300)
        fresh.first_listed_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5, seconds=20)).isoformat()
        tripwire = AssertionError("timing code ran on the delivery thread")
        with patch.object(timing, "row_for", side_effect=tripwire), patch.object(timing, "write_rows", side_effect=tripwire), \
                patch.object(timing.Feature, "after_cycle", side_effect=tripwire), \
                patch.object(store, "write", side_effect=tripwire), patch.object(store, "read", side_effect=tripwire), \
                patch.object(store, "read_checked", side_effect=tripwire), patch.object(store, "read_primary", side_effect=tripwire), \
                patch.object(store, "read_primary_checked", side_effect=tripwire):
            self.assertIn("Listed 5 min ago", self.text(self.card(fresh)))
        self.assertEqual(optional.workers(), [])

    def test_a_real_search_card_timestamp_reaches_the_first_alert_without_starting_a_worker(self):
        self.declared()
        listed = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=40, seconds=20)).isoformat()
        listing = rightmove._listing(rm_property(2, firstVisibleDate=listed))
        listing.monthly_cost, listing.deposit, listing.travel_minutes, listing.crime_score = 1580, 1500, 25, 30
        self.assertIn("Listed 40 min ago", self.text(self.card(listing)))
        self.assertEqual(optional.workers(), [])

    def test_the_shipped_registration_declares_the_template_before_loading_the_feature(self):
        import importlib
        optional.reset()
        with patch.object(registry_loader.importlib, "import_module", side_effect=AssertionError("feature imported")):
            importlib.reload(registry_loader)
            self.assertEqual(len(optional._templates), 1)
            fresh = home(2, 1300)
            fresh.first_listed_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=40, seconds=20)).isoformat()
            self.assertIn("Listed 40 min ago", self.text(self.card(fresh)))
        flag, loader = optional._registry[-1]
        self.assertEqual(flag, "ALERT_TIMING")
        self.assertIs(loader(), timing.FEATURE)


class OffTests(OptionalBase):
    def test_off_creates_nothing_and_shows_nothing(self):
        import importlib
        with patch.dict(os.environ, {"ALERT_TIMING": "0"}):
            importlib.reload(registry_loader)
            self.baseline()
            self.full = [home(1, 1200), home(2, 1250)]
            self.watcher.cycle()
            self.assertFalse(store.exists())
            self.assertEqual(timing.status_lines(), [])
            self.assertEqual((len(optional._templates), optional.workers()), (1, []))
            fresh = home(3, 1100)
            fresh.first_listed_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=40)).isoformat()
            self.assertEqual(optional.extend_card("card", fresh, True), "card")
            with sqlite3.connect(users.localdb.state_dir() / "pisobot.db") as db:
                self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name = 'alert_timing'").fetchall(), [])


class TimingOffTraceTests(test_optional.RealSendBase):
    scenario = test_optional.OffIsTheReleaseTests.scenario

    def setUp(self):
        super().setUp()
        optional.reset()
        self.addCleanup(optional.reset)

    def test_the_shipped_feature_disabled_has_the_releases_complete_trace(self):
        import importlib
        with patch.dict(os.environ, {"ALERT_TIMING": "0"}):
            importlib.reload(registry_loader)
            theirs, ours = self.scenario(True), self.scenario(False)
        self.assertGreater(len([event for event in ours[0] if event[0] == "telegram"]), 3)
        self.assertEqual(theirs, ours)
        self.assertEqual(optional.workers(), [])



if __name__ == "__main__":
    unittest.main()
