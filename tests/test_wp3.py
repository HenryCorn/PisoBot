"""Work package 3: pipeline, viewing reminders, renter profile and enquiry draft, contact phone, More
photos, move-in fit, urgency tier and onboarding. Offline: Telegram is a Mock, geocoding is patched."""
import datetime as dt
import json
import os
import unittest
from unittest.mock import Mock, patch

from html.parser import HTMLParser

from pisobot import archive, bot, collage, d1, enrich, main, renter, sweep, telegram, tgtext, tracker, users
from pisobot.prefs import sid as short_id
from pisobot.sources import rightmove
from test_portal_facts import rm_property
from test_users import FRIEND, OWNER, Base, home, message


def shown_units(text):
    """What Telegram counts against its limits: the visible text (tags dropped, entities decoded),
    in UTF-16 code units. Independent of tgtext so the tests do not grade it with itself."""
    parts = []
    parser = HTMLParser(convert_charrefs=True)
    parser.handle_data = parts.append
    parser.feed(text)
    parser.close()
    return len("".join(parts).encode("utf-16-le")) // 2

UTC = dt.timezone.utc
LONDON = tracker.LONDON
NOW = dt.datetime(2026, 11, 10, 10, 0, tzinfo=UTC)          # a Tuesday, 10:00 in London (GMT)


def london(*args):
    return dt.datetime(*args, tzinfo=LONDON)


def publish(l):
    """What prefs.publish does for a card: the row its buttons resolve to (current person's DB)."""
    d1.query("INSERT OR REPLACE INTO listings (sid, key, lat, lng, url, address, cost) VALUES (?, ?, ?, ?, ?, ?, ?)",
             l.short_id, l.key, l.lat, l.lng, l.url, l.address, l.monthly_cost)
    return {"sid": l.short_id, "key": l.key, "url": l.url, "address": l.address, "cost": l.monthly_cost}


def press(data, chat=OWNER, markup=None, message_id=7):
    return {"callback_query": {"id": "q1", "data": data, "message": {
        "message_id": message_id, "chat": {"id": int(chat), "type": "private"},
        "reply_markup": markup or {"inline_keyboard": []}}}}


