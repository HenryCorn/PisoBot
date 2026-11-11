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

from pisobot import archive, budget, cache, config, d1, epc, facts, factsdb, localdb, main, sweep, telegram, users, watch
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
        with patch.object(factsdb, "connect", side_effect=AssertionError("capture touched the database")):
            row = facts.capture(l, sent_card(77), OWNER, NOW)
        self.assertEqual((row["chat_id"], row["key"], row["message_id"]), (OWNER, "rm:2", 77))
        payload = json.loads(row["payload"])
        self.assertEqual(payload["full_postcode"], "N7 6DJ")
        self.assertNotIn("images", payload)

    def test_nothing_to_reply_to_or_nothing_to_say_is_not_queued(self):
        l = flat_with_postcode()
        for card in (None, {"result": {}}, {"result": {"message_id": "7"}}):
            self.assertIsNone(facts.capture(l, card, OWNER))
        bare = home(3)
        bare.address = "Somewhere"
        self.assertIsNone(facts.capture(bare, sent_card(), OWNER))
        sale = flat_with_postcode()
        sale.tenure = "sale"
        self.assertIsNone(facts.capture(sale, sent_card(), OWNER))

    def test_defer_swallows_anything_so_it_can_never_cost_a_receipt(self):
        class Bad:
            def __getattr__(self, name):
                raise RuntimeError("boom")
        facts._buffer.clear()
        facts.defer([(Bad(), sent_card()), (None, None)], OWNER)          # no exception escapes
        self.assertEqual(facts._buffer, [])


