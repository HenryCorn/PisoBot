"""facts.py: optional facts delivered AFTER the alert (G2). Re-review findings ported, the redesign proven.
Offline: fakes only; the supervised child is exercised for real with no network."""
import collections
import contextlib
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from pisobot import archive, budget, cache, config, epc, facts, localdb, main, sweep, telegram, users, watch
from pisobot.models import Listing

from test_epc import BUILDING, CERTS, Fake, NOW, advert, cert, make, peer, summary
from test_users import Base, FRIEND, OWNER, home

ROOT = Path(__file__).resolve().parents[1]
ENV_ON = {"EPC_API_TOKEN": "x", "STORAGE": "local"}


def sent_card(message_id=500):
    return {"ok": True, "result": {"message_id": message_id}}


def flat_with_postcode(i=2, rent=1300):
    l = home(i, rent)
    l.address = "Flat 3, 12 Hornsey Road, London N7 6DJ"
    l.full_postcode = "N7 6DJ"
    l.description = "One bedroom flat."
    return l


# ---------------------------------------------------------------- capture and queue

class CaptureTests(unittest.TestCase):
    def test_capture_does_no_io_and_keeps_only_what_a_fact_needs(self):
        l = flat_with_postcode()
        with patch.object(facts, "_db", side_effect=AssertionError("capture touched the database")):
            row = facts.capture(l, sent_card(77), OWNER, NOW)
        self.assertEqual((row["chat_id"], row["key"], row["message_id"]), (OWNER, "rm:2", 77))
        payload = json.loads(row["payload"])
        self.assertEqual(payload["full_postcode"], "N7 6DJ")
        self.assertNotIn("images", payload)

    def test_nothing_to_reply_to_or_nothing_to_say_is_not_queued(self):
        l = flat_with_postcode()
        self.assertIsNone(facts.capture(l, None, OWNER))
        self.assertIsNone(facts.capture(l, {"result": {}}, OWNER))
        self.assertIsNone(facts.capture(l, {"result": {"message_id": "7"}}, OWNER))
        bare = home(3)
        bare.address = "Somewhere"
        self.assertIsNone(facts.capture(bare, sent_card(), OWNER))
        sale = flat_with_postcode()
        sale.tenure = "sale"
        self.assertIsNone(facts.capture(sale, sent_card(), OWNER))


class QueueTests(Base):
    def test_enqueue_is_idempotent_and_pending_counts_live_rows(self):
        row = facts.capture(flat_with_postcode(), sent_card(), OWNER)
        self.assertEqual(facts.enqueue([row]), 1)
        self.assertEqual(facts.enqueue([row]), 0)
        self.assertEqual(facts.pending(), 1)

    def test_rows_expire_after_a_day_and_after_three_attempts(self):
        old = facts.capture(flat_with_postcode(2), sent_card(1), OWNER, dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2))
        spent = facts.capture(flat_with_postcode(3), sent_card(2), OWNER)
        fresh = facts.capture(flat_with_postcode(4), sent_card(3), OWNER)
        facts.enqueue([old, spent, fresh])
        with facts._db() as db:
            db.execute("UPDATE facts_wanted SET attempts = ? WHERE key = 'rm:3'", (facts.MAX_ATTEMPTS,))
        self.assertEqual(facts.pending(), 1)
        deleted = facts.purge_expired()
        self.assertEqual(deleted["facts_wanted"], 2)
        self.assertEqual(facts.pending(), 1)

    def test_a_contended_database_skips_the_enqueue_quickly(self):
        facts.enqueue([facts.capture(flat_with_postcode(9), sent_card(9), OWNER)])      # table exists
        db = sqlite3.connect(localdb.state_dir() / "pisobot.db")
        self.addCleanup(db.close)
        db.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        written = facts.enqueue([facts.capture(flat_with_postcode(2), sent_card(), OWNER)])
        elapsed = time.monotonic() - started
        db.rollback()
        self.assertEqual(written, 0)
        self.assertLess(elapsed, 1.5)                               # the database's own busy timeout is 30 s
        self.assertGreater(localdb.BUSY_TIMEOUT_MS / 1000, 10)


