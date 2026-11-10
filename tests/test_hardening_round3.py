"""Round-3 regressions ported from the round-2 reviewer reproduction file.

The migration test had contradictory [] and [new] assertions. Its no-flood check
now excludes only previously seen keys, and a live upgrade sends no baseline message.

Offline only: every portal / TfL / Telegram / healthcheck call is stubbed by the repository's own
JudgingBase fixture. Each test asserts the CORRECT behaviour, so a FAIL means the defect reproduced.
Run from the worktree root:

  cd /Users/quique/repo/PisoBot-hreview && PYTHONDONTWRITEBYTECODE=1 \
    /Users/quique/repo/PisoBot/.venv/bin/python <scratchpad>/wp2-round2-repros.py -v

Tests marked CONTROL pass on correct behaviour and document what was checked and found sound.
The same file also runs against dee641e (base of the PR) for regression comparison; tests that need
round-2 internals skip themselves there.
"""
import io
import os
import sqlite3
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = os.getcwd()
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

from test_sweep import JudgingBase          # noqa: E402  (repository fixture: no live I/O)
from test_archive import advert, at         # noqa: E402
from pisobot import archive, config, d1, enrich, main, net, sweep, telegram, users, watch  # noqa: E402
from pisobot.sources import rightmove       # noqa: E402

ROUND2 = hasattr(archive, "begin_lookup")
HEAD = [advert(1, 1200), advert(2, 1210), advert(3, 1220)]
NEW = [advert(4, 1230), advert(5, 1240), advert(6, 1250)]


def failing_details(keys):
    """Rightmove details unavailable for these keys (portal 429 cool-down, 5xx, bot page...)."""
    def refine(l):
        if l.key in keys:
            l.detail_unavailable = True
            return False
        return True
    return refine


class Repro(JudgingBase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch("sys.stdout", new=io.StringIO()))
        self.enterContext(patch("sys.stderr", new=io.StringIO()))
        # The deep-sweep crawl is stubbed (no portal request is possible)
        self.crawl = self.enterContext(patch.object(sweep, "crawl"))

    def cycle(self, head, patches=None, moment=None):
        targets = [patch.object(rightmove, "fetch", return_value=list(head))]
        targets += [patch.object(obj, name, value) for (obj, name), value in (patches or {}).items()]
        if moment is not None:
            targets.append(patch.object(archive, "now", return_value=moment))
        for t in targets:
            t.start()
        try:
            watch.Watcher(["rm"]).cycle()
            return None
        except Exception as ex:
            return ex
        finally:
            for t in reversed(targets):
                t.stop()

    def job(self, key, chat="100"):
        if not ROUND2:
            return None
        with archive._db() as db:
            rows = archive._rows(db, "SELECT * FROM advert_work WHERE chat_id=? AND key=?", chat, key)
        return rows[0] if rows else None

    def verdicts(self):
        return {r["key"]: r["outcome"] for r in archive.verdicts_since("100", 3650)}


class R1_Outages(Repro):
    """A short outage of details/TfL turns a live new home into a terminal 'failed' obligation."""

    def setUp(self):
        super().setUp()
        self.archive_sweep(HEAD)                       # steady state: baseline done, sweep generation complete
        self.assertIsNone(self.cycle(HEAD, moment=at(-1)))
        self.assertEqual(self.alerts, [])

    def evidence(self):
        j = self.job("rm:4") if ROUND2 else None
        return (f"alerts={self.alerts} job={j and (j['state'], j['failures'], bool(j['notify']))} "
                f"verdict={self.verdicts().get('rm:4')} seen_has_rm4={'rm:4' in self.seen()}")

    def test_R1a_thirty_minute_details_outage_permanently_loses_a_new_home(self):
        bad = {(rightmove, "refine"): failing_details({"rm:4"})}      # e.g. Rightmove 429 cool-down
        for quarter in range(3):                                      # 3 cycles, 15 minutes apart
            self.cycle(HEAD + NEW[:1], bad, moment=at(quarter * 0.25))
        after_outage = self.evidence()
        for hour in (1, 2, 4, 8, 24, 48):                             # portal healthy again
            self.cycle(HEAD + NEW[:1], moment=at(hour))
        self.assertEqual(self.alerts, [("rm:4", "new")],
                         f"\n after the outage: {after_outage}\n after 6 healthy cycles over 48 h: {self.evidence()}")

    def test_R1b_tfl_outage_is_invisible_and_permanently_loses_new_homes(self):
        down = {(enrich, "journey"): Mock(side_effect=ConnectionError("TfL 503"))}
        ping = Mock(return_value=Mock(raise_for_status=Mock()))
        results = []
        with patch.dict(os.environ, {"HEALTHCHECK_URL": "https://hc.invalid/ping"}), \
                patch.object(net.session, "get", ping):
            for quarter in range(3):
                results.append(self.cycle(HEAD + NEW[:2], down, moment=at(quarter * 0.25)))
            outage = (f"cycles raised={[type(r).__name__ for r in results]} pings={ping.call_count} "
                      f"owner warnings={[t for t in self.texts if 'incomplete' in t]}")
            after = self.evidence()
            for hour in (1, 2, 4, 8, 24):
                self.cycle(HEAD + NEW[:2], moment=at(hour))
        self.assertEqual({k for k, _ in self.alerts}, {"rm:4", "rm:5"},
                         f"\n during the outage: {outage}\n after outage: {after}\n after recovery: {self.evidence()}")


