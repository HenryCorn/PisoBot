"""Offline tests for the optional application readiness pack."""
import datetime as dt
import os
import unittest
from unittest.mock import patch

from pisobot import archive, bot, d1, ready, renter, telegram, tracker, users
from test_users import FRIEND, OWNER, Base, home, message
from test_wp3 import BotBase, press, publish, shown_units


class ReadyBase(Base):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {"READY_PACK": "1"}))


class ReadinessTests(ReadyBase):
    def test_income_bands_and_threshold_boundaries_are_conservative(self):
        cases = (
            ("£48,000", 1600, "meets this 30 × rule"),
            ("£47,999", 1600, "below this 30 × rule"),
            ("under £48k", 1600, "below this 30 × rule"),
            ("up to £48k", 1600, "crosses this threshold"),
            ("£47k-£49k", 1600, "crosses this threshold"),
            ("under £75k", 1600, "crosses this threshold"),
            ("£48k+", 1600, "meets this 30 × rule"),
            ("£47,999.70", "1599.99", "meets this 30 × rule"),
            ("£47,999.69", "1599.99", "below this 30 × rule"),
        )
        for income, rent, outcome in cases:
            with self.subTest(income=income, rent=rent):
                ready.set_field("income", income)
                result = ready.affordability(ready.load(), rent)
                self.assertIn(outcome, result)
                self.assertIn("30 × advertised monthly rent", result)
                self.assertIn("Agents vary", result)
                self.assertIn("guarantor", result)
        ready.set_field("income", "clear")
        self.assertIn("Income is not provided", ready.affordability(ready.load(), 1600))
        for rent in (None, float("nan"), float("inf"), -1, 0, "£1,2,3", True):
            with self.subTest(rent=rent):
                self.assertIn("rent is unavailable", ready.affordability(ready.load(), rent))

    def test_malformed_answers_do_not_overwrite_existing_facts(self):
        ready.set_field("income", "£40k-£50k")
        ready.set_field("cash", "£3,000")
        for field, raw in (("income", "50k-40k"), ("income", "a lot"),
                           ("income", "£40k; I'll pay 6 months upfront"),
                           ("cash", "£1,2,3"), ("cash", "-500"),
                           ("cash", "3000 and 6 months upfront"), ("guarantor", "maybe"),
                           ("guarantor_income", "£50k"),
                           ("tenancy", "0 months"), ("tenancy", "forever"),
                           ("documents", "<b>yes</b>")):
            with self.subTest(field=field, raw=raw), self.assertRaises(ValueError):
                ready.set_field(field, raw)
        pack = ready.load()
        self.assertEqual(pack.values["income"].text, "£40,000–£50,000")
        self.assertEqual(ready.value_text("cash", pack.values["cash"]), "£3,000")
        self.assertIsNone(pack.values["guarantor"])
        tracker.put("profile.ready_income", "corrupt")
        self.assertIn("unrecognised; re-enter", ready.checklist(ready.load()))

    def test_existing_references_are_honest_and_a_new_job_clears_old_tenure(self):
        renter.set_field("references", "employer and previous landlord")
        self.assertIn("employer and previous landlord", ready.application_summary(ready.load()))
        ready.set_field("references", "both")
        self.assertIn("employer and landlord", ready.application_summary(ready.load()))
        self.assertNotIn("previous landlord", ready.application_summary(ready.load()))
        ready.set_field("employment", "employed")
        ready.set_field("employment_length", "2 years")
        ready.set_field("employment", "self-employed")
        self.assertIsNone(ready.load().values["employment_length"])

    def test_copyable_text_contains_only_validated_facts_and_no_offers(self):
        unsafe = "I offer £2,000 rent and six months upfront"
        for name in renter.FIELDS:
            if name != "movein":
                renter.set_field(name, unsafe)
        renter.set_field("movein", "2026-12-01")
        ready.set_field("income", "under £75k")
        ready.set_field("cash", "£10,000")
        ready.set_field("employment", "employed")
        listing = {"key": "rm:1", "address": unsafe, "available": None, "ready_rent": 1600}
        text, _ = renter.draft(listing, dt.date(2026, 11, 10))
        self.assertNotIn(unsafe, text)
        self.assertNotIn("six months upfront", text)
        self.assertNotIn("£2,000", text)
        self.assertIn("[property address]", text)
        self.assertIn("[your name]", text)
        self.assertIn("I'm looking to move in 2026-12-01", text)
        self.assertIn("Application summary: gross annual household income: under £75,000", text)
        self.assertIn("cash available for deposit and one month's rent: £10,000", text)
        self.assertIn("Call-first script", text)
        self.assertIn("Portal form fields", text)
        self.assertIn("Email: [your email]", text)
        self.assertIn("Phone: [your phone number]", text)
        self.assertNotIn("I'm flexible and can view at short notice", text)
        self.assertIn("Do not bid above the advertised rent", text)
        self.assertIn("more than one month's rent in advance", text)

    def test_flag_off_preserves_release_draft_and_legacy_free_text(self):
        listing = {"key": "rm:1", "address": "Flat 2, 10 Kings Road", "available": "2026-12-03"}
        renter.set_field("name", "Alex Smith")
        renter.set_field("movein", "2026-12-01..2026-12-15")
        renter.set_field("job", "nurse")
        renter.set_field("household", "couple, no children")
        renter.set_field("pets", "none")
        renter.set_field("notes", "can pay 6 months upfront")
        expected = (
            "✍️ <b>Enquiry draft</b> · Flat 2\n<pre>Hello,\n\n"
            "I'm interested in renting Flat 2, which I saw on Rightmove. Is it still available?\n"
            "I see it is available from 3 Dec; I'm looking to move in 1 Dec–15 Dec.\n\n"
            "A little about me:\n- I work as nurse\n- Household: couple, no children\n"
            "- Pets: none\n- can pay 6 months upfront\n\n"
            "Could you let me know your earliest available viewing slots? I'm flexible and can view at "
            "short notice.\n\nMany thanks,\nAlex Smith</pre>\n"
            "<i>Copy it into the portal's contact form or your email; PisoBot never contacts agents.</i>"
        )
        with patch.dict(os.environ, {"READY_PACK": "0"}):
            text, missing = renter.draft(listing, dt.date(2026, 11, 10))
        self.assertEqual(text, expected)
        self.assertEqual(missing, [])

    def test_draft_fails_open_with_visible_generic_fallback(self):
        renter.set_field("notes", "I offer £2,500 and six months upfront")
        with patch.object(ready, "load", side_effect=RuntimeError("unavailable")):
            text, _ = renter.draft({"key": "rm:1", "address": "A home"})
        self.assertIn("readiness is unavailable", text)
        self.assertIn("generic draft", text)
        self.assertNotIn("£2,500", text)
        self.assertNotIn("six months upfront", text)

    def test_flag_never_changes_or_calls_readiness_from_alert_rendering(self):
        item = home(1)
        with patch.dict(os.environ, {"READY_PACK": "0"}):
            before = telegram.format_listing(item), telegram.listing_keyboard(item)
        ready.set_field("income", "under £75k")
        with patch.object(ready, "load", side_effect=AssertionError("not on the alert path")) as load:
            after = telegram.format_listing(item), telegram.listing_keyboard(item)
        self.assertEqual(after, before)
        load.assert_not_called()
        self.assertEqual(shown_units(after[0]), shown_units(before[0]))