class QueueTests(Base):
    def setUp(self):
        super().setUp()
        facts._buffer.clear()
        patcher = patch.dict(os.environ, ENV_ON)
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, *rows):
        facts._buffer.extend(rows)
        return facts.flush()

    def test_the_queue_lives_in_its_own_file_never_in_the_main_database(self):
        self.assertEqual(self.put(facts.capture(flat_with_postcode(), sent_card(), OWNER)), 1)
        self.assertTrue(factsdb.exists())
        with sqlite3.connect(localdb.state_dir() / "pisobot.db") as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        self.assertFalse({n for n in names if n.startswith(("facts_", "epc_", "floorplan_"))})
        with factsdb.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM facts_wanted").fetchone()[0], 1)

    def test_flush_is_idempotent_and_pending_counts_live_rows(self):
        row = facts.capture(flat_with_postcode(), sent_card(), OWNER)
        self.assertEqual(self.put(row), 1)
        self.assertEqual(self.put(row), 0)
        self.assertEqual(facts.pending(), 1)

    def test_a_lock_on_the_facts_file_never_delays_the_main_process_writes(self):
        """re-review: a 0.25 s timeout limits waiting, not HOLDING; now the feature's writers live in another file."""
        self.put(facts.capture(flat_with_postcode(), sent_card(), OWNER))
        holder = sqlite3.connect(factsdb.path())
        holder.execute("BEGIN IMMEDIATE")                                  # the child's optional transaction, held for a while
        self.addCleanup(holder.close)
        started = time.monotonic()
        d1.query("INSERT INTO meta (name, value) VALUES ('probe', '1') ON CONFLICT(name) DO UPDATE SET value = excluded.value")
        main_write = time.monotonic() - started
        holder.rollback()
        self.assertLess(main_write, 0.25)

    def test_a_contended_facts_file_skips_the_flush_quickly(self):
        self.put(facts.capture(flat_with_postcode(9), sent_card(9), OWNER))
        holder = sqlite3.connect(factsdb.path())
        holder.execute("BEGIN IMMEDIATE")
        self.addCleanup(holder.close)
        started = time.monotonic()
        facts._buffer.append(facts.capture(flat_with_postcode(2), sent_card(), OWNER))
        written = facts.flush()
        elapsed = time.monotonic() - started
        holder.rollback()
        self.assertEqual(written, 0)
        self.assertLess(elapsed, 1.5)

    def test_flush_writes_nothing_when_off_or_in_the_wrong_mode(self):
        facts._buffer.append(facts.capture(flat_with_postcode(), sent_card(), OWNER))
        with patch.dict(os.environ, {"STORAGE": "local"}, clear=True):
            self.assertEqual(facts.flush(), 0)
        self.assertFalse(factsdb.exists())


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
        facts._buffer.clear()
        watch.Watcher(["rm"]).cycle()                                # baseline (quiet)
        self.sent.clear()
        self.events.clear()

    def _send(self, l, reason):
        self.events.append(("telegram", users.current_chat(), l.key))
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

    def test_an_exception_in_capture_cannot_lose_a_receipt_or_repeat_a_card(self):
        """re-review (15): capture raised after Telegram accepted and before the receipt: the card was sent twice."""
        self.listings = [home(1, 1200), flat_with_postcode(2)]
        with patch.dict(os.environ, ENV_ON), patch.object(facts, "capture", side_effect=RuntimeError("fixture capture bug")):
            watch.Watcher(["rm"]).cycle()
            self.assertIn("rm:2", main.load_seen())                   # the receipt was written
            watch.Watcher(["rm"]).cycle()
        self.assertEqual(sorted(k for c, k, r in self.sent), ["rm:2"])      # and the card was not sent again

    def test_serialisation_and_queueing_happen_after_the_receipt_and_acknowledgement_and_after_every_persons_send(self):
        self.admit({"bills": 100.0})
        watch.Watcher(["rm"]).cycle()                                  # the friend's quiet baseline
        self.sent.clear()
        self.events.clear()
        self.listings = [home(1, 1200), flat_with_postcode(2)]
        real_save, real_verdicts, real_defer, real_flush, real_locked = main.save_seen, archive.record_verdicts, facts.defer, facts.flush, main.locked

        @contextlib.contextmanager
        def locked():
            self.events.append(("lock",))
            with real_locked():
                yield
            self.events.append(("unlock",))

        def save(seen):
            if "rm:2" in seen:
                self.events.append(("receipt", users.current_chat()))
            return real_save(seen)

        def verdicts(*a, **kw):
            if kw.get("acknowledged"):
                self.events.append(("ack",))
            return real_verdicts(*a, **kw)

        def defer(accepted, chat):
            self.events.append(("defer", chat))
            return real_defer(accepted, chat)

        def flush():
            self.events.append(("flush",))
            return real_flush()

        with patch.dict(os.environ, ENV_ON), patch.object(main, "save_seen", side_effect=save), \
                patch.object(archive, "record_verdicts", side_effect=verdicts), patch.object(facts, "defer", side_effect=defer), \
                patch.object(facts, "flush", side_effect=flush), patch.object(main, "locked", side_effect=locked):
            watch.Watcher(["rm"]).cycle()
        order = [e[0] for e in self.events]
        self.assertEqual(order.count("flush"), 1)
        last_send = max(i for i, e in enumerate(self.events) if e[0] == "telegram")
        self.assertGreater(order.index("flush"), last_send)                         # after BOTH people's primary sends
        self.assertGreater(order.index("flush"), order.index("unlock"))             # and after the run lock
        for chat in (OWNER, FRIEND):
            receipt = next(i for i, e in enumerate(self.events) if e == ("receipt", chat))
            defer = next(i for i, e in enumerate(self.events) if e == ("defer", chat))
            self.assertLess(receipt, defer)

    def test_a_dead_facts_file_loses_no_alert_and_fails_nothing(self):
        self.listings = [home(1, 1200), flat_with_postcode(2)]
        with patch.dict(os.environ, ENV_ON), patch.object(factsdb, "connect", side_effect=sqlite3.OperationalError("fixture")):
            watch.Watcher(["rm"]).cycle()
        self.assertEqual([(c, k) for c, k, r in self.sent], [(OWNER, "rm:2")])
        self.assertIn("rm:2", main.load_seen())

    def test_a_sweep_slice_is_not_deferred_by_optional_work(self):
        self.listings = [flat_with_postcode(2)]
        slow = AssertionError("optional work ran inside the slice")
        clock = {"t": 0.0}
        with patch.dict(os.environ, ENV_ON), patch.object(epc, "annotate", side_effect=slow), patch.object(epc, "default", side_effect=slow):
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
        self.assertEqual(len(calls), 2)