class R2_UnsatisfiableIntents(Repro):
    """Live intents that can never be satisfied are never retired: re-judged every cycle, forever."""

    def setUp(self):
        super().setUp()
        if not ROUND2:
            self.skipTest("round-2 ledger not present")
        self.archive_sweep(HEAD)
        self.assertIsNone(self.cycle(HEAD, moment=at(-1)))

    def test_R2a_a_new_home_in_a_hidden_building_keeps_a_live_intent_and_is_rejudged_every_cycle(self):
        """The person pressed 🔕 Hide building on a card earlier; a NEW advert then appears in that building."""
        d1.query("INSERT INTO hidden_dev (lat, lng) VALUES (?, ?)", 51.5300, -0.1400)
        tower = advert(4, 1230, lat=51.5301, lng=-0.1401)
        looks = []

        def refine(l):
            if l.key == "rm:4":
                looks.append(l.key)
            return True
        for quarter in range(8):                                        # two hours of cycles
            self.cycle(HEAD + [tower], {(rightmove, "refine"): refine}, moment=at(quarter * 0.25))
        j = self.job("rm:4")
        pend = [l.key for l in archive.pending("100", ["rm"], main.settings_key(self.owner), 25)]
        self.assertEqual(self.alerts, [])
        self.assertFalse(j["notify"] and j["state"] == "pending",
                         f"\n a home in a hidden building is still a live pending obligation: job={j['state'], bool(j['notify'])};"
                         f" lookups of rm:4 in 8 cycles={len(looks)}; archive.pending() offers {pend}")

    def test_R2b_a_superseded_price_drop_intent_is_never_retired(self):
        with patch.object(config, "MAX_ALERTS_PER_RUN", 0):                # the cap defers the drop alert
            self.cycle([advert(1, 1150), HEAD[1], HEAD[2]], moment=at(0))
        self.assertTrue(self.job("rm:1")["notify"])
        for quarter in range(1, 9):                                       # price reverts; allowance is back
            self.cycle(HEAD, moment=at(quarter * 0.25))
        j = self.job("rm:1")
        self.assertEqual(self.alerts, [])
        self.assertFalse(j["notify"] and j["state"] == "pending",
                         f"\n the superseded drop is still a live obligation: {j['state'], bool(j['notify'])}"
                         f" verdict={self.verdicts().get('rm:1')}")

    def test_R2c_stuck_intents_sort_ahead_of_new_inventory_and_can_fill_every_batch(self):
        """26 hidden new homes (e.g. a hidden building) outrank a brand-new sweep-only home forever."""
        stuck = [advert(100 + i, 1300 + i) for i in range(26)]
        for a in stuck:
            d1.query("INSERT INTO hidden (key) VALUES (?)", a.key)
        self.cycle(HEAD + stuck, moment=at(0))
        self.archive_sweep([advert(200, 1400)], hour=1)                   # a new home only the sweep has seen
        settings = main.settings_key(self.owner)
        first_batch = [l.key for l in archive.pending("100", ["rm"], settings, 25, at=at(1.5))]
        everything = [l.key for l in archive.pending("100", ["rm"], settings, 500, at=at(1.5))]
        self.assertIn("rm:200", first_batch,
                      f"\n the new home is at position {everything.index('rm:200') + 1} of {len(everything)};"
                      f" a judge batch is 25 and the first 25 are {sorted(set(first_batch) & {a.key for a in stuck}) == sorted(first_batch)}"
                      f" all stuck hidden homes")


