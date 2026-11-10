"""epc.py with recorded-shape fixtures and a fake HTTP function: no network, no credentials."""
import datetime as dt
import os
import tempfile
import unittest
from unittest.mock import patch

from pisobot import config, epc
from pisobot.models import Listing
from pisobot.sources import rightmove

NOW = dt.datetime(2026, 11, 10, 12, tzinfo=dt.timezone.utc)
TODAY = NOW.date()


def summary(n, line1, line2="", date="2021-05-01", band="C", uprn=None):
    return {"certificateNumber": n, "addressLine1": line1, "addressLine2": line2, "addressLine3": "",
            "addressLine4": "", "postcode": "N7 6DJ", "postTown": "LONDON", "council": "Islington",
            "currentEnergyEfficiencyBand": band, "registrationDate": date, "uprn": uprn or n, "schemaType": "RdSAP"}


def cert(area=50.0, rooms=2, band="C", dwelling="Mid-floor flat", heating="Boiler and radiators, mains gas",
         date="2021-05-01", costs=(300.0, 150.0, 60.0)):
    return {"data": {"total_floor_area": area, "habitable_room_count": rooms,
                     "current_energy_efficiency_band": band, "dwelling_type": dwelling,
                     "main_heating": [{"description": {"value": heating, "language": "1"}}],
                     "heating_cost_current": costs[0], "hot_water_cost_current": costs[1],
                     "lighting_cost_current": costs[2], "registration_date": date}}


class Fake:
    """Stands in for requests.get: serves search and certificate payloads and counts calls."""
    def __init__(self, summaries=(), certs=None, status=200):
        self.summaries, self.certs, self.status, self.calls = list(summaries), certs or {}, status, []

    def __call__(self, url, params, headers):
        assert headers["Authorization"].startswith("Bearer ")
        self.calls.append((url.rsplit("/api/", 1)[1], dict(params)))
        if self.status != 200:
            return self.status, {"error": "x"}
        if url.endswith("/search"):
            if not self.summaries:
                return 404, {"error": "no match"}
            return 200, {"data": self.summaries, "pagination": {"totalRecords": len(self.summaries), "nextPage": None}}
        c = self.certs.get(params["certificate_number"])
        return (200, c) if c else (404, {})


def make(fake, **kw):
    kw.setdefault("sleep", lambda s: None)
    return epc.Epc(http=fake, store=epc.Store(os.path.join(tempfile.mkdtemp(), "t.db")), now=lambda: NOW, **kw)


def advert(address="Flat 3, 12 Hornsey Road, London N7 6DJ", beds=1, **kw):
    l = Listing(source="rm", id="1", url="u", address=address, bedrooms=beds, scheme="Private rent",
                tenure="rent", rent=1500, description="One bedroom flat.", **kw)
    l.full_postcode = "N7 6DJ"
    return l


BUILDING = [summary("A1", "Flat 1, 12 Hornsey Road"), summary("A2", "Flat 2, 12 Hornsey Road"),
            summary("A3", "Flat 3, 12 Hornsey Road"), summary("B1", "14 Hornsey Road")]
CERTS = {"A1": cert(48), "A2": cert(52), "A3": cert(50.5, band="D"), "B1": cert(110, 5, dwelling="Mid-terrace house")}


class ParseTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(epc.parse_address("Flat 3, 12 Hornsey Road, London N7 6DJ"),
                         {"flat": "3", "number": "12", "street": "hornsey road", "building": None})
        self.assertEqual(epc.parse_address("12 Hornsey Road, London")["flat"], None)
        self.assertEqual(epc.parse_address("3, 12 Hornsey Road")["flat"], "3")
        p = epc.parse_address("Apartment 7, Kestrel House, Hornsey Road")
        self.assertEqual((p["flat"], p["number"], p["building"]), ("7", None, "kestrel house"))
        self.assertEqual(epc.parse_address("Hornsey Road, London N7")["number"], None)
        self.assertEqual(epc.norm_postcode("n76dj"), "N7 6DJ")
        self.assertEqual(epc.norm_postcode("N7"), "")