class OffAndDryRunTests(AlertPathBase):
    def tree(self):
        return sorted(str(p.relative_to(localdb.state_dir())) for p in Path(localdb.state_dir()).rglob("*") if p.is_file())

    def test_with_the_features_off_no_file_table_or_child_appears(self):
        before = self.tree()
        self.listings = [home(1, 1200), flat_with_postcode(2)]
        with patch.object(watch.subprocess, "Popen", side_effect=AssertionError("a child was started")):
            watch.Watcher(["rm"]).cycle()
        self.assertEqual([f for f in self.tree() if "facts" in f], [])
        self.assertFalse(factsdb.exists())
        self.assertEqual([f for f in self.tree() if f not in before and "facts" in f], [])

    def test_a_dry_run_writes_nothing_even_with_the_features_on(self):
        self.listings = [home(1, 1200), flat_with_postcode(2)]
        with patch.dict(os.environ, ENV_ON), patch.object(watch.subprocess, "Popen", side_effect=AssertionError("child")):
            watch.Watcher(["rm"]).cycle(dry_run=True)
        self.assertFalse(factsdb.exists())
        self.assertEqual(facts._buffer, [])

    def test_a_cycle_does_no_database_maintenance_before_the_alerts(self):
        """re-review (9, 14): three purges used to run, locking, before the pipeline started."""
        with patch.object(epc, "purge_expired", side_effect=AssertionError("purge in the cycle")), \
                patch.object(facts, "_housekeeping", side_effect=AssertionError("housekeeping in the cycle")):
            watch.Watcher(["rm"]).cycle()
            watch.Watcher(["rm"]).cycle(dry_run=True)

    def test_importing_the_feature_modules_does_no_work(self):
        state = tempfile.mkdtemp()
        code = ("import threading, sys\nfrom pisobot import facts, factsdb, epc, watch, main, telegram\n"
                "print(threading.active_count())\n")
        done = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=60,
                              env={**os.environ, "PISOBOT_STATE_DIR": state, "STORAGE": "local", "PYTHONPATH": str(ROOT)})
        self.assertEqual(done.returncode, 0, done.stderr[-400:])
        self.assertEqual(done.stdout.strip(), "1")
        self.assertEqual(os.listdir(state), [])                                    # nothing created at import


# ---------------------------------------------------------------- the step

