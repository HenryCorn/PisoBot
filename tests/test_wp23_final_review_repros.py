"""Independent PR #15 adverse compositions. No implementation edits or live requests.

Run from the assigned review worktree with PYTHONDONTWRITEBYTECODE=1.
Assertions describe required behavior: failures are the review's executed evidence.
"""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import requests
from pisobot import archive, bot, config, d1, main, net, prefs, renter, sweep, telegram, tracker, users, watch
from test_archive import SCOPE, advert, at
from test_hardening_round2 import HEAD, Repro
from test_sweep import JudgingBase
from test_wp3 import NOW, publish

EVIDENCE = {}


@contextlib.contextmanager
def case(cls=Repro):
    with patch.object(requests.sessions.Session, "request",
                      side_effect=AssertionError("Live HTTP forbidden by independent review")):
        fixture = cls()
        fixture.setUp()
        try:
            yield fixture
        finally:
            fixture.doCleanups()


def baseline(c):
    c.archive_sweep(HEAD)
    assert c.cycle(HEAD) is None


def job(key, chat="100"):
    with archive._db() as db:
        row = archive._job(db, chat, key)
        return dict(row) if row else None


def saved(item):
    info = {"url": item.url, "address": item.address, "cost": item.monthly_cost, "rent": item.rent}
    d1.query("INSERT INTO saved (key,info) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET info=excluded.info",
             item.key, json.dumps(info))


def disappear(item, hour):
    for offset in (0, 1):
        sid = archive.start_sweep("rm", SCOPE, {}, at(hour + offset))
        archive.finish_sweep(sid, True, at=at(hour + offset + .5))
    assert archive.advert(item.key)["status"] == "unseen"


def accepted(path, label):
    with path.open("a") as out:
        out.write(label + "\n")
        out.flush()
        os.fsync(out.fileno())


def fork_crash(work):
    pid = os.fork()
    if pid == 0:
        try:
            work()
        except BaseException:
            os._exit(91)
        os._exit(0)
    return os.waitpid(pid, 0)[1] >> 8