class R3_Migration(Repro):
    def test_R3a_upgrade_from_the_live_schema_absorbs_homes_listed_since_the_last_cycle(self):
        """CONTROL for flood, DEFECT for loss: state as the live wp0 system leaves it, then first start."""
        # live system: seen.json authoritative, watch_initialized=1, watch_settings = the old params JSON
        main.save_seen({"rm:1": {"alerted": True, "cost": 1480, "first": "2026-11-09T10:00", "last": "2026-11-09",
                                 "url": "u", "beds": 1, "price": 1200, "rent": 1200, "lat": 51.52, "lng": -0.14,
                                 "address": "Home 1"},
                        "rm:2": {"alerted": True, "cost": 1490, "first": "2026-11-09T10:00", "last": "2026-11-09",
                                 "url": "u", "beds": 1, "price": 1210, "rent": 1210, "lat": 51.52, "lng": -0.14,
                                 "address": "Home 2"}})
        d1.query("INSERT INTO meta (name, value) VALUES ('watch_initialized', '1')")
        d1.query("INSERT INTO meta (name, value) VALUES ('watch_settings', '{}')")
        with archive._db() as db:       # no archive tables at all, as on the live VPS
            for table in ("adverts", "observations", "verdicts", "sweeps", "advert_work", "unseen_events",
                          "unseen_deliveries"):
                db.execute(f"DROP TABLE IF EXISTS {table}")
        # first cycle of the new code: rm:3 is a genuinely NEW home, listed since the previous live cycle
        self.assertIsNone(self.cycle([HEAD[0], HEAD[1], HEAD[2]]))
        self.assertFalse({key for key, _ in self.alerts} & {"rm:1", "rm:2"},
                         "Previously alerted homes must not repeat on upgrade")
        live_msgs = [t for t in self.texts if "Rental watch is live" in t]
        self.assertEqual(len(live_msgs), 0)
        self.assertEqual(self.alerts, [("rm:3", "new")],
                         f"\n rm:3 was never in seen.json (a new home) but the upgrade baseline recorded it silently:"
                         f" seen={sorted(self.seen())} message={live_msgs}")

    def test_R3b_round1_rows_for_homes_already_delivered_become_stuck_obligations(self):
        if not ROUND2:
            self.skipTest("round-2 ledger not present")
        self.archive_sweep(HEAD)
        self.assertIsNone(self.cycle(HEAD, moment=at(-1)))
        # a database written by fe24507: rm:1 was delivered (seen has it) but its 'unsent' verdict was never
        # acknowledged because the process died between save_seen and the DB write
        settings = main.settings_key(self.owner)
        with archive._db() as db:
            db.execute("DELETE FROM advert_work WHERE key='rm:1'")
            db.execute("UPDATE verdicts SET outcome='unsent', settings=? WHERE key='rm:1'", (settings,))
        for quarter in range(6):
            self.cycle(HEAD, moment=at(quarter * 0.25))
        j = self.job("rm:1")
        self.assertEqual(self.alerts, [])
        self.assertFalse(j and j["notify"] and j["state"] == "pending",
                         f"\n a delivered home is still a live obligation after 6 cycles: "
                         f"{j and (j['state'], bool(j['notify']))}")


class R4_Isolation(Repro):
    def setUp(self):
        super().setUp()
        if not ROUND2:
            self.skipTest("round-2 ledger not present")
        self.enterContext(patch.dict(os.environ, {"TELEGRAM_INVITE_USERNAMES": "bea", "MAX_USERS": "2"}))
        self.friend = users.register("200", "Bea", "bea")
        self.assertIsNotNone(self.friend)

    def test_R4a_CONTROL_removed_person_does_not_disturb_the_owner_and_their_rows_are_inert(self):
        self.archive_sweep(HEAD)
        self.assertIsNone(self.cycle(HEAD, moment=at(-1)))
        with patch.object(config, "MAX_ALERTS_PER_RUN", 0):
            self.cycle(HEAD + NEW[:1], moment=at(0))                      # both people get a pending intent
        for chat in ("100", "200"):
            self.assertTrue(self.job("rm:4", chat)["notify"], chat)
        users.remove("200")
        for quarter in range(1, 4):
            self.assertIsNone(self.cycle(HEAD + NEW[:1], moment=at(quarter * 0.25)))
        self.assertEqual([k for k, _ in self.alerts], ["rm:4"])           # owner alerted exactly once
        self.assertTrue(self.job("rm:4", "200")["notify"])                # the removed person's rows stay inert
        self.assertIsNone(users.get("200"))


