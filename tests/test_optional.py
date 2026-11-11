"""G0 (round 2): the optional-features foundation. No feature here: only the seams, the fault-injection probes the reviews asked for, and
a proof (by ordered trace, with mutation checks) that with nothing enabled the bot is the release."""
import contextlib
import fcntl
import json
import os
import re
import select
import sqlite3
import subprocess
import sys
import threading
import time
import types
import unittest
import dataclasses
from pathlib import Path
from unittest.mock import MagicMock, patch

from pisobot import archive, cache, collage, config, localdb, main, optional, telegram, tgtext, users, watch
from pisobot.optional import runner, store
from pisobot.sources import rightmove

from test_sweep import Market
from test_users import Base, FRIEND, OWNER, home

ROOT = Path(__file__).resolve().parents[1]
REAL_SEND_TEXT = telegram.send_text
REAL_SEND_LISTING = telegram.send_listing          # captured at import, before any test replaces it
FLAG = "PISOBOT_TEST_FEATURE"


def wait_for(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class Feature:
    """A stand-in optional feature. Records the thread every one of its methods ran on."""
    name = "probe"

    def __init__(self, after_cycle=None, work=False):
        self.threads, self.batches, self._after, self.work = set(), [], after_cycle, work

    def _here(self):
        self.threads.add(threading.current_thread())

    def after_cycle(self, items):
        self._here()
        self.batches.append(items)
        return self._after(items) if self._after else None

    def has_work(self):
        self._here()
        return self.work

    def step(self, deadline):
        self._here()

    @property
    def seqs(self):
        return [i.seq for batch in self.batches for i in batch]


class OptionalBase(Base):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(optional, "execution_available", return_value=True))
        optional.reset()
        self.addCleanup(optional.reset)
        for target in (patch.object(main.enrich, "geocode"), patch.object(main.enrich, "assign_crime_percentiles"),
                       patch.object(main, "passes_area", return_value=None), patch.object(main, "_ensure_photos"),
                       patch.object(telegram, "throttle")):
            target.start()
            self.addCleanup(target.stop)
        self.enterContext(patch.object(main, "passes_basic", side_effect=lambda l, **kw: main._cost_reject(l)))
        self.full = [home(1, 1200)]
        self.enterContext(patch.object(main.SOURCES["rm"], "fetch", side_effect=lambda: list(self.full)))
        self.sent = []
        self.enterContext(patch.object(telegram, "send_listing", side_effect=self._send))
        self.enterContext(patch.object(telegram, "send_text"))
        self.admit({"bills": 100.0})
        self.watcher = watch.Watcher(["rm"])

    def _send(self, l, reason):
        self.sent.append((users.current_chat(), l.key, reason))
        return {"result": {"message_id": 7}}

    def enable(self, feature, flag=FLAG):
        optional.register(flag, lambda: feature)
        patcher = patch.dict(os.environ, {flag: "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        return feature

    def baseline(self):
        self.watcher.cycle()
        self.sent.clear()


# ---------------------------------------------------------------- the registry: off means off

class RegistryTests(OptionalBase):
    def test_startup_registry_exposes_first_card_templates_without_loading_a_feature(self):
        code = """
import datetime, importlib.abc, importlib.util, os, sys, types
class Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "pisobot.registry_loader":
            return importlib.util.spec_from_loader(fullname, self)
    def exec_module(self, module):
        from pisobot import optional
        def loader():
            raise AssertionError("feature loaded on the delivery path")
        optional.register("TEST_TEMPLATE", loader, templates=(
            {"field": "first_listed_at", "kind": "age", "text": "Listed {} ago",
             "priority": 1, "max_seconds": 10800},))
sys.meta_path.insert(0, Finder())
os.environ["TEST_TEMPLATE"] = "1"
from pisobot import optional
optional.execution_available = lambda: True  # exercise template rendering independently of the isolation gate
stamp = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=40, seconds=20)).isoformat()
listing = types.SimpleNamespace(first_listed_at=stamp)
assert optional.extend_card("card", listing, False) == "card\\nListed 40 min ago"
assert not optional.workers()
"""
        child = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 0, child.stderr)

    def test_a_feature_whose_flag_is_not_one_is_never_loaded_and_no_thread_starts(self):
        loaded, before = [], threading.active_count()
        optional.register(FLAG, lambda: loaded.append(1))
        for value in ("0", "", "true"):
            with patch.dict(os.environ, {FLAG: value}):
                self.assertFalse(optional.enabled_any())
                optional.start()
                optional.after_cycle()
        self.assertEqual((loaded, threading.active_count()), ([], before))

    def test_the_loader_runs_on_the_features_own_thread_and_a_failing_loader_is_contained(self):
        where = []
        optional.register(FLAG, lambda: where.append(threading.current_thread()) or Feature())
        optional.register("PISOBOT_BAD", lambda: 1 / 0)
        with patch.dict(os.environ, {FLAG: "1", "PISOBOT_BAD": "1"}):
            optional.start()
            self.assertTrue(wait_for(lambda: where and any(w.failed for w in optional.workers())))
        self.assertNotIn(threading.current_thread(), where)
        self.assertEqual(sorted(w.failed for w in optional.workers()), [False, True])

    def test_importing_the_package_creates_nothing(self):
        self.assertFalse(store.exists())
        self.assertEqual(store.read("SELECT 1"), [])
        self.assertFalse(store.exists())
        self.assertIsNone(runner.RUNNER.proc)

    def test_with_nothing_enabled_the_hooks_do_nothing_and_cost_nothing(self):
        optional.after_ack(home(1), OWNER, {"result": {"message_id": 1}}, {})
        self.assertEqual(optional._items, [])
        self.assertEqual(optional.after_cycle(), 0)
        text = "<b>card</b>"
        self.assertIs(optional.extend_card(text, home(1), True), text)
        self.assertFalse(runner.RUNNER.maybe_start(popen=MagicMock(side_effect=AssertionError("started a process"))))
        self.assertEqual(optional.workers(), [])

    def test_message_id_reads_what_the_send_returned(self):
        self.assertEqual(optional.message_id({"result": {"message_id": 5}}), 5)
        for bad in (None, {}, {"result": {}}, {"result": {"message_id": "5"}}, "x", {"result": None}):
            self.assertIsNone(optional.message_id(bad))

    def test_requested_execution_is_refused_without_enforceable_isolation(self):
        from pisobot.optional import step
        loaded = []
        optional.register(FLAG, lambda: loaded.append(True), templates=(
            {"field": "first_listed_at", "kind": "age", "text": "Listed {}", "priority": 1, "max_seconds": 3600},))
        listing = home(1)
        listing.first_listed_at = stamp(60)
        with patch.object(optional, "execution_available", return_value=False), \
                patch.dict(os.environ, {FLAG: "1", "STORAGE": "local", "OPTIONAL_ALLOW_NO_LIMIT": "1"}), \
                self.assertLogs("pisobot.optional", level="ERROR"):
            self.assertFalse(optional.enabled_any())
            optional.start()
            optional._ensure_workers()
            optional.after_ack(listing, OWNER, None, {"state": "sent"})
            self.assertEqual(optional.after_cycle(), 0)
            self.assertEqual(optional.extend_card("card", listing, False), "card")
            self.assertEqual(optional.workers(), [])
            self.assertEqual(optional._items, [])
            self.assertEqual(optional.health()["refused"], optional.ISOLATION_REFUSAL)
            popen = MagicMock()
            self.assertFalse(runner.Runner().maybe_start(popen=popen))
            popen.assert_not_called()
            worker = optional.Worker(FLAG, lambda: loaded.append(True))
            worker.start()
            worker.join(5)
            self.assertTrue(worker.failed)
            with patch.object(step, "load_features", side_effect=AssertionError("feature loaded")), \
                    patch.object(step, "open_lock", side_effect=AssertionError("lock opened")):
                self.assertEqual(step.main(), 3)
            self.assertEqual(step.load_features(), [])
        self.assertEqual(loaded, [])
        self.assertFalse(store.exists())


# ---------------------------------------------------------------- P1-1: no feature code on the delivery or watcher thread

class NoFeatureCodeOnThePipelineThreadTests(OptionalBase):
    def test_nothing_a_feature_supplies_runs_on_the_calling_thread_even_when_all_of_it_blocks(self):
        """Loader, name, repr, has_work, after_cycle, step, a returned iterator: every one blocks for a long time. The cycle, the hooks and
        the card path return at once, and no feature method was ever called on this (the pipeline) thread."""
        release = threading.Event()
        self.addCleanup(release.set)
        calls = []

        class Hostile:
            def __init__(self):
                calls.append(("init", threading.current_thread()))

            @property
            def name(self):
                calls.append(("name", threading.current_thread()))
                release.wait(30)
                return "hostile"

            def __repr__(self):
                calls.append(("repr", threading.current_thread()))
                release.wait(30)
                return "x"

            def has_work(self):
                calls.append(("has_work", threading.current_thread()))
                release.wait(30)

            def step(self, deadline):
                pass

            def after_cycle(self, items):
                calls.append(("after_cycle", threading.current_thread()))
                release.wait(30)

        def loader():
            calls.append(("loader", threading.current_thread()))
            release.wait(30)
            return Hostile()
        optional.register(FLAG, loader)
        self.baseline()
        with patch.dict(os.environ, {FLAG: "1"}):
            start = time.monotonic()
            optional.start()
            self.full = [home(1, 1200), home(2, 1250)]
            self.watcher.cycle()
            optional.extend_card("<b>x</b>", home(1), True)
            runner.RUNNER.maybe_start(popen=MagicMock(side_effect=AssertionError("no work was reported")))
            self.assertLess(time.monotonic() - start, 2.0)
            self.assertGreater(len(self.sent), 0)
            self.assertTrue(wait_for(lambda: calls))
        self.assertTrue(all(thread is not threading.current_thread() for _, thread in calls))

    def test_a_feature_that_blocks_forever_is_never_entered_twice_and_loses_are_counted(self):
        release, inside, peak = threading.Event(), [0], [0]
        self.addCleanup(release.set)

        def block(items):
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
            release.wait(30)
            inside[0] -= 1
        feature = self.enable(Feature(after_cycle=block))
        for i in range(4):
            optional.after_ack(home(i), OWNER, None, {"state": "sent"})
            optional.after_cycle()
            time.sleep(0.05)
        self.assertTrue(wait_for(lambda: inside[0] == 1))
        self.assertEqual(peak[0], 1)                                       # single flight: the abandoned call is never run alongside another
        self.assertEqual(len(feature.batches), 1)
        self.assertEqual(optional.workers()[0].status()["waiting"], 3)     # the rest wait, bounded, in its own inbox
        release.set()
        self.assertTrue(wait_for(lambda: len(feature.batches) == 2))
        self.assertEqual(peak[0], 1)

    def test_the_inbox_is_bounded_and_aged_and_every_loss_is_counted(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.enable(Feature(after_cycle=lambda items: release.wait(30)))
        optional.after_ack(home(0), OWNER, None, {"state": "sent"})
        optional.after_cycle()
        self.assertTrue(wait_for(lambda: optional.workers()[0].busy_since is not None))
        for i in range(optional.BUFFER_LIMIT + 7):
            optional._items.append(types.SimpleNamespace(seq=10 + i, at=time.time()))
        optional.after_cycle()
        self.assertEqual(optional.lost["evicted"], 7)
        worker = optional.workers()[0]
        with worker.lock:
            worker.inbox[0][0] = dataclasses.replace(worker.inbox[0][0], at=0.0) if dataclasses.is_dataclass(worker.inbox[0][0]) \
                else types.SimpleNamespace(seq=1, at=0.0)
        release.set()
        self.assertTrue(wait_for(lambda: optional.lost["expired"] >= 1))

    def test_retries_are_limited_and_counted_and_a_kept_item_comes_back(self):
        feature = self.enable(Feature(after_cycle=lambda items: True))
        optional.after_ack(home(1), OWNER, None, {"state": "sent"})
        optional.after_cycle()
        self.assertTrue(wait_for(lambda: optional.lost["failed"] == 1 or len(feature.batches) >= 1))
        for _ in range(5):
            optional.after_cycle()
            time.sleep(0.05)
        self.assertTrue(wait_for(lambda: optional.lost["failed"] == 1))
        self.assertEqual(len(feature.batches), optional.MAX_ATTEMPTS)
        self.assertEqual(len({s for s in feature.seqs}), 1)               # the same item (same seq) each time

    def test_a_feature_may_keep_some_items_by_seq_and_a_raising_feature_does_not_matter(self):
        keep = {}

        def partial(items):
            keep.setdefault("first", items[0].seq)
            return [i for i in items if i.seq == keep["first"]] if len(feature.batches) == 1 else None
        feature = self.enable(Feature(after_cycle=partial))
        for i in range(3):
            optional.after_ack(home(i), OWNER, None, {"state": "sent"})
        optional.after_cycle()
        self.assertTrue(wait_for(lambda: len(feature.batches) == 1))
        optional.after_cycle()
        self.assertTrue(wait_for(lambda: len(feature.batches) == 2))
        self.assertEqual([i.seq for i in feature.batches[1]], [keep["first"]])

        optional.reset()
        bad = self.enable(Feature(after_cycle=lambda items: 1 / 0))
        optional.after_ack(home(1), OWNER, None, {"state": "sent"})
        optional.after_cycle()
        self.assertTrue(wait_for(lambda: bad.batches))


# ---------------------------------------------------------------- the hook after the acknowledgement

class AfterAckTests(OptionalBase):
    def test_the_hook_comes_after_telegram_the_receipt_and_the_db_acknowledgement(self):
        self.baseline()
        self.full = [home(1, 1200), home(2, 1250)]
        events = []
        real_save, real_verdicts, real_send = main.save_seen, archive.record_verdicts, telegram.send_listing
        with patch.object(telegram, "send_listing", side_effect=lambda l, r: (events.append(("send", l.key)), real_send(l, r))[1]), \
                patch.object(main, "save_seen", side_effect=lambda s: (events.append(("seen",)), real_save(s))[1]), \
                patch.object(archive, "record_verdicts",
                             side_effect=lambda *a, **kw: (events.append(("ack" if kw.get("acknowledged") else "books",)),
                                                           real_verdicts(*a, **kw))[1]), \
                patch.object(optional, "after_ack",
                             side_effect=lambda l, p, reply, ctx: events.append(("hook", l.key, optional.message_id(reply)))):
            self.watcher.cycle()
        sequence = [e[0] for e in events if e[0] in ("send", "seen", "ack", "hook")]
        for i in range(len(sequence) - 3):
            if sequence[i] == "send":
                self.assertEqual(sequence[i:i + 4], ["send", "seen", "ack", "hook"])
        hooks = [e for e in events if e[0] == "hook"]
        self.assertGreaterEqual(len(hooks), 2)
        self.assertTrue(all(e[2] == 7 for e in hooks))

    def test_a_hook_that_fails_in_every_way_costs_no_receipt(self):
        """Fault injection: the buffer, the clock and the item construction all explode. Alerts are recorded and acknowledged once."""
        self.baseline()
        optional.register(FLAG, lambda: Feature())
        self.full = [home(1, 1200), home(2, 1250)]
        with patch.dict(os.environ, {FLAG: "1"}), patch.object(optional, "_items", new=_Exploding()):
            self.watcher.cycle()
        self.assertGreater(len(self.sent), 0)
        self.assertIn("rm:2", json.loads((self.state / "seen.json").read_text()))
        self.sent.clear()
        with patch.dict(os.environ, {FLAG: "1"}), patch.object(optional, "_scalar", side_effect=MemoryError()):
            self.watcher.cycle()
        self.assertEqual(self.sent, [])

    def test_the_hook_inspects_nothing_hostile_and_does_no_io(self):
        self.enable(Feature())

        class Hostile:
            def __getattr__(self, name):
                raise RuntimeError(name)
        with patch("builtins.open", side_effect=AssertionError("I/O in the hook")), \
                patch.object(sqlite3, "connect", side_effect=AssertionError("DB in the hook")), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("process in the hook")):
            optional.after_ack(Hostile(), OWNER, Hostile(), {"reason": "new"})
            optional.after_ack(home(1), OWNER, {"result": {"message_id": 3}}, {"reason": "new", "state": "sent"})
        self.assertEqual(optional.lost["malformed"] + len(optional._items), 2)
        item = optional._items[-1]
        self.assertEqual((item.message_id, item.get("key"), item.state), (3, "rm:1", "sent"))

    def test_an_item_is_immutable_plain_data_not_the_live_listing(self):
        self.enable(Feature())
        l = home(1)
        optional.after_ack(l, OWNER, None, {"state": "sent"})
        item = optional._items[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            item.person = "x"
        l.rent = 99999
        self.assertNotEqual(item.get("rent"), 99999)
        self.assertNotIn(l, item.data)

    def test_feature_supplied_scalar_subclasses_are_not_evaluated_in_the_hook(self):
        self.enable(Feature())

        class SuppliedText(str):
            def __len__(self):
                raise AssertionError("feature code ran in the hook")

        listing = home(1)
        listing.url = SuppliedText("url")
        optional.after_ack(listing, OWNER, None, {"state": "sent"})
        self.assertEqual(len(optional._items), 1)
        self.assertIsNone(optional._items[0].get("url"))

    def test_the_buffer_is_bounded_and_the_overflow_is_counted(self):
        self.enable(Feature())
        for i in range(optional.BUFFER_LIMIT + 7):
            optional.after_ack(home(1), OWNER, None, {})
        self.assertEqual((len(optional._items), optional.lost["overflow"]), (optional.BUFFER_LIMIT, 7))

    def test_base_exceptions_are_not_swallowed(self):
        self.enable(Feature())
        with patch.object(optional, "_scalar", side_effect=KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
            optional.after_ack(home(1), OWNER, None, {})


class _Exploding(list):
    def append(self, item):
        raise RuntimeError("boom")

    def __len__(self):
        raise RuntimeError("boom")


# ---------------------------------------------------------------- the hook after the cycle

def _lock_is_free():
    fd = os.open(Path(main.STATE_DIR) / "run.lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


class AfterCycleTests(OptionalBase):
    def test_features_get_the_items_after_every_person_sent_and_the_run_lock_is_free_and_never_in_a_dry_run(self):
        self.baseline()
        free = []
        feature = self.enable(Feature(after_cycle=lambda items: free.append((_lock_is_free(), len(self.sent)))))
        self.full = [home(1, 1200), home(2, 1250)]
        self.watcher.cycle()
        self.assertTrue(wait_for(lambda: free))
        self.assertEqual(free, [(True, len(self.sent))])
        self.assertEqual(len(feature.batches[0]), len(self.sent))
        n = len(feature.batches)
        self.full = [home(1, 1200), home(2, 1250), home(3, 1300)]
        self.watcher.cycle(dry_run=True)
        time.sleep(0.2)
        self.assertEqual(len(feature.batches), n)
        self.assertEqual(optional._items, [])

    def test_the_prefix_of_a_failed_cycle_is_delivered_and_the_cycles_own_exception_propagates_unchanged(self):
        self.baseline()
        feature = self.enable(Feature())
        self.full = [home(1, 1200), home(2, 1250)]
        real = telegram.send_listing

        def flaky(l, reason):
            if l.key == "rm:2" and users.current_chat() == OWNER:
                raise RuntimeError("telegram down")
            return real(l, reason)
        with patch.object(telegram, "send_listing", side_effect=flaky), \
                patch.object(runner.RUNNER, "reap", side_effect=OSError("reaper broke")), \
                self.assertRaises(RuntimeError) as caught:
            self.watcher.cycle()
        self.assertNotIsInstance(caught.exception, OSError)
        self.assertTrue(wait_for(lambda: feature.batches and feature.batches[0]))

    def test_a_raising_reaper_cannot_stop_or_replace_a_cycle(self):
        with patch.object(runner.RUNNER, "reap", side_effect=OSError("poll failed")):
            self.assertIsInstance(self.watcher.cycle(), dict)
        with patch.object(optional, "after_cycle", side_effect=OSError("x")):
            self.assertIsInstance(self.watcher.cycle(), dict)

    def test_features_are_isolated_from_each_other(self):
        stuck = threading.Event()
        self.addCleanup(stuck.set)
        bad = Feature(after_cycle=lambda items: stuck.wait(30))
        bad.name = "bad"
        good = Feature()
        optional.register("PISOBOT_BAD", lambda: bad)
        optional.register(FLAG, lambda: good)
        with patch.dict(os.environ, {"PISOBOT_BAD": "1", FLAG: "1"}):
            optional.after_ack(home(1), OWNER, None, {"state": "sent"})
            optional.after_cycle()
            self.assertTrue(wait_for(lambda: good.batches))
        self.assertEqual(optional.health()["workers"][0]["flag"], "PISOBOT_BAD")

    def test_it_never_raises(self):
        self.enable(Feature())
        with patch.object(optional, "_ensure_workers", side_effect=RuntimeError("x")):
            self.assertEqual(optional.after_cycle(), 0)
            optional.start()


# ---------------------------------------------------------------- storage

class StorageTests(OptionalBase):
    DDL = "CREATE TABLE IF NOT EXISTS t (k TEXT, v TEXT)"

    def primary(self):
        path = localdb.state_dir() / "pisobot.db"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS probe (v TEXT)")
            db.execute("DELETE FROM probe")
            db.execute("INSERT INTO probe VALUES ('kept')")
        return path

    def kept(self, path):
        with sqlite3.connect(path) as db:
            return db.execute("SELECT v FROM probe").fetchall()

    def test_the_file_is_created_by_the_first_write_and_not_before(self):
        self.assertFalse(store.exists())
        self.assertTrue(store.write(self.DDL, [("INSERT INTO t VALUES (?, ?)", ("a", "1"))]))
        self.assertEqual(store.read("SELECT k, v FROM t"), [{"k": "a", "v": "1"}])

    def test_tables_and_indexes_can_be_created_in_the_optional_file(self):
        self.assertTrue(store.write("CREATE TABLE IF NOT EXISTS u (k TEXT); CREATE INDEX IF NOT EXISTS u_k ON u (k)", [("INSERT INTO u VALUES ('a')", ())]))
        self.assertEqual(store.read("SELECT k FROM u"), [{"k": "a"}])

    def test_a_contended_database_fails_fast(self):
        store.write(self.DDL, [])
        holder = sqlite3.connect(store.path())
        holder.execute("BEGIN IMMEDIATE")
        self.addCleanup(holder.close)
        start = time.monotonic()
        self.assertFalse(store.write(self.DDL, [("INSERT INTO t VALUES (?, ?)", ("a", "1"))]))
        self.assertLess(time.monotonic() - start, store.BUSY_SECONDS + 1.0)

    def test_a_runaway_statement_is_interrupted(self):
        slow = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 500000000) SELECT count(*) FROM c"
        start = time.monotonic()
        self.assertFalse(store.write(self.DDL, [(slow, ())]))
        self.assertLess(time.monotonic() - start, store.TX_SECONDS + 2.0)

    def test_a_failed_batch_leaves_nothing_half_written_and_a_big_one_is_refused_before_io(self):
        self.assertFalse(store.write(self.DDL, [("INSERT INTO t VALUES (?, ?)", ("a", "1")), ("INSERT INTO nope VALUES (1)", ())]))
        self.assertEqual(store.read("SELECT * FROM t"), [])
        optional_path = store.path()
        os.remove(optional_path) if optional_path.exists() else None
        with self.assertRaises(ValueError):
            store.write(self.DDL, [("SELECT 1", ())] * (store.MAX_STATEMENTS + 1))

    def test_the_writer_cannot_attach_and_write_the_primary_database(self):
        """The reviewer's reproduction: ATTACH + UPDATE through the public write API."""
        primary = self.primary()
        self.assertFalse(store.write("", [("ATTACH DATABASE ? AS primary_db", (str(primary),)),
                                          ("UPDATE primary_db.probe SET v='changed by optional writer'", ())]))
        self.assertEqual(self.kept(primary), [("kept",)])
        for sql in ("PRAGMA writable_schema = 1", "DETACH DATABASE main", "VACUUM INTO '/tmp/x'", "BEGIN", "SAVEPOINT s"):
            self.assertFalse(store.write("", [(sql, ())]), sql)

    def test_feature_sql_cannot_change_or_drop_the_reserved_identity(self):
        self.assertTrue(store.write(self.DDL, []))
        for sql in (f"DELETE FROM {store.IDENTITY_TABLE}", f"UPDATE {store.IDENTITY_TABLE} SET inode = '0'",
                    f"DROP TABLE {store.IDENTITY_TABLE}", f"ALTER TABLE {store.IDENTITY_TABLE} RENAME TO gone"):
            self.assertFalse(store.write("", [(sql, ())]), sql)
        self.assertTrue(store.write("", [("INSERT INTO t VALUES ('still', 'usable')", ())]))

    def test_an_existing_unmarked_optional_file_is_refused_without_modification(self):
        with sqlite3.connect(store.path()) as db:
            db.execute("CREATE TABLE legacy(v TEXT)")
            db.execute("INSERT INTO legacy VALUES ('keep')")
        self.assertFalse(store.write("", [("DELETE FROM legacy", ())]))
        self.assertIn("identity", store.last_error[0])
        with sqlite3.connect(store.path()) as db:
            self.assertEqual(db.execute("SELECT v FROM legacy").fetchall(), [("keep",)])

    def test_the_optional_file_can_never_be_a_primary_file_through_symlinks_hardlinks_or_relative_paths(self):
        primary = self.primary()
        target = store.path()
        # symlink optional.db -> the primary database
        os.symlink(primary, target)
        self.assertFalse(store.write(self.DDL, [("DELETE FROM probe", ())]))
        self.assertEqual(self.kept(primary), [("kept",)])
        self.assertEqual(store.read("SELECT * FROM probe"), [])
        os.remove(target)
        # hardlink
        os.link(primary, target)
        self.assertFalse(store.write("", [("DELETE FROM probe", ())]))
        self.assertEqual(self.kept(primary), [("kept",)])
        os.remove(target)
        # a state directory reached through a symlink and a relative path resolves to the same real directory: still fine, still not primary
        link = Path(self.tmp.name).parent / f"link-{os.getpid()}"
        os.symlink(self.tmp.name, link)
        self.addCleanup(lambda: os.path.lexists(link) and os.remove(link))
        with patch.dict(os.environ, {"PISOBOT_STATE_DIR": os.path.relpath(link)}):
            self.assertTrue(store.write(self.DDL, [("INSERT INTO t VALUES ('r', '1')", ())]))
        self.assertEqual(self.kept(primary), [("kept",)])
        with self.assertRaises(ValueError), patch.object(store, "path", return_value=Path(self.tmp.name) / "pisobot.db"):
            store.checked_path()

    def test_reads_are_select_only_bounded_and_close_their_snapshot(self):
        primary = self.primary()
        self.assertEqual(store.read_primary("SELECT v FROM probe"), [{"v": "kept"}])
        for sql in ("INSERT INTO probe VALUES ('x')", "DROP TABLE probe", "PRAGMA journal_mode = DELETE",
                    "ATTACH DATABASE ':memory:' AS m", "SELECT * FROM missing"):
            self.assertEqual(store.read_primary(sql), [], sql)
        self.assertEqual(self.kept(primary), [("kept",)])
        # the row cap
        with sqlite3.connect(primary) as db:
            db.executemany("INSERT INTO probe VALUES (?)", [("r",)] * (store.MAX_ROWS + 5))
        self.assertEqual(store.read_primary("SELECT v FROM probe"), [])
        # a runaway read is cut by the deadline
        start = time.monotonic()
        with self.assertLogs("pisobot.optional.store", level="WARNING") as logs:
            self.assertEqual(store.read_primary("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 500000000) "
                                                "SELECT count(*) AS n FROM c"), [])
        self.assertLess(time.monotonic() - start, store.READ_SECONDS + 2.0)
        self.assertEqual(store.last_primary_error[0], "query interrupted")
        self.assertIn("bounded primary read did not complete", "\n".join(logs.output))

    def test_a_read_does_not_pin_the_primary_wal(self):
        primary = self.primary()
        with sqlite3.connect(primary) as db:
            db.execute("PRAGMA journal_mode = WAL")
        store.read_primary("SELECT v FROM probe")
        with sqlite3.connect(primary) as db:
            db.execute("INSERT INTO probe VALUES ('more')")
            db.commit()
            busy, _, _ = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        self.assertEqual(busy, 0)

    def test_the_primary_reader_creates_nothing(self):
        before = sorted(p.name for p in localdb.state_dir().iterdir())
        self.assertEqual(store.read_primary("SELECT 1", person="999"), [])
        self.assertEqual(sorted(p.name for p in localdb.state_dir().iterdir()), before)


# ---------------------------------------------------------------- the supervised step

class FakeProc:
    pid = 2 ** 22 - 3

    def __init__(self, stubborn=False):
        self.killed, self.stubborn, self.waited = False, stubborn, 0

    def poll(self):
        return -9 if self.killed and not self.stubborn else None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited += 1
        if self.stubborn:
            raise subprocess.TimeoutExpired("x", timeout)


class ChildGroupTests(unittest.TestCase):
    def test_expiry_cannot_signal_after_or_during_the_final_reap(self):
        entered, release = threading.Event(), threading.Event()
        proc = types.SimpleNamespace(pid=12345, returncode=None)

        def wait(timeout):
            entered.set()
            release.wait(5)
            proc.returncode = 0

        proc.wait = wait
        group = runner.ChildGroup(proc, time.monotonic() + 60)
        with patch.object(runner.os, "waitid", return_value=object()), patch.object(runner.os, "killpg") as killpg:
            thread = threading.Thread(target=group.finish)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                group.terminate()
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(group.done.is_set())
            group.terminate()
            killpg.assert_not_called()

    def test_an_externally_reaped_leader_is_never_signalled(self):
        proc = types.SimpleNamespace(pid=12345, returncode=None)
        group = runner.ChildGroup(proc, time.monotonic() + 60)
        with patch.object(runner.os, "waitid", side_effect=ChildProcessError), \
                patch.object(runner.os, "killpg") as killpg, self.assertLogs("pisobot.optional.runner", level="ERROR"):
            group.terminate()
        self.assertTrue(group.lost)
        self.assertFalse(group.done.is_set())
        killpg.assert_not_called()


class RunnerTests(OptionalBase):
    def setUp(self):
        super().setUp()
        self.runner = runner.Runner()
        self.addCleanup(self.runner.stop)

    def launch(self, code, *, pipe=False):
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE if pipe else subprocess.DEVNULL,
                                text=True, start_new_session=True)

        def cleanup():
            group = self.runner.group
            if group is not None and group.proc is proc:
                self.runner.stop()
                group.done.wait(3)
            elif proc.returncode is None:
                with contextlib.suppress(ChildProcessError, ProcessLookupError):
                    os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    os.killpg(proc.pid, 9)
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2)
            if proc.stdout is not None:
                proc.stdout.close()
        self.addCleanup(cleanup)
        with patch.object(optional, "enabled_any", return_value=True), patch.object(self.runner, "has_work", return_value=True):
            self.assertTrue(self.runner.maybe_start(popen=lambda *args, **kwargs: proc))
        group = self.runner.group
        self.assertTrue(wait_for(lambda: group.proc is proc))
        return proc, group

    def test_nothing_is_started_without_an_enabled_feature_that_reports_queued_work(self):
        popen = MagicMock(side_effect=AssertionError("started"))
        self.assertFalse(self.runner.maybe_start(popen=popen))
        feature = self.enable(Feature(work=False))
        optional.start()
        wait_for(lambda: optional.workers()[0].feature is not None)
        self.assertFalse(self.runner.maybe_start(popen=popen))
        feature.work = True
        optional.after_ack(home(1), OWNER, None, {"state": "sent"})
        optional.after_cycle()
        self.assertTrue(wait_for(lambda: optional.workers()[0].work))
        self.launch("import time; time.sleep(60)")
        self.assertFalse(self.runner.maybe_start(popen=popen))              # one at a time
        self.runner.stop()

    def test_a_child_is_not_started_when_child_reaping_is_not_owned(self):
        popen = MagicMock()
        with patch.object(optional, "enabled_any", return_value=True), \
                patch.object(self.runner, "has_work", return_value=True), \
                patch.object(runner.signal, "getsignal", return_value=runner.signal.SIG_IGN), \
                self.assertLogs("pisobot.optional.runner", level="WARNING"):
            self.assertFalse(self.runner.maybe_start(popen=popen))
        popen.assert_not_called()

    def test_timer_start_failure_prevents_process_launch(self):
        popen = MagicMock()
        with patch.object(optional, "enabled_any", return_value=True), \
                patch.object(self.runner, "has_work", return_value=True), \
                patch.object(threading.Timer, "start", side_effect=RuntimeError("no timer")), \
                self.assertLogs("pisobot.optional.runner", level="WARNING"):
            self.assertFalse(self.runner.maybe_start(popen=popen))
            self.assertIsNone(self.runner.reap())
        popen.assert_not_called()

    def test_monitor_start_failure_does_not_launch_a_child(self):
        popen = MagicMock()
        original_start = threading.Thread.start

        def start(thread):
            if isinstance(thread, threading.Timer):
                return original_start(thread)
            raise RuntimeError("no monitor")

        with patch.object(optional, "enabled_any", return_value=True), \
                patch.object(self.runner, "has_work", return_value=True), \
                patch.object(threading.Thread, "start", autospec=True, side_effect=start), \
                self.assertLogs("pisobot.optional.runner", level="WARNING"):
            self.assertFalse(self.runner.maybe_start(popen=popen))
        popen.assert_not_called()
        self.assertIsNone(self.runner.proc)

    def test_stalled_launch_does_not_block_and_a_late_child_is_killed_after_timeout(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        caller = threading.current_thread()

        def launch(*args, **kwargs):
            self.assertIsNot(threading.current_thread(), caller)
            entered.set()
            release.wait(5)
            return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)

        with patch.object(optional, "enabled_any", return_value=True), \
                patch.object(self.runner, "has_work", return_value=True), \
                patch.object(runner, "STEP_SECONDS", 0.05), patch.object(runner, "KILL_GRACE_SECONDS", 0.05):
            started = time.monotonic()
            self.assertTrue(self.runner.maybe_start(popen=launch))
            self.assertLess(time.monotonic() - started, 1)
            self.assertTrue(entered.wait(2))
            group = self.runner.group
            self.assertTrue(group.stopping.wait(2))
            self.assertIsNone(group.proc)
            self.assertEqual(self.runner.reap(), "running")
            another = MagicMock()
            self.assertFalse(self.runner.maybe_start(popen=another))
            another.assert_not_called()
            release.set()
            self.assertTrue(group.done.wait(5))
            self.assertIsNotNone(group.proc.returncode)
            self.assertIsNone(self.runner.reap())

    def test_shutdown_retains_a_pending_launch_and_stops_its_late_child(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def launch(*args, **kwargs):
            entered.set()
            release.wait(5)
            return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)

        with patch.object(optional, "enabled_any", return_value=True), \
                patch.object(self.runner, "has_work", return_value=True), patch.object(runner, "CONFIRM_SECONDS", 0.01):
            self.assertTrue(self.runner.maybe_start(popen=launch))
            self.assertTrue(entered.wait(2))
            group = self.runner.group
            self.runner.stop()
            self.assertIs(self.runner.group, group)
            self.assertTrue(group.stopping.is_set())
            release.set()
            self.assertTrue(group.done.wait(5))
            self.assertIsNotNone(group.proc.returncode)
            self.assertIsNone(self.runner.reap())

    def test_failed_background_launch_is_reported_and_releases_ownership(self):
        with patch.object(optional, "enabled_any", return_value=True), \
                patch.object(self.runner, "has_work", return_value=True), \
                self.assertLogs("pisobot.optional.runner", level="WARNING") as logs:
            self.assertTrue(self.runner.maybe_start(popen=MagicMock(side_effect=OSError("private detail"))))
            group = self.runner.group
            self.assertTrue(group.done.wait(5))
            self.assertIsNone(self.runner.reap())
        self.assertIn("could not start (OSError)", "\n".join(logs.output))
        self.assertNotIn("private detail", "\n".join(logs.output))

    def test_an_overdue_child_is_killed_at_the_next_cycle_start_and_ownership_is_kept_until_it_is_dead(self):
        proc, group = self.launch("import time; time.sleep(60)")
        self.runner.started = time.monotonic() - runner.STEP_SECONDS - runner.KILL_GRACE_SECONDS - 1
        with patch.object(optional, "RUNNER", self.runner), patch.object(group, "signal") as signal_group:
            start = time.monotonic()
            self.baseline()
            self.assertLess(time.monotonic() - start, 5)
            self.assertTrue(group.stopping.is_set())
            self.assertTrue(wait_for(lambda: signal_group.called))
            self.assertIn(proc, self.runner.dying)

    def test_a_child_within_its_budget_is_left_alone(self):
        proc, group = self.launch("import time; time.sleep(60)")
        self.assertEqual(self.runner.reap(), "running")
        self.assertFalse(group.stopping.is_set())
        self.assertIsNone(proc.returncode)

    def test_the_timer_is_cancelled_when_the_child_is_reaped(self):
        proc, group = self.launch("pass")
        timer = self.runner.timer
        self.assertTrue(group.done.wait(5))
        self.runner.reap()
        self.assertTrue(timer.finished.is_set())
        self.assertIsNone(self.runner.timer)
        self.assertIsNotNone(proc.returncode)

    def test_shutdown_kills_the_whole_process_group_and_reaps_the_child(self):
        child, group = self.launch(
            "import subprocess,sys,time;g=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
            "print(g.pid,flush=True);time.sleep(60)", pipe=True)
        grandchild = int(child.stdout.readline())
        self.runner.stop()
        self.assertTrue(group.done.wait(5))
        self.assertIsNotNone(child.returncode)
        self.assertTrue(wait_for(lambda: not _alive(grandchild)))            # descendants died with the group

    def test_the_supervisor_never_raises(self):
        proc = MagicMock()
        group = MagicMock()
        group.done.is_set.side_effect = OSError("state failed")
        group.done.wait.side_effect = OSError("state failed")
        self.runner.proc = proc
        self.runner.group = group
        self.assertEqual(self.runner.reap(), "running")
        with patch.object(optional, "enabled_any", return_value=True), patch.object(self.runner, "has_work", return_value=True):
            self.assertFalse(self.runner.maybe_start(popen=MagicMock(side_effect=AssertionError("lost ownership"))))
        self.assertIs(self.runner.proc, proc)
        self.runner.stop()

    def test_reaping_an_unconfirmed_child_never_waits_or_starts_another(self):
        stubborn = FakeProc(stubborn=True)
        group = types.SimpleNamespace(proc=stubborn, done=threading.Event(), request_stop=MagicMock(), terminate=MagicMock())
        self.runner.proc, self.runner.group = stubborn, group
        self.runner.started = time.monotonic() - runner.STEP_SECONDS - runner.KILL_GRACE_SECONDS - 1
        with patch.object(optional, "enabled_any", return_value=True), \
                patch.object(self.runner, "has_work", return_value=True):
            self.assertEqual(self.runner.reap(), "running")
            self.assertEqual(stubborn.waited, 0)
            self.assertFalse(self.runner.maybe_start(popen=MagicMock(side_effect=AssertionError("started another child"))))
            self.assertEqual(stubborn.waited, 0)
            self.assertEqual(self.runner.dying, [stubborn])
            group.done.set()
            self.assertIsNone(self.runner.reap())

    def test_a_leaders_descendants_are_killed_even_without_another_cycle(self):
        code = ("import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)']);"
                "print(p.pid,flush=True)")
        real_killpg, signals = os.killpg, []

        def signal_owned(pgid, sig):
            os.waitid(os.P_PID, pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            signals.append((pgid, sig))
            return real_killpg(pgid, sig)

        with patch.object(runner.os, "killpg", side_effect=signal_owned):
            proc, group = self.launch(code, pipe=True)
            self.assertTrue(select.select([proc.stdout], [], [], 5)[0])
            descendant = int(proc.stdout.readline())
            self.assertTrue(wait_for(lambda: not _alive(descendant), 5))
            self.assertTrue(group.done.wait(5))
            self.assertTrue(signals)
            count = len(signals)
            group.terminate()
            self.assertEqual(len(signals), count)
            self.assertTrue(wait_for(lambda: self.runner.reap() is None, 5))

    def test_a_finished_leader_does_not_release_a_live_group(self):
        proc, group = self.launch(
            "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)']);"
            "print(p.pid,flush=True);time.sleep(.3)", pipe=True)
        descendant = int(proc.stdout.readline())
        with patch.object(group, "signal"), patch.object(optional, "enabled_any", return_value=True), \
                patch.object(self.runner, "has_work", return_value=True):
            self.assertTrue(wait_for(lambda: os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None))
            self.assertEqual(self.runner.reap(), "running")
            self.assertFalse(self.runner.maybe_start(popen=MagicMock(side_effect=AssertionError("second group"))))
            self.assertTrue(_alive(descendant))
        group.request_stop()
        self.assertTrue(group.done.wait(5))
        self.assertFalse(_alive(descendant))

    def test_a_failed_group_inspection_keeps_the_leader_owned_until_rechecked(self):
        proc, group = self.launch("import time; time.sleep(60)")
        with self.assertLogs("pisobot.optional.runner", level="WARNING"), \
                patch.object(runner, "group_usage", side_effect=OSError("inspection failed")) as inspect:
            group.request_stop()
            self.assertTrue(wait_for(lambda: inspect.called))
            self.assertTrue(wait_for(lambda: os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None))
            self.assertFalse(group.done.is_set())
            self.assertIsNone(proc.returncode)
            self.assertEqual(self.runner.reap(), "running")
        group.wake.set()
        self.assertTrue(group.done.wait(5))

    def test_an_empty_group_inspection_does_not_reap_an_unverified_leader(self):
        proc, group = self.launch("import time; time.sleep(60)")
        with self.assertLogs("pisobot.optional.runner", level="WARNING"), \
                patch.object(runner, "group_usage", return_value=(0, 0, 0)) as inspect:
            group.request_stop()
            self.assertTrue(wait_for(lambda: inspect.called))
            self.assertTrue(wait_for(lambda: os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None))
            self.assertFalse(group.done.is_set())
            self.assertIsNone(proc.returncode)
        group.wake.set()
        self.assertTrue(group.done.wait(5))

    def test_aggregate_group_memory_stops_children_that_are_individually_below_the_limit(self):
        code = """
import subprocess, sys, time
program = "import time; data=bytearray(30000000); print('ready',flush=True); time.sleep(20)"
children = [subprocess.Popen([sys.executable, '-c', program], stdout=subprocess.PIPE, text=True) for _ in range(2)]
for child in children:
    assert child.stdout.readline().strip() == 'ready'
print(*(child.pid for child in children), flush=True)
time.sleep(20)
"""
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True)

        def cleanup():
            group = self.runner.group
            if group is not None and group.proc is proc:
                self.runner.stop()
                group.done.wait(3)
            elif proc.returncode is None:
                with contextlib.suppress(ChildProcessError, ProcessLookupError):
                    os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    os.killpg(proc.pid, 9)
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2)
            proc.stdout.close()
        self.addCleanup(cleanup)
        self.assertTrue(select.select([proc.stdout], [], [], 5)[0])
        children = [int(value) for value in proc.stdout.readline().split()]
        self.assertEqual(len(children), 2)
        memory, count, live = runner.group_usage(proc.pid)
        limit = 65_000_000
        self.assertGreater(memory, limit)
        self.assertEqual(count, 3)
        self.assertEqual(live, 3)
        sizes = subprocess.check_output(["/bin/ps", "-p", ",".join(map(str, [proc.pid, *children])), "-o", "rss="], text=True)
        self.assertTrue(all(int(size) * 1024 < limit for size in sizes.split()))
        with patch.object(runner, "GROUP_MEMORY_BYTES", limit), patch.object(optional, "enabled_any", return_value=True), \
                patch.object(self.runner, "has_work", return_value=True):
            self.assertTrue(self.runner.maybe_start(popen=lambda *args, **kwargs: proc))
            self.assertTrue(self.runner.group.done.wait(5))
            self.assertIsNotNone(proc.returncode)
            self.assertTrue(wait_for(lambda: all(not _alive(pid) for pid in children), 5))

    def test_the_step_refuses_to_run_without_resource_limits_unless_opted_out(self):
        from pisobot.optional import step
        with patch.dict(os.environ, {"STORAGE": "local"}), patch("resource.setrlimit", side_effect=ValueError("no")), \
                patch.object(step, "load_features", side_effect=AssertionError("loaded features before the limits")):
            self.assertEqual(step.main(), 3)

    def test_the_step_takes_the_single_flight_lock_before_it_loads_any_feature(self):
        from pisobot.optional import step
        held = []

        def probe():
            fd = os.open(store.path().with_suffix(".lock"), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)
            return []
        with patch.dict(os.environ, {"STORAGE": "local", "OPTIONAL_ALLOW_NO_LIMIT": "1"}), patch.object(step, "load_features", side_effect=probe), \
                patch.object(step, "limit_resources", return_value="test"), patch("signal.alarm"), patch.object(threading.Thread, "start"):
            self.assertEqual(step.main(), 0)
        self.assertEqual(held, [True])

    def test_child_gives_has_work_the_remaining_budget_and_logs_a_timeout(self):
        from pisobot.optional import step
        bounds = []

        def timed_out(fn, seconds):
            bounds.append(seconds)
            return False, None

        with patch.dict(os.environ, {"STORAGE": "local"}), \
                patch.object(step, "load_features", return_value=[Feature()]), \
                patch.object(step, "limit_resources", return_value="test"), patch("signal.alarm"), \
                patch.object(threading.Thread, "start"), patch.object(step, "bounded", side_effect=timed_out), \
                self.assertLogs("pisobot.optional.step", level="WARNING") as logs:
            self.assertEqual(step.main(), 0)
        self.assertEqual(len(bounds), 1)
        self.assertGreater(bounds[0], 5)
        self.assertIn("has_work failed or exceeded its time budget", "\n".join(logs.output))

    def test_a_second_child_exits_without_loading_anything(self):
        from pisobot.optional import step
        os.makedirs(store.path().parent, exist_ok=True)
        lock = open(store.path().with_suffix(".lock"), "w")
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch.dict(os.environ, {"STORAGE": "local"}), patch.object(step, "load_features", side_effect=AssertionError("loaded")):
            self.assertEqual(step.main(), 0)

    def test_a_real_child_exits_cleanly_when_nothing_is_enabled(self):
        env = {**os.environ, "STORAGE": "local", "PISOBOT_STATE_DIR": self.tmp.name, "OPTIONAL_ALLOW_NO_LIMIT": "1"}
        env.pop(FLAG, None)
        done = subprocess.run([sys.executable, "-m", "pisobot.optional.step"], cwd=ROOT, env=env, timeout=60, capture_output=True)
        self.assertEqual(done.returncode, 0)

    def test_the_child_is_never_started_by_a_dry_run_or_inside_a_cycle(self):
        with patch.object(runner.RUNNER, "maybe_start") as start:
            self.watcher.cycle(dry_run=True)
            self.watcher.cycle()
        start.assert_not_called()                                           # only serve() starts it, after the cycle


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return "Z" not in subprocess.check_output(["ps", "-o", "stat=", "-p", str(pid)], text=True)
    except subprocess.CalledProcessError:
        return False