class TrackerTests(Base):
    def test_viewing_times_are_read_in_uk_time_and_must_be_soon_and_future(self):
        cases = {
            "tomorrow 9:15am": london(2026, 11, 11, 9, 15),
            "Sat 11:00": london(2026, 11, 14, 11, 0),
            "saturday at 6pm": london(2026, 11, 14, 18, 0),
            "14/11 18:30": london(2026, 11, 14, 18, 30),
            "14/11/2026 6.30pm": london(2026, 11, 14, 18, 30),
            "14 Nov 18:30": london(2026, 11, 14, 18, 30),
            "2026-11-14 18:30": london(2026, 11, 14, 18, 30),
            "Tue 9:00": london(2026, 11, 17, 9, 0),           # today's 9:00 has passed: next week
            "1/1 10:00": london(2027, 1, 1, 10, 0),           # day/month already past this year: next year
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(tracker.parse_when(text, NOW), expected)
        for text in ("soon", "Sat", "Sat 1400", "today 9:00", "Sat 25:00", "2027-06-01 10:00", "31/2 10:00"):
            with self.subTest(text), self.assertRaises(ValueError):
                tracker.parse_when(text, NOW)

    def test_reminders_evening_before_two_hours_before_then_rating_each_once(self):
        listing = publish(home(1))
        tracker.move(listing, "viewing", NOW)
        tracker.set_viewing(listing["key"], london(2026, 11, 14, 11, 0), NOW)

        def due(*when):
            found = tracker.due_reminders(london(*when))
            for key, flag, _, _ in found:
                tracker.reminded(key, flag)
            return [flag for _, flag, _, _ in found]
        self.assertEqual(due(2026, 11, 13, 17, 59), [])
        self.assertEqual(due(2026, 11, 13, 18, 0), ["eve"])
        self.assertEqual(due(2026, 11, 13, 21, 0), [])
        self.assertEqual(due(2026, 11, 14, 9, 0), ["soon"])
        self.assertEqual(due(2026, 11, 14, 11, 30), [])
        found = tracker.due_reminders(london(2026, 11, 14, 12, 5))
        self.assertEqual([f for _, f, _, _ in found], ["after"])
        self.assertIn("v:5:", json.dumps(found[0][3]))
        tracker.reminded(found[0][0], "after")
        self.assertEqual(due(2026, 11, 14, 13, 0), [])

    def test_late_booking_skips_reminders_already_due_and_stale_ones_are_never_sent(self):
        listing = publish(home(1))
        tracker.move(listing, "viewing", NOW)
        tracker.set_viewing(listing["key"], london(2026, 11, 11, 9, 0), london(2026, 11, 10, 20, 0))
        self.assertEqual(tracker.get(listing["key"])["reminded"], "eve")
        self.assertEqual([f for _, f, _, _ in tracker.due_reminders(london(2026, 11, 11, 7, 30))], ["soon"])
        self.assertEqual(tracker.due_reminders(london(2026, 11, 14, 9, 0)), [])     # > 2 days after

    def test_archive_rent_and_unseen_changes_are_told_once_for_active_stages_only(self):
        l = home(1, rent=1200)
        archive.upsert([l], "head")
        listing = publish(l)
        tracker.move(listing, "enquired")
        self.assertEqual(tracker.changes(), [])
        archive.upsert([home(1, rent=1100)], "head")
        found = tracker.changes()
        self.assertEqual(len(found), 1)
        self.assertIn("£1,200 → <b>£1,100</b>", found[0][1])
        tracker.told(*found[0][0:1], found[0][2], found[0][3])
        self.assertEqual(tracker.changes(), [])
        with archive._db() as db:
            db.execute("UPDATE adverts SET status = 'unseen' WHERE key = ?", (l.key,))
        found = tracker.changes()
        self.assertIn("No longer found", found[0][1])
        tracker.told(found[0][0], found[0][2], found[0][3])
        tracker.move(listing, "lost")
        archive.upsert([home(1, rent=900)], "head")
        self.assertEqual(tracker.changes(), [])            # closed homes are not followed

    def test_pipeline_view_groups_by_stage_with_next_action_and_short_buttons(self):
        a, b, c = home(1), home(2), home(3)
        archive.upsert([a, b], "head")
        archive.record_verdicts(OWNER, [(a.key, "matched", a.rent, a.monthly_cost, 31, 20)], "x")
        tracker.move(publish(a), "enquired", NOW)
        tracker.move(publish(b), "viewing", NOW)
        tracker.set_viewing(b.key, london(2026, 11, 14, 11, 0), NOW)
        tracker.set_field(b.key, "note", "<agent> says busy")
        publish(c)
        text, markup = tracker.view({c.key: {"url": c.url, "address": c.address, "cost": 1480}}, OWNER, NOW)
        self.assertLess(text.index("Viewing booked"), text.index("Enquired"))
        self.assertLess(text.index("Enquired"), text.index("Saved"))
        self.assertIn("£1,200 · 31 min · next: book a viewing", text)
        self.assertIn("next: viewing Sat 14 Nov 11:00", text)
        self.assertIn("next: send an enquiry", text)
        self.assertIn("&lt;agent&gt; says busy", text)
        data = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
        self.assertIn(f"P:t:{b.short_id}:0", data)
        self.assertIn(f"q:{c.short_id}", data)
        self.assertTrue(all(len(d.encode()) <= 64 for d in data))

    def test_long_pipeline_is_paged_with_numbering_across_pages(self):
        for i in range(1, 26):
            tracker.move(publish(home(i)), "enquired" if i < 23 else "offer", NOW)
        tracker.move(publish(home(30)), "won", NOW)
        first, nav = tracker.view({}, OWNER, NOW)
        self.assertIn("page 1/3", first)
        self.assertIn("Offer made (3)", first)
        self.assertEqual(nav["inline_keyboard"][-1], [{"text": "Next ▶", "callback_data": "P:g:1"}])
        self.assertNotIn("🏆", first)                                  # closed summary: last page only
        second, nav = tracker.view({}, OWNER, NOW, page=1)
        self.assertTrue(second.split("\n\n<b>")[1].split("</b>\n")[1].startswith("11. "))
        self.assertIn("Enquired (22)", second)                            # the whole group's count
        self.assertEqual([b["callback_data"] for b in nav["inline_keyboard"][-1]], ["P:g:0", "P:g:2"])
        self.assertTrue(all(d.endswith(":1") for row in nav["inline_keyboard"][:-1]
                            for d in (b["callback_data"] for b in row) if d.startswith("P:")))
        last, nav = tracker.view({}, OWNER, NOW, page=99)                # out of range: the last page
        self.assertIn("page 3/3", last)
        self.assertIn("🏆 Won 1", last)


class ProfileTests(Base):
    def test_move_in_windows_and_fit(self):
        today = dt.date(2026, 11, 10)
        cases = {"asap": ("2026-11-10", "2026-12-10"), "1 Dec": ("2026-12-01", "2026-12-01"),
                 "1/12-15/12": ("2026-12-01", "2026-12-15"), "December": ("2026-12-01", "2026-12-31"),
                 "2027-01-05..2027-01-20": ("2027-01-05", "2027-01-20"), "1 Dec to 3 Jan 2027": ("2026-12-01", "2027-01-03"),
                 "March": ("2027-03-01", "2027-03-31")}
        for text, (start, end) in cases.items():
            with self.subTest(text):
                self.assertEqual(renter.parse_window(text, today), (dt.date.fromisoformat(start), dt.date.fromisoformat(end)))
        for text in ("soonish", "15/12-1/12"):
            with self.subTest(text), self.assertRaises(ValueError):
                renter.parse_window(text, today)
        self.assertIsNone(renter.fit("2026-12-03", today))
        renter.set_field("movein", "1/12-15/12", today)
        self.assertEqual(renter.fit("2026-12-03", today)[0], "✅")
        self.assertEqual(renter.fit("2026-11-24", today), ("🟡", "available 24 Nov, 7 days before your move-in (1 Dec–15 Dec)"))
        self.assertEqual(renter.fit("2027-02-01", today)[0], "⚠️")
        self.assertEqual(renter.fit("now", today)[1], "available 10 Nov, 21 days before your move-in (1 Dec–15 Dec)")
        self.assertEqual(renter.fit(None, today)[0], "❔")

    def test_enquiry_draft_uses_profile_escapes_html_and_marks_gaps(self):
        listing = {"key": "rm:1", "address": "Flat 2, 10 Kings Road, London", "url": "https://x", "available": "2026-12-03"}
        text, missing = renter.draft(listing, dt.date(2026, 11, 10))
        self.assertEqual(missing, ["name", "move-in", "occupation", "household"])
        self.assertIn("[your name]", text)
        self.assertIn("/profile", text)
        renter.set_field("name", "Alex <b>Smith</b>")
        renter.set_field("movein", "1/12-15/12", dt.date(2026, 11, 10))
        renter.set_field("job", "nurse at St Thomas'")
        renter.set_field("household", "couple, no children")
        renter.set_field("pets", "none")
        text, missing = renter.draft(listing, dt.date(2026, 11, 10))
        self.assertEqual(missing, [])
        self.assertIn("Alex &lt;b&gt;Smith&lt;/b&gt;", text)
        self.assertIn("renting Flat 2, which I saw on Rightmove", text)
        self.assertIn("available from 3 Dec; I'm looking to move in 1 Dec–15 Dec", text)
        self.assertIn("earliest available viewing slots", text)
        self.assertIn("Pets: none", text)
        self.assertIn("never contacts agents", text)
        renter.set_field("pets", "clear")
        self.assertIsNone(renter.profile()["pets"])


class UrgencyTests(Base):
    def test_just_listed_or_best_fifth_of_recent_matches_is_top(self):
        fresh = home(1, rent=2400)
        fresh.first_listed_at = (NOW - dt.timedelta(hours=2)).isoformat()
        self.assertEqual(renter.tier(fresh, OWNER, NOW), "top")
        fresh.first_listed_at = (NOW - dt.timedelta(hours=4)).isoformat()
        self.assertIsNone(renter.tier(fresh, OWNER, NOW))          # no recent matches to compare with
        rows = [(f"rm:{100 + i}", "matched", 1000 + 100 * i, 1280 + 100 * i, 25, 30) for i in range(10)]
        archive.record_verdicts(OWNER, rows, "x", at=(NOW - dt.timedelta(days=1)).isoformat(timespec="seconds"))
        archive.record_verdicts(OWNER, [("rm:old", "matched", 500, 600, 5, 1)], "x",
                                at=(NOW - dt.timedelta(days=20)).isoformat(timespec="seconds"))
        cheap, dear = home(2, rent=1050), home(3, rent=1300)
        self.assertEqual(renter.tier(cheap, OWNER, NOW), "top")     # within the best two of ten
        self.assertIsNone(renter.tier(dear, OWNER, NOW))
        self.assertIsNone(renter.tier(cheap, FRIEND, NOW))          # someone else's matches don't count

    def test_alert_loop_marks_urgency_but_a_failure_never_blocks_the_alert(self):
        l = home(1)
        with patch.object(renter, "tier", return_value="top"):
            main._set_urgency(l)
        self.assertEqual(l.urgency, "top")
        l = home(2)
        with patch.object(renter, "tier", side_effect=RuntimeError("boom")):
            main._set_urgency(l)
        self.assertIsNone(l.urgency)

    def test_top_match_is_loud_in_quiet_hours_only_with_urgent_on(self):
        l = home(1)
        l.urgency = "top"
        post = Mock()
        post.return_value.json.return_value = {"ok": True, "result": {}}
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}), \
                patch.object(telegram, "quiet_now", return_value=True), \
                patch.object(telegram.collage, "build", return_value=None), \
                patch.object(telegram.net.session, "post", post):
            telegram.send_listing(l, "new")
            renter.set_urgent(True)
            telegram.send_listing(l, "new")
            l.urgency = None
            telegram.send_listing(l, "new")
        sent = [c.kwargs["data"] for c in post.call_args_list]
        self.assertIn("⚡ <b>Top match</b>", sent[0]["text"])
        self.assertEqual([d.get("disable_notification") for d in sent], ["true", None, "true"])
        self.assertTrue(all("loud" not in d for d in sent))


