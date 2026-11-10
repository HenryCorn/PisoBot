"""Language-model advert reading: fake client only, no network, no API key."""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pisobot import config, extract, main
from pisobot.models import Listing
from pisobot.sources import PRIVATE_RENT, rightmove

TEXT = ("Bright one bedroom flat on the second floor with a lift. Shared kitchen and bathroom with two "
        "other tenants. Pay a holding fee by bank transfer before we arrange any viewing. 12 month minimum.")


def listing(description=TEXT, **kw):
    values = dict(source="rm", id="1", url="https://example.com/1", address="10 Secret Street, London E5",
                  bedrooms=1, scheme=PRIVATE_RENT, tenure="rent", rent=1400, lat=51.52, lng=-0.14,
                  description=description, available="2026-12-01", min_deposit=1615)
    values.update(kw)
    return Listing(**values)


def record(kind="whole_flat", confidence="high", evidence="", flags=(), **facts):
    return {"kind": kind, "confidence": confidence, "evidence": evidence,
            "facts": {"furnished": "furnished", "lift": "yes", "heating": "unknown", "bills_included": "unknown",
                      "pets": "unknown", "outdoor_space": "unknown", "condition": "good", "floor": "2nd",
                      "min_term_months": 12, **facts},
            "red_flags": [{"flag": f, "phrase": p} for f, p in flags],
            "summary": "One bed flat, second floor with a lift."}


class FakeClient:
    """Stands in for anthropic.Anthropic(): records every request, returns canned replies."""

    def __init__(self, reply=None, error=None, stop_reason="end_turn", raw=None):
        self.reply, self.error, self.stop_reason, self.raw, self.calls = reply, error, stop_reason, raw, []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        text = self.raw if self.raw is not None else json.dumps(self.reply)
        return SimpleNamespace(
            stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=600, output_tokens=250, cache_read_input_tokens=1200,
                                  cache_creation_input_tokens=0))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = extract.Store(str(Path(self.tmp.name) / "extract.db"))
        self.clock = [dt.datetime(2026, 11, 10, 9, 0, tzinfo=dt.timezone.utc)]
        self.addCleanup(extract.set_default, None)

    def extractor(self, client, **kw):
        return extract.Extractor(client=client, store=self.store, now=lambda: self.clock[0], **kw)


class ValidationTests(unittest.TestCase):
    def test_a_good_record_is_accepted_and_normalised(self):
        ex = extract.validate(record("room_in_shared", "high", "Shared kitchen and bathroom with two other tenants",
                                     [("payment_before_viewing", "Pay a holding fee by bank transfer")]), TEXT)
        self.assertEqual((ex.kind, ex.confidence), ("room_in_shared", "high"))
        self.assertEqual(ex.evidence, "Shared kitchen and bathroom with two other tenants")
        self.assertEqual(ex.red_flags, [{"flag": "payment_before_viewing", "phrase": "Pay a holding fee by bank transfer"}])
        self.assertEqual((ex.facts["floor"], ex.facts["min_term_months"], ex.facts["lift"]), ("2nd", 12, "yes"))

    def test_quotes_must_really_occur_in_the_advert(self):
        ex = extract.validate(record("room_in_shared", "high", "this is a room in a shared house",
                                     [("off_platform_contact", "message me on WhatsApp")]), TEXT)
        self.assertEqual(ex.evidence, "")             # invented: dropped, so it can never justify a rejection
        self.assertEqual(ex.red_flags, [])

    def test_quote_matching_ignores_case_whitespace_quote_marks_and_joins_ellipsis_parts(self):
        self.assertTrue(extract.quoted("SHARED   kitchen\nand bathroom", TEXT))
        self.assertTrue(extract.quoted("“Shared kitchen” … 12 month minimum", TEXT))
        self.assertFalse(extract.quoted("shared kitchen ... roof terrace", TEXT))
        self.assertFalse(extract.quoted("", TEXT))
        self.assertFalse(extract.quoted("abc", TEXT))      # too short to be evidence

    def test_unexpected_values_become_unknown_and_bad_kinds_are_rejected(self):
        raw = record(lift="maybe", furnished="lovely", min_term_months=999, floor="  ")
        ex = extract.validate(raw, TEXT)
        self.assertEqual((ex.facts["lift"], ex.facts["furnished"], ex.facts["min_term_months"], ex.facts["floor"]),
                         ("unknown", "unknown", None, "unknown"))
        for bad in (record("palace"), record(confidence="certain"), "text", None, {**record(), "facts": 3}):
            with self.assertRaises(ValueError):
                extract.validate(bad, TEXT)

    def test_summary_is_one_short_line(self):
        raw = record()
        raw["summary"] = "Line one.\n\nLine   two. " + "x" * 400
        ex = extract.validate(raw, TEXT)
        self.assertNotIn("\n", ex.summary)
        self.assertLessEqual(len(ex.summary), 160)

    def test_the_schema_is_the_structured_output_shape(self):
        self.assertEqual(set(extract.SCHEMA["required"]), set(extract.SCHEMA["properties"]))
        self.assertIs(extract.SCHEMA["additionalProperties"], False)
        self.assertEqual(extract.SCHEMA["properties"]["kind"]["enum"], list(extract.KINDS))
        self.assertIs(extract.SCHEMA["properties"]["facts"]["additionalProperties"], False)
        self.assertIn("unknown", extract.SCHEMA["properties"]["facts"]["properties"]["furnished"]["enum"])
        json.dumps(extract.SCHEMA)