class R5_AlertLifecycle(Repro):
    """CONTROLS: kill points around the delivery write. These assert the sound behaviour and pass."""

    def setUp(self):
        super().setUp()
        self.archive_sweep(HEAD)
        self.assertIsNone(self.cycle(HEAD, moment=at(-1)) if ROUND2 else self.cycle(HEAD))

    def test_R5a_CONTROL_kill_after_intent_before_send_delivers_exactly_once(self):
        class Kill(BaseException):
            pass

        def die(l, reason):
            raise Kill()
        with patch.object(telegram, "send_listing", die):
            with self.assertRaises(Kill):
                self._cycle_raw(HEAD + NEW[:1])
        self.cycle(HEAD + NEW[:1]); self.cycle(HEAD + NEW[:1])
        self.assertEqual(self.alerts, [("rm:4", "new")])

    def _cycle_raw(self, head):
        with patch.object(rightmove, "fetch", return_value=list(head)):
            watch.Watcher(["rm"]).cycle()

    def test_R5b_CONTROL_kill_between_seen_and_db_ack_does_not_duplicate(self):
        if not ROUND2:
            self.skipTest("round-2 ledger not present")
        class Kill(BaseException):
            pass
        real = archive.record_verdicts

        def die_on_ack(*a, **k):
            if k.get("acknowledged"):
                raise Kill()
            return real(*a, **k)
        with patch.object(archive, "record_verdicts", die_on_ack):
            with self.assertRaises(Kill):
                self._cycle_raw(HEAD + NEW[:1])
        self.assertEqual(self.alerts, [("rm:4", "new")])                  # the card WAS sent before the kill
        self.cycle(HEAD + NEW[:1]); self.cycle(HEAD + NEW[:1])
        self.assertEqual(self.alerts, [("rm:4", "new")])                  # and is not sent again

    def test_R5c_CONTROL_telegram_outage_then_recovery_sends_each_home_once_within_the_cap(self):
        def down(l, reason):
            raise RuntimeError("Telegram 502")
        with patch.object(telegram, "send_listing", down):
            for _ in range(3):
                self.cycle(HEAD + NEW)
        for _ in range(4):
            self.cycle(HEAD + NEW)
        self.assertEqual(sorted(k for k, _ in self.alerts), ["rm:4", "rm:5", "rm:6"])

    def test_R5d_CONTROL_settings_change_after_baseline_does_not_flood(self):
        for _ in range(2):
            self.cycle(HEAD + NEW)
        self.alerts.clear()
        users.save_params(users.owner(), {"commute": 35})
        d1.query("DELETE FROM meta WHERE name = 'watch_initialized'")       # what /set does
        for _ in range(3):
            self.cycle(HEAD + NEW)
        self.assertEqual(self.alerts, [])


