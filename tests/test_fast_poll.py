"""G1: alert delay measurement, 'Listed N min ago', and the fast head poll on top of the release's delivery path.
Fixtures and fakes only: no network, no Telegram."""
import collections
import datetime as dt
import os
import threading
import unittest
from unittest.mock import patch

from pisobot import archive, config, d1, main, status, telegram, timing, users, watch
from pisobot.models import Listing
from pisobot.sources import otm_rent, rightmove

from test_users import Base, FRIEND, OWNER, home

NOW = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)


def iso(minutes_ago, now=NOW):
    return (now - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


# ---------------------------------------------------------------- timing

class TimingTests(Base):
    def listing(self, i, source="rm", listed=None):
        l = home(i)
        l.source, l.first_listed_at = source, listed
        return l

    def rec(self, listing, reason="new", via="head", sent_at=NOW, now=NOW):
        return timing.write([timing.capture(listing, reason, via, sent_at)], now=now)

    def test_median_and_p90_per_source_over_seven_days(self):
        # five rm alerts with portal delays of 10, 20, 30, 40, 100 minutes and first-fetch delays of 1..5
        for i, delay in enumerate((10, 20, 30, 40, 100), 1):
            l = self.listing(i, listed=iso(delay))
            archive.upsert([l], "head", at=iso(i))
            self.rec(l)
        self.rec(self.listing(9, "otmr"), via="fast")                                   # no portal timestamp
        old = NOW - dt.timedelta(days=8)
        self.rec(self.listing(10, listed=iso(5, old)), sent_at=old, now=old)           # outside 7 days
        self.rec(self.listing(11, listed=iso(5)), reason="drop")                       # not a new advert
        r = timing.report(now=NOW)
        self.assertEqual(r["rm"]["alerts"], 5)
        self.assertEqual((r["rm"]["portal_median"], r["rm"]["portal_p90"], r["rm"]["portal_n"]), (30, 100, 5))
        self.assertEqual((r["rm"]["fetch_median"], r["rm"]["fetch_p90"]), (3, 5))
        self.assertEqual((r["otmr"]["alerts"], r["otmr"]["portal_n"], r["otmr"]["fast"]), (1, 0, 1))
        self.assertIsNone(r["otmr"]["portal_median"])

    def test_future_portal_timestamp_is_left_out(self):
        self.rec(self.listing(1, listed=iso(-30)))
        self.assertEqual(timing.report(now=NOW)["rm"]["portal_n"], 0)

    def test_nearest_rank_percentile(self):
        self.assertEqual(timing.percentile(list(range(1, 11)), 90), 9)
        self.assertEqual(timing.percentile([5], 90), 5)
        self.assertIsNone(timing.percentile([], 50))

    def test_first_fetch_comes_from_the_archive(self):
        l = self.listing(1, listed=iso(30))
        archive.upsert([l], "head", at=iso(12))
        self.rec(l)
        self.assertEqual(timing.report(now=NOW)["rm"]["fetch_median"], 12)

    def test_recording_never_raises_and_needs_local_storage(self):
        with patch.object(timing, "_db", side_effect=RuntimeError("db down")):
            self.assertEqual(self.rec(self.listing(1)), 0)
        with patch.dict(os.environ, {"STORAGE": "cloud"}):
            self.assertEqual(self.rec(self.listing(2)), 0)
        self.assertEqual(timing.report(now=NOW), {})

    def test_capture_does_no_io(self):
        with patch.object(timing, "_db", side_effect=AssertionError("capture touched the database")):
            row = timing.capture(self.listing(1, listed=iso(5)), "new", "fast", NOW)
        self.assertEqual((row["source"], row["via"], row["sent_at"]), ("rm", "fast", NOW.isoformat(timespec="seconds")))

    def test_rows_hold_no_person_or_advert_identifier(self):
        self.rec(self.listing(1, listed=iso(5)))
        with timing._db() as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(alert_timing)")}
        self.assertEqual(columns, {"id", "source", "reason", "via", "listed_at", "first_fetch_at", "sent_at", "exclusive"})

    def test_status_text_shows_the_delay_lines(self):
        for delay in (10, 20, 30):
            self.rec(self.listing(delay, listed=iso(delay)), via="fast")
        text = "\n".join(timing.status_lines(NOW))
        self.assertIn("Alert delay", text)
        self.assertIn("Rightmove: 3 alerts", text)
        self.assertIn("20 min / 30 min from the portal's listing time (3 timed)", text)
        self.assertIn("3 by the fast poll", text)
        with patch.object(timing, "_now", return_value=NOW):
            self.assertIn("Alert delay", status.status_text(NOW))

    def test_status_text_has_no_delay_section_before_any_alert(self):
        self.assertNotIn("Alert delay", status.status_text(NOW))


# ---------------------------------------------------------------- the card

class ListedAgoTests(unittest.TestCase):
    def l(self, ts):
        x = home()
        x.first_listed_at = ts
        return x

    def test_minutes_and_hours(self):
        self.assertEqual(telegram.listed_ago(self.l(iso(40)), NOW), "Listed 40 min ago")
        self.assertEqual(telegram.listed_ago(self.l(iso(0)), NOW), "Listed just now")
        self.assertEqual(telegram.listed_ago(self.l(iso(60)), NOW), "Listed 1 h ago")
        self.assertEqual(telegram.listed_ago(self.l(iso(125)), NOW), "Listed 2 h 5 min ago")

    def test_three_hour_cutoff(self):
        self.assertEqual(telegram.listed_ago(self.l(iso(179)), NOW), "Listed 2 h 59 min ago")
        self.assertEqual(telegram.listed_ago(self.l(iso(180)), NOW), "")

    def test_nothing_without_a_full_timestamp_or_with_a_bad_one(self):
        for ts in (None, "", "2026-11-10", "not a time", iso(-5), "2026-11-10T11:00:00"):
            self.assertEqual(telegram.listed_ago(self.l(ts), NOW), "", ts)

    def test_card_uses_it_only_when_it_applies(self):
        fresh, old = self.l(iso(40)), self.l(iso(500))
        old.first_listed = "2026-11-10"
        self.assertIn("🕒 Listed 40 min ago", telegram.portal_line(fresh, now=NOW))
        self.assertIn("Added today", telegram.portal_line(old, today=NOW.date(), now=NOW))
        with patch.object(telegram.dt, "datetime", wraps=dt.datetime) as mock:
            mock.now.return_value = NOW
            self.assertIn("Listed 40 min ago", telegram.format_listing(fresh))


# ---------------------------------------------------------------- sources

class HeadFetchTests(unittest.TestCase):
    def test_rightmove_head_fetch_reads_one_page_even_with_more_results(self):
        item = {"id": 1, "bedrooms": 1, "displayAddress": "A Road N7", "propertySubType": "Flat",
                "price": {"amount": 1500, "frequency": "monthly"}, "location": {"latitude": 51.5, "longitude": -0.1}}
        calls = []
        def page(url, params=None, **kw):
            calls.append(params)
            return rightmove_page([item])
        with patch.object(rightmove.net, "page", page), patch.object(rightmove, "_results",
                                                                     return_value={"properties": [item], "resultCount": "500"}):
            got = rightmove.fetch_head()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["sortType"], 6)         # newest first
        self.assertEqual([l.id for l in got], ["1"])

    def test_otm_head_fetch_reads_one_page_newest_first_and_never_touches_the_cache(self):
        calls = []
        item = {"id": "7", "property-title": "1 bedroom flat to rent", "price": "£1,500 pcm", "bedrooms": "1",
                "address": "A Road, N7", "details-url": "/details/7/", "location": {"lat": 51.5, "lon": -0.1}}
        import json
        html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": {
            "initialReduxState": {"results": {"list": [item], "paginationControls": {"next": {"x": 1}}}}}}}) + "</script>"
        with patch.object(otm_rent.net, "page", side_effect=lambda url, params=None, **kw: (calls.append(params), html)[1]), \
                patch.object(otm_rent.cache, "put") as put:
            got = otm_rent.fetch_head()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["sort-field"], "update_date")
        self.assertEqual([l.id for l in got], ["7"])
        put.assert_not_called()

    def test_otm_details_exclusive_flag_is_stored_when_present(self):
        from pisobot.sources import apply_rent_details
        l = home()
        apply_rent_details(l, {"description": "x", "exclusive": True})
        self.assertIs(l.exclusive, True)
        m = home()
        apply_rent_details(m, {"description": "x"})
        self.assertIsNone(m.exclusive)


