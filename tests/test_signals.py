"""signals.py (G3 v2): pure detectors, the archive-backed history, the send-path rule, the post-send log, off-equals-release."""
import collections
import contextlib
import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from pisobot import archive, cache, config, d1, localdb, main, net, signals, telegram, users, watch
from pisobot.models import Listing

from test_users import Base, FRIEND, OWNER, home

ROOT = Path(__file__).resolve().parents[1]
TODAY = dt.date(2026, 11, 10)
NOW = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)
CENTRE = (51.5, -0.1)
SCOPE = {"centre": list(CENTRE), "min_rent": 0, "max_rent": 5000, "min_beds": 0, "miles": 10}


def at(days_ago, hours=0):
    return (NOW - dt.timedelta(days=days_ago, hours=hours)).isoformat(timespec="seconds")


def flat(i=1, rent=1500, beds=1, lat=51.5, lng=-0.1, source="rm", **kw):
    kw.setdefault("images", [f"https://m/{i}_{n}.jpg" for n in range(8)])
    kw.setdefault("floorplan", "https://m/plan.png")
    kw.setdefault("description", "A bright one bedroom flat close to the station with a large living room. " * 6)
    return Listing(source=source, id=str(i), url=f"https://{source}/{i}", address="Road, London N7", bedrooms=beds,
                   scheme="Private rent", tenure="rent", rent=rent, lat=lat, lng=lng, **kw)


# ---------------------------------------------------------------- pure detectors

class UnderPresentedTests(unittest.TestCase):
    def test_well_presented_flat_is_quiet(self):
        self.assertIsNone(signals.under_presented(flat()))

    def test_one_shortfall_is_not_enough(self):
        self.assertIsNone(signals.under_presented(flat(floorplan=None)))

    def test_two_and_three_shortfalls(self):
        s = signals.under_presented(flat(floorplan=None, images=["a", "b"]))
        self.assertEqual((s.name, s.strength), ("under_presented", 2))
        self.assertEqual(signals.under_presented(flat(floorplan=None, images=["a"], description="Nice flat.")).strength, 3)

    def test_studio_that_is_a_one_bed_uses_the_portal_area(self):
        s = signals.under_presented(flat(studio=True, floor_area_m2=44, description="Studio. One bedroom flat. " * 20))
        self.assertEqual(s.strength, 3)
        self.assertIn("(portal)", s.evidence)
        self.assertIsNone(signals.under_presented(flat(studio=True, floor_area_m2=24, description="Studio flat. " * 30)))

    def test_two_bed_in_tiny_area(self):
        self.assertIn("2 bedrooms in only 33 m²", signals.under_presented(flat(beds=2, floor_area_m2=33)).evidence)

    def test_the_only_area_read_is_the_stated_one_and_the_extension_point_is_marked(self):
        self.assertIsNone(signals.stated_area(flat()))
        self.assertEqual(signals.stated_area(flat(floor_area_m2=45.0)), 45.0)
        import inspect
        self.assertIn("EXTENSION POINT", inspect.getsource(signals.stated_area))
        source = inspect.getsource(signals)
        self.assertEqual(source.count("floor_area_m2"), 1)             # nothing else in the module reads an area directly