class CardTests(Base):
    def test_rightmove_phone_and_listing_time_are_parsed_defensively(self):
        page = {"contactInfo": {"telephoneNumbers": {"localNumber": " 020 7123  4567 ", "internationalNumber": "x"}}}
        self.assertEqual(rightmove._contact_phone(page), "020 7123 4567")
        self.assertEqual(rightmove._contact_phone(
            {"contactInfo": {"telephoneNumbers": {"internationalNumber": "+44 20 7123 4567"}}}), "+44 20 7123 4567")
        for junk in ({}, {"contactInfo": "call us"}, {"contactInfo": {"telephoneNumbers": ["020"]}},
                     {"contactInfo": {"telephoneNumbers": {"localNumber": "<a href=tel:1>call</a>"}}},
                     {"contactInfo": {"telephoneNumbers": {"localNumber": "12345"}}}):
            self.assertIsNone(rightmove._contact_phone(junk))
        listed = rightmove._listing(rm_property(5, firstVisibleDate="2026-11-08T09:30:00Z"))
        self.assertEqual((listed.first_listed, listed.first_listed_at), ("2026-11-08", "2026-11-08T09:30:00+00:00"))
        self.assertIsNone(rightmove._listing(rm_property(6, firstVisibleDate="2026-11-08")).first_listed_at)

    def test_card_shows_phone_as_text_and_move_in_fit(self):
        l = home(1)
        l.agent, l.agent_phone, l.available = "Foxtons", "020 7123 4567", "2026-12-03"
        renter.set_field("movein", "1/12-15/12", dt.date(2026, 11, 10))
        with patch.object(renter, "_today", return_value=dt.date(2026, 11, 10)):
            text = telegram.format_listing(l)
        self.assertIn("🏢 Foxtons · ☎️ 020 7123 4567", text)
        self.assertNotIn("tel:", text + json.dumps(telegram.listing_keyboard(l)))
        self.assertIn("✅ available 3 Dec fits your move-in (1 Dec–15 Dec)", text)

    def test_local_cards_gain_one_action_row_and_cloud_cards_are_unchanged(self):
        l = home(1)
        rows = telegram.listing_keyboard(l)["inline_keyboard"]
        self.assertEqual([b.get("callback_data", "url") for b in rows[1] + rows[2]],
                         [f"e:{l.short_id}", f"q:{l.short_id}", f"m:{l.short_id}"])
        self.assertEqual(rows[-1][0]["url"], l.url)
        tracker.move(publish(l), "viewing")
        self.assertEqual(telegram.listing_keyboard(l)["inline_keyboard"][1], tracker.card_row("viewing", l.short_id))
        with patch.dict(os.environ, {"STORAGE": ""}):
            os.environ.pop("STORAGE")
            cloud = telegram.listing_keyboard(l)["inline_keyboard"]
        self.assertEqual(len(cloud), 2)
        self.assertNotIn("Move-in", telegram.format_listing(l))

    def test_archive_keeps_more_photos_than_the_search_cache(self):
        l = home(1)
        l.images = [f"https://img/{i}.jpg" for i in range(20)]
        archive.upsert([l], "head")
        self.assertEqual(len(json.loads(archive.advert(l.key)["data"])["images"]), archive.ARCHIVE_IMAGES)
        from pisobot.sources import snapshot
        self.assertEqual(len(snapshot(l)["images"]), 6)


class BotBase(Base):
    def setUp(self):
        super().setUp()
        self.client = Mock()
        self.trigger = Mock(return_value=True)
        self.handler = bot.Handler(OWNER, self.trigger, self.client)

    def calls(self, method):
        return [c.kwargs for c in self.client.call.call_args_list if c.args[0] == method]

    def texts(self, chat=OWNER):
        return [k["text"] for k in self.calls("sendMessage") if str(k["chat_id"]) == chat]

    def answers(self):
        return [k["text"] for k in self.calls("answerCallbackQuery")]