class MatchTests(unittest.TestCase):
    def test_exact_flat(self):
        r = epc.match(advert(), "N7 6DJ", make(Fake(BUILDING, CERTS)), TODAY)
        self.assertEqual((r["match"], r["low"], r["high"], r["band"], r["n"]), ("exact", 50.5, 50.5, "D", 1))
        self.assertEqual(r["annual_cost"], 510.0)
        self.assertIn("mains gas", r["heating"])

    def test_exact_reads_only_one_certificate(self):
        fake = Fake(BUILDING, CERTS)
        epc.match(advert(), "N7 6DJ", make(fake), TODAY)
        self.assertEqual([c[0] for c in fake.calls], ["domestic/search", "certificate"])

    def test_house_is_exact_when_only_one_dwelling_at_the_number(self):
        r = epc.match(advert("14 Hornsey Road, London N7", beds=4), "N7 6DJ", make(Fake(BUILDING, CERTS)), TODAY)
        self.assertEqual((r["match"], r["low"]), ("exact", 110))

    def test_no_flat_given_in_a_block_is_range_not_exact(self):
        r = epc.match(advert("12 Hornsey Road, London N7"), "N7 6DJ", make(Fake(BUILDING, CERTS)), TODAY)
        self.assertEqual((r["match"], r["low"], r["high"], r["n"]), ("range", 48, 52, 3))
        self.assertIsNone(r["annual_cost"])
        self.assertIsNone(r["band"])            # bands C and D disagree: none shown

    def test_flat_missing_from_certificates_falls_back_to_building_range(self):
        r = epc.match(advert("Flat 9, 12 Hornsey Road"), "N7 6DJ", make(Fake(BUILDING, CERTS)), TODAY)
        self.assertEqual(r["match"], "range")

    def test_disagreeing_certificates_report_none(self):
        certs = dict(CERTS, A2=cert(75))
        r = epc.match(advert("12 Hornsey Road"), "N7 6DJ", make(Fake(BUILDING, certs)), TODAY)
        self.assertEqual(r["match"], "none")
        self.assertIn("disagree", r["reason"])

    def test_single_candidate_is_never_a_range(self):
        summaries = [summary("A1", "Flat 1, 12 Hornsey Road")]
        r = epc.match(advert("Hornsey Road, London N7"), "N7 6DJ", make(Fake(summaries, CERTS)), TODAY)
        self.assertEqual(r["match"], "none")

    def test_incompatible_room_count_excluded(self):
        certs = dict(CERTS, A1=cert(48, rooms=3), A2=cert(52, rooms=3))
        r = epc.match(advert("12 Hornsey Road"), "N7 6DJ", make(Fake(BUILDING, certs)), TODAY)
        self.assertEqual(r["match"], "none")

    def test_too_many_certificates_not_compared(self):
        many = [summary(f"C{i}", f"Flat {i}, {i} Other Street") for i in range(epc.MAX_CERTS_PER_POSTCODE + 10)]
        fake = Fake(many, {})
        r = epc.match(advert("Hornsey Road, London N7"), "N7 6DJ", make(fake), TODAY)
        self.assertEqual(r["match"], "none")
        self.assertEqual(len(fake.calls), 1)     # only the search was spent

    def test_latest_certificate_per_address_wins(self):
        old = summary("OLD", "Flat 3, 12 Hornsey Road", date="2015-01-01", uprn="9")
        new = summary("NEW", "Flat 3, 12 Hornsey Road", date="2023-01-01", uprn="9")
        r = epc.match(advert(), "N7 6DJ", make(Fake([old, new], {"OLD": cert(40), "NEW": cert(44)})), TODAY)
        self.assertEqual((r["low"], r["date"]), (44, "2021-05-01"))

    def test_expired_certificates_ignored(self):
        old = summary("OLD", "Flat 3, 12 Hornsey Road", date="2009-01-01")
        r = epc.match(advert(), "N7 6DJ", make(Fake([old], {"OLD": cert(40)})), TODAY)
        self.assertEqual(r["match"], "none")

    def test_no_certificates_is_a_cached_none(self):
        fake = Fake([], {})
        e = make(fake)
        self.assertEqual(epc.match(advert(), "N7 6DJ", e, TODAY)["match"], "none")
        epc.match(advert(), "N7 6DJ", e, TODAY)
        self.assertEqual(len(fake.calls), 1)     # a 404 (no certificates) is cached too


class CacheAndCapTests(unittest.TestCase):
    def test_second_lookup_costs_no_requests(self):
        fake = Fake(BUILDING, CERTS)
        e = make(fake)
        epc.match(advert(), "N7 6DJ", e, TODAY)
        n = len(fake.calls)
        epc.match(advert(), "N7 6DJ", e, TODAY)
        self.assertEqual(len(fake.calls), n)

    def test_cache_expires_after_thirty_days(self):
        fake = Fake(BUILDING, CERTS)
        e = make(fake)
        epc.match(advert(), "N7 6DJ", e, TODAY)
        n = len(fake.calls)
        e._now = lambda: NOW + dt.timedelta(days=31)
        epc.match(advert(), "N7 6DJ", e, TODAY)
        self.assertGreater(len(fake.calls), n)

    def test_daily_cap_raises_unavailable(self):
        e = make(Fake(BUILDING, CERTS), daily_limit=1)
        with self.assertRaises(epc.Unavailable):
            epc.match(advert(), "N7 6DJ", e, TODAY)

    def test_rate_limit_blocks_further_requests(self):
        fake = Fake(BUILDING, CERTS, status=429)
        e = make(fake)
        for _ in range(3):
            with self.assertRaises(epc.Unavailable):
                e.summaries("N7 6DJ")
        self.assertEqual(len(fake.calls), 1)

    def test_purge_removes_expired_rows(self):
        e = make(Fake(BUILDING, CERTS))
        epc.match(advert(), "N7 6DJ", e, TODAY)
        e.store.purge(NOW + dt.timedelta(days=40))
        self.assertIsNone(e.store.search_get("N7 6DJ", NOW))


