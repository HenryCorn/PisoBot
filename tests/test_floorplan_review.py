"""Re-review findings for G6 (round 3): ported reproductions, asserting the redesigned behaviour. Offline: fakes, and a loopback
HTTP server on 127.0.0.1 (no external network)."""
import contextlib
import datetime as dt
import http.server
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from PIL import Image

from pisobot import budget, config, epc, extract, facts, floorplan, main, telegram, watch
from pisobot.models import Listing

from test_floorplan import GOOD, NOW, FakeClient, advert, png, raw, reader, total, unique_png
from test_users import Base

ROOT = Path(__file__).resolve().parents[1]


def new_reader(client=None, **kw):
    kw.setdefault("fetcher", unique_png)
    return reader(client or FakeClient([GOOD]), **kw)


# ---------------------------------------------------------------- content-only caching (no URL trust, no URL-keyed cache)

class ContentCacheTests(unittest.TestCase):
    def test_a_replaced_image_is_a_different_reading_immediately(self):
        images = [png(300, 100)]
        client = FakeClient([GOOD, raw([total("Total area 70 sq m", sqm=70)])])
        rd = reader(client, fetcher=lambda url: images[0])
        self.assertEqual(floorplan.annotate(advert(), rd).floor_area_m2, 52.3)
        images[0] = png(300, 150)                                    # no waiting window: the next read sees the new bytes
        self.assertEqual(floorplan.annotate(advert(), rd).floor_area_m2, 70)

    def test_a_different_advert_at_the_same_url_gets_its_own_bytes(self):
        served = {"a": png(300, 100), "b": png(300, 150)}
        current = ["a"]
        client = FakeClient([GOOD, raw([total("Total area 80 sq m", sqm=80)])])
        rd = reader(client, fetcher=lambda url: served[current[0]])
        first = floorplan.annotate(advert(), rd)
        current[0] = "b"
        other = advert()
        other.id = "different-advert"
        second = floorplan.annotate(other, rd)
        self.assertEqual((first.floor_area_m2, second.floor_area_m2), (52.3, 80))

    def test_every_read_downloads_once_and_identical_bytes_share_one_model_request(self):
        downloads, client = [], FakeClient([GOOD])
        rd = reader(client, fetcher=lambda url: downloads.append(url) or png(300, 100))
        rd.read("https://image.invalid/plan.png?sig=1", "a")
        rd.read("https://image.invalid/plan.png?sig=2", "b")
        rd.read("https://image.invalid/plan.png?sig=1", "a")
        self.assertEqual((len(downloads), len(client.calls)), (3, 1))   # one download per read; one request for one image

    def test_there_is_no_url_keyed_state(self):
        for name in ("fetch", "clear_images", "_images", "IMAGE_RECHECK_HOURS"):
            self.assertFalse(hasattr(floorplan, name), name)
        rd = new_reader()
        rd.read("u", "a")
        with rd.store._db() as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertNotIn("floorplan_images", names)

    def test_a_reading_older_than_thirty_days_is_not_trusted(self):
        client = FakeClient([GOOD])
        rd = reader(client, fetcher=lambda url: png(300, 100))
        rd.read("u", "a")
        rd.now = lambda: NOW + dt.timedelta(days=floorplan.READ_DAYS + 1)
        rd.read("u", "a")
        self.assertEqual(len(client.calls), 2)

    def test_a_prompt_version_bump_invalidates_readings(self):
        client = FakeClient([GOOD])
        rd = reader(client, fetcher=lambda url: png(300, 100))
        rd.read("u", "a")
        with patch.object(floorplan, "PROMPT_VERSION", "review-version"):
            rd.read("u", "a")
        self.assertEqual(len(client.calls), 2)

    def test_expired_rows_counters_and_failures_are_deleted_even_when_the_feature_is_off(self):
        path = os.path.join(tempfile.mkdtemp(), "p.db")
        self.assertEqual(floorplan.purge_expired(NOW, path), {})
        self.assertFalse(os.path.exists(path))
        store = floorplan.Store(path)
        old = NOW - dt.timedelta(days=45)
        store.put("old", "c", "m", {"status": "read"}, {}, old.isoformat())
        store.put("new", "c", "m", {"status": "read"}, {}, (NOW - dt.timedelta(days=5)).isoformat())
        store.fail("a", "h", old - dt.timedelta(seconds=floorplan.FAIL_BACKOFF_SECONDS))
        store.reserve_request((old).date().isoformat(), 5)
        deleted = floorplan.purge_expired(NOW, path)
        self.assertEqual((deleted["floorplan_reads"], deleted["floorplan_failures"], deleted["floorplan_usage"]), (1, 1, 1))
        self.assertIsNotNone(store.get("new"))