# ---------------------------------------------------------------- the alert path: nothing optional before the send

class AlertPathBase(Base):
    def setUp(self):
        super().setUp()
        for target in (patch.object(main.enrich, "geocode"), patch.object(main.enrich, "assign_crime_percentiles"),
                       patch.object(main, "passes_area", return_value=None), patch.object(main, "_ensure_photos"),
                       patch.object(telegram, "throttle")):
            target.start()
            self.addCleanup(target.stop)
        self.enterContext(patch.object(main, "passes_basic", side_effect=lambda l, **kw: main._cost_reject(l)))
        self.listings = [home(1, 1200)]
        self.enterContext(patch.object(main.SOURCES["rm"], "fetch", side_effect=lambda: list(self.listings)))
        self.events, self.sent = [], []
        self.enterContext(patch.object(telegram, "send_listing", side_effect=self._send))
        self.enterContext(patch.object(telegram, "send_text"))
        watch.Watcher(["rm"]).cycle()                                # baseline (quiet)
        self.sent.clear()
        self.events.clear()

    def _send(self, l, reason):
        self.events.append(("telegram", l.key))
        self.sent.append((users.current_chat(), l.key, reason))
        return sent_card(1000 + int(l.id))


class AlertPathTests(AlertPathBase):
    def test_no_optional_work_exists_in_the_alert_path(self):
        self.listings = [home(1, 1200), flat_with_postcode(2), flat_with_postcode(3)]
        for l in self.listings[1:]:
            l.floorplan = "https://image.invalid/plan.png"
        boom = AssertionError("optional work ran in the alert path")
        with patch.dict(os.environ, {**ENV_ON, "FLOORPLAN_AREA": "1", "ANTHROPIC_API_KEY": "x"}), \
                patch.object(epc, "annotate", side_effect=boom), patch.object(epc, "default", side_effect=boom), \
                patch.object(epc, "value_for", side_effect=boom), patch.object(epc, "note_portal_area", side_effect=boom):
            watch.Watcher(["rm"]).cycle()
        self.assertEqual(sorted(k for c, k, r in self.sent), ["rm:2", "rm:3"])

    def test_capture_is_memory_only_and_the_queue_is_written_after_receipt_and_acknowledgement(self):
        self.listings = [home(1, 1200), flat_with_postcode(2)]
        real_save, real_verdicts = main.save_seen, archive.record_verdicts
        real_capture, real_enqueue = facts.capture, facts.enqueue

        def save(seen):
            if "rm:2" in seen:
                self.events.append(("receipt", "rm:2"))
            return real_save(seen)

        def verdicts(*a, **kw):
            if kw.get("acknowledged"):
                self.events.append(("ack", ""))
            return real_verdicts(*a, **kw)

        def capture(*a, **kw):
            self.events.append(("capture", ""))
            with patch.object(facts, "_db", side_effect=AssertionError("capture did I/O")):
                return real_capture(*a, **kw)

        def enqueue(rows):
            self.events.append(("enqueue", len(rows)))
            return real_enqueue(rows)

        with patch.dict(os.environ, ENV_ON), patch.object(main, "save_seen", side_effect=save), \
                patch.object(archive, "record_verdicts", side_effect=verdicts), patch.object(facts, "capture", side_effect=capture), \
                patch.object(facts, "enqueue", side_effect=enqueue):
            watch.Watcher(["rm"]).cycle()
        order = [e[0] for e in self.events]
        self.assertLess(order.index("telegram"), order.index("capture"))
        self.assertLess(order.index("capture"), order.index("receipt"))
        self.assertLess(order.index("receipt"), order.index("ack"))
        self.assertLess(order.index("ack"), order.index("enqueue"))
        self.assertEqual(facts.pending(), 1)

    def test_a_dead_queue_database_loses_no_alert_and_fails_nothing(self):
        self.listings = [home(1, 1200), flat_with_postcode(2)]
        with patch.dict(os.environ, ENV_ON), patch.object(facts, "_db", side_effect=sqlite3.OperationalError("fixture queue unavailable")):
            watch.Watcher(["rm"]).cycle()
        self.assertEqual([(c, k) for c, k, r in self.sent], [(OWNER, "rm:2")])
        self.assertIn("rm:2", main.load_seen())

    def test_a_sweep_slice_is_not_deferred_by_optional_work(self):
        """re-review: optional lookups used to consume the sweep's send deadline (one match, zero sends)."""
        self.listings = [flat_with_postcode(2)]
        slow = AssertionError("optional work ran inside the slice")
        clock = {"t": 0.0}
        with patch.dict(os.environ, ENV_ON), patch.object(epc, "annotate", side_effect=slow), \
                patch.object(epc, "default", side_effect=slow):
            main._run(["rm"], False, False, rentals=True, watch=True, prefetched=(list(self.listings), []),
                      sweep=True, deadline=0.15, clock=lambda: clock["t"], alert_budget=main.new_alert_budget())
        self.assertEqual([k for c, k, r in self.sent], ["rm:2"])

    def test_the_header_makes_no_extra_download_for_optional_facts(self):
        from pisobot import collage
        calls = []
        with patch.dict(os.environ, {**ENV_ON, "FLOORPLAN_AREA": "1", "ANTHROPIC_API_KEY": "x"}), \
                patch.object(collage.net, "get", side_effect=lambda url, **kw: (calls.append(url), types.SimpleNamespace(content=b"x"))[1]):
            collage._thumb("https://image.invalid/plan.png", floorplan=True)
            collage._thumb("https://image.invalid/plan.png", floorplan=True)
        self.assertEqual(len(calls), 2)                            # exactly the release's behaviour: its own download, every time


