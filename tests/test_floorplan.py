"""floorplan.py with a fake client and generated images: no network, no key."""
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from pisobot import epc, floorplan
from pisobot.models import Listing

NOW = dt.datetime(2026, 11, 10, 12, tzinfo=dt.timezone.utc)


def total(quote, sqm=None, sqft=None, scope="whole_flat"):
    return {"quote": quote, "sqm": sqm, "sqft": sqft, "scope": scope}


def raw(totals=(), plan=True, dims=False, beds=(None, ""), kitchen=("unknown", ""), outside=("unknown", "")):
    return {"is_floorplan": plan, "totals": list(totals), "room_dimensions_only": dims,
            "bedrooms": {"count": beds[0], "quote": beds[1]}, "kitchen": {"separate": kitchen[0], "quote": kitchen[1]},
            "outside_space": {"kind": outside[0], "quote": outside[1]}}


class InterpretTests(unittest.TestCase):
    def test_square_metres(self):
        r = floorplan.interpret(raw([total("Total area: 52.3 sq m", sqm=52.3)]))
        self.assertEqual((r["status"], r["area_sqm"], r["unit"]), ("read", 52.3, "sqm"))

    def test_square_feet_converted(self):
        r = floorplan.interpret(raw([total("Total area approx. 563 sq ft", sqft=563)]))
        self.assertEqual((r["status"], r["area_sqm"], r["unit"]), ("read", 52.3, "sqft"))

    def test_spelled_out_units(self):
        r = floorplan.interpret(raw([total("TOTAL AREA: APPROX. 29.3 SQ. METRES (315.0 SQ. FEET)", sqm=29.3, sqft=315.0)]))
        self.assertEqual((r["status"], r["area_sqm"]), ("read", 29.3))
        r = floorplan.interpret(raw([total("TOTAL APPROX. FLOOR AREA 453 SQ.FT. (42.1 SQ.M.)", sqm=42.1, sqft=453)]))
        self.assertEqual((r["status"], r["area_sqm"]), ("read", 42.1))

    def test_both_units_agreeing(self):
        r = floorplan.interpret(raw([total("Total 52.3 sq m (563 sq ft)", sqm=52.3, sqft=563)]))
        self.assertEqual(r["area_sqm"], 52.3)

    def test_units_that_disagree_are_discarded(self):
        r = floorplan.interpret(raw([total("Total 52.3 sq m (600 sq ft)", sqm=52.3, sqft=600)]))
        self.assertEqual(r["status"], "discarded")
        self.assertIn("units disagree", r["reason"])

    def test_two_percent_boundary(self):
        # 565 sq ft = 52.49 m2: 0.4% off 52.3 -> ok; 580 sq ft = 53.88 m2: 3% off -> discarded
        self.assertEqual(floorplan.interpret(raw([total("Total 52.3 sq m 565 sq ft", sqm=52.3, sqft=565)]))["status"], "read")
        self.assertEqual(floorplan.interpret(raw([total("Total 52.3 sq m 580 sq ft", sqm=52.3, sqft=580)]))["status"], "discarded")

    def test_figure_not_in_quote_is_refused(self):
        r = floorplan.interpret(raw([total("Total area: 48 sq m", sqm=52.3)]))
        self.assertEqual(r["status"], "discarded")

    def test_quote_without_unit_is_refused(self):
        self.assertEqual(floorplan.interpret(raw([total("Total 52.3", sqm=52.3)]))["status"], "discarded")

    def test_extras_scopes_are_not_a_total(self):
        for scope in ("includes_extras", "excludes_extras", "part_only", "unclear"):
            r = floorplan.interpret(raw([total("Total 60 sq m including garage", sqm=60, scope=scope)]))
            self.assertEqual(r["status"], "discarded", scope)
            self.assertIsNone(r["area_sqm"])

    def test_whole_flat_chosen_when_extras_also_printed(self):
        r = floorplan.interpret(raw([total("Total area 52 sq m", sqm=52),
                                     total("Total inc. terrace 61 sq m", sqm=61, scope="includes_extras")]))
        self.assertEqual(r["area_sqm"], 52)

    def test_conflicting_whole_flat_totals_discarded(self):
        r = floorplan.interpret(raw([total("Total floor 1: 30 sq m", sqm=30), total("Total floor 2: 22 sq m", sqm=22)]))
        self.assertEqual(r["status"], "discarded")
        self.assertIn("conflicting", r["reason"])

    def test_same_total_printed_twice_is_fine(self):
        r = floorplan.interpret(raw([total("Total area 52 sq m", sqm=52), total("Total area: 52.0 sqm", sqm=52.0)]))
        self.assertEqual(r["area_sqm"], 52)

    def test_range_limits(self):
        self.assertEqual(floorplan.interpret(raw([total("Total 11 sq m", sqm=11)]))["status"], "discarded")
        self.assertEqual(floorplan.interpret(raw([total("Total 12 sq m", sqm=12)]))["status"], "read")
        self.assertEqual(floorplan.interpret(raw([total("Total 250 sq m", sqm=250)]))["status"], "read")
        self.assertEqual(floorplan.interpret(raw([total("Total 251 sq m", sqm=251)]))["status"], "discarded")
        self.assertEqual(floorplan.interpret(raw([total("Total 1,200 sq ft", sqft=1200)]))["area_sqm"], 111.5)
        self.assertEqual(floorplan.interpret(raw([total("Total 5,000 sq ft", sqft=5000)]))["status"], "discarded")

    def test_room_dimensions_only_is_no_total(self):
        r = floorplan.interpret(raw(dims=True))
        self.assertEqual((r["status"], r["reason"], r["area_sqm"]), ("no_total", "room dimensions only", None))

    def test_not_a_floorplan(self):
        self.assertEqual(floorplan.interpret(raw(plan=False))["reason"], "not a floorplan")

    def test_bedrooms_kitchen_outside_need_a_quote(self):
        r = floorplan.interpret(raw([total("Total 52 sq m", sqm=52)], beds=(2, "Bedroom 1, Bedroom 2"),
                                    kitchen=("no", "Open plan kitchen/living"), outside=("balcony", "Balcony")))
        self.assertEqual((r["bedrooms"], r["kitchen_separate"], r["outside"]), (2, "no", "balcony"))
        r = floorplan.interpret(raw([total("Total 52 sq m", sqm=52)], beds=(2, ""), kitchen=("yes", ""), outside=("garden", "")))
        self.assertEqual((r["bedrooms"], r["kitchen_separate"], r["outside"]), (None, "unknown", "unknown"))

    def test_garbage_answers(self):
        self.assertEqual(floorplan.interpret(None)["status"], "discarded")
        self.assertEqual(floorplan.interpret({"is_floorplan": True, "totals": ["x", 3]})["status"], "no_total")

    # --- review finding 1: a quoted number is bound to ITS unit; the model's fields only have to agree
    def test_a_number_is_bound_to_its_own_unit(self):
        r = floorplan.interpret(raw([total("Total 200 sq ft (18.6 sq m)", sqm=200)]))
        self.assertEqual(r["status"], "discarded")                     # was accepted as 200 m2
        ok = floorplan.interpret(raw([total("Total 200 sq ft (18.6 sq m)", sqm=18.6, sqft=200)]))
        self.assertEqual((ok["status"], ok["area_sqm"]), ("read", 18.6))

    def test_a_printed_second_unit_cannot_be_omitted_by_the_model(self):
        self.assertEqual(floorplan.interpret(raw([total("Total 52 sq m (700 sq ft)", sqm=52, sqft=None)]))["status"], "discarded")
        self.assertEqual(floorplan.interpret(raw([total("Total 52 sq m (700 sq ft)", sqm=52, sqft=700)]))["status"], "discarded")
        agree = floorplan.interpret(raw([total("Total 52.3 sq m (563 sq ft)", sqm=52.3, sqft=None)]))
        self.assertEqual((agree["status"], agree["area_sqm"]), ("read", 52.3))      # omitted but consistent: still fine

    def test_an_unrelated_number_in_the_quote_is_not_the_area(self):
        r = floorplan.interpret(raw([total("Total 48 sq m; Drawing reference 52.3", sqm=52.3)]))
        self.assertEqual(r["status"], "discarded")
        self.assertEqual(floorplan.interpret(raw([total("Total 48 sq m; Drawing reference 52.3", sqm=48)]))["area_sqm"], 48)

    def test_several_figures_in_one_quote_are_refused(self):
        r = floorplan.interpret(raw([total("Total 48 sq m and 30 sq m", sqm=48)]))
        self.assertEqual(r["status"], "discarded")

    def test_parse_pairs(self):
        self.assertEqual(floorplan.parse_pairs("Total area: 28.4 m² ... 305 ft²"), [(28.4, "m"), (305.0, "ft")])
        self.assertEqual(floorplan.parse_pairs("485 sq ft / 45.1 sq m"), [(485.0, "ft"), (45.1, "m")])
        self.assertEqual(floorplan.parse_pairs("APPROX. 29.3 SQ. METRES (315.0 SQ. FEET)"), [(29.3, "m"), (315.0, "ft")])
        self.assertEqual(floorplan.parse_pairs("Room 3.2m x 4.1m, 12 metres"), [])
        self.assertEqual(floorplan.parse_pairs("1,200 sq ft"), [(1200.0, "ft")])

    # --- review finding 3: counter-evidence is never outvoted
    def test_the_quote_must_support_the_scope(self):
        for quote in ("Total 60 sq m including garage", "Total 60 sq m excl. terrace", "Total 60 sq m inc. store",
                      "Area 60 sq m", "Ground floor 60 sq m", "Room 4.0m x 3.0m total 12 sq m"):
            r = floorplan.interpret(raw([total(quote, sqm=60 if "12 sq" not in quote else 12)]))
            self.assertEqual(r["status"], "discarded", quote)

    def test_a_contradictory_dimensions_flag_discards_the_plan(self):
        r = floorplan.interpret(raw([total("Room dimensions 12 m2 x 2 m2", sqm=12)], dims=True))
        self.assertEqual(r["status"], "discarded")
        r = floorplan.interpret(raw([total("Total area 52 sq m", sqm=52)], dims=True))
        self.assertEqual(r["status"], "discarded")

    def test_an_invalid_second_whole_flat_total_makes_the_plan_ambiguous(self):
        r = floorplan.interpret(raw([total("Total area 52 sq m", sqm=52), total("Total area 100 sq m (700 sq ft)", sqm=100, sqft=700)]))
        self.assertEqual(r["status"], "discarded")                     # was a confident 52 m2
        self.assertEqual(floorplan.interpret(raw([total("Total area 52 sq m", sqm=52), total("Total area 100 sq m", sqm=100)]))["status"],
                         "discarded")

    def test_a_part_larger_than_the_claimed_whole_discards_it(self):
        r = floorplan.interpret(raw([total("Total area 40 sq m", sqm=40), total("First floor 60 sq m", sqm=60, scope="part_only")]))
        self.assertEqual(r["status"], "discarded")
        ok = floorplan.interpret(raw([total("Total area 52 sq m", sqm=52), total("First floor 20 sq m", sqm=20, scope="part_only")]))
        self.assertEqual(ok["area_sqm"], 52)