# ---------------------------------------------------------------- card lines

class CardTests(Base):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(optional, "execution_available", return_value=True))
        optional.reset()
        self.addCleanup(optional.reset)
        self.enterContext(patch.dict(os.environ, {"f": "1", "a": "1", "b": "1", "probe": "1"}))

    def send_card_calls(self, text, photo):
        calls = []
        with patch.object(telegram, "_call", side_effect=lambda method, **kw: (calls.append((method, kw)), {})[1]), \
                patch.object(telegram, "throttle"):
            telegram.send_card(b"jpeg" if photo else None, text, {}, "short")
        return [c[0] for c in calls]

    def test_plan_is_exactly_what_send_card_does_for_every_kind_of_card(self):
        samples = ["x", "x" * 1024, "x" * 1025, "🏠" * 512, "🏠" * 513, "&amp;" * 200, "&amp;" * 205, "<b>" + "y" * 1010 + "</b>",
                   "z" * 4096, "z" * 4097, "a\n" * 2100, "&#x1F3E0;" * 2000, "<i>" + "q\n" * 3000 + "</i>", "w" * 9000]
        for text in samples:
            for photo in (True, False):
                methods = self.send_card_calls(text, photo)
                kind, n = optional.plan(photo, text)
                self.assertEqual(n, len(methods), (text[:20], photo))
                self.assertEqual(kind, "photo-caption" if methods == ["sendPhoto"] else
                                 ("photo-then-text" if photo else "text"), (text[:20], photo))

    def test_a_line_is_added_only_while_the_plan_is_unchanged_in_every_card_kind(self):
        line = "L" * 60
        for base, photo, expect in (("x" * 900, True, True), ("x" * 990, True, False), ("x" * 1500, True, True),
                                    ("x" * 4000, False, True), ("x" * 4050, False, False), ("x" * 4050, True, False),
                                    ("x" * 1000, False, True)):
            optional.reset()
            optional.register(FLAG, lambda: Feature())
            with patch.dict(os.environ, {FLAG: "1"}):
                optional.publish_lines("f", {"rm:1": [(5, line)]})
                out = optional.extend_card(base, home(1), photo)
            self.assertEqual(out != base, expect, (len(base), photo))
            self.assertEqual(optional.plan(photo, out), optional.plan(photo, base))

    def test_the_reviewers_text_card_suffix_case_through_the_real_sender(self):
        """4,000 visible units + the 'Header unavailable' suffix is one message; a 54-unit extra line used to make it two."""
        listing = home()
        listing.station = "S"
        size = tgtext.visible(telegram.format_listing(listing))
        listing.station += "s" * (4000 - size)
        outcomes = []
        for lines in (None, [(1, "extra " + "y" * 48)]):
            optional.reset()
            optional.register(FLAG, lambda: Feature())
            calls = []
            with patch.dict(os.environ, {FLAG: "1"}):
                if lines:
                    optional.publish_lines("f", {listing.key: lines})
                with patch.object(telegram, "listing_keyboard", return_value={}), patch.object(telegram.collage, "build", return_value=None), \
                        patch.object(telegram, "remember_photos"), \
                        patch.object(telegram, "_call", side_effect=lambda m, **kw: (calls.append((m, kw)), {})[1]):
                    REAL_SEND_LISTING(listing)
            outcomes.append(len(calls))
        self.assertEqual(outcomes, [1, 1])

    def test_published_lines_are_validated_escaped_and_bounded(self):
        n = optional.publish_lines("f", {"rm:1": [(1, "<b>bold</b> & co"), (2, "bad\x00\x1b control"), ("x", "no priority"), (3, "z" * 500),
                                                   (4, 5)], 5: [(1, "key is not a string")], "": [(1, "empty key")]})
        self.assertEqual(n, 1)
        lines = optional._snapshot["rm:1"]
        self.assertEqual(lines[0][1], "bad   control")
        self.assertIn("&lt;b&gt;bold&lt;/b&gt; &amp; co", [t for _, t, _ in lines])
        self.assertEqual(len(lines), 2)

    def test_the_snapshot_is_replaced_atomically_and_features_can_replace_their_own_lines(self):
        optional.publish_lines("a", {"rm:1": [(1, "one")]})
        before = optional._snapshot
        optional.publish_lines("a", {"rm:2": [(1, "two")]})
        self.assertIn("rm:1", before)                                       # the old snapshot object was never mutated
        self.assertNotIn("rm:1", optional._snapshot)
        optional.publish_lines("b", {"rm:2": [(9, "high")]})
        self.assertEqual([t for _, t, _ in optional._snapshot["rm:2"]], ["high", "two"])

    def test_published_lines_stop_appearing_when_their_own_flag_is_disabled(self):
        optional.publish_lines("a", {"rm:1": [(2, "A")]})
        optional.publish_lines("b", {"rm:1": [(1, "B")]})
        with patch.dict(os.environ, {"a": "0"}):
            self.assertEqual(optional.extend_card("card", home(1), False), "card\nB")
        with patch.dict(os.environ, {"a": "0", "b": "0"}):
            self.assertEqual(optional.extend_card("card", home(1), False), "card")

    def test_feature_supplied_string_subclasses_never_reach_the_card_snapshot(self):
        class SuppliedKey(str):
            pass

        class SuppliedText(str):
            pass

        self.assertEqual(optional.publish_lines("f", {SuppliedKey("rm:1"): [(1, "line")]}), 0)
        self.assertEqual(optional.publish_lines("f", {"rm:1": [(1, SuppliedText("line"))]}), 0)
        self.assertEqual(optional.extend_card("card", home(1), False), "card")

    def test_the_delivery_path_calls_no_feature_code(self):
        class Tripwire:
            def __getattr__(self, name):
                raise AssertionError("feature code on the delivery thread")
        optional.register(FLAG, lambda: Tripwire())
        optional.publish_lines("probe", {"rm:1": [(1, "line")]})
        with patch.dict(os.environ, {FLAG: "1"}):
            self.assertIn("line", optional.extend_card("<b>x</b>", home(1), True))

    def test_the_card_text_is_the_releases_with_nothing_published(self):
        l = home(1)
        self.assertEqual(telegram.format_listing(l), telegram.format_listing(l))
        self.assertEqual(optional.extend_card("t", l, True), "t")


