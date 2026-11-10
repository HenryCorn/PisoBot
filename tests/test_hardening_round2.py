"""Round-2 regressions ported verbatim from the independent reviewer\'s seven offline repros.

Every test asserts correct behaviour, including the complete Watcher.cycle composition.
"""
import io
import os
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = os.getcwd()
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

from test_sweep import JudgingBase          # noqa: E402  (repository fixture: no live I/O)
from test_archive import advert, SCOPE, at      # noqa: E402
from pisobot import archive, config, d1, main, net, status, sweep, telegram, users, watch  # noqa: E402
from pisobot.sources import rightmove, otm_rent       # noqa: E402

HEAD = [advert(1, 1200), advert(2, 1210), advert(3, 1220)]
NEW = [advert(4, 1230), advert(5, 1240), advert(6, 1250)]


def failing_details(keys, times=None):
    """Rightmove details unavailable for these keys (e.g. 5xx, bot page, ID mismatch)."""
    left = {k: times for k in keys}

    def refine(l):
        if l.key in left and (left[l.key] is None or left[l.key] > 0):
            if left[l.key] is not None:
                left[l.key] -= 1
            l.detail_unavailable = True
            return False
        return True
    return refine


class Repro(JudgingBase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch("sys.stdout", new=io.StringIO()))
        self.enterContext(patch("sys.stderr", new=io.StringIO()))
        # Deep-sweep crawl is stubbed: the generation is "still in progress / not yet complete",
        # as after an upgrade, a /set change or a new person. No portal request can be made.
        self.crawl = self.enterContext(patch.object(sweep, "crawl"))

    def cycle(self, head, patches=None):
        targets = [patch.object(rightmove, "fetch", return_value=list(head))]
        targets += [patch.object(obj, name, value) for (obj, name), value in (patches or {}).items()]
        for t in targets:
            t.start()
        try:
            watch.Watcher(["rm"]).cycle()
            return None
        except Exception as ex:   # the cycle reports incompleteness by raising
            return ex
        finally:
            for t in reversed(targets):
                t.stop()

    def outcomes(self):
        return {r["key"]: r["outcome"] for r in archive.verdicts_since("100", 1)}

    def evidence(self):
        return (f"alerts={self.alerts} seen={sorted(self.seen())} verdicts={self.outcomes()} "
                f"sweep_base={sweep._meta('sweep_base:rm')!r} found={sweep._meta('sweep_found:rm')!r}")


class Control(Repro):
    def test_control_new_head_match_alerts_once(self):
        self.assertIsNone(self.cycle(HEAD))                      # silent first baseline
        self.assertEqual(self.alerts, [])
        self.assertIsNone(self.cycle(HEAD + NEW[:1]))
        self.assertIsNone(self.cycle(HEAD + NEW[:1]))
        self.assertEqual(self.alerts, [("rm:4", "new")], self.evidence())


class QuietSweepAbsorbsHeadLeftovers(Repro):
    """F1: head leftovers are now 'unsent' and a quiet (not yet baselined) sweep judges them silently."""

    def test_A1_telegram_failure_mid_batch_remaining_alerts_are_lost(self):
        self.assertIsNone(self.cycle(HEAD))
        calls = []

        def flaky(l, reason):
            calls.append(l.key)
            if len(calls) == 2:
                raise RuntimeError("Telegram 502")
            self.alerts.append((l.key, reason))
        self.assertIsInstance(self.cycle(HEAD + NEW, {(telegram, "send_listing"): flaky}), RuntimeError)
        after_failure = self.evidence()
        for _ in range(3):
            self.cycle(HEAD + NEW)
        self.assertEqual({k for k, _ in self.alerts}, {"rm:4", "rm:5", "rm:6"},
                         f"\n after failed cycle: {after_failure}\n after 3 retries:     {self.evidence()}")

    def test_A2_alert_cap_overflow_is_lost(self):
        self.assertIsNone(self.cycle(HEAD))
        with patch.object(config, "MAX_ALERTS_PER_RUN", 1):
            for _ in range(4):
                self.cycle(HEAD + NEW)
        self.assertEqual({k for k, _ in self.alerts}, {"rm:4", "rm:5", "rm:6"}, self.evidence())

    def test_A3_one_transient_head_detail_failure_is_lost(self):
        self.assertIsNone(self.cycle(HEAD))
        err = self.cycle(HEAD + NEW[:1], {(rightmove, "refine"): failing_details({"rm:4"}, times=1)})
        after_failure = self.evidence()
        for _ in range(2):
            self.cycle(HEAD + NEW[:1])                           # details work again
        self.assertEqual(self.alerts, [("rm:4", "new")],
                         f"\n failed cycle raised {err!r}\n after failure: {after_failure}\n"
                         f" after 2 healthy cycles: {self.evidence()}")