def png(w=200, h=100, mode="RGB"):
    out = io.BytesIO()
    Image.new(mode, (w, h), "white").save(out, "PNG")
    return out.getvalue()


class FakeClient:
    def __init__(self, answers, fail=None):
        self.answers, self.fail, self.calls = list(answers), fail, []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise self.fail
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(a))],
                               usage=SimpleNamespace(input_tokens=1500, output_tokens=120,
                                                     cache_read_input_tokens=0, cache_creation_input_tokens=0))


def unique_png(url):
    """Distinct image bytes per url, as distinct plans have."""
    return png(120 + sum(map(ord, url)) % 400, 100)


def reader(client, limit=60, fetcher=None, store=None, **kw):
    return floorplan.Reader(client=client, store=store or floorplan.Store(os.path.join(tempfile.mkdtemp(), "f.db")),
                            daily_limit=limit, now=lambda: NOW, fetcher=fetcher or unique_png, **kw)


def advert(floorplan_url="https://m/plan1.png", beds=1, **kw):
    return Listing(source="rm", id="1", url="u", address="Road, London N7", bedrooms=beds, scheme="Private rent",
                   tenure="rent", rent=1500, floorplan=floorplan_url, description="A flat.", **kw)


GOOD = raw([total("Total area: 52.3 sq m", sqm=52.3)], beds=(1, "Bedroom"))