class InputTests(unittest.TestCase):
    def test_only_advert_text_and_public_listing_fields_are_sent(self):
        text = extract.build_input(listing(address="10 Secret Street, London E5"))
        for private in ("Secret Street", "E5", "example.com"):
            self.assertNotIn(private, text)
        for public in ("Rightmove", "Bedrooms: 1", "Rent per month (GBP): 1400", "Deposit (GBP): 1615",
                       "Available from: 2026-12-01", "Advert text:", "Shared kitchen"):
            self.assertIn(public, text)

    def test_the_input_carries_nothing_about_people_or_settings(self):
        text = extract.build_input(listing())
        for word in ("chat", "budget", "commute", "saved", "TELEGRAM"):
            self.assertNotIn(word, text)

    def test_the_hash_depends_on_text_model_and_prompt_version(self):
        a = extract.input_hash("x", "m")
        self.assertEqual(a, extract.input_hash("x", "m"))
        self.assertNotEqual(a, extract.input_hash("y", "m"))
        self.assertNotEqual(a, extract.input_hash("x", "other"))
        with patch.object(extract, "PROMPT_VERSION", "2"):
            self.assertNotEqual(a, extract.input_hash("x", "m"))

    def test_fixed_instructions_are_long_enough_to_cache(self):
        self.assertGreater(len(extract.INSTRUCTIONS), 3000)    # well over 512 tokens