# ---------------------------------------------------------------- the step

class StepTests(Base):
    def setUp(self):
        super().setUp()
        self.replies = []

    def send(self, chat_id, message_id, text):
        self.replies.append((chat_id, message_id, text))

    def queue(self, listing, chat=OWNER, message_id=500, now=None):
        row = facts.capture(listing, sent_card(message_id), chat, now)
        facts.enqueue([row])
        return row

    def run_step(self, fake=None, **kw):
        client = make(fake or Fake(BUILDING, CERTS))
        with patch.dict(os.environ, ENV_ON):
            return facts.run_step(NOW, kw.pop("seconds", 30), self.send, epc_client=client, store=client.store, **kw)

    def test_a_fact_goes_out_as_a_threaded_reply_to_the_sent_card(self):
        self.queue(flat_with_postcode(), message_id=777)
        stats = self.run_step()
        self.assertEqual(stats["sent"], 1)
        chat, mid, text = self.replies[0]
        self.assertEqual((chat, mid), (OWNER, 777))
        self.assertIn("50.5 m² (EPC 2021)", text)
        self.assertEqual(facts.pending(NOW), 0)                     # delivered: the row is gone

    def test_one_set_of_lookups_serves_everyone_who_was_told(self):
        fake = Fake(BUILDING, CERTS)
        l = flat_with_postcode()
        self.queue(l, OWNER, 10)
        self.queue(l, FRIEND, 20)
        self.run_step(fake)
        self.assertEqual(sorted((c, m) for c, m, t in self.replies), [(OWNER, 10), (FRIEND, 20)])
        self.assertEqual(sum(1 for c in fake.calls if c[0] == "domestic/search"), 1)

    def test_a_failed_reply_waits_and_each_attempt_is_counted_before_the_work(self):
        self.queue(flat_with_postcode())
        def boom(*a):
            raise RuntimeError("Telegram sendMessage failed (500)")
        with patch.dict(os.environ, ENV_ON):
            client = make(Fake(BUILDING, CERTS))
            for expected in (1, 2, 3):
                stats = facts.run_step(NOW, 30, boom, epc_client=client, store=client.store)
                self.assertEqual(stats["sent"], 0)
                with facts._db() as db:
                    attempts = db.execute("SELECT attempts FROM facts_wanted").fetchone()
                self.assertEqual(attempts["attempts"], expected)
            self.assertEqual(facts.run_step(NOW, 30, boom, epc_client=client, store=client.store)["rows"], 0)   # given up
        self.assertEqual(facts.pending(NOW), 0)

    def test_nothing_to_say_drops_the_row_silently(self):
        l = home(5)
        l.floor_area_m2 = 40.0                                      # portal-stated, no postcode, no plan: only a comparable
        self.queue(l)
        stats = self.run_step()
        self.assertEqual((stats["sent"], stats["dropped"], self.replies), (0, 1, []))
        self.assertEqual(facts.pending(NOW), 0)

    def test_a_hung_lookup_is_abandoned_without_sending_and_the_next_advert_proceeds(self):
        release = threading.Event()
        class Slow(Fake):
            def __call__(inner, url, params, headers):
                if params.get("postcode") == "N7 6DJ":
                    release.wait(5)
                return super().__call__(url, params, headers)
        first = flat_with_postcode(2)
        second = home(3)
        second.floor_area_m2 = 40.0
        self.queue(first, message_id=1)
        self.queue(second, message_id=2)
        started = time.monotonic()
        with patch.object(facts, "ROW_SECONDS", 0.3):
            stats = self.run_step(Slow(BUILDING, CERTS), seconds=5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.replies, [])                          # the abandoned thread never sends
        self.assertEqual(stats["waiting"], 1)
        release.set()
        time.sleep(0.2)
        self.assertEqual(self.replies, [])
        self.assertEqual(facts.pending(NOW), 1)                     # the hung advert waits for the next step

    def test_the_step_budget_leaves_unfinished_rows_waiting(self):
        for i in range(2, 6):
            self.queue(flat_with_postcode(i), message_id=i)
        clock = {"t": 0.0}
        def tick():
            clock["t"] += 10
            return clock["t"]
        with patch.dict(os.environ, ENV_ON):
            client = make(Fake(BUILDING, CERTS))
            stats = facts.run_step(NOW, 25, self.send, clock=tick, epc_client=client, store=client.store)
        self.assertGreater(stats["waiting"], 0)
        self.assertLess(len(self.replies), 4)

    def test_value_is_computed_from_portal_stated_comparables_only(self):
        store = epc.Store(os.path.join(tempfile.mkdtemp(), "v.db"))
        for i in range(1, 10):
            p = peer(i, 40)
            epc.note_portal_area(p, store, NOW)
        mine = flat_with_postcode()
        mine.rent = 1500
        self.queue(mine)
        client = make(Fake(BUILDING, CERTS))
        client.store = store
        with patch.dict(os.environ, ENV_ON):
            facts.run_step(NOW, 30, self.send, epc_client=client, store=store)
        self.assertIn("roomier than", self.replies[0][2])
        self.assertIn("N7", self.replies[0][2])

    def test_the_advert_own_portal_area_becomes_a_comparable_without_epc_or_floorplan_calls(self):
        l = home(8)
        l.address = "Road, London N7"
        l.floor_area_m2 = 44.0
        self.queue(l)
        store = epc.Store(os.path.join(tempfile.mkdtemp(), "c.db"))
        with patch.dict(os.environ, ENV_ON):
            facts.run_step(NOW, 30, self.send, store=store)
        self.assertEqual([r["key"] for r in store.area_peers("N7", 1, NOW)], ["rm:8"])