class ReducedStaleTests(unittest.TestCase):
    def test_fresh_unreduced_flat_is_quiet(self):
        self.assertIsNone(signals.reduced_stale(flat(first_listed="2026-11-08"), today=TODAY))

    def test_portal_reduced_and_the_labelled_idea(self):
        s = signals.reduced_stale(flat(update_reason="reduced", update_date="2026-11-09"), today=TODAY)
        self.assertIn("portal says reduced 1 days ago", s.evidence)
        self.assertIn("offers above asking are not allowed", s.idea)

    def test_stale_after_21_days_uses_the_archive_date_when_the_listing_has_none(self):
        self.assertIsNone(signals.reduced_stale(flat(first_listed="2026-10-20"), today=TODAY))
        self.assertIn("22 days", signals.reduced_stale(flat(first_listed="2026-10-19"), today=TODAY).evidence)
        self.assertIn("22 days", signals.reduced_stale(flat(), today=TODAY, first_listed="2026-10-19").evidence)

    def test_cuts_from_observations_ignore_unseen_rows(self):
        rows = [{"at": at(40), "rent": 1650, "status": "active"}, {"at": at(30), "rent": 1600, "status": "active"},
                {"at": at(20), "rent": 1600, "status": "unseen"}, {"at": at(10), "rent": 1500, "status": "active"}]
        s = signals.reduced_stale(flat(rent=1500, first_listed="2026-10-01"), rows, TODAY)
        self.assertIn("2 cuts seen, £150 off in total (£1,650 to £1,500)", s.evidence)
        self.assertIn("5% under", s.idea)

    def test_idea_is_never_above_asking(self):
        self.assertIn("about £1,420", signals.reduced_stale(flat(rent=1500, first_listed="2026-09-01"), today=TODAY).idea)


class BackOnMarketTests(unittest.TestCase):
    def rows(self, *items):
        return [{"at": a, "rent": 1500, "status": s} for a, s in items]

    def test_unseen_then_active_is_back_on_the_market(self):
        s = signals.back_on_market(flat(), self.rows((at(20), "active"), (at(12), "unseen"), (at(2), "active")), TODAY)
        self.assertEqual((s.name, s.strength), ("back_on_market", 3))
        self.assertIn("absent from 2+ complete sweeps", s.evidence)
        self.assertNotIn("let agreed", s.evidence.lower())                      # never claims what the release does not record
        self.assertNotIn("fell through", s.evidence.lower())

    def test_a_short_absence_is_medium(self):
        s = signals.back_on_market(flat(), self.rows((at(20), "active"), (at(3), "unseen"), (at(2), "active")), TODAY)
        self.assertEqual(s.strength, 2)

    def test_the_other_transitions_are_not_back_on_the_market(self):
        for rows in ([(at(5), "active")], [(at(5), "active"), (at(3), "active")],            # first sight, rent change
                     [(at(5), "active"), (at(3), "unseen")],                                  # still unseen
                     [(at(60), "active"), (at(50), "unseen"), (at(45), "active")],          # returned long ago
                     [(at(5), "unseen"), (at(4), "active")][:1]):
            self.assertIsNone(signals.back_on_market(flat(), self.rows(*rows), TODAY), rows)
        self.assertIsNone(signals.back_on_market(flat(), [], TODAY))


class SameFlatTests(unittest.TestCase):
    def test_shared_photo_points_to_the_cheaper_one(self):
        a = flat(1, rent=1600, images=["https://a/x/p1.jpg"] + ["https://a/q%d.jpg" % i for i in range(5)])
        b = flat(2, rent=1500, images=["https://b/y/p1.jpg"] + ["https://b/r%d.jpg" % i for i in range(5)], agent="Other Agents")
        s = signals.same_flat_two_prices(a, [b])
        self.assertEqual((s.label, s.strength), ("Probably also listed £100/mo cheaper", 3))
        self.assertIn("Other Agents", s.evidence)
        self.assertEqual(signals.same_flat_two_prices(b, [a]).label, "Cheapest of 2 adverts for this flat")

    def test_same_portal_area_and_text_is_medium_and_needs_both_areas(self):
        text = "Spacious modern apartment with balcony and concierge near station"
        a = flat(1, rent=1600, floor_area_m2=50.0, description=text, images=["a1"])
        b = flat(2, rent=1500, floor_area_m2=50.2, description=text + " available now", images=["b1"])
        self.assertEqual(signals.same_flat_two_prices(a, [b]).strength, 2)
        b.floor_area_m2 = None
        self.assertIsNone(signals.same_flat_two_prices(a, [b]))

    def test_identical_units_with_different_text_far_other_beds_or_same_price_are_ignored(self):
        a = flat(1, rent=1600, floor_area_m2=50.0, description="Brand new tower block flat with gym", images=["a1"])
        for other in (flat(2, rent=1500, floor_area_m2=50.0, description="Victorian conversion garden views quiet", images=["b1"]),
                      flat(3, rent=1500, lat=51.502, images=["a1"]), flat(4, rent=1500, beds=2, images=["a1"]),
                      flat(5, rent=1610, images=["a1"])):
            self.assertIsNone(signals.same_flat_two_prices(a, [other]))


