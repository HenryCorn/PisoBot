"""Offline tests for the optional application readiness pack."""
import datetime as dt
import os
import sqlite3
import threading
import unittest
from unittest.mock import Mock, patch

from pisobot import archive, bot, d1, localdb, main, ready, readydb, renter, telegram, tracker, users, watch
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
        readydb.put("profile.ready_income", "corrupt")
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

    def test_shared_fields_follow_later_profile_edits_and_clears(self):
        renter.set_field("pets", "cat")
        ready.set_field("pets", "dog")
        self.assertIn("Pets: dog", renter.show_profile())
        renter.set_field("pets", "cat")
        self.assertEqual(ready.load().values["pets"], "cat")
        ready.set_field("pets", "clear")
        self.assertIsNone(ready.load().values["pets"])
        with patch.dict(os.environ, {"READY_PACK": "0"}):
            renter.set_field("pets", "dog")
        self.assertEqual(ready.load().values["pets"], "dog")
        with patch.dict(os.environ, {"READY_PACK": "0"}):
            renter.set_field("pets", "cat")
        self.assertEqual(ready.load().values["pets"], "cat")

    def test_cleared_or_unassociated_employment_never_reuses_tenure(self):
        ready.set_field("employment", "employed")
        ready.set_field("employment_length", "2 years")
        ready.set_field("employment", "clear")
        self.assertIsNone(ready.load().values["employment_length"])
        with self.assertRaisesRegex(ValueError, "employment"):
            ready.set_field("employment_length", "2 years")
        ready.set_field("employment", "unemployed")
        self.assertNotIn("24 months", ready.application_summary(ready.load()))

        # A legacy tenure has no evidence tying it to the currently saved employment.
        readydb.put("profile.ready_employment_length", "24 months")
        pack = ready.load()
        self.assertIsNone(pack.values["employment_length"])
        self.assertIn("re-enter", ready.checklist(pack))
        self.assertNotIn("24 months", ready.application_summary(pack))
        renter.set_field("job", "nurse")
        self.assertIsNone(ready.load().values["employment_length"])
        ready.set_field("employment_length", "6 months")
        ready.set_field("employment", "unemployed")
        self.assertIn("time unemployed: 6 months", ready.application_summary(ready.load()))
        ready.set_field("employment", "contractor")
        self.assertIsNone(ready.load().values["employment_length"])

    def test_tenure_is_reconfirmed_for_the_current_job_and_status(self):
        renter.set_field("job", "nurse")
        ready.set_field("employment", "employed")
        ready.set_field("employment_length", "2 years")
        self.assertIn("time in employment: 24 months", ready.application_summary(ready.load()))
        renter.set_field("job", "nurse")
        self.assertIsNone(ready.load().values["employment_length"])
        ready.set_field("employment_length", "2 years")

        renter.set_field("job", "teacher")
        pack = ready.load()
        self.assertIsNone(pack.values["employment_length"])
        self.assertIn("re-enter", ready.checklist(pack))
        self.assertNotIn("24 months", ready.application_summary(pack))

        ready.set_field("employment", "unemployed")
        ready.set_field("employment_length", "6 months")
        summary = ready.application_summary(ready.load())
        self.assertIn("time unemployed: 6 months", summary)
        self.assertNotIn("time in employment: 6 months", summary)
        for status, label in (("student", "time as a student"), ("retired", "time retired"),
                              ("other", "time in current status")):
            with self.subTest(status=status):
                ready.set_field("employment", status)
                ready.set_field("employment_length", "6 months")
                self.assertIn(f"{label}: 6 months", ready.application_summary(ready.load()))

    def test_tenure_is_not_reused_if_the_job_changes_while_disabled(self):
        renter.set_field("job", "nurse")
        ready.set_field("employment", "employed")
        ready.set_field("employment_length", "2 years")
        with patch.dict(os.environ, {"READY_PACK": "0"}):
            renter.set_field("job", "teacher")
        self.assertIsNone(ready.load().values["employment_length"])
        with patch.dict(os.environ, {"READY_PACK": "0"}):
            renter.set_field("job", "nurse")
        self.assertIsNone(ready.load().values["employment_length"])

    def test_tenure_is_invalidated_before_a_job_save_can_crash(self):
        renter.set_field("job", "nurse")
        ready.set_field("employment", "employed")
        ready.set_field("employment_length", "2 years")
        put = tracker.put

        def save_then_crash(name, value):
            put(name, value)
            raise RuntimeError("power cut")

        with patch.object(tracker, "put", side_effect=save_then_crash):
            for job in ("teacher", "nurse"):
                with self.assertRaisesRegex(RuntimeError, "power cut"):
                    renter.set_field("job", job)
        self.assertIsNone(ready.load().values["employment_length"])

    def test_draft_keeps_validated_unit_smoker_and_archived_availability(self):
        renter.set_field("smoker", "no")
        listing = {"key": "rm:1", "address": "10 Kings Road, Flat 2, London",
                   "available": "2026-12-03"}
        text, _ = renter.draft(listing, dt.date(2026, 11, 10))
        self.assertIn("10 Kings Road, Flat 2, London", text)
        self.assertIn("Smoker: no", text)
        self.assertIn("2026-12-03", text)
        self.assertIn("confirm", text.lower())

        listing["address"] = "10 Kings Road, mysterious rear annex, London"
        listing["available"] = "2026-12-03; I will pay extra"
        renter.set_field("smoker", "I offer extra rent")
        text, _ = renter.draft(listing, dt.date(2026, 11, 10))
        self.assertIn("[property address]", text)
        self.assertNotIn("mysterious rear annex", text)
        self.assertNotIn("2026-12-03", text)
        self.assertNotIn("offer extra rent", text)

    def test_approximate_and_legacy_movein_windows_require_confirmation(self):
        today = dt.date(2026, 11, 11)
        listing = {"key": "rm:1", "address": "10 Kings Road", "ready_rent": 1600}
        for answer in ("asap", "anytime"):
            with self.subTest(answer=answer):
                renter.set_field("movein", answer, today)
                pack = ready.load()
                self.assertIsNone(pack.movein)
                self.assertIn("confirm", ready.checklist(pack).lower())
                text, _ = renter.draft(listing, today)
                self.assertIn("[confirm move-in date]", text)
                self.assertNotIn("2026-11-11 to 2026-12-11", text)
                self.assertNotIn("11 Nov–11 Dec", renter.show_profile())
                self.assertEqual(renter.fit("2026-11-20", today)[0], "✅")

        # A legacy 30-day range could have originated from "asap" or "anytime".
        tracker.put("profile.movein", "2026-11-11..2026-12-11")
        self.assertIsNone(ready.load().movein)
        renter.set_field("movein", "2026-12-01..2026-12-31", today)
        self.assertIn("[confirm move-in date]", renter.draft(listing, today)[0])
        renter.set_field("movein", "2026-12-01..2026-12-15", today)
        text, _ = renter.draft(listing, today)
        self.assertIn("I'm looking to move in 2026-12-01 to 2026-12-15", text)

    def test_safe_profile_and_property_details_are_reused_without_free_text(self):
        renter.set_field("name", "Alex Smith")
        renter.set_field("job", "nurse")
        renter.set_field("household", "couple, no children")
        renter.set_field("notes", "I offer six months upfront")
        renter.set_field("smoker", "I can pay extra rent")
        listing = {"key": "rm:1", "address": "Flat 2, 10 Kings Road, London", "ready_rent": 1600}
        text, missing = renter.draft(listing)
        self.assertIn("renting Flat 2, 10 Kings Road", text)
        self.assertIn("Property: Flat 2, 10 Kings Road", text)
        self.assertIn("Name: Alex Smith", text)
        self.assertIn("Occupation: nurse", text)
        self.assertIn("Household: couple, no children", text)
        self.assertIn("Many thanks,\nAlex Smith", text)
        self.assertNotIn("six months upfront", text)
        self.assertNotIn("pay extra rent", text)
        self.assertIn("review and edit", text.lower())
        self.assertNotIn("name", missing)
        self.assertNotIn("occupation", missing)
        self.assertNotIn("household", missing)

        renter.set_field("name", "Zoë O’Connor")
        renter.set_field("job", "nurse at St Thomas', full time")
        listing["address"] = "Flat 2A, 10 King's Road, London, SW1A 1AA"
        text, _ = renter.draft(listing)
        self.assertIn("Name: Zoë O’Connor", text)
        self.assertIn("Occupation: nurse, full time", text)
        self.assertNotIn("St Thomas", text)
        self.assertIn("Property: Flat 2A, 10 King's Road, London, SW1A 1AA", text)

        renter.set_field("name", "offer more rent")
        renter.set_field("job", "nurse and I offer six months upfront")
        renter.set_field("household", "couple willing to pay extra")
        text, _ = renter.draft({"key": "rm:1", "address": "10 Pay More Road, London"})
        self.assertNotIn("offer more rent", text)
        self.assertNotIn("six months upfront", text)
        self.assertNotIn("couple willing to pay extra", text)
        self.assertIn("[property address]", text)
        self.assertIn("[your name]", text)
        self.assertIn("[who will live there]", text)

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
            self.assertEqual(readydb.expected()["key"], "flow:income")
            self.handler.handle(message("lots", OWNER))
            self.assertEqual(readydb.expected()["key"], "flow:income")
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
                self.assertEqual(readydb.expected()["key"], pending)
            self.handler.handle(message("partial", OWNER))
            self.assertIsNone(readydb.expected())
            self.assertEqual(ready.missing(ready.load()), [])
            self.assertEqual(readydb.profile(renter.FIELDS)["pets"], "cat")
            self.assertEqual(readydb.profile(renter.FIELDS)["references"], "employer and landlord")
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

    def finish_ready_flow(self):
        for _ in range(len(ready.FIELDS) + 1):
            pending = readydb.expected()
            if pending is None:
                return
            self.assertEqual(pending["kind"], "ready")
            self.handler.handle(message("skip", OWNER))
        self.fail("readiness flow did not finish")

    def test_manual_rent_survives_answers_and_replay_but_not_the_next_flow(self):
        item = home(1, rent=1200)
        archive.upsert([item], "head")
        publish(item)
        self.handler.handle(press(f"q:{item.short_id}"))
        start = message("/ready rent 1600", OWNER)
        start["update_id"] = 101
        self.handler.handle(start)
        answer = message("£48k", OWNER)
        answer["update_id"] = 102
        with patch.object(readydb, "record", side_effect=SystemExit("power cut")):
            with self.assertRaises(SystemExit):
                self.handler.handle(answer)
        self.handler.handle(answer)
        self.handler.handle(answer)
        self.finish_ready_flow()
        result = self.texts()[-1]
        self.assertIn("Your entered advertised rent: £1,600/month", result)
        self.assertIn("meets this 30 × rule", result)
        self.assertNotIn("£1,200/month", result)
        self.handler.handle(message("/ready status", OWNER))
        self.assertIn("Last archived advertised rent: £1,200/month", self.texts()[-1])
        self.assertNotIn("Your entered advertised rent", self.texts()[-1])

    def test_selecting_another_home_invalidates_the_manual_rent_flow(self):
        first, second = home(1, rent=1200), home(2, rent=1300)
        archive.upsert([first, second], "head")
        publish(first)
        publish(second)
        self.handler.handle(press(f"q:{first.short_id}"))
        self.handler.handle(message("/ready rent 1600", OWNER))
        self.handler.handle(message("£48k", OWNER))
        self.handler.handle(press(f"q:{second.short_id}"))
        self.finish_ready_flow()
        result = self.texts()[-1]
        self.assertIn("home changed", result.lower())
        self.assertIn("/ready rent", result)
        self.assertIn("Last selected home: Home 2", result)
        self.assertIn("Last archived advertised rent: £1,300/month", result)
        self.assertNotIn("£1,600/month", result)

    def test_manual_rent_remains_private_and_follows_single_field_edits(self):
        self.admit()
        self.handler.handle(message("/ready rent 1600", OWNER))
        self.handler.handle(message("/ready status", FRIEND))
        self.assertNotIn("£1,600/month", self.texts(FRIEND)[-1])
        self.handler.handle(message("/ready income £48k", OWNER))
        self.assertIn("Your entered advertised rent: £1,600/month", self.texts()[-1])
        self.handler.handle(message("/ready status", OWNER))
        self.assertNotIn("Your entered advertised rent", self.texts()[-1])

        self.handler.handle(message("/ready rent 1400", OWNER))
        self.handler.handle(message("/ready employment", OWNER))
        self.handler.handle(message("employed", OWNER))
        self.assertIn("Your entered advertised rent: £1,400/month", self.texts()[-1])
        self.assertNotIn("£1,600/month", self.texts()[-1])

    def test_skipping_employment_skips_the_dependent_tenure_question(self):
        self.handler.handle(message("/ready", OWNER))
        self.handler.handle(message("skip", OWNER))
        self.assertEqual(readydb.expected()["key"], "flow:employment")
        self.handler.handle(message("skip", OWNER))
        self.assertEqual(readydb.expected()["key"], "flow:references")

    def test_movein_confirmation_does_not_assert_an_approximate_deadline(self):
        with patch.object(renter, "_today", return_value=dt.date(2026, 11, 11)):
            self.handler.handle(message("/profile movein asap", OWNER))
        result = self.texts()[-1]
        self.assertIn("confirm", result.lower())
        self.assertNotIn("11 Nov–11 Dec", result)

    def test_replayed_answer_does_not_fill_next_question(self):
        first = message("/ready", OWNER)
        first["update_id"] = 1
        self.client.call.return_value = {"message_id": 100}
        self.handler.handle(first)
        answer = message("£48k", OWNER)
        answer["update_id"] = 2
        with patch.object(readydb, "record", side_effect=SystemExit("power cut")):
            with self.assertRaises(SystemExit):
                self.handler.handle(answer)
        self.assertEqual(readydb.expected()["key"], "flow:income")
        self.handler.handle(answer)
        self.handler.handle(answer)
        pack = ready.load()
        self.assertEqual(pack.values["income"].text, "£48,000")
        self.assertIsNone(pack.values["employment"])
        self.assertEqual(readydb.expected()["message_id"], 100)

    def test_failed_final_outbox_replays_manual_rent_for_flow_and_single_edit(self):
        for mode in ("flow", "prompted edit", "direct edit"):
            with self.subTest(mode=mode):
                self.handler.handle(message("/ready rent 1600", OWNER))
                if mode == "prompted edit":
                    self.handler.handle(message("/ready income", OWNER))
                elif mode == "flow":
                    while readydb.expected()["key"].split(":")[1] != "documents":
                        self.handler.handle(message("skip", OWNER))
                answer = message("/ready income £48k" if mode == "direct edit" else "skip", OWNER)
                answer["update_id"] = {"flow": 901, "prompted edit": 902, "direct edit": 903}[mode]
                before = len(self.texts())
                with patch.object(readydb, "record", side_effect=SystemExit("power cut")):
                    with self.assertRaises(SystemExit):
                        self.handler.handle(answer)
                self.handler.handle(answer)
                self.handler.handle(answer)
                replies = self.texts()[before:]
                self.assertEqual(len(replies), 1)
                self.assertIn("Your entered advertised rent: £1,600/month", replies[0])

    def test_flag_off_ignores_ready_command(self):
        with patch.dict(os.environ, {"READY_PACK": "0"}):
            self.handler.handle(message("/ready", OWNER))
        self.assertEqual(self.calls("sendMessage"), [])
        self.assertIsNone(tracker.expected())

    def test_completed_release_enquiry_is_not_replayed_after_enabling_readiness(self):
        item = home(1)
        publish(item)
        enquiry = press(f"q:{item.short_id}")
        enquiry["update_id"] = 1004
        with patch.dict(os.environ, {"READY_PACK": "0"}):
            self.handler.handle(enquiry)
        before = self.texts()
        self.assertEqual(len(before), 1)
        with patch.object(readydb, "transaction", side_effect=AssertionError("already handled")):
            self.handler.handle(enquiry)
        self.assertEqual(self.texts(), before)

    def test_pending_manual_rent_survives_an_off_interval_without_replay(self):
        self.handler.handle({**message("/ready rent 1600", OWNER), "update_id": 1006})
        with patch.dict(os.environ, {"READY_PACK": "0"}):
            self.handler.handle({**message("/help", OWNER), "update_id": 1007})
        self.handler.handle({**message("/ready income £48k", OWNER), "update_id": 1008})
        self.assertIn("Your entered advertised rent: £1,600/month", self.texts()[-1])

    def test_pending_release_enquiry_is_replaced_with_a_safe_readiness_draft(self):
        item = home(1)
        publish(item)
        enquiry = press(f"q:{item.short_id}")
        enquiry["update_id"] = 1005
        tracker.record(1005, [{"method": "sendMessage", "body": {
            "chat_id": OWNER, "text": "I offer six months upfront"}}])
        self.handler.handle(enquiry)
        self.assertIn("Enquiry draft", self.texts()[-1])
        self.assertNotIn("six months upfront", self.texts()[-1])

    def test_optional_selection_failure_still_returns_a_safe_draft(self):
        item = home(1)
        publish(item)
        with patch.object(readydb, "put", side_effect=RuntimeError("busy")):
            self.handler.handle(press(f"q:{item.short_id}"))
        self.assertTrue(any("could not be remembered" in text for text in self.texts()))
        self.assertIn("Enquiry draft", self.texts()[-1])

    def test_readiness_updates_do_not_use_the_alert_database_for_state_or_outbox(self):
        item = home(1)
        archive.upsert([item], "head")
        publish(item)
        start = message("/ready income", OWNER)
        start["update_id"] = 1001
        answer = message("£48k", OWNER)
        answer["update_id"] = 1002
        enquiry = press(f"q:{item.short_id}")
        enquiry["update_id"] = 1003
        with patch.object(tracker, "expect", side_effect=AssertionError("primary prompt")), \
                patch.object(tracker, "done", side_effect=AssertionError("primary marker")), \
                patch.object(tracker, "record", side_effect=AssertionError("primary journal")), \
                patch.object(tracker, "bind", side_effect=AssertionError("primary binding")), \
                patch.object(tracker, "put", side_effect=AssertionError("primary readiness field")), \
                patch.object(d1, "query", side_effect=AssertionError("primary listing query")), \
                patch.object(archive, "advert", side_effect=AssertionError("primary archive connection")):
            self.handler.handle(start)
            self.handler.handle(answer)
            self.handler.handle(enquiry)
        self.assertIn("£48,000", self.texts()[-1])
        self.assertNotEqual(readydb.path(), localdb.database_path())
        self.assertEqual(tracker.query("SELECT name FROM renter WHERE name LIKE 'profile.ready_%'"), [])

    def test_unavailable_readiness_store_still_sends_a_generic_enquiry(self):
        item = home(1)
        publish(item)
        with patch.object(readydb, "transaction", side_effect=sqlite3.OperationalError("busy")):
            self.handler.handle(press(f"q:{item.short_id}"))
            self.handler.handle(message("/ready status", OWNER))
        self.assertTrue(any("could not be remembered" in text for text in self.texts()))
        self.assertTrue(any("generic draft" in text for text in self.texts()))
        self.assertIn("readiness is unavailable", self.texts()[-1].lower())

    def test_unavailable_store_does_not_send_enquiry_to_a_group(self):
        item = home(1)
        publish(item)
        enquiry = {**press(f"q:{item.short_id}"), "update_id": 1100}
        enquiry["callback_query"]["message"]["chat"]["type"] = "group"
        with patch.object(readydb, "transaction", side_effect=sqlite3.OperationalError("busy")):
            self.handler.handle(enquiry)
        self.assertIn("private chat", self.answers()[-1].lower())
        self.assertFalse(any("enquiry draft" in text.lower() for text in self.texts()))

    def test_existing_readiness_answers_and_pending_prompt_migrate_privately(self):
        tracker.put("profile.ready_income", "£48,000")
        tracker.put("profile.ready_employment", "employed")
        tracker.put("profile.ready_employment_length", "24 months")
        tracker.put("profile.ready_employment_length_for", "employed")
        tracker.expect("ready", "single:cash")
        self.handler.handle(message("£3,000", OWNER))
        pack = ready.load()
        self.assertEqual(pack.values["income"].text, "£48,000")
        self.assertEqual(ready.value_text("cash", pack.values["cash"]), "£3,000")
        self.assertIsNone(pack.values["employment_length"])
        self.assertIn("re-enter", ready.checklist(pack))
        self.assertEqual(readydb.path().stat().st_mode & 0o777, 0o600)

    def test_clearing_warns_when_an_original_database_copy_remains(self):
        tracker.put("profile.ready_income", "£48,000")
        tracker.put("profile.pets", "cat")
        for field in ("income", "pets"):
            with self.subTest(field=field):
                before = len(self.texts())
                with patch.object(tracker, "drop", side_effect=AssertionError("primary deletion")):
                    self.handler.handle(message(f"/ready {field} clear", OWNER))
                self.assertTrue(any("original database" in text.lower() and "not fully delete" in text.lower()
                                    for text in self.texts()[before:]))
        self.assertIsNone(ready.load().values["income"])
        self.assertIsNone(ready.load().values["pets"])

        ready.set_field("cash", "£500")
        before = len(self.texts())
        self.handler.handle(message("/ready cash clear", OWNER))
        self.assertFalse(any("original database" in text.lower() for text in self.texts()[before:]))

    def test_implicitly_cleared_legacy_dependents_also_warn(self):
        tracker.put("profile.ready_guarantor_income", "£50,000")
        tracker.put("profile.ready_employment_length", "24 months")
        for command in ("/ready guarantor no", "/ready employment unemployed"):
            with self.subTest(command=command):
                before = len(self.texts())
                self.handler.handle(message(command, OWNER))
                self.assertTrue(any("original database" in text.lower() and "not fully delete" in text.lower()
                                    for text in self.texts()[before:]))

    def test_legacy_final_outbox_replays_without_writing_the_alert_database(self):
        tracker.done(update=1201)
        tracker.record(1201, [{"method": "sendMessage", "body": {
            "chat_id": OWNER, "text": "<b>📋 Application readiness</b>\n"
                                      "Your entered advertised rent: £1,600/month"}}])
        answer = {**message("skip", OWNER), "update_id": 1201}
        with patch.object(tracker, "record", side_effect=AssertionError("primary journal")), \
                patch.object(tracker, "done", side_effect=AssertionError("primary marker")):
            self.handler.handle(answer)
            self.handler.handle(answer)
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("Your entered advertised rent: £1,600/month", self.texts()[0])

    def test_unjournaled_legacy_completion_does_not_write_the_alert_database(self):
        tracker.done(update=1202)
        answer = {**message("skip", OWNER), "update_id": 1202}
        with patch.object(tracker, "record", side_effect=AssertionError("primary journal")), \
                patch.object(tracker, "done", side_effect=AssertionError("primary marker")):
            self.handler.handle(answer)
            self.handler.handle(answer)
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("previous result could not be recovered", self.texts()[0].lower())

    def test_completed_primary_replay_needs_no_tracker_or_sidecar_write(self):
        tracker.done(update=1207)
        tracker.record(1207, [])
        answer = {**message("skip", OWNER), "update_id": 1207}
        with patch.object(tracker, "_connect", side_effect=AssertionError("primary tracker")), \
                patch.object(readydb, "transaction", side_effect=AssertionError("sidecar transaction")):
            self.handler.handle(answer)
        self.assertEqual(self.texts(), [])

    def test_legacy_readiness_error_outboxes_replay_without_primary_writes(self):
        errors = ("⚠️ That readiness question is no longer available; use /ready.",
                  "⚠️ The one-off rent could not be recovered; use /ready rent 1600 again.",
                  "⚠️ Application readiness is unavailable. Check /ready and try again.",
                  "⚠️ Could not finish that readiness answer. Check /ready before retrying.")
        for update_id, text in enumerate(errors, 1203):
            with self.subTest(text=text):
                tracker.done(update=update_id)
                tracker.record(update_id, [{"method": "sendMessage", "body": {"chat_id": OWNER, "text": text}}])
                answer = {**message("skip", OWNER), "update_id": update_id}
                before = len(self.texts())
                with patch.object(tracker, "record", side_effect=AssertionError("primary journal")):
                    self.handler.handle(answer)
                    self.handler.handle(answer)
                self.assertEqual(self.texts()[before:], [text])

    def test_answered_migrated_prompt_does_not_warn_on_later_chatter(self):
        tracker.expect("ready", "single:cash", update=1210, nonce="legacy")
        self.handler.handle({**message("£3,000", OWNER), "update_id": 1211})
        before = self.texts()
        with patch.object(tracker, "record", side_effect=AssertionError("primary journal")):
            self.handler.handle({**message("hello", OWNER), "update_id": 1212})
        self.assertEqual(self.texts(), before)
        self.assertEqual(ready.value_text("cash", ready.load().values["cash"]), "£3,000")
        with patch.object(tracker, "record", side_effect=AssertionError("primary journal")):
            self.handler.handle({**message("/cancel", OWNER), "update_id": 1216})
        self.assertEqual(self.texts()[-1], "Nothing to cancel.")

    def test_expired_migrated_prompt_does_not_trigger_primary_cleanup(self):
        now = tracker.now()
        with patch.object(tracker, "now", return_value=now):
            tracker.expect("ready", "single:cash", update=1217, nonce="legacy")
            self.handler.handle({**message("£3,000", OWNER), "update_id": 1218})
        before = self.texts()
        later = now + dt.timedelta(minutes=tracker.AWAIT_MINUTES + 1)
        with patch.object(tracker, "now", return_value=later), \
                patch.object(tracker, "record", side_effect=AssertionError("primary journal")), \
                patch.object(tracker, "drop", side_effect=AssertionError("primary cleanup")):
            self.handler.handle({**message("hello", OWNER), "update_id": 1219})
            self.handler.handle({**message("/cancel", OWNER), "update_id": 1223})
        self.assertEqual(self.texts()[:-1], before)
        self.assertEqual(self.texts()[-1], "Nothing to cancel.")

    def test_prompt_answered_before_upgrade_is_logically_retired(self):
        tracker.expect("ready", "single:cash", update=1213, nonce="legacy")
        readydb.done(update=1214)
        readydb.drop("_legacy_prompt")
        with patch.object(tracker, "record", side_effect=AssertionError("primary journal")):
            self.handler.handle({**message("hello", OWNER), "update_id": 1215})
        self.assertEqual(self.texts(), [])

    def test_transient_final_outbox_failure_retries_without_losing_rent(self):
        self.handler.handle(message("/ready rent 1600", OWNER))
        answer = {**message("/ready income £48k", OWNER), "update_id": 1220}
        record = readydb.record
        failures = []

        def fail_once(update_id, outbox, at=None):
            if not failures:
                failures.append(True)
                raise sqlite3.OperationalError("fixture")
            return record(update_id, outbox, at)

        before = len(self.texts())
        with patch.object(readydb, "record", side_effect=fail_once):
            self.handler.handle(answer)
            self.handler.handle(answer)
        replies = self.texts()[before:]
        self.assertEqual(len(replies), 1)
        self.assertIn("Your entered advertised rent: £1,600/month", replies[0])

    def test_final_answer_rolls_back_if_building_the_result_fails(self):
        for mode, update_id in (("typed", 1221), ("direct", 1222)):
            with self.subTest(mode=mode):
                self.handler.handle(message("/ready rent 1600", OWNER))
                if mode == "typed":
                    self.handler.handle(message("/ready income", OWNER))
                answer = "£48k" if mode == "typed" else "/ready income £48k"
                checklist = self.handler._ready_checklist
                attempts = []

                def fail_once(*args, **kwargs):
                    attempts.append(True)
                    if len(attempts) == 1:
                        raise sqlite3.OperationalError("fixture")
                    return checklist(*args, **kwargs)

                before = len(self.texts())
                with patch.object(self.handler, "_ready_checklist", side_effect=fail_once):
                    self.handler.handle({**message(answer, OWNER), "update_id": update_id})
                self.assertEqual(len(attempts), 2)
                self.assertEqual(len(self.texts()[before:]), 1)
                self.assertIn("Your entered advertised rent: £1,600/month", self.texts()[-1])

    def test_persistent_outbox_failure_warns_and_does_not_block_other_users(self):
        self.admit()
        self.handler.handle(message("/ready rent 1600", OWNER))
        updates = [{**message("/ready income £48k", OWNER), "update_id": 1230},
                   {**message("/help", FRIEND), "update_id": 1231}]

        class Client:
            def __init__(self):
                self.gets = 0
                self.messages = []

            def clear_webhook(self):
                pass

            def call(self, method, **body):
                if method == "getUpdates":
                    self.gets += 1
                    return updates if self.gets == 1 else []
                if method == "sendMessage":
                    self.messages.append(body)
                return {"message_id": 1}

        client = Client()

        class Stop:
            def is_set(self):
                return client.gets >= 2

            def wait(self, seconds):
                pass

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "fixture"}), \
                patch.object(bot, "TelegramClient", return_value=client), \
                patch.object(bot.Handler, "tick"), \
                patch.object(readydb, "record", side_effect=sqlite3.OperationalError("fixture")):
            bot.poll(Mock(return_value=True), Stop())
        self.assertTrue(any(str(item["chat_id"]) == OWNER and "readiness" in item["text"].lower()
                            and "retry" in item["text"].lower() for item in client.messages))
        self.assertTrue(any(str(item["chat_id"]) == FRIEND for item in client.messages))
        self.assertEqual(d1.query("SELECT value FROM meta WHERE name = ?", bot.OFFSET_KEY),
                         [{"value": "1232"}])
        self.handler.handle({**message("/ready income £48k", OWNER), "update_id": 1232})
        self.assertIn("Your entered advertised rent: £1,600/month", self.texts()[-1])

    def test_failed_outbox_checkpoint_after_sending_is_visible(self):
        self.handler.handle(message("/ready rent 1600", OWNER))
        answer = {**message("/ready income £48k", OWNER), "update_id": 1240}
        record = readydb.record

        def fail_checkpoint(update_id, outbox, at=None):
            if not outbox:
                raise sqlite3.OperationalError("fixture")
            return record(update_id, outbox, at)

        before = len(self.texts())
        with patch.object(readydb, "record", side_effect=fail_checkpoint):
            self.handler.handle(answer)
        replies = self.texts()[before:]
        self.assertEqual(sum("Your entered advertised rent" in text for text in replies), 1)
        self.assertTrue(any("readiness" in text.lower() and "retry" in text.lower() for text in replies))

    def test_enquiry_draft_still_follows_a_failed_post_send_checkpoint(self):
        item = home(1)
        publish(item)
        record = readydb.record

        for update_id, fail_after in ((1244, "callback"), (1245, "draft")):
            with self.subTest(fail_after=fail_after):
                enquiry = {**press(f"q:{item.short_id}"), "update_id": update_id}

                def fail_checkpoint(current_id, outbox, at=None):
                    if ((fail_after == "callback" and len(outbox) == 1
                         and outbox[0]["method"] == "sendMessage")
                            or (fail_after == "draft" and not outbox)):
                        raise sqlite3.OperationalError("fixture")
                    return record(current_id, outbox, at)

                before = len(self.texts())
                with patch.object(readydb, "record", side_effect=fail_checkpoint):
                    self.handler.handle(enquiry)
                replies = self.texts()[before:]
                self.assertEqual(sum("Enquiry draft" in text for text in replies), 1)
                self.assertTrue(any("delivery could not be saved" in text.lower() for text in replies))

    def test_profile_save_warns_when_readiness_reconciliation_fails(self):
        renter.set_field("pets", "cat")
        ready.set_field("pets", "dog")
        with patch.object(readydb, "clear_shared", side_effect=sqlite3.OperationalError("fixture")):
            self.handler.handle(message("/profile pets cat", OWNER))
        self.assertEqual(renter.profile()["pets"], "cat")
        self.assertTrue(any("readiness" in text.lower() and "could not" in text.lower()
                            for text in self.texts()))

        with patch.object(readydb, "invalidate_tenure", side_effect=sqlite3.OperationalError("fixture")):
            self.handler.handle(message("/profile job teacher", OWNER))
        self.assertEqual(renter.profile()["job"], "teacher")
        self.assertTrue(any("tenure" in text.lower() and "could not" in text.lower()
                            for text in self.texts()))

    def test_onboarding_warns_if_readiness_reconciliation_fails(self):
        for field, helper, value, warning in (
                ("job", "invalidate_tenure", "teacher", "tenure"),
                ("pets", "clear_shared", "cat", "reconcile")):
            with self.subTest(field=field):
                step = next(index for index, item in enumerate(bot.ONBOARDING) if item[0] == field)
                tracker.expect("onboard", str(step), update=1241 + step)
                before = len(self.texts())
                with patch.object(readydb, helper, side_effect=sqlite3.OperationalError("fixture")):
                    self.handler.handle({**message(value, OWNER), "update_id": 1250 + step})
                self.assertEqual(renter.profile()[field], value)
                self.assertTrue(any(warning in text.lower() and "could not" in text.lower()
                                    for text in self.texts()[before:]))

    def test_locked_sidecar_does_not_delay_unrelated_profile_answers(self):
        ready.set_field("income", "£48k")
        self.handler.handle({**message("/ready cash", OWNER), "update_id": 1249})
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []
        owner = users.owner()

        def hold_sidecar():
            try:
                with users.use(owner), readydb.transaction():
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("sidecar test did not release its transaction")
            except BaseException as error:
                errors.append(error)

        def answer_profile():
            try:
                self.handler.handle({**message("/profile name", OWNER), "update_id": 1250})
                self.handler.handle({**message("Alex Smith", OWNER), "update_id": 1251})
                self.handler.handle({**message("/profile job teacher", OWNER), "update_id": 1252})
                self.handler.handle({**message("/profile pets cat", OWNER), "update_id": 1253})
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        holder = threading.Thread(target=hold_sidecar)
        answer = threading.Thread(target=answer_profile)
        holder.start()
        try:
            self.assertTrue(entered.wait(2))
            answer.start()
            self.assertTrue(finished.wait(1), "profile handling waited for readiness storage")
        finally:
            release.set()
            holder.join(3)
            if answer.ident is not None:
                answer.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(renter.profile()["name"], "Alex Smith")
        self.assertEqual(renter.profile()["job"], "teacher")
        self.assertEqual(renter.profile()["pets"], "cat")
        before = self.texts()
        self.handler.handle({**message("hello", OWNER), "update_id": 1254})
        self.assertEqual(self.texts(), before)
        self.assertIsNone(ready.load().values["cash"])

    def test_profile_and_readiness_prompts_do_not_consume_each_others_answers(self):
        self.handler.handle(message("/profile job", OWNER))
        self.handler.handle(message("/ready income", OWNER))
        self.assertIn("Finish or /cancel", self.texts()[-1])
        self.handler.handle(message("nurse", OWNER))
        self.assertEqual(renter.profile()["job"], "nurse")
        self.handler.handle(message("/ready income", OWNER))
        self.handler.handle(message("/profile job", OWNER))
        self.handler.handle(message("teacher", OWNER))
        self.assertEqual(renter.profile()["job"], "teacher")
        self.assertIsNone(ready.load().values["income"])
        self.assertIsNone(readydb.expected())


