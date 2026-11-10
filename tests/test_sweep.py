"""Deep sweep: price bands, resumption, limits, judging for each person, first-sweep baseline,
staged gates, gone alerts, /status and /whatif. Fixtures only: the fixture fails any live request."""
import itertools
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from requests import ConnectionError

try:
    from tests.test_archive import ArchiveBase, SCOPE, advert, at
except ImportError:      # run as `python -m unittest discover -s tests`
    from test_archive import ArchiveBase, SCOPE, advert, at

from pisobot import archive, bot, config, d1, enrich, main, prefs, status, sweep, telegram, users, watch
from pisobot.sources import otm_rent, rightmove

OWNER = "100"


class Market:
    """A synthetic portal: id -> rent. band_page pages it the way the real portals do."""

    def __init__(self, prices, source="rm", size=24, reachable=1008, total_known=True, fail=()):
        self.prices, self.source, self.size, self.reachable = dict(prices), source, size, reachable
        self.total_known, self.fail, self.calls = total_known, list(fail), []

    def band_page(self, low, high, page):
        self.calls.append((low, high, page))
        if self.fail:
            raise self.fail.pop(0)
        ids = sorted(i for i, rent in self.prices.items() if low <= rent <= high)
        start = page * self.size
        chunk = ids[start:start + self.size] if start < self.reachable else []
        return {"listings": [advert(i, self.prices[i], source=self.source) for i in chunk],
                "total": len(ids) if self.total_known else None,
                "more": start + self.size < (min(len(ids), self.reachable) if self.total_known else len(ids))}


def big_market():
    """1,800 adverts: 700 between 1500 and 1600 as observed, the rest either side."""
    prices = {}
    for n in range(600):
        prices[n + 1] = 1000 + n % 500
    for n in range(700):
        prices[1001 + n] = 1500 + n % 101
    for n in range(500):
        prices[2001 + n] = 1601 + n % 160
    return prices


def drive(source="rm", budget=10_000, clock=lambda: 0):
    return sweep.crawl(source, [users.owner()], budget, clock=clock)


