"""G0: the optional-features foundation. No feature here: only the seams, the fault-injection probes that earlier reviews
asked every feature to survive, and a proof that with nothing enabled the bot is the release."""
import contextlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pisobot import archive, cache, config, localdb, main, optional, telegram, users, watch
from pisobot.optional import runner, store
from pisobot.sources import rightmove

from test_sweep import Market
from test_users import Base, FRIEND, OWNER, home

ROOT = Path(__file__).resolve().parents[1]
FLAG = "PISOBOT_TEST_FEATURE"


class Feature:
    """A stand-in optional feature: records what it is given; behaviour is set per test."""
    name = "probe"

    def __init__(self, after_cycle=None, card_line=None, work=False):
        self.calls, self._after, self._card, self.work = [], after_cycle, card_line, work
        if card_line is not None:
            self.card_line = card_line

    def after_cycle(self, items, deadline):
        self.calls.append(list(items))
        return self._after(items, deadline) if self._after else None

    def has_work(self):
        return self.work

    def step(self, deadline):
        pass


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

    def enable(self, feature):
        optional.register(FLAG, lambda: feature)
        patcher = patch.dict(os.environ, {FLAG: "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        return feature

    def baseline(self):
        self.watcher.cycle()
        self.sent.clear()
        optional.reset()
        optional._registry.clear()


# ---------------------------------------------------------------- the registry: off means off

class RegistryTests(OptionalBase):
    def test_a_feature_whose_flag_is_not_one_is_never_loaded(self):
        loaded = []
        optional.register(FLAG, lambda: loaded.append(1))
        with patch.dict(os.environ, {FLAG: "0"}):
            self.assertFalse(optional.enabled_any())
            self.assertEqual(optional.features(), [])
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(FLAG, None)
            self.assertEqual(optional.features(), [])
        self.assertEqual(loaded, [])

    def test_a_feature_that_fails_to_load_is_skipped_and_only_tried_once(self):
        tries = []

        def loader():
            tries.append(1)
            raise ImportError("broken")
        optional.register(FLAG, loader)
        with patch.dict(os.environ, {FLAG: "1"}):
            self.assertEqual(optional.features(), [])
            self.assertEqual(optional.features(), [])
        self.assertEqual(len(tries), 1)

    def test_importing_the_package_creates_nothing(self):
        self.assertFalse(store.exists())
        self.assertEqual(store.read("SELECT 1"), [])
        self.assertFalse(store.exists())                       # a read never creates the file
        self.assertIsNone(runner.RUNNER.proc)

    def test_with_nothing_enabled_the_hooks_do_nothing_and_cost_nothing(self):
        optional.after_ack(home(1), OWNER, {"result": {"message_id": 1}}, {})
        self.assertEqual(optional._items, [])
        self.assertEqual(optional.after_cycle(), 0)
        lines = ["a", "b"]
        self.assertIs(optional.card_lines(lines, home(1)), lines)
        self.assertFalse(runner.RUNNER.maybe_start(popen=MagicMock(side_effect=AssertionError("started a process"))))

    def test_message_id_reads_what_the_send_returned(self):
        self.assertEqual(optional.message_id({"result": {"message_id": 5}}), 5)
        for bad in (None, {}, {"result": {}}, {"result": {"message_id": "5"}}, "x", {"result": None}):
            self.assertIsNone(optional.message_id(bad))


# ---------------------------------------------------------------- the hook after the acknowledgement

class AfterAckTests(OptionalBase):
    def test_the_hook_comes_after_telegram_the_receipt_and_the_db_acknowledgement_and_nothing_else_is_between(self):
        self.baseline()
        self.full = [home(1, 1200), home(2, 1250)]
        events = []
        real_save, real_verdicts = main.save_seen, archive.record_verdicts
        real_send = telegram.send_listing

        def send(l, reason):
            events.append(("send", l.key))
            return real_send(l, reason)

        with patch.object(telegram, "send_listing", side_effect=send), \
                patch.object(main, "save_seen", side_effect=lambda s: (events.append(("seen",)), real_save(s))[1]), \
                patch.object(archive, "record_verdicts",
                             side_effect=lambda *a, **kw: (events.append(("ack" if kw.get("acknowledged") else "books",)),
                                                           real_verdicts(*a, **kw))[1]), \
                patch.object(optional, "after_ack",
                             side_effect=lambda l, p, reply, ctx: events.append(("hook", l.key, optional.message_id(reply)))):
            self.watcher.cycle()
        sequence = [e[0] for e in events if e[0] in ("send", "seen", "ack", "hook")]
        # per alert: send -> seen -> ack -> hook, exactly in this order (the hook is the last thing before the next alert)
        for i in range(0, len(sequence) - 3):
            if sequence[i] == "send":
                self.assertEqual(sequence[i:i + 4], ["send", "seen", "ack", "hook"])
        self.assertGreaterEqual(len([e for e in events if e[0] == "hook"]), 2)
        self.assertTrue(all(e[2] == 7 for e in events if e[0] == "hook"))

    def test_a_hook_that_raises_costs_no_receipt_and_changes_nothing(self):
        """Fault injection: everything inside after_ack explodes. The alert is still recorded and acknowledged once, and is not repeated."""
        self.baseline()
        optional.register(FLAG, lambda: Feature())
        self.full = [home(1, 1200), home(2, 1250)]
        with patch.dict(os.environ, {FLAG: "1"}), patch.object(optional, "_items", new=_Exploding()):
            self.watcher.cycle()
        self.assertGreater(len(self.sent), 0)
        seen = json.loads((self.state / "seen.json").read_text())
        self.assertIn("rm:2", seen)
        self.sent.clear()
        with patch.dict(os.environ, {FLAG: "1"}):
            self.watcher.cycle()
        self.assertEqual(self.sent, [])                              # nothing repeated

    def test_the_hook_is_an_in_memory_append_with_no_io(self):
        self.enable(Feature())
        with patch("builtins.open", side_effect=AssertionError("I/O in the hook")), \
                patch.object(sqlite3, "connect", side_effect=AssertionError("DB in the hook")), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("process in the hook")):
            optional.after_ack(home(1), OWNER, {"result": {"message_id": 3}}, {"reason": "new"})
        self.assertEqual(len(optional._items), 1)
        self.assertEqual(optional._items[0]["reply"], {"result": {"message_id": 3}})

    def test_the_buffer_is_bounded_and_the_overflow_is_counted(self):
        self.enable(Feature())
        for i in range(optional.BUFFER_LIMIT + 7):
            optional.after_ack(home(1), OWNER, None, {})
        self.assertEqual(len(optional._items), optional.BUFFER_LIMIT)
        self.assertEqual(optional.dropped, 7)

    def test_price_drop_and_saved_cards_pass_no_reply_because_the_release_discards_it(self):
        self.baseline()
        self.enable(Feature())
        self.full = [home(1, 1100)]
        self.watcher.cycle()
        self.assertTrue(optional._items or optional.features()[0].calls)
        self.assertTrue(all(i["reply"] is None for call in optional.features()[0].calls for i in call))


class _Exploding(list):
    def append(self, item):
        raise RuntimeError("boom")

    def __len__(self):
        raise RuntimeError("boom")


# ---------------------------------------------------------------- the hook after the cycle

class AfterCycleTests(OptionalBase):
    def test_it_runs_after_the_run_lock_is_released_and_never_in_a_dry_run(self):
        self.baseline()
        feature = self.enable(Feature())
        held = []

        def probe(items, deadline):
            with contextlib.suppress(Exception):
                held.append(not _lock_is_free())
        feature._after = probe
        self.full = [home(1, 1200), home(2, 1250)]
        self.watcher.cycle()
        self.assertEqual(held, [False])                                # the lock was free when the feature ran
        self.assertEqual(len(feature.calls), 1)
        self.assertEqual(len(feature.calls[0]), len(self.sent))
        feature.calls.clear()
        self.full = [home(1, 1200), home(2, 1250), home(3, 1300)]
        self.watcher.cycle(dry_run=True)
        self.assertEqual(feature.calls, [])
        self.assertEqual(optional._items, [])

    def test_items_survive_a_failed_cycle_and_reach_the_feature_after_it(self):
        """The successful prefix of a partially failed run is not lost."""
        self.baseline()
        feature = self.enable(Feature())
        self.full = [home(1, 1200), home(2, 1250)]
        real = telegram.send_listing

        def flaky(l, reason):
            if l.key == "rm:2" and users.current_chat() == OWNER:
                raise RuntimeError("telegram down")
            return real(l, reason)
        with patch.object(telegram, "send_listing", side_effect=flaky), self.assertRaises(Exception):
            self.watcher.cycle()
        self.assertTrue(feature.calls and feature.calls[0])             # the alerts that were accepted were handed over

    def test_a_feature_that_raises_is_isolated_from_the_others_and_from_the_cycle(self):
        good = Feature()
        bad = Feature(after_cycle=lambda items, deadline: 1 / 0)
        bad.name = "bad"
        optional.register("PISOBOT_BAD", lambda: bad)
        optional.register(FLAG, lambda: good)
        with patch.dict(os.environ, {"PISOBOT_BAD": "1", FLAG: "1"}):
            optional.after_ack(home(1), OWNER, None, {})
            optional.after_cycle()
        self.assertEqual(len(good.calls), 1)

    def test_a_blocking_feature_cannot_hold_the_cycle_past_the_budget(self):
        """Fault injection: a hook that blocks for ten seconds."""
        stop = threading.Event()
        feature = self.enable(Feature(after_cycle=lambda items, deadline: stop.wait(10)))
        self.addCleanup(stop.set)
        optional.after_ack(home(1), OWNER, None, {})
        start = time.monotonic()
        optional.after_cycle(budget=0.3)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(len(optional._pending["probe"]), 1)           # kept for next time

    def test_unfinished_items_are_offered_again_and_bounded(self):
        feature = self.enable(Feature(after_cycle=lambda items, deadline: True))      # True: "keep these"
        optional.after_ack(home(1), OWNER, None, {})
        optional.after_cycle()
        optional.after_ack(home(2), OWNER, None, {})
        optional.after_cycle()
        self.assertEqual([len(c) for c in feature.calls], [1, 2])
        for _ in range(optional.BUFFER_LIMIT + 50):
            optional._items.append({})
        optional.after_cycle()
        self.assertLessEqual(len(optional._pending["probe"]), optional.BUFFER_LIMIT)

    def test_it_never_raises(self):
        self.enable(Feature())
        with patch.object(optional, "features", side_effect=RuntimeError("x")):
            self.assertEqual(optional.after_cycle(), 0)


def _lock_is_free():
    import fcntl
    path = Path(main.STATE_DIR) / "run.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


# ---------------------------------------------------------------- storage

class StorageTests(OptionalBase):
    DDL = "CREATE TABLE IF NOT EXISTS t (k TEXT, v TEXT)"

    def test_the_file_is_created_by_the_first_write_and_not_before(self):
        self.assertFalse(store.exists())
        self.assertTrue(store.write(self.DDL, [("INSERT INTO t VALUES (?, ?)", ("a", "1"))]))
        self.assertTrue(store.exists())
        self.assertEqual(store.read("SELECT k, v FROM t"), [{"k": "a", "v": "1"}])

    def test_a_contended_database_fails_fast_and_changes_nothing_else(self):
        """Fault injection: another connection holds the write lock."""
        store.write(self.DDL, [])
        holder = sqlite3.connect(store.path())
        holder.execute("BEGIN IMMEDIATE")
        self.addCleanup(holder.close)
        start = time.monotonic()
        self.assertFalse(store.write(self.DDL, [("INSERT INTO t VALUES (?, ?)", ("a", "1"))]))
        self.assertLess(time.monotonic() - start, store.BUSY_SECONDS + 1.0)
        holder.rollback()
        self.assertEqual(store.read("SELECT * FROM t"), [])

    def test_a_contended_optional_db_does_not_touch_the_primary_database_or_the_alert(self):
        self.baseline()
        store.write(self.DDL, [])
        holder = sqlite3.connect(store.path())
        holder.execute("BEGIN IMMEDIATE")
        self.addCleanup(holder.close)
        feature = self.enable(Feature(after_cycle=lambda items, deadline: store.write(
            self.DDL, [("INSERT INTO t VALUES (?, ?)", (i["listing"].key, "x")) for i in items]) and None))
        self.full = [home(1, 1200), home(2, 1250)]
        start = time.monotonic()
        self.watcher.cycle()
        self.assertLess(time.monotonic() - start, 10)
        self.assertGreater(len(self.sent), 0)
        seen = json.loads((self.state / "seen.json").read_text())
        self.assertIn("rm:2", seen)

    def test_a_transaction_that_runs_too_long_is_interrupted_not_waited_for(self):
        slow = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 500000000) SELECT count(*) FROM c"
        start = time.monotonic()
        self.assertFalse(store.write(self.DDL, [(slow, ())]))
        self.assertLess(time.monotonic() - start, store.TX_SECONDS + 2.0)
        self.assertEqual(store.read("SELECT * FROM t"), [])

    def test_a_failed_batch_leaves_nothing_half_written(self):
        self.assertFalse(store.write(self.DDL, [("INSERT INTO t VALUES (?, ?)", ("a", "1")), ("INSERT INTO nope VALUES (1)", ())]))
        self.assertEqual(store.read("SELECT * FROM t"), [])

    def test_a_large_batch_is_refused_before_any_io(self):
        with self.assertRaises(ValueError):
            store.write(self.DDL, [("SELECT 1", ())] * (store.MAX_STATEMENTS + 1))
        self.assertFalse(store.exists())

    def test_optional_code_cannot_write_to_the_primary_database(self):
        main_db = localdb.state_dir() / "pisobot.db"
        with sqlite3.connect(main_db) as db:
            db.execute("CREATE TABLE IF NOT EXISTS probe (v TEXT)")
            db.execute("INSERT INTO probe VALUES ('kept')")
        self.assertEqual(store.read_primary("SELECT v FROM probe"), [{"v": "kept"}])
        self.assertEqual(store.read_primary("INSERT INTO probe VALUES ('hacked')"), [])
        self.assertEqual(store.read_primary("DROP TABLE probe"), [])
        self.assertEqual(store.read_primary("SELECT v FROM probe"), [{"v": "kept"}])
        self.assertEqual(store.read_primary("SELECT * FROM missing_table"), [])

    def test_the_primary_reader_creates_nothing(self):
        before = sorted(p.name for p in localdb.state_dir().iterdir())
        self.assertEqual(store.read_primary("SELECT 1", person="999"), [])
        self.assertEqual(sorted(p.name for p in localdb.state_dir().iterdir()), before)


