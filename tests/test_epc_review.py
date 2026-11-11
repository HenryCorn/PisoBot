"""Independent-review findings for G2/G6 (ported reproductions) and their fixes. Offline: fakes only."""
import contextlib
import datetime as dt
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pisobot import budget, config, epc, facts, localdb, main, telegram, users, watch
from pisobot.models import Listing
from pisobot.sources import CACHE_FIELDS, snapshot

from test_epc import BUILDING, CERTS, Fake, NOW, TODAY, advert, cert, make, peer, summary
from test_users import Base, FRIEND, OWNER, home


# ---------------------------------------------------------------- finding 4: exact matches must fit the dwelling

class ExactMatchTests(unittest.TestCase):
    def test_a_whole_house_is_never_an_exact_match_for_a_flat(self):
        fake = Fake([summary("HOUSE", "12 Hornsey Road")], {"HOUSE": cert(110, rooms=5, dwelling="Mid-terrace house")})
        listing = advert("Ground floor flat, 12 Hornsey Road, London N7 6DJ")
        epc.annotate(listing, make(fake))
        self.assertNotEqual(listing.epc.get("match"), "exact")
        self.assertIsNone(listing.floor_area_m2)
        self.assertNotIn("110 m²", epc.card_line(listing))

    def test_an_advert_naming_no_flat_is_not_matched_to_one_flat_of_a_building(self):
        fake = Fake([summary("F", "12 Hornsey Road")], {"F": cert(50, rooms=2, dwelling="Top-floor flat")})
        self.assertNotEqual(epc.match(advert("12 Hornsey Road, London N7 6DJ"), "N7 6DJ", make(fake), TODAY)["match"], "exact")

    def test_room_count_far_from_the_bedrooms_vetoes_an_exact_flat(self):
        fake = Fake([summary("A", "Flat 3, 12 Hornsey Road")], {"A": cert(110, rooms=6)})
        self.assertNotEqual(epc.match(advert(), "N7 6DJ", make(fake), TODAY)["match"], "exact")
        fake = Fake([summary("A", "Flat 3, 12 Hornsey Road")], {"A": cert(48, rooms=3)})       # separate kitchen-diner: fine
        self.assertEqual(epc.match(advert(), "N7 6DJ", make(fake), TODAY)["match"], "exact")

    def test_a_flat_certificate_is_not_exact_for_an_advert_that_reads_as_a_house(self):
        fake = Fake([summary("A", "14 Hornsey Road")], {"A": cert(50, rooms=2)})
        house = advert("14 Hornsey Road, London N7", beds=1)
        house.description = "One bedroom terraced house."
        self.assertNotEqual(epc.match(house, "N7 6DJ", make(fake), TODAY)["match"], "exact")

    def test_a_number_range_is_not_collapsed_to_its_first_number(self):
        fake = Fake([summary("BLOCK", "Flat 3, 12-14 Hornsey Road")], {"BLOCK": cert(95)})
        self.assertNotEqual(epc.match(advert("Flat 3, 12 Hornsey Road, London N7 6DJ"), "N7 6DJ", make(fake), TODAY)["match"], "exact")
        both = epc.match(advert("Flat 3, 12-14 Hornsey Road, London N7 6DJ"), "N7 6DJ", make(fake), TODAY)
        self.assertEqual(both["match"], "exact")
        self.assertEqual(epc.parse_address("12 - 14 Hornsey Road")["number"], "12-14")


