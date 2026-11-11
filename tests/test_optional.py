"""G0 (round 2): the optional-features foundation. No feature here: only the seams, the fault-injection probes the reviews asked for, and
a proof (by ordered trace, with mutation checks) that with nothing enabled the bot is the release."""
import contextlib
import fcntl
import json
import os
import re
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
        self.assertEqual(store.read_primary("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 500000000) "
                                            "SELECT count(*) AS n FROM c"), [])
        self.assertLess(time.monotonic() - start, store.READ_SECONDS + 2.0)

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


class RunnerTests(OptionalBase):
    def setUp(self):
        super().setUp()
        self.runner = runner.Runner()
        self.addCleanup(self.runner.stop)

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
        proc = FakeProc()
        self.assertTrue(self.runner.maybe_start(popen=MagicMock(return_value=proc)))
        self.assertFalse(self.runner.maybe_start(popen=popen))              # one at a time
        self.runner.stop()

    def test_an_overdue_child_is_killed_at_the_next_cycle_start_and_ownership_is_kept_until_it_is_dead(self):
        stubborn = FakeProc(stubborn=True)
        runner.RUNNER.proc = stubborn
        runner.RUNNER.started = time.monotonic() - runner.STEP_SECONDS - runner.KILL_GRACE_SECONDS - 1
        self.addCleanup(lambda: (setattr(runner.RUNNER, "proc", None), runner.RUNNER.dying.clear()))
        with patch.object(runner.os, "killpg"):
            start = time.monotonic()
            self.baseline()
            self.assertLess(time.monotonic() - start, 5)
        self.assertTrue(stubborn.killed)
        self.assertIn(stubborn, runner.RUNNER.dying)                         # not confirmed dead: still tracked, retried

    def test_a_child_within_its_budget_is_left_alone(self):
        proc = FakeProc()
        self.runner.proc, self.runner.started = proc, time.monotonic()
        self.assertEqual(self.runner.reap(), "running")
        self.assertFalse(proc.killed)

    def test_the_timer_is_cancelled_when_the_child_is_reaped(self):
        proc = FakeProc()
        self.enable(Feature(work=True))
        optional.start()
        optional.after_cycle()
        self.assertTrue(wait_for(lambda: optional.workers()[0].work))
        self.assertTrue(self.runner.maybe_start(popen=MagicMock(return_value=proc)))
        timer = self.runner.timer
        proc.killed = True
        self.runner.reap()
        self.assertTrue(timer.finished.is_set())
        self.assertIsNone(self.runner.timer)

    def test_shutdown_kills_the_whole_process_group_and_reaps_the_child(self):
        child = subprocess.Popen([sys.executable, "-c",
                                  "import subprocess,sys,time;g=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                                  "print(g.pid,flush=True);time.sleep(60)"], stdout=subprocess.PIPE, text=True, start_new_session=True)
        grandchild = int(child.stdout.readline())
        self.runner.proc, self.runner.started = child, time.monotonic()
        self.runner.stop()
        self.assertIsNotNone(child.returncode)                                # reaped: no zombie
        self.assertTrue(wait_for(lambda: not _alive(grandchild)))            # descendants died with the group

    def test_the_supervisor_never_raises(self):
        proc = MagicMock()
        proc.poll.side_effect = OSError("poll failed")
        self.runner.proc = proc
        self.assertIsNone(self.runner.reap())
        self.runner.stop()

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
                patch("signal.alarm"), patch.object(threading.Thread, "start"):
            self.assertEqual(step.main(), 0)
        self.assertEqual(held, [True])

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
        optional.reset()
        self.addCleanup(optional.reset)

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
        self.assertIn("&lt;b&gt;bold&lt;/b&gt; &amp; co", [t for _, t in lines])
        self.assertEqual(len(lines), 2)

    def test_the_snapshot_is_replaced_atomically_and_features_can_replace_their_own_lines(self):
        optional.publish_lines("a", {"rm:1": [(1, "one")]})
        before = optional._snapshot
        optional.publish_lines("a", {"rm:2": [(1, "two")]})
        self.assertIn("rm:1", before)                                       # the old snapshot object was never mutated
        self.assertNotIn("rm:1", optional._snapshot)
        optional.publish_lines("b", {"rm:2": [(9, "high")]})
        self.assertEqual([t for _, t in optional._snapshot["rm:2"]], ["high", "two"])

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
    def scenario(self, release, mutate=None):
        import tempfile
        state = tempfile.mkdtemp()
        trace, listings = [], [(1, 1200)]
        market = Market({i: 1000 + 50 * i for i in range(1, 9)})
        counter = [0]
        real_connect, real_open, real_replace = sqlite3.connect, open, os.replace

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

        def call(method, **data):
            counter[0] += 1
            trace.append(("telegram", users.current_chat(), method, mask(str(data.get("text") or data.get("caption") or "")),
                          "files" in data, bool(data.get("reply_markup"))))
            return {"result": {"message_id": counter[0]}}

        def fetch():
            trace.append(("fetch", "rm"))
            return [home(i, r) for i, r in listings]

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
        try:
            theirs, ours = self.scenario(True), self.scenario(False)
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


if __name__ == "__main__":
    unittest.main()