class AnnotateTests(unittest.TestCase):
    def test_off_without_credentials(self):
        fake = Fake(BUILDING, CERTS)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EPC_API_TOKEN", None)
            l = epc.annotate(advert())
        self.assertEqual(l.epc, {})
        self.assertEqual(fake.calls, [])

    def test_exact_fills_missing_area_and_labels_source(self):
        l = epc.annotate(advert(), make(Fake(BUILDING, CERTS)))
        self.assertEqual((l.floor_area_m2, l.area_source, l.epc["match"]), (50.5, "epc", "exact"))

    def test_advert_area_is_never_overwritten(self):
        l = advert(floor_area_m2=60.0)
        epc.annotate(l, make(Fake(BUILDING, CERTS)))
        self.assertEqual((l.floor_area_m2, l.area_source), (60.0, "advert"))
        self.assertEqual(l.epc["advert_area"], 60.0)

    def test_range_does_not_set_area(self):
        l = epc.annotate(advert("12 Hornsey Road"), make(Fake(BUILDING, CERTS)))
        self.assertIsNone(l.floor_area_m2)
        self.assertEqual(l.epc["match"], "range")

    def test_failure_leaves_the_advert_untouched(self):
        def boom(url, params, headers):
            raise RuntimeError("down")
        l = advert()
        epc.annotate(l, make(boom))
        self.assertEqual((l.epc, l.floor_area_m2, l.area_source), ({}, None, None))

    def test_sale_listing_and_missing_postcode_skipped(self):
        fake = Fake(BUILDING, CERTS)
        e = make(fake)
        sale = advert()
        sale.tenure = "sale"
        epc.annotate(sale, e)
        none = advert()
        none.full_postcode, none.address = None, "Hornsey Road, London N7"
        epc.annotate(none, e)
        self.assertEqual(fake.calls, [])

    def test_postcode_found_in_address_text(self):
        l = advert()
        l.full_postcode = None
        self.assertEqual(epc.postcode_of(l), "N7 6DJ")


def peer(i, area, rent=1500, district="N7", beds=1):
    l = Listing(source="rm", id=str(i), url="u", address=f"Road, {district} 1AA", bedrooms=beds, scheme="Private rent",
                tenure="rent", rent=rent, floor_area_m2=area)
    l.area_source = "advert"
    return l


class ValueTests(unittest.TestCase):
    def test_percentile_needs_eight_comparables(self):
        me = peer(0, 60)
        self.assertIsNone(epc.value_against(me, [me] + [peer(i, 40) for i in range(1, 8)]))
        v = epc.value_against(me, [me] + [peer(i, 40) for i in range(1, 9)])
        self.assertEqual((v["per_m2"], v["percentile"], v["n"]), (25.0, 100, 8))

    def test_other_beds_and_districts_not_compared(self):
        me = peer(0, 60)
        scan = [me] + [peer(i, 40, beds=2) for i in range(1, 6)] + [peer(i, 40, district="E5") for i in range(6, 12)]
        self.assertIsNone(epc.value_against(me, scan))

    def test_range_area_is_not_used_for_value(self):
        me = peer(0, 60)
        me.area_source, me.epc = None, {"match": "range"}
        self.assertIsNone(epc.value_against(me, [me] + [peer(i, 40) for i in range(1, 10)]))

    def test_annotate_values_is_off_without_epc(self):
        scan = [peer(0, 60)] + [peer(i, 40) for i in range(1, 10)]
        os.environ.pop("EPC_API_TOKEN", None)
        os.environ.pop("EPC_TOKEN", None)
        epc.annotate_values(scan)
        self.assertEqual(scan[0].value, {})
        epc.set_default(make(Fake()))
        try:
            epc.annotate_values(scan)
        finally:
            epc.set_default(None)
        self.assertEqual(scan[0].value["percentile"], 100)

    def test_card_line(self):
        l = epc.annotate(advert(), make(Fake(BUILDING, CERTS)))
        l.value = {"per_m2": 29.7, "percentile": 80, "n": 10, "district": "N7", "beds": 1}
        line = epc.card_line(l, TODAY)
        self.assertEqual(line, "📐 50.5 m² (EPC 2021) · £29.7/m² · roomier than 80% of N7 1-beds (10 seen) · EPC D · "
                               "~£42/mo energy (certificate 2021, Boiler and radiators, mains gas)")

    def test_card_line_range_is_labelled_not_fact(self):
        l = epc.annotate(advert("12 Hornsey Road"), make(Fake(BUILDING, CERTS)))
        self.assertIn("48-52 m² (postcode range of 3 EPCs, not this flat)", epc.card_line(l, TODAY))

    def test_card_line_flags_conflict_with_advert(self):
        l = advert(floor_area_m2=75.0)
        epc.annotate(l, make(Fake(BUILDING, CERTS)))
        self.assertIn("⚠ EPC says 50.5 m²", epc.card_line(l, TODAY))

    def test_card_line_empty_when_nothing_known(self):
        self.assertEqual(epc.card_line(advert(), TODAY), "")