class SelectionTests(unittest.TestCase):
    def test_at_most_two_strongest_first(self):
        l = flat(1, floorplan=None, images=["a"], description="short", first_listed="2026-09-01")
        rows = [{"at": at(20), "rent": 1500, "status": "active"}, {"at": at(9), "rent": 1500, "status": "unseen"},
                {"at": at(1), "rent": 1500, "status": "active"}]
        reader = types.SimpleNamespace(history=lambda key: rows, first_listed=lambda key: None, neighbours=lambda l: [])
        out = signals.compute(l, reader, TODAY)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].name, "back_on_market")
        self.assertGreaterEqual(len(signals.compute_all(l, reader, TODAY)), 3)

    def test_sale_listing_gets_nothing(self):
        l = flat()
        l.tenure = "sale"
        self.assertEqual(signals.compute(l, None, TODAY), [])

    def test_card_line(self):
        l = flat()
        self.assertEqual(signals.card_line(l), "")
        l.signals = [{"name": "reduced_stale", "label": "Long on the market", "evidence": "x", "strength": 2, "idea": "i"}]
        self.assertEqual(signals.card_line(l), "🔎 Long on the market")
        self.assertEqual(signals.card_ideas(l), ["i"])


# ---------------------------------------------------------------- the real archive (the release's own functions write it)

class ArchiveBase(Base):
    def advance(self, listing, days_ago, hours=0, via="head"):
        archive.upsert([listing], via, at=at(days_ago, hours))

    def sweeps_miss(self, days_ago, count=2):
        """`count` complete sweeps, started after the advert was last seen, none of which reached it."""
        for n in range(count):
            started = at(days_ago - n * 0.5)
            sweep_id = archive.start_sweep("rm", SCOPE, {}, at=started)
            archive.finish_sweep(sweep_id, True, at=at(days_ago - n * 0.5, -1))