class CrawlTests(ArchiveBase):
    def pager(self, market, module=rightmove):
        patcher = patch.object(module, "band_page", market.band_page)
        patcher.start()
        self.addCleanup(patcher.stop)
        return market

    def test_bands_halve_until_every_one_is_reachable_and_nothing_is_missed(self):
        market = self.pager(Market(big_market()))
        self.assertEqual(len(market.prices), 1800)
        finished = drive()
        row = archive.last_sweep("rm")
        self.assertEqual((finished, row["complete"], row["note"]), (row["id"], True, ""))
        self.assertEqual(archive.counts(), {"active": 1800})
        self.assertEqual(row["adverts_seen"], 1800)
        self.assertGreater(row["state"]["splits"], 0)
        self.assertLess(row["requests"], config.DEEP_SWEEP_MAX_REQUESTS)
        # a band is only paged beyond its first page when it fits what the portal serves
        for low, high, page in market.calls:
            if page > 0:
                count = sum(low <= rent <= high for rent in market.prices.values())
                self.assertLessEqual(count, rightmove.REACHABLE, (low, high))
        self.assertEqual(market.calls[0][:2], (1000, 1760))       # RENT_MIN .. 110% of the £1,600 cap
        self.assertEqual(row["scope"]["max_rent"], 1760)

    def test_the_sweep_resumes_across_cycles_in_short_slices(self):
        market = self.pager(Market(big_market()))
        ticks = itertools.count(0, 10)          # every clock reading is 10 s later
        per_call, finished, calls = [], None, 0
        while finished is None:
            before = len(market.calls)
            finished = drive(budget=25, clock=lambda: next(ticks))
            per_call.append(len(market.calls) - before)
            calls += 1
            self.assertLess(calls, 200)
        self.assertLessEqual(max(per_call), 3)                    # a slice never runs on
        self.assertGreater(calls, 20)
        self.assertEqual(archive.counts(), {"active": 1800})
        self.assertTrue(archive.last_sweep("rm")["complete"])
        self.assertIsNone(archive.running_sweep("rm"))

    def test_request_cap_ends_the_sweep_as_incomplete_and_marks_nothing_gone(self):
        archive.upsert([advert(9999, 1400)], "head")
        market = self.pager(Market(big_market()))
        with patch.object(config, "DEEP_SWEEP_MAX_REQUESTS", 5):
            drive()
        row = archive.last_sweep("rm")
        self.assertEqual((row["complete"], row["requests"]), (False, 5))
        self.assertIn("request limit", row["note"])
        self.assertEqual(len(market.calls), 5)
        self.assertEqual(archive.advert("rm:9999")["status"], "active")
        self.assertEqual(archive.advert("rm:9999")["missed"], 0)

    def test_failures_are_retried_then_the_sweep_is_closed_honestly(self):
        market = self.pager(Market({1: 1400}, fail=[ConnectionError("boom")] * 3))
        self.assertIsNone(drive(budget=0))
        self.assertIsNone(drive(budget=0))
        self.assertEqual(archive.running_sweep("rm")["state"]["errors"], 2)
        self.assertIsNotNone(drive(budget=0))
        row = archive.last_sweep("rm")
        self.assertEqual((row["complete"], row["requests"]), (False, 3))
        self.assertIn("3 consecutive request failures", row["note"])
        self.assertEqual(archive.counts(), {})

    def test_one_failure_does_not_stop_the_sweep(self):
        market = self.pager(Market({1: 1400, 2: 1500}, fail=[ConnectionError("blip")]))
        self.assertIsNone(drive(budget=0))
        finished = drive()
        self.assertIsNotNone(finished)
        row = archive.last_sweep("rm")
        self.assertEqual((row["complete"], archive.counts()), (True, {"active": 2}))

    def test_a_price_band_the_portal_cannot_page_is_reported_not_hidden(self):
        market = self.pager(Market({n: 1500 for n in range(1, 31)}))
        with patch.object(rightmove, "REACHABLE", 24):
            drive()
        row = archive.last_sweep("rm")
        self.assertFalse(row["complete"])
        self.assertIn("too large", row["note"])
        self.assertEqual(row["state"]["truncated"], 1)
        self.assertEqual(len(archive.pending("100", ["rm"], "s", 100)), 24)       # the reachable part is kept

    def test_onthemarket_without_a_total_splits_when_the_page_limit_is_reached(self):
        market = self.pager(Market({n: 1000 + n for n in range(200)}, source="otmr", size=30, total_known=False),
                            otm_rent)
        with patch.object(otm_rent, "REACHABLE", 90):
            drive("otmr")
        row = archive.last_sweep("otmr")
        self.assertTrue(row["complete"])
        self.assertEqual(archive.counts(), {"active": 200})
        self.assertGreater(row["state"]["splits"], 0)

    def test_a_sweep_is_due_only_every_deep_sweep_hours(self):
        self.assertTrue(sweep.due("rm"))
        archive.start_sweep("rm", SCOPE, {}, at(0))
        self.assertFalse(sweep.due("rm", at=at(11)))
        self.assertTrue(sweep.due("rm", at=at(12)))
        self.assertTrue(sweep.due("otmr", at=at(0)))
        self.assertFalse(sweep.due("rm", hours=48, at=at(47)))

    def test_scope_is_the_widest_of_everyones_searches(self):
        a = users.User(OWNER, owner=True, params={"rent": 1600, "minrent": 900})
        b = users.User("200", params={"rent": 1400, "beds": 2, "minrent": 1000})
        scope = sweep.scope_for([a, b])
        self.assertEqual((scope["min_rent"], scope["max_rent"], scope["min_beds"], scope["miles"]), (900, 1760, 1, 10.0))
        self.assertEqual(sweep.scope_for([b])["max_rent"], 1540)
        self.assertEqual(sweep.scope_for([b])["min_beds"], 2)

    def test_disabled_when_hours_are_zero_or_no_swept_source_is_watched(self):
        self.assertTrue(sweep.enabled(["rm", "orent"]))
        self.assertFalse(sweep.enabled(["orent", "sr"]))
        with patch.object(config, "DEEP_SWEEP_HOURS", 0):
            self.assertFalse(sweep.enabled(["rm"]))
        with patch.dict("os.environ", {"STORAGE": ""}):
            self.assertFalse(sweep.enabled(["rm"]))


