"""Comprehensive offline integration seam tests for wp23-reviewed-integration.

Reconciles:
1. WP2 Hardening durable ledger, retry clocks, and shared allowances (4a5ab61).
2. WP3 pipeline, interactive prompt replay, urgency tiers, and shared change_id claims (927b5ca).
3. Production card delivery isolation and HTML age escaping (fcc2225).
"""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import Mock, patch

from pisobot import archive, bot, config, d1, main, prefs, renter, sweep, telegram, tracker, users, watch
from pisobot.sources import rightmove
from test_archive import advert, at
from test_hardening_round2 import HEAD, Repro
from test_sweep import JudgingBase
from test_users import Base, FRIEND, OWNER, home, message
from test_wp3 import BotBase, publish


class SettingsAndArchiveIntegration(Base):
    def test_profile_and_urgent_commands_preserve_hardened_fingerprint_for_each_person(self):
        friend = self.admit({"rent": 1400})
        owner = users.owner()
        handler = bot.Handler(OWNER, Mock(return_value=True), Mock())
        identities = {p.chat_id: main.settings_key(p) for p in (owner, friend)}
        for person in (owner, friend):
            with users.use(person):
                d1.query("INSERT INTO meta (name, value) VALUES ('watch_initialized', ?)",
                         identities[person.chat_id])
            handler.handle(message("/set urgent on", person.chat_id))
            handler.handle(message(f"/profile name Person {person.chat_id}", person.chat_id))
            self.assertEqual(main.settings_key(users.get(person.chat_id)), identities[person.chat_id])
            with users.use(person):
                self.assertTrue(renter.urgent_on())
                self.assertEqual(renter.profile()["name"], f"Person {person.chat_id}")
                self.assertEqual(d1.query("SELECT value FROM meta WHERE name = 'watch_initialized'")[0]["value"],
                                 identities[person.chat_id])
        self.assertNotEqual(identities[OWNER], identities[FRIEND])

    def test_new_card_metadata_survives_shared_archive_invalidation(self):
        self.admit()
        listing = home(1)
        listing.images = [f"https://example.com/image-{i}.jpg" for i in range(20)]
        listing.agent_phone = "020 7123 4567"
        listing.first_listed_at = dt.datetime.now(dt.timezone.utc).isoformat()
        archive.upsert([listing], "head")
        for chat in (OWNER, FRIEND):
            archive.record_verdicts(chat, [(listing.key, "matched", listing.rent,
                                          listing.monthly_cost, 25, 30)], "policy")
        listing.rent -= 50
        archive.upsert([listing], "head")
        for chat in (OWNER, FRIEND):
            candidates = archive.pending(chat, ["rm"], "policy", 10)
            self.assertEqual(len(candidates), 1)
            recovered = candidates[0]
            self.assertEqual(len(recovered.images), archive.ARCHIVE_IMAGES)
            self.assertEqual(recovered.agent_phone, listing.agent_phone)
            self.assertEqual(recovered.first_listed_at, listing.first_listed_at)
            self.assertEqual(renter.tier(recovered, chat), "top")


class UnseenNotificationSeams(Base):
    def test_saved_pipeline_home_notified_once_when_sweep_goes_first(self):
        listing = home(1)
        archive.upsert([listing], "head")
        tracker.move(publish(listing), "enquired")
        d1.query("INSERT INTO saved (key, info) VALUES (?, ?)", listing.key,
                 json.dumps({"url": listing.url, "address": listing.address, "cost": listing.monthly_cost}))
        with archive._db() as db:
            db.execute("UPDATE adverts SET status = 'unseen' WHERE key = ?", (listing.key,))
            db.execute("INSERT INTO observations (key, at, rent, status) VALUES (?, ?, ?, 'unseen')",
                       (listing.key, at(0), listing.rent))
            db.execute("INSERT INTO unseen_events (sweep_id, key) VALUES (?, ?)", (1, listing.key))
        client = Mock()
        handler = bot.Handler(OWNER, Mock(return_value=True), client)
        with patch("pisobot.telegram.send_text") as notify, patch("pisobot.telegram.throttle"):
            sweep.notify_unseen()
            self.assertEqual(notify.call_count, 1)
            handler.tick()
        self.assertEqual(client.call.call_count, 0)
        self.assertEqual(tracker.get(listing.key)["told_status"], "unseen")
        self.assertEqual(archive.pending_unseen(OWNER), [])

    def test_saved_pipeline_home_notified_once_when_pipeline_goes_first(self):
        """Reverse order: pipeline notifies first via tracker.notify_changes, then sweep runs."""
        listing = home(2)
        archive.upsert([listing], "head")
        tracker.move(publish(listing), "enquired")
        d1.query("INSERT INTO saved (key, info) VALUES (?, ?)", listing.key,
                 json.dumps({"url": listing.url, "address": listing.address, "cost": listing.monthly_cost}))
        with archive._db() as db:
            db.execute("UPDATE adverts SET status = 'unseen' WHERE key = ?", (listing.key,))
            db.execute("INSERT INTO observations (key, at, rent, status) VALUES (?, ?, ?, 'unseen')",
                       (listing.key, at(0), listing.rent))
            db.execute("INSERT INTO unseen_events (sweep_id, key) VALUES (?, ?)", (1, listing.key))
        client = Mock()
        handler = bot.Handler(OWNER, Mock(return_value=True), client)
        # 1. Pipeline tick runs first
        handler.tick()
        self.assertEqual(client.call.call_count, 1)
        self.assertIn("No longer found in the search", client.call.call_args[1]["text"])
        self.assertEqual(tracker.get(listing.key)["told_status"], "unseen")
        # 2. Sweep notify_unseen runs second: shares acknowledgement via tracker.once
        with patch("pisobot.telegram.send_text") as notify:
            sweep.notify_unseen()
            self.assertEqual(notify.call_count, 0)
        # Durable event in archive is acknowledged; no repeat
        self.assertEqual(archive.pending_unseen(OWNER), [])