class ReaderTests(unittest.TestCase):
    def test_image_request_shape(self):
        c = FakeClient([GOOD])
        r = reader(c).read("https://m/plan1.png")
        self.assertEqual(r["area_sqm"], 52.3)
        kw = c.calls[0]
        block = kw["messages"][0]["content"][0]
        self.assertEqual((block["type"], block["source"]["type"], block["source"]["media_type"]), ("image", "base64", "image/jpeg"))
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(kw["system"][0]["cache_control"], {"type": "ephemeral"})

    def test_read_once_per_url(self):
        c = FakeClient([GOOD])
        rd = reader(c)
        rd.read("https://m/plan1.png")
        rd.read("https://m/plan1.png")
        rd2 = reader(c, store=rd.store)
        rd2.read("https://m/plan1.png")          # a new process: SQLite hit
        self.assertEqual(len(c.calls), 1)
        rd.read("https://m/plan2.png")
        self.assertEqual(len(c.calls), 2)

    def test_refusal_is_cached_too(self):
        c = FakeClient([raw(dims=True)])
        rd = reader(c)
        self.assertEqual(rd.read("u")["status"], "no_total")
        rd.read("u")
        self.assertEqual(len(c.calls), 1)

    def test_prompt_version_changes_the_key(self):
        a = floorplan.read_key("u", "m")
        with patch.object(floorplan, "PROMPT_VERSION", "fp-test"):
            self.assertNotEqual(a, floorplan.read_key("u", "m"))

    def test_daily_cap(self):
        c = FakeClient([GOOD])
        rd = reader(c, limit=2)
        self.assertTrue(rd.read("a"))
        self.assertTrue(rd.read("b"))
        self.assertIsNone(rd.read("c"))
        self.assertEqual(len(c.calls), 2)

    def test_api_failure_fails_open_and_is_not_cached(self):
        c = FakeClient([GOOD], fail=RuntimeError("down"))
        rd = reader(c)
        self.assertIsNone(rd.read("u"))
        self.assertIsNone(rd.read("u"))            # backed off: no second request
        self.assertEqual(len(c.calls), 1)
        self.assertEqual(rd.store.get(floorplan.read_key("u", rd.model)), None)

    def test_image_failure_makes_no_api_call(self):
        c = FakeClient([GOOD])
        def boom(url):
            raise OSError("404")
        self.assertIsNone(reader(c, fetcher=boom).read("u"))
        self.assertEqual(c.calls, [])

    def test_not_an_image(self):
        c = FakeClient([GOOD])
        self.assertIsNone(reader(c, fetcher=lambda u: b"%PDF-1.4").read("u"))
        self.assertEqual(c.calls, [])


