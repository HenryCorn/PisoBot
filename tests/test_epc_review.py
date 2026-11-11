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

from pisobot import budget, config, enrichment, epc, localdb, main, telegram, users, watch
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
        self.assertEqual(deleted, {"epc_search": 1, "epc_cert": 0, "epc_area_facts": 1})
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
        epc.observe([stated, derived, from_epc], store, NOW)
        self.assertEqual([r["key"] for r in store.area_peers("N7", 1, NOW)], ["rm:1"])

    def test_value_uses_recent_comparables_and_reports_where_they_come_from(self):
        store = self.store()
        epc.observe([peer(i, 40) for i in range(1, 10)], store, NOW)
        mine = peer(0, 60)
        mine.area_source = "floorplan"
        epc.value_for(mine, store, NOW)
        self.assertEqual((mine.value["percentile"], mine.value["n"], mine.value["peers"]), (100, 9, "portal-stated areas"))
        epc.value_for(mine, store, NOW + dt.timedelta(days=epc.AREA_FACT_DAYS + 1))          # comparables went stale
        self.assertEqual(mine.value, {})

    def test_too_few_comparables_say_nothing(self):
        store = self.store()
        epc.observe([peer(i, 40) for i in range(1, 8)], store, NOW)
        mine = peer(0, 60)
        epc.value_for(mine, store, NOW)
        self.assertEqual(mine.value, {})


# ---------------------------------------------------------------- finding 5: a hard budget in the alert path

class SlowProvider:
    def __init__(self, release=None, seconds=0.0):
        self.release, self.seconds, self.calls = release, seconds, 0

    def __call__(self, listing):
        self.calls += 1
        if self.release is not None:
            self.release.wait(5)
        elif self.seconds:
            time.sleep(self.seconds)
        listing.floor_area_m2, listing.area_source = 61.0, "epc"
        return listing


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        enrichment.reset()
        self.addCleanup(enrichment.reset)

    def listing(self, i=1):
        l = home(i)
        l.floor_area_m2 = None
        return l

    def test_off_returns_before_touching_anything(self):
        with patch.object(enrichment, "providers", return_value=[]), patch.object(enrichment, "_pool") as pool:
            self.assertEqual(enrichment.enrich([self.listing()]), 0)
        pool.submit.assert_not_called()

    def test_facts_are_copied_back_on_success(self):
        l = self.listing()
        with patch.object(enrichment, "providers", return_value=[SlowProvider()]):
            self.assertEqual(enrichment.enrich([l]), 1)
        self.assertEqual((l.floor_area_m2, l.area_source), (61.0, "epc"))

    def test_a_slow_lookup_costs_the_card_its_facts_not_its_delivery(self):
        release = threading.Event()
        slow = SlowProvider(release)
        first, second = self.listing(1), self.listing(2)
        started = time.monotonic()
        with patch.object(enrichment, "providers", return_value=[slow]):
            enrichment.enrich([first, second], advert_seconds=0.3)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNone(first.floor_area_m2)
        self.assertEqual(slow.calls, 1)                    # nothing queued behind the stuck worker
        release.set()                                      # the late worker finishes: it must not touch the card
        time.sleep(0.2)
        self.assertIsNone(first.floor_area_m2)
        self.assertIsNone(second.floor_area_m2)

    def test_the_window_budget_bounds_the_total(self):
        slow = SlowProvider(seconds=0.2)
        with patch.object(enrichment, "providers", return_value=[slow]):
            done = enrichment.enrich([self.listing(i) for i in range(1, 8)], advert_seconds=1, window_seconds=0.5)
        self.assertLess(done, 7)
        self.assertGreaterEqual(done, 1)
        with patch.object(enrichment, "providers", return_value=[slow]):
            self.assertEqual(enrichment.enrich([self.listing(9)], advert_seconds=1, window_seconds=0.5), 0)   # same window

    def test_a_failing_provider_leaves_the_card_alone(self):
        def boom(listing):
            listing.floor_area_m2 = 99
            raise RuntimeError("x")
        l = self.listing()
        with patch.object(enrichment, "providers", return_value=[boom]):
            enrichment.enrich([l])
        self.assertIsNone(l.floor_area_m2)

    def test_the_deadline_is_visible_to_lookups(self):
        seen = []
        def look(listing):
            seen.append(budget.time_left())
        with patch.object(enrichment, "providers", return_value=[look]):
            enrichment.enrich([self.listing()], advert_seconds=5)
        self.assertTrue(0 < seen[0] <= 5)
        self.assertIsNone(budget.time_left())              # not leaked into ordinary code