class DetailFailureBlocksHeadBaseline(Repro):
    """F2: one advert with persistently unavailable details now blocks the whole head baseline."""

    def test_B_one_bad_advert_blocks_baseline_and_later_new_home_never_alerts(self):
        bad = {(rightmove, "refine"): failing_details({"rm:2"})}
        errors = [self.cycle(HEAD, bad) for _ in range(3)]
        live_msgs = [t for t in self.texts if "Rental watch is live" in t]
        initialized = sweep._meta("watch_initialized")
        self.cycle(HEAD + NEW[:1], bad)                        # rm:4 is listed while blocked
        self.cycle([HEAD[0], HEAD[2]] + NEW[:1])                 # bad advert withdrawn: baseline completes
        self.cycle([HEAD[0], HEAD[2]] + NEW[:1])
        self.assertEqual(self.alerts, [("rm:4", "new")],
                         f"\n first 3 cycles raised {[type(e).__name__ + ': ' + str(e) for e in errors]}"
                         f"\n 'watch is live' messages={len(live_msgs)} watch_initialized={initialized!r}"
                         f"\n end: {self.evidence()}")


class DetailFailureMakesEveryCycleIncomplete(Repro):
    """F3: a persistent single-advert detail failure turns off the healthcheck every cycle."""

    def test_C1_steady_state_one_bad_advert_suppresses_healthcheck(self):
        self.assertIsNone(self.cycle(HEAD))
        ping = Mock(return_value=Mock(raise_for_status=Mock()))
        with patch.dict(os.environ, {"HEALTHCHECK_URL": "https://hc.invalid/ping"}), \
                patch.object(net.session, "get", ping):
            errors = [self.cycle(HEAD + NEW[:1], {(rightmove, "refine"): failing_details({"rm:4"})})
                      for _ in range(3)]
        self.assertEqual(ping.call_count, 3,
                         f"\n cycles raised {[repr(e) for e in errors]}; pings={ping.call_count}; "
                         f"watch_last_success={sweep._meta('watch_last_success')!r}")


class PersistentEnrichmentKeepsSweepQuiet(Repro):
    """F4: an unresolvable enrichment row prevents the sweep baseline forever; later sweep-only
    homes are recorded silently (lost), not 'delayed' as docs/wp2-hardening.md limit 4 says."""

    def test_C2_sweep_only_new_home_is_absorbed_not_delayed(self):
        bad = failing_details({"rm:2"})
        with patch.object(rightmove, "refine", bad):
            self.archive_sweep([advert(1, 1200), advert(2, 1210)])
            for _ in range(3):
                sweep.judge(self.owner, ["rm"], 1000, clock=lambda: 0)
            base_after_three = sweep._meta("sweep_base:rm")
            self.archive_sweep([advert(1, 1200), advert(2, 1210), advert(3, 1220)], hour=12)
            for _ in range(3):
                sweep.judge(self.owner, ["rm"], 1000, clock=lambda: 0)
        self.assertEqual(self.alerts, [("rm:3", "new")],
                         f"\n sweep_base after 3 judges of a complete covering sweep: {base_after_three!r}"
                         f"\n end: {self.evidence()}")