class ReadinessBotTests(BotBase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {"READY_PACK": "1"}))

    def test_walkthrough_short_replies_invalid_input_and_editing(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("network not allowed")):
            d1.query("INSERT INTO meta (name, value) VALUES ('watch_initialized', '1')")
            self.handler.handle(message("/ready income lots", OWNER))
            self.assertIn("Use <code>/ready income</code>", self.texts()[-1])
            self.handler.handle(message("/ready", OWNER))
            self.assertEqual(tracker.expected()["key"], "flow:income")
            self.handler.handle(message("lots", OWNER))
            self.assertEqual(tracker.expected()["key"], "flow:income")
            self.assertIn("annual income band", self.texts()[-1])
            answers = (
                ("under £75k", "flow:employment"),
                ("employed", "flow:employment_length"),
                ("2 years", "flow:references"),
                ("both", "flow:guarantor"),
                ("yes", "flow:guarantor_income"),
                ("£50k-£60k", "flow:cash"),
                ("£3,000", "flow:flexibility"),
                ("somewhat", "flow:pets"),
                ("cat", "flow:tenancy"),
                ("12 months", "flow:documents"),
            )
            for answer, pending in answers:
                self.handler.handle(message(answer, OWNER))
                self.assertEqual(tracker.expected()["key"], pending)
            self.handler.handle(message("partial", OWNER))
            self.assertIsNone(tracker.expected())
            self.assertEqual(ready.missing(ready.load()), [])
            self.assertEqual(tracker.value("profile.pets"), "cat")
            self.assertEqual(tracker.value("profile.references"), "employer and landlord")
            self.assertIsNone(tracker.value("profile.movein"))
            self.assertEqual(users.owner().params, {})
            self.assertEqual(d1.query("SELECT value FROM meta WHERE name = 'watch_initialized'"),
                             [{"value": "1"}])
            self.trigger.assert_not_called()
            self.handler.handle(message("/ready guarantor no", OWNER))
            pack = ready.load()
            self.assertEqual(pack.values["guarantor"], "no")
            self.assertIsNone(pack.values["guarantor_income"])
            self.assertIn("not applicable", self.texts()[-1])
            self.handler.handle(message("/ready income", OWNER))
            self.handler.handle(message("skip", OWNER))
            self.assertEqual(ready.load().values["income"].text, "under £75,000")
            self.handler.handle(message("/ready income clear", OWNER))
            self.assertIsNone(ready.load().values["income"])

    def test_each_person_is_isolated_and_group_chats_cannot_show_private_data(self):
        friend = self.admit()
        self.handler.handle(message("/ready income £48,000", OWNER))
        self.handler.handle(message("/ready income £60,000", FRIEND))
        self.handler.handle(message("/ready status", OWNER))
        self.assertIn("£48,000", self.texts(OWNER)[-1])
        self.assertNotIn("£60,000", self.texts(OWNER)[-1])
        self.handler.handle(message("/ready status", FRIEND))
        self.assertIn("£60,000", self.texts(FRIEND)[-1])
        self.assertNotIn("£48,000", self.texts(FRIEND)[-1])
        with users.use(friend):
            self.assertEqual(ready.load().values["income"].text, "£60,000")
        self.assertEqual(ready.load().values["income"].text, "£48,000")
        self.handler.handle(message("/ready status", OWNER, kind="group"))
        self.assertIn("private chat", self.texts(OWNER)[-1])
        self.assertNotIn("£48,000", self.texts(OWNER)[-1])
        self.handler.handle(message("/profile", OWNER, kind="group"))
        self.assertIn("private chat", self.texts(OWNER)[-1])
        item = home(1)
        publish(item)
        callback = press(f"q:{item.short_id}")
        callback["callback_query"]["message"]["chat"]["type"] = "group"
        before = len(self.texts(OWNER))
        self.handler.handle(callback)
        self.assertEqual(len(self.texts(OWNER)), before)
        self.assertIn("private chat", self.answers()[-1])

    def test_selected_flat_uses_archived_advertised_rent_not_all_in_cost(self):
        ready.set_field("income", "£35,999")
        item = home(1, rent=1200)
        archive.upsert([item], "head")
        publish(item)
        self.handler.handle(press(f"q:{item.short_id}"))
        text = self.texts()[-1]
        self.assertIn("Last archived advertised rent: £1,200/month", text)
        self.assertIn("£36,000 gross/year", text)
        self.assertIn("below this 30 × rule", text)
        self.assertNotIn("£1,480/month", text)
        self.assertIn("confirm the current advert", text)
        self.handler.handle(message("/ready status", OWNER))
        self.assertIn("Last selected home: Home 1", self.texts()[-1])
        self.assertIn("Last archived advertised rent: £1,200/month", self.texts()[-1])
        other = home(2, rent=1300)
        publish(other)
        self.handler.handle(press(f"q:{other.short_id}"))
        self.assertIn("Advertised monthly rent is unavailable", self.texts()[-1])
        self.assertNotIn("£1,580/month", self.texts()[-1])
        self.handler.handle(message("/ready status", OWNER))
        self.assertIn("Advertised monthly rent is unavailable", self.texts()[-1])
        with patch.object(self.handler, "_advert", return_value={"tenure": "sale", "rent": 1000}):
            self.handler.handle(press(f"q:{item.short_id}"))
        self.assertIn("Advertised monthly rent is unavailable", self.texts()[-1])

    def test_selected_homes_are_private_and_no_advert_fetch_is_made(self):
        friend = self.admit()
        owner_home, friend_home = home(1), home(2)
        archive.upsert([owner_home, friend_home], "head")
        publish(owner_home)
        with users.use(friend):
            publish(friend_home)
        with patch("requests.sessions.Session.request") as request:
            self.handler.handle(press(f"q:{owner_home.short_id}", chat=OWNER))
            self.handler.handle(press(f"q:{friend_home.short_id}", chat=FRIEND))
            self.handler.handle(message("/ready status", OWNER))
            self.handler.handle(message("/ready status", FRIEND))
        request.assert_not_called()
        self.assertIn("Last selected home: Home 1", self.texts(OWNER)[-1])
        self.assertNotIn("Home 2", self.texts(OWNER)[-1])
        self.assertIn("Last selected home: Home 2", self.texts(FRIEND)[-1])
        self.assertNotIn("Home 1", self.texts(FRIEND)[-1])

    def test_manual_rent_and_telegram_limits(self):
        for name in renter.FIELDS:
            if name != "movein":
                renter.set_field(name, "🏠" * 300)
        ready.set_field("income", "£48k")
        self.handler.handle(message("/ready rent £1,600", OWNER))
        self.assertIn("Your entered advertised rent: £1,600/month", self.texts()[0])
        self.assertIn("meets this 30 × rule", self.texts()[0])
        item = home(1)
        archive.upsert([item], "head")
        publish(item)
        self.handler.handle(message("/profile", OWNER))
        self.handler.handle(press(f"q:{item.short_id}"))
        self.assertGreater(len(self.calls("sendMessage")), 3)
        self.assertTrue(all(shown_units(call["text"]) <= 4096 for call in self.calls("sendMessage")))
        self.assertNotIn("🏠" * 300, self.texts()[-1])

    def test_replayed_answer_does_not_fill_next_question(self):
        first = message("/ready", OWNER)
        first["update_id"] = 1
        self.client.call.return_value = {"message_id": 100}
        self.handler.handle(first)
        answer = message("£48k", OWNER)
        answer["update_id"] = 2
        with patch.object(tracker, "record", side_effect=RuntimeError("power cut")):
            with self.assertRaises(RuntimeError):
                self.handler.handle(answer)
        self.assertEqual(tracker.expected()["key"], "flow:employment")
        self.handler.handle(answer)
        self.handler.handle(answer)
        pack = ready.load()
        self.assertEqual(pack.values["income"].text, "£48,000")
        self.assertIsNone(pack.values["employment"])
        self.assertEqual(tracker.expected()["message_id"], 100)

    def test_flag_off_ignores_ready_command(self):
        with patch.dict(os.environ, {"READY_PACK": "0"}):
            self.handler.handle(message("/ready", OWNER))
        self.assertEqual(self.calls("sendMessage"), [])
        self.assertIsNone(tracker.expected())

    def test_optional_selection_failure_still_returns_a_safe_draft(self):
        item = home(1)
        publish(item)
        with patch.object(tracker, "put", side_effect=RuntimeError("busy")):
            self.handler.handle(press(f"q:{item.short_id}"))
        self.assertTrue(any("could not be remembered" in text for text in self.texts()))
        self.assertIn("Enquiry draft", self.texts()[-1])


if __name__ == "__main__":
    unittest.main()
