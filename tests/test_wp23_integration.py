"""Offline boundary checks for the WP2-hardening + WP3 integration branch.

These complement, not replace, independent review of either feature. All state is temporary;
judging fixtures mock portal, TfL and Telegram operations.
"""
import datetime as dt
import json
from unittest.mock import Mock, patch

from pisobot import archive, bot, d1, main, renter, sweep, tracker, users
from test_archive import advert
from test_sweep import JudgingBase
from test_users import Base, FRIEND, OWNER, home, message
from test_wp3 import publish


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

    def test_saved_pipeline_home_is_not_notified_twice_when_sweep_notifies_first(self):
        listing = home(1)
        archive.upsert([listing], "head")
        tracker.move(publish(listing), "enquired")
        d1.query("INSERT INTO saved (key, info) VALUES (?, ?)", listing.key,
                 json.dumps({"url": listing.url, "address": listing.address, "cost": listing.monthly_cost}))
        with archive._db() as db:
            db.execute("UPDATE adverts SET status = 'unseen' WHERE key = ?", (listing.key,))
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


class DeliveryIntegration(JudgingBase):
    def test_recent_listing_keeps_urgency_when_durable_failed_delivery_is_retried(self):
        listing = advert(1)
        listing.first_listed_at = dt.datetime.now(dt.timezone.utc).isoformat()
        self.archive_sweep([listing])
        with users.use(self.owner):
            sweep._put_meta("sweep_base:rm", main.settings_key(self.owner))
        attempts = []

        def fail_first(candidate, reason):
            attempts.append((candidate.key, reason, candidate.urgency))
            if len(attempts) == 1:
                raise RuntimeError("Telegram unavailable")

        with patch("pisobot.telegram.send_listing", side_effect=fail_first):
            with self.assertRaises(RuntimeError):
                self.judge()
            self.assertEqual(archive.verdicts_since(OWNER, 1)[0]["outcome"], "unsent")
            self.judge()
            self.judge()
        self.assertEqual(attempts, [(listing.key, "new", "top"), (listing.key, "new", "top")])
        self.assertEqual(archive.pending(OWNER, ["rm"], main.settings_key(self.owner), 10), [])
        self.assertEqual(self.seen(), {listing.key})