class PriceChangeSeams(Repro):
    def test_price_change_told_once_whether_watcher_or_pipeline_goes_first(self):
        self.archive_sweep(HEAD)
        self.assertIsNone(self.cycle(HEAD))

        item = home(4, rent=1400)
        archive.upsert([item], "head")
        tracker.move(publish(item), "enquired")
        with users.use(self.owner):
            p = prefs.load()
            p["saved"][item.key] = {"url": item.url, "address": item.address, "cost": item.monthly_cost}
            prefs.save(p)

        # Baseline seen at 1400
        self.cycle(HEAD + [item])

        # Drop price by £50 in archive
        item.rent = 1350
        archive.upsert([item], "head")

        # Order 1: Watcher sends change card first
        client = Mock()
        handler = bot.Handler(OWNER, Mock(return_value=True), client)
        sent_listings = []
        with patch("pisobot.telegram.send_listing", side_effect=lambda l, r: sent_listings.append((l.key, r))):
            self.cycle(HEAD + [item])
        self.assertEqual(sent_listings, [(item.key, "drop")])

        # Pipeline tick runs second: already told by tracker.once
        handler.tick()
        self.assertEqual(client.call.call_count, 0)
        self.assertEqual(tracker.get(item.key)["told_rent"], 1350)


class DeliveryAndCardIsolationSeams(Repro):
    def setUp(self):
        super().setUp()
        self.archive_sweep(HEAD)
        self.assertIsNone(self.cycle(HEAD))

    def test_failed_send_stays_pending_unsent_while_later_cards_deliver_under_allowance(self):
        """Reconciliation of card delivery isolation with shared alert allowance and durable ledger."""
        item1 = advert(10, 1400)
        item2 = advert(11, 1410)

        calls = []
        def flaky_send(listing, reason):
            calls.append(listing.key)
            if listing.key == item1.key:
                raise RuntimeError("Telegram sendPhoto failed (400): Bad Request: can't parse entities")
            # item2 succeeds

        with patch("pisobot.telegram.send_listing", side_effect=flaky_send), \
                patch.dict(os.environ, {"STORAGE": "local"}):
            err = self.cycle(HEAD + [item1, item2])
            self.assertIsInstance(err, RuntimeError)

        # In cycle 1, both cards were attempted under the allowance (head attempt + sweep retry)
        self.assertEqual(calls, [item1.key, item2.key, item1.key])
        seen = self.seen()
        # Failed card is NOT marked seen
        self.assertNotIn(item1.key, seen)
        # Succeeded card IS marked seen
        self.assertIn(item2.key, seen)

        # Failed card remains pending unsent in durable ledger
        with archive._db() as db:
            job1 = archive._job(db, OWNER, item1.key)
            job2 = archive._job(db, OWNER, item2.key)

        self.assertEqual(job1["state"], "pending")
        self.assertTrue(job1["notify"])

        # Succeeded card is acknowledged done in durable ledger
        self.assertEqual(job2["state"], "done")
        self.assertFalse(job2["notify"])

        # Second cycle: Telegram healthy, failed card retries and succeeds without repeating item2
        calls.clear()
        with patch("pisobot.telegram.send_listing", side_effect=lambda l, r: calls.append(l.key)), \
                patch.dict(os.environ, {"STORAGE": "local"}):
            self.cycle(HEAD + [item1, item2])
        self.assertEqual(calls, [item1.key])
        self.assertIn(item1.key, self.seen())

    def test_existing_receipt_tokens_preserved_when_seen_refreshed(self):
        item = advert(20, 1400)
        with patch.dict(os.environ, {"STORAGE": "local"}):
            self.cycle(HEAD + [item])

        seen = main.load_seen()
        token = seen[item.key].get("delivery_intent")
        self.assertIsNotNone(token)

        # Run again without changes; token must not be erased or fabricated anew
        with patch.dict(os.environ, {"STORAGE": "local"}):
            self.cycle(HEAD + [item])
        refreshed = main.load_seen()
        self.assertEqual(refreshed[item.key]["delivery_intent"], token)