class FinalReview(unittest.TestCase):
    def test_whole_cycle_attempt_cap_under_repeated_delivery_failure(self):
        measured = []
        for cap in (2, 40):
            with case() as c:
                baseline(c)
                items = [advert(100 + i, 1300) for i in range(cap + 1)]
                attempts = []
                def down(item, reason):
                    attempts.append(item.key)
                    raise RuntimeError("Telegram sendPhoto failed (400): rejected image")
                counts = []
                with patch.object(config, "MAX_ALERTS_PER_RUN", cap), \
                        patch.object(telegram, "send_listing", side_effect=down):
                    for _ in range(3):
                        before = len(attempts)
                        self.assertIsInstance(c.cycle(HEAD + items), RuntimeError)
                        counts.append(len(attempts) - before)
                measured.append({"cap": cap, "attempts_per_cycle": counts,
                                 "seen_new": len(c.seen() & {i.key for i in items}),
                                 "pending": archive.work_counts("100")["intent"]})
        EVIDENCE["whole_cycle_failed_attempts"] = measured
        self.assertTrue(all(max(m["attempts_per_cycle"]) <= m["cap"] for m in measured), measured)

    def test_repeated_bad_prefix_does_not_starve_a_healthy_later_card(self):
        results = []
        for cap in (2, 40):
            with case() as c:
                baseline(c)
                bad = [advert(100 + i, 1250) for i in range(cap)]
                good = advert(999, 1500)
                attempts, successes = [], []
                def send(item, reason):
                    attempts.append(item.key)
                    if item.key in {i.key for i in bad}:
                        raise RuntimeError("Telegram sendPhoto failed (400): rejected image")
                    successes.append(item.key)
                with patch.object(config, "MAX_ALERTS_PER_RUN", cap), \
                        patch.object(telegram, "send_listing", side_effect=send):
                    for _ in range(3):
                        c.cycle(HEAD + bad + [good])
                results.append({"cap": cap, "attempt_count": len(attempts), "successes": successes,
                                "healthy_attempted": good.key in attempts,
                                "healthy_seen": good.key in c.seen(), "healthy_job": job(good.key)})
        EVIDENCE["poison_prefix"] = results
        self.assertTrue(all(result["healthy_attempted"] for result in results), results)

    def test_second_unseen_episode_is_not_silently_acknowledged(self):
        with case(JudgingBase) as c:
            item = advert(4, 1400)
            archive.upsert([item], "head", at=at(0))
            saved(item)
            disappear(item, 2)
            first = archive.change_id(item.key, "status", "unseen")
            sweep.notify_unseen()
            self.assertEqual(len(c.texts), 1)
            archive.upsert([item], "head", at=at(4))
            main._run(["rm"], False, False, rentals=True, watch=True, prefetched=([item], []))
            after_return = prefs.load()["saved"][item.key].get("unseen")
            disappear(item, 6)
            second = archive.change_id(item.key, "status", "unseen")
            before = len(archive.pending_unseen("100"))
            sweep.notify_unseen()
            EVIDENCE["second_unseen_episode"] = {
                "episode_ids": [first, second], "unseen_flag_after_return": after_return,
                "queued_before_second_drain": before, "total_notices": len(c.texts),
                "queued_after": len(archive.pending_unseen("100")),
                "second_episode_claims": tracker.query("SELECT * FROM notices WHERE event=?", second)}
            self.assertEqual(len(c.texts), 2, EVIDENCE["second_unseen_episode"])

    def test_failed_pipeline_notice_cannot_be_a_watcher_delivery_receipt(self):
        with case() as c:
            baseline(c)
            item = advert(4, 1400)
            c.cycle(HEAD + [item])
            tracker.move(publish(item), "enquired")
            cut = advert(4, 1300)
            archive.upsert([cut], "head")
            event = archive.change_id(item.key, "rent", cut.rent)
            client = Mock()
            client.call.side_effect = bot.TelegramError("Rejected request", permanent=True)
            bot.Handler("100", Mock(return_value=True), client).tick()
            claimed = tracker.query("SELECT * FROM notices WHERE event=?", event)
            cards = []
            with patch.object(telegram, "send_listing", side_effect=lambda l, r: cards.append((l.key, r))):
                err = c.cycle(HEAD + [cut])
            EVIDENCE["failed_notice_false_receipt"] = {
                "rejected_pipeline_requests": client.call.call_count,
                "notice_after_rejection": claimed, "watcher_cards": cards,
                "watcher_error": type(err).__name__, "seen_rent": main.load_seen()[item.key]["rent"],
                "ledger_state": job(item.key)["state"], "ledger_notify": bool(job(item.key)["notify"])}
            self.assertEqual(cards, [(item.key, "drop")], EVIDENCE["failed_notice_false_receipt"])

    def test_rent_notice_both_orders_share_one_successful_receipt(self):
        results = []
        for pipeline_first in (True, False):
            with case() as c:
                baseline(c)
                item = advert(4, 1400)
                c.cycle(HEAD + [item])
                tracker.move(publish(item), "enquired")
                cut = advert(4, 1300)
                archive.upsert([cut], "head")
                client = Mock()
                client.call.return_value = {"message_id": 1}
                handler = bot.Handler("100", Mock(return_value=True), client)
                cards = []
                with patch.object(telegram, "send_listing", side_effect=lambda l, r: cards.append((l.key, r))):
                    if pipeline_first:
                        handler.tick()
                    self.assertIsNone(c.cycle(HEAD + [cut]))
                    if not pipeline_first:
                        handler.tick()
                result = {"pipeline_first": pipeline_first, "pipeline_requests": client.call.call_count,
                          "watcher_cards": cards, "seen_rent": main.load_seen()[item.key]["rent"],
                          "ledger_state": job(item.key)["state"]}
                results.append(result)
                self.assertEqual(client.call.call_count + len(cards), 1, result)
                self.assertEqual(result["seen_rent"], 1300)
                self.assertEqual(result["ledger_state"], "done")
        EVIDENCE["rent_both_orders"] = results

    def test_transient_notice_failure_releases_claim_for_other_path(self):
        with case() as c:
            baseline(c)
            item = advert(4, 1400)
            c.cycle(HEAD + [item])
            tracker.move(publish(item), "enquired")
            cut = advert(4, 1300)
            archive.upsert([cut], "head")
            client = Mock()
            client.call.side_effect = bot.TelegramError("Transient timeout")
            handler = bot.Handler("100", Mock(return_value=True), client)
            handler.tick()
            self.assertEqual(tracker.query("SELECT * FROM notices"), [])
            cards = []
            with patch.object(telegram, "send_listing", side_effect=lambda l, r: cards.append((l.key, r))):
                self.assertIsNone(c.cycle(HEAD + [cut]))
            client.call.side_effect = None
            client.call.reset_mock()
            handler.tick()
            EVIDENCE["transient_notice_retry"] = {"watcher_cards": cards,
                                                 "later_pipeline_requests": client.call.call_count}
            self.assertEqual(cards, [(item.key, "drop")])
            self.assertEqual(client.call.call_count, 0)

    def test_failed_owner_card_does_not_stop_guest_or_leak_profile(self):
        with case() as c:
            with patch.dict(os.environ, {"TELEGRAM_INVITE_USERNAMES": "bea"}):
                friend = users.register("200", "Guest", "bea")
            self.assertIsNotNone(friend)
            baseline(c)
            renter.set_field("name", "Owner profile")
            with users.use(friend):
                renter.set_field("name", "Guest profile")
            sent, tries = [], []
            def send(item, reason):
                chat = users.current_chat()
                tries.append((chat, item.key, renter.profile()["name"]))
                if chat == "100":
                    raise RuntimeError("Telegram unavailable for owner")
                sent.append((chat, item.key))
            item = advert(4, 1400)
            with patch.object(telegram, "send_listing", side_effect=send):
                self.assertIsInstance(c.cycle(HEAD + [item]), RuntimeError)
            with users.use(friend):
                guest_seen = item.key in main.load_seen()
            EVIDENCE["owner_guest_isolation"] = {
                "attempts": tries, "successes": sent, "guest_seen": guest_seen,
                "owner_seen": item.key in c.seen(),
                "ledger_notify": {chat: bool(job(item.key, chat)["notify"]) for chat in ("100", "200")}}
            self.assertEqual(sent, [("200", item.key)])
            self.assertTrue(guest_seen)
            self.assertNotIn(item.key, c.seen())
            self.assertTrue(job(item.key, "100")["notify"])
            self.assertFalse(job(item.key, "200")["notify"])
            self.assertTrue(all(name == ("Owner profile" if chat == "100" else "Guest profile")
                                for chat, _, name in tries))

    def test_unseen_failure_is_private_and_does_not_block_guest(self):
        with case() as c:
            with patch.dict(os.environ, {"TELEGRAM_INVITE_USERNAMES": "bea"}):
                friend = users.register("200", "Guest", "bea")
            item = advert(4, 1400)
            archive.upsert([item], "head", at=at(0))
            for person in (c.owner, friend):
                with users.use(person):
                    saved(item)
            disappear(item, 2)
            sent = []
            def send(text, **kw):
                if users.current_chat() == "100":
                    raise RuntimeError("Owner temporarily unavailable")
                sent.append(users.current_chat())
            with patch.object(telegram, "send_text", side_effect=send):
                sweep.notify_unseen()
            EVIDENCE["unseen_person_failure"] = {
                "delivered": sent, "owner_pending": len(archive.pending_unseen("100")),
                "guest_pending": len(archive.pending_unseen("200"))}
            self.assertEqual(sent, ["200"])
            self.assertEqual(len(archive.pending_unseen("100")), 1)
            self.assertEqual(archive.pending_unseen("200"), [])
            self.assertEqual(tracker.query("SELECT * FROM notices"), [])

    def test_crash_after_claim_before_send_recovers_only_after_lease(self):
        with case() as c:
            baseline(c)
            item = advert(4, 1400)
            c.cycle(HEAD + [item])
            tracker.move(publish(item), "enquired")
            cut = advert(4, 1300)
            archive.upsert([cut], "head")
            def child():
                with patch.object(tracker, "now", return_value=NOW):
                    tracker.once(item.key, "rent", 1300, lambda: os._exit(73))
            exited = fork_crash(child)
            cards = []
            with patch.object(telegram, "send_listing", side_effect=lambda l, r: cards.append((l.key, r))), \
                    patch.object(tracker, "now", return_value=NOW):
                c.cycle(HEAD + [cut])
            busy = {"cards": list(cards), "seen_rent": main.load_seen()[item.key]["rent"],
                    "notify": bool(job(item.key)["notify"])}
            with patch.object(telegram, "send_listing", side_effect=lambda l, r: cards.append((l.key, r))), \
                    patch.object(tracker, "now", return_value=NOW + tracker.NOTICE_LEASE + dt.timedelta(seconds=1)):
                c.cycle(HEAD + [cut])
            EVIDENCE["crash_claim_before_send"] = {"child_exit": exited, "during_lease": busy,
                                                  "after_expiry_cards": cards,
                                                  "after_expiry_state": job(item.key)["state"]}
            self.assertEqual(exited, 73)
            self.assertEqual(busy, {"cards": [], "seen_rent": 1400, "notify": True})
            self.assertEqual(cards, [(item.key, "drop")])
            self.assertEqual(job(item.key)["state"], "done")

    def test_crash_after_change_ack_before_seen_recovers_without_resend(self):
        with case() as c:
            baseline(c)
            item = advert(4, 1400)
            c.cycle(HEAD + [item])
            tracker.move(publish(item), "enquired")
            cut = advert(4, 1300)
            archive.upsert([cut], "head")
            log = Path(c.tmp.name) / "accepted.txt"
            def child():
                with patch.object(telegram, "send_listing", side_effect=lambda l, r: accepted(log, l.key)), \
                        patch.object(main, "save_seen", side_effect=lambda state: os._exit(73)):
                    main._run(["rm"], False, False, rentals=True, watch=True, prefetched=([cut], []))
            exited = fork_crash(child)
            before = {"seen_rent": main.load_seen()[item.key]["rent"], "state": job(item.key)["state"]}
            with patch.object(telegram, "send_listing", side_effect=lambda l, r: accepted(log, l.key)):
                c.cycle(HEAD + [cut])
            EVIDENCE["crash_ack_before_seen"] = {"child_exit": exited, "before_restart": before,
                                                "accepted_lines": log.read_text().splitlines(),
                                                "after_seen_rent": main.load_seen()[item.key]["rent"],
                                                "after_state": job(item.key)["state"]}
            self.assertEqual(exited, 73)
            self.assertEqual(before, {"seen_rent": 1400, "state": "pending"})
            self.assertEqual(log.read_text().splitlines(), [item.key])
            self.assertEqual(job(item.key)["state"], "done")

    def test_crash_after_notice_ack_before_unseen_event_ack_recovers(self):
        with case(JudgingBase) as c:
            item = advert(4, 1400)
            archive.upsert([item], "head", at=at(0))
            saved(item)
            disappear(item, 2)
            log = Path(c.tmp.name) / "accepted-unseen.txt"
            def child():
                with patch.object(telegram, "send_text", side_effect=lambda *a, **k: accepted(log, item.key)), \
                        patch.object(archive, "acknowledge_unseen", side_effect=lambda *a: os._exit(73)):
                    sweep.notify_unseen()
            exited = fork_crash(child)
            pending = len(archive.pending_unseen("100"))
            with patch.object(telegram, "send_text", side_effect=lambda *a, **k: accepted(log, item.key)):
                sweep.notify_unseen()
            EVIDENCE["crash_notice_ack_before_event_ack"] = {
                "child_exit": exited, "pending_after_crash": pending,
                "accepted_lines": log.read_text().splitlines(),
                "pending_after_restart": len(archive.pending_unseen("100")),
                "saved_unseen": prefs.load()["saved"][item.key].get("unseen")}
            self.assertEqual(exited, 73)
            self.assertEqual(pending, 1)
            self.assertEqual(log.read_text().splitlines(), [item.key])
            self.assertEqual(archive.pending_unseen("100"), [])

    def test_expired_sender_cannot_release_another_senders_new_lease(self):
        with case(JudgingBase):
            item = advert(4, 1400)
            archive.upsert([item], "head")
            event = archive.change_id(item.key, "rent", 1400)
            self.assertEqual(tracker.claim(item.key, "rent", event, at=NOW), "claimed")
            later = NOW + tracker.NOTICE_LEASE + dt.timedelta(seconds=1)
            self.assertEqual(tracker.claim(item.key, "rent", event, at=later), "claimed")
            tracker.release(item.key, "rent", event)  # expired sender A finishes with a failure
            third = tracker.claim(item.key, "rent", event, at=later)
            EVIDENCE["expired_claim_owner"] = {"third_sender_during_sender_b_lease": third}
            self.assertEqual(third, "busy", EVIDENCE["expired_claim_owner"])

    def test_sweep_only_send_failures_are_reported_to_cycle_health(self):
        with case() as c:
            baseline(c)
            item = advert(4, 1400)
            archive.upsert([item], "sweep", c.archive_sweep([], hour=12))
            ping = Mock(return_value=Mock(raise_for_status=Mock()))
            attempts, errors = [], []
            def down(item, reason):
                attempts.append(item.key)
                raise RuntimeError("Telegram transport failure")
            with patch.dict(os.environ, {"HEALTHCHECK_URL": "https://healthcheck.invalid/offline"}), \
                    patch.object(net.session, "get", ping), \
                    patch.object(telegram, "send_listing", side_effect=down):
                for _ in range(3):
                    errors.append(type(c.cycle(HEAD)).__name__)
            EVIDENCE["sweep_delivery_health"] = {
                "attempts": attempts, "cycle_errors": errors, "healthcheck_pings": ping.call_count,
                "pending": bool(job(item.key)["notify"]), "watch_success": sweep._meta("watch_last_success"),
                "lookup_incident_cycles": sweep._meta("watch_lookup_failures"),
                "lookup_degraded": sweep._meta("watch_degraded_adverts")}
            self.assertEqual(ping.call_count, 0, EVIDENCE["sweep_delivery_health"])


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    print("\nINDEPENDENT_EVIDENCE\n" + json.dumps(EVIDENCE, indent=2, sort_keys=True))
    raise SystemExit(not result.wasSuccessful())