# ---------------------------------------------------------------- budgets and caps

class BudgetTests(unittest.TestCase):
    def test_invalid_images_are_charged_to_the_download_budget(self):
        calls = []
        def bad(url):
            calls.append(url)
            return b"<html>not an image</html>"
        rd = new_reader(fetcher=bad, limit=1, download_limit=5)
        for i in range(30):
            rd.read(f"https://image.invalid/{i}.png", str(i))
        self.assertEqual(len(calls), 5)

    def test_an_exhausted_cap_writes_nothing_against_the_advert(self):
        rd = new_reader(download_limit=0)
        for i in range(10):
            rd.read(f"https://image.invalid/{i}.png", str(i))
        with rd.store._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM floorplan_failures").fetchone()[0], 0)    # was ten rows
        self.assertFalse(hasattr(rd, "failed"))                                                        # and no in-memory table

    def test_a_failure_is_not_retried_by_a_restarted_reader(self):
        failure = FakeClient([GOOD], fail=TimeoutError("stubbed"))
        rd = new_reader(failure, fetcher=lambda url: png(300, 100))
        self.assertIsNone(rd.read("https://image.invalid/retry.png", "a"))
        success = FakeClient([GOOD])
        reboot = reader(success, store=rd.store, fetcher=lambda url: png(300, 100))
        self.assertIsNone(reboot.read("https://image.invalid/retry.png", "a"))
        self.assertEqual(len(failure.calls) + len(success.calls), 1)
        reboot.now = lambda: NOW + dt.timedelta(seconds=floorplan.FAIL_BACKOFF_SECONDS + 5)
        self.assertEqual(reboot.read("https://image.invalid/retry.png", "a")["status"], "read")

    def test_the_cap_day_is_the_day_of_the_request_actually_sent(self):
        clock = [NOW.replace(hour=23, minute=59, second=59)]
        def fetch(url):
            clock[0] = NOW + dt.timedelta(days=1)
            return unique_png(url)
        client = FakeClient([GOOD])
        rd = new_reader(client, fetcher=fetch, limit=1)
        rd.now = lambda: clock[0]
        rd.read("before-midnight.png", "a")
        rd.read("after-midnight.png", "b")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(rd.store.requests_today((NOW + dt.timedelta(days=1)).date().isoformat()), 1)

    def test_two_readers_cannot_exceed_the_same_sqlite_cap(self):
        path = os.path.join(tempfile.mkdtemp(), "race.db")
        floorplan.Store(path).requests_today("2026-11-10")
        barrier = threading.Barrier(2)
        class Racing(floorplan.Store):
            def reserve_request(self, day, limit):
                barrier.wait(timeout=3)
                return super().reserve_request(day, limit)
        clients = [FakeClient([GOOD]), FakeClient([GOOD])]
        readers = [reader(clients[i], limit=1, store=Racing(path), fetcher=unique_png) for i in range(2)]
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(readers[i].read(f"https://i.invalid/{i}.png", str(i))))
                   for i in range(2)]
        [t.start() for t in threads]
        [t.join(5) for t in threads]
        self.assertEqual(sorted(bool(r) for r in results), [False, True])

    def test_every_write_uses_a_short_busy_timeout_and_is_skipped_on_contention(self):
        store = floorplan.Store(os.path.join(tempfile.mkdtemp(), "b.db"))
        store.requests_today("2026-11-10")
        db = sqlite3.connect(store.path)
        self.addCleanup(db.close)
        db.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        rd = new_reader(store=store)
        result = rd.read("https://image.invalid/plan.png", "a")
        db.rollback()
        self.assertIsNone(result)                                     # skipped, not waited for
        self.assertLess(time.monotonic() - started, 2.5)