class StepTests(Base):
    def setUp(self):
        super().setUp()
        self.replies = []
        facts._buffer.clear()
        patcher = patch.dict(os.environ, ENV_ON)
        patcher.start()
        self.addCleanup(patcher.stop)

    def send(self, chat_id, message_id, text):
        self.replies.append((chat_id, message_id, text))

    def queue(self, listing, chat=OWNER, message_id=500, now=None):
        row = facts.capture(listing, sent_card(message_id), chat, now)
        facts._buffer.append(row)
        facts.flush()
        return row

    def rows(self):
        with factsdb.connect() as db:
            return [dict(r) for r in db.execute("SELECT chat_id, key, message_id, state, attempts FROM facts_wanted ORDER BY id")]

    def run_step(self, fake=None, **kw):
        client = make(fake or Fake(BUILDING, CERTS))
        kw.setdefault("active", lambda chat: True)
        kw.setdefault("hidden", lambda *a: False)
        return facts.run_step(kw.pop("now", NOW), kw.pop("seconds", 30), kw.pop("send", self.send), epc_client=client, store=client.store, **kw)

    def test_a_fact_goes_out_as_a_threaded_reply_to_the_sent_card(self):
        self.queue(flat_with_postcode(), message_id=777)
        stats = self.run_step()
        self.assertEqual(stats["sent"], 1)
        chat, mid, text = self.replies[0]
        self.assertEqual((chat, mid), (OWNER, 777))
        self.assertIn("50.5 m² (EPC 2021)", text)
        self.assertEqual(facts.pending(NOW), 0)

    def test_each_recipient_gets_their_own_reply_built_from_their_own_card(self):
        """re-review (5): one person's snapshot used to be applied to the other's reply."""
        a, b = flat_with_postcode(), flat_with_postcode()
        a.rent, b.rent = 1200, 1600
        a.floor_area_m2 = b.floor_area_m2 = None
        store = epc.Store(os.path.join(tempfile.mkdtemp(), "v.db"))
        for i in range(1, 10):
            epc.note_portal_area(peer(i, 40), store, NOW)
        self.queue(a, OWNER, 10)
        self.queue(b, FRIEND, 20)
        client = make(Fake(BUILDING, CERTS))
        client.store = store
        facts.run_step(NOW, 30, self.send, epc_client=client, store=store, active=lambda c: True, hidden=lambda *x: False)
        by_chat = {c: t for c, m, t in self.replies}
        self.assertEqual(set(by_chat), {OWNER, FRIEND})
        self.assertIn("£23.8/m²", by_chat[OWNER])        # £1,200 for 50.5 m²
        self.assertIn("£31.7/m²", by_chat[FRIEND])       # £1,600 for 50.5 m²

    def test_the_expensive_lookup_is_shared_between_recipients_by_identical_input(self):
        fake = Fake(BUILDING, CERTS)
        l = flat_with_postcode()
        self.queue(l, OWNER, 10)
        self.queue(l, FRIEND, 20)
        self.run_step(fake)
        self.assertEqual(sorted((c, m) for c, m, t in self.replies), [(OWNER, 10), (FRIEND, 20)])
        self.assertEqual(sum(1 for c in fake.calls if c[0] == "domestic/search"), 1)

    def test_an_attempt_is_counted_only_when_a_lookup_actually_starts(self):
        """re-review (4): four prefetched rows burned all three attempts while only the first was ever computed."""
        for i in range(2, 6):
            self.queue(flat_with_postcode(i), message_id=i)
        clock = {"t": 0.0}
        def tick():
            clock["t"] += 10                      # the first lookup uses up the 15 s budget
            return clock["t"]
        for _ in range(3):
            clock["t"] = 0.0
            client = make(Fake(BUILDING, CERTS))
            facts.run_step(NOW, 15, lambda *a: None, clock=tick, epc_client=client, store=client.store, active=lambda c: True,
                           hidden=lambda *a: False)
        attempts = {r["message_id"]: r["attempts"] for r in self.rows()} if self.rows() else {}
        self.assertTrue(all(v <= 1 for k, v in attempts.items() if k != 2) or attempts == {})
        waiting = [r for r in self.rows() if r["state"] == "wanted"]
        self.assertTrue(all(r["attempts"] == 0 for r in waiting))          # rows the budget never reached are uncharged
        self.assertEqual(facts.pending(NOW), len(waiting))

    def test_a_failed_reply_goes_back_to_wanted_and_is_given_up_after_three_attempts(self):
        self.queue(flat_with_postcode())
        def boom(*a):
            raise RuntimeError("Telegram sendMessage failed (500)")
        for expected in (1, 2, 3):
            self.run_step(send=boom)
            row = self.rows()[0]
            self.assertEqual((row["state"], row["attempts"]), ("wanted", expected))
        self.assertEqual(self.run_step(send=boom)["rows"], 0)
        self.assertEqual(facts.pending(NOW), 0)

    def test_a_reply_is_recorded_with_its_row_in_one_transaction_and_a_retry_never_reposts(self):
        """re-review (1): the delete failed after a successful post and the next step posted it again."""
        self.queue(flat_with_postcode(), message_id=5)
        def lock_record(row, now):
            raise sqlite3.OperationalError("database is locked")
        with patch.object(facts, "_record_sent", side_effect=lock_record):
            first = self.run_step()
        self.assertEqual(first["sent"], 1)
        self.assertEqual(self.rows()[0]["state"], "sending")                 # marked BEFORE posting
        later = NOW + dt.timedelta(minutes=10)                                  # the lease expires: abandoned, never reposted
        self.run_step(now=later)
        self.assertEqual(len(self.replies), 1)
        self.assertEqual(facts.pending(later), 0)

    def test_a_kill_while_posting_the_second_reply_does_not_repeat_the_first(self):
        """re-review (2)."""
        l = flat_with_postcode()
        self.queue(l, OWNER, 10)
        self.queue(l, FRIEND, 20)
        def kill_on_second(chat, mid, text):
            if chat == FRIEND:
                raise KeyboardInterrupt("killed while posting")
            self.replies.append((chat, mid, text))
        with self.assertRaises(KeyboardInterrupt):
            self.run_step(send=kill_on_second)
        self.assertEqual([r["state"] for r in self.rows()], ["done", "sending"])
        self.run_step(now=NOW + dt.timedelta(minutes=10))
        self.assertEqual([c for c, m, t in self.replies], [OWNER])              # the first was recorded at once; never repeated

    def test_two_steps_cannot_claim_and_reply_to_the_same_row(self):
        """re-review (3)."""
        self.queue(flat_with_postcode(), message_id=5)
        barrier = threading.Barrier(2)
        client = make(Fake(BUILDING, CERTS))
        real_claim = facts._claim
        def claim(row_id, now):
            barrier.wait(timeout=3)
            return real_claim(row_id, now)
        results = []
        def step():
            results.append(facts.run_step(NOW, 20, self.send, epc_client=client, store=client.store, active=lambda c: True,
                                          hidden=lambda *a: False))
        with patch.object(facts, "_claim", side_effect=claim):
            threads = [threading.Thread(target=step) for _ in range(2)]
            [t.start() for t in threads]
            [t.join(10) for t in threads]
        self.assertEqual(len(self.replies), 1)

    def test_only_one_child_step_runs_at_a_time(self):
        import fcntl
        self.queue(flat_with_postcode())
        holder = open(factsdb.path().with_suffix(".lock"), "w")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX)
        with patch.object(facts, "run_step", side_effect=AssertionError("a second step ran")):
            self.assertEqual(facts.main(), 0)

    def test_a_removed_person_and_a_hidden_home_get_no_reply(self):
        """re-review (6): the queued fact was still posted after the person left or hid the advert."""
        friend = self.admit_friend()
        self.queue(flat_with_postcode(2), FRIEND, 1)
        self.queue(flat_with_postcode(3), OWNER, 2)
        users.remove(FRIEND)
        with users.use(users.owner()):
            d1.query("INSERT OR IGNORE INTO hidden (key) VALUES ('rm:3')")
        client = make(Fake(BUILDING, CERTS))
        stats = facts.run_step(NOW, 20, self.send, epc_client=client, store=client.store)          # the REAL read-only checks
        self.assertEqual(self.replies, [])
        self.assertEqual(stats["dropped"], 2)

    def test_a_person_who_is_still_active_and_has_not_hidden_it_gets_the_reply(self):
        d1.query("INSERT INTO meta (name, value) VALUES ('probe', '1') ON CONFLICT(name) DO UPDATE SET value = excluded.value")
        self.queue(flat_with_postcode(2), OWNER, 1)
        client = make(Fake(BUILDING, CERTS))
        facts.run_step(NOW, 20, self.send, epc_client=client, store=client.store)
        self.assertEqual(len(self.replies), 1)

    def test_when_the_checks_are_unknown_no_reply_is_sent_and_nothing_is_charged(self):
        self.queue(flat_with_postcode(2), OWNER, 1)
        stats = self.run_step(active=lambda c: None)
        self.assertEqual((self.replies, stats["waiting"]), ([], 1))
        self.assertEqual(self.rows()[0]["attempts"], 0)
        self.run_step(hidden=lambda *a: None)
        self.assertEqual(self.replies, [])

    def test_the_checks_are_read_only_and_short(self):
        d1.query("INSERT INTO meta (name, value) VALUES ('probe', '1') ON CONFLICT(name) DO UPDATE SET value = excluded.value")
        with patch.object(factsdb, "read_only", wraps=factsdb.read_only) as ro:
            self.assertTrue(facts.person_active(OWNER))
            self.assertFalse(facts.home_hidden(OWNER, "rm:1", None, None))
        self.assertEqual(ro.call_count, 1)
        self.assertEqual(factsdb.BUSY_SECONDS, 0.25)
        with factsdb.read_only(localdb.state_dir() / "pisobot.db") as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM meta")

    def test_nothing_to_say_drops_the_row_silently(self):
        l = home(5)
        l.floor_area_m2 = 40.0
        self.queue(l)
        stats = self.run_step()
        self.assertEqual((stats["sent"], stats["dropped"], self.replies), (0, 1, []))

    def test_a_hung_lookup_is_abandoned_without_sending_and_the_next_advert_proceeds(self):
        release = threading.Event()
        class Slow(Fake):
            def __call__(inner, url, params, headers):
                if params.get("postcode") == "N7 6DJ":
                    release.wait(5)
                return super().__call__(url, params, headers)
        second = home(3)
        second.floor_area_m2 = 40.0
        self.queue(flat_with_postcode(2), message_id=1)
        self.queue(second, message_id=2)
        started = time.monotonic()
        with patch.object(facts, "ROW_SECONDS", 0.3):
            stats = self.run_step(Slow(BUILDING, CERTS), seconds=5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.replies, [])
        release.set()
        time.sleep(0.2)
        self.assertEqual(self.replies, [])
        self.assertEqual([r["state"] for r in self.rows() if r["message_id"] == 1], ["wanted"])

    def test_value_is_computed_from_portal_stated_comparables_only(self):
        store = epc.Store(os.path.join(tempfile.mkdtemp(), "v.db"))
        for i in range(1, 10):
            epc.note_portal_area(peer(i, 40), store, NOW)
        mine = flat_with_postcode()
        mine.rent = 1500
        self.queue(mine)
        client = make(Fake(BUILDING, CERTS))
        client.store = store
        facts.run_step(NOW, 30, self.send, epc_client=client, store=store, active=lambda c: True, hidden=lambda *a: False)
        self.assertIn("roomier than", self.replies[0][2])

    def test_the_advert_own_portal_area_becomes_a_comparable(self):
        l = home(8)
        l.address = "Road, London N7"
        l.floor_area_m2 = 44.0
        self.queue(l)
        store = epc.Store(os.path.join(tempfile.mkdtemp(), "c.db"))
        facts.run_step(NOW, 30, self.send, store=store, active=lambda c: True, hidden=lambda *a: False)
        self.assertEqual([r["key"] for r in store.area_peers("N7", 1, NOW)], ["rm:8"])

    def admit_friend(self):
        return users.register(FRIEND, "Bea", "TheBeatifulest")