class RequestTests(Base):
    def test_request_shape_structured_output_caching_and_no_sampling(self):
        client = FakeClient(record())
        ex = self.extractor(client).extract_text("advert text")
        self.assertEqual(ex.kind, "whole_flat")
        [call] = client.calls
        self.assertEqual(call["model"], "claude-haiku-5-5")
        self.assertEqual(call["messages"], [{"role": "user", "content": "advert text"}])
        self.assertEqual(call["system"], [{"type": "text", "text": extract.INSTRUCTIONS,
                                           "cache_control": {"type": "ephemeral"}}])
        self.assertEqual(call["output_config"]["format"], {"type": "json_schema", "schema": extract.SCHEMA})
        self.assertEqual(call["output_config"]["effort"], "low")
        for forbidden in ("temperature", "top_p", "top_k", "tools", "tool_choice", "thinking"):
            self.assertNotIn(forbidden, call)
        self.assertGreaterEqual(call["max_tokens"], 1000)

    def test_an_advert_is_only_ever_sent_once(self):
        client = FakeClient(record())
        first = self.extractor(client)
        first.extract_text("same advert")
        first.extract_text("same advert")
        self.assertEqual(len(client.calls), 1)
        again = self.extractor(client)                 # a new process: the SQLite cache answers
        self.assertEqual(again.extract_text("same advert").kind, "whole_flat")
        self.assertEqual(len(client.calls), 1)
        again.extract_text("a different advert")
        self.assertEqual(len(client.calls), 2)

    def test_a_new_prompt_version_or_model_asks_again(self):
        client = FakeClient(record())
        self.extractor(client).extract_text("advert")
        with patch.object(extract, "PROMPT_VERSION", "2"):
            self.extractor(client).extract_text("advert")
        self.extractor(client, model="claude-sonnet-5-5").extract_text("advert")
        self.assertEqual(len(client.calls), 3)

    def test_usage_and_cost_are_stored(self):
        client = FakeClient(record())
        extractor = self.extractor(client)
        extractor.extract_text("advert")
        self.assertEqual(extractor.last_usage, {"input": 600, "output": 250, "cache_read": 1200, "cache_write": 0})
        self.assertAlmostEqual(extract.cost_usd(**{"input_tokens": 600, "output_tokens": 250, "cache_read": 1200}),
                               (600 * 0.10 + 1200 * 0.01 + 250 * 0.50) / 1e6)

    def test_daily_limit_caps_requests_and_resets_next_day(self):
        client = FakeClient(record())
        extractor = self.extractor(client, daily_limit=2)
        self.assertIsNotNone(extractor.extract_text("a"))
        self.assertIsNotNone(extractor.extract_text("b"))
        self.assertIsNone(extractor.extract_text("c"))
        self.assertEqual(len(client.calls), 2)
        self.assertIsNotNone(extractor.extract_text("a"))          # cached results stay free
        self.clock[0] += dt.timedelta(days=1)
        self.assertIsNotNone(extractor.extract_text("c"))
        self.assertEqual(len(client.calls), 3)

    def test_every_failure_fails_open_and_is_counted(self):
        for name, client in {
            "timeout": FakeClient(error=TimeoutError("slow")),
            "http": FakeClient(error=RuntimeError("500")),
            "refusal": FakeClient(record(), stop_reason="refusal"),
            "truncated": FakeClient(record(), stop_reason="max_tokens"),
            "not json": FakeClient(raw="Sorry, here is some prose"),
            "wrong shape": FakeClient(record("palace")),
            "empty": FakeClient(raw=""),
        }.items():
            with self.subTest(name):
                extractor = self.extractor(client)
                self.assertIsNone(extractor.extract_text(f"advert {name}"))
                decision = extractor.decide(listing(f"advert {name}"), True)
                self.assertEqual((decision.reject, decision.outcome, decision.notes), (True, "rules", []))
                self.assertFalse(extractor.decide(listing(f"advert {name} again"), False).reject)
        with self.store._db() as db:
            row = db.execute("SELECT requests, failures FROM extraction_usage").fetchone()
        self.assertEqual(row["requests"], row["failures"])

    def test_a_failed_request_is_not_cached_so_it_is_retried(self):
        client = FakeClient(error=RuntimeError("down"))
        extractor = self.extractor(client)
        self.assertIsNone(extractor.extract_text("advert"))
        client.error, client.reply = None, record()
        self.assertIsNotNone(extractor.extract_text("advert"))

    def test_storage_trouble_also_fails_open(self):
        extractor = extract.Extractor(client=FakeClient(record()), store=extract.Store("/nonexistent-dir/x/y/db"))
        with patch("os.makedirs", side_effect=OSError("read-only")):
            self.assertIsNone(extractor.extract_text("advert"))