# ---------------------------------------------------------------- fetch-time facts: declared templates, rendered by the foundation at send

LISTED = {"field": "first_listed_at", "kind": "age", "text": "🕒 Listed {} ago", "priority": 5, "max_seconds": 3 * 3600}


def stamp(seconds_ago):
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)).isoformat()


def fresh(i=1, seconds_ago=40 * 60 + 20, rent=1200):
    listing = home(i, rent)
    listing.first_listed_at = stamp(seconds_ago)
    return listing


class FetchFactTests(Base):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(optional, "execution_available", return_value=True))
        optional.reset()
        self.addCleanup(optional.reset)
        flags = {"timing": "1", "f": "1"}
        flags.update({f"{prefix}{n}": "1" for prefix in ("g", "f") for n in range(10)})
        self.enterContext(patch.dict(os.environ, flags))

    def real_send(self, listing, photo):
        calls = []
        with patch.object(telegram, "listing_keyboard", return_value={}), patch.object(telegram, "remember_photos"), \
                patch.object(telegram.collage, "build", return_value=b"jpeg" if photo else None), patch.object(telegram, "throttle"), \
                patch.object(telegram, "_call", side_effect=lambda m, **kw: (calls.append((m, kw)), {})[1]):
            REAL_SEND_LISTING(listing)
        return calls

    def test_the_first_alert_carries_the_line_and_the_telegram_calls_are_the_same(self):
        for photo in (True, False):
            optional.reset()
            before = self.real_send(fresh(), photo)
            self.assertEqual(optional.publish_templates("timing", [LISTED]), 1)
            after = self.real_send(fresh(), photo)
            self.assertEqual([m for m, _ in before], [m for m, _ in after])
            text = lambda calls: calls[0][1].get("caption") or calls[0][1].get("text")
            self.assertEqual(text(after), text(before) + "\n🕒 Listed 40 min ago")

    def test_startup_declarations_work_before_the_feature_worker_has_loaded(self):
        loader = MagicMock(side_effect=AssertionError("feature loader ran on the alert path"))
        with patch.dict(os.environ, {FLAG: "1"}):
            optional.register(FLAG, loader, templates=[LISTED])
            self.assertEqual(optional.extend_card("card", fresh(), True), "card\n🕒 Listed 40 min ago")
            self.assertEqual(optional.workers(), [])
            loader.assert_not_called()
            with patch.dict(os.environ, {FLAG: "0"}):
                self.assertEqual(optional.extend_card("card", fresh(), True), "card")

    def test_the_age_is_read_at_send_and_only_inside_the_declared_window(self):
        optional.publish_templates("timing", [LISTED])
        for seconds, expect in ((5, "under 1 min"), (61, "1 min"), (59 * 60, "59 min"), (3600, "1 h"), (2 * 3600 + 5 * 60 + 3, "2 h 5 min"),
                                (3 * 3600 - 2, "2 h 59 min"), (3 * 3600 + 1, None), (86400, None), (-30, "under 1 min"), (-600, None)):
            out = optional.extend_card("card", fresh(1, seconds), True)
            self.assertEqual(out, "card" if expect is None else f"card\n🕒 Listed {expect} ago", seconds)
        self.assertEqual(optional.age_words(49 * 3600), "2 days")

    def test_a_missing_naive_or_junk_fact_gives_no_line_and_never_raises(self):
        optional.publish_templates("timing", [LISTED])

        class Str(str):
            def endswith(self, *a):
                raise AssertionError("code supplied with the data ran on the delivery thread")
            __len__ = endswith

        for value in (None, "", "2026-11-11", "2026-11-11T10:00:00", "junk" * 5, "9" * 500, 5, 1.5, True, b"2026", ["x"], {"a": 1},
                      Str(stamp(60)), "\ud800" * 12, float("nan")):
            listing = home(1)
            listing.first_listed_at = value
            self.assertEqual(optional.extend_card("card", listing, True), "card", repr(value)[:30])
        self.assertEqual(optional.extend_card("card", object(), True), "card")
        self.assertEqual(optional.extend_card("card", None, False), "card")
        self.assertEqual(optional.epoch("2026-11-09T14:03:12Z"), optional.epoch("2026-11-09T14:03:12+00:00"))

    def test_a_template_does_not_read_the_listing_key_when_no_lines_are_published(self):
        optional.publish_templates("timing", [LISTED])

        class NoKey(type(home())):
            @property
            def key(self):
                raise AssertionError("an unrelated property ran")

        listing = NoKey("rm", "1", "u", "Home", 1, "Private rent")
        listing.first_listed_at = stamp(40 * 60 + 20)
        self.assertEqual(optional.extend_card("card", listing, True), "card\n🕒 Listed 40 min ago")

    def test_only_the_instance_dictionary_is_read_never_a_property_or_a_feature_object(self):
        optional.publish_templates("f", [dict(LISTED, field="trap"), dict(LISTED, field="key", kind="value", text="k {}")])

        class Trapped(type(home())):
            @property
            def trap(self):
                raise AssertionError("a property ran on the delivery thread")

            def __getattr__(self, name):
                raise AssertionError("attribute lookup ran on the delivery thread")
        listing = Trapped("rm", "1", "u", "Home", 1, "Private rent")
        self.assertEqual(optional.extend_card("card", listing, True), "card")       # `key` is a property: not read either

    def test_templates_are_validated_and_are_plain_immutable_data(self):
        bad = [dict(LISTED, text="no placeholder"), dict(LISTED, text="{} and {}"), dict(LISTED, text="{0.__class__} {}"),
               dict(LISTED, text="{} }"), dict(LISTED, kind="eval"), dict(LISTED, field="__dict__"), dict(LISTED, field="a.b"),
               dict(LISTED, priority="5"), dict(LISTED, max_seconds=0), dict(LISTED, max_seconds=10 ** 9), dict(LISTED, text="x{}" * 1 + "y" * 200),
               {"field": "rent"}, None, 7]
        self.assertEqual(optional.publish_templates("f", bad), 0)
        self.assertEqual(optional._templates, ())
        self.assertEqual(optional.publish_templates("f", [dict(LISTED, text="<b>{}</b> & \x00co")] + [LISTED] * 9), optional.TEMPLATES_PER_FEATURE)
        first = optional._templates[0]
        self.assertEqual(first.text, "&lt;b&gt;{}&lt;/b&gt; &amp;  co")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.text = "changed"
        before = optional._templates
        optional.publish_templates("f", [LISTED])                                 # a feature replaces its own set; the old tuple is untouched
        self.assertEqual((len(before), len(optional._templates)), (optional.TEMPLATES_PER_FEATURE, 1))
        for n in range(10):
            optional.publish_templates(f"g{n}", [LISTED] * 5)
        with patch.object(optional, "render", wraps=optional.render) as render:
            optional.extend_card("card", fresh(), True)
        self.assertEqual(render.call_count, optional.TEMPLATES_MAX)

    def test_a_feature_supplied_kind_subclass_is_not_retained(self):
        class SuppliedKind(str):
            pass

        self.assertEqual(optional.publish_templates("timing", [dict(LISTED, kind=SuppliedKind("age"))]), 0)
        self.assertEqual(optional.extend_card("card", fresh(), True), "card")

    def test_optional_work_is_bounded_for_large_cards_and_many_publishers(self):
        for n in range(optional.PUBLISHERS_MAX + 1):
            optional.publish_templates(f"feature_{n}", [LISTED] * optional.TEMPLATES_PER_FEATURE)
        self.assertEqual(len(optional._template_sets), optional.PUBLISHERS_MAX)
        self.assertGreater(optional.lost["malformed"], 0)
        with patch.object(optional, "plan", side_effect=AssertionError("large card was planned")):
            text = "x" * (optional.MAX_CARD_CHARS + 1)
            self.assertIs(optional.extend_card(text, fresh(), True), text)

    def test_a_value_template_escapes_the_fact_and_refuses_anything_that_is_not_a_short_scalar(self):
        optional.publish_templates("f", [{"field": "agent", "kind": "value", "text": "Agent: {}", "priority": 1}])
        for value, expect in (("A <b>& B", "Agent: A &lt;b&gt;&amp; B"), (12, "Agent: 12"), (62.5, "Agent: 62.5"), ("x" * 61, None),
                              ("", None), (None, None), (True, None), (float("inf"), None), ("a\nb", "Agent: a b")):
            listing = home(1)
            listing.agent = value
            self.assertEqual(optional.extend_card("card", listing, False), "card" if expect is None else "card\n" + expect, repr(value))

    def test_an_oversized_value_is_rejected_before_it_is_processed(self):
        optional.publish_templates("f", [{"field": "agent", "kind": "value", "text": "Agent: {}", "priority": 1}])
        listing = home(1)
        listing.agent = "x" * 1_000_000
        with patch.object(optional.re, "sub", side_effect=AssertionError("processed an unbounded fact")):
            self.assertEqual(optional.extend_card("card", listing, False), "card")

    def test_a_rendered_line_never_changes_the_call_plan_at_any_boundary(self):
        """Defect class (d): the same planning as published lines, against the final text and the real media outcome, through the real sender."""
        optional.publish_templates("timing", [LISTED])
        extra = tgtext.visible("\n🕒 Listed 40 min ago")
        for photo in (True, False):
            listing = fresh()
            listing.station = "S"
            optional.reset()
            base = self.real_send(listing, photo)
            sent = base[0][1].get("caption") or base[0][1].get("text")
            size = len(sent.encode("utf-16-le")) // 2 if photo else tgtext.visible(sent)   # each limit as the release measures it
            limit = tgtext.CAPTION if photo else tgtext.MESSAGE
            for room in range(extra + 3, -3, -1):                                  # from "fits easily" to "the release text is already over"
                target = limit - room
                if target < size:
                    continue
                listing = fresh()
                listing.station = "S" + "s" * (target - size)
                optional.reset()
                without = [m for m, _ in self.real_send(listing, photo)]
                optional.publish_templates("timing", [LISTED])
                with_line = self.real_send(listing, photo)
                self.assertEqual(without, [m for m, _ in with_line], (photo, room))
                joined = "".join(str(kw.get("caption") or kw.get("text") or "") for _, kw in with_line)
                self.assertEqual("Listed 40 min ago" in joined, room >= extra or room < 0, (photo, room))     # already two calls: stays two

    def test_rendering_does_no_io_takes_no_lock_and_calls_nothing_a_feature_supplied(self):
        class Tripwire:
            def __getattr__(self, name):
                raise AssertionError("feature code on the delivery thread")
        optional.register(FLAG, lambda: Tripwire())
        optional.publish_templates("timing", [LISTED])
        with patch.dict(os.environ, {FLAG: "1"}), patch("builtins.open", side_effect=AssertionError("file")), \
                patch.object(sqlite3, "connect", side_effect=AssertionError("db")), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("process")), optional._lock:      # the lock is HELD: not needed
            self.assertIn("Listed 40 min ago", optional.extend_card("card", fresh(), True))
        self.assertEqual(optional.workers(), [])                                   # rendering started nothing

    def test_a_broken_renderer_or_planner_leaves_the_release_card(self):
        optional.publish_templates("timing", [LISTED])
        optional.publish_lines("f", {"rm:1": [(9, "published")]})
        for target in ("render", "plan", "age_words", "epoch"):
            with patch.object(optional, target, side_effect=RuntimeError("boom")):
                out = optional.extend_card("card", fresh(), True)
            self.assertIn(out, ("card", "card\npublished"), target)
        with patch.object(optional.time, "time", side_effect=MemoryError()):
            self.assertEqual(optional.extend_card("card", fresh(), True), "card")

    def test_published_lines_and_rendered_lines_share_one_priority_order_and_duplicates_collapse(self):
        optional.publish_templates("timing", [LISTED, LISTED])
        optional.publish_lines("f", {"rm:1": [(9, "high"), (1, "low")]})
        self.assertEqual(optional.extend_card("card", fresh(), False), "card\nhigh\n🕒 Listed 40 min ago\nlow")
        tight = "x" * (tgtext.CAPTION - 6)                                          # room for "high" only: the rest is left out, not split
        self.assertEqual(optional.extend_card(tight, fresh(), True), tight + "\nhigh")

    def test_twenty_templates_on_a_card_cost_well_under_a_millisecond_each(self):
        for n in range(4):
            optional.publish_templates(f"f{n}", [dict(LISTED, text=f"{n}{k} {{}}") for k in range(5)])
        listing, started = fresh(), time.perf_counter()
        for _ in range(50):
            optional.extend_card("x" * 500, listing, True)
        self.assertLess((time.perf_counter() - started) / 50, 0.02)                # the whole card, 20 lines offered: bounded work