# ---------------------------------------------------------------- the supervised step

class FakeProc:
    def __init__(self):
        self.killed, self.waited = False, False

    def poll(self):
        return -9 if self.killed else None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True


class RunnerTests(OptionalBase):
    def test_nothing_is_started_without_an_enabled_feature_with_queued_work(self):
        popen = MagicMock(side_effect=AssertionError("started"))
        self.assertFalse(runner.RUNNER.maybe_start(popen=popen))
        self.enable(Feature(work=False))
        self.assertFalse(runner.RUNNER.maybe_start(popen=popen))

    def test_a_child_is_started_once_and_only_one_runs(self):
        self.enable(Feature(work=True))
        proc = FakeProc()
        popen = MagicMock(return_value=proc)
        r = runner.Runner()
        self.assertTrue(r.maybe_start(popen=popen))
        self.assertFalse(r.maybe_start(popen=popen))                   # single flight in the supervisor too
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(popen.call_args[0][0][-1], "pisobot.optional.step")
        r.stop()

    def test_an_overdue_child_is_killed_at_the_start_of_the_next_cycle(self):
        """Fault injection: a step that never ends."""
        proc = FakeProc()
        runner.RUNNER.proc, runner.RUNNER.started = proc, time.monotonic() - runner.STEP_SECONDS - runner.KILL_GRACE_SECONDS - 1
        self.addCleanup(lambda: setattr(runner.RUNNER, "proc", None))
        self.baseline()
        self.assertTrue(proc.killed and proc.waited)
        self.assertIsNone(runner.RUNNER.proc)

    def test_a_child_within_its_budget_is_left_alone(self):
        proc = FakeProc()
        runner.RUNNER.proc, runner.RUNNER.started = proc, time.monotonic()
        self.addCleanup(lambda: setattr(runner.RUNNER, "proc", None))
        self.assertEqual(runner.RUNNER.reap(), "running")
        self.assertFalse(proc.killed)

    def test_the_child_is_never_started_by_a_dry_run_or_inside_a_cycle(self):
        self.enable(Feature(work=True))
        with patch.object(runner.RUNNER, "maybe_start") as start:
            self.watcher.cycle(dry_run=True)
            self.watcher.cycle()
        start.assert_not_called()                                      # only serve() starts it, after the cycle

    def test_the_step_refuses_to_run_without_resource_limits_unless_opted_out(self):
        from pisobot.optional import step
        self.assertTrue(callable(step.main))
        with patch.dict(os.environ, {"STORAGE": "local"}), patch("resource.setrlimit", side_effect=ValueError("no")), \
                patch.object(step, "features", return_value=[Feature(work=True)]), patch.object(store, "exists", return_value=True), \
                patch.object(store, "path", return_value=Path(self.tmp.name) / "optional.db"):
            self.assertEqual(step.main(), 3)

    def test_a_real_child_exits_cleanly_when_nothing_is_enabled(self):
        env = {**os.environ, "STORAGE": "local", "PISOBOT_STATE_DIR": self.tmp.name}
        env.pop(FLAG, None)
        done = subprocess.run([os.sys.executable, "-m", "pisobot.optional.step"], cwd=ROOT, env=env, timeout=60,
                              capture_output=True)
        self.assertEqual(done.returncode, 0)
        self.assertFalse(store.exists())