class ImageTests(unittest.TestCase):
    def test_downscale_and_flatten(self):
        media, data = floorplan.prepare(png(4000, 2000, "RGBA"))
        import base64
        img = Image.open(io.BytesIO(base64.b64decode(data)))
        self.assertEqual((media, max(img.size)), ("image/jpeg", floorplan.MAX_EDGE))
        self.assertEqual(img.size, (1600, 800))

    def test_small_image_not_upscaled(self):
        import base64
        _, data = floorplan.prepare(png(300, 200))
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(data))).size, (300, 200))


class AnnotateTests(unittest.TestCase):
    def test_sets_area_and_source(self):
        l = floorplan.annotate(advert(), reader(FakeClient([GOOD])))
        self.assertEqual((l.floor_area_m2, l.area_source), (52.3, "floorplan"))
        self.assertEqual(l.floorplan_read["quote"], "Total area: 52.3 sq m")
        self.assertEqual(l.check_notes, [])

    def test_portal_area_is_never_replaced_or_read(self):
        c = FakeClient([GOOD])
        l = floorplan.annotate(advert(floor_area_m2=60.0), reader(c))
        self.assertEqual((l.floor_area_m2, l.area_source), (60.0, None))
        self.assertEqual(c.calls, [])

    def test_no_floorplan_no_request(self):
        c = FakeClient([GOOD])
        floorplan.annotate(advert(floorplan_url=None), reader(c))
        self.assertEqual(c.calls, [])

    def test_off_by_default(self):
        c = FakeClient([GOOD])
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOORPLAN_AREA", None)
            l = floorplan.annotate(advert())
        self.assertIsNone(l.area_source)

    def test_enabled_needs_flag_and_key(self):
        env = {"FLOORPLAN_AREA": "1", "STORAGE": "local", "ANTHROPIC_API_KEY": "x"}
        with patch.dict(os.environ, env):
            self.assertTrue(floorplan.enabled())
        for drop in env:
            with patch.dict(os.environ, {k: v for k, v in env.items() if k != drop}, clear=True):
                self.assertFalse(floorplan.enabled(), drop)

    def test_bedroom_contradiction_adds_note_only(self):
        a = raw([total("Total 52 sq m", sqm=52)], beds=(2, "Bedroom 1, Bedroom 2"))
        l = floorplan.annotate(advert(beds=1), reader(FakeClient([a])))
        self.assertEqual(l.check_notes, ["floorplan shows 2 bedrooms, advert says 1"])
        self.assertEqual(l.bedrooms, 1)

    def test_studio_is_not_a_contradiction(self):
        a = raw([total("Total 30 sq m", sqm=30)], beds=(1, "Bedroom"))
        l = floorplan.annotate(advert(beds=0, studio=True), reader(FakeClient([a])))
        self.assertEqual(l.check_notes, [])

    def test_discarded_read_leaves_area_empty_but_records_why(self):
        l = floorplan.annotate(advert(), reader(FakeClient([raw([total("60 sq m inc garage", sqm=60, scope="includes_extras")])])))
        self.assertIsNone(l.floor_area_m2)
        self.assertEqual(l.floorplan_read["status"], "discarded")

    def test_failure_leaves_advert_untouched(self):
        l = advert()
        before = dict(vars(l))
        floorplan.annotate(l, reader(FakeClient([GOOD], fail=RuntimeError("x"))))
        self.assertEqual(vars(l), before)

    def test_sale_listing_skipped(self):
        l = advert()
        l.tenure = "sale"
        c = FakeClient([GOOD])
        floorplan.annotate(l, reader(c))
        self.assertEqual(c.calls, [])