# ---------------------------------------------------------------- supervision

class SupervisorTests(Base):
    def setUp(self):
        super().setUp()
        self.w = watch.Watcher(["rm"])

    def queue(self):
        facts.enqueue([facts.capture(flat_with_postcode(), sent_card(), OWNER)])

    def test_the_step_is_a_child_process_started_only_when_wanted(self):
        with patch.dict(os.environ, ENV_ON), patch.object(watch.subprocess, "Popen") as popen:
            self.w.start_facts_step()                               # nothing queued
            popen.assert_not_called()
            self.queue()
            self.w.start_facts_step()
        args = popen.call_args.args[0]
        self.assertEqual(args[1:], ["-m", "pisobot.facts"])
        self.assertEqual(popen.call_args.kwargs["cwd"], str(ROOT))

    def test_not_started_when_off_or_already_running(self):
        self.queue()
        with patch.dict(os.environ, {"STORAGE": "local"}, clear=True), patch.object(watch.subprocess, "Popen") as popen:
            self.w.start_facts_step()
            popen.assert_not_called()
        running = types.SimpleNamespace(poll=lambda: None, kill=lambda: None, wait=lambda timeout=None: 0)
        self.w._facts_proc, self.w._facts_started = running, time.monotonic()
        with patch.dict(os.environ, ENV_ON), patch.object(watch.subprocess, "Popen") as popen:
            self.w.start_facts_step()
            popen.assert_not_called()

    def test_a_step_that_outlives_its_budget_is_killed(self):
        killed = []
        proc = types.SimpleNamespace(poll=lambda: None, kill=lambda: killed.append(1), wait=lambda timeout=None: 0)
        self.w._facts_proc, self.w._facts_started = proc, time.monotonic() - facts.STEP_SECONDS - 16
        self.assertIsNone(self.w.reap_facts())
        self.assertEqual(killed, [1])
        self.assertIsNone(self.w._facts_proc)

    def test_a_finished_step_is_collected(self):
        self.w._facts_proc = types.SimpleNamespace(poll=lambda: 0)
        self.assertIsNone(self.w.reap_facts())
        self.assertIsNone(self.w._facts_proc)

    def test_serve_starts_the_step_after_each_cycle_and_never_inside_one(self):
        events, stop = [], threading.Event()
        def cycle(*a, **kw):
            events.append("cycle")
            return {"matches": 0}
        def start():
            events.append("facts")
            stop.set()
        with patch.object(self.w, "cycle", side_effect=cycle), patch.object(self.w, "start_facts_step", side_effect=start):
            self.w.serve(stop, lambda *a: stop.wait())
        self.assertEqual(events, ["cycle", "facts"])

    def test_retention_runs_at_the_start_of_every_cycle_even_when_it_then_fails(self):
        order = []
        with patch.object(facts, "purge_expired", side_effect=lambda *a: order.append("purge")), \
                patch.object(main, "fetch_sources", side_effect=RuntimeError("portal down")), \
                patch.object(main.SOURCES["rm"], "fetch", side_effect=RuntimeError("portal down")):
            with self.assertRaises(Exception):
                self.w.cycle()
        self.assertEqual(order, ["purge"])

    def test_the_real_child_runs_the_queue_without_network(self):
        l = home(8)
        l.address = "Road, London N7"
        l.floor_area_m2 = 44.0                                      # nothing to look up: the row is dropped silently
        facts.enqueue([facts.capture(l, sent_card(), OWNER)])
        env = {**os.environ, "STORAGE": "local", "PISOBOT_STATE_DIR": os.environ["PISOBOT_STATE_DIR"], "EPC_API_TOKEN": "x"}
        done = subprocess.run([sys.executable, "-m", "pisobot.facts"], cwd=str(ROOT), env={**env, "PYTHONPATH": str(ROOT)},
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        self.assertEqual(facts.pending(), 0)
        self.assertIn("facts step", done.stderr)

    def test_the_child_sets_an_address_space_limit_and_does_nothing_when_off(self):
        import resource
        with patch.dict(os.environ, ENV_ON), patch.object(resource, "setrlimit") as limit, \
                patch.object(facts, "run_step", return_value={}) as step:
            facts.main()
        limit.assert_called_once()
        step.assert_called_once()
        with patch.dict(os.environ, {"STORAGE": "local"}, clear=True), patch.object(facts, "run_step") as step:
            self.assertEqual(facts.main(), 0)
        step.assert_not_called()


# ---------------------------------------------------------------- off is off: the release code path, compared

def release_module(name, relative):
    source = subprocess.check_output(["git", "-C", str(ROOT), "show", f"origin/release-archive-pipeline:{relative}"],
                                     text=True, stderr=subprocess.DEVNULL)
    module = types.ModuleType(f"pisobot._release_{name}")
    module.__file__ = str(ROOT / relative)
    module.__package__ = "pisobot"
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def dump(path):
    """Every table's contents with timestamps and random ids masked: the whole database, comparable across runs."""
    out = {}
    with contextlib.closing(sqlite3.connect(path)) as db:
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
            rows = db.execute(f"SELECT * FROM {name}").fetchall()
            masked = sorted(re.sub(r"\d{4}-\d\d-\d\dT[\d:.+\-Z]+|[0-9a-f]{16,}", "~", json.dumps(r, default=str)) for r in rows)
            out[name] = masked
    return out


class OffEquivalenceTests(Base):
    def setUp(self):
        super().setUp()
        for target in (patch.object(main.enrich, "geocode"), patch.object(main.enrich, "assign_crime_percentiles"),
                       patch.object(main, "passes_area", return_value=None), patch.object(main, "_ensure_photos"),
                       patch.object(telegram, "throttle")):
            target.start()
            self.addCleanup(target.stop)
        self.enterContext(patch.object(main, "passes_basic", side_effect=lambda l, **kw: main._cost_reject(l)))
        self.enterContext(patch.object(telegram, "send_text"))

    def scenario(self, watcher_class, pipeline_module):
        """A baseline, a new advert, a price drop and a repeat: same fixtures every time."""
        state = tempfile.mkdtemp()
        sent = []
        listings = [home(1, 1200)]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"PISOBOT_STATE_DIR": state, "STORAGE": "local"}))
            stack.enter_context(patch.object(main, "STATE_DIR", state))
            stack.enter_context(patch.object(main, "SEEN_PATH", str(Path(state) / "seen.json")))
            stack.enter_context(patch.object(cache, "PATH", str(Path(state) / "cache.json")))
            stack.enter_context(patch.object(cache, "_data", {}))
            stack.enter_context(patch.object(main.SOURCES["rm"], "fetch", side_effect=lambda: [home(i, r) for i, r in listings]))
            stack.enter_context(patch.object(telegram, "send_listing", side_effect=lambda l, r: (
                sent.append((users.current_chat(), l.key, r, l.floor_area_m2, l.area_source)), sent_card(1))[1]))
            if pipeline_module is not main:
                for attr in ("STATE_DIR", "SEEN_PATH", "passes_basic", "passes_area", "_ensure_photos"):
                    setattr(pipeline_module, attr, getattr(main, attr))
            stack.enter_context(patch.object(watch, "pipeline", pipeline_module))
            users.register(FRIEND, "Bea", "TheBeatifulest")
            listings[:] = [(1, 1200)]
            watcher = watcher_class(["rm"])
            watcher.cycle()
            listings[:] = [(1, 1200), (2, 1300)]
            watcher.cycle()
            listings[:] = [(1, 1200), (2, 1250)]
            watcher.cycle()
            listings[:] = [(1, 1200), (2, 1250)]
            watcher.cycle()
            seen = json.loads((Path(state) / "seen.json").read_text())
            tables = dump(Path(state) / "pisobot.db")
            mask = lambda x: re.sub(r"\d{4}-\d\d-\d\dT?[\d:.+\-Z]*|[0-9a-f]{16,}", "~", json.dumps(x, sort_keys=True))
            return sent, mask(seen), tables

    def test_with_both_features_off_the_release_path_and_ours_do_the_same_thing(self):
        try:
            release_main = release_module("main", "pisobot/main.py")
            release_watch = release_module("watch", "pisobot/watch.py")
        except Exception:
            self.skipTest("release code not available from git")
        release_watch.pipeline = release_main
        users.register(FRIEND, "Bea", "TheBeatifulest")
        with patch.dict(os.environ, {}, clear=False):
            for name in ("EPC_API_TOKEN", "EPC_TOKEN", "FLOORPLAN_AREA", "ANTHROPIC_API_KEY"):
                os.environ.pop(name, None)
            self.assertFalse(facts.enabled())
            theirs = self.scenario(release_watch.Watcher, release_main)
            ours = self.scenario(watch.Watcher, main)
        self.assertEqual(theirs[0], ours[0])                        # the same alerts, in the same order, with the same card fields
        self.assertEqual(theirs[1], ours[1])                        # the same seen state
        self.assertEqual(theirs[2], ours[2])                        # every table, every row: the same database
        self.assertFalse({t for t in ours[2] if t.startswith(("facts_", "epc_", "floorplan_"))})
        self.assertGreater(len(ours[0]), 2)                         # the scenario really delivered alerts


if __name__ == "__main__":
    unittest.main()