class RealProcessExitSeams(BotBase):
    def test_hard_exit_after_pipeline_move_preserves_atomic_followup(self):
        """Hard process exit using os._exit(73) immediately after tracker.move."""
        source = textwrap.dedent("""
            import contextlib
            import json
            import os
            import sys
            from unittest.mock import patch
            import requests
            from pisobot import bot, tracker

            class OfflineClient:
                def call(self, *args, **kwargs):
                    raise AssertionError("No requests before exit")

            move = tracker.move
            def crash(*args, **kwargs):
                move(*args, **kwargs)
                os._exit(73)
            tracker.move = crash

            with patch.object(requests.sessions.Session, "request",
                              side_effect=AssertionError("Live HTTP forbidden")):
                bot.Handler("100", lambda *args: False, OfflineClient()).handle(json.loads(sys.argv[1]))
        """)
        item = home(99)
        tracker.move(publish(item), "enquired")
        update = {"update_id": 9999, "callback_query": {
            "id": "cb1", "from": {"id": 100}, "data": f"p:b:{item.short_id}",
            "message": {"message_id": 1, "chat": {"id": 100}, "date": 0, "text": "card"}
        }}
        res = subprocess.run([sys.executable, "-c", source, json.dumps(update)],
                             cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertEqual(res.returncode, 73)

        # Listing stage was committed
        self.assertEqual(tracker.get(item.key)["stage"], "viewing")
        # Followup was committed in handled outbox
        self.assertEqual(len(tracker.handled(9999)), 1)

        # Fresh handler replays and delivers followup using self.client
        self.client.reset_mock()
        self.client.call.return_value = {"message_id": 999}
        self.handler = bot.Handler(OWNER, self.trigger, self.client)
        self.handler.handle(update)
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("Viewing booked. When is it?", self.texts()[0])
        self.assertEqual(tracker.handled(9999), [])


class ReleaseSeamTests(JudgingBase):
    def test_staged_archive_judging_enforces_furnishing_and_studio_area_before_ai_extraction(self):
        """Preferences dealbreakers reject in staged details pass before any AI extraction call."""
        from pisobot import extract
        client = Mock()
        extract.set_default(extract.Extractor(client=client))
        self.addCleanup(lambda: extract.set_default(None))

        # Staged rental listing
        item = home(50, rent=1300)
        item.description = "Compact studio flat. Studio apartment."
        item.floor_area_m2 = 20

        with patch.object(config, "STUDIO_MIN_AREA", 25):
            # stage="cheap" passes through because studio area requires details page
            self.assertIsNone(main.passes_basic(item, stage="cheap", defer_model=True))
            # stage="details" rejects for studio_area without invoking AI
            self.assertEqual(main.passes_basic(item, stage="details", defer_model=True), "studio_area")
            self.assertEqual(client.call_count, 0)

    def test_card_formatting_reconciles_urgency_portal_age_escaping_and_agent_phone(self):
        """Card formatting preserves WP3 urgency, escaped age with comparison operators, and agent phone."""
        item = home(51)
        item.urgency = "top"
        item.agent_phone = "020 7946 0999"
        item.update_text = "Reduced < 14 days"
        item.update_text_on = dt.date.today().isoformat()
        with patch.object(telegram, "_today", return_value=dt.date.today()):
            card = telegram.format_listing(item)
        self.assertIn("⚡ <b>Top match</b>", card)
        self.assertIn("🕒 Reduced &lt; 14 days", card)
        self.assertNotIn("Reduced < 14 days", card)
        self.assertIn("☎️ 020 7946 0999", card)


if __name__ == "__main__":
    unittest.main()