# ---------------------------------------------------------------- the bounded download (a real loopback server)

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        mode = self.path.strip("/")
        if mode == "ok":
            body = png(100, 50)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif mode == "html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html></html>")
        elif mode == "trickle":                      # one byte every 50 ms, forever: every read is "alive"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            try:
                for _ in range(2000):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass
        elif mode == "stall":                        # headers, then silence
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.flush()
            time.sleep(3)
        elif mode == "huge":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            try:
                for _ in range(400):
                    self.wfile.write(b"x" * 65536)
            except OSError:
                pass
        elif mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "/html")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.enterContext(patch.object(floorplan, "ALLOW_LOCAL_HOSTS", True))
        self.enterContext(patch.object(floorplan.net, "session", requests.Session()))

    def test_a_good_image(self):
        self.assertEqual(floorplan.download(f"{self.base}/ok"), png(100, 50))

    def test_a_trickling_server_is_cut_off_at_the_callers_deadline(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            floorplan.download(f"{self.base}/trickle", seconds=0.5)
        self.assertLess(time.monotonic() - started, 1.5)           # every read succeeded; only the total deadline stopped it

    def test_a_server_that_stalls_after_the_headers_is_cut_off(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            floorplan.download(f"{self.base}/stall", seconds=0.5)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_the_time_budget_shortens_the_deadline(self):
        token = budget.DEADLINE.set(time.monotonic() + 0.4)
        try:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                floorplan.download(f"{self.base}/trickle", seconds=10)
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            budget.DEADLINE.reset(token)

    def test_an_oversized_body_is_cut_off_at_the_byte_cap(self):
        with patch.object(floorplan, "MAX_IMAGE_BYTES", 200_000):
            with self.assertRaises(ValueError):
                floorplan.download(f"{self.base}/huge")

    def test_non_image_types_and_redirects_to_them_are_refused(self):
        for path in ("html", "redirect"):
            with self.assertRaises(ValueError):
                floorplan.download(f"{self.base}/{path}")

    def test_address_urls_are_refused_unless_a_test_allows_them(self):
        with patch.object(floorplan, "ALLOW_LOCAL_HOSTS", False):
            for url in ("http://127.0.0.1:9/a.png", "http://localhost/a.png", "file:///etc/passwd", "ftp://x/y.png"):
                with self.assertRaises(ValueError):
                    floorplan.download(url)

    def test_a_hung_read_does_not_block_a_reader(self):
        rd = new_reader(fetcher=lambda url: floorplan.download(url, seconds=0.4))
        started = time.monotonic()
        self.assertIsNone(rd.read(f"{self.base}/stall", "a"))
        self.assertLess(time.monotonic() - started, 1.5)


class PrepareTests(unittest.TestCase):
    def test_pixels_are_checked_from_the_header_before_any_decoding(self):
        decoded = []
        real = Image.Image.load
        def load(self, *a, **k):
            decoded.append(self.size)
            return real(self, *a, **k)
        content = png(25, 25)
        with patch.object(floorplan, "MAX_PIXELS", 100), patch.object(Image.Image, "load", load):
            with self.assertRaises(ValueError):
                floorplan.prepare(content)
        self.assertEqual(decoded, [])

    def test_the_default_pixel_cap_is_modest(self):
        self.assertLessEqual(floorplan.MAX_PIXELS, 12_000_000)

    def test_nothing_full_size_is_allocated_after_the_decode(self):
        original_new = Image.new
        allocated = []
        def track(mode, size, *a, **k):
            allocated.append((mode, size))
            return original_new(mode, size, *a, **k)
        content = png(800, 400, "RGBA")
        with patch.object(floorplan, "MAX_EDGE", 16), patch.object(Image, "new", track):
            floorplan.prepare(content)
        self.assertTrue(all(max(size) <= 16 for _, size in allocated))

    def test_transparent_plans_are_flattened_on_white(self):
        out = io.BytesIO()
        Image.new("RGBA", (50, 50), (0, 0, 0, 0)).save(out, "PNG")
        import base64
        _, data = floorplan.prepare(out.getvalue())
        img = Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")
        self.assertGreater(min(img.getpixel((10, 10))), 240)


# ---------------------------------------------------------------- the parser (re-review)

class ParserTests(unittest.TestCase):
    def test_real_world_forms(self):
        self.assertEqual(floorplan.parse_pairs("1,076 sq ft"), [(1076.0, "ft")])
        self.assertEqual(floorplan.parse_pairs("52.3 sq. m."), [(52.3, "m")])
        self.assertEqual(floorplan.parse_pairs("52.3 m2 / 563 ft2"), [(52.3, "m"), (563.0, "ft")])
        self.assertEqual(floorplan.parse_pairs("Area 52.3 sqm"), [(52.3, "m")])
        self.assertEqual(floorplan.parse_pairs("52,3 sq m"), [(52.3, "m")])               # decimal comma

    def test_a_decimal_comma_total_is_read(self):
        r = floorplan.interpret(raw([total("Total area 52,3 sq m", sqm=52.3)]))
        self.assertEqual((r["status"], r["area_sqm"]), ("read", 52.3))

    def test_a_range_is_never_a_definite_area(self):
        for quote in ("Total 40-60 sq m", "Total 40 - 60 sq m", "Total 40–60 m²", "Total 40 to 60 sq m", "Total 430-645 sq ft"):
            for sqm, sqft in ((60, None), (40, None), (None, 645)):
                r = floorplan.interpret(raw([total(quote, sqm=sqm, sqft=sqft)]))
                self.assertEqual(r["status"], "discarded", (quote, sqm, sqft))
                self.assertIsNone(r["area_sqm"])
        self.assertIn("range", floorplan.interpret(raw([total("Total 40-60 sq m", sqm=60)]))["reason"])

    def test_a_reference_next_to_a_total_does_not_make_a_range(self):
        r = floorplan.interpret(raw([total("Total area 52 sq m (plan 12-34)", sqm=52)]))
        self.assertEqual(r["area_sqm"], 52)

    def test_a_quote_with_more_than_one_candidate_total_is_refused(self):
        self.assertEqual(floorplan.interpret(raw([total("Total 48 sq m / total 52 sq m", sqm=48)]))["status"], "discarded")
        self.assertEqual(floorplan.interpret(raw([total("Total 48 sq m (516 sq ft) or 600 sq ft", sqm=48, sqft=516)]))["status"], "discarded")


# ---------------------------------------------------------------- the step uses it; the card and the alert path are untouched

class StepTests(Base):
    def setUp(self):
        super().setUp()
        self.replies = []
        self.env = {"FLOORPLAN_AREA": "1", "STORAGE": "local", "ANTHROPIC_API_KEY": "x"}

    def queue(self, listing, chat="100", message_id=900):
        facts.enqueue([facts.capture(listing, {"result": {"message_id": message_id}}, chat)])

    def test_a_floorplan_area_is_delivered_as_a_threaded_reply_after_the_alert(self):
        l = advert()
        self.queue(l)
        rd = new_reader(fetcher=lambda url: png(300, 100))
        with patch.dict(os.environ, self.env):
            stats = facts.run_step(NOW, 20, lambda c, m, t: self.replies.append((c, m, t)), floorplan_reader=rd, store=epc.Store(
                os.path.join(tempfile.mkdtemp(), "e.db")))
        self.assertEqual(stats["sent"], 1)
        chat, mid, text = self.replies[0]
        self.assertEqual((chat, mid), ("100", 900))
        self.assertIn("52.3 m² (floorplan)", text)

    def test_a_bedroom_contradiction_is_a_check_advert_note_in_the_reply(self):
        l = advert(beds=1)
        self.queue(l)
        answer = raw([total("Total area 52 sq m", sqm=52)], beds=(2, "Bedroom 1, Bedroom 2"))
        rd = new_reader(FakeClient([answer]), fetcher=lambda url: png(300, 100))
        with patch.dict(os.environ, self.env):
            facts.run_step(NOW, 20, lambda c, m, t: self.replies.append(t), floorplan_reader=rd)
        self.assertIn("Check advert: floorplan shows 2 bedrooms, advert says 1", self.replies[0])

    def test_a_portal_stated_area_is_never_read_from_the_plan(self):
        l = advert(floor_area_m2=60.0)
        self.queue(l)
        client = FakeClient([GOOD])
        with patch.dict(os.environ, self.env):
            facts.run_step(NOW, 20, lambda *a: self.replies.append(a), floorplan_reader=new_reader(client))
        self.assertEqual(client.calls, [])

    def test_the_alert_path_makes_no_download_and_no_model_request(self):
        for target in (patch.object(main.enrich, "geocode"), patch.object(main.enrich, "assign_crime_percentiles"),
                       patch.object(main, "passes_area", return_value=None), patch.object(main, "_ensure_photos"),
                       patch.object(telegram, "throttle"), patch.object(telegram, "send_text")):
            target.start()
            self.addCleanup(target.stop)
        self.enterContext(patch.object(main, "passes_basic", side_effect=lambda l, **kw: main._cost_reject(l)))
        from test_users import home
        with_plan = home(2, 1300)
        with_plan.floorplan = "https://image.invalid/slow.png"
        listings = [home(1, 1200)]
        self.enterContext(patch.object(main.SOURCES["rm"], "fetch", side_effect=lambda: list(listings)))
        sent = []
        self.enterContext(patch.object(telegram, "send_listing", side_effect=lambda l, r: (sent.append(l.key), {"result": {"message_id": 5}})[1]))
        watch.Watcher(["rm"]).cycle()
        listings[:] = [home(1, 1200), with_plan]
        boom = AssertionError("optional work ran in the alert path")
        with patch.dict(os.environ, self.env), patch.object(floorplan, "download", side_effect=boom), \
                patch.object(floorplan, "annotate", side_effect=boom), patch.object(floorplan, "Reader", side_effect=boom):
            watch.Watcher(["rm"]).cycle()
        self.assertEqual(sent, ["rm:2"])
        self.assertEqual(facts.pending(), 1)                          # the plan is queued for the post-alert step, nothing more


class OffTests(unittest.TestCase):
    def test_the_card_header_code_is_exactly_the_releases(self):
        try:
            changed = subprocess.check_output(["git", "-C", str(ROOT), "diff", "--name-only", "origin/release-archive-pipeline", "--",
                                               "pisobot/collage.py", "pisobot/telegram.py"], text=True, stderr=subprocess.DEVNULL)
        except Exception:
            self.skipTest("git not available")
        self.assertEqual(changed.split(), ["pisobot/telegram.py"])      # only the additive send_reply; collage.py is untouched

    def test_a_retained_default_reader_is_not_authority_to_run(self):
        rd = new_reader()
        floorplan.set_default(rd)
        self.addCleanup(floorplan.set_default, None)
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(floorplan.enabled())
            floorplan.annotate(advert())
        self.assertEqual(rd._client.calls, [])

    def test_off_creates_no_floorplan_tables_and_makes_no_call(self):
        state = tempfile.mkdtemp()
        l = advert()
        with patch.dict(os.environ, {"STORAGE": "local", "PISOBOT_STATE_DIR": state}, clear=True), \
                patch.object(floorplan, "Reader", side_effect=AssertionError("a Reader was created while off")):
            self.assertFalse(facts.enabled())
            floorplan.annotate(l)
            facts.purge_expired()
        self.assertIsNone(l.area_source)
        db = Path(state, "pisobot.db")
        if db.exists():
            with contextlib.closing(sqlite3.connect(db)) as conn:
                names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            self.assertFalse({n for n in names if n.startswith(("epc_", "floorplan_", "facts_"))})


# ---------------------------------------------------------------- the evaluator's ledger

class EvaluatorTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("eval_floorplan_review", ROOT / "scripts" / "eval_floorplan.py")
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def ledger(self, ceiling):
        return {"user_authorized_ceiling_usd": ceiling, "total_all_evaluations_estimated_usd": 0, "new_tests_reserved_usd": 0}

    def test_the_guard_covers_the_real_maximum_request(self):
        rd = new_reader()
        self.assertGreaterEqual(self.mod.worst_case_cost(rd.model), extract.cost_usd(8000, config.EXTRACT_MAX_TOKENS, model=rd.model) - 1e-12)
        # the largest image we send, with the newer tokenizer and the instructions, fits the reserve
        image_tokens = (floorplan.MAX_EDGE * floorplan.MAX_EDGE / 750) * 1.3
        self.assertLess(image_tokens + 1500, self.mod.WORST_INPUT_TOKENS)

    def test_a_response_at_the_input_and_output_limits_cannot_exceed_a_one_request_ledger(self):
        class Client(FakeClient):
            def create(self, **kw):
                r = super().create(**kw)
                r.usage.input_tokens, r.usage.output_tokens = self.mod_input, config.EXTRACT_MAX_TOKENS
                return r
        client = Client([GOOD])
        client.mod_input = self.mod.WORST_INPUT_TOKENS
        rd = new_reader(client)
        guard = self.mod.worst_case_cost(rd.model)
        result = self.mod.run({"adverts": [{"id": "1", "floorplans": ["a.png"]}]}, rd, self.ledger(guard), out=lambda *a: None)
        self.assertLessEqual(result["spent"], guard + 1e-12)

    def test_billed_truncated_responses_are_charged_and_stop_the_run(self):
        class Client(FakeClient):
            def create(self, **kw):
                r = super().create(**kw)
                r.stop_reason = "max_tokens"
                r.usage.input_tokens, r.usage.output_tokens = 4000, 2500
                return r
        client = Client([GOOD])
        rd = new_reader(client)
        guard = self.mod.worst_case_cost(rd.model)
        sample = {"adverts": [{"id": str(i), "floorplans": [f"t{i}.png"]} for i in range(5)]}
        result = self.mod.run(sample, rd, self.ledger(guard * 1.5), out=lambda *a: None)
        self.assertEqual(len(client.calls), 1)
        self.assertGreater(result["spent"], 0)

    def test_a_timeout_is_charged_at_the_worst_case(self):
        client = FakeClient([GOOD], fail=TimeoutError("stubbed"))
        rd = new_reader(client)
        guard = self.mod.worst_case_cost(rd.model)
        sample = {"adverts": [{"id": str(i), "floorplans": [f"t{i}.png"]} for i in range(5)]}
        result = self.mod.run(sample, rd, self.ledger(guard * 2.5), out=lambda *a: None)
        self.assertEqual(len(client.calls), 2)
        self.assertAlmostEqual(result["spent"], 2 * guard, places=9)

    def test_a_request_billed_after_midnight_is_never_negative_spend(self):
        """re-review: tokens_before came from day 1 and was subtracted from day 2's counter."""
        class Client(FakeClient):
            def create(self, **kw):
                r = super().create(**kw)
                r.usage.input_tokens, r.usage.output_tokens = 3000, 300
                return r
        clock = [NOW.replace(hour=23, minute=59, second=59)]
        rd = new_reader(Client([GOOD]), fetcher=lambda url: (clock.__setitem__(0, NOW + dt.timedelta(days=1)), unique_png(url))[1])
        rd.now = lambda: clock[0]
        rd.store.add_tokens(NOW.date().isoformat(), {"input": 8000, "output": 0})
        result = self.mod.run({"adverts": [{"id": "1", "floorplans": ["a.png"]}]}, rd, self.ledger(1.0), out=lambda *a: None)
        self.assertGreater(result["spent"], 0)
        self.assertAlmostEqual(result["spent"], extract.cost_usd(3000, 300, model=rd.model), places=9)

    def test_concurrent_evaluators_cannot_exceed_a_shared_ceiling(self):
        path = os.path.join(tempfile.mkdtemp(), "ledger.json")
        with open(path, "w") as f:
            json.dump(self.ledger(0.0035), f)
        results = []
        barrier = threading.Barrier(4)
        def worker():
            barrier.wait(timeout=3)
            results.append(self.mod.reserve_spend(path, 0.001))
        threads = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in threads]
        [t.join(5) for t in threads]
        self.assertEqual(sorted(results), [False, True, True, True])
        with open(path) as f:
            self.assertAlmostEqual(json.load(f)["total_all_evaluations_estimated_usd"], 0.003, places=9)

    def test_the_worst_case_is_reserved_in_the_file_before_the_request_and_settled_after(self):
        path = os.path.join(tempfile.mkdtemp(), "ledger.json")
        with open(path, "w") as f:
            json.dump(self.ledger(1.0), f)
        seen = []
        class Client(FakeClient):
            def create(inner, **kw):
                with open(path) as f:
                    seen.append(json.load(f)["total_all_evaluations_estimated_usd"])
                return super().create(**kw)
        rd = new_reader(Client([GOOD]))
        guard = self.mod.worst_case_cost(rd.model)
        result = self.mod.run({"adverts": [{"id": "1", "floorplans": ["a.png"]}]}, rd, self.ledger(1.0), out=lambda *a: None, ledger_path=path)
        self.assertAlmostEqual(seen[0], guard, places=9)
        with open(path) as f:
            final = json.load(f)["total_all_evaluations_estimated_usd"]
        self.assertAlmostEqual(final, result["spent"], places=9)
        self.assertLess(final, guard)


class ControlTests(unittest.TestCase):
    def test_bad_json_shapes_fail_open(self):
        for payload in ({"bedrooms": 3}, {"totals": 7}, {"kitchen": ["not an object"]}):
            rd = new_reader(FakeClient([payload]))
            l = advert(floorplan_url=str(payload))
            floorplan.annotate(l, rd)
            self.assertIsNone(l.area_source)

    def test_the_area_does_not_change_ranking(self):
        from pisobot import ranking
        l = advert(monthly_cost=1500, travel_minutes=20, crime_score=10)
        before = ranking.score(l)
        floorplan.annotate(l, new_reader())
        self.assertEqual(ranking.score(l), before)


if __name__ == "__main__":
    unittest.main()