class PolicyTests(Base):
    n = 0

    def decide(self, rules_reject, rec, description=TEXT, fresh=True):
        """fresh: make the advert text unique, because an advert already seen is answered from the cache."""
        if fresh:
            PolicyTests.n += 1
            description = f"{description} Reference {PolicyTests.n}."
        return self.extractor(FakeClient(rec)).decide(listing(description), rules_reject)

    def test_rescue_only_for_a_confident_whole_flat(self):
        d = self.decide(True, record("whole_flat", "high"))
        self.assertEqual((d.reject, d.outcome), (False, "rescued"))
        for rec in (record("whole_flat", "medium"), record("whole_flat", "low"), record("studio", "high"),
                    record("room_in_shared", "high", "Shared kitchen")):
            d = self.decide(True, rec)
            self.assertTrue(d.reject, rec["kind"])
            self.assertNotEqual(d.outcome, "rescued")

    def test_reject_needs_a_reject_kind_high_confidence_and_a_real_quote(self):
        for kind in extract.REJECT_KINDS:
            d = self.decide(False, record(kind, "high", "Shared kitchen and bathroom"))
            self.assertEqual((d.reject, d.outcome), (True, "rejected"), kind)
        for rec in (record("room_in_shared", "high", "this is a house share"),      # invented quote
                    record("room_in_shared", "high", ""),
                    record("room_in_shared", "medium", "Shared kitchen"),
                    record("short_let", "low", "Shared kitchen"),
                    record("studio", "high", "Shared kitchen"),
                    record("other", "high", "Shared kitchen")):
            d = self.decide(False, rec)
            self.assertFalse(d.reject, rec)
            self.assertEqual(d.outcome, "warned")
            self.assertEqual(len(d.notes), 1)
            self.assertIn("may", d.notes[0])

    def test_agreement_changes_nothing_and_adds_no_note(self):
        d = self.decide(False, record("whole_flat", "medium"))
        self.assertEqual((d.reject, d.outcome, d.notes), (False, "agreed", []))
        d = self.decide(True, record("room_in_shared", "high", "Shared kitchen"))
        self.assertEqual((d.reject, d.outcome), (True, "agreed"))

    def test_red_flags_become_check_advert_notes_on_kept_adverts_only(self):
        rec = record("whole_flat", "high", flags=[("payment_before_viewing", "Pay a holding fee by bank transfer"),
                                                   ("other", "invented words")])
        d = self.decide(False, rec)
        self.assertEqual(d.notes, ["asks for payment before a viewing"])
        d = self.decide(False, record("room_in_shared", "high", "Shared kitchen",
                                      flags=[("payment_before_viewing", "holding fee")]))
        self.assertEqual((d.reject, d.notes), (True, []))

    def test_every_disagreement_is_recorded_once_with_its_phrase(self):
        self.decide(True, record("whole_flat", "high"), "Plain text one bedroom flat to rent")
        self.decide(False, record("room_in_shared", "high", "Shared kitchen"), fresh=False)
        self.decide(False, record("studio", "medium"), "A studio style one bed with its own door")
        self.decide(False, record("whole_flat", "high"), "Fine flat")                     # agreement: nothing
        self.decide(True, record("room_in_shared", "high", "Shared kitchen"), "Another advert")   # agreement
        self.decide(False, record("room_in_shared", "high", "Shared kitchen"), fresh=False)       # repeat: one row
        rows = self.store.disagreements()
        self.assertEqual(sorted((r["rules"], r["model_kind"], r["outcome"]) for r in rows),
                         [("keep", "room_in_shared", "rejected"), ("keep", "studio", "warned"),
                          ("reject", "whole_flat", "rescued")])
        rejected = next(r for r in rows if r["outcome"] == "rejected")
        self.assertEqual((rejected["phrase"], rejected["key"]), ("Shared kitchen", "rm:1"))
        self.assertEqual(len(rows), 3)


