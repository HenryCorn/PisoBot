"""scripts/eval_extract.py: fixture cache, fake client, no network, never writes the cache."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("eval_extract", ROOT / "scripts" / "eval_extract.py")
eval_extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eval_extract)

ADVERTS = {
    "1": "1 bedroom flat to rent. Bright flat with balcony and lift.",
    "2": "1 bedroom flat to rent. Room to rent in a shared house, shared kitchen.",
    "3": "1 bedroom flat to rent. Spacious one bed, own front door. Ideal for sharers, shared garden.",
    "4": "Studio flat to rent. Modern studio apartment with kitchenette.",
    "5": "1 bedroom flat to rent. Quiet flat near the park, unfurnished.",
    "6": "1 bedroom flat to rent. Top floor flat with views, pay a holding fee before viewing.",
    "7": "1 bedroom flat to rent. Newly refurbished, gas central heating, pets considered.",
    "8": "1 bedroom flat to rent. Garden flat, furnished, bills included.",
    "10": "1 bedroom flat to rent. Entire flat, once marketed as Room to rent, now fully self-contained.",
    "11": "1 bedroom flat to rent. Quiet flat in a converted house, the landlord lives upstairs.",
}


def cache_file(directory):
    listings = [{"source": "rm", "id": i, "url": f"https://x/{i}", "address": f"Secret {i} Road", "bedrooms": 1,
                 "scheme": "Private rent", "tenure": "rent", "rent": 1400, "description": text}
                for i, text in ADVERTS.items()]
    listings.append({"source": "rm", "id": "9", "description": "", "tenure": "rent", "address": "x", "url": "u",
                     "bedrooms": 1, "scheme": "Private rent"})                 # no description: ignored
    listings.append({**listings[0]})                                           # duplicate key: counted once
    path = Path(directory) / "cache.json"
    path.write_text(json.dumps({"rent_search": {"rm": {"at": "2026-11-01T00:00:00+00:00", "listings": listings}},
                                "journey": {"a": {"minutes": 20}}}))
    return path


def reply_for(text):
    def base(kind, confidence="high", evidence="", flags=()):
        return {"kind": kind, "confidence": confidence, "evidence": evidence,
                "facts": {"furnished": "unknown", "lift": "unknown", "heating": "unknown", "bills_included": "unknown",
                          "pets": "unknown", "outdoor_space": "unknown", "condition": "unknown", "floor": "unknown",
                          "min_term_months": None},
                "red_flags": [{"flag": f, "phrase": p} for f, p in flags], "summary": "A flat."}
    if "fully self-contained" in text:
        return base("whole_flat", "high", "fully self-contained")
    if "landlord lives upstairs" in text:
        return base("room_in_shared", "medium", "the landlord lives upstairs")
    if "Room to rent in a shared house" in text:
        return base("room_in_shared", "high", "Room to rent in a shared house")
    if "Studio flat" in text or "studio apartment" in text:
        return base("studio", "high", "Modern studio apartment")
    if "holding fee" in text:
        return base("whole_flat", "high", flags=[("payment_before_viewing", "pay a holding fee before viewing")])
    if "Ideal for sharers" in text:
        return base("whole_flat", "high", "Spacious one bed, own front door")
    return base("whole_flat", "high")


class FakeClient:
    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = kwargs["messages"][0]["content"]
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(reply_for(text)))],
            usage=SimpleNamespace(input_tokens=500, output_tokens=200, cache_read_input_tokens=1100,
                                  cache_creation_input_tokens=0))


class EvalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = cache_file(self.tmp.name)
        self.db = str(Path(self.tmp.name) / "eval.db")
        self.lines = []

    def run_eval(self, *extra, env=None, client=None):
        code = eval_extract.main(["--cache", str(self.cache), "--db", self.db, *extra], env=env or {},
                                 client=client, out=self.lines.append)
        return code, "\n".join(self.lines)

    def test_rules_half_runs_without_a_key_and_says_the_model_half_did_not(self):
        before = hashlib.sha256(self.cache.read_bytes()).hexdigest()
        code, out = self.run_eval()
        self.assertEqual(code, 0)
        self.assertIn("Rules over 10 cached adverts", out)          # 10 with text, the duplicate counted once
        self.assertIn("rules reject rm:2", out)
        self.assertIn("rules reject rm:4", out)
        self.assertIn("ANTHROPIC_API_KEY is not set: the model half of the evaluation was NOT run", out)
        self.assertNotIn("Agreement table", out)
        self.assertEqual(hashlib.sha256(self.cache.read_bytes()).hexdigest(), before)    # never written
        self.assertFalse(Path(self.db).exists())

    def test_a_key_without_the_sdk_is_reported_not_crashed_and_never_printed(self):
        with patch.dict(sys.modules, {"anthropic": None}):
            code, out = self.run_eval(env={"ANTHROPIC_API_KEY": "sk-ant-secret-value"})
        self.assertEqual(code, 2)
        self.assertIn("'anthropic' package is not installed", out)
        self.assertNotIn("sk-ant-secret-value", out)

    def test_with_a_client_every_rejected_advert_is_sampled_and_the_tables_print(self):
        client = FakeClient()
        code, out = self.run_eval("--sample", "5", client=client)
        self.assertEqual(code, 0)
        self.assertIn("Sample: 5 adverts = 3 the rules reject + 2 random others", out)
        self.assertEqual(len(client.calls), 5)
        sent = [c["messages"][0]["content"] for c in client.calls]
        self.assertTrue(any("Room to rent in a shared house" in t for t in sent))
        self.assertTrue(any("Modern studio apartment" in t for t in sent))
        self.assertTrue(all("Secret" not in t for t in sent))                       # no addresses sent
        self.assertIn("Agreement table", out)
        self.assertIn("room_in_shared", out)
        self.assertIn("Measured over 5 adverts", out)
        self.assertIn("Cost per advert", out)
        self.assertIn("Projected at 300 new adverts a day", out)

    def test_disagreements_are_listed_with_the_quoted_phrase_and_the_policy_outcome(self):
        code, out = self.run_eval("--sample", "10", client=FakeClient())
        section = out.split("Disagreements")[1].split("Red flags raised")[0]
        self.assertIn("rm:10", section)
        self.assertIn("rules=reject model=whole_flat/high -> rescued (final: keep)", section)
        self.assertIn("quote: fully self-contained", section)
        self.assertIn("rm:11", section)
        self.assertIn("rules=keep   model=room_in_shared/medium -> warned (final: keep)", section)
        self.assertIn("quote: the landlord lives upstairs", section)
        for agreed in ("rm:2 ", "rm:4 ", "rm:3 "):               # rules and model agree on these
            self.assertNotIn(agreed, section)
        self.assertIn("Red flags raised: 1", out)
        self.assertIn("payment_before_viewing: pay a holding fee before viewing", out)

    def test_a_rerun_is_free_because_results_are_cached_in_the_scratch_db(self):
        client = FakeClient()
        self.run_eval("--sample", "10", client=client)
        first = len(client.calls)
        self.lines.clear()
        self.run_eval("--sample", "10", client=client)
        self.assertEqual(len(client.calls), first)

    def test_an_unknown_models_usage_is_reported_without_inventing_a_price(self):
        with patch.object(eval_extract.extract.config, "EXTRACT_MODEL", "undocumented-model"):
            code, out = self.run_eval("--sample", "5", client=FakeClient())
        self.assertEqual(code, 0)
        self.assertIn("Measured over 5 adverts", out)
        self.assertIn("Cost unavailable: No documented extraction pricing for undocumented-model", out)
        self.assertNotIn("Projected at", out)

    def test_sample_always_contains_every_rejected_advert_even_when_bigger_than_the_sample(self):
        listings = eval_extract.load_listings(str(self.cache))
        rejected = {l.key for l in listings if eval_extract.rules_verdict(l)[0]}
        chosen = eval_extract.choose_sample(listings, rejected, 1, seed=1)
        self.assertEqual({l.key for l in chosen}, rejected)
        a = eval_extract.choose_sample(listings, rejected, 6, seed=3)
        b = eval_extract.choose_sample(listings, rejected, 6, seed=3)
        self.assertEqual([l.key for l in a], [l.key for l in b])                   # reproducible
        self.assertEqual(len(a), 6)

    def test_an_empty_or_missing_cache_is_reported(self):
        self.cache.write_text(json.dumps({"rent_search": {}}))
        code, out = self.run_eval()
        self.assertEqual(code, 1)
        self.assertIn("No cached rental adverts", out)


if __name__ == "__main__":
    unittest.main()