class BotTests(BotBase):
    def test_card_buttons_and_typed_time_move_a_home_through_the_pipeline(self):
        l = home(1)
        listing = publish(l)
        card = telegram.listing_keyboard(l)
        self.handler.handle(press(f"e:{l.short_id}", markup=card))
        self.assertEqual(tracker.get(l.key)["stage"], "enquired")
        edited = self.calls("editMessageReplyMarkup")[-1]["reply_markup"]["inline_keyboard"]
        self.assertEqual(edited[1], tracker.card_row("enquired", l.short_id))
        self.assertEqual(edited[0], card["inline_keyboard"][0])          # Save/Hide row untouched
        self.handler.handle(press(f"p:b:{l.short_id}", markup={"inline_keyboard": edited}))
        self.assertEqual(tracker.get(l.key)["stage"], "viewing")
        self.assertTrue(self.calls("sendMessage")[-1]["reply_markup"]["force_reply"])
        self.handler.handle(message("whenever", OWNER))
        self.assertIn("Give a day and a time", self.texts()[-1])
        self.handler.handle(message("Sat 25:00", OWNER))
        self.assertIn("couldn't read the time", self.texts()[-1])
        self.handler.handle(message("tomorrow 18:30", OWNER))
        self.assertIn("Viewing booked for", self.texts()[-1])
        self.assertIsNotNone(tracker.get(l.key)["viewing_at"])
        self.handler.handle(message("more chatter", OWNER))
        self.assertIn("Viewing booked for", self.texts()[-1])           # no prompt pending: ignored
        self.handler.handle(press(f"v:4:{l.short_id}"))
        self.assertEqual((tracker.get(l.key)["stage"], tracker.get(l.key)["rating"]), ("viewed", 4))
        self.handler.handle(message("good light, small kitchen", OWNER))
        self.assertEqual(tracker.get(l.key)["note"], "good light, small kitchen")
        self.handler.handle(press(f"p:l:{l.short_id}"))
        self.handler.handle(message("let agreed with someone else", OWNER))
        self.assertEqual(tracker.get(l.key)["lost_reason"], "let agreed with someone else")
        self.assertEqual(listing["key"], l.key)

    def test_an_old_cards_enquired_button_never_moves_a_home_back(self):
        l = home(1)
        tracker.move(publish(l), "offer")
        self.handler.handle(press(f"e:{l.short_id}", markup=telegram.listing_keyboard(l)))
        self.assertEqual(tracker.get(l.key)["stage"], "offer")
        self.assertIn("Already in /pipeline", self.answers()[-1])

    def test_pipeline_command_and_its_buttons_refresh_the_list(self):
        l = home(1)
        publish(l)
        d1.query("INSERT INTO saved (key, info) VALUES (?, ?)", l.key, json.dumps({"url": l.url, "address": l.address}))
        self.handler.handle(message("/pipeline", OWNER))
        sent = self.calls("sendMessage")[-1]
        self.assertIn("Saved (1)", sent["text"])
        self.handler.handle(press(f"P:e:{l.short_id}", markup=sent["reply_markup"]))
        refreshed = self.calls("editMessageText")[-1]
        self.assertIn("Enquired (1)", refreshed["text"])
        self.assertNotIn("Saved (", refreshed["text"])

    def test_urgent_switch_never_touches_search_settings_or_baseline(self):
        d1.query("INSERT INTO meta (name, value) VALUES ('watch_initialized', '1')")
        before = main.settings_key(users.owner())
        self.handler.handle(message("/set urgent on", OWNER))
        self.assertTrue(renter.urgent_on())
        self.assertEqual(users.owner().params, {})
        self.assertEqual(main.settings_key(users.owner()), before)
        self.assertEqual(d1.query("SELECT value FROM meta WHERE name = 'watch_initialized'"), [{"value": "1"}])
        self.trigger.assert_not_called()
        self.handler.handle(message("/settings", OWNER))
        self.assertIn("<b>urgent</b> on", self.texts()[-1])
        self.handler.handle(message("/set urgent maybe", OWNER))
        self.assertIn("Usage", self.texts()[-1])

    def test_profile_pipeline_and_draft_edits_never_reset_the_search(self):
        d1.query("INSERT INTO meta (name, value) VALUES ('watch_initialized', '1')")
        l = home(1)
        publish(l)
        self.handler.handle(message("/profile name Alex Smith", OWNER))
        self.handler.handle(message("/profile movein", OWNER))
        self.handler.handle(message("asap", OWNER))
        self.handler.handle(press(f"e:{l.short_id}"))
        self.handler.handle(press(f"q:{l.short_id}"))
        self.assertIn("<pre>", self.texts()[-1])
        self.assertIn("Alex Smith", self.texts()[-1])
        self.assertEqual(users.owner().params, {})
        self.assertEqual(d1.query("SELECT value FROM meta WHERE name = 'watch_initialized'"), [{"value": "1"}])
        self.trigger.assert_not_called()
        self.handler.handle(message("/profile", OWNER))
        self.assertIn("Alex Smith", self.texts()[-1])

    def test_more_photos_sends_the_photos_the_card_did_not_show(self):
        l = home(1)
        l.image = "https://img/0.jpg"
        l.images = [f"https://img/{i}.jpg" for i in range(10)] + ["https://img/floorplan.jpg"]
        l.floorplan = "https://img/floorplan.jpg"
        archive.upsert([l], "head")
        publish(l)
        tracker.record_shown(l.key, 7, [f"https://img/{i}.jpg" for i in range(4)])
        self.handler.handle(press(f"m:{l.short_id}"))
        media = self.calls("sendMediaGroup")[-1]["media"]
        self.assertEqual([m["media"] for m in media], [f"https://img/{i}.jpg" for i in range(4, 10)])
        self.assertIn("caption", media[0])
        self.assertNotIn("caption", media[1])
        self.assertEqual(self.answers()[-1], "📷 6 more photos")
        few = home(2)
        few.images = [f"https://img/{i}.jpg" for i in range(5)]
        archive.upsert([few], "head")
        publish(few)
        tracker.record_shown(few.key, 7, [f"https://img/{i}.jpg" for i in range(4)])
        self.handler.handle(press(f"m:{few.short_id}"))
        self.assertEqual(self.calls("sendPhoto")[-1]["photo"], "https://img/4.jpg")
        none = home(3)
        archive.upsert([none], "head")
        publish(none)
        self.handler.handle(press(f"m:{none.short_id}"))
        self.assertIn("No more photos", self.answers()[-1])
        self.client.call.side_effect = lambda method, **kw: (_ for _ in ()).throw(
            bot.TelegramError("x", permanent=True)) if method == "sendMediaGroup" else None
        self.handler.handle(press(f"m:{l.short_id}"))                  # rejected for good: say so
        self.assertEqual(self.texts()[-1], "📷 Photos unavailable; use Open listing")

    def test_buttons_are_vps_only_and_unknown_or_oversized_data_is_ignored(self):
        l = home(1)
        publish(l)
        self.handler.handle(press(f"p:z:{l.short_id}"))
        self.handler.handle(press(f"v:9:{l.short_id}"))
        self.handler.handle(press("q:" + "x" * 70))
        self.assertIsNone(tracker.get(l.key))
        self.assertEqual(self.calls("sendMessage"), [])

    def test_tick_sends_each_reminder_once_and_rings_only_for_the_two_hour_one_at_night(self):
        l = home(1)
        tracker.move(publish(l), "viewing", NOW)
        tracker.set_viewing(l.key, london(2026, 11, 11, 8, 0), NOW)
        self.handler.tick(london(2026, 11, 10, 18, 5))
        self.handler.tick(london(2026, 11, 10, 18, 6))
        self.handler.tick(london(2026, 11, 11, 6, 30))
        sent = self.calls("sendMessage")
        self.assertEqual(len(sent), 2)
        self.assertIn("Viewing tomorrow", sent[0]["text"])
        self.assertNotIn("disable_notification", sent[0])
        self.assertIn("Viewing in two hours", sent[1]["text"])
        self.assertNotIn("disable_notification", sent[1])          # 06:30 is quiet, but it was booked
        self.handler.tick(london(2026, 11, 11, 23, 0))
        self.assertTrue(self.calls("sendMessage")[-1]["disable_notification"])   # "how was it?" at night
        self.client.call.side_effect = bot.TelegramError("down")
        tracker.move(publish(home(2)), "viewing", NOW)
        tracker.set_viewing(home(2).key, london(2026, 11, 12, 12, 0), NOW)
        self.handler.tick(london(2026, 11, 11, 18, 0))            # failure: logged, retried next tick
        self.assertEqual(tracker.get(home(2).key)["reminded"], "")

    def test_tick_is_a_no_op_on_the_cloud_path(self):
        with patch.object(users, "enabled", return_value=False), patch.object(tracker, "due_reminders") as due:
            self.handler.tick(NOW)
        due.assert_not_called()