class RangeAndHttpTests(unittest.TestCase):
    def test_a_flat_is_never_compared_with_only_houses_in_a_range(self):
        """re-review: 'Ground floor flat' with a description that says only 'property' got an 80-82 m2 range from two houses."""
        houses = [summary("H1", "10 Test Road"), summary("H2", "14 Test Road")]
        certs = {"H1": cert(80, rooms=2, dwelling="Mid-terrace house"), "H2": cert(82, rooms=2, dwelling="End-terrace house")}
        listing = advert("Ground floor flat, 12 Test Road, London N7 6DJ")
        listing.description = "One bedroom property."
        self.assertEqual(epc.match(listing, "N7 6DJ", make(Fake(houses, certs)), TODAY)["match"], "none")
        flats = {"H1": cert(48, rooms=2), "H2": cert(50, rooms=2)}
        self.assertEqual(epc.match(listing, "N7 6DJ", make(Fake(houses, flats)), TODAY)["match"], "range")

    def test_an_advert_that_reads_as_a_house_is_compared_only_with_houses(self):
        flats = [summary("F1", "10 Test Road"), summary("F2", "14 Test Road")]
        certs = {"F1": cert(48, rooms=2), "F2": cert(50, rooms=2)}
        house = advert("12 Test Road, London N7 6DJ")
        house.description = "One bedroom terraced house."
        self.assertEqual(epc.match(house, "N7 6DJ", make(Fake(flats, certs)), TODAY)["match"], "none")

    def test_the_api_response_is_streamed_and_an_oversized_body_is_refused(self):
        class Response:
            status_code = 200
            def __init__(self, size):
                self.size = size
            def iter_content(self, n):
                for _ in range(self.size // n):
                    yield b" " * n
            def close(self):
                pass
        with patch("requests.get", return_value=Response(epc.MAX_RESPONSE_BYTES + 200_000)) as get:
            with self.assertRaises(ValueError):
                epc.Epc._http("https://x.invalid", {}, {})
        self.assertTrue(get.call_args.kwargs["stream"])
        small = Response(65536)
        small.iter_content = lambda n: iter([b'{"data": []}'])
        with patch("requests.get", return_value=small):
            self.assertEqual(epc.Epc._http("https://x.invalid", {}, {}), (200, {"data": []}))


# ---------------------------------------------------------------- findings 7 and 9: retention and the cap

class RetentionAndCapTests(unittest.TestCase):
    def test_a_long_lived_client_deletes_rows_past_thirty_days_without_a_cache_hit(self):
        clock = [NOW]
        fake = Fake(BUILDING, CERTS)
        path = os.path.join(tempfile.mkdtemp(), "r.db")
        client = epc.Epc(http=fake, store=epc.Store(path), now=lambda: clock[0], sleep=lambda s: None)
        client.summaries("N7 6DJ")
        client.certificate("A1")
        clock[0] += dt.timedelta(days=40)
        fake.summaries = []
        client.summaries("E1 1AA")
        with contextlib.closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM epc_search WHERE postcode='N7 6DJ'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM epc_cert").fetchone()[0], 0)

    def test_a_failed_purge_is_retried_not_forgotten(self):
        client = make(Fake(BUILDING, CERTS))
        calls = []
        real = client.store.purge
        def flaky(now):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("locked")
            return real(now)
        with patch.object(client.store, "purge", flaky):
            client.summaries("N7 6DJ")
            client.certificate("A1")
            client.certificate("A2")                       # within the hour of the success: no further purge
        self.assertEqual(len(calls), 2)

    def test_purge_expired_runs_without_the_feature_and_creates_nothing(self):
        path = os.path.join(tempfile.mkdtemp(), "p.db")
        self.assertEqual(epc.purge_expired(NOW, path), {})
        self.assertFalse(os.path.exists(path))                     # never touched a database that does not exist
        sqlite3.connect(path).close()
        self.assertEqual(epc.purge_expired(NOW, path), {})
        with contextlib.closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)
        store = epc.Store(path)
        store.search_put("N7 6DJ", BUILDING, NOW - dt.timedelta(days=45))
        store.cert_put("A1", {"area": 50}, NOW - dt.timedelta(days=5))
        store.note_area("rm:1", "N7", 1, 1500, 50, NOW - dt.timedelta(days=45))
        deleted = epc.purge_expired(NOW, path)
        self.assertEqual(deleted, {"epc_search": 1, "epc_cert": 0, "epc_area_facts": 1, "epc_usage": 0})
        self.assertIsNotNone(store.cert_get("A1", NOW))

    def test_the_cap_is_taken_atomically_per_request_from_the_current_day(self):
        path = os.path.join(tempfile.mkdtemp(), "c.db")
        results = []
        barrier = threading.Barrier(2)
        def take():
            store = epc.Store(path)
            barrier.wait(timeout=3)
            results.append(store.reserve("2026-11-10", 1))
        threads = [threading.Thread(target=take) for _ in range(2)]
        [t.start() for t in threads]
        [t.join(5) for t in threads]
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(epc.Store(path).requests_today("2026-11-10"), 1)

    def test_the_request_is_charged_to_the_day_it_is_sent(self):
        clock = [NOW.replace(hour=23, minute=59, second=59)]
        def slow_sleep(seconds):
            clock[0] = NOW + dt.timedelta(days=1, seconds=1)
        client = epc.Epc(http=Fake(BUILDING, CERTS), store=epc.Store(os.path.join(tempfile.mkdtemp(), "d.db")),
                         daily_limit=1, now=lambda: clock[0], sleep=slow_sleep)
        client.requests_made = 1                                    # so the pause (which crosses midnight) happens
        client.summaries("N7 6DJ")
        self.assertEqual(client.store.requests_today("2026-11-10"), 0)
        self.assertEqual(client.store.requests_today("2026-11-11"), 1)

    def test_a_request_is_refused_when_the_time_budget_is_spent(self):
        fake = Fake(BUILDING, CERTS)
        client = make(fake)
        token = budget.DEADLINE.set(time.monotonic() - 1)
        try:
            with self.assertRaises(epc.Unavailable):
                client.summaries("N7 6DJ")
        finally:
            budget.DEADLINE.reset(token)
        self.assertEqual(fake.calls, [])

    def test_certificate_rows_keep_addresses_in_the_primary_database(self):
        override = localdb.DATABASE.set(os.path.join(tempfile.mkdtemp(), "person", "pisobot.db"))
        try:
            with patch.dict(os.environ, {"PISOBOT_STATE_DIR": tempfile.mkdtemp()}):
                epc.Store().search_put("N7 6DJ", BUILDING, NOW)
                primary = Path(os.environ["PISOBOT_STATE_DIR"], "pisobot.db")
                self.assertTrue(primary.exists())
        finally:
            localdb.DATABASE.reset(override)