class AlertPathTests(Base):
    """A slow optional lookup must not delay or lose an alert in the real pipeline."""

    def setUp(self):
        super().setUp()
        enrichment.reset()
        self.addCleanup(enrichment.reset)
        for target in (patch.object(main.enrich, "geocode"), patch.object(main.enrich, "assign_crime_percentiles"),
                       patch.object(main, "passes_area", return_value=None), patch.object(main, "_ensure_photos"),
                       patch.object(telegram, "throttle")):
            target.start()
            self.addCleanup(target.stop)
        self.enterContext(patch.object(main, "passes_basic", side_effect=lambda l, **kw: main._cost_reject(l)))
        self.listings = [home(1, 1200)]
        self.enterContext(patch.object(main.SOURCES["rm"], "fetch", side_effect=lambda: list(self.listings)))
        self.sent = []
        self.enterContext(patch.object(telegram, "send_listing", side_effect=lambda l, r: self.sent.append(l.key)))
        self.enterContext(patch.object(telegram, "send_text"))
        watch.Watcher(["rm"]).cycle()                               # baseline
        self.sent.clear()

    def test_slow_enrichment_does_not_hold_up_either_alert(self):
        release = threading.Event()
        slow = SlowProvider(release)
        self.listings = [home(1, 1200), home(2, 1300), home(3, 1250)]
        started = time.monotonic()
        with patch.object(enrichment, "providers", return_value=[slow]), patch.object(config, "ENRICH_ADVERT_SECONDS", 0.3):
            watch.Watcher(["rm"]).cycle()
        self.assertLess(time.monotonic() - started, 5)
        release.set()
        self.assertEqual(sorted(self.sent), ["rm:2", "rm:3"])      # both alerts, once each


# ---------------------------------------------------------------- finding 10: off is off

class OffTests(unittest.TestCase):
    RELEASE_FIELDS = ("source", "id", "url", "address", "bedrooms", "scheme", "tenure", "rent", "service_charge",
                      "bills", "available", "min_deposit", "lat", "lng", "image", "images", "floorplan",
                      "floor_area_m2", "description", "estimated", "council_tax", "floor", "lease_years",
                      "council_tax_band", "council_tax_included", "council_tax_monthly", "utilities_monthly",
                      "council_tax_label", "first_listed", "update_reason", "update_date", "update_text",
                      "update_text_on", "bathrooms", "agent", "agent_phone", "first_listed_at",
                      "advert_deposit", "studio", "furnishing")

    def test_stored_search_and_archive_snapshots_are_what_the_release_stores(self):
        self.assertEqual(tuple(CACHE_FIELDS), self.RELEASE_FIELDS)
        l = home()
        l.full_postcode = "N7 6DJ"
        self.assertEqual(set(snapshot(l)), set(self.RELEASE_FIELDS))

    def test_an_installed_client_is_not_authority_to_run(self):
        fake = Fake(BUILDING, CERTS)
        epc.set_default(make(fake))
        self.addCleanup(epc.set_default, None)
        with patch.dict(os.environ, {"EPC_DISABLE": "1", "EPC_API_TOKEN": "x", "STORAGE": "local"}):
            self.assertFalse(epc.enabled())
            epc.annotate(advert())
            epc.observe([advert()])
        with patch.dict(os.environ, {}, clear=True):
            epc.annotate(advert())
        self.assertEqual(fake.calls, [])

    def test_enrichment_and_the_card_are_inert_without_a_feature(self):
        l = home()
        before = dict(vars(l))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(enrichment.enrich([l]), 0)
            enrichment.observe([l])
        self.assertEqual(vars(l), before)
        self.assertEqual(epc.card_line(l), "")

    def test_off_creates_no_tables_and_calls_no_provider(self):
        state = tempfile.mkdtemp()
        boom = patch.object(epc, "default", side_effect=AssertionError("EPC client created while off"))
        with patch.dict(os.environ, {"STORAGE": "local", "PISOBOT_STATE_DIR": state}, clear=True), boom:
            enrichment.enrich([home()])
            enrichment.observe([home()])
            epc.purge_expired()
            epc.annotate(advert())
        db = Path(state, "pisobot.db")
        if db.exists():
            with contextlib.closing(sqlite3.connect(db)) as conn:
                names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            self.assertFalse({n for n in names if n.startswith(("epc_", "floorplan_"))})


if __name__ == "__main__":
    unittest.main()