# ---------------------------------------------------------------- supervision

class SupervisorTests(Base):
    def setUp(self):
        super().setUp()
        self.w = watch.Watcher(["rm"])
        facts._buffer.clear()
        patcher = patch.dict(os.environ, ENV_ON)
        patcher.start()
        self.addCleanup(patcher.stop)

    def queue(self):
        facts._buffer.append(facts.capture(flat_with_postcode(), sent_card(), OWNER))
        facts.flush()

    def test_the_step_is_a_child_process_started_only_when_wanted_and_has_its_own_kill_timer(self):
        timers = []
        class FakeTimer:
            daemon = False
            def __init__(self, interval, fn, args=()):
                timers.append((interval, fn, args))
            def start(self):
                pass
        with patch.object(watch.subprocess, "Popen") as popen, patch.object(watch.threading, "Timer", FakeTimer):
            self.w.start_facts_step()                               # nothing queued
            popen.assert_not_called()
            self.queue()
            self.w.start_facts_step()
        self.assertEqual(popen.call_args.args[0][1:], ["-m", "pisobot.facts"])
        self.assertEqual(popen.call_args.kwargs["cwd"], str(ROOT))
        self.assertEqual(timers[0][0], facts.STEP_SECONDS + facts.KILL_GRACE_SECONDS)
        self.assertEqual(timers[0][1], watch.Watcher._kill_facts)

    def test_the_timer_kills_an_overdue_child_whatever_the_watcher_is_doing(self):
        killed = []
        proc = types.SimpleNamespace(poll=lambda: None, kill=lambda: killed.append(1))
        watch.Watcher._kill_facts(proc)
        self.assertEqual(killed, [1])
        watch.Watcher._kill_facts(types.SimpleNamespace(poll=lambda: 0, kill=lambda: killed.append(2)))
        self.assertEqual(killed, [1])

    def test_an_overdue_child_is_killed_at_the_start_and_the_end_of_every_cycle(self):
        """re-review (7): a long cycle used to keep the supervisor from enforcing the child's deadline."""
        killed, stage = [], []
        proc = types.SimpleNamespace(poll=lambda: None, kill=lambda: killed.append(len(stage)), wait=lambda timeout=None: 0)
        self.w._facts_proc, self.w._facts_started = proc, time.monotonic() - facts.STEP_SECONDS - facts.KILL_GRACE_SECONDS - 1
        with patch.object(self.w, "_cycle", side_effect=lambda *a: stage.append("cycle") or {"matches": 0}):
            self.w.cycle()
        self.assertEqual(killed, [0])                                           # killed before the cycle began
        proc2 = types.SimpleNamespace(poll=lambda: None, kill=lambda: killed.append("end"), wait=lambda timeout=None: 0)
        def long_cycle(*a):
            self.w._facts_proc = proc2
            self.w._facts_started = time.monotonic() - 1000
            return {"matches": 0}
        with patch.object(self.w, "_cycle", side_effect=long_cycle):
            self.w.cycle()
        self.assertEqual(killed, [0, "end"])                                    # and again when the cycle ends

    def test_not_started_when_off_or_already_running(self):
        self.queue()
        with patch.dict(os.environ, {"STORAGE": "local"}, clear=True), patch.object(watch.subprocess, "Popen") as popen:
            self.w.start_facts_step()
            popen.assert_not_called()
        running = types.SimpleNamespace(poll=lambda: None, kill=lambda: None, wait=lambda timeout=None: 0)
        self.w._facts_proc, self.w._facts_started = running, time.monotonic()
        with patch.object(watch.subprocess, "Popen") as popen:
            self.w.start_facts_step()
            popen.assert_not_called()

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

    def test_the_real_child_runs_the_queue_without_network(self):
        l = home(8)
        l.address = "Road, London N7"
        l.floor_area_m2 = 44.0                                      # nothing to look up: the row is dropped silently
        d1.query("INSERT INTO meta (name, value) VALUES ('probe', '1') ON CONFLICT(name) DO UPDATE SET value = excluded.value")
        facts._buffer.append(facts.capture(l, sent_card(), OWNER))
        facts.flush()
        env = {**os.environ, "STORAGE": "local", "PISOBOT_STATE_DIR": os.environ["PISOBOT_STATE_DIR"], "EPC_API_TOKEN": "x",
               "TELEGRAM_CHAT_ID": OWNER, "PYTHONPATH": str(ROOT), "FACTS_ALLOW_NO_LIMIT": "1"}
        done = subprocess.run([sys.executable, "-m", "pisobot.facts"], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        self.assertEqual(facts.pending(), 0)
        self.assertIn("facts step", done.stderr)

    def test_the_child_has_a_watchdog_and_limits_and_refuses_to_run_without_them(self):
        import resource, signal
        with patch.object(resource, "setrlimit") as limit, patch.object(signal, "alarm") as alarm, \
                patch.object(facts, "run_step", return_value={}) as step:
            self.queue()
            self.assertEqual(facts.main(), 0)
        self.assertEqual(limit.call_count, 2)                       # address space and CPU
        alarm.assert_called_once()
        self.assertLess(alarm.call_args.args[0], facts.STEP_SECONDS + facts.KILL_GRACE_SECONDS)
        step.assert_called_once()
        with patch.object(resource, "setrlimit", side_effect=ValueError("not supported")), patch.object(signal, "alarm"), \
                patch.object(facts, "run_step") as step:
            self.assertEqual(facts.main(), 3)                       # re-review (12): a failed limit used to be ignored
        step.assert_not_called()
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
