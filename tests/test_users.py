import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pisobot import bot, cache, config, d1, main, telegram, users, watch
from pisobot.models import Listing

OWNER = "100"
FRIEND = "200"


def home(i=1, rent=1200):
    return Listing("rm", str(i), f"https://example.com/{i}", f"Home {i}", 1, "Private rent",
                   tenure="rent", rent=rent, bills=280, monthly_cost=rent + 280, deposit=1500,
                   lat=51.51 + i * 0.002, lng=-0.14, travel_minutes=25, crime_score=30)


def message(text, chat=FRIEND, username="TheBeatifulest", kind="private"):
    return {"message": {"chat": {"id": int(chat), "type": kind}, "text": text,
                        "from": {"id": int(chat), "username": username, "first_name": "Bea"}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        for target in (
            patch.dict(os.environ, {"STORAGE": "local", "PISOBOT_STATE_DIR": self.tmp.name,
                                    "HEALTHCHECK_URL": "", "TELEGRAM_CHAT_ID": OWNER,
                                    "TELEGRAM_INVITE_USERNAMES": "@TheBeatifulest", "MAX_USERS": "2"}),
            patch.object(main, "STATE_DIR", self.tmp.name),
            patch.object(main, "SEEN_PATH", str(self.state / "seen.json")),
            patch.object(cache, "PATH", str(self.state / "cache.json")),
            patch.object(cache, "_data", {}),
            patch("sys.stdout"),
            patch("sys.stderr"),
        ):
            target.start()
            self.addCleanup(target.stop)

    def admit(self, params=None):
        person = users.register(FRIEND, "Bea", "TheBeatifulest")
        if params:
            users.save_params(person, params)
        return person


class ParamTests(Base):
    def test_parse_validates_and_normalises(self):
        self.assertEqual(users.parse("budget", "£1,750"), 1750)
        self.assertEqual(users.parse("commute", "45 min"), 45)
        self.assertEqual(users.parse("radius", "7"), 5)
        self.assertEqual(users.parse("radius", "8"), 10)
        for name, raw in (("budget", "lots"), ("budget", "50"), ("beds", "9"), ("commute", "nan")):
            with self.assertRaises(ValueError):
                users.parse(name, raw)

    def test_criteria_overrides_and_restores_config(self):
        before = config.MAX_MONTHLY_COST, config.TARGET_NAME
        params = {"budget": 1900.0, "destination": {"name": "Bank", "lat": 51.513, "lng": -0.089}}
        with users.criteria(params):
            self.assertEqual((config.MAX_MONTHLY_COST, config.TARGET_NAME), (1900.0, "Bank"))
        self.assertEqual((config.MAX_MONTHLY_COST, config.TARGET_NAME), before)

    def test_fetch_params_are_the_union_and_never_move_the_search_centre(self):
        a = users.User(OWNER, owner=True, params={"budget": 1600, "beds": 2})
        b = users.User(FRIEND, params={"budget": 2000, "radius": 10, "minrent": 900,
                                       "destination": {"name": "Bank", "lat": 51.5, "lng": -0.09}})
        union = users.fetch_params([a, b])
        self.assertEqual(union, {"budget": 2000, "radius": 10, "beds": 1, "minrent": 900})

    def test_only_invited_usernames_and_max_users_are_admitted(self):
        self.assertIsNone(users.register("300", "Eve", "eve"))
        self.assertIsNotNone(self.admit())
        with patch.dict(os.environ, {"TELEGRAM_INVITE_USERNAMES": "TheBeatifulest,eve"}):
            self.assertIsNone(users.register("300", "Eve", "eve"))
        self.assertEqual([p.chat_id for p in users.everyone()], [OWNER, FRIEND])
        self.assertTrue(users.everyone()[0].owner)

    def test_people_have_separate_databases_and_seen_files(self):
        friend = self.admit()
        d1.query("INSERT INTO hidden (key) VALUES ('rm:1')")
        with users.use(friend):
            self.assertEqual(d1.query("SELECT key FROM hidden"), [])
            main.save_seen({"rm:9": {}})
        self.assertEqual(main.load_seen(), {})
        self.assertTrue((self.state / "users" / FRIEND / "seen.json").exists())
        self.assertTrue((self.state / "users" / FRIEND / "pisobot.db").exists())


class SharedWatchTests(Base):
    def setUp(self):
        super().setUp()
        for target in (
            patch.object(main.enrich, "geocode"),
            patch.object(main.enrich, "assign_crime_percentiles"),
            patch.object(main, "passes_area", return_value=None),
            patch.object(main, "_ensure_photos"),
            patch.object(telegram, "throttle"),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.enterContext(patch.object(
            main, "passes_basic",
            side_effect=lambda l: "cost" if l.rent + l.bills > config.MAX_MONTHLY_COST else None))
        self.fetch = self.enterContext(patch.object(main.SOURCES["rm"], "fetch",
                                                    return_value=[home(1, 1200), home(2, 1500)]))
        self.sent = []
        self.enterContext(patch.object(telegram, "send_listing", side_effect=lambda l, reason: self.sent.append(
            (users.current_chat(), l.key, reason))))
        self.texts = []
        self.enterContext(patch.object(telegram, "send_text", side_effect=lambda text, **_: self.texts.append(
            (users.current_chat(), text))))
        self.admit({"budget": 2000.0, "bills": 100.0})

    def test_one_fetch_serves_everyone_with_their_own_criteria_and_alerts(self):
        result = watch.Watcher(["rm"]).cycle()
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(result["people"], {OWNER: 1, "Bea": 2})
        self.assertEqual([(chat, text.split(":")[1].split()[0]) for chat, text in self.texts],
                         [(OWNER, "1"), (FRIEND, "2")])
        self.fetch.return_value = [home(1, 1200), home(2, 1500), home(3, 1300)]
        watch.Watcher(["rm"]).cycle()
        self.assertEqual(sorted(self.sent), [(OWNER, "rm:3", "new"), (FRIEND, "rm:3", "new")])
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(config.MAX_MONTHLY_COST, users.DEFAULTS["MAX_MONTHLY_COST"])

    def test_report_goes_only_to_whoever_asked(self):
        watch.Watcher(["rm"]).cycle()
        with patch.object(main, "send_report") as report:
            watch.Watcher(["rm"]).cycle("report", chat_id=FRIEND)
        self.assertEqual(report.call_count, 1)

    def test_one_persons_failure_does_not_block_the_other(self):
        real = main._run

        def flaky(*args, **kwargs):
            if users.current_chat() == FRIEND:
                raise RuntimeError("blocked by user")
            return real(*args, **kwargs)
        with patch.object(main, "_run", side_effect=flaky):
            with self.assertRaisesRegex(RuntimeError, "blocked"):
                watch.Watcher(["rm"]).cycle()
        self.assertEqual(self.texts, [(OWNER, self.texts[0][1])])
        self.assertEqual(watch._meta("watch_initialized"), "1")


class SharedBotTests(Base):
    def setUp(self):
        super().setUp()
        self.client = Mock()
        self.trigger = Mock(return_value=True)
        self.handler = bot.Handler(OWNER, self.trigger, self.client)

    def texts(self, chat=None):
        return [c.kwargs["text"] for c in self.client.call.call_args_list
                if c.args[0] == "sendMessage" and (chat is None or str(c.kwargs["chat_id"]) == chat)]

    def test_invited_username_joins_and_strangers_are_ignored(self):
        self.handler.handle(message("/start", "300", "stranger"))
        self.handler.handle(message("/start", FRIEND, "TheBeatifulest", kind="group"))
        self.client.call.assert_not_called()
        self.handler.handle(message("/start", FRIEND, "thebeatifulest"))
        self.assertIn("Welcome", self.texts(FRIEND)[0])
        self.assertIn("joined", self.texts(OWNER)[0])
        self.assertEqual(self.texts(FRIEND)[-1], bot.HELP)
        self.handler.handle(message("/report", FRIEND, "renamed"))
        self.trigger.assert_called_with("report", FRIEND)

    def test_set_and_show_settings_per_person(self):
        self.admit()
        self.handler.handle(message("/set budget £1,750"))
        self.assertIn("Search updated", self.texts(FRIEND)[-1])
        self.trigger.assert_called_with("rentals", FRIEND)
        self.assertEqual(users.get(FRIEND).params, {"budget": 1750})
        self.assertEqual(users.owner().params, {})
        self.handler.handle(message("/set beds lots"))
        self.assertIn("expected a number", self.texts(FRIEND)[-1])
        self.handler.handle(message("/set nonsense 1"))
        self.assertIn("Usage", self.texts(FRIEND)[-1])
        self.handler.handle(message("/settings"))
        self.assertIn("<b>budget</b> £1,750", self.texts(FRIEND)[-1])
        self.assertIn("<b>beds</b> 1 <i>(default)</i>", self.texts(FRIEND)[-1])
        self.handler.handle(message("/set budget default"))
        self.assertEqual(users.get(FRIEND).params, {})

    def test_owner_settings_live_in_the_users_table(self):
        self.handler.handle(message("/set commute 35", OWNER, "quique"))
        self.assertEqual(users.owner().params, {"commute": 35})
        self.trigger.assert_called_with("rentals")

    def test_settings_change_rebaselines_only_that_person(self):
        friend = self.admit()
        watch._put_meta("watch_initialized", "1")
        with users.use(friend):
            watch._put_meta("watch_initialized", "1")
        self.handler.handle(message("/set commute 30"))
        self.assertEqual(watch._meta("watch_initialized"), "1")
        with users.use(friend):
            self.assertIsNone(watch._meta("watch_initialized"))

    def test_destination_lookup(self):
        self.admit()
        with patch.object(bot, "_locate", return_value=("Canary Wharf", 51.5054, -0.0235)):
            self.handler.handle(message("/set destination canary wharf"))
        self.assertEqual(users.get(FRIEND).params["destination"]["name"], "Canary Wharf")
        with patch.object(bot, "_locate", return_value=("Brighton", 50.82, -0.14)):
            self.handler.handle(message("/set destination Brighton"))
        self.assertIn("outside London", self.texts(FRIEND)[-1])
        self.assertEqual(bot._locate("51.5, -0.12")[1:], (51.5, -0.12))

    def test_buttons_record_choices_in_the_tapping_persons_database(self):
        friend = self.admit()
        with users.use(friend):
            d1.query("INSERT INTO listings VALUES ('0123456789', 'rm:1', 51.5, -0.1, 'u', 'a', 1)")
        self.handler.handle({"callback_query": {"id": "q", "data": "s:0123456789", "message": {
            "chat": {"id": int(FRIEND), "type": "private"}, "message_id": 5,
            "reply_markup": {"inline_keyboard": [[{"text": "Save", "callback_data": "s:0123456789"}]]}}}})
        self.assertEqual(d1.query("SELECT key FROM saved"), [])
        with users.use(friend):
            self.assertEqual([r["key"] for r in d1.query("SELECT key FROM saved")], ["rm:1"])

    def test_only_owner_manages_users(self):
        self.admit()
        self.handler.handle(message("/users"))
        self.assertEqual(self.texts(), [])
        self.handler.handle(message("/users", OWNER, "quique"))
        self.assertIn("Bea", self.texts(OWNER)[-1])
        self.handler.handle(message(f"/removeuser {FRIEND}", OWNER, "quique"))
        self.assertIsNone(users.get(FRIEND))


if __name__ == "__main__":
    unittest.main()