class RealHistoryTests(ArchiveBase):
    def test_the_releases_unseen_and_return_transitions_give_back_on_the_market(self):
        l = flat(1)
        self.advance(l, 20)
        self.sweeps_miss(15)                                          # absent from two complete sweeps -> 'unseen'
        row = archive.history(l.key)
        self.assertEqual([r["status"] for r in row], ["active", "unseen"])
        self.advance(l, 2)                                            # listed again: the release reactivates it
        self.assertEqual([r["status"] for r in archive.history(l.key)], ["active", "unseen", "active"])
        with signals.ArchiveReader() as reader:
            found = signals.compute_all(l, reader, TODAY)
        names = [s.name for s in found]
        self.assertIn("back_on_market", names)
        back = next(s for s in found if s.name == "back_on_market")
        self.assertIn("absent from 2+ complete sweeps", back.evidence)

    def test_one_missed_sweep_is_not_enough_for_the_release_to_mark_it_unseen(self):
        l = flat(1)
        self.advance(l, 20)
        self.sweeps_miss(15, count=1)
        self.advance(l, 2)
        with signals.ArchiveReader() as reader:
            self.assertNotIn("back_on_market", [s.name for s in signals.compute_all(l, reader, TODAY)])

    def test_rent_cuts_and_the_portal_date_come_from_the_archive(self):
        l = flat(1, rent=1650)
        self.advance(l, 40)
        l.rent = 1600
        self.advance(l, 25)
        l.rent = 1500
        self.advance(l, 5)
        stored = flat(1, rent=1500)                                    # the live listing carries no portal date of its own
        archive_row = archive._db  # noqa: F841
        with archive._db() as db:
            db.execute("UPDATE adverts SET portal_first_listed = ? WHERE key = ?", ("2026-09-20", l.key))
        with signals.ArchiveReader() as reader:
            found = {s.name: s for s in signals.compute_all(stored, reader, TODAY)}
        s = found["reduced_stale"]
        self.assertIn("2 cuts seen", s.evidence)
        self.assertIn("first listed 51 days ago", s.evidence)
        self.assertEqual(s.strength, 3)

    def test_same_flat_looks_across_the_active_archive_not_only_the_current_scan(self):
        shared = ["https://a/x/p1.jpg", "https://a/q1.jpg", "https://a/q2.jpg", "https://a/q3.jpg"]
        other = flat(2, rent=1500, images=["https://b/y/p1.jpg"] + shared[1:3] + ["https://b/z.jpg"], lat=51.50005, source="otmr", agent="Other")
        self.advance(other, 3)
        far = flat(3, rent=1400, images=["https://c/y/p1.jpg"], lat=51.51)                                  # 1 km away
        self.advance(far, 3)
        gone = flat(4, rent=1300, images=["https://d/y/p1.jpg"], lat=51.50002)
        self.advance(gone, 20)
        self.sweeps_miss(15)
        mine = flat(1, rent=1600, images=["https://a/x/p1.jpg", "https://a/q9.jpg"] + shared[2:], lat=51.5)
        self.advance(mine, 1)
        with signals.ArchiveReader() as reader:
            near = [n.key for n in reader.neighbours(mine)]
            found = {s.name: s for s in signals.compute_all(mine, reader, TODAY)}
        self.assertEqual(near, ["otmr:2"])                              # not the far one, not the unseen one, not itself
        self.assertEqual(found["same_flat_two_prices"].label, "Probably also listed £100/mo cheaper")
        self.assertIn("otmr:2", found["same_flat_two_prices"].evidence)

    def test_the_neighbour_query_is_one_bounded_index_assisted_query_per_source(self):
        with archive._db() as db:
            rows = [(f"rm:{i}", "rm", "u", "a", 1, 1500, 51.4 + (i % 100) * 0.002, -0.2 + (i // 100) * 0.002,
                     at(1), at(1), None, "active", 1, None, 0, json.dumps({"source": "rm", "id": str(i)})) for i in range(6000)]
            db.executemany("INSERT INTO adverts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        probe = flat(1, lat=51.5, lng=-0.1)
        with signals.ArchiveReader() as reader:
            plan = " ".join(str(tuple(r)) for r in reader.db.execute(
                "EXPLAIN QUERY PLAN SELECT key FROM adverts WHERE source = 'rm' AND status = 'active' AND bedrooms = 1 "
                "AND lat BETWEEN 1 AND 2 AND lng BETWEEN 1 AND 2 LIMIT 50"))
            started = time.monotonic()
            reader.neighbours(probe)
            reader.history(probe.key)
            elapsed = time.monotonic() - started
        self.assertIn("adverts_source_status", plan)
        self.assertLess(elapsed, 0.25)

    def test_the_reader_is_read_only_and_sees_no_write_lock(self):
        self.advance(flat(1), 3)
        writer = sqlite3.connect(localdb.state_dir() / "pisobot.db")
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")                               # another process is writing
        with signals.ArchiveReader() as reader:
            started = time.monotonic()
            self.assertEqual(len(reader.history("rm:1")), 1)
            self.assertLess(time.monotonic() - started, 0.5)
            with self.assertRaises(sqlite3.OperationalError):
                reader.db.execute("DELETE FROM observations")
        writer.rollback()

    def test_a_missing_database_means_no_signals_not_an_error(self):
        shutil.rmtree(localdb.state_dir(), ignore_errors=True)
        with patch.dict(os.environ, {"SIGNALS": "1"}):
            self.assertEqual(signals.annotate([flat()]), 0)


# ---------------------------------------------------------------- the send-path rule

class SendPathTests(ArchiveBase):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, {"SIGNALS": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_computing_signals_makes_no_network_call_of_any_kind(self):
        thin = flat(1, floorplan=None, images=["a"], description="Nice flat.")
        self.advance(thin, 30)
        forbid = AssertionError("network used while computing signals")
        with patch.object(socket.socket, "connect", side_effect=forbid), patch.object(socket, "create_connection", side_effect=forbid), \
                patch.object(requests.Session, "request", side_effect=forbid), patch.object(net, "page", side_effect=forbid), \
                patch.object(net, "get", side_effect=forbid), patch.object(net, "post", side_effect=forbid):
            self.assertEqual(signals.annotate([thin], today=TODAY), 1)
        self.assertTrue(thin.signals)

    def test_nothing_ever_raises_into_the_send_path(self):
        l = flat(1, floorplan=None, images=["a"], description="Nice flat.")
        for factory in (lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")), lambda: (_ for _ in ()).throw(KeyboardInterrupt) if False else None):
            self.assertEqual(signals.annotate([l], reader_factory=factory), 0)
        broken = types.SimpleNamespace(history=lambda k: 1 / 0, first_listed=lambda k: 1 / 0, neighbours=lambda l: 1 / 0)
        self.assertEqual([s.name for s in signals.compute(l, broken, TODAY)], ["under_presented"])      # the pure detector still works
        with patch.object(signals, "under_presented", side_effect=RuntimeError("detector bug")):
            self.assertEqual(signals.compute(l, broken, TODAY), [])

    def test_a_failing_signals_layer_loses_no_alert(self):
        for target in (patch.object(main.enrich, "geocode"), patch.object(main.enrich, "assign_crime_percentiles"),
                       patch.object(main, "passes_area", return_value=None), patch.object(main, "_ensure_photos"),
                       patch.object(telegram, "throttle"), patch.object(telegram, "send_text")):
            target.start()
            self.addCleanup(target.stop)
        self.enterContext(patch.object(main, "passes_basic", side_effect=lambda l, **kw: main._cost_reject(l)))
        listings = [home(1, 1200)]
        self.enterContext(patch.object(main.SOURCES["rm"], "fetch", side_effect=lambda: list(listings)))
        sent = []
        self.enterContext(patch.object(telegram, "send_listing", side_effect=lambda l, r: sent.append(l.key) or {"result": {"message_id": 1}}))
        watch.Watcher(["rm"]).cycle()
        listings[:] = [home(1, 1200), home(2, 1300)]
        with patch.object(signals.ArchiveReader, "__enter__", side_effect=sqlite3.OperationalError("database is locked")):
            watch.Watcher(["rm"]).cycle()
        self.assertEqual(sent, ["rm:2"])

    def test_the_added_time_is_small_and_hard_bounded(self):
        listings = []
        with archive._db() as db:
            db.executemany("INSERT INTO adverts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           [(f"rm:{i}", "rm", "u", "a", 1, 1500, 51.4 + (i % 100) * 0.002, -0.2 + (i // 100) * 0.002,
                             at(1), at(1), None, "active", 1, None, 0, json.dumps({"source": "rm", "id": str(i)})) for i in range(6000)])
        for i in range(40):
            listings.append(flat(10000 + i, lat=51.4 + i * 0.002))
        started = time.monotonic()
        signals.annotate(listings, today=TODAY)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)                                    # 40 cards against a 6,000-advert archive
        capped = [flat(20000 + i) for i in range(10)]
        counter = {"n": 0}
        def clock():
            counter["n"] += 1
            return counter["n"] * 0.1
        signals.annotate(capped, budget=0.35, clock=clock, today=TODAY)
        self.assertTrue(all(not l.signals for l in capped[4:]))          # the budget stops it: later cards simply get none

    def test_signals_are_off_without_the_flag(self):
        with patch.dict(os.environ, {"SIGNALS": ""}), patch.object(signals, "ArchiveReader", side_effect=AssertionError("opened while off")):
            self.assertEqual(signals.annotate([flat()]), 0)


# ---------------------------------------------------------------- the log, written after the send sequence

class LogTests(Base):
    def setUp(self):
        super().setUp()
        self.db = os.path.join(tempfile.mkdtemp(), "s.db")
        signals._buffer.clear()

    def test_capture_does_no_io_and_logs_what_the_card_showed(self):
        l = flat(1)
        l.signals = [{"name": "reduced_stale", "label": "x", "evidence": "e", "strength": 2}]
        with patch.object(signals, "_db", side_effect=AssertionError("capture did I/O")):
            rows = signals.capture(l, OWNER, "report", NOW)
        self.assertEqual((rows[0]["chat_id"], rows[0]["key"], rows[0]["via"]), (OWNER, "rm:1", "report"))
        self.assertEqual(signals.capture(flat(2), OWNER, "alert"), [])

    def test_shown_then_saved_and_hidden_per_person(self):
        for i in (1, 2, 3):
            l = flat(i)
            l.signals = [{"name": "reduced_stale", "label": "x", "evidence": "e", "strength": 2}]
            signals.defer(signals.capture(l, OWNER, "alert"))
        signals.flush(self.db)
        signals.log_action(OWNER, "rm:1", "s", path=self.db)
        signals.log_action(OWNER, "rm:2", "h", path=self.db)
        signals.log_action(FRIEND, "rm:3", "s", path=self.db)               # another person's save is not the owner's
        r = signals.report(self.db)["reduced_stale"]
        self.assertEqual((r["shown"], r["saved"], r["hidden"], r["no_action"]), (3, 1, 1, 1))

    def test_rows_older_than_ninety_days_are_deleted(self):
        l = flat(1)
        l.signals = [{"name": "reduced_stale", "label": "x", "evidence": "e", "strength": 2}]
        old = NOW - dt.timedelta(days=120)
        signals.defer(signals.capture(l, OWNER, "alert", old))
        signals.flush(self.db, now=old)
        signals.defer(signals.capture(flat(2), OWNER, "alert", NOW))
        l2 = flat(2)
        l2.signals = l.signals
        signals.defer(signals.capture(l2, OWNER, "alert", NOW))
        signals.flush(self.db, now=NOW)
        with signals._db(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0], 1)

    def test_a_contended_or_dead_log_is_skipped_quietly(self):
        l = flat(1)
        l.signals = [{"name": "reduced_stale", "label": "x", "evidence": "e", "strength": 2}]
        signals.defer(signals.capture(l, OWNER, "alert"))
        with patch.object(signals, "_db", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(signals.flush(self.db), 0)


class CycleBase(Base):
    def setUp(self):
        super().setUp()
        for target in (patch.object(main.enrich, "geocode"), patch.object(main.enrich, "assign_crime_percentiles"),
                       patch.object(main, "passes_area", return_value=None), patch.object(main, "_ensure_photos"),
                       patch.object(telegram, "throttle"), patch.object(telegram, "send_text")):
            target.start()
            self.addCleanup(target.stop)
        self.enterContext(patch.object(main, "passes_basic", side_effect=lambda l, **kw: main._cost_reject(l)))
        self.listings = [home(1, 1200)]
        self.enterContext(patch.object(main.SOURCES["rm"], "fetch", side_effect=lambda: list(self.listings)))
        self.events, self.sent, self.texts = [], [], []
        self.enterContext(patch.object(telegram, "send_listing", side_effect=self._send))
        self.admit({"bills": 100.0})
        signals._buffer.clear()
        self.watcher = watch.Watcher(["rm"])

    def _send(self, l, reason):
        self.events.append("telegram")
        self.sent.append((users.current_chat(), l.key, reason, telegram.format_listing(l)))
        return {"result": {"message_id": 1}}


class CycleLogTests(CycleBase):
    def test_signals_appear_on_new_alert_cards_and_are_logged_after_the_send_sequence_and_the_lock(self):
        with patch.dict(os.environ, {"SIGNALS": "1"}):
            self.watcher.cycle()                                              # baseline
            self.sent.clear()
            self.events.clear()
            self.listings = [home(1, 1200), home(2, 1300)]
            real_save, real_verdicts, real_flush, real_locked = main.save_seen, archive.record_verdicts, signals.flush, main.locked

            @contextlib.contextmanager
            def locked():
                self.events.append("lock")
                with real_locked():
                    yield
                self.events.append("unlock")

            def save(seen):
                if "rm:2" in seen:
                    self.events.append("receipt")
                return real_save(seen)

            def verdicts(*a, **kw):
                if kw.get("acknowledged"):
                    self.events.append("ack")
                return real_verdicts(*a, **kw)

            def flush(*a, **kw):
                self.events.append("flush")
                return real_flush(*a, **kw)

            with patch.object(main, "save_seen", side_effect=save), patch.object(archive, "record_verdicts", side_effect=verdicts), \
                    patch.object(signals, "flush", side_effect=flush), patch.object(main, "locked", side_effect=locked), \
                    patch.object(signals, "_db", wraps=signals._db) as db:
                self.watcher.cycle()
        card = self.sent[0][3]
        self.assertIn("🔎 Thin advert", card)
        order = self.events
        self.assertLess(order.index("telegram"), order.index("receipt"))
        self.assertLess(order.index("receipt"), order.index("ack"))
        self.assertLess(order.index("ack"), order.index("unlock"))
        self.assertLess(order.index("unlock"), order.index("flush"))
        with signals._db() as conn:
            rows = [dict(r) for r in conn.execute("SELECT chat_id, key, signal, via FROM signal_log ORDER BY id")]
        self.assertEqual({(r["key"], r["via"]) for r in rows}, {("rm:2", "alert")})
        self.assertEqual({r["chat_id"] for r in rows}, {OWNER, FRIEND})

    def test_nothing_is_written_to_the_log_inside_run(self):
        with patch.dict(os.environ, {"SIGNALS": "1"}):
            self.watcher.cycle()
            self.listings = [home(1, 1200), home(2, 1300)]
            with patch.object(signals, "_db", side_effect=AssertionError("signal log touched inside _run")), \
                    patch.object(signals, "flush"):
                main._run(["rm"], False, False, rentals=True, watch=True, prefetched=(list(self.listings), []))
        self.assertTrue(signals._buffer)

    def test_report_cards_show_and_log_their_signals_too(self):
        with patch.dict(os.environ, {"SIGNALS": "1"}):
            self.watcher.cycle()
            self.listings = [home(1, 1200), home(2, 1300)]
            cards = []
            def send_report(top, saved, rest, title, flags_of, *a, **kw):
                cards.extend(telegram.format_listing(l) for l in list(top) + list(saved))
            with patch.object(main, "send_report", side_effect=send_report):
                self.watcher.cycle(kind="report", chat_id=OWNER)
        self.assertTrue(cards)
        self.assertTrue(all("🔎" in c for c in cards))
        with signals._db() as conn:
            vias = {r["via"] for r in conn.execute("SELECT via FROM signal_log")}
        self.assertIn("report", vias)

    def test_the_save_button_is_logged_against_the_card(self):
        l = home(1)
        l.signals = [{"name": "reduced_stale", "label": "x", "evidence": "e", "strength": 2}]
        with patch.dict(os.environ, {"SIGNALS": "1"}):
            signals.defer(signals.capture(l, OWNER, "alert"))
            signals.flush()
            signals.log_action(OWNER, l.key, "s")
            self.assertEqual(signals.report()["reduced_stale"]["saved"], 1)


# ---------------------------------------------------------------- off is the release

def release_module(name, relative):
    source = subprocess.check_output(["git", "-C", str(ROOT), "show", f"origin/release-archive-pipeline:{relative}"],
                                     text=True, stderr=subprocess.DEVNULL)
    module = types.ModuleType(f"pisobot._release_{name}")
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


class OffEquivalenceTests(CycleBase):
    def scenario(self, watcher_class, pipeline_module):
        state, sent, listings = tempfile.mkdtemp(), [], [(1, 1200)]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"PISOBOT_STATE_DIR": state, "STORAGE": "local"}))
            stack.enter_context(patch.object(main, "STATE_DIR", state))
            stack.enter_context(patch.object(main, "SEEN_PATH", str(Path(state) / "seen.json")))
            stack.enter_context(patch.object(cache, "PATH", str(Path(state) / "cache.json")))
            stack.enter_context(patch.object(cache, "_data", {}))
            stack.enter_context(patch.object(main.SOURCES["rm"], "fetch", side_effect=lambda: [home(i, r) for i, r in listings]))
            stack.enter_context(patch.object(telegram, "send_listing", side_effect=lambda l, r: (
                sent.append((users.current_chat(), l.key, r, telegram.format_listing(l))), {"result": {"message_id": 1}})[1]))
            if pipeline_module is not main:
                for attr in ("STATE_DIR", "SEEN_PATH", "passes_basic", "passes_area", "_ensure_photos"):
                    setattr(pipeline_module, attr, getattr(main, attr))
            stack.enter_context(patch.object(watch, "pipeline", pipeline_module))
            users.register(FRIEND, "Bea", "TheBeatifulest")
            watcher = watcher_class(["rm"])
            for step in ([(1, 1200)], [(1, 1200), (2, 1300)], [(1, 1200), (2, 1250)], [(1, 1200), (2, 1250)]):
                listings[:] = step
                watcher.cycle()
            seen = re.sub(r"\d{4}-\d\d-\d\dT?[\d:.+\-Z]*|[0-9a-f]{16,}", "~", (Path(state) / "seen.json").read_text())
            return sent, seen, dump(Path(state) / "pisobot.db")

    def test_with_signals_off_the_release_path_and_ours_do_the_same_thing(self):
        try:
            release_main = release_module("main", "pisobot/main.py")
            release_watch = release_module("watch", "pisobot/watch.py")
        except Exception:
            self.skipTest("release code not available from git")
        release_watch.pipeline = release_main
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SIGNALS", None)
            with patch.object(signals, "ArchiveReader", side_effect=AssertionError("archive opened while off")):
                theirs = self.scenario(release_watch.Watcher, release_main)
                ours = self.scenario(watch.Watcher, main)
        self.assertEqual(theirs[0], ours[0])                                   # same alerts, people, reasons and card text
        self.assertEqual(theirs[1], ours[1])
        self.assertEqual(theirs[2], ours[2])                                   # every table, every row
        self.assertNotIn("signal_log", ours[2])
        self.assertGreater(len(ours[0]), 2)