class RightmoveTests(unittest.TestCase):
    def test_details_capture_full_postcode(self):
        d = rightmove._details({"address": {"outcode": "N7", "incode": "6DJ"}})
        self.assertEqual(d["postcode"], "N7 6DJ")
        self.assertIsNone(rightmove._details({"address": {"outcode": "N7"}})["postcode"])
        self.assertIsNone(rightmove._details({})["postcode"])


class ParseCertificateTests(unittest.TestCase):
    def test_nested_values_and_codes(self):
        r = epc.parse_certificate({"data": {"total_floor_area": {"value": 45.8, "quantity": "square metres"},
                                            "habitable_room_count": 2, "property_type": 2}}, "X")
        self.assertEqual((r["area"], r["rooms"], r["flat"]), (45.8, 2, True))

    def test_absurd_area_dropped(self):
        self.assertIsNone(epc.parse_certificate({"total_floor_area": 5})["area"])


class RecordedResponseTests(unittest.TestCase):
    """Real API responses (addresses, UPRNs and certificate numbers replaced), captured 2026-11-10."""
    def setUp(self):
        import json, pathlib
        self.fx = json.loads((pathlib.Path(__file__).parent / "fixtures_epc.json").read_text())

    def test_parse_real_certificates(self):
        for number, payload in self.fx["certificates"].items():
            r = epc.parse_certificate(payload, number)
            self.assertTrue(8 <= r["area"] <= 300)
            self.assertIsInstance(r["rooms"], float)
            self.assertIn(r["band"], list("ABCDEFG"))
            self.assertIsInstance(r["heating"], str)      # main_heating[0].description is a plain string
            self.assertGreater(r["annual_cost"], 0)       # costs arrive as {"value": n, "currency": "GBP"}
            self.assertTrue(r["flat"])

    def test_exact_match_on_recorded_responses(self):
        def http(url, params, headers):
            if url.endswith("/search"):
                return 200, self.fx["search"]
            return 200, self.fx["certificates"][params["certificate_number"]]
        l = advert("Flat 2, 10 Example Road, London AA1 1AA")
        l.full_postcode = "AA1 1AA"
        r = epc.match(l, "AA1 1AA", make(http), dt.date(2026, 11, 10))
        self.assertEqual(r["match"], "exact")
        self.assertEqual(r["low"], epc.parse_certificate(self.fx["certificates"]["0000-0000-0000-0000-0002"])["area"])


class EvalScriptTests(unittest.TestCase):
    def setUp(self):
        import importlib.util, json, pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("eval_epc", root / "scripts" / "eval_epc.py")
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.cache = os.path.join(tempfile.mkdtemp(), "cache.json")
        items = [{"source": "rm", "id": "1", "url": "u", "address": "Flat 3, 12 Hornsey Road, London N7 6DJ",
                  "bedrooms": 1, "scheme": "x", "tenure": "rent", "rent": 1500, "floor_area_m2": 52.0},
                 {"source": "rm", "id": "2", "url": "u", "address": "Somewhere, London N7", "bedrooms": 1,
                  "scheme": "x", "tenure": "rent", "rent": 1500}]
        with open(self.cache, "w") as f:
            json.dump({"rent_search": {"s": {"listings": items}}}, f)
        self.before = open(self.cache).read()

    def test_without_token_stops_after_counting_and_does_not_write(self):
        lines = []
        rc = self.mod.main(["--cache", self.cache], env={}, out=lines.append)
        self.assertEqual(rc, 0)
        self.assertIn("1 cached adverts carry a full postcode", lines[0])
        self.assertIn("NOT run", lines[-1])
        self.assertEqual(open(self.cache).read(), self.before)

    def test_with_client_reports_match_and_error(self):
        lines = []
        self.mod.main(["--cache", self.cache], client=make(Fake(BUILDING, CERTS)), out=lines.append)
        text = "\n".join(lines)
        self.assertIn("exact 1 (100%)", text)
        self.assertIn("-1.5", text)         # certificate 50.5 vs advert 52


if __name__ == "__main__":
    unittest.main()