class RoundTwoPrinciples(Repro):
    def job(self, key, chat="100"):
        with archive._db() as db:
            return dict(db.execute("SELECT * FROM advert_work WHERE chat_id=? AND key=?",
                                   (chat, key)).fetchone())

    def test_whole_cycle_cap_then_quiet_sweep_preserves_intents_after_head_drops_them(self):
        self.assertIsNone(self.cycle(HEAD))
        with patch.object(config, "MAX_ALERTS_PER_RUN", 1):
            self.assertIsNone(self.cycle(HEAD + NEW))
            self.assertEqual(self.alerts, [("rm:4", "new")])
            self.assertNotIn("rm:5", self.seen())
            self.assertTrue(self.job("rm:5")["notify"])
            # These adverts fall off the head pages. Only the quiet archive judge can deliver.
            self.cycle(HEAD)
            self.cycle(HEAD)
        self.assertEqual(self.alerts, [("rm:4", "new"), ("rm:5", "new"), ("rm:6", "new")])
        self.assertEqual(sweep._meta("sweep_found:rm"), "0")
        self.assertEqual(archive.work_counts("100")["intent"], 0)

    def test_interrupted_live_lookup_is_not_reclassified_as_a_quiet_baseline(self):
        item = advert(20)
        archive.upsert([item], "head")
        pid = os.fork()
        if pid == 0:
            with users.use(self.owner), patch.object(main.enrich, "journey", side_effect=lambda *a: os._exit(73)):
                main._run(["rm"], False, False, rentals=True, watch=True, prefetched=([item], []))
            os._exit(0)
        self.assertEqual(os.waitpid(pid, 0)[1] >> 8, 73)
        self.assertEqual(self.job(item.key)["state"], "evaluating")
        self.assertFalse(self.job(item.key)["silent"])
        self.assertNotIn(item.key, self.seen())
        self.judge()  # first sweep is still quiet and no complete generation exists
        self.assertEqual(self.alerts, [(item.key, "new")])
        self.assertEqual(sweep._meta("sweep_found:rm"), "0")

    def test_live_intent_survives_settings_change_and_a_zero_budget_baseline(self):
        item = advert(20, 1300)
        archive.upsert([item], "head")
        with users.use(self.owner), users.criteria(self.owner.params):
            main._run(["rm"], False, False, rentals=True, watch=True,
                      prefetched=([item], []), alert_budget={"remaining": 0})
        self.assertTrue(self.job(item.key)["notify"])
        users.save_params(self.owner, {"rent": 1700})
        sweep.judge(self.owner, ["rm"], 1000, clock=lambda: 0, alert_budget={"remaining": 0})
        self.assertNotIn(item.key, self.seen())
        self.assertTrue(self.job(item.key)["notify"])
        sweep.judge(self.owner, ["rm"], 1000, clock=lambda: 0)
        self.assertEqual(self.alerts, [(item.key, "new")])
        self.assertFalse(self.job(item.key)["notify"])
        self.assertEqual(sweep._meta("sweep_found:rm"), "0")

    def test_dry_run_does_not_read_or_create_delivery_work(self):
        with patch.object(archive, "_db", side_effect=AssertionError("dry-run work write/read")) as db:
            result = main._run(["rm"], True, False, rentals=True, watch=True,
                               prefetched=([advert(20)], []))
        self.assertEqual(result["matches"], 1)
        db.assert_not_called()
        self.assertEqual(self.alerts, [])

    def test_price_reversion_and_quiet_run_cannot_receipt_a_different_intent(self):
        item = advert(20, 1400)
        archive.upsert([item], "head")
        with users.use(self.owner):
            main._run(["rm"], False, False, rentals=True, watch=True, prefetched=([item], []))
        first_receipt = main.load_seen()[item.key]["delivery_intent"]
        cut = advert(20, 1300)
        archive.upsert([cut], "head")
        with users.use(self.owner):
            main._run(["rm"], False, False, rentals=True, watch=True, prefetched=([cut], []),
                      alert_budget={"remaining": 0})
        outstanding = archive.intent_token("100", item.key)
        self.assertNotEqual(first_receipt, outstanding)
        archive.upsert([item], "head")  # same rent as the PREVIOUS actual receipt, not this intent
        with users.use(self.owner):
            main._run(["rm"], False, False, rentals=True, watch=True, baseline=True,
                      prefetched=([item], []))
        self.assertEqual(self.alerts, [(item.key, "new")])
        self.assertEqual(archive.intent_token("100", item.key), outstanding)
        self.assertTrue(self.job(item.key)["notify"])
        self.assertEqual(main.load_seen()[item.key]["delivery_intent"], first_receipt)

    def test_exact_delivery_token_recovers_a_crash_between_seen_and_database_ack(self):
        item = advert(20)
        archive.upsert([item], "head")
        original = archive.record_verdicts
        def before_ack(*args, **kwargs):
            if kwargs.get("acknowledged"):
                os._exit(73)
            return original(*args, **kwargs)
        pid = os.fork()
        if pid == 0:
            with users.use(self.owner), patch.object(archive, "record_verdicts", before_ack):
                main._run(["rm"], False, False, rentals=True, watch=True, prefetched=([item], []))
            os._exit(0)
        self.assertEqual(os.waitpid(pid, 0)[1] >> 8, 73)
        self.assertTrue(self.job(item.key)["notify"])
        self.assertEqual(main.load_seen()[item.key]["delivery_intent"], archive.intent_token("100", item.key))
        self.judge()  # quiet, but this exact token proves ACTUAL delivery, not silent bookkeeping
        self.assertEqual(self.alerts, [])
        self.assertFalse(self.job(item.key)["notify"])
        self.assertEqual(self.job(item.key)["state"], "done")

    def test_quiet_bookkeeping_cannot_acknowledge_a_live_intent_even_at_storage_boundary(self):
        item = advert(20, 1300)
        archive.upsert([item], "head")
        row = (item.key, "unsent", item.rent, 1500, 25, 30)
        archive.record_verdicts("100", [row], "one", baseline=False)
        archive.record_verdicts("100", [(item.key, "matched", item.rent, 1500, 25, 30)],
                                "two", baseline=True)
        self.assertTrue(self.job(item.key)["notify"])
        self.assertFalse(self.job(item.key)["silent"])
        self.assertEqual(self.outcomes()[item.key], "unsent")
        # A newly tighter criterion holds the intent, but does not silently receipt it.
        archive.record_verdicts("100", [(item.key, "rent_cap", item.rent, 1500, None, None)],
                                "two", baseline=True)
        self.assertEqual(self.job(item.key)["state"], "blocked")
        self.assertTrue(self.job(item.key)["notify"])
        self.assertIn("1 intent(s) blocked", status.status_text())
        with self.assertRaises(ValueError):
            archive.record_verdicts("100", [(item.key, "matched", item.rent, 1500, 25, 30)],
                                    "two", baseline=True, acknowledged=True)
        self.assertEqual(archive.silent_keys("100", [item.key], baseline=True), set())
        archive.record_verdicts("100", [(item.key, "matched", item.rent, 1500, 25, 30)],
                                "two", acknowledged=True)
        self.assertFalse(self.job(item.key)["notify"])

    def test_bounded_retry_backoff_final_state_and_healthy_whole_cycles(self):
        self.archive_sweep(HEAD)
        attempts = []
        bad = failing_details({"rm:2"})
        def refine(l):
            if l.key == "rm:2":
                attempts.append(archive.now())
            return bad(l)
        ping = Mock(return_value=Mock(raise_for_status=Mock()))
        with patch.dict(os.environ, {"HEALTHCHECK_URL": "https://hc.invalid/ping"}), \
                patch.object(net.session, "get", ping):
            for moment in (at(0), at(.01), at(.2), at(24)):
                with patch.object(archive, "now", return_value=moment):
                    self.assertIsNone(self.cycle(HEAD, {(rightmove, "refine"): refine}))
                    if moment == at(0):
                        self.assertEqual(sweep._meta("watch_initialized"), "1")
                        self.assertEqual(sweep._meta("sweep_base:rm"), main.settings_key(self.owner))
                        self.assertEqual(self.job("rm:2")["failures"], 2)
                    if moment == at(.01):
                        self.assertEqual(len(attempts), 2)  # real head AND sweep both obey backoff
        self.assertEqual(len(attempts), archive.LOOKUP_ATTEMPTS)
        self.assertEqual(ping.call_count, 4)
        self.assertIsNotNone(sweep._meta("watch_last_success"))
        self.assertEqual(self.job("rm:2")["state"], "failed")
        self.assertEqual(self.outcomes()["rm:2"], "enrichment_failed")
        self.assertNotIn("rm:2", self.seen())
        shown = status.status_text()
        self.assertIn("1 exhausted", shown)
        self.assertIn("Home 2", shown)
        self.assertIn("3 attempts; enrichment_failed", shown)
        self.assertIn("source scan is live", shown)

    def test_baseline_member_recovers_silently_after_both_head_and_sweep_go_live(self):
        self.archive_sweep(HEAD)
        with patch.object(archive, "now", return_value=at(0)):
            self.assertIsNone(self.cycle(HEAD, {(rightmove, "refine"): failing_details({"rm:2"})}))
        self.assertEqual(sweep._meta("watch_initialized"), "1")
        self.assertEqual(sweep._meta("sweep_base:rm"), main.settings_key(self.owner))
        self.assertNotIn("rm:2", self.seen())
        with patch.object(archive, "now", return_value=at(.2)):
            self.assertIsNone(self.cycle(HEAD + NEW[:1]))
        self.assertEqual(self.alerts, [("rm:4", "new")])
        self.assertIn("rm:2", self.seen())  # intentionally silent old member, not absorbed live work
        self.assertEqual(self.job("rm:2")["failures"], 0)

    def test_failed_live_work_stays_pending_and_revives_on_new_facts_not_as_silent_baseline(self):
        self.cycle(HEAD)
        with patch.object(archive, "now", return_value=at(0)):
            self.cycle(HEAD + NEW[:1], {(rightmove, "refine"): failing_details({"rm:4"})})
        with patch.object(archive, "now", return_value=at(.2)):
            self.cycle(HEAD + NEW[:1], {(rightmove, "refine"): failing_details({"rm:4"})})
        self.assertEqual(self.job("rm:4")["state"], "failed")
        self.assertTrue(self.job("rm:4")["notify"])
        self.assertNotIn("rm:4", self.seen())
        changed = advert(4, 1225, description="Updated self-contained flat")
        self.cycle(HEAD + [changed])
        self.assertEqual(self.alerts, [("rm:4", "new")])
        self.assertEqual(self.job("rm:4")["state"], "done")
        self.assertFalse(self.job("rm:4")["notify"])

    def test_reactivation_resets_terminal_retry_admission_without_erasing_the_live_intent(self):
        item = advert(20)
        archive.upsert([item], "head")
        key = main.settings_key(self.owner)
        for _ in range(archive.LOOKUP_ATTEMPTS):
            archive.record_verdicts("100", [(item.key, "enrichment", item.rent, 1500, 25, None)],
                                    key, baseline=False)
        self.assertEqual(archive.retry_block("100", item.key, key), "enrichment_failed")
        with archive._db() as db:
            db.execute("UPDATE adverts SET status='unseen' WHERE key=?", (item.key,))
        archive.upsert([item], "head")  # same public facts, but a new active episode
        self.assertIsNone(archive.retry_block("100", item.key, key))
        self.assertTrue(self.job(item.key)["notify"])
        self.judge()
        self.assertEqual(self.alerts, [(item.key, "new")])
        self.assertFalse(self.job(item.key)["notify"])

    def test_retry_provenance_is_per_person_even_with_identical_criteria(self):
        item = advert(20)
        archive.upsert([item], "head")
        friend = users.User("200")
        for person, baseline in ((self.owner, True), (friend, False)):
            with users.use(person), users.criteria(person.params), \
                    patch.object(rightmove, "refine", failing_details({item.key})):
                main._run(["rm"], False, False, rentals=True, watch=True, baseline=baseline,
                          sweep=True, prefetched=([item], []))
        self.assertTrue(self.job(item.key)["silent"])
        self.assertFalse(self.job(item.key)["notify"])
        self.assertTrue(self.job(item.key, "200")["notify"])
        sent = []
        with patch.object(telegram, "send_listing", side_effect=lambda l, r: sent.append(users.current_chat())):
            for person, baseline in ((self.owner, False), (friend, True)):
                with users.use(person), users.criteria(person.params):
                    main._run(["rm"], False, False, rentals=True, watch=True, baseline=baseline,
                              sweep=True, prefetched=([item], []))
        self.assertEqual(sent, ["200"])

    def test_new_quiet_baseline_does_not_promote_a_resolved_rejection_to_live_work(self):
        item = advert(20)
        archive.upsert([item], "head")
        key = main.settings_key(self.owner)
        archive.record_verdicts("100", [(item.key, "crime", item.rent, 1500, 25, 200)], key, baseline=False)
        self.assertFalse(self.job(item.key)["notify"])
        with users.use(self.owner):
            main._run(["rm"], False, False, rentals=True, watch=True, baseline=True,
                      prefetched=([item], []))  # now succeeds, e.g. refreshed monthly crime data
        self.assertEqual(self.alerts, [])
        self.assertTrue(self.job(item.key)["silent"])
        self.assertIn(item.key, self.seen())

    def test_successful_existing_match_resets_its_consecutive_failure_streak(self):
        self.cycle(HEAD)
        with patch.object(archive, "now", return_value=at(0)):
            self.cycle(HEAD, {(rightmove, "refine"): failing_details({"rm:2"})})
        self.assertEqual(self.job("rm:2")["failures"], 2)
        with patch.object(archive, "now", return_value=at(.2)):
            self.cycle(HEAD)
        self.assertEqual(self.job("rm:2")["failures"], 0)
        self.assertEqual(self.job("rm:2")["state"], "done")
        self.assertEqual(self.alerts, [])

    def test_completed_receipts_are_not_rejudged_after_another_send_fails(self):
        self.archive_sweep(NEW)
        sweep._put_meta("sweep_base:rm", main.settings_key(self.owner))
        sent = []
        def flaky(l, reason):
            if sent:
                raise RuntimeError("another card failed")
            sent.append(l.key)
        with patch.object(telegram, "send_listing", flaky):
            with self.assertRaises(RuntimeError):
                self.judge()
        pending = archive.pending("100", ["rm"], main.settings_key(self.owner), 100)
        self.assertEqual(len(pending), 2)
        self.assertNotIn(sent[0], {l.key for l in pending})
        self.assertEqual(self.job(sent[0])["state"], "done")

    def test_one_permanently_over_budget_advert_does_not_hog_the_baseline_or_new_work(self):
        slow = advert(1, lat=51.52)
        good = advert(2, lat=51.53)
        self.archive_sweep([slow, good])
        ticks = [0]
        def journey(lat, lng):
            if lat == slow.lat:
                ticks[0] += 15
            return {"minutes": 25, "route": "Tube", "station": "S", "walk": 5}
        with patch.object(main.enrich, "journey", journey):
            sweep.judge(self.owner, ["rm"], 10, clock=lambda: ticks[0])
            self.assertIsNone(sweep._meta("sweep_base:rm"))  # good has not been attempted yet
            ticks[0] = 0
            sweep.judge(self.owner, ["rm"], 10, clock=lambda: ticks[0])
        self.assertIn(good.key, self.seen())  # unattempted inventory outranks a yielding retry
        self.assertEqual(sweep._meta("sweep_base:rm"), main.settings_key(self.owner))
        self.assertNotIn(slow.key, self.seen())
        newer = advert(3, lat=51.54)
        archive.upsert([newer], "sweep", at=at(24))
        ticks[0] = 0
        with patch.object(main.enrich, "journey", journey), patch.object(archive, "now", return_value=at(24)):
            sweep.judge(self.owner, ["rm"], 10, clock=lambda: ticks[0])
        self.assertEqual(self.job(slow.key)["state"], "failed")
        self.assertEqual(self.outcomes()[slow.key], "enrichment_failed")
        self.assertTrue(self.job(newer.key)["notify"])
        self.assertNotIn(newer.key, self.seen())  # deadline deferred DELIVERY, never silently absorbed it
        ticks[0] = 0
        with patch.object(main.enrich, "journey", journey), patch.object(archive, "now", return_value=at(24)):
            sweep.judge(self.owner, ["rm"], 10, clock=lambda: ticks[0])
        self.assertEqual(self.alerts, [(newer.key, "new")])

    def test_source_time_sharing_and_rotation_keep_otm_from_starving(self):
        self.archive_sweep([advert(i) for i in range(1, 5)])
        other = advert(30, source="otmr", lat=51.53)
        archive.upsert([other], "head")
        for source in ("rm", "otmr"):
            sweep._put_meta(f"sweep_base:{source}", main.settings_key(self.owner))
        ticks = [0]
        def journey(lat, lng):
            if lat != other.lat:
                ticks[0] += 15  # RM's first request overruns its 10-second share
            return {"minutes": 25, "route": "Tube", "station": "S", "walk": 5}
        with patch.object(main.enrich, "journey", journey), patch.object(otm_rent, "refine", return_value=True):
            sweep.judge(self.owner, ["rm", "otmr"], 20, clock=lambda: ticks[0])
        self.assertEqual(self.alerts, [("otmr:30", "new")])
        self.assertEqual(sweep._meta("sweep_judge_turn"), "1")
        original = archive.pending
        with patch.object(archive, "pending", wraps=original) as pending:
            sweep.judge(self.owner, ["rm", "otmr"], 1, clock=lambda: 0)
        self.assertEqual(pending.call_args_list[0].args[1], ["otmr"])

    def test_step_reserves_notice_time_before_judgement_uses_the_slice(self):
        item = advert(20)
        archive.upsert([item], "head", at=at(0))
        for hour in (12, 24):
            sid = archive.start_sweep("rm", SCOPE, {}, at(hour))
            archive.finish_sweep(sid, True, at=at(hour+.5))
        d1.query("INSERT INTO saved(key,info) VALUES(?,?)", item.key,
                 '{"url":"https://example.com/20","address":"Notice home"}')
        ticks, notices = [0], []
        def crawl(*args):
            ticks[0] += 60
        def send(*args, **kw):
            notices.append(users.current_chat())
            ticks[0] += 1
        def judge(person, sources, seconds, *args, **kw):
            self.assertEqual(notices, ["100"])
            ticks[0] += seconds
            return {}
        self.crawl.side_effect = crawl
        with patch.object(telegram, "send_text", send), patch.object(sweep, "judge", judge):
            sweep.step(["rm"], [self.owner], clock=lambda: ticks[0])
        self.assertEqual(ticks[0], config.DEEP_SWEEP_STEP_SECONDS)
        self.assertEqual(archive.pending_unseen("100"), [])

    def test_step_rotates_people_when_one_inflight_operation_uses_the_entire_slice(self):
        friend = users.User("200", name="Bea")
        users.save_params(friend, {})
        ticks, attempted = [0], []
        def judge(person, sources, seconds, *args, **kw):
            if seconds > 0:
                attempted.append(person.chat_id)
                ticks[0] += config.DEEP_SWEEP_STEP_SECONDS
            return {}
        with patch.object(sweep, "notify_unseen"), patch.object(sweep, "judge", judge):
            sweep.step(["rm"], [self.owner, friend], clock=lambda: ticks[0])
            ticks[0] = 0
            sweep.step(["rm"], [self.owner, friend], clock=lambda: ticks[0])
        self.assertEqual(attempted, ["100", "200"])

    def test_parser_exception_degrades_only_its_advert_not_the_head_baseline(self):
        def broken(l):
            if l.key == "rm:2":
                raise ValueError("bad advert detail shape")
            return True
        self.assertIsNone(self.cycle(HEAD, {(rightmove, "refine"): broken}))
        self.assertEqual(sweep._meta("watch_initialized"), "1")
        self.assertIsNone(self.cycle(HEAD + NEW[:1], {(rightmove, "refine"): broken}))
        self.assertEqual(self.alerts, [("rm:4", "new")])
        self.assertEqual(self.job("rm:2")["failures"], 2)

    def test_round_robin_unseen_recipient_survives_slow_first_chat(self):
        item = advert(20)
        archive.upsert([item], "head", at=at(0))
        for hour in (12, 24):
            sid = archive.start_sweep("rm", SCOPE, {}, at(hour))
            archive.finish_sweep(sid, True, at=at(hour+.5))
        friend = users.User("200", name="Bea")
        users.save_params(friend, {})
        for person in (self.owner, friend):
            with users.use(person):
                d1.query("INSERT INTO saved(key,info) VALUES(?,?)", item.key,
                         '{"url":"https://example.com/20","address":"Home 20"}')
        ticks, sent = [0], []
        def slow_owner(*args, **kw):
            if users.current_chat() == "100":
                ticks[0] += 11
                raise RuntimeError("slow owner chat")
            sent.append(users.current_chat())
        with patch.object(telegram, "send_text", slow_owner):
            sweep.notify_unseen(deadline=10, clock=lambda: ticks[0])
            self.assertEqual(sent, [])
            ticks[0] = 0
            sweep.notify_unseen(deadline=10, clock=lambda: ticks[0])
        self.assertEqual(sent, ["200"])
        self.assertEqual(len(archive.pending_unseen("100")), 1)
        self.assertEqual(archive.pending_unseen("200"), [])

    def test_legacy_unsent_migration_is_conservative_not_a_silent_receipt(self):
        item = advert(20)
        archive.upsert([item], "head")
        with archive._db() as db:
            db.execute("INSERT INTO verdicts VALUES(?,?,?,?,?,?,?,?,?)",
                       ("100", item.key, "unsent", item.rent, 1500, 25, 30, "legacy", at(0)))
        self.judge()
        self.assertEqual(self.alerts, [(item.key, "new")])
        self.assertEqual(self.job(item.key)["state"], "done")


if __name__ == "__main__":
    unittest.main()