class OnboardingTests(BotBase):
    def test_new_person_answers_skips_pauses_and_resumes(self):
        users.register(FRIEND, "Bea", "TheBeatifulest")
        self.handler.handle(message("/start", FRIEND))
        self.assertIn("1/11", self.texts(FRIEND)[-1])
        with patch.object(bot, "_locate", return_value=("Bank", 51.513, -0.089)):
            self.handler.handle(message("Bank", FRIEND))
        self.assertEqual(users.get(FRIEND).params["destination"]["name"], "Bank")
        self.handler.handle(press("o:0:0", FRIEND))
        self.assertIn("That question has passed", self.answers()[-1])
        self.handler.handle(press("o:1:1", FRIEND))                        # rent 1500
        self.handler.handle(press("o:2:s", FRIEND))                        # skip commute
        self.handler.handle(message("2", FRIEND))                         # beds typed
        self.assertEqual({k: v for k, v in users.get(FRIEND).params.items() if k != "destination"},
                         {"rent": 1500, "beds": 2})
        self.handler.handle(message("soonish", FRIEND))
        self.assertIn("couldn't read that move-in", self.texts(FRIEND)[-1])
        self.handler.handle(press("o:4:0", FRIEND))                        # asap
        self.trigger.assert_not_called()
        self.handler.handle(press("o:5:x", FRIEND))                        # pause at name
        self.trigger.assert_called_once_with("rentals", FRIEND)            # one scan for the new search
        self.assertIn("Paused", self.texts(FRIEND)[-1])
        self.handler.handle(message("/start", FRIEND))
        self.assertIn("<b>PisoBot</b>", self.texts(FRIEND)[-1])            # paused: /start is help again
        self.handler.handle(message("/onboard", FRIEND))
        self.assertIn("6/11", self.texts(FRIEND)[-1])
        for answer in ("Bea Jones", "designer", "just me"):
            self.handler.handle(message(answer, FRIEND))
        for data in ("o:8:0", "o:9:0", "o:10:0"):
            self.handler.handle(press(data, FRIEND))
        self.assertIn("All set", self.texts(FRIEND)[-1])
        with users.use(users.get(FRIEND)):
            facts = renter.profile()
        self.assertEqual((facts["name"], facts["pets"], facts["smoker"]), ("Bea Jones", "none", "no"))
        self.assertEqual(self.trigger.call_count, 1)                       # no search change after the pause
        with users.use(users.owner()):
            self.assertEqual(renter.profile()["name"], None)               # per person