# ---------------------------------------------------------------- the card line

class CardLineTests(OptionalBase):
    def lines(self, size):
        return ["x" * size]

    def feature(self, *offers):
        it = iter(offers)
        f = Feature(card_line=lambda l: next(it, None))
        return self.enable(f)

    def test_a_line_that_fits_is_added(self):
        self.feature((5, "📐 53 m²"))
        out = optional.card_lines(["head", "body"], home(1))
        self.assertEqual(out, ["head", "body", "📐 53 m²"])

    def test_an_over_long_line_is_refused_and_the_card_stays_one_call(self):
        """Fault injection: a line that would push the caption past 1,024 units."""
        self.feature((9, "y" * 2000))
        base = self.lines(1000)
        self.assertEqual(optional.card_lines(base, home(1)), base)

    def test_the_caption_limit_is_measured_in_utf16_units_like_send_card(self):
        # 500 emoji = 1000 UTF-16 units: 1000 bytes-of-text-as-characters would pass a naive len(), the real count does not
        base = ["🏠" * 500]
        self.assertEqual(len(base[0].encode("utf-16-le")) // 2, 1000)
        self.feature((9, "📐📐📐📐📐📐📐📐📐📐"))                              # +20 units -> 1021 with newline: fits
        out = optional.card_lines(base, home(1))
        self.assertLessEqual(len("\n".join(out).encode("utf-16-le")) // 2, 1024)
        self.assertEqual(len(out), 2)
        optional.reset()
        optional._registry.clear()
        self.feature((9, "📐" * 12))                                       # +24+1 units -> 1025: does not fit
        self.assertEqual(optional.card_lines(base, home(1)), base)

    def test_the_lowest_priority_is_dropped_first(self):
        a = Feature(card_line=lambda l: (1, "L" * 60))
        a.name = "low"
        b = Feature(card_line=lambda l: (8, "H" * 60))
        b.name = "high"
        optional.register("PISOBOT_A", lambda: a)
        optional.register("PISOBOT_B", lambda: b)
        with patch.dict(os.environ, {"PISOBOT_A": "1", "PISOBOT_B": "1"}):
            out = optional.card_lines(["z" * 950], home(1))
        self.assertEqual(out[1:], ["H" * 60])

    def test_a_card_that_already_goes_out_as_photo_plus_text_gets_lines_only_within_one_message(self):
        base = ["z" * 1500]
        self.feature((5, "w" * 100), (5, "w" * 100))
        out = optional.card_lines(base, home(1))
        self.assertEqual(len(out), 2)
        optional.reset()
        optional._registry.clear()
        self.feature((5, "w" * 3000))
        self.assertEqual(optional.card_lines(base, home(1)), base)

    def test_a_slow_provider_yields_no_line_and_is_skipped_afterwards(self):
        calls = []

        def slow(l):
            calls.append(1)
            time.sleep(0.5)
            return (9, "late")
        self.enable(Feature(card_line=slow))
        start = time.monotonic()
        out = optional.card_lines(["a"], home(1))
        self.assertLess(time.monotonic() - start, 0.4)
        self.assertEqual(out, ["a"])
        self.assertEqual(optional.card_lines(["a"], home(1)), ["a"])
        self.assertEqual(len(calls), 1)                                 # not asked again while it is slow

    def test_a_provider_that_raises_or_returns_junk_changes_nothing(self):
        for offer in (lambda l: 1 / 0, lambda l: (1, None), lambda l: (1, "   "), lambda l: "text"):
            optional.reset()
            self.enable(Feature(card_line=offer))
            self.assertEqual(optional.card_lines(["a"], home(1)), ["a"])

    def test_the_real_card_is_unchanged_with_nothing_enabled(self):
        l = home(1)
        with patch.object(optional, "card_lines", wraps=optional.card_lines) as wrapped:
            text = telegram.format_listing(l)
        wrapped.assert_called_once()
        optional.reset()
        self.assertEqual(telegram.format_listing(l), text)


# ---------------------------------------------------------------- off is the release

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
            out[name] = sorted(re.sub(r"\d{4}-\d\d-\d\dT[\d:.+\-Z]+|[0-9a-f]{16,}", "~", json.dumps(r, default=str)) for r in rows)
    return out


class OffIsTheReleaseTests(OptionalBase):
    def scenario(self, watcher_class, pipeline_module):
        import tempfile
        state = tempfile.mkdtemp()
        sent, texts, listings = [], [], [(1, 1200)]
        market = Market({i: 1000 + 50 * i for i in range(1, 9)})
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"PISOBOT_STATE_DIR": state, "STORAGE": "local"}))
            stack.enter_context(patch.object(main, "STATE_DIR", state))
            stack.enter_context(patch.object(main, "SEEN_PATH", str(Path(state) / "seen.json")))
            stack.enter_context(patch.object(cache, "PATH", str(Path(state) / "cache.json")))
            stack.enter_context(patch.object(cache, "_data", {}))
            stack.enter_context(patch.object(main.SOURCES["rm"], "fetch", side_effect=lambda: [home(i, r) for i, r in listings]))
            stack.enter_context(patch.object(rightmove, "band_page", market.band_page))
            stack.enter_context(patch.object(config, "DEEP_SWEEP_HOURS", 1))
            stack.enter_context(patch.object(telegram, "send_listing", side_effect=lambda l, r: (
                sent.append((users.current_chat(), l.key, r, telegram.format_listing(l))), {"result": {"message_id": 1}})[1]))
            stack.enter_context(patch.object(telegram, "send_text", side_effect=lambda text, *a, **kw: texts.append(
                (users.current_chat(), re.sub(r"\d{1,2}:\d\d|\d+ (?:min|h)\w*", "~", str(text))))))
            if pipeline_module is not main:
                for attr in ("STATE_DIR", "SEEN_PATH", "passes_basic", "passes_area", "_ensure_photos"):
                    setattr(pipeline_module, attr, getattr(main, attr))
            stack.enter_context(patch.object(watch, "pipeline", pipeline_module))
            users.register(FRIEND, "Bea", "TheBeatifulest")
            watcher = watcher_class(["rm"])
            for kind, step in (("rentals", [(1, 1200)]), ("rentals", [(1, 1200), (2, 1300)]), ("rentals", [(1, 1200), (2, 1250)]),
                               ("report", [(1, 1200), (2, 1250), (3, 1100)]), ("rentals", [(1, 1200), (2, 1250), (3, 1100)])):
                listings[:] = step
                try:
                    watcher.cycle(kind, chat_id=FRIEND if kind == "report" else None)
                except Exception as ex:                              # an incomplete sweep slice raises in both; the type must match
                    texts.append(("raised", type(ex).__name__))
            files = sorted(p.name for p in Path(state).iterdir() if p.suffix != ".lock" and "-wal" not in p.name and "-shm" not in p.name)
            seen = re.sub(r"\d{4}-\d\d-\d\dT?[\d:.+\-Z]*|[0-9a-f]{16,}", "~", (Path(state) / "seen.json").read_text())
            return sent, texts, seen, dump(Path(state) / "pisobot.db"), files, market.calls

    def test_cycle_outputs_and_the_primary_database_are_identical_to_the_releases_with_nothing_enabled(self):
        try:
            release_main = release_module("main", "pisobot/main.py")
            release_watch = release_module("watch", "pisobot/watch.py")
        except Exception:
            self.skipTest("release code not available from git")
        release_watch.pipeline = release_main
        theirs = self.scenario(release_watch.Watcher, release_main)
        ours = self.scenario(watch.Watcher, main)
        self.assertGreater(len(ours[0]), 3)                             # real alerts for several people
        self.assertTrue(ours[5])                                        # the sweep slice really ran
        self.assertTrue(any("raised" not in t for t in ours[1]))
        self.assertEqual(theirs[0], ours[0])                            # sends, with card text
        self.assertEqual(theirs[1], ours[1])                            # the report and other messages
        self.assertEqual(theirs[2], ours[2])                            # seen state
        self.assertEqual(theirs[3], ours[3])                            # every table of the primary DB
        self.assertEqual(theirs[4], ours[4])                            # the same files: no optional.db, nothing new
        self.assertEqual(len(theirs[5]), len(ours[5]))                  # the same portal requests
        self.assertNotIn("optional.db", ours[4])
        self.assertIsNone(runner.RUNNER.proc)


if __name__ == "__main__":
    unittest.main()