class JudgingBase(ArchiveBase):
    """The pipeline pieces that would otherwise reach TfL, the portals or Telegram are replaced."""

    def setUp(self):
        super().setUp()
        tmp = self.tmp.name
        self.alerts, self.texts = [], []
        for target in (
            patch.object(main, "STATE_DIR", tmp),
            patch.object(main, "SEEN_PATH", str(Path(tmp) / "seen.json")),
            patch.object(enrich, "journey", return_value={"minutes": 25, "route": "Tube", "station": "Oxford Circus",
                                                          "walk": 5, "changes": 0}),
            patch.object(enrich, "crime_score", return_value=30),
            patch.object(enrich, "assign_crime_percentiles"),
            patch.object(enrich, "geocode"),
            patch.object(rightmove, "refine", return_value=True),
            patch.object(main, "_ensure_photos"),
            patch.object(telegram, "throttle"),
            patch.object(telegram, "send_listing", side_effect=lambda l, reason: self.alerts.append((l.key, reason))),
            patch.object(telegram, "send_text", side_effect=lambda text, **kw: self.texts.append(text)),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.owner = users.owner()

    def archive_sweep(self, listings, hour=0, complete=True):
        sid = archive.start_sweep("rm", SCOPE, {}, at(hour))
        archive.upsert(listings, "sweep", sid, at(hour))
        if complete:
            archive.finish_sweep(sid, True, "", at(hour + 0.5))
        return sid

    def judge(self, person=None, budget=1000):
        return sweep.judge(person or self.owner, ["rm"], budget, clock=lambda: 0)

    def seen(self):
        return set(main.load_seen())


class Judging(JudgingBase):

    def test_first_completed_sweep_records_matches_silently_then_sends_one_summary(self):
        self.archive_sweep([advert(1, 1200), advert(2, 1300), advert(3, 1400), advert(4, 1700)])
        finished = self.judge()
        self.assertEqual(finished, {"rm": 3})                   # rm:4 is over the £1,600 rent cap
        self.assertEqual(self.alerts, [])
        self.assertEqual(self.texts, [])                        # judge itself sends nothing
        self.assertEqual(self.seen(), {"rm:1", "rm:2", "rm:3"})
        sweep.summarise(self.owner, finished)
        self.assertEqual(len(self.texts), 1)
        self.assertIn("3 more existing matches", self.texts[0])
        self.assertIn("/report", self.texts[0])
        self.assertEqual(self.judge(), {})                      # nothing left to judge, baseline stays set
        sweep.summarise(self.owner, {})
        self.assertEqual(len(self.texts), 1)

    def test_later_sweeps_alert_for_new_matches_within_the_alert_cap(self):
        self.archive_sweep([advert(1, 1200)])
        self.judge()
        self.archive_sweep([advert(n, 1300) for n in range(10, 15)], hour=12)
        with patch.object(config, "MAX_ALERTS_PER_RUN", 2):
            self.judge()
        self.assertEqual(len(self.alerts), 2)
        self.assertTrue(all(reason == "new" for _, reason in self.alerts))
        first = {key for key, _ in self.alerts}
        with patch.object(config, "MAX_ALERTS_PER_RUN", 2):
            self.judge()                                       # the three unsent matches are judged again
        self.assertEqual(len(self.alerts), 4)
        self.assertTrue(first.isdisjoint({key for key, _ in self.alerts[2:]}))
        with patch.object(config, "MAX_ALERTS_PER_RUN", 2):
            self.judge()
        self.assertEqual(len(self.alerts), 5)
        self.judge()
        self.assertEqual(len(self.alerts), 5)                  # everything alerted exactly once

    def test_adverts_the_head_poll_already_alerted_do_not_alert_again(self):
        listing = advert(1, 1200)
        main._run(["rm"], False, False, rentals=True, watch=True, baseline=False, prefetched=([listing], []))
        self.assertEqual(self.alerts, [("rm:1", "new")])
        self.archive_sweep([advert(1, 1200)])
        self.judge()
        self.judge()
        self.assertEqual(self.alerts, [("rm:1", "new")])

    def test_an_unfinished_first_sweep_stays_quiet_and_counts_until_it_completes(self):
        sid = self.archive_sweep([advert(1, 1200), advert(2, 1300)], complete=False)
        self.assertEqual(self.judge(), {})
        self.assertEqual((self.alerts, self.seen()), ([], {"rm:1", "rm:2"}))
        archive.upsert([advert(3, 1400)], "sweep", sid, at(1))
        self.assertEqual(self.judge(), {})
        self.assertEqual(self.alerts, [])
        archive.finish_sweep(sid, True, "", at(2))
        self.assertEqual(self.judge(), {"rm": 3})
        self.assertEqual(self.alerts, [])

    def test_a_settings_change_rebaselines_without_alerts(self):
        self.archive_sweep([advert(1, 1200), advert(2, 1550)])
        self.judge()
        users.save_params(users.owner(), {"rent": 1500.0})
        person = users.owner()
        self.assertEqual(self.judge(person), {"rm": 0})        # rm:2 is now over the cap: no new matches
        self.assertEqual(self.alerts, [])
        users.save_params(users.owner(), {"rent": 1600.0, "budget": 1500.0})
        self.assertEqual(self.judge(users.owner()), {"rm": 0})
        self.assertEqual(self.alerts, [])
        outcomes = {r["key"]: r["outcome"] for r in archive.verdicts_since(OWNER, 1)}
        self.assertEqual(outcomes, {"rm:1": "matched", "rm:2": "all_in_cap"})

    def test_two_people_are_judged_separately_with_their_own_limits(self):
        friend = users.User("200", name="Bea", params={"rent": 1300.0})
        self.archive_sweep([advert(1, 1200), advert(2, 1500)])
        self.assertEqual(self.judge(), {"rm": 2})
        self.assertEqual(sweep.judge(friend, ["rm"], 1000, clock=lambda: 0), {"rm": 1})
        by_person = {(r["key"], r["outcome"]) for r in archive.verdicts_since("200", 1)}
        self.assertEqual(by_person, {("rm:1", "matched"), ("rm:2", "rent_cap")})
        self.assertEqual({r["outcome"] for r in archive.verdicts_since(OWNER, 1)}, {"matched"})

    def test_a_persons_radius_is_not_a_filter_for_sweep_adverts_but_the_commute_is(self):
        far = advert(1, 1400, lat=51.5152 + 0.13)         # about 14 km from Oxford Circus
        with users.criteria({"radius": 1}):
            self.assertEqual(main.passes_basic(far, stage="cheap"), "too far")
            self.assertIsNone(main.passes_basic(far, sweep=True, stage="cheap"))
        beyond = advert(2, 1400, lat=51.5152 + 0.2)       # outside the 10-mile sweep
        self.assertEqual(main.passes_basic(beyond, sweep=True, stage="cheap"), "too far")
        with patch.object(enrich, "journey", return_value={"minutes": 55, "route": "x", "station": "y", "walk": 3}):
            self.assertEqual(main.passes_area(far), "travel")

    def test_coordinate_checks_run_before_any_details_request(self):
        refine = Mock(return_value=True)
        slow = {"minutes": 55, "route": "x", "station": "S", "walk": 3}
        with patch.object(rightmove, "refine", refine):
            with patch.object(enrich, "journey", return_value=slow):
                main._run(["rm"], False, False, rentals=True, watch=True, baseline=True,
                          prefetched=([advert(1, 1400)], []))
            refine.assert_not_called()
            [row] = archive.verdicts_since(OWNER, 1)
            self.assertEqual((row["outcome"], row["travel_minutes"], row["rent"]), ("travel", 55, 1400))
            main._run(["rm"], False, False, rentals=True, watch=True, baseline=True,
                      prefetched=([advert(2, 1400)], []))
            refine.assert_called_once()
        rows = {r["key"]: r for r in archive.verdicts_since(OWNER, 1)}
        self.assertEqual((rows["rm:2"]["outcome"], rows["rm:2"]["travel_minutes"], rows["rm:2"]["crime_score"]),
                         ("matched", 25, 30))
        self.assertGreater(rows["rm:2"]["all_in"], 1400)

    def test_crime_rejections_keep_the_score_they_were_judged_on(self):
        with patch.object(enrich, "crime_score", return_value=140):
            main._run(["rm"], False, False, rentals=True, watch=True, baseline=True, prefetched=([advert(1, 1400)], []))
        [row] = archive.verdicts_since(OWNER, 1)
        self.assertEqual((row["outcome"], row["crime_score"], row["travel_minutes"]), ("crime", 140, 25))

    def test_the_zone_hook_can_reject_before_the_tfl_call(self):
        with patch.object(main, "zone_prefilter", return_value=False), patch.object(enrich, "journey") as journey:
            self.assertEqual(main.passes_area(advert(1, 1400)), "travel")
        journey.assert_not_called()
        self.assertTrue(main.zone_prefilter(advert(1)))        # no zone module yet: everything goes on to TfL

    def test_only_local_private_rentals_are_staged(self):
        self.assertTrue(main.staged(advert(1)))
        sale = advert(2)
        sale.tenure = "sale"
        self.assertFalse(main.staged(sale))
        scheme = advert(3)
        scheme.bills = None
        self.assertFalse(main.staged(scheme))
        with patch.dict("os.environ", {"STORAGE": ""}):
            self.assertFalse(main.staged(advert(4)))

    def test_unstaged_gate_order_is_unchanged(self):
        refine = Mock(return_value=True)
        with patch.object(rightmove, "refine", refine), patch.dict("os.environ", {"STORAGE": ""}):
            self.assertIsNone(main.passes_basic(advert(1, 1400)))
        refine.assert_called_once()
        self.assertEqual(main.passes_basic(advert(2, 1700)), "rent_cap")
        refine.assert_called_once()                                # still the cheap rent gate before details

    def test_enrichment_failures_are_retried_next_time_not_baselined_away(self):
        self.archive_sweep([advert(1, 1200)])
        with patch.object(enrich, "journey", side_effect=ConnectionError("tfl down")):
            self.assertEqual(self.judge(), {"rm": 0})            # drained after one try, nothing matched
        self.assertEqual(archive.verdicts_since(OWNER, 1)[0]["outcome"], "enrichment")
        self.assertEqual(self.judge(), {})
        self.assertEqual(archive.verdicts_since(OWNER, 1)[0]["outcome"], "matched")


class GoneNotifications(JudgingBase):
    def test_a_saved_home_that_goes_gone_triggers_the_existing_message_once(self):
        market = Market({n: 1400 + n for n in range(1, 6)})
        clock = itertools.count(0, 60)       # sweeps are minutes apart, as timestamps have one-second resolution
        self.enterContext(patch.object(archive, "now", side_effect=lambda: at(next(clock) / 3600)))
        d1.query("INSERT INTO saved (key, info) VALUES (?, ?)", "rm:2",
                 json.dumps({"url": "https://example.com/2", "address": "Home 2 & Co", "cost": 1700}))
        with patch.object(rightmove, "band_page", market.band_page), patch.object(sweep, "due", return_value=True):
            drive()
            del market.prices[2]
            drive()
            self.assertEqual(self.texts, [])
            drive()
        gone = [t for t in self.texts if "no longer listed" in t]
        self.assertEqual(len(gone), 1)
        self.assertIn("Home 2 &amp; Co", gone[0])
        self.assertEqual(archive.advert("rm:2")["status"], "gone")
        self.assertTrue(json.loads(d1.query("SELECT info FROM saved WHERE key = 'rm:2'")[0]["info"])["gone"])
        with patch.object(rightmove, "band_page", market.band_page), patch.object(sweep, "due", return_value=True):
            drive()
        self.assertEqual(len([t for t in self.texts if "no longer listed" in t]), 1)


class WatcherIntegration(JudgingBase):
    def test_a_cycle_archives_the_head_poll_runs_a_sweep_slice_and_audits_recall(self):
        head = [advert(n, 1200 + n) for n in (1, 2, 3)]
        market = Market({n: 1200 + n for n in range(1, 11)})
        with patch.object(rightmove, "fetch", return_value=head), patch.object(rightmove, "band_page", market.band_page):
            result = watch.Watcher(["rm"]).cycle()
        self.assertEqual(result["matches"], 3)
        self.assertEqual(archive.counts(), {"active": 10})
        self.assertEqual(sum(archive.advert(f"rm:{n}")["head_seen"] for n in range(1, 11)), 3)
        row = archive.last_sweep("rm")
        self.assertEqual((row["complete"], row["adverts_seen"], row["unseen_by_head"]), (True, 10, 7))
        self.assertAlmostEqual(archive.recall(row), 0.3)
        self.assertEqual(len(archive.verdicts_since(OWNER, 1)), 10)      # head poll and sweep, one person
        self.assertEqual(self.alerts, [])                                # the first sweep does not flood
        self.assertEqual(len([t for t in self.texts if "deep sweep" in t]), 1)
        self.assertIn("7 more existing matches", [t for t in self.texts if "deep sweep" in t][0])
        # the next cycle finds a brand-new advert only the sweep sees: it alerts
        market.prices[11] = 1250
        with patch.object(rightmove, "fetch", return_value=head), patch.object(rightmove, "band_page", market.band_page), \
                patch.object(sweep, "due", return_value=True):
            watch.Watcher(["rm"]).cycle()
        self.assertEqual(self.alerts, [("rm:11", "new")])

    def test_dry_runs_and_disabled_sweeps_make_no_sweep(self):
        head = [advert(1, 1200)]
        market = Market({1: 1200})
        with patch.object(rightmove, "fetch", return_value=head), patch.object(rightmove, "band_page", market.band_page):
            watch.Watcher(["rm"]).cycle(dry_run=True)
            with patch.object(config, "DEEP_SWEEP_HOURS", 0):
                watch.Watcher(["rm"]).cycle()
        self.assertEqual(market.calls, [])
        self.assertIsNone(archive.last_sweep("rm"))
        self.assertEqual(archive.counts(), {"active": 1})               # the real cycle still archived the head poll

    def test_report_includes_matches_only_the_sweep_found(self):
        self.archive_sweep([advert(5, 1300)])
        self.judge()
        shown = {}

        def capture(selected, dry_run, verbose, **kw):
            shown["keys"] = {l.key for l in kw["prefetched"][0]}
            return {"matches": 0, "complete": True, "failures": [], "enrich_failures": 0, "fresh": 0}

        d1.query("INSERT INTO meta (name, value) VALUES ('watch_initialized', '1')")
        d1.query("INSERT INTO meta (name, value) VALUES ('watch_settings', '{}')")
        with patch.object(main, "_run", side_effect=capture):
            watch.Watcher(["rm"])._person_cycle("report", ([advert(1, 1200)], []), None, False)
        self.assertEqual(shown["keys"], {"rm:1", "rm:5"})


class Commands(JudgingBase):
    def setUp(self):
        super().setUp()
        self.client = Mock()
        self.handler = bot.Handler(OWNER, Mock(return_value=True), self.client)

    def send(self, text):
        self.handler.handle({"message": {"chat": {"id": int(OWNER), "type": "private"}, "text": text,
                                         "from": {"id": int(OWNER), "username": "quique", "first_name": "Q"}}})
        return [c.kwargs["text"] for c in self.client.call.call_args_list if c.args[0] == "sendMessage"][-1]

    def test_whatif_counts_one_limit_at_a_time_and_says_how_rough_they_are(self):
        rows = [("rm:1", "rent_cap", 1640, None, None, None), ("rm:2", "rent_cap", 1690, None, None, None),
                ("rm:3", "rent_cap", 1800, None, None, None), ("rm:4", "travel", 1400, None, 44, None),
                ("rm:5", "travel", 1400, None, 49, None), ("rm:6", "travel", 1400, None, 60, None),
                ("rm:7", "crime", 1400, None, 20, 140), ("rm:8", "crime", 1400, None, 20, 160),
                ("rm:9", "matched", 1400, 1700, 20, 30), ("rm:10", "keywords", 1400, None, None, None)]
        archive.record_verdicts(OWNER, rows, "s")
        n = status.whatif_counts(archive.verdicts_since(OWNER, 7), 1600, 40, 120)
        self.assertEqual((n["judged"], n["matched"], n["rent"], n["commute"], n["crime"]),
                         (10, 1, {50: 1, 100: 2}, {5: 1, 10: 2}, 1))
        text = self.send("/whatif")
        for line in ("Rent cap £1,650 (+£50): <b>+1</b>", "Rent cap £1,700 (+£100): <b>+2</b>",
                     "Commute 45 min (+5): <b>+1</b>", "Commute 50 min (+10): <b>+2</b>",
                     "Crime limit 150 (+25%): <b>+1</b>", "10 adverts judged", "lower bounds", "first failing"):
            self.assertIn(line, text)

    def test_whatif_with_nothing_judged_says_so(self):
        self.assertIn("Nothing has been judged", self.send("/whatif"))

    def test_whatif_is_per_person(self):
        archive.record_verdicts("200", [("rm:1", "rent_cap", 1640, None, None, None)], "s")
        self.assertIn("Nothing has been judged", self.send("/whatif"))

    def test_status_reports_scan_sources_sweep_and_recall(self):
        head = [advert(n, 1200 + n) for n in (1, 2)]
        market = Market({n: 1200 + n for n in range(1, 5)})
        with patch.object(rightmove, "fetch", return_value=head), patch.object(rightmove, "band_page", market.band_page):
            watch.Watcher(["rm"]).cycle()
        text = self.send("/status")
        for part in ("PisoBot status", "Last complete scan", "rm: 2 fetched", "Deep sweep", "Rightmove: complete",
                     "4 adverts", "2 the regular scan had not shown", "recall: <b>50%</b>", "OnTheMarket: not run yet",
                     "4 active adverts"):
            self.assertIn(part, text)

    def test_status_shows_a_running_sweep_an_incomplete_one_and_a_failed_source(self):
        sid = archive.start_sweep("rm", SCOPE, {"queue": [[1000, 1380], [1381, 1760]], "current": None}, at(0))
        archive.save_sweep(sid, {"queue": [[1381, 1760]], "current": None}, 12)
        text = status.status_text()
        self.assertIn("running, 12/150 requests, 1 price band(s) queued", text)
        archive.finish_sweep(sid, False, "stopped at the 150-request limit", at(1))
        self.assertIn("incomplete (stopped at the 150-request limit)", status.status_text())
        d1.query("INSERT INTO rental_funnel (created_at, stats) VALUES (?, ?)", at(2), json.dumps(
            {"sources": {"rm": {"fetched": 0, "matched": 0}, "otmr": {"fetched": 30, "matched": 4}},
             "source_failures": ["rm"]}))
        text = status.status_text()
        self.assertIn("rm: ⚠️ failed", text)
        self.assertIn("otmr: 30 fetched · 4 matched", text)
        with patch.object(config, "DEEP_SWEEP_HOURS", 0):
            self.assertIn("off (DEEP_SWEEP_HOURS=0)", status.status_text())

    def test_commands_are_listed_in_help(self):
        self.assertIn("/status", bot.HELP)
        self.assertIn("/whatif", bot.HELP)


if __name__ == "__main__":
    unittest.main()