class ReviewFixTests(BotBase):
    """Regressions for the independent WP3 review (wp3-independent-review.md), one or more per finding."""

    def update(self, payload, update_id):
        return {**payload, "update_id": update_id}

    # ---- P1-1: every message fits Telegram's limits; a rejected reply never blocks later updates ----

    def long_pipeline(self, count=20):
        for i in range(1, count + 1):
            item = home(i)
            item.address = f"Flat {i}, Example Residence 🏠🏠, 123 Long London Street, London N1 1AA"
            tracker.move(publish(item), "enquired", NOW)
            tracker.set_field(item.key, "note", ("Bright rooms 🌞, ask agent about damp & insulation. " * 3)[:120])

    def test_every_pipeline_page_fits_one_message(self):
        self.long_pipeline(40)
        text, markup = tracker.view({}, OWNER, NOW)
        pages = int(text.split("page 1/")[1].split("<")[0].split()[0])
        self.assertGreater(pages, 1)
        for page in range(pages):
            text, _ = tracker.view({}, OWNER, NOW, page=page)
            self.assertLessEqual(shown_units(text), 4096)

    def test_a_long_pipeline_never_blocks_later_updates(self):
        self.long_pipeline()
        self.admit()
        client = self.poll_client(lambda body: shown_units(body["text"]) > 4096 and bot.TelegramError("too long"))
        self.poll(client, [message("/pipeline", OWNER), message("/help", FRIEND)])
        self.assertIn(FRIEND, client.chats)
        self.assertEqual(d1.query("SELECT value FROM meta WHERE name = ?", bot.OFFSET_KEY), [{"value": "12"}])

    def test_a_reply_telegram_rejects_for_good_is_skipped_but_its_change_is_kept(self):
        self.admit()
        client = self.poll_client(lambda body: str(body["chat_id"]) == OWNER
                                  and bot.TelegramError("Bad Request", permanent=True))
        self.poll(client, [message("/profile name Alex Smith", OWNER), message("/help", FRIEND)])
        self.assertEqual(renter.profile()["name"], "Alex Smith")
        self.assertEqual(client.chats.count(OWNER), 1)                      # tried once, not forever
        self.assertIn(FRIEND, client.chats)
        self.assertEqual(d1.query("SELECT value FROM meta WHERE name = ?", bot.OFFSET_KEY), [{"value": "12"}])

    def test_poll_skips_an_update_a_permanent_error_escapes_from(self):
        client = self.poll_client(lambda body: None)
        with patch.object(bot.Handler, "handle", side_effect=[bot.TelegramError("x", permanent=True), None]):
            self.poll(client, [message("/help", OWNER), message("/help", OWNER)])
        self.assertEqual(d1.query("SELECT value FROM meta WHERE name = ?", bot.OFFSET_KEY), [{"value": "12"}])

    def poll_client(self, reject):
        class Client:
            chats, gets = [], 0
            def clear_webhook(self):
                pass
            def call(self, method, **body):
                if method == "getUpdates":
                    self.gets += 1
                    return self.updates if self.gets == 1 else []
                if method == "sendMessage":
                    self.chats.append(str(body["chat_id"]))
                    error = reject(body)
                    if error:
                        raise error
                return {"message_id": 1}
        return Client()

    def poll(self, client, updates):
        client.updates = [self.update(u, 10 + i) for i, u in enumerate(updates)]

        class Stop:
            waits = 0
            def is_set(self):
                return client.gets >= 2
            def wait(self, seconds):
                self.waits += 1
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "offline-token"}), \
                patch.object(bot, "TelegramClient", return_value=client), patch.object(bot.time, "monotonic", return_value=0):
            bot.poll(Mock(return_value=True), Stop())

    def test_long_profile_and_draft_are_split_under_the_limit(self):
        for name in renter.FIELDS:
            if name != "movein":
                renter.set_field(name, "🏠" * 300)
        l = home(1)
        publish(l)
        self.handler.handle(message("/profile", OWNER))
        self.handler.handle(press(f"q:{l.short_id}"))
        sent = self.calls("sendMessage")
        self.assertGreater(len(sent), 2)
        self.assertTrue(all(shown_units(m["text"]) <= 4096 for m in sent))

    def test_split_keeps_html_balanced_and_counts_utf16(self):
        text = "<b>" + "🏠" * 3000 + "</b>\n<i>tail &amp; end</i>"
        pieces = tgtext.split_html(text)
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertLessEqual(shown_units(piece), 4096)
            self.assertEqual(piece.count("<b>"), piece.count("</b>"))
        self.assertEqual("".join(p.replace("<b>", "").replace("</b>", "") for p in pieces).count("🏠"), 3000)
        self.assertTrue(pieces[-1].endswith("<i>tail &amp; end</i>"))
        self.assertLessEqual(len(tgtext.clip("é🏠" * 200, 200).encode("utf-16-le")) // 2, 200)

    # ---- P1-2: stage changes are checked against the current stage and replay-safe ----

    def test_stale_buttons_never_move_a_home_backwards_or_out_of_won_or_lost(self):
        item = home(1)
        listing = publish(item)
        tracker.move(listing, "offer")
        self.handler.handle(press(f"p:b:{item.short_id}"))               # an old card's Book viewing
        self.assertEqual(tracker.get(item.key)["stage"], "offer")
        self.assertEqual(self.answers()[-1], "Already " + tracker.LABELS["offer"])
        tracker.move(listing, "won")
        for data in (f"p:t:{item.short_id}", f"P:e:{item.short_id}:0", f"p:l:{item.short_id}",
                     f"v:3:{item.short_id}", f"p:o:{item.short_id}"):
            self.handler.handle(press(data))
            self.assertEqual(tracker.get(item.key)["stage"], "won", data)
        self.assertIsNone(tracker.expected())                              # no question was asked
        self.assertIsNone(tracker.get(item.key)["rating"])

    def test_reopen_restores_the_stage_a_home_was_closed_from(self):
        a, b = home(1), home(2)
        tracker.move(publish(a), "offer")
        self.handler.handle(press(f"p:l:{a.short_id}"))
        self.handler.handle(message("let to someone else", OWNER))
        self.assertIn(f"p:r:{a.short_id}", json.dumps(tracker.card_row("lost", a.short_id)))
        self.handler.handle(press(f"p:r:{a.short_id}"))
        self.assertEqual((tracker.get(a.key)["stage"], tracker.get(a.key)["lost_reason"]), ("offer", None))
        d1.query("INSERT INTO saved (key, info) VALUES (?, ?)", b.key, json.dumps({"url": b.url}))
        publish(b)
        self.handler.handle(press(f"P:l:{b.short_id}:0"))                 # lost straight from Saved
        self.handler.handle(press(f"p:r:{b.short_id}"))
        self.assertIsNone(tracker.get(b.key))                              # back to just saved

    def test_a_replayed_button_update_is_applied_once_and_only_unsent_replies_are_resent(self):
        item = home(1)
        publish(item)
        failed = []

        def flaky(method, **body):
            if method == "sendMessage" and not failed:
                failed.append(1)
                raise bot.TelegramError("timeout")
            return {"message_id": 50}
        self.client.call.side_effect = flaky
        update = self.update(press(f"p:b:{item.short_id}"), 30)
        with self.assertRaises(bot.TelegramError):
            self.handler.handle(update)
        self.assertEqual(tracker.get(item.key)["stage"], "viewing")
        tracker.move(publish(item), "viewed")                              # meanwhile, another button
        self.handler.handle(update)                                        # poll retries the update
        self.handler.handle(update)                                        # ... and once more
        self.assertEqual(tracker.get(item.key)["stage"], "viewed")         # never re-applied
        self.assertEqual(len(self.calls("answerCallbackQuery")), 1)
        self.assertEqual(len(self.calls("sendMessage")), 2)                # failed once, then sent once
        self.assertIn("When is it?", self.texts()[-1])

    # ---- P1-3: a failed onboarding question never shifts answers ----

    def onboarding_at_job(self):
        tracker.put("onboarded", "running")
        tracker.put("onboard.step", "6")
        tracker.expect("onboard", "6", at=NOW)
        return self.update(message("nurse at the hospital", OWNER), 10)

    def test_onboarding_answer_replayed_after_the_next_question_failed_is_not_reused(self):
        update = self.onboarding_at_job()
        failed = []

        def flaky(method, **body):
            if method == "sendMessage" and not failed:
                failed.append(1)
                raise bot.TelegramError("timeout")
            return {"message_id": 200}
        self.client.call.side_effect = flaky
        with patch.object(tracker, "now", return_value=NOW):
            with self.assertRaises(bot.TelegramError):
                self.handler.handle(update)
            self.handler.handle(update)
            self.assertEqual(tracker.expected()["message_id"], 200)     # the resent question is bound
        facts = renter.profile()
        self.assertEqual((facts["job"], facts["household"]), ("nurse at the hospital", None))
        self.assertEqual(tracker.value("onboard.step"), "7")
        self.assertIn("8/11", self.texts()[-1])

    def test_onboarding_answer_replayed_after_a_crash_before_its_record_is_not_reused(self):
        update = self.onboarding_at_job()
        with patch.object(tracker, "now", return_value=NOW):
            with patch.object(tracker, "record", side_effect=RuntimeError("power cut")), self.assertRaises(RuntimeError):
                self.handler.handle(update)
            self.assertEqual(self.calls("sendMessage"), [])
            self.handler.handle(update)
            self.handler.handle(update)
        facts = renter.profile()
        self.assertEqual((facts["job"], facts["household"]), ("nurse at the hospital", None))
        self.assertEqual(tracker.value("onboard.step"), "7")
        self.assertEqual(len(self.calls("sendMessage")), 1)
        self.assertIn("8/11", self.texts()[-1])

    def test_an_old_onboarding_button_never_answers_a_later_question(self):
        tracker.put("onboarded", "running")
        tracker.put("onboard.step", "7")
        self.handler.handle(press("o:6:0"))
        self.assertIn("That question has passed", self.answers()[-1])
        self.assertIsNone(renter.profile()["job"])

    # ---- P1-4: a typed answer is bound to its prompt and home ----

    def test_a_reply_to_an_older_prompt_never_books_another_home(self):
        a, b = home(1), home(2)
        ids = [110]

        def numbered(method, **body):
            if method == "sendMessage":
                ids[0] += 1
                return {"message_id": ids[0]}
            return True
        self.client.call.side_effect = numbered
        publish(a)
        publish(b)
        self.handler.handle(press(f"p:b:{a.short_id}"))
        prompt_a = ids[0]
        self.handler.handle(press(f"p:b:{b.short_id}"))
        prompt_b = ids[0]
        reply = message("2026-11-14 18:30", OWNER)
        reply["message"]["reply_to_message"] = {"message_id": prompt_a}
        with patch.object(tracker, "now", return_value=NOW):
            self.handler.handle(reply)
            self.assertIsNone(tracker.get(a.key)["viewing_at"])
            self.assertIsNone(tracker.get(b.key)["viewing_at"])
            self.assertIn("earlier message", self.texts()[-1])
            reply["message"]["reply_to_message"] = {"message_id": prompt_b}
            self.handler.handle(reply)
        self.assertIsNotNone(tracker.get(b.key)["viewing_at"])
        self.assertIsNone(tracker.get(a.key)["viewing_at"])

    def test_a_time_typed_after_the_home_moved_on_books_nothing(self):
        item = home(1)
        publish(item)
        self.handler.handle(press(f"p:b:{item.short_id}"))
        self.handler.handle(press(f"p:o:{item.short_id}"))                # offer made before answering
        with patch.object(tracker, "now", return_value=NOW):
            self.handler.handle(message("Sat 11:00", OWNER))
        self.assertEqual((tracker.get(item.key)["stage"], tracker.get(item.key)["viewing_at"]), ("offer", None))
        self.assertIn("no viewing time was set", self.texts()[-1])

    # ---- P2-5: one acknowledgement per change, shared by every path ----

    def unseen_saved_enquired_home(self):
        item = home(1)
        archive.upsert([item], "head", at="2026-11-10T00:00:00+00:00")
        tracker.move(publish(item), "enquired", NOW)
        d1.query("INSERT INTO saved (key, info) VALUES (?, ?)", item.key,
                 json.dumps({"url": item.url, "address": item.address}))
        scope = {"miles": 10.0, "min_rent": 1000, "max_rent": 1760, "min_beds": 1, "centre": [51.5152, -0.1419]}
        unseen = []
        for day in (11, 12):
            sweep_id = archive.start_sweep("rm", scope, {}, at=f"2026-11-{day}T00:00:00+00:00")
            unseen = archive.finish_sweep(sweep_id, True, at=f"2026-11-{day}T01:00:00+00:00")
        self.assertEqual(unseen, [item.key])
        return item, unseen

    def sweep_notice(self, keys):
        with patch.object(telegram, "send_text", side_effect=lambda text, *a, **kw: self.client.call(
                "sendMessage", chat_id=users.current_chat() or OWNER, text=text)), patch.object(telegram, "throttle"):
            sweep.notify_unseen(keys)

    def test_unseen_saved_home_is_told_once_when_the_sweep_goes_first(self):
        item, keys = self.unseen_saved_enquired_home()
        self.sweep_notice(keys)
        self.handler.tick(NOW)
        self.handler.tick(NOW)
        self.assertEqual(len(self.calls("sendMessage")), 1)
        self.assertIn("Saved home no longer found", self.texts()[0])
        self.assertEqual(tracker.get(item.key)["told_status"], "unseen")

    def test_unseen_saved_home_is_told_once_when_the_pipeline_goes_first(self):
        item, keys = self.unseen_saved_enquired_home()
        self.handler.tick(NOW)
        self.sweep_notice(keys)
        self.sweep_notice(keys)
        self.assertEqual(len(self.calls("sendMessage")), 1)
        self.assertIn("No longer found", self.texts()[0])
        self.assertTrue(json.loads(d1.query("SELECT info FROM saved")[0]["info"])["unseen"])

    def run_watch(self, listings, cards, baseline=False):
        with patch.object(enrich, "journey", return_value={"minutes": 25, "route": "Tube", "station": "S", "walk": 5}), \
                patch.object(enrich, "crime_score", return_value=30), patch.object(enrich, "assign_crime_percentiles"), \
                patch.object(enrich, "geocode"), patch.object(rightmove, "refine", return_value=True), \
                patch.object(main, "_ensure_photos"), patch.object(telegram, "throttle"), \
                patch.object(telegram, "send_listing", side_effect=lambda l, reason: cards.append((l.key, reason))):
            with users.use(users.owner()), users.criteria({}):
                main._run(["rm"], False, False, rentals=True, watch=True, baseline=baseline, sweep=True,
                          prefetched=(listings, []))

    def test_a_rent_cut_is_told_once_whether_the_watcher_or_the_pipeline_goes_first(self):
        for watcher_first in (True, False):
            with self.subTest(watcher_first=watcher_first):
                self.client.call.reset_mock()
                n = 1 if watcher_first else 2
                item = home(n, rent=1200)
                archive.upsert([item], "head")
                cards = []
                self.run_watch([item], cards, baseline=True)
                tracker.move(publish(item), "enquired", NOW)
                cut = home(n, rent=1100)
                archive.upsert([cut], "head")
                if watcher_first:
                    self.run_watch([cut], cards)
                    self.handler.tick(NOW)
                else:
                    self.handler.tick(NOW)
                    self.run_watch([cut], cards)
                    self.run_watch([cut], cards)                       # seen: never retried either
                self.assertEqual(len(cards) + len(self.texts()), 1, (cards, self.texts()))
                self.assertEqual(tracker.get(item.key)["told_rent"], 1100)

    def test_a_change_another_path_is_sending_waits_and_is_not_lost(self):
        l = home(1, rent=1200)
        archive.upsert([l], "head")
        tracker.move(publish(l), "enquired", NOW)
        archive.upsert([home(1, rent=1100)], "head")
        event = archive.change_id(l.key, "rent", 1100)
        self.assertEqual(tracker.claim(l.key, "rent", event), "claimed")      # the watcher is mid-send
        self.assertEqual(tracker.notify_changes(lambda text, kb: self.fail("sent twice")), 0)
        self.assertEqual(tracker.get(l.key)["told_rent"], 1200)               # baseline kept for later
        self.assertEqual(tracker.once(l.key, "rent", 1100, lambda: self.fail("sent twice")), "busy")
        tracker.release(l.key, "rent", event)                                 # its send failed
        sent = []
        self.assertEqual(tracker.notify_changes(lambda text, kb: sent.append(text)), 1)
        self.assertIn("£1,100", sent[0])
        later = tracker.now() + tracker.NOTICE_LEASE + dt.timedelta(seconds=1)
        self.assertEqual(tracker.claim(l.key, "rent", event, at=later), "told")

    # ---- P2-6: More photos skips exactly what the header showed ----

    def card_with_floorplan_header(self):
        from PIL import Image
        item = home(1)
        item.image = "https://img/0.jpg"
        item.images = [f"https://img/{i}.jpg" for i in range(4)]
        item.floorplan = "https://img/floorplan.jpg"
        archive.upsert([item], "head")
        publish(item)
        with patch.object(collage, "_thumb", return_value=Image.new("RGB", (collage.PHOTO_W, collage.HEIGHT))), \
                patch.object(collage, "_gallery_thumb", side_effect=lambda url, size: Image.new("RGB", size)), \
                patch.object(collage, "_map", return_value=Image.new("RGB", (collage.MAP_W, collage.HEIGHT))), \
                patch.object(telegram, "_call", return_value={"ok": True, "result": {"message_id": 7}}):
            telegram.send_listing(item, "new")
        return item

    def test_more_photos_sends_the_photo_a_floorplan_header_left_out(self):
        item = self.card_with_floorplan_header()
        shown = tracker.shown_urls(item.key, 7)
        self.assertNotIn("https://img/3.jpg", shown)
        self.handler.handle(press(f"m:{item.short_id}", message_id=7))
        self.assertEqual(self.calls("sendPhoto")[-1]["photo"], "https://img/3.jpg")
        self.assertEqual(self.answers()[-1], "📷 1 more photo")

    def test_more_photos_without_a_record_of_the_card_sends_all_and_says_so(self):
        item = home(1)
        item.images = [f"https://img/{i}.jpg" for i in range(3)]
        archive.upsert([item], "head")
        publish(item)
        self.handler.handle(press(f"m:{item.short_id}"))
        self.assertEqual([m["media"] for m in self.calls("sendMediaGroup")[-1]["media"]],
                         [f"https://img/{i}.jpg" for i in range(3)])
        self.assertEqual(self.answers()[-1], "📷 All 3 photos")

    # ---- P2-7: the evening-before reminder is only sent the evening before ----

    def test_a_missed_evening_reminder_is_not_sent_on_the_day(self):
        item = home(1)
        tracker.move(publish(item), "viewing", NOW)
        tracker.set_viewing(item.key, london(2026, 11, 14, 15, 0), NOW)
        self.assertEqual(tracker.due_reminders(london(2026, 11, 14, 9, 0)), [])
        self.assertEqual([r[1] for r in tracker.due_reminders(london(2026, 11, 14, 13, 0))], ["soon"])
        eve = tracker.due_reminders(london(2026, 11, 13, 23, 59))
        self.assertIn("Viewing tomorrow", eve[0][2])

    def test_a_viewing_just_after_midnight_gets_its_evening_reminder_until_two_hours_before(self):
        item = home(1)
        tracker.move(publish(item), "viewing", NOW)
        tracker.set_viewing(item.key, london(2026, 11, 14, 0, 30), NOW)
        self.assertEqual([r[1] for r in tracker.due_reminders(london(2026, 11, 13, 22, 0))], ["eve"])
        self.assertEqual([r[1] for r in tracker.due_reminders(london(2026, 11, 13, 22, 31))], ["soon"])

    # ---- P3-8: early/mid/late month windows ----

    def test_month_parts_set_the_window(self):
        today = NOW.date()
        cases = {"late December": ("2026-12-21", "2026-12-31"), "end of December": ("2026-12-21", "2026-12-31"),
                 "early December": ("2026-12-01", "2026-12-10"), "beginning of Jan": ("2027-01-01", "2027-01-10"),
                 "mid-December": ("2026-12-11", "2026-12-20"), "middle of February": ("2027-02-11", "2027-02-20"),
                 "late Feb": ("2027-02-21", "2027-02-28"), "early Dec to mid Jan": ("2026-12-01", "2027-01-20"),
                 "early November": ("2026-11-01", "2026-11-10"), "early November 2027": ("2027-11-01", "2027-11-10")}
        for text, (start, end) in cases.items():
            with self.subTest(text):
                self.assertEqual(renter.parse_window(text, today),
                                 (dt.date.fromisoformat(start), dt.date.fromisoformat(end)))
        renter.set_field("movein", "late December", today)
        self.assertEqual(tracker.value("profile.movein"), "2026-12-21..2026-12-31")
        self.assertNotEqual(renter.fit("2026-12-01", today)[0], "✅")

    def test_onboarding_echoes_the_move_in_window_it_read(self):
        tracker.put("onboarded", "running")
        tracker.put("onboard.step", "4")
        tracker.expect("onboard", "4")
        with patch.object(renter, "_today", return_value=NOW.date()):
            self.handler.handle(message("late December", OWNER))
        self.assertIn("📅 Move-in: 21 Dec–31 Dec", self.texts()[-2])
        self.assertIn("6/11", self.texts()[-1])


if __name__ == "__main__":
    unittest.main()