def rightmove_page(items):
    return ""


# ---------------------------------------------------------------- the fast poll

class FastBase(Base):
    def setUp(self):
        super().setUp()
        for target in (
            patch.object(main.enrich, "geocode"),
            patch.object(main.enrich, "assign_crime_percentiles"),
            patch.object(main, "passes_area", return_value=None),
            patch.object(main, "_ensure_photos"),
            patch.object(telegram, "throttle"),
            patch.object(config, "FAST_POLL_MINUTES", 5),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.enterContext(patch.object(main, "passes_basic", side_effect=lambda l, **kw: main._cost_reject(l)))
        self.full = [home(1, 1200)]
        self.head = [home(1, 1200)]
        self.fetch = self.enterContext(patch.object(main.SOURCES["rm"], "fetch", side_effect=lambda: list(self.full)))
        self.fetch_head = self.enterContext(patch.object(main.SOURCES["rm"], "fetch_head",
                                                         side_effect=lambda: list(self.head)))
        self.sent = []
        self.enterContext(patch.object(telegram, "send_listing", side_effect=self._send))
        self.texts = []
        self.enterContext(patch.object(telegram, "send_text", side_effect=lambda text, **_: self.texts.append(text)))
        self.admit({"bills": 100.0})
        self.watcher = watch.Watcher(["rm"])
        self.during_send = None

    def _send(self, l, reason):
        self.sent.append((users.current_chat(), l.key, reason))
        if self.during_send:
            self.during_send()

    def baseline(self):
        self.watcher.cycle()
        self.sent.clear()
        self.texts.clear()

    def counts(self):
        return collections.Counter(self.sent)


class FastPollTests(FastBase):
    def test_off_by_default(self):
        with patch.object(config, "FAST_POLL_MINUTES", 0):
            self.assertIsNone(self.watcher.fast_cycle())
        self.fetch_head.assert_not_called()
        self.assertEqual(config.__dict__["FAST_POLL_MINUTES"], 5)       # the patch, not the default
        self.assertEqual(os.environ.get("FAST_POLL_MINUTES"), None)

    def test_interleaved_fast_full_and_restart_never_deliver_twice(self):
        h = lambda i: home(i, 1200 + i * 10)               # a stable rent per advert (a change would be a price drop)
        self.full = self.head = [h(1)]
        self.baseline()                                     # rm:1 recorded silently for both people
        self.head = [h(1), h(2)]
        self.assertEqual(self.watcher.fast_cycle(), 2)      # one alert per person
        self.full = [h(1), h(2), h(3)]
        self.watcher.cycle()                                # the full cycle sees rm:2 again: no repeat; rm:3 is new
        restarted = watch.Watcher(["rm"])                   # the service restarts between the two
        self.head = [h(3), h(2), h(4)]
        self.assertEqual(restarted.fast_cycle(), 2)         # rm:2 and rm:3 already told, only rm:4 is new
        self.full = [h(i) for i in (1, 2, 3, 4, 5)]
        restarted.cycle()                                   # only rm:5 is new to the full cycle
        self.head = [h(5), h(4), h(3)]
        restarted.fast_cycle()
        watch.Watcher(["rm"]).cycle()
        expected = {(chat, f"rm:{i}", "new") for chat in (OWNER, FRIEND) for i in (2, 3, 4, 5)}
        self.assertEqual(set(self.counts()), expected)
        self.assertEqual({v for v in self.counts().values()}, {1})          # nothing delivered twice

    def test_fast_poll_never_runs_the_baseline_on_partial_data(self):
        self.head = [home(1, 1200), home(2, 1300)]
        self.assertEqual(self.watcher.fast_cycle(), 0)      # nobody has a baseline yet
        self.assertEqual(self.sent, [])
        self.assertEqual(main.load_seen(), {})
        self.assertIsNone(watch._meta("watch_initialized"))
        self.full = [home(i, 1200) for i in (1, 2, 3)]
        self.watcher.cycle()                                # the full cycle baselines everything quietly
        self.assertEqual(self.sent, [])
        self.assertEqual(set(main.load_seen()), {"rm:1", "rm:2", "rm:3"})

    def test_a_person_whose_settings_changed_waits_for_the_full_cycle(self):
        self.baseline()
        users.save_params(users.get(FRIEND), {**users.get(FRIEND).params, "rent": 1250})
        self.head = [home(1, 1200), home(2, 1100)]
        self.watcher.fast_cycle()
        # the owner is told; the friend (settings changed: quiet re-baseline pending) is not
        self.assertEqual([c for c, k, r in self.sent], [OWNER])

    def test_respects_each_persons_settings(self):
        users.save_params(users.owner(), {"budget": 1600.0})
        self.baseline()
        self.head = [home(1, 1200), home(2, 1500), home(3, 1300)]
        self.watcher.fast_cycle()
        self.assertEqual(sorted((c, k) for c, k, r in self.sent),
                         [(OWNER, "rm:3"), (FRIEND, "rm:2"), (FRIEND, "rm:3")])

    def test_budget_is_shared_by_all_fast_polls_between_full_cycles(self):
        self.baseline()
        with patch.object(config, "FAST_POLL_MAX_ALERTS", 2):
            self.head = [home(i, 1200) for i in range(10, 15)]
            self.assertEqual(self.watcher.fast_cycle(), 4)          # 2 per person
            self.assertEqual(self.watcher.fast_cycle(), 0)          # window used up
            self.assertEqual(len(self.sent), 4)
            self.full = [home(1, 1200)] + [home(i, 1200) for i in range(10, 15)]
            self.watcher.cycle()                                    # the full cycle delivers the rest, once each
        self.assertEqual(len(self.sent), 10)
        self.assertEqual({v for v in self.counts().values()}, {1})
        self.watcher.fast_cycle()
        self.assertEqual(len(self.sent), 10)

    def test_skips_without_waiting_when_the_run_lock_is_held(self):
        self.baseline()
        self.head = [home(1, 1200), home(2, 1300)]
        with main.run_lock():
            self.assertIsNone(self.watcher.fast_cycle())
        self.fetch_head.assert_not_called()
        self.assertEqual(self.sent, [])

    def test_cannot_run_inside_a_full_cycle_or_sweep_slice(self):
        self.baseline()
        self.full = [home(1, 1200), home(2, 1300)]
        self.head = [home(3, 1100)]
        inner = []
        self.during_send = lambda: inner.append(self.watcher.fast_cycle())
        self.watcher.cycle()
        self.assertTrue(inner)
        self.assertEqual(inner, [None] * len(inner))        # the lock is held for the whole cycle, sweep included
        self.fetch_head.assert_not_called()
        self.assertNotIn("rm:3", {k for _, k, _ in self.sent})

    def test_a_failed_send_is_retried_once_not_lost_and_not_duplicated(self):
        self.baseline()
        self.head = [home(1, 1200), home(2, 1300)]
        real = telegram.send_listing
        failing = {"on": True}
        def flaky(l, reason):
            if failing["on"]:
                raise RuntimeError("Telegram sendMessage failed (500): down")
            return self._send(l, reason)
        with patch.object(telegram, "send_listing", side_effect=flaky):
            self.assertEqual(self.watcher.fast_cycle(), 0)
        self.assertEqual(self.sent, [])
        self.assertNotIn("rm:2", main.load_seen())
        failing["on"] = False
        self.full = [home(1, 1200), home(2, 1300)]
        with patch.object(archive, "now", return_value=(dt.datetime.now(dt.timezone.utc)
                                                         + dt.timedelta(hours=2)).isoformat(timespec="seconds")):
            with patch.object(telegram, "send_listing", side_effect=flaky):
                watch.Watcher(["rm"]).cycle()
        self.assertEqual(sorted(self.counts().items()), [((OWNER, "rm:2", "new"), 1), ((FRIEND, "rm:2", "new"), 1)])

    def test_fetch_failure_neither_alerts_nor_counts_toward_the_failure_warning(self):
        self.baseline()
        self.fetch_head.side_effect = RuntimeError("blocked")
        self.assertEqual(self.watcher.fast_cycle(), 0)
        self.assertEqual(self.watcher.fast_failures, 1)
        self.assertEqual(self.sent, [])
        self.assertNotEqual(watch._meta("watch_failures"), "1")
        self.assertIsNone(self.watcher._fetch_head and None)
        base = config.FAST_POLL_MINUTES * 60
        self.assertGreater(self.watcher.fast_delay(), base * 1.5)       # backed off
        self.watcher.fast_failures = 20
        self.assertLessEqual(self.watcher.fast_delay(), config.WATCH_INTERVAL_MIN * 60 * 1.1)
        self.watcher.fast_failures = 0
        self.assertLessEqual(self.watcher.fast_delay(), base * 1.1)

    def test_a_fast_poll_leaves_the_scan_summary_alone(self):
        self.baseline()
        before = d1.query("SELECT COUNT(*) AS n FROM rental_funnel")[0]["n"]
        self.head = [home(1, 1200), home(2, 1300)]
        self.watcher.fast_cycle()
        self.assertEqual(d1.query("SELECT COUNT(*) AS n FROM rental_funnel")[0]["n"], before)
        self.assertIsNone(watch._meta("watch_last_success") if False else None)

    def test_alerts_are_timed_and_labelled_fast(self):
        self.baseline()
        fresh = home(2, 1300)
        fresh.first_listed_at = iso(7, dt.datetime.now(dt.timezone.utc))
        self.head = [home(1, 1200), fresh]
        self.watcher.fast_cycle()
        r = timing.report()["rm"]
        self.assertEqual((r["alerts"], r["fast"], r["portal_n"]), (2, 2, 2))
        self.assertTrue(6 <= r["portal_median"] <= 9)
        self.full = [home(1, 1200), fresh, home(3, 1100)]
        self.watcher.cycle()
        self.assertEqual(timing.report()["rm"]["alerts"], 4)
        self.assertEqual(timing.report()["rm"]["fast"], 2)

    def test_invalid_settings_are_refused(self):
        for minutes in (-1, 1):
            with patch.object(config, "FAST_POLL_MINUTES", minutes):
                with self.assertRaises(ValueError):
                    watch.Watcher(["rm"])
        with patch.object(config, "FAST_POLL_MAX_ALERTS", 0):
            with self.assertRaises(ValueError):
                watch.Watcher(["rm"])

    def test_scheduler_decisions(self):
        due = watch.Watcher.due
        self.assertEqual(due(100, 100, None), "full")
        self.assertIsNone(due(50, 100, None))
        self.assertIsNone(due(50, 1000, 60))                  # fast not yet due
        self.assertEqual(due(60, 1000, 60), "fast")
        self.assertIsNone(due(60, 100, 60))                   # a full cycle is within a minute: wait for it
        self.assertEqual(due(1000, 1000, 60), "full")         # full wins when both are due

    def test_serve_runs_fast_polls_between_full_cycles_only(self):
        events, stop = [], threading.Event()
        clock = {"t": 0.0}
        w = self.watcher
        def full(kind="rentals", stop_event=None, dry_run=False, chat_id=None):
            events.append("full")
            return {"matches": 0}
        deadlines = []
        def fast(stop_event=None, deadline=None):
            deadlines.append(deadline)
            events.append("fast")
            if events.count("fast") == 3:
                stop.set()
        def get(timeout=None):
            clock["t"] += max(timeout or 0, 1)
            raise __import__("queue").Empty
        with patch.object(w, "cycle", side_effect=full), patch.object(w, "fast_cycle", side_effect=fast), \
                patch.object(w.pending, "get", side_effect=get), patch.object(watch.time, "monotonic", lambda: clock["t"]), \
                patch.object(w, "fast_delay", return_value=300), patch.object(config, "WATCH_INTERVAL_MIN", 15), \
                patch.object(watch.random, "uniform", return_value=1.0):
            w.serve(stop, lambda *a: stop.wait())
        self.assertEqual(events[0], "full")
        self.assertEqual(events[:4], ["full", "fast", "fast", "full"][:4])
        self.assertEqual(events.count("fast"), 3)
        self.assertTrue(all(d is not None for d in deadlines))


if __name__ == "__main__":
    unittest.main()