class ReadinessIsolationTests(ReadyBase):
    def test_readiness_refuses_a_link_to_the_alert_database(self):
        tracker.put("profile.job", "nurse")
        primary = localdb.database_path()
        sidecar = readydb.path()
        for link in (os.link, os.symlink):
            with self.subTest(link=link.__name__):
                link(primary, sidecar)
                try:
                    with self.assertRaisesRegex(OSError, "Readiness storage must"):
                        ready.set_field("income", "£48k")
                finally:
                    sidecar.unlink()
        self.assertIsNone(tracker.value("profile.ready_income"))

    def test_stalled_readiness_write_does_not_delay_two_person_alert_receipts(self):
        self.admit({"bills": 100.0})
        for target in (
            patch.object(main.enrich, "geocode"),
            patch.object(main.enrich, "assign_crime_percentiles"),
            patch.object(main, "passes_area", return_value=None),
            patch.object(main, "_ensure_photos"),
            patch.object(telegram, "throttle"),
            patch.object(main, "passes_basic", side_effect=lambda listing, **kw: main._cost_reject(listing)),
            patch.object(telegram, "send_text"),
        ):
            self.enterContext(target)
        fetch = self.enterContext(patch.object(main.SOURCES["rm"], "fetch",
                                               return_value=[home(1), home(2, 1500)]))
        events = []
        self.enterContext(patch.object(telegram, "send_listing",
                                      side_effect=lambda listing, reason: events.append(
                                          ("send", users.current_chat()))))
        watcher = watch.Watcher(["rm"])
        watcher.cycle()
        events.clear()
        fetch.return_value = [home(1), home(2, 1500), home(3, 1300)]
        save_seen, record_verdicts = main.save_seen, archive.record_verdicts

        target_key = "rm:3"

        def capture_seen(seen):
            save_seen(seen)
            event = ("seen", users.current_chat())
            if seen.get(target_key, {}).get("alerted") and event not in events:
                events.append(event)

        def capture_ack(chat_id, rows, settings, *args, **kwargs):
            result = record_verdicts(chat_id, rows, settings, *args, **kwargs)
            event = ("ack", str(chat_id))
            if (kwargs.get("acknowledged") and any(row[0] == target_key for row in rows)
                    and event not in events):
                events.append(event)
            return result

        entered, release = threading.Event(), threading.Event()
        errors = []
        owner = users.owner()

        def stall_readiness():
            try:
                with users.use(owner), readydb.transaction():
                    ready.set_field("income", "£48k")
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("readiness test did not release its transaction")
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=stall_readiness)
        with patch.object(localdb, "BUSY_TIMEOUT_MS", 150), \
                patch.object(main, "save_seen", side_effect=capture_seen), \
                patch.object(archive, "record_verdicts", side_effect=capture_ack):
            worker.start()
            try:
                self.assertTrue(entered.wait(2), "readiness transaction did not start")
                result = watcher.cycle()
                self.assertTrue(worker.is_alive(), "readiness transaction was not held during delivery")
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(result["complete"])
        self.assertEqual(events, [("send", OWNER), ("seen", OWNER), ("ack", OWNER),
                                  ("send", FRIEND), ("seen", FRIEND), ("ack", FRIEND)])

        target_key = "rm:4"
        events.clear()
        fetch.return_value = [home(1), home(2, 1500), home(3, 1300), home(4, 1250)]
        tracker.done(update=1301)
        tracker.record(1301, [{"method": "sendMessage", "body": {
            "chat_id": OWNER, "text": "<b>📋 Application readiness</b>\nlegacy result"}}])
        handler = bot.Handler(OWNER, Mock(return_value=True), Mock())
        entered, release = threading.Event(), threading.Event()
        record = readydb.record

        def stall_legacy_record(update_id, outbox, at=None):
            if update_id == 1301 and outbox:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("legacy replay test did not release its transaction")
            return record(update_id, outbox, at)

        def replay_legacy():
            try:
                handler.handle({**message("skip", OWNER), "update_id": 1301})
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=replay_legacy)
        with patch.object(localdb, "BUSY_TIMEOUT_MS", 150), \
                patch.object(main, "save_seen", side_effect=capture_seen), \
                patch.object(archive, "record_verdicts", side_effect=capture_ack), \
                patch.object(readydb, "record", side_effect=stall_legacy_record):
            worker.start()
            try:
                self.assertTrue(entered.wait(2), "legacy replay did not reach its sidecar write")
                result = watcher.cycle()
                self.assertTrue(worker.is_alive(), "legacy replay was not held during delivery")
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(result["complete"])
        self.assertEqual(events, [("send", OWNER), ("seen", OWNER), ("ack", OWNER),
                                  ("send", FRIEND), ("seen", FRIEND), ("ack", FRIEND)])


if __name__ == "__main__":
    unittest.main()