class PipelineTests(Base):
    def setUp(self):
        super().setUp()
        for target in (patch.object(rightmove, "refine", return_value=True),
                       patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "STORAGE": ""})):
            target.start()
            self.addCleanup(target.stop)

    def install(self, rec, **kw):
        client = FakeClient(rec, **kw)
        extract.set_default(self.extractor(client))
        return client

    def test_without_a_key_the_rules_decide_exactly_as_before_and_no_client_is_built(self):
        self.assertFalse(extract.enabled())
        with patch.object(extract, "Extractor", side_effect=AssertionError("must not be built")):
            self.assertEqual(main.passes_basic(listing("Room in a shared house, bills included")), "keywords")
            self.assertIsNone(main.passes_basic(listing("Lovely flat")))
        self.assertTrue(listing("Room in a shared house").excluded_by_keywords())

    def test_enabled_needs_a_key_local_storage_and_no_kill_switch(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "STORAGE": "local"}):
            self.assertTrue(extract.enabled())
            with patch.dict(os.environ, {"EXTRACT_DISABLE": "1"}):
                self.assertFalse(extract.enabled())
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "STORAGE": ""}):
            self.assertFalse(extract.enabled())
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "  ", "STORAGE": "local"}):
            self.assertFalse(extract.enabled())

    def test_a_rules_rejected_flat_is_rescued_when_the_model_is_sure(self):
        text = "Double room in a lovely shared flat - one bedroom with its own entrance"
        self.assertEqual(main.passes_basic(listing(text)), "keywords")          # layer off: the rules reject
        client = self.install(record("whole_flat", "high", "one bedroom with its own entrance"))
        l = listing(text)
        self.assertTrue(l.excluded_by_keywords())
        self.assertIsNone(main.passes_basic(l))
        self.assertEqual(len(client.calls), 1)          # the same text is not sent a second time
        self.assertEqual(l.ai_summary, "One bed flat, second floor with a lift.")
        self.assertEqual(l.ai_facts["lift"], "yes")

    def test_a_rules_rejected_flat_stays_rejected_when_the_model_is_only_fairly_sure(self):
        self.install(record("whole_flat", "medium"))
        self.assertEqual(main.passes_basic(listing("Double room in a lovely shared flat")), "keywords")

    def test_the_model_is_only_asked_after_the_free_checks_pass(self):
        client = self.install(record("whole_flat", "high"))
        far = listing("Double room in a lovely shared flat", lat=52.5)
        self.assertEqual(main.passes_basic(far), "too far")
        dear = listing("Double room in a lovely shared flat", rent=2500)
        self.assertEqual(main.passes_basic(dear), "cost")
        self.assertEqual(client.calls, [])

    def test_a_rules_kept_flat_is_rejected_only_on_a_quoted_high_confidence_reject_kind(self):
        text = "Nice place. Tenants have the run of the property with the landlord's family."
        self.install(record("room_in_shared", "high", "the run of the property with the landlord's family"))
        l = listing(text)
        self.assertFalse(l.excluded_by_keywords())
        self.assertEqual(main.passes_basic(l), "keywords")

    def test_in_between_keeps_the_advert_and_shows_a_check_advert_warning(self):
        self.install(record("room_in_shared", "medium", "the landlord lives upstairs"))
        l = listing("A flat in a converted house; the landlord lives upstairs.")
        self.assertFalse(l.excluded_by_keywords())
        self.assertIsNone(main.passes_basic(l))
        self.assertEqual(l.check_notes, ["may be a room in shared accommodation"])
        self.assertIn("⚠️ Check advert: may be a room in shared accommodation", l.feature_tags)

    def test_red_flags_show_on_the_card_and_the_summary_is_a_line(self):
        from pisobot import telegram
        self.install(record("whole_flat", "high", flags=[("payment_before_viewing", "Pay a holding fee by bank transfer")]))
        l = listing()
        l.description = TEXT.replace("Shared kitchen and bathroom with two other tenants. ", "")
        l.description += " Pay a holding fee by bank transfer before we arrange any viewing."
        self.assertIsNone(main.passes_basic(l))
        l.travel_minutes, l.crime_score, l.monthly_cost = 20, 30, 1700
        card = telegram.format_listing(l)
        self.assertIn("Check advert: asks for payment before a viewing", card)
        self.assertIn("ℹ️ <i>One bed flat, second floor with a lift.</i>", card)

    def test_a_failing_model_leaves_the_rules_verdict_untouched(self):
        self.install(None, error=TimeoutError("slow"))
        self.assertEqual(main.passes_basic(listing("Room in a shared house")), "keywords")
        self.assertIsNone(main.passes_basic(listing("Lovely flat")))

    def test_sale_listings_never_reach_the_model(self):
        client = self.install(record("whole_flat", "high"))
        sale = listing("Shared ownership flat", tenure="sale")
        self.assertFalse(extract.keyword_verdict(sale, False))
        self.assertTrue(extract.keyword_verdict(sale, True))
        self.assertEqual(client.calls, [])
        self.assertEqual(sale.check_notes, [])


class NoNetworkTests(unittest.TestCase):
    def test_importing_the_module_does_not_import_the_sdk_or_touch_a_key(self):
        code = ("import sys, os; os.environ.pop('ANTHROPIC_API_KEY', None); import pisobot.extract as e; "
                "print('anthropic' in sys.modules, e.enabled())")
        import subprocess
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=str(Path(__file__).resolve().parents[1])).stdout.split()
        self.assertEqual(out, ["False", "False"])


if __name__ == "__main__":
    unittest.main()