# ---------------------------------------------------------------- the evaluation script

class EvalScriptTests(ArchiveBase):
    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location("eval_signals", ROOT / "scripts" / "eval_signals.py")
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def copy_of_archive(self):
        with contextlib.closing(sqlite3.connect(localdb.state_dir() / "pisobot.db")) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target = os.path.join(tempfile.mkdtemp(), "copy.db")
        shutil.copy(localdb.state_dir() / "pisobot.db", target)
        return target

    def test_it_counts_each_signal_prints_examples_and_never_writes(self):
        thin = flat(1, floorplan=None, images=["a"], description="Nice flat.")
        self.advance(thin, 30)
        for i in range(2, 16):
            self.advance(flat(i, floorplan=None, images=["a"], description="Nice flat.", lat=51.3 + i * 0.01), 5)
        back = flat(99)
        self.advance(back, 20)
        self.sweeps_miss(15)
        self.advance(back, 2)
        path = self.copy_of_archive()
        before = Path(path).read_bytes()
        lines = []
        result = self.mod.evaluate(path, TODAY, 10, lines.append)
        text = "\n".join(lines)
        self.assertEqual(Path(path).read_bytes(), before)                       # opened read-only
        self.assertEqual(result["fired"]["under_presented"], 14)               # the 15th went unseen in the two missed sweeps
        self.assertEqual(result["fired"]["back_on_market"], 1)
        self.assertEqual(text.count("rm:") >= 11, True)
        self.assertLessEqual(text.split("== under_presented")[1].split("== ")[0].count("https://rm/"), 10)      # ten examples at most
        for name in signals.PRIORITY:
            self.assertIn(f"== {name}:", text)
        self.assertIn("Data coverage", text)

    def test_a_missing_file_is_refused_and_nothing_is_created(self):
        lines = []
        missing = os.path.join(tempfile.mkdtemp(), "nope.db")
        self.assertEqual(self.mod.main([missing], out=lines.append), 2)
        self.assertFalse(os.path.exists(missing))


if __name__ == "__main__":
    unittest.main()
