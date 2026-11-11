"""Independent PR17 seam cases. Synthetic state, fake model, external HTTP blocked."""
import contextlib
import datetime as dt
import html
import importlib.util
import json
import os
import re
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
import test_wp23_final_review_repros as old
from pisobot import archive, bot, config, costs, d1, extract, main, net, prefs, renter, sweep, telegram, tracker, users, watch
from pisobot.sources import rightmove
from test_archive import advert, at
from test_extract import FakeClient, record
from test_hardening_round2 import HEAD, Repro
from test_sweep import JudgingBase
from test_wp3 import NOW, publish
from requests import ConnectionError

EVIDENCE = {}


def register_guest():
    with patch.dict(os.environ, {"TELEGRAM_INVITE_USERNAMES": "bea"}):
        return users.register("200", "Guest", "bea")


def execute(c, items, baseline=False):
    return main._run(["rm"], False, False, rentals=True, watch=True, baseline=baseline,
                     prefetched=(items, []))


class ReleaseSeams(unittest.TestCase):
    def test_repeat_unseen_when_reappearance_is_sweep_only_or_filtered(self):
        results = []
        for filtered_head in (False, True):
            with self.subTest(filtered_head=filtered_head), old.case(JudgingBase) as c:
                item = advert(4, 1400, furnishing="furnished")
                archive.upsert([item], "head", at=at(0))
                old.saved(item)
                old.disappear(item, 2)
                first = archive.change_id(item.key, "status", "unseen")
                sweep.notify_unseen()
                self.assertEqual(len(c.texts), 1)
                archive.upsert([item], "sweep", at=at(4))
                if filtered_head:
                    with patch.object(config, "FURNISHED", "unfurnished"), \
                            patch.object(archive, "now", return_value=at(4)):
                        execute(c, [item])
                flag = prefs.load()["saved"][item.key].get("unseen")
                old.disappear(item, 6)
                second = archive.change_id(item.key, "status", "unseen")
                queued = len(archive.pending_unseen("100"))
                sweep.notify_unseen()
                result = {"filtered_head": filtered_head, "episode_ids": [first, second],
                          "flag_after_reappearance": flag, "notices": len(c.texts),
                          "pending_before": queued, "pending_after": len(archive.pending_unseen("100")),
                          "second_notice_rows": tracker.query("SELECT * FROM notices WHERE event=?", second)}
                results.append(result)
                EVIDENCE["repeat_unseen"] = results
                self.assertEqual(len(c.texts), 2, result)

    def test_sweep_only_poisoned_first_batch_does_not_starve_a_healthy_second_batch(self):
        with old.case() as c:
            old.baseline(c)
            bad = [advert(100 + i, 1250) for i in range(sweep.EVALUATE_BATCH)]
            good = advert(999, 1500)
            c.archive_sweep(HEAD + bad + [good], hour=12)
            attempts, successes = [], []
            def send(item, reason):
                attempts.append(item.key)
                if item.key != good.key:
                    raise RuntimeError("Synthetic rejected photo")
                successes.append(item.key)
            per_cycle = []
            c.quarter = 52
            with patch.object(config, "MAX_ALERTS_PER_RUN", 40), \
                    patch.object(telegram, "send_listing", side_effect=send):
                for quarter in range(3):
                    start = len(attempts)
                    self.assertIsInstance(c.cycle(HEAD), RuntimeError)
                    per_cycle.append(len(attempts) - start)
            EVIDENCE["sweep_poison_prefix"] = {
                "batch_size": sweep.EVALUATE_BATCH, "attempt_cap": 40,
                "attempts_per_cycle": per_cycle, "healthy_attempted": good.key in attempts,
                "successes": successes, "healthy_seen": good.key in c.seen(),
                "healthy_verdict": [r for r in archive.verdicts_since("100", 1) if r["key"] == good.key],
            }
            self.assertIn(good.key, attempts, EVIDENCE["sweep_poison_prefix"])

    def test_unseen_only_delivery_failures_suppress_healthy_cycle_heartbeat(self):
        with old.case() as c:
            old.baseline(c)
            item = advert(44, 1400)
            archive.upsert([item], "head", at=at(0))
            old.saved(item)
            old.disappear(item, 2)
            tries = []
            def down(text, **kwargs):
                tries.append(text[:50])
                raise RuntimeError("Synthetic unseen notification failure")
            ping = Mock(return_value=Mock(raise_for_status=Mock()))
            results = []
            c.quarter = 16
            before = sweep._meta("watch_last_success")
            with patch.dict(os.environ, {"HEALTHCHECK_URL": "https://offline.invalid/heartbeat"}), \
                    patch.object(net.session, "get", ping), \
                    patch.object(telegram, "send_text", side_effect=down):
                for quarter in range(3):
                    results.append(type(c.cycle(HEAD)).__name__)
            EVIDENCE["unseen_delivery_health"] = {
                "notification_attempts": len(tries), "cycle_errors": results,
                "healthcheck_pings": ping.call_count,
                "pending_events": len(archive.pending_unseen("100")),
                "success_before": before, "success_after": sweep._meta("watch_last_success"),
            }
            self.assertEqual(ping.call_count, 0, EVIDENCE["unseen_delivery_health"])

    def test_rejected_pipeline_notices_obey_the_attempt_limit(self):
        with old.case(JudgingBase):
            count = bot.NOTICES_PER_TICK + 5
            for i in range(count):
                item = advert(100 + i, 1400)
                archive.upsert([item], "head")
                tracker.move(publish(item), "enquired")
                archive.upsert([advert(100 + i, 1300)], "head")
            client = Mock()
            client.call.side_effect = bot.TelegramError("Synthetic rejected request", permanent=True)
            handler = bot.Handler("100", Mock(return_value=True), client)
            attempts = []
            for _ in range(3):
                start = client.call.call_count
                handler.tick()
                attempts.append(client.call.call_count - start)
            rows = tracker.query("SELECT state, COUNT(*) AS n FROM notices GROUP BY state")
            EVIDENCE["pipeline_attempt_limit"] = {
                "configured_limit": bot.NOTICES_PER_TICK, "changed_homes": count,
                "requests_per_tick": attempts, "notice_states": rows,
            }
            self.assertLessEqual(max(attempts), bot.NOTICES_PER_TICK, EVIDENCE["pipeline_attempt_limit"])

    def test_rejected_notice_prefix_rotates_without_marking_any_episode_delivered(self):
        with old.case(JudgingBase):
            keys = []
            for i in range(15):
                item = advert(100 + i, 1400)
                archive.upsert([item], "head")
                tracker.move(publish(item), "enquired")
                archive.upsert([advert(100 + i, 1300)], "head")
                keys.append(item.key)
            seen, per_tick = set(), []
            def rejected(text, markup):
                seen.update(re.findall(r"https://example\.com/(\d+)", text))
                per_tick[-1] += 1
                raise bot.TelegramError("Synthetic rejected request", permanent=True)
            fingerprint = main.settings_key()
            for _ in range(3):
                per_tick.append(0)
                self.assertEqual(tracker.notify_changes(rejected, limit=10), 0)
            self.assertTrue(all(n <= 10 for n in per_tick))
            self.assertEqual(seen, {str(100 + i) for i in range(15)})
            self.assertEqual(main.settings_key(), fingerprint)
            self.assertEqual(tracker.query("SELECT * FROM notices WHERE state='sent'"), [])

    def test_crawl_output_failure_remains_unhealthy_after_its_deadline_is_consumed(self):
        with old.case() as c:
            old.baseline(c)
            ticks = [0]
            def failed_output(*args, **kwargs):
                ticks[0] += config.DEEP_SWEEP_STEP_SECONDS + 1
                raise RuntimeError("Synthetic crawl notification/storage failure")
            with patch.object(sweep, "crawl", side_effect=failed_output):
                result = sweep.step(["rm"], [users.owner()], clock=lambda: ticks[0])
            self.assertFalse(result["complete"])
            self.assertEqual(len(result["errors"]), 1)

    def test_stale_token_cannot_ack_reject_or_release_replacement_lease(self):
        with old.case(JudgingBase):
            item = advert(4, 1400)
            archive.upsert([item], "head")
            event = archive.change_id(item.key, "rent", 1400)
            first = tracker.claim(item.key, "rent", event, at=NOW)
            later = NOW + tracker.NOTICE_LEASE + dt.timedelta(seconds=1)
            second = tracker.claim(item.key, "rent", event, at=later)
            self.assertNotEqual(first.token, second.token)
            tracker.ack(item.key, "rent", event, token=first.token)
            tracker.reject(item.key, "rent", event, token=first.token)
            tracker.release(item.key, "rent", event, token=first.token)
            row, = tracker.query("SELECT * FROM notices")
            self.assertEqual((row["state"], row["token"], row["claims"]), ("pending", second.token, 2))
            self.assertEqual(tracker.claim(item.key, "rent", event, at=later), "busy")
            tracker.ack(item.key, "rent", event, token=second.token)
            self.assertEqual(tracker.claim(item.key, "rent", event, at=later), "told")
            EVIDENCE["lease_fencing"] = "stale ack/reject/release leave replacement pending; replacement can ack"

    def test_model_cap_is_shared_across_profiles_head_sweep_and_cached_results(self):
        with old.case() as c:
            friend = register_guest()
            self.assertIsNotNone(friend)
            old.baseline(c)
            client = FakeClient(record("whole_flat", "high", "Entire flat"))
            ex = extract.Extractor(client=client, daily_limit=3, now=lambda: NOW)
            extract.set_default(ex)
            self.addCleanup(extract.set_default, None)
            items = [advert(100 + i, 1300, description=f"Entire flat with a separate bedroom; public advert {i}.")
                     for i in range(5)]
            self.assertIsNone(c.cycle(HEAD + items))
            self.assertIsNone(c.cycle(HEAD + items))
            extra = advert(999, 1400, description="Entire flat with a separate bedroom; separate sweep advert.")
            c.archive_sweep(HEAD + items + [extra], hour=12)
            c.quarter = 52
            self.assertIsNone(c.cycle(HEAD + items))
            count = ex.store.requests_today(NOW.date().isoformat())
            raw = client.calls[0]["messages"][0]["content"]
            original_input = html.unescape(raw[len("<advert_data>\n"):-len("\n</advert_data>")])
            ex.memory.clear()
            with users.use(friend):
                cached = ex.extract_text(original_input)
            EVIDENCE["model_daily_cap"] = {"configured_limit": 3, "fake_requests": len(client.calls),
                                           "persisted_requests": count, "cached_after_cap": cached is not None}
            self.assertEqual((len(client.calls), count), (3, 3))
            self.assertIsNotNone(cached)

    def test_full_staged_pipeline_keeps_hard_gates_before_any_model_request(self):
        scenarios = [
            ("rent_cap", advert(10, 1700, description="Entire flat"), {}),
            ("studio", advert(11, 1300, bedrooms=0, description="Studio apartment."), {"STUDIO_MIN_AREA": None}),
            ("studio_area", advert(12, 1300, bedrooms=0, description="Studio apartment.", floor_area_m2=20),
             {"STUDIO_MIN_AREA": 35}),
            ("furnishing", advert(13, 1300, furnishing="unfurnished", description="Entire unfurnished flat."),
             {"FURNISHED": "furnished"}),
            ("keywords", advert(14, 1300, description="Double room to rent in a self-contained flat."), {}),
            ("area_excluded", advert(15, 1300, description="Entire flat"), {"AREAS_EXCLUDE": ("E5",)}),
            ("walk", advert(16, 1300, description="Entire flat"), {"MAX_WALK_MINUTES": 1}),
            ("travel", advert(17, 1300, description="Entire flat"), {"MAX_TRAVEL_MINUTES": 10}),
        ]
        measured = []
        for expected, item, settings in scenarios:
            with self.subTest(expected=expected), old.case(JudgingBase) as c:
                client = FakeClient(record("whole_flat", "high", "Entire flat"))
                ex = extract.Extractor(client=client)
                extract.set_default(ex)
                try:
                    with contextlib.ExitStack() as stack:
                        for name, value in settings.items():
                            stack.enter_context(patch.object(config, name, value))
                        execute(c, [item])
                    outcome = archive.verdicts_since("100", 1)[0]["outcome"]
                    measured.append({"expected": expected, "outcome": outcome, "fake_requests": len(client.calls)})
                    self.assertEqual(outcome, expected)
                    self.assertEqual(client.calls, [])
                finally:
                    extract.set_default(None)
        EVIDENCE["model_hard_gates"] = measured

    def test_real_live_marker_migrates_two_custom_profiles_without_replaying_seen(self):
        with old.case() as c:
            friend = register_guest()
            owner_params = {"rent": 1500, "studios": 35, "furnished": "unfurnished",
                            "walk": 10, "changes": 1, "areas": {"exclude": ["SW1"], "include": []},
                            "destination": {"name": "Synthetic Office", "lat": 51.5152, "lng": -.1419}}
            guest_params = {"rent": 1300, "studios": None, "furnished": "any", "walk": 15, "changes": 2}
            users.save_params(users.owner(), owner_params)
            users.save_params(friend, guest_params)
            people = users.everyone()
            homes = [advert(1, 1200, furnishing="unfurnished"), advert(2, 1210, furnishing="unfurnished")]
            for home in homes:
                costs.compute(home)
            before = {}
            for person in people:
                with users.use(person):
                    main.save_seen({home.key: main._record(home, "2026-11-09", True) for home in homes})
                    old.saved(homes[0])
                    d1.query("INSERT INTO hidden(key) VALUES ('rm:777')")
                    d1.query("INSERT INTO meta(name,value) VALUES ('watch_initialized','1')")
                    d1.query("INSERT INTO meta(name,value) VALUES ('watch_settings',?)",
                             json.dumps(person.params, sort_keys=True))
                    before[person.chat_id] = {"params": person.params, "prefs": prefs.load()}
            sent = []
            newer = advert(3, 1230, furnishing="unfurnished")
            with patch.object(telegram, "send_listing", side_effect=lambda l, r: sent.append((users.current_chat(), l.key, r))):
                self.assertIsNone(c.cycle(homes + [newer]))
                self.assertIsNone(c.cycle(homes + [newer]))
            after = {}
            for person in users.everyone():
                with users.use(person):
                    p = prefs.load()
                    after[person.chat_id] = {"params": person.params,
                                            "saved": sorted(p["saved"]), "hidden": p["hidden"],
                                            "seen": sorted(main.load_seen()),
                                            "marker": sweep._meta("watch_settings")}
                    self.assertEqual(person.params, before[person.chat_id]["params"])
                    self.assertIn(homes[0].key, p["saved"])
                    self.assertIn("rm:777", p["hidden"])
            EVIDENCE["live_profile_migration"] = {"deliveries": sent, "profiles": after,
                                                 "baseline_notices": [t for t in c.texts if "watch is live" in t]}
            self.assertCountEqual(sent, [("100", newer.key, "new"), ("200", newer.key, "new")])
            self.assertEqual(EVIDENCE["live_profile_migration"]["baseline_notices"], [])

    def test_attempt_allowances_are_per_person_and_shared_with_the_sweep(self):
        with old.case() as c:
            self.assertIsNotNone(register_guest())
            old.baseline(c)
            items = [advert(100 + i, 1300) for i in range(4)]
            attempts = {}
            def down(item, reason):
                chat = users.current_chat()
                attempts[chat] = attempts.get(chat, 0) + 1
                raise RuntimeError("Synthetic failure")
            with patch.object(config, "MAX_ALERTS_PER_RUN", 2), \
                    patch.object(telegram, "send_listing", side_effect=down):
                self.assertIsInstance(c.cycle(HEAD + items), RuntimeError)
            EVIDENCE["person_attempt_budgets"] = attempts
            self.assertEqual(attempts, {"100": 2, "200": 2})

    def test_crawl_request_reservation_survives_actual_process_death(self):
        with old.case(JudgingBase) as c:
            log = Path(c.tmp.name) / "portal-attempts.txt"
            people = [c.owner]
            def crash(*args):
                old.accepted(log, "attempt")
                os._exit(73)
            with patch.object(config, "DEEP_SWEEP_MAX_REQUESTS", 2):
                def child():
                    with patch.object(rightmove, "band_page", side_effect=crash):
                        sweep.crawl("rm", people, 100, clock=lambda: 0)
                self.assertEqual(old.fork_crash(child), 73)
                def failure(*args):
                    old.accepted(log, "attempt")
                    raise ConnectionError("Synthetic portal failure")
                with patch.object(rightmove, "band_page", side_effect=failure):
                    for _ in range(5):
                        sweep.crawl("rm", people, 100, clock=lambda: 0)
            last = archive.last_sweep("rm")
            EVIDENCE["portal_request_crash_cap"] = {
                "observed_attempts": len(log.read_text().splitlines()),
                "reserved_requests": last["requests"], "complete": last["complete"], "note": last["note"]}
            self.assertEqual((len(log.read_text().splitlines()), last["requests"]), (2, 2))
            self.assertFalse(last["complete"])


if __name__ == "__main__":
    with patch.object(socket.socket, "connect", side_effect=AssertionError("Live sockets forbidden")):
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(ReleaseSeams))
    print("\nRELEASE_SEAM_EVIDENCE\n" + json.dumps(EVIDENCE, indent=2, sort_keys=True))
    raise SystemExit(not result.wasSuccessful())