class CardAndValueTests(unittest.TestCase):
    def test_card_line_labels_floorplan(self):
        l = floorplan.annotate(advert(), reader(FakeClient([GOOD])))
        self.assertEqual(epc.card_line(l), "📐 52.3 m² (floorplan)")

    def test_epc_does_not_relabel_a_floorplan_area(self):
        l = floorplan.annotate(advert(), reader(FakeClient([GOOD])))
        l.full_postcode = None
        epc.annotate(l, epc.Epc(http=lambda *a: (404, None), store=epc.Store(os.path.join(tempfile.mkdtemp(), "e.db")),
                                now=lambda: NOW, sleep=lambda s: None))
        self.assertEqual((l.area_source, l.floor_area_m2), ("floorplan", 52.3))

    def test_value_comparison_uses_floorplan_areas(self):
        peers = []
        for i in range(9):
            p = advert(floorplan_url=None)
            p.id, p.floor_area_m2, p.area_source = str(100 + i), 40.0, "floorplan"
            peers.append(p)
        me = floorplan.annotate(advert(), reader(FakeClient([GOOD])))
        self.assertEqual(epc.value_against(me, [me, *peers])["percentile"], 100)

    def test_value_uses_the_floorplan_area_against_portal_stated_comparables(self):
        store = epc.Store(os.path.join(tempfile.mkdtemp(), "v.db"))
        peers = []
        for n in range(9):
            p = advert(floorplan_url=None)
            p.id, p.floor_area_m2, p.rent = str(100 + n), 40.0, 1500
            peers.append(p)
        for p in peers:
            epc.note_portal_area(p, store, NOW)
        me = floorplan.annotate(advert(), reader(FakeClient([GOOD])))
        epc.value_for(me, store, NOW)
        self.assertEqual((me.value["percentile"], me.value["peers"]), (100, "portal-stated areas"))