# ---------------------------------------------------------------- finding 4/value: comparables are portal-stated

class ValueProvenanceTests(unittest.TestCase):
    def store(self):
        return epc.Store(os.path.join(tempfile.mkdtemp(), "v.db"))

    def test_only_portal_stated_areas_are_remembered_as_comparables(self):
        store = self.store()
        stated = peer(1, 40)
        derived = peer(2, 50)
        derived.area_source = "floorplan"
        from_epc = peer(3, 60)
        from_epc.area_source, from_epc.epc = "epc", {"match": "exact"}
        for l in (stated, derived, from_epc):
            epc.note_portal_area(l, store, NOW)
        self.assertEqual([r["key"] for r in store.area_peers("N7", 1, NOW)], ["rm:1"])

    def test_value_uses_recent_comparables_and_reports_where_they_come_from(self):
        store = self.store()
        for l in [peer(i, 40) for i in range(1, 10)]:
            epc.note_portal_area(l, store, NOW)
        mine = peer(0, 60)
        mine.area_source = "floorplan"
        epc.value_for(mine, store, NOW)
        self.assertEqual((mine.value["percentile"], mine.value["n"], mine.value["peers"]), (100, 9, "portal-stated areas"))
        epc.value_for(mine, store, NOW + dt.timedelta(days=epc.AREA_FACT_DAYS + 1))          # comparables went stale
        self.assertEqual(mine.value, {})

    def test_too_few_comparables_say_nothing(self):
        store = self.store()
        for l in [peer(i, 40) for i in range(1, 8)]:
            epc.note_portal_area(l, store, NOW)
        mine = peer(0, 60)
        epc.value_for(mine, store, NOW)
        self.assertEqual(mine.value, {})


# ---------------------------------------------------------------- finding 10: off is off

class OffTests(unittest.TestCase):
    def test_an_installed_client_is_not_authority_to_run(self):
        fake = Fake(BUILDING, CERTS)
        epc.set_default(make(fake))
        self.addCleanup(epc.set_default, None)
        with patch.dict(os.environ, {"EPC_DISABLE": "1", "EPC_API_TOKEN": "x", "STORAGE": "local"}):
            self.assertFalse(epc.enabled())
            epc.annotate(advert())
        with patch.dict(os.environ, {}, clear=True):
            epc.annotate(advert())
        self.assertEqual(fake.calls, [])

    def test_off_creates_no_tables_and_creates_no_client(self):
        state = tempfile.mkdtemp()
        boom = patch.object(epc, "default", side_effect=AssertionError("EPC client created while off"))
        with patch.dict(os.environ, {"STORAGE": "local", "PISOBOT_STATE_DIR": state}, clear=True), boom:
            self.assertFalse(facts.enabled())
            self.assertEqual(facts.pending(), 0)
            facts.purge_expired()
            epc.annotate(advert())
        db = Path(state, "pisobot.db")
        if db.exists():
            with contextlib.closing(sqlite3.connect(db)) as conn:
                names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            self.assertFalse({n for n in names if n.startswith(("epc_", "floorplan_", "facts_"))})


if __name__ == "__main__":
    unittest.main()