# ---------------------------------------------------------------- round 3: the re-review's findings, one regression test each

CHILD = """
import os, sys, time
from pisobot import optional
from pisobot.optional import step
optional.execution_available = lambda: True  # exercise the existing child watchdog in isolation
class F:
    def has_work(self): return True
    def step(self, deadline):
        {body}
step.load_features = lambda: [F()]
{setup}
sys.exit(step.main())
"""


class RereviewTests(OptionalBase):
    def primary(self):
        path = localdb.state_dir() / "pisobot.db"
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS probe (v TEXT)")
            db.execute("INSERT INTO probe VALUES ('before')")
            db.commit()
        return path

    def value(self, path):
        with contextlib.closing(sqlite3.connect(path)) as db:
            return db.execute("SELECT v FROM probe").fetchone()[0]

    def child(self, body, setup="", **env):
        code = CHILD.format(body=body, setup=setup)
        full = {**os.environ, "STORAGE": "local", "PISOBOT_STATE_DIR": self.tmp.name, **env}
        full.pop("OPTIONAL_ALLOW_NO_LIMIT", None)
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=full, timeout=60, capture_output=True)

    # P1: the optional database path is verified AFTER the open, so a swap between the check and the open is seen
    def test_a_link_swapped_in_between_the_check_and_the_open_is_refused_and_the_primary_is_untouched(self):
        for link in (os.symlink, os.link):
            primary, target, real_connect, swapped = self.primary(), store.path(), sqlite3.connect, []
            with contextlib.suppress(FileNotFoundError):
                os.unlink(target)

            def race(path, *a, **kw):
                if Path(path) == target and not swapped:
                    os.unlink(target)
                    link(primary, target)
                    swapped.append(True)
                return real_connect(path, *a, **kw)
            with patch.object(store.sqlite3, "connect", side_effect=race):
                self.assertFalse(store.write("", [("UPDATE probe SET v='after'", ())]), link.__name__)
            self.assertTrue(swapped)
            self.assertEqual(self.value(primary), "before", link.__name__)
            self.assertIn("optional.db", store.last_error[0])
            self.assertEqual(optional.health()["storage"], store.last_error[0])
            self.assertEqual(store.read("SELECT v FROM probe"), [], link.__name__)      # the reader does not follow it either
            os.unlink(target)
            with contextlib.closing(real_connect(primary)) as db:
                db.execute("DELETE FROM probe")
                db.commit()
        self.assertTrue(store.write("CREATE TABLE IF NOT EXISTS t (k TEXT)", [("INSERT INTO t VALUES ('x')", ())]))   # the honest path works
        self.assertEqual(store.last_error[0], "")

    def test_a_hard_link_swapped_during_open_and_then_restored_does_not_write_the_primary(self):
        primary, target, real_connect = self.primary(), store.path(), sqlite3.connect
        original = target.with_name("original-optional.db")

        def race(path, *args, **kwargs):
            if Path(path) != target:
                return real_connect(path, *args, **kwargs)
            os.rename(target, original)
            os.link(primary, target)
            opened = real_connect(path, *args, **kwargs)
            os.unlink(target)
            os.rename(original, target)
            return opened

        with patch.object(store.sqlite3, "connect", side_effect=race):
            written = store.write("", [("UPDATE probe SET v='after'", ())])
        self.assertFalse(written, (self.value(primary), store.last_error[0]))
        self.assertEqual(self.value(primary), "before")

    def test_a_state_directory_others_can_write_is_refused_before_anything_is_created(self):
        os.chmod(localdb.state_dir(), 0o777)
        self.addCleanup(os.chmod, localdb.state_dir(), 0o700)
        self.assertFalse(store.write("CREATE TABLE IF NOT EXISTS t (k TEXT)", []))
        self.assertFalse(store.exists())
        self.assertIn("writable", store.last_error[0])
        os.chmod(localdb.state_dir(), 0o755)
        self.assertTrue(store.write("CREATE TABLE IF NOT EXISTS t (k TEXT)", []))

    # P1: the child's lock file is opened without following links and without truncating
    def test_the_lock_file_never_follows_a_link_and_never_truncates(self):
        from pisobot.optional import step
        lock = store.path().with_suffix(".lock")
        for link in (os.symlink, os.link):
            victim = Path(self.tmp.name) / f"victim-{link.__name__}"
            victim.write_text("primary bytes")
            link(victim, lock)
            with patch.dict(os.environ, {"STORAGE": "local"}), patch.object(step, "load_features", side_effect=AssertionError("loaded")), \
                    patch.object(step, "limit_resources", side_effect=AssertionError("went on")):
                self.assertEqual(step.main(), 5, link.__name__)
            self.assertEqual(victim.read_text(), "primary bytes", link.__name__)
            os.unlink(lock)
        lock.write_text("left by an older version")                               # a plain file is used as it is, not emptied
        handle = step.open_lock(lock)
        handle.close()
        self.assertEqual(lock.read_text(), "left by an older version")
        os.unlink(lock)
        os.mkfifo(lock)
        with self.assertRaises((ValueError, OSError)):
            step.open_lock(lock)

    # P2: shutdown keeps ownership of a child it could not confirm dead
    def test_shutdown_keeps_a_child_whose_death_is_not_confirmed_and_later_reaps_retry_it(self):
        stubborn, r = FakeProc(stubborn=True), runner.Runner()
        group = types.SimpleNamespace(proc=stubborn, done=threading.Event(), request_stop=MagicMock(), terminate=MagicMock())
        r.proc, r.group, r.started = stubborn, group, time.monotonic()
        with patch.object(runner, "CONFIRM_SECONDS", 0.01):
            r.stop()
            self.assertIsNone(r.proc)
            self.assertEqual(r.dying, [stubborn])                                 # still owned
            r.stop()
            self.assertEqual(r.dying, [stubborn])                                 # a second shutdown does not forget it either
            r.reap()
            self.assertEqual(r.dying, [stubborn])
            group.done.set()
            r.reap()
        self.assertEqual(r.dying, [])

    def test_the_lock_refuses_an_untrusted_state_directory_before_opening(self):
        from pisobot.optional import step
        lock = store.path().with_suffix(".lock")
        os.chmod(localdb.state_dir(), 0o777)
        self.addCleanup(os.chmod, localdb.state_dir(), 0o700)
        with patch.dict(os.environ, {"STORAGE": "local"}), patch.object(step, "load_features") as load:
            self.assertEqual(step.main(), 5)
        load.assert_not_called()
        self.assertFalse(lock.exists())
        os.chmod(localdb.state_dir(), 0o700)
        info = types.SimpleNamespace(st_uid=os.getuid() + 1, st_mode=0o700)
        with patch.object(store.os, "stat", return_value=info):
            with self.assertRaises(ValueError):
                step.open_lock(lock)
        self.assertFalse(lock.exists())

    # P2: the child runs with limits on this platform too, without the opt-out
    def test_a_real_child_runs_a_step_under_limits_without_the_opt_out_on_this_platform(self):
        marker = Path(self.tmp.name) / "ran"
        done = self.child(f"open({str(marker)!r}, 'w').write('x')")
        self.assertEqual(done.returncode, 0, done.stderr[-300:])
        self.assertTrue(marker.exists())
        self.assertIn(b"optional step limits: cpu, memory by", done.stderr)

    def test_a_real_child_over_its_memory_ceiling_does_not_survive(self):
        marker = Path(self.tmp.name) / "survived"
        body = (f"b = bytearray(80_000_000)\n        for i in range(0, len(b), 4096): b[i] = 1\n        time.sleep(3)\n"
                f"        open({str(marker)!r}, 'w').write('x')")
        done = self.child(body, setup="step.MEMORY_LIMIT_BYTES = 50_000_000")
        self.assertFalse(marker.exists(), done.stderr[-300:])
        if sys.platform == "darwin":
            self.assertEqual(done.returncode, 6)                                  # stopped by the resident-memory watchdog

    def test_a_child_stops_if_memory_supervision_fails(self):
        marker = Path(self.tmp.name) / "survived"
        setup = """reads = [0]
original = step.resident_bytes
def failing_monitor():
    reads[0] += 1
    if reads[0] > 1:
        raise OSError("monitor failed")
    return original()
step.resident_bytes = failing_monitor"""
        done = self.child(f"time.sleep(3)\n        open({str(marker)!r}, 'w').write('x')", setup=setup)
        self.assertEqual(done.returncode, 7, done.stderr[-300:])
        self.assertIn(b"memory supervision failed", done.stderr)
        self.assertFalse(marker.exists())

    # P3: a feature whose loader failed keeps nothing
    def test_a_failed_loader_keeps_no_items_and_every_one_is_counted(self):
        gate = threading.Event()

        def loader():
            gate.wait(5)
            raise RuntimeError("cannot load")
        self.baseline()
        optional.register(FLAG, loader)
        self.full.append(home(2, 1300))
        with patch.dict(os.environ, {FLAG: "1"}):
            self.watcher.cycle()                                                  # offered while the loader is still running
            worker = optional.workers()[0]
            n = worker.status()["waiting"]
            self.assertEqual(n, len(self.sent))                                   # one per person alerted
            self.assertGreater(n, 0)
            gate.set()
            self.assertTrue(wait_for(lambda: worker.failed and not worker.is_alive()))
            self.assertEqual((worker.status()["waiting"], optional.lost["failed"]), (0, n))
            self.full.append(home(3, 1100))
            self.watcher.cycle()                                                  # offered to a dead worker
            self.assertEqual((worker.status()["waiting"], optional.lost["failed"]), (0, len(self.sent)))
            self.assertGreater(len(self.sent), n)
            with patch.object(optional.time, "time", return_value=time.time() + 10 * 3600):
                self.assertEqual(optional.health()["workers"][0]["waiting"], 0)   # nothing left to go stale

    def test_a_loader_failure_cannot_race_an_offer_into_a_dead_inbox(self):
        loading, fail, entering, proceed = (threading.Event() for _ in range(4))
        self.addCleanup(fail.set)
        self.addCleanup(proceed.set)
        lock = threading.Lock()

        class GateLock:
            def acquire(self, blocking=True):
                if threading.current_thread().name == "test-offer":
                    entering.set()
                    proceed.wait(5)
                return lock.acquire(blocking=blocking)

            def release(self):
                lock.release()

            def __enter__(self):
                self.acquire()

            def __exit__(self, *args):
                self.release()

        def loader():
            loading.set()
            fail.wait(5)
            raise RuntimeError("loader failed")

        worker = optional.Worker(FLAG, loader)
        worker.lock = GateLock()
        item = optional.Item(1, time.time(), OWNER, None, "new", "sent", "head", ())
        worker.start()
        self.assertTrue(loading.wait(5))
        offer = threading.Thread(target=worker.offer, args=([item],), name="test-offer", daemon=True)
        offer.start()
        self.assertTrue(entering.wait(5))
        fail.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        proceed.set()
        offer.join(5)
        self.assertFalse(offer.is_alive())
        self.assertEqual((worker.status()["waiting"], optional.lost["failed"]), (0, 1))

    def test_a_busy_worker_inbox_does_not_block_the_scheduler_and_counts_the_loss(self):
        worker = optional.Worker(FLAG, lambda: Feature())
        item = optional.Item(1, time.time(), OWNER, None, "new", "sent", "head", ())
        with worker.lock:
            worker.offer([item])
        self.assertEqual((worker.status()["waiting"], optional.lost["contended"]), (0, 1))

    def test_the_cycle_handoff_does_not_wait_for_snapshot_publication(self):
        self.enable(Feature())
        optional.after_ack(home(1), OWNER, None, {"reason": "new", "state": "sent"})
        done = threading.Event()
        with optional._lock:
            handoff = threading.Thread(target=lambda: (optional.after_cycle(), done.set()), daemon=True)
            handoff.start()
            self.assertTrue(done.wait(1), "handoff waited for a feature publishing a snapshot")
        handoff.join(5)

    def test_status_cannot_observe_a_batch_between_inbox_and_inflight(self):
        worker = optional.Worker(FLAG, lambda: Feature())
        item = optional.Item(1, time.time(), OWNER, None, "new", "sent", "head", ())
        worker.inbox.append([item, 0])
        started, done, observed = threading.Event(), threading.Event(), []

        def read_status():
            started.set()
            observed.append(worker.status())
            done.set()

        with worker.lock:
            worker.inbox.clear()
            reader = threading.Thread(target=read_status, daemon=True)
            reader.start()
            self.assertTrue(started.wait(5))
            self.assertFalse(done.wait(0.1))
            worker.inflight = 1
        reader.join(5)
        self.assertEqual((observed[0]["waiting"], observed[0]["inflight"]), (0, 1))

    # P3: what a feature is working on right now is visible
    def test_items_taken_but_not_yet_settled_are_reported_in_flight(self):
        entered, release = threading.Event(), threading.Event()
        feature = self.enable(Feature(after_cycle=lambda items: (entered.set(), release.wait(5), None)[2]))
        self.addCleanup(release.set)
        self.baseline()
        self.full.append(home(2, 1300))
        self.watcher.cycle()
        self.assertTrue(entered.wait(5))
        status = optional.health()["workers"][0]
        self.assertEqual((status["waiting"], status["inflight"]), (0, len(self.sent)))     # one per person alerted
        release.set()
        self.assertTrue(wait_for(lambda: optional.health()["workers"][0]["inflight"] == 0))
        kept = self.enable(Feature(after_cycle=lambda items: True), flag=FLAG + "_2")
        self.full.append(home(3, 1100))
        self.watcher.cycle()
        self.assertTrue(wait_for(lambda: kept.batches))
        self.assertTrue(wait_for(lambda: sum(optional.workers()[1].status()[k] for k in ("waiting", "inflight")) == len(kept.batches[0])))   # never in neither
        self.assertTrue(feature.batches)

    def test_a_blocked_has_work_is_reported_as_stuck(self):
        entered, release = threading.Event(), threading.Event()

        class SlowHasWork:
            def has_work(self):
                entered.set()
                release.wait(5)
                return False

            def step(self, deadline):
                pass

        worker = optional.Worker(FLAG, SlowHasWork)
        worker.start()
        self.assertTrue(entered.wait(5))
        with patch.object(optional, "STUCK_SECONDS", 0.01):
            time.sleep(0.02)
            self.assertTrue(worker.status()["stuck"])
        release.set()
        worker.stop()
        worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_a_blocked_loader_is_reported_as_stuck(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def loader():
            entered.set()
            release.wait(5)
            return Feature()

        worker = optional.Worker(FLAG, loader)
        worker.start()
        self.assertTrue(entered.wait(5))
        with patch.object(optional, "STUCK_SECONDS", 0):
            self.assertTrue(worker.status()["stuck"])
        release.set()
        worker.stop()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(worker.status()["stuck"])

    def test_a_has_work_exception_is_logged(self):
        class BadWork:
            def has_work(self):
                raise RuntimeError("private detail")

            def step(self, deadline):
                pass

        worker = optional.Worker(FLAG, BadWork)
        worker.feature = BadWork()
        with self.assertLogs("pisobot.optional", level="WARNING") as logs:
            worker._refresh()
        self.assertFalse(worker.work)
        self.assertIn("has_work failed (RuntimeError)", "\n".join(logs.output))
        self.assertNotIn("private detail", "\n".join(logs.output))

    def test_a_blocked_result_iterator_remains_in_flight_and_stuck(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def returned(items):
            def values():
                entered.set()
                release.wait(5)
                yield items[0].seq
            return values()

        self.enable(Feature(after_cycle=returned))
        optional.after_ack(home(1), OWNER, None, {"state": "sent"})
        optional.after_cycle()
        self.assertTrue(entered.wait(5))
        worker = optional.workers()[0]
        with patch.object(optional, "STUCK_SECONDS", 0.01):
            time.sleep(0.02)
            self.assertTrue(worker.status()["stuck"])
            self.assertEqual(worker.status()["inflight"], 1)
        release.set()
        self.assertTrue(wait_for(lambda: not worker.status()["stuck"]))

    def test_result_iteration_is_bounded_and_retried(self):
        consumed = []

        def endless(items):
            while True:
                consumed.append(1)
                yield items[0].seq

        worker = optional.Worker(FLAG, lambda: Feature())
        worker.feature = Feature(after_cycle=endless)
        item = optional.Item(1, time.time(), OWNER, None, "new", "sent", "head", ())
        worker._deliver([[item, 0]])
        self.assertEqual(len(consumed), optional.BUFFER_LIMIT + 1)
        self.assertEqual(worker.status()["waiting"], 1)

    def test_shutdown_drains_and_joins_accepted_work(self):
        feature = self.enable(Feature(after_cycle=lambda items: time.sleep(0.1)))
        optional.after_ack(home(1), OWNER, None, {"state": "sent"})
        optional.after_cycle()
        worker = optional.workers()[0]
        optional.shutdown()
        self.assertEqual(len(feature.batches), 1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(optional.lost["shutdown"], 0)

    def test_shutdown_deadline_counts_queued_loss_and_logs_unsettled_work(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.enable(Feature(after_cycle=lambda items: (entered.set(), release.wait(5), None)[2]))
        optional.after_ack(home(1), OWNER, None, {"state": "sent"})
        optional.after_cycle()
        self.assertTrue(entered.wait(5))
        optional.after_ack(home(2), OWNER, None, {"state": "sent"})
        optional.after_cycle()
        with patch.object(optional, "DRAIN_SECONDS", 0.05), self.assertLogs("pisobot.optional", level="WARNING") as logs:
            start = time.monotonic()
            optional.shutdown()
            self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(optional.lost["shutdown"], 1)
        self.assertIn("1 queued dropped, 1 in flight", "\n".join(logs.output))
        release.set()

    def test_once_drains_after_releasing_the_watcher_lock(self):
        feature = self.enable(Feature(after_cycle=lambda items: time.sleep(0.1)))
        self.baseline()
        self.full.append(home(2, 1300))
        events = []
        real_shutdown = optional.shutdown

        @contextlib.contextmanager
        def lock(name="run"):
            events.append(("enter", name))
            try:
                yield
            finally:
                events.append(("exit", name))

        def shutdown():
            events.append(("shutdown", ""))
            real_shutdown()

        with patch("sys.argv", ["watch", "--once"]), patch.object(watch.signal, "signal"), \
                patch.object(watch, "Watcher", return_value=self.watcher), patch.object(main, "run_lock", side_effect=lock), \
                patch.object(optional, "shutdown", side_effect=shutdown):
            watch.main()
        self.assertLess(events.index(("exit", "watch")), events.index(("shutdown", "")))
        self.assertTrue(feature.batches)
        self.assertTrue(all(not worker.is_alive() for worker in optional.workers()))

    # P3: publishing consumes no more than the cap, whatever it is handed
    def test_publishing_never_consumes_more_than_its_caps(self):
        consumed = [0, 0]

        class Endless(dict):
            def items(self):
                n = 0
                while True:
                    consumed[0] += 1
                    n += 1
                    yield f"rm:{n}", offers()

        def offers():
            while True:
                consumed[1] += 1
                yield (1, "line")
        self.assertEqual(optional.publish_lines("f", Endless()), optional.SNAPSHOT_MAX)
        self.assertEqual(consumed, [optional.SNAPSHOT_MAX, optional.SNAPSHOT_MAX * 5])

        def templates():
            while True:
                consumed.append(1)
                yield LISTED
        self.assertEqual(optional.publish_templates("f", templates()), optional.TEMPLATES_PER_FEATURE)
        self.assertEqual(len(consumed) - 2, optional.TEMPLATES_PER_FEATURE)
        self.assertEqual(optional.publish_lines("g", {"rm:1": [(1, "x" * 10 ** 6)]}), 0)       # a huge text is refused by its length alone


# ---------------------------------------------------------------- off is the release: ordered trace, with mutation checks

def release_module(name, relative):
    source = subprocess.check_output(["git", "-C", str(ROOT), "show", f"origin/release-archive-pipeline:{relative}"],
                                     text=True, stderr=subprocess.DEVNULL)
    module = types.ModuleType(f"pisobot._g0_release_{name}")
    module.__file__ = str(ROOT / relative)
    module.__package__ = "pisobot"
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def dump(path):
    out = {}
    with contextlib.closing(sqlite3.connect(path)) as db:
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
            rows = db.execute(f"SELECT * FROM {name}").fetchall()
            out[name] = sorted(mask(json.dumps(r, default=str)) for r in rows)
    return out


def mask(text):
    return re.sub(r"\d{4}-\d\d-\d\d[T ][\d:.+\-Z]+|\d{4}-\d\d-\d\d|[0-9a-f]{16,}|tmp[a-z0-9_]{6,}|\b1[67]\d{8}(?:\.\d+)?\b", "~", text)


class RealSendBase(Base):
    def setUp(self):
        super().setUp()
        for target in (patch.object(main.enrich, "geocode"), patch.object(main.enrich, "assign_crime_percentiles"),
                       patch.object(main, "passes_area", return_value=None), patch.object(main, "_ensure_photos"),
                       patch.object(telegram, "throttle")):
            target.start()
            self.addCleanup(target.stop)
        self.enterContext(patch.object(main, "passes_basic", side_effect=lambda l, **kw: main._cost_reject(l)))


class OffIsTheReleaseTests(RealSendBase):
    def setUp(self):
        super().setUp()
        optional.reset()
        self.addCleanup(optional.reset)

    def scenario(self, release, mutate=None):
        import tempfile
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = directory.name
        trace, listings = [], [(1, 1200)]
        market = Market({i: 1000 + 50 * i for i in range(1, 9)})
        counter = [0]
        real_connect, real_open, real_replace = sqlite3.connect, open, os.replace
        real_os_open, real_os_write, real_os_close = os.open, os.write, os.close
        write_fds = {}

        def connect(*a, **kw):
            db = real_connect(*a, **kw)
            with contextlib.suppress(Exception):
                db.set_trace_callback(lambda sql: trace.append(("sql", mask(sql))))
            return db

        def opener(file, mode="r", *a, **kw):
            if any(c in str(mode) for c in "wax+") and str(file).startswith(state):
                trace.append(("open", mask(os.path.relpath(str(file), state)), mode))
            return real_open(file, mode, *a, **kw)

        def replace(src, dst, *a, **kw):
            if str(dst).startswith(state):
                trace.append(("replace", mask(os.path.relpath(str(dst), state))))
            return real_replace(src, dst, *a, **kw)

        def open_fd(path, flags, *args, **kwargs):
            name = os.fsdecode(path)
            writable = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            relative = mask(os.path.relpath(name, state)) if writable and name.startswith(state + os.sep) else None
            if relative is not None:
                trace.append(("os-open", relative, flags))
            fd = real_os_open(path, flags, *args, **kwargs)
            write_fds.pop(fd, None)
            if relative is not None:
                info = os.fstat(fd)
                write_fds[fd] = (relative, info.st_dev, info.st_ino)
            return fd

        def write_fd(fd, data):
            entry = write_fds.get(fd)
            if entry is not None:
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) == entry[1:]:
                    trace.append(("os-write", entry[0], len(data)))
            return real_os_write(fd, data)

        def close_fd(fd):
            write_fds.pop(fd, None)
            return real_os_close(fd)

        def call(method, **data):
            counter[0] += 1
            trace.append(("telegram", users.current_chat(), method, mask(str(data.get("text") or data.get("caption") or "")),
                          "files" in data, bool(data.get("reply_markup"))))
            return {"result": {"message_id": counter[0]}}

        listed_at = stamp(40 * 60 + 20)                 # every advert carries a fresh portal timestamp: a template WOULD render

        def fetch():
            trace.append(("fetch", "rm"))
            found = [home(i, r) for i, r in listings]
            for listing in found:
                listing.first_listed_at = listed_at
            return found

        def band(low, high, page):
            trace.append(("band", low, high, page))
            return market.band_page(low, high, page)

        def build(l, sources=None, **kw):
            return b"jpeg" if int(l.key.split(":")[1]) % 2 else None

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"PISOBOT_STATE_DIR": state, "STORAGE": "local"}))
            stack.enter_context(patch.object(main, "STATE_DIR", state))
            stack.enter_context(patch.object(main, "SEEN_PATH", str(Path(state) / "seen.json")))
            stack.enter_context(patch.object(cache, "PATH", str(Path(state) / "cache.json")))
            stack.enter_context(patch.object(cache, "_data", {}))
            stack.enter_context(patch.object(main.SOURCES["rm"], "fetch", side_effect=fetch))
            stack.enter_context(patch.object(rightmove, "band_page", band))
            stack.enter_context(patch.object(config, "DEEP_SWEEP_HOURS", 1))
            stack.enter_context(patch.object(collage, "build", build))
            stack.enter_context(patch.object(telegram, "listing_keyboard", return_value={}))
            stack.enter_context(patch.object(sqlite3, "connect", connect))
            stack.enter_context(patch("builtins.open", opener))
            stack.enter_context(patch.object(os, "replace", replace))
            stack.enter_context(patch.object(os, "open", open_fd))
            stack.enter_context(patch.object(os, "write", write_fd))
            stack.enter_context(patch.object(os, "close", close_fd))
            if release:
                r_tel, r_main, r_sweep, r_watch = (release_module(n, f"pisobot/{n}.py") for n in ("telegram", "main", "sweep", "watch"))
                for module in (r_tel, ):
                    module._call = call
                for attr in ("STATE_DIR", "SEEN_PATH", "passes_basic", "passes_area", "_ensure_photos"):
                    setattr(r_main, attr, getattr(main, attr))
                r_main.telegram, r_sweep.telegram, r_watch.telegram = r_tel, r_tel, r_tel
                r_sweep.pipeline, r_watch.pipeline, r_watch.sweep = r_main, r_main, r_sweep
                r_tel.throttle = lambda *a, **k: None
                r_tel.listing_keyboard = lambda *a, **k: {}
                watcher_class = r_watch.Watcher
            else:
                stack.enter_context(patch.object(telegram, "_call", call))
                watcher_class = watch.Watcher
            if mutate:
                mutate(stack)
            users.register(FRIEND, "Bea", "TheBeatifulest")
            watcher = watcher_class(["rm"])
            for kind, step in (("rentals", [(1, 1200)]), ("rentals", [(1, 1200), (2, 1300)]), ("rentals", [(1, 1200), (2, 1250)]),
                               ("report", [(1, 1200), (2, 1250), (3, 1100)]), ("rentals", [(1, 1200), (2, 1250), (3, 1100)])):
                listings[:] = step
                try:
                    watcher.cycle(kind, chat_id=FRIEND if kind == "report" else None)
                except Exception as ex:
                    trace.append(("raised", type(ex).__name__))
            files, states = [], {}
            for path in sorted(Path(state).rglob("*")):
                rel = str(path.relative_to(state))
                if path.is_file() and not rel.endswith(("-wal", "-shm", ".lock")):
                    files.append(rel)
                    if path.name == "pisobot.db":
                        states[rel] = dump(path)
                    elif path.suffix == ".json":
                        states[rel] = mask(path.read_text())
            return trace, files, states

    def test_the_ordered_trace_and_every_persons_state_are_identical_to_the_releases_with_nothing_enabled(self):
        def disabled(stack):
            stack.enter_context(patch.dict(os.environ, {FLAG: "0"}))
            optional.register(FLAG, lambda: self.fail("disabled loader ran"), templates=[LISTED])
            optional.publish_lines(FLAG, {"rm:2": [(5, "disabled published line")]})

        try:
            theirs, ours = self.scenario(True), self.scenario(False, disabled)
        except subprocess.CalledProcessError:
            self.skipTest("release code not available from git")
        self.assertGreater(len([t for t in ours[0] if t[0] == "telegram"]), 3)
        self.assertTrue([t for t in ours[0] if t[0] == "band"])               # the sweep slice ran
        self.assertTrue([t for t in ours[0] if t[0] == "sql"])
        self.assertTrue(any(t[2] == "sendPhoto" for t in ours[0] if t[0] == "telegram"))
        self.assertTrue(any(t[2] == "sendMessage" for t in ours[0] if t[0] == "telegram"))
        self.assertEqual(theirs[0], ours[0])                                  # ordered: sends, requests, file writes, every SQL statement
        self.assertEqual(theirs[1], ours[1])                                  # every file, recursively (each person's directory too)
        self.assertEqual(theirs[2], ours[2])                                  # every person's database and json state
        self.assertNotIn("optional.db", " ".join(ours[1]))
        self.assertIsNone(runner.RUNNER.proc)
        self.assertNotIn("Listed", str(ours[0]))                              # the fetch-time seam renders nothing while nothing is published

    # --- the comparison is only worth something if it fails when the bot changes: three mutants of this branch
    def mutant(self, mutate):
        try:
            theirs = self.scenario(True)
        except subprocess.CalledProcessError:
            self.skipTest("release code not available from git")
        return theirs[0], self.scenario(False, mutate)[0]

    def test_mutation_the_send_order_changes(self):
        real = users.everyone
        theirs, mutated = self.mutant(lambda stack: stack.enter_context(patch.object(users, "everyone", lambda: list(reversed(real())))))
        self.assertNotEqual(theirs, mutated)

    def test_mutation_a_write_is_added_before_the_acknowledgement(self):
        real = main.save_seen

        def extra(stack):
            def save(seen):
                with contextlib.suppress(Exception), contextlib.closing(sqlite3.connect(localdb.state_dir() / "pisobot.db")) as db:
                    db.execute("UPDATE adverts SET key = key WHERE 0")
                    db.commit()
                return real(seen)
            stack.enter_context(patch.object(main, "save_seen", save))
        theirs, mutated = self.mutant(extra)
        self.assertNotEqual(theirs, mutated)

    def test_mutation_an_extra_portal_request_is_made(self):
        real = main.fetch_sources
        theirs, mutated = self.mutant(lambda stack: stack.enter_context(
            patch.object(main, "fetch_sources", lambda *a, **kw: (real(*a, **kw), real(*a, **kw))[1])))
        self.assertNotEqual(theirs, mutated)

    def test_mutation_a_low_level_file_write_changes_the_trace_even_when_state_is_unchanged(self):
        real = main.save_seen

        def extra(stack):
            def save(seen):
                real(seen)
                fd = os.open(main.SEEN_PATH, os.O_WRONLY | os.O_APPEND)
                try:
                    os.write(fd, b"")
                finally:
                    os.close(fd)
            stack.enter_context(patch.object(main, "save_seen", save))

        theirs, mutated = self.scenario(True), self.scenario(False, extra)
        self.assertNotEqual(theirs[0], mutated[0])
        self.assertTrue(any(event[0] == "os-write" for event in mutated[0]))
        self.assertEqual(theirs[1:], mutated[1:])

    def test_mutation_a_template_left_published_changes_the_trace_and_only_by_its_line(self):
        """The scenario really exercises the fetch-to-send seam: a published template shows up in the trace. And it changes NOTHING else:
        with the line removed from the card texts the ordered trace (calls, requests, file writes, SQL) is the release's again."""
        optional.reset()
        self.addCleanup(optional.reset)
        def enabled_template(stack):
            stack.enter_context(patch.dict(os.environ, {"timing": "1"}))
            stack.enter_context(patch.object(optional, "execution_available", return_value=True))
            optional.publish_templates("timing", [LISTED])

        theirs, mutated = self.mutant(enabled_template)
        self.assertNotEqual(theirs, mutated)
        line = "\n🕒 Listed 40 min ago"
        self.assertTrue(any(line in t[3] for t in mutated if t[0] == "telegram"))
        stripped = [(t[:3] + (t[3].replace(line, ""),) + t[4:]) if t[0] == "telegram" else t for t in mutated]
        self.assertEqual(theirs, stripped)


if __name__ == "__main__":
    unittest.main()