class EvalScriptTests(unittest.TestCase):
    def setUp(self):
        import importlib.util, pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("eval_floorplan", root / "scripts" / "eval_floorplan.py")
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_display_size(self):
        self.assertEqual(self.mod.parse_display_size("424 sq. ft."), 39.4)
        self.assertEqual(self.mod.parse_display_size("52 sq. m."), 52.0)
        self.assertIsNone(self.mod.parse_display_size(""))

    def test_choose_prefers_ground_truth(self):
        items = [{"id": 1, "numberOfFloorplans": 1, "displaySize": ""}, {"id": 2, "numberOfFloorplans": 0, "displaySize": "300 sq. ft."},
                 {"id": 3, "numberOfFloorplans": 1, "displaySize": "300 sq. ft."}]
        self.assertEqual([x["id"] for x in self.mod.choose(items, 5)], [3, 1])

    def test_choose_blind_sample_prefers_no_size_and_skips_excluded(self):
        items = [{"id": 1, "numberOfFloorplans": 1, "displaySize": "300 sq. ft."}, {"id": 2, "numberOfFloorplans": 1, "displaySize": ""},
                 {"id": 3, "numberOfFloorplans": 1, "displaySize": ""}]
        self.assertEqual([x["id"] for x in self.mod.choose(items, 5, prefer_no_size=True, exclude={"3"})], [2, 1])

    def test_gather_respects_request_budget(self):
        import json as j
        items = [{"id": i, "numberOfFloorplans": 1, "displaySize": "300 sq. ft."} for i in range(30)]
        html = '<script id="__NEXT_DATA__" type="application/json">' + j.dumps(
            {"props": {"pageProps": {"searchResults": {"properties": items}}}}) + "</script>"
        calls = []
        out = os.path.join(tempfile.mkdtemp(), "s.json")
        sample = self.mod.gather(out, 5, (0, 24), page_fn=lambda i: (calls.append("s"), html)[1],
                                 details_fn=lambda x: (calls.append("d"), {"sizings": [{"unit": "sqm", "minimumSize": 28}],
                                                                           "floorplans": [{"url": "https://m/p.png"}]})[1],
                                 out_fn=lambda *_: None)
        self.assertEqual(len(calls), 5)
        self.assertEqual(sample[0]["stated_sqm"], 28)

    def test_run_scores_against_stated_area_and_respects_ledger(self):
        sample = {"adverts": [{"id": "1", "beds": 1, "stated_sqm": 40.0, "floorplans": ["https://m/a.png"]},
                              {"id": "2", "beds": 1, "stated_sqm": 40.0, "floorplans": ["https://m/b.png"]}]}
        answers = [raw([total("Total 40.5 sq m", sqm=40.5)]), raw([total("Total 50 sq m", sqm=50)])]
        c = FakeClient(answers)
        lines = []
        ledger = {"user_authorized_ceiling_usd": 0.3, "total_all_evaluations_estimated_usd": 0.02, "new_tests_reserved_usd": 0.01}
        res = self.mod.run(sample, reader(c), ledger, 40, out=lines.append)
        text = "\n".join(lines)
        self.assertEqual(res["n"], 2)
        self.assertIn("MISS 2", text)
        self.assertIn("within max(2 m², 5%): 1 of 2", text)
        tight = dict(ledger, user_authorized_ceiling_usd=0.0305)
        c2 = FakeClient(answers)
        res = self.mod.run(sample, reader(c2), tight, 40, out=lambda *_: None)
        self.assertEqual((res["n"], len(c2.calls)), (0, 0))
        self.assertIn("stopped", res["stopped"])

    def test_ledger_append(self):
        import json as j
        path = os.path.join(tempfile.mkdtemp(), "l.json")
        open(path, "w").write(j.dumps({"user_authorized_ceiling_usd": 0.3, "total_all_evaluations_estimated_usd": 0.02}))
        self.mod.append_spend(path, 0.004, {"images": 12})
        d = j.load(open(path))
        self.assertAlmostEqual(d["total_all_evaluations_estimated_usd"], 0.024)
        self.assertEqual(d["g6_floorplan_evaluation"]["images"], 12)

    def test_key_loader_does_not_print(self):
        import io, contextlib
        path = os.path.join(tempfile.mkdtemp(), "k.env")
        open(path, "w").write("ANTHROPIC_API_KEY=sk-test-123\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), patch.dict(os.environ, {}, clear=False):
            self.assertTrue(self.mod.load_key(path))
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-test-123")
            os.environ.pop("ANTHROPIC_API_KEY")
        self.assertEqual(buf.getvalue(), "")

    def test_synthetic_images_open(self):
        for name in self.mod.SYNTHETIC:
            Image.open(io.BytesIO(self.mod.synthetic_image(self.mod.SYNTHETIC[name]))).verify()


if __name__ == "__main__":
    unittest.main()
