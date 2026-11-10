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
        before = config.MAX_RENT, config.TARGET_NAME
        params = {"rent": 1900.0, "destination": {"name": "Bank", "lat": 51.513, "lng": -0.089}}
        with users.criteria(params):
            self.assertEqual((config.MAX_RENT, config.TARGET_NAME), (1900.0, "Bank"))
        self.assertEqual((config.MAX_RENT, config.TARGET_NAME), before)

    def test_portal_groups_share_only_identical_queries_and_ignore_destination(self):
        a = users.User(OWNER, owner=True, params={"rent": 1600, "beds": 2})
        b = users.User(FRIEND, params={"rent": 2000, "radius": 10, "minrent": 900,
                                       "destination": {"name": "Bank", "lat": 51.5, "lng": -0.09}})
        groups = users.search_groups([a, b])
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0][0], {"rent": 1600, "radius": 5, "beds": 2, "minrent": 1000})
        self.assertEqual(groups[1][0], {"rent": 2000, "radius": 10, "beds": 1, "minrent": 900})
        b.params = {**a.params, "destination": b.params["destination"], "bills": 100}
        self.assertEqual(users.search_groups([a, b]), [(groups[0][0], [a, b])])

    def test_rent_is_a_portal_filter_but_the_all_in_budget_is_local(self):
        owner = users.User(OWNER, owner=True, params={"budget": 1800})
        friend = users.User(FRIEND, params={"rent": 1400})
        same_search = users.User("300", params={"budget": 1700})
        groups = users.search_groups([owner, same_search, friend])
        self.assertEqual([[p.chat_id for p in g[1]] for g in groups], [[OWNER, "300"], [FRIEND]])
        self.assertEqual(groups[0][0]["rent"], 1600)
        self.assertEqual(groups[1][0]["rent"], 1400)
        self.assertNotIn("budget", groups[0][0])

    def test_stored_legacy_budget_keeps_meaning_an_all_in_cap(self):
        owner = users.User(OWNER, owner=True, params={"budget": 1800})
        values = users.effective(owner)
        self.assertEqual((values["budget"], values["rent"]), (1800, 1600))
        self.assertEqual(users.overrides(owner.params), {"MAX_ALL_IN": 1800})
        self.assertIn("<b>budget</b> £1,800", users.describe(owner))
        with users.criteria(owner.params):
            self.assertEqual((config.MAX_ALL_IN, config.MAX_RENT), (1800, 1600))
            self.assertEqual(main._cost_reject(home(1, 1600)), "all_in_cap")   # 1600 + ~283 bills > 1800
            self.assertIsNone(main._cost_reject(home(1, 1500)))
        self.assertIsNone(config.MAX_ALL_IN)

    def test_all_in_cap_is_off_by_default_and_listed_as_off(self):
        person = users.User(FRIEND)
        self.assertIsNone(users.effective(person)["budget"])
        text = users.describe(person)
        self.assertIn("<b>budget</b> off <i>(default)</i>", text)
        self.assertIn("<b>rent</b> £1,600 <i>(default)</i>", text)

    def test_two_people_keep_separate_caps_and_destinations(self):
        a = users.User(OWNER, owner=True, params={"rent": 1500, "budget": 1700})
        b = users.User(FRIEND, params={"destination": {"name": "Bank", "lat": 51.513, "lng": -0.089}})
        with users.criteria(a.params):
            seen_a = (config.MAX_RENT, config.MAX_ALL_IN, config.TARGET_NAME)
            with users.criteria(b.params):
                seen_b = (config.MAX_RENT, config.MAX_ALL_IN, config.TARGET_NAME)
        self.assertEqual(seen_a, (1500, 1700, "Oxford Circus"))
        self.assertEqual(seen_b, (1500, 1700, "Bank"))      # criteria() only overrides what is set
        with users.criteria(b.params):
            self.assertEqual((config.MAX_RENT, config.MAX_ALL_IN), (1600, None))
        self.assertEqual((config.MAX_RENT, config.MAX_ALL_IN), (1600, None))

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

    def test_corrupt_settings_are_not_silently_reset(self):
        self.admit()
        d1.query("UPDATE users SET params = ? WHERE chat_id = ?", "{broken", FRIEND)
        with self.assertRaisesRegex(RuntimeError, "Invalid search settings"):
            users.get(FRIEND)

    def test_radius_and_destination_do_not_create_a_hidden_12km_destination_cutoff(self):
        before = config.SEARCH_RADIUS_KM, config.TARGET_LAT, config.TARGET_LNG
        person = self.admit()
        listing = home()
        listing.lat, listing.lng = config.TARGET_LAT + 0.125, config.TARGET_LNG
        with users.use(person), users.criteria({"radius": 10}), \
                patch.object(main.SOURCES["rm"], "refine", return_value=True):
            self.assertIsNone(main.passes_basic(listing))
        listing.lat = config.TARGET_LAT
        with users.use(person), users.criteria({
                "destination": {"name": "East London", "lat": 51.5152, "lng": 0.15}}), \
                patch.object(main.SOURCES["rm"], "refine", return_value=True):
            self.assertIsNone(main.passes_basic(listing))
        self.assertEqual((config.SEARCH_RADIUS_KM, config.TARGET_LAT, config.TARGET_LNG), before)

    def test_missing_longitude_is_rejected_without_failing_the_shared_cycle(self):
        listing = home()
        listing.lng = None
        with patch.object(main.SOURCES["rm"], "fetch", return_value=[listing]), \
                patch.object(main.enrich, "geocode"), patch.object(main.enrich, "assign_crime_percentiles"):
            result = watch.Watcher(["rm"]).cycle(dry_run=True)
        self.assertTrue(result["complete"])
        self.assertEqual(result["matches"], 0)

    def test_telegram_sender_routes_actual_requests_and_restores_owner_context(self):
        friend = self.admit()
        response = Mock()
        response.json.return_value = {"ok": True}
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}), \
                patch.object(telegram.net.session, "post", return_value=response) as post:
            with users.use(friend):
                telegram.send_text("Friend")
            telegram.send_text("Owner")
        self.assertEqual([call.kwargs["data"]["chat_id"] for call in post.call_args_list], [FRIEND, OWNER])


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
            side_effect=lambda l: main._cost_reject(l)))
        self.fetch = self.enterContext(patch.object(main.SOURCES["rm"], "fetch",
                                                    return_value=[home(1, 1200), home(2, 1500)]))
        self.sent = []
        self.enterContext(patch.object(telegram, "send_listing", side_effect=lambda l, reason: self.sent.append(
            (users.current_chat(), l.key, reason))))
        self.texts = []
        self.enterContext(patch.object(telegram, "send_text", side_effect=lambda text, **_: self.texts.append(
            (users.current_chat(), text))))
        self.admit({"bills": 100.0})

    def test_one_fetch_serves_everyone_with_their_own_criteria_and_alerts(self):
        users.save_params(users.owner(), {"budget": 1600.0})   # owner caps rent + bills, friend only rent
        result = watch.Watcher(["rm"]).cycle()
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(result["people"], {OWNER: 1, "Bea": 2})
        self.assertEqual([(chat, text.split(":")[1].split()[0]) for chat, text in self.texts],
                         [(OWNER, "1"), (FRIEND, "2")])
        self.fetch.return_value = [home(1, 1200), home(2, 1500), home(3, 1300)]
        watch.Watcher(["rm"]).cycle()
        self.assertEqual(sorted(self.sent), [(OWNER, "rm:3", "new"), (FRIEND, "rm:3", "new")])
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(config.MAX_RENT, users.DEFAULTS["MAX_RENT"])
        owner_stats = json.loads(d1.query("SELECT stats FROM rental_funnel ORDER BY id LIMIT 1")[0]["stats"])
        with users.use(users.get(FRIEND)):
            friend_stats = json.loads(d1.query("SELECT stats FROM rental_funnel ORDER BY id LIMIT 1")[0]["stats"])
        self.assertEqual(owner_stats["sources"]["rm"]["rejected"], {"all_in_cap": 1})
        self.assertEqual(friend_stats["sources"]["rm"]["rejected"], {})

    def _owner_alerts(self):
        return sorted((key, reason) for chat, key, reason in self.sent if chat == OWNER)

    def _owner_seen(self):
        return main.load_seen()

    def test_changing_bills_estimates_never_fires_price_drops(self):
        watch.Watcher(["rm"]).cycle()                       # baseline: rm:1 at £1,200, rm:2 at £1,500
        self.assertEqual(self._owner_seen()["rm:1"]["rent"], 1200)
        cheaper_tax = [home(1, 1200), home(2, 1500)]
        for l in cheaper_tax:
            l.council_tax_band = "A"                       # all-in falls ~£50 with the rent unchanged
        self.fetch.return_value = cheaper_tax
        watch.Watcher(["rm"]).cycle()
        self.assertEqual(self.sent, [])
        dearer_tax = [home(1, 1200), home(2, 1500)]
        for l in dearer_tax:
            l.council_tax_band = "H"
        self.fetch.return_value = dearer_tax
        watch.Watcher(["rm"]).cycle()
        self.assertEqual(self.sent, [])

    def test_an_advertised_rent_cut_still_alerts_and_stores_the_new_rent(self):
        watch.Watcher(["rm"]).cycle()
        self.fetch.return_value = [home(1, 1150), home(2, 1500)]
        watch.Watcher(["rm"]).cycle()
        self.assertEqual(self._owner_alerts(), [("rm:1", "drop")])
        self.assertEqual(self._owner_seen()["rm:1"]["rent"], 1150)
        self.sent.clear()
        watch.Watcher(["rm"]).cycle()
        self.assertEqual(self.sent, [])

    def test_records_without_a_stored_rent_do_not_alert_and_get_one(self):
        watch.Watcher(["rm"]).cycle()
        seen = self._owner_seen()
        for record in seen.values():
            record.pop("rent")                              # written before rents were stored
            record["cost"] = 3000                           # an old all-in figure must not matter
        main.save_seen(seen)
        self.fetch.return_value = [home(1, 1100), home(2, 1500)]
        watch.Watcher(["rm"]).cycle()
        self.assertEqual(self._owner_alerts(), [])             # the friend's records do carry a rent
        self.assertEqual(self._owner_seen()["rm:1"]["rent"], 1100)    # backfilled
        self.fetch.return_value = [home(1, 1050), home(2, 1500)]
        watch.Watcher(["rm"]).cycle()
        self.assertEqual(self._owner_alerts(), [("rm:1", "drop")])

    def test_stored_budget_1800_baselines_quietly_when_it_is_changed(self):
        users.save_params(users.owner(), {"budget": 1800.0})   # production owner today
        self.fetch.return_value = [home(1, 1200), home(2, 1600)]
        result = watch.Watcher(["rm"]).cycle()
        self.assertEqual(result["people"][OWNER], 1)            # 1600 + ~283 > 1800: all_in_cap
        stats = json.loads(d1.query("SELECT stats FROM rental_funnel ORDER BY id LIMIT 1")[0]["stats"])
        self.assertEqual(stats["sources"]["rm"]["rejected"], {"all_in_cap": 1})
        self.assertEqual(set(main.load_seen()), {"rm:1"})
        self.assertEqual(self.sent, [])
        # the owner turns the all-in cap off WITHOUT clearing the baseline marker: the settings
        # fingerprint alone must re-baseline, so the newly eligible rm:2 is not a flood of alerts
        users.save_params(users.owner(), {})
        before = len([t for c, t in self.texts if c == OWNER])
        result = watch.Watcher(["rm"]).cycle()
        self.assertEqual(result["people"][OWNER], 2)
        self.assertEqual([(key, reason) for chat, key, reason in self.sent if chat == OWNER], [])
        owner_texts = [t for c, t in self.texts if c == OWNER]
        self.assertEqual(len(owner_texts), before + 1)
        self.assertIn("2 current matches", owner_texts[-1])
        self.assertEqual(set(main.load_seen()), {"rm:1", "rm:2"})
        # changing the rent cap re-baselines the same way, and ordinary alerts resume afterwards
        users.save_params(users.owner(), {"rent": 1700.0})
        self.fetch.return_value = [home(1, 1200), home(2, 1600), home(3, 1650)]
        watch.Watcher(["rm"]).cycle()
        self.assertEqual([k for c, k, r in self.sent if c == OWNER], [])
        self.assertEqual(set(main.load_seen()), {"rm:1", "rm:2", "rm:3"})
        self.fetch.return_value = [home(1, 1200), home(2, 1600), home(3, 1650), home(4, 1300)]
        watch.Watcher(["rm"]).cycle()
        self.assertEqual(self._owner_alerts(), [("rm:4", "new")])

    def test_rent_cap_rejections_are_reported_separately_from_the_all_in_cap(self):
        self.fetch.return_value = [home(1, 1550), home(2, 1700)]
        watch.Watcher(["rm"]).cycle()
        owner = json.loads(d1.query("SELECT stats FROM rental_funnel ORDER BY id LIMIT 1")[0]["stats"])
        self.assertEqual(owner["sources"]["rm"]["rejected"], {"rent_cap": 1})
        self.assertEqual(owner["sources"]["rm"]["matched"], 1)       # £1,550 rent is no longer hidden
        self.assertIn("rm:1", main.load_seen())

    def test_report_goes_only_to_whoever_asked(self):
        watch.Watcher(["rm"]).cycle()
        recipients = []
        with patch.object(main, "send_report", side_effect=lambda *a, **k: recipients.append(
                users.current_chat())) as report:
            watch.Watcher(["rm"]).cycle("report", chat_id=FRIEND)
        self.assertEqual(report.call_count, 1)
        self.assertEqual(recipients, [FRIEND])

    def test_different_queries_preserve_both_peoples_capped_inventory(self):
        friend = users.get(FRIEND)
        users.save_params(friend, {**friend.params, "rent": 2000})
        queries = []

        def fetch():
            queries.append((config.MAX_RENT, config.TARGET_LAT, config.TARGET_LNG))
            return [home(1)] if config.MAX_RENT == 1600 else [home(2, 1500)]

        self.fetch.side_effect = fetch
        result = watch.Watcher(["rm"]).cycle()
        self.assertEqual(queries, [(1600, 51.5152, -0.1419), (2000, 51.5152, -0.1419)])
        self.assertEqual(result["people"], {OWNER: 1, "Bea": 1})
        self.assertEqual(set(main.load_seen()), {"rm:1"})
        with users.use(friend):
            self.assertEqual(set(main.load_seen()), {"rm:2"})

    def test_settings_changed_during_baseline_are_baselined_again_without_alert_flood(self):
        real = main._run

        def change_during_baseline(*args, **kwargs):
            if users.current_chat() == OWNER:
                users.save_params(users.owner(), {"rent": 2000})
                d1.query("DELETE FROM meta WHERE name = 'watch_initialized'")
            return real(*args, **kwargs)

        with patch.object(main, "_run", side_effect=change_during_baseline):
            watch.Watcher(["rm"]).cycle()
        watch.Watcher(["rm"]).cycle()
        self.assertEqual(self.sent, [])
        self.assertEqual(len([text for chat, text in self.texts if chat == OWNER]), 2)
        self.assertEqual(set(main.load_seen()), {"rm:1", "rm:2"})

    def test_legacy_owner_baseline_is_kept_on_upgrade(self):
        watch.Watcher(["rm"]).cycle()
        d1.query("DELETE FROM meta WHERE name = 'watch_settings'")
        watch.Watcher(["rm"]).cycle()
        self.assertEqual(len([text for chat, text in self.texts if chat == OWNER]), 1)
        self.assertEqual(self.sent, [])

    def test_queued_report_keeps_its_recipient_when_another_request_arrives(self):
        watcher = watch.Watcher(["rm"])
        self.assertTrue(watcher.request_run("report", OWNER))
        first = watcher.pending.get_nowait()
        self.assertTrue(watcher.request_run("report", FRIEND))
        self.assertEqual(first, ("report", OWNER))
        self.assertEqual(watcher.pending.get_nowait(), ("report", FRIEND))

    def test_a_failed_search_group_does_not_block_the_other_group(self):
        friend = users.get(FRIEND)
        users.save_params(friend, {**friend.params, "rent": 2000})

        def fetch():
            if config.MAX_RENT == 1600:
                raise RuntimeError("owner search failed")
            return [home(2, 1500)]

        self.fetch.side_effect = fetch
        with self.assertRaisesRegex(RuntimeError, "Every source failed"):
            watch.Watcher(["rm"]).cycle()
        self.assertIsNone(watch._meta("watch_initialized"))
        with users.use(friend):
            self.assertEqual(watch._meta("watch_initialized"), "1")

    def test_local_cli_uses_owner_settings_but_search_centre_stays_fixed(self):
        users.save_params(users.owner(), {
            "rent": 2000, "destination": {"name": "Bank", "lat": 51.513, "lng": -0.089}})
        queries = []

        def fetch():
            queries.append((config.MAX_RENT, config.TARGET_NAME))
            return [home(2, 1500)]

        self.fetch.side_effect = fetch
        result = main.run(["rm"], True, False, rentals=True)
        self.assertEqual(queries, [(2000, "Oxford Circus")])
        self.assertEqual(result["matches"], 1)
        self.assertEqual(config.TARGET_NAME, "Oxford Circus")

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
        self.handler.handle(message("/settings"))
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

    def test_budget_can_be_turned_off_and_settings_explain_rent_budget_and_bills(self):
        self.admit()
        self.handler.handle(message("/set budget 1800"))
        self.assertEqual(users.get(FRIEND).params, {"budget": 1800})
        for word in ("off", "default"):
            self.handler.handle(message("/set budget 1800"))
            self.handler.handle(message(f"/set budget {word}"))
            self.assertEqual(users.get(FRIEND).params, {}, word)
        self.handler.handle(message("/set rent 1500"))
        self.assertEqual(users.get(FRIEND).params, {"rent": 1500})
        self.handler.handle(message("/settings"))
        text = self.texts(FRIEND)[-1]
        self.assertIn("<b>rent</b> £1,500", text)
        self.assertIn("<b>budget</b> off <i>(default)</i>", text)
        self.assertIn("hard limit on the advertised monthly rent", text)
        self.assertIn("council tax", text)
        self.assertIn("/set budget off", text)
        self.assertIn("utilities", text)

    def test_changing_rent_or_budget_rebaselines_that_person(self):
        friend = self.admit()
        for command in ("/set rent 1500", "/set budget 1800", "/set budget off"):
            with users.use(friend):
                watch._put_meta("watch_initialized", "1")
            self.handler.handle(message(command))
            with users.use(friend):
                self.assertIsNone(watch._meta("watch_initialized"), command)

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

    def test_destination_service_failures_are_not_reported_as_unknown_places(self):
        with patch.object(bot.requests, "Session") as factory:
            response = factory.return_value.__enter__.return_value.get.return_value
            response.status_code = 503
            response.raise_for_status.side_effect = bot.requests.HTTPError("unavailable")
            with self.assertRaisesRegex(ValueError, "unavailable"):
                bot._locate("SW1A 1AA")

    def test_invalid_destination_coordinates_do_not_block_polling(self):
        self.admit()
        with patch.object(bot, "_locate", return_value=("Unknown", None, None)):
            self.handler.handle(message("/set destination Unknown"))
        self.assertIn("invalid coordinates", self.texts(FRIEND)[-1])
        self.assertEqual(users.get(FRIEND).params, {})

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
        self.client.reset_mock()
        self.handler.handle(message("/saved"))
        self.handler.handle(message("/start"))
        self.client.call.assert_not_called()
        self.assertIsNone(users.register("300", "Another person", "TheBeatifulest"))
        self.assertEqual([p.chat_id for p in users.everyone()], [OWNER])


if __name__ == "__main__":
    unittest.main()