class RetryHealth(Repro):
    def setUp(self):
        super().setUp()
        self.archive_sweep(HEAD)
        self.assertIsNone(self.cycle(HEAD, moment=at(-1)))

    def test_head_and_sweep_share_the_first_retry_cooldown(self):
        calls = []
        bad = failing_details({"rm:4"})
        def refine(listing):
            if listing.key == "rm:4":
                calls.append(archive.now())
            return bad(listing)
        down = {(rightmove, "refine"): refine}
        self.cycle(HEAD + NEW[:1], down, moment=at(0))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.job("rm:4")["failures"], 1)
        self.cycle(HEAD + NEW[:1], down, moment=at(.01))
        self.assertEqual(len(calls), 1)
        self.cycle(HEAD + NEW[:1], moment=at(.1))
        self.assertEqual(self.alerts, [("rm:4", "new")])

    def test_warning_is_once_per_incident_and_recovery_is_announced(self):
        down = {(enrich, "journey"): Mock(side_effect=ConnectionError("TfL unavailable"))}
        for hour in (0, .25, .5, .75):
            self.assertIsNone(self.cycle(HEAD + NEW[:1], down, moment=at(hour)))
        self.assertEqual(sum("lookups have been degraded" in text for text in self.texts), 1)
        self.assertEqual(d1.query("SELECT value FROM meta WHERE name='watch_lookup_warned'")[0]["value"], "1")
        self.assertIsNone(self.cycle(HEAD + NEW[:1], moment=at(2)))
        self.assertEqual(sum("lookups have recovered" in text for text in self.texts), 1)
        self.assertEqual(self.alerts, [("rm:4", "new")])
        self.cycle(HEAD + NEW[:1], moment=at(3))
        self.assertEqual(sum("lookups have recovered" in text for text in self.texts), 1)

    def test_failed_warning_delivery_retries_without_marking_it_sent(self):
        watcher = watch.Watcher(["rm"])
        with patch.object(telegram, "send_text", side_effect=RuntimeError("Telegram down")):
            for _ in range(config.WATCH_FAILURE_LIMIT):
                watcher._lookup_health(3)
        self.assertNotEqual(sweep._meta("watch_lookup_warned"), "1")
        watcher._lookup_health(3)
        self.assertEqual(sweep._meta("watch_lookup_warned"), "1")
        self.assertEqual(sum("lookups have been degraded" in text for text in self.texts), 1)

    def test_round2_terminal_failure_is_readmitted_without_new_facts(self):
        item = NEW[0]
        archive.upsert([item], "head", at=at(0))
        settings = main.settings_key(self.owner)
        archive.record_verdicts("100", [(item.key, "enrichment", item.rent, 1500, 25, 30)],
                                settings, at=at(0), baseline=False)
        with archive._db() as db:
            db.execute("UPDATE advert_work SET state='failed',failures=3,next_retry=NULL WHERE key=?", (item.key,))
            db.execute("UPDATE verdicts SET outcome='enrichment_failed' WHERE key=?", (item.key,))
        self.assertEqual(archive.pending("100", ["rm"], settings, 10, at=at(.5)), [])
        self.assertEqual([x.key for x in archive.pending("100", ["rm"], settings, 10, at=at(1))], [item.key])
        self.cycle(HEAD + [item], moment=at(1))
        self.assertEqual(self.alerts, [(item.key, "new")])
        self.assertEqual(self.job(item.key)["state"], "done")

    def test_inactive_and_stale_contexts_do_not_hold_lookup_health_alarm(self):
        item = NEW[0]
        archive.upsert([item], "head")
        settings = main.settings_key(self.owner)
        archive.record_verdicts("100", [(item.key, "enrichment", item.rent, 1500, 25, 30)], settings)
        self.assertEqual(archive.lookup_degraded("100", settings), 1)
        self.assertEqual(archive.lookup_degraded("200", settings), 0)
        self.assertEqual(archive.lookup_degraded("100", "different-settings"), 0)
        with archive._db() as db:
            db.execute("UPDATE adverts SET status='unseen' WHERE key=?", (item.key,))
        self.assertEqual(archive.lookup_degraded("100", settings), 0)

    def test_repeated_outages_have_bounded_rate_not_a_terminal_budget(self):
        item = NEW[0]
        archive.upsert([item], "head")
        settings = main.settings_key(self.owner)
        token = None
        import datetime as dt
        for hour in range(10):
            archive.record_verdicts("100", [(item.key, "enrichment", item.rent, 1500, 25, 30)],
                                    settings, at=at(hour), baseline=False)
            row = self.job(item.key)
            token = token or row["intent_id"]
            self.assertEqual(row["intent_id"], token)
            self.assertEqual(row["state"], "backoff")
            delay = dt.datetime.fromisoformat(row["next_retry"]) - dt.datetime.fromisoformat(at(hour))
            self.assertGreaterEqual(delay.total_seconds(), archive.RETRY_BACKOFF_SECONDS)
            self.assertLessEqual(delay.total_seconds(), archive.MAX_RETRY_BACKOFF_SECONDS)
        self.cycle(HEAD + [item], moment=at(12))
        self.assertEqual(self.alerts, [(item.key, "new")])


if __name__ == "__main__":
    unittest.main()
