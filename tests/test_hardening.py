"""Offline regressions for the independent WP2 review. No external service may be called."""
import io
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

from requests import ConnectionError, HTTPError

try:
    from tests.test_sweep import JudgingBase, Market, drive
    from tests.test_archive import advert, at, SCOPE
except ImportError:
    from test_sweep import JudgingBase, Market, drive
    from test_archive import advert, at, SCOPE

from pisobot import archive, config, costs, d1, enrich, localdb, main, net, prefs, status, sweep, telegram, users, watch
from pisobot.models import Listing
from pisobot.sources import rightmove, otm_rent

REAL_RM_REFINE = rightmove.refine
REAL_OTM_REFINE = otm_rent.refine


class Hardening(JudgingBase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch('sys.stdout', new=io.StringIO()))
        self.enterContext(patch('sys.stderr', new=io.StringIO()))

    def live(self, person=None, sources=('rm',)):
        person = person or self.owner
        with users.use(person):
            for source in sources:
                sweep._put_meta(f'sweep_base:{source}', main.settings_key(person))

    def pending(self, person=None, sources=('rm',)):
        person = person or self.owner
        return archive.pending(person.chat_id, list(sources), main.settings_key(person), 1000)

    def test_hard_exit_after_intent_commit_before_delivery_recovers(self):
        self.archive_sweep([advert(1)])
        self.live()
        pid = os.fork()
        if pid == 0:
            with patch.object(prefs, 'publish', side_effect=lambda *a: os._exit(73)):
                self.judge()
            os._exit(0)
        self.assertEqual(os.waitpid(pid, 0)[1] >> 8, 73)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.seen(), set())
        self.judge()
        self.judge()
        self.assertEqual(self.alerts, [('rm:1', 'new')])

    def test_hard_exit_after_seen_checkpoint_does_not_duplicate_delivery(self):
        self.archive_sweep([advert(1)])
        self.live()
        accepted = Path(self.tmp.name) / 'accepted'
        def accept(*args):
            with accepted.open('a') as f:
                f.write('accepted\n'); f.flush(); os.fsync(f.fileno())
        pid = os.fork()
        if pid == 0:
            with patch.object(telegram, 'send_listing', side_effect=accept), \
                    patch.object(telegram, 'throttle', side_effect=lambda: os._exit(73)):
                self.judge()
            os._exit(0)
        self.assertEqual(os.waitpid(pid, 0)[1] >> 8, 73)
        self.assertEqual(self.seen(), {'rm:1'})
        self.assertEqual(len(self.pending()), 0)  # per-advert receipt precedes throttle/batch completion
        self.judge()
        self.assertEqual(self.alerts, [])
        self.assertEqual(accepted.read_text(), 'accepted\n')
        self.assertEqual(self.pending(), [])

    def test_restart_between_silent_baseline_batches_preserves_count_and_silence(self):
        self.archive_sweep([advert(i) for i in range(1, 52)])
        original = main._run
        calls = [0]
        def stop_second(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError('restart between batches')
            return original(*args, **kwargs)
        with patch.object(main, '_run', side_effect=stop_second):
            with self.assertRaises(RuntimeError):
                self.judge()
        self.assertEqual(len(self.seen()), 25)
        self.assertIsNone(sweep._meta('sweep_base:rm'))
        self.assertEqual(self.judge(), {'rm':51})
        self.assertEqual(self.alerts, [])
        self.assertEqual(len(self.seen()), 51)

    def test_delivery_failure_keeps_durable_intent_and_retries_once(self):
        self.archive_sweep([advert(1)])
        self.live()
        with patch.object(telegram, 'send_listing', side_effect=RuntimeError('Telegram down')):
            with self.assertRaises(RuntimeError):
                self.judge()
        self.assertEqual(archive.verdicts_since('100', 1)[0]['outcome'], 'unsent')
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.seen(), set())
        self.judge()
        self.judge()
        self.assertEqual(self.alerts, [('rm:1', 'new')])
        self.assertEqual(self.pending(), [])

    def test_failure_after_one_send_does_not_lose_remaining_batch(self):
        self.archive_sweep([advert(i) for i in range(1, 4)])
        self.live()
        def fail_second(l, reason):
            if self.alerts:
                raise RuntimeError('second send failed')
            self.alerts.append((l.key, reason))
        with patch.object(telegram, 'send_listing', side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.judge()
        self.assertEqual(len(self.seen()), 1)
        self.judge()
        self.assertEqual(len(self.alerts), 3)
        self.assertEqual(len({key for key, _ in self.alerts}), 3)
        self.assertEqual(self.pending(), [])

    def test_failure_after_judging_before_sending_is_retryable(self):
        self.archive_sweep([advert(1)])
        self.live()
        with patch.object(prefs, 'publish', side_effect=RuntimeError('publish failure')):
            with self.assertRaises(RuntimeError):
                self.judge()
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.alerts, [])
        self.judge()
        self.assertEqual(len(self.alerts), 1)

    def test_no_delivery_without_durable_intent(self):
        self.archive_sweep([advert(1)])
        self.live()
        with patch.object(archive, 'record_verdicts', side_effect=RuntimeError('DB busy')):
            with self.assertRaises(RuntimeError):
                self.judge()
        self.assertEqual(self.alerts, [])
        self.assertEqual(len(self.pending()), 1)

    def test_alert_cap_is_shared_across_batches_and_retry_drains_without_duplicates(self):
        self.archive_sweep([advert(i) for i in range(1, 52)])
        self.live()
        with patch.object(config, 'MAX_ALERTS_PER_RUN', 2):
            self.judge()
            self.assertEqual(len(self.alerts), 2)
            for _ in range(30):
                before = len(self.alerts)
                self.judge()
                self.assertLessEqual(len(self.alerts) - before, 2)
        self.assertEqual(len(self.alerts), 51)
        self.assertEqual(len({key for key, _ in self.alerts}), 51)
        self.assertEqual(self.pending(), [])

    def test_alert_cap_is_shared_between_sources(self):
        self.archive_sweep([advert(1), advert(2), advert(3)])
        archive.upsert([advert(1, source='otmr')], 'head')
        self.live(sources=('rm', 'otmr'))
        with patch.object(config, 'MAX_ALERTS_PER_RUN', 2), patch.object(otm_rent, 'refine', return_value=True):
            sweep.judge(self.owner, ['rm', 'otmr'], 1000, clock=lambda: 0)
        self.assertEqual(len(self.alerts), 2)
        self.assertEqual(len(self.pending(sources=('rm', 'otmr'))), 2)

    def test_head_and_sweep_share_the_same_cycle_budget(self):
        self.archive_sweep([advert(i) for i in range(1, 5)])
        self.live()
        budget = {'remaining': 2}
        with users.use(self.owner), users.criteria(self.owner.params):
            main._run(['rm'], False, False, rentals=True, watch=True,
                      prefetched=([advert(1), advert(2), advert(3)], []), alert_budget=budget)
        self.assertEqual(len(self.alerts), 2)
        sweep.judge(self.owner, ['rm'], 1000, clock=lambda: 0, alert_budget=budget)
        self.assertEqual(len(self.alerts), 2)
        sweep.judge(self.owner, ['rm'], 1000, clock=lambda: 0, alert_budget={'remaining': 2})
        self.assertEqual(len(self.alerts), 4)
        self.assertEqual(self.pending(), [])

    def test_price_cut_invalidates_rejection_and_alerts_newly_eligible_flat(self):
        self.archive_sweep([advert(1, 1700)])
        self.judge()
        archive.upsert([advert(1, 1550)], 'sweep')
        self.assertEqual(len(self.pending()), 1)
        self.judge()
        self.assertEqual(self.alerts, [('rm:1', 'new')])
        self.assertEqual(self.pending(), [])

    def test_changed_facts_keep_seen_price_history_for_a_drop_alert(self):
        self.archive_sweep([advert(1, 1400)])
        self.judge()
        archive.upsert([advert(1, 1300)], 'sweep')
        self.judge()
        self.judge()
        self.assertEqual(self.alerts, [('rm:1', 'drop')])
        self.assertEqual(main.load_seen()['rm:1']['rent'], 1300)

    def test_revision_invalidates_all_people_not_their_independent_limits(self):
        friend = users.User('200', params={'rent': 1400})
        self.archive_sweep([advert(1, 1750)])
        self.judge()
        self.judge(friend)
        archive.upsert([advert(1, 1500)], 'sweep')
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(len(self.pending(friend)), 1)
        self.judge()
        self.judge(friend)
        self.assertEqual(archive.verdicts_since('100', 1)[0]['outcome'], 'matched')
        self.assertEqual(archive.verdicts_since('200', 1)[0]['outcome'], 'rent_cap')
        self.assertEqual(self.alerts, [('rm:1', 'new')])

    def test_per_person_estimates_do_not_create_false_advert_revisions(self):
        self.archive_sweep([advert(1)])
        self.judge()
        other = advert(1)
        other.bills = 999
        other.utilities_monthly = 900
        other.estimated = ['different bill estimate']
        archive.upsert([other], 'head')
        self.assertEqual(self.pending(), [])
        other.description = 'Now explicitly a room in a shared house'
        archive.upsert([other], 'head')
        self.assertEqual(len(self.pending()), 1)

    def test_truncated_report_does_not_acknowledge_unshown_delivery_intents(self):
        self.archive_sweep([advert(i, lat=51.52+i*.002) for i in range(1, 52)])
        self.live()
        with patch.object(config, 'MAX_ALERTS_PER_RUN', 2):
            self.judge()
        previously_seen = self.seen()
        with users.use(self.owner):
            sweep._put_meta('watch_initialized', 1)
            sweep._put_meta('watch_settings', main.settings_key(self.owner))
            with patch.object(main, 'send_report'):
                watch.Watcher(['rm'])._person_cycle('report', ([], []), None, False)
        reported_new = self.seen() - previously_seen
        self.assertTrue(reported_new)
        self.assertTrue(self.pending())
        self.assertTrue(all(l.key not in self.seen() for l in self.pending()))
        before_alerts = len(self.alerts)
        for _ in range(3):
            self.judge()
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.seen()), 51)
        self.assertTrue(reported_new.isdisjoint({key for key, _ in self.alerts[before_alerts:]}))

    def test_report_preserves_sweep_provenance_and_does_not_poison_verdict(self):
        self.archive_sweep([advert(1, 1400, lat=51.6452)])
        self.judge()
        with users.use(self.owner):
            sweep._put_meta('watch_initialized', 1)
            sweep._put_meta('watch_settings', main.settings_key(self.owner))
            with users.criteria(self.owner.params), patch.object(main, 'send_report') as report:
                result = watch.Watcher(['rm'])._person_cycle('report', ([], []), None, False)
        self.assertEqual(result['matches'], 1)
        self.assertEqual(report.call_args.args[0][0].key, 'rm:1')
        self.assertEqual(archive.verdicts_since('100', 1)[0]['outcome'], 'matched')
        self.assertEqual(self.pending(), [])

    def test_first_baseline_stays_silent_across_enrichment_failure_and_recovery(self):
        self.archive_sweep([advert(i) for i in range(1, 51)])
        with patch.object(enrich, 'journey', side_effect=RuntimeError('TfL outage')):
            self.assertEqual(self.judge(), {'rm': 0})
        self.assertEqual(sweep._meta('sweep_base:rm'), main.settings_key(self.owner))
        self.assertEqual(len(self.pending()), 50)  # first retry due, but SILENT membership is durable
        self.assertEqual(self.judge(), {})
        self.assertEqual(self.alerts, [])
        self.assertEqual(len(self.seen()), 50)
        archive.upsert([advert(99)], 'sweep')
        self.judge()
        self.assertEqual(self.alerts, [('rm:99', 'new')])

    def test_cap_expansion_waits_for_a_compatible_completed_generation(self):
        users.save_params(self.owner, {'rent': 1300})
        old = sweep.scope_for([self.owner])
        sid = archive.start_sweep('rm', old, {}, at(0))
        archive.upsert([advert(1, 1200)], 'sweep', sid, at(0))
        archive.finish_sweep(sid, True, at=at(.5))
        self.judge()
        old_marker = sweep._meta('sweep_base:rm')
        users.save_params(self.owner, {'rent': 1600})
        self.assertEqual(self.judge(), {})
        self.assertEqual(sweep._meta('sweep_base:rm'), old_marker)
        scope = sweep.scope_for([self.owner])
        sid = archive.start_sweep('rm', scope, sweep.initial_state(scope), at(12))
        archive.upsert([advert(i, 1550, lat=51.6452) for i in range(2, 52)], 'sweep', sid, at(12))
        self.assertEqual(self.judge(), {})
        self.assertEqual(self.alerts, [])
        archive.finish_sweep(sid, True, at=at(12.5))
        self.assertEqual(self.judge(), {'rm': 50})
        self.assertEqual(self.alerts, [])

    def test_newer_running_generation_prevents_premature_baseline(self):
        self.archive_sweep([advert(1)])
        sid = self.archive_sweep([advert(2)], hour=12, complete=False)
        self.assertEqual(self.judge(), {})
        self.assertIsNone(sweep._meta('sweep_base:rm'))
        archive.finish_sweep(sid, True)
        self.assertEqual(self.judge(), {'rm': 2})
        self.assertEqual(self.alerts, [])

    def test_deadline_is_checked_between_adverts_and_before_details_or_delivery(self):
        self.archive_sweep([advert(i, lat=51.52+i*.0005) for i in range(1, 26)])
        self.live()
        clock = [0]
        def slow(*args):
            clock[0] += 10
            return {'minutes': 25, 'route': 'Tube', 'station': 'S', 'walk': 5}
        with patch.object(enrich, 'journey', side_effect=slow) as journey, \
                patch.object(rightmove, 'refine', return_value=True) as details:
            sweep.judge(self.owner, ['rm'], 30, clock=lambda: clock[0])
        self.assertEqual(clock[0], 30)
        self.assertEqual(journey.call_count, 3)
        self.assertEqual(details.call_count, 2)
        self.assertEqual(len(self.pending()), 25)
        self.assertEqual(self.alerts, [])
        self.judge()
        self.assertEqual(len(self.alerts), 25)

    def test_zero_time_budget_starts_no_advert(self):
        self.archive_sweep([advert(1)])
        with patch.object(enrich, 'journey') as journey:
            sweep.judge(self.owner, ['rm'], 0, clock=lambda: 0)
        journey.assert_not_called()
        self.assertEqual(len(self.pending()), 1)

    def test_archive_failure_never_advances_page_or_counts_absence(self):
        archive.upsert([advert(1)], 'head', at=at(0))
        market = Market({1: 1400})
        with patch.object(rightmove, 'band_page', market.band_page):
            with patch.object(archive, 'upsert', side_effect=RuntimeError('SQLite busy')):
                for _ in range(2):
                    with self.assertRaises(RuntimeError):
                        drive()
            self.assertIsNone(archive.last_sweep('rm'))
            self.assertEqual(archive.running_sweep('rm')['state']['current']['page'], 0)
            self.assertEqual(archive.advert('rm:1')['missed'], 0)
            drive()
        self.assertTrue(archive.last_sweep('rm')['complete'])
        self.assertEqual(archive.last_sweep('rm')['requests'], 3)
        self.assertEqual(archive.advert('rm:1')['status'], 'active')

    def test_request_reservation_survives_hard_exit_and_cannot_exceed_cap(self):
        sid = archive.start_sweep('rm', SCOPE, sweep.initial_state(SCOPE))
        archive.save_sweep(sid, sweep.initial_state(SCOPE), 149)
        attempts = Path(self.tmp.name) / 'attempts'
        original = archive.upsert
        def page(*args):
            with attempts.open('a') as f:
                f.write('request\n'); f.flush(); os.fsync(f.fileno())
            return {'listings': [advert(1)], 'raw': 1, 'total': 1, 'more': False}
        def hard_exit(*args, **kw):
            original(*args, **kw)
            os._exit(73)
        pid = os.fork()
        if pid == 0:
            with patch.object(rightmove, 'band_page', side_effect=page), \
                    patch.object(archive, 'upsert', side_effect=hard_exit):
                drive()
            os._exit(0)
        self.assertEqual(os.waitpid(pid, 0)[1] >> 8, 73)
        self.assertEqual(archive.running_sweep('rm')['requests'], 150)
        with patch.object(rightmove, 'band_page', side_effect=page):
            drive()
        self.assertEqual(len(attempts.read_text().splitlines()), 1)
        self.assertEqual(archive.last_sweep('rm')['requests'], 150)
        self.assertFalse(archive.last_sweep('rm')['complete'])  # replay unavailable, never fake coverage
        self.assertIsNotNone(archive.advert('rm:1'))

    def test_default_and_policy_changes_change_fingerprint_without_other_user_leak(self):
        before = main.settings_key(self.owner)
        with patch.object(config, 'MAX_TRAVEL_MINUTES', 80):
            self.assertEqual(main.settings_key(self.owner), before)  # another user's in-place criteria
        with patch.dict(users.DEFAULTS, {'MAX_TRAVEL_MINUTES': 50}):
            self.assertNotEqual(main.settings_key(self.owner), before)  # service restart's defaults
        with patch.object(config, 'DEEP_SWEEP_MILES', 15):
            self.assertNotEqual(main.settings_key(self.owner), before)

    def test_unknown_legacy_policy_rebaselines_head_silently_then_alerts_new_homes(self):
        self.archive_sweep([advert(1)])
        self.judge()
        sweep._put_meta('watch_initialized', 1)
        with patch.object(sweep, 'step'), patch.object(rightmove, 'fetch', return_value=[advert(1), advert(2)]):
            watch.Watcher(['rm']).cycle()
        self.assertEqual(self.alerts, [])
        self.assertEqual(self.seen(), {'rm:1', 'rm:2'})
        with patch.object(sweep, 'step'), patch.object(rightmove, 'fetch', return_value=[advert(1), advert(2), advert(3)]):
            watch.Watcher(['rm']).cycle()
        self.assertEqual(self.alerts, [('rm:3', 'new')])

    def test_other_source_outage_does_not_stop_healthy_sweep_or_retry_queue(self):
        with users.use(self.owner):
            sweep._put_meta('watch_initialized', 1)
            sweep._put_meta('watch_settings', main.settings_key(self.owner))
        with patch.object(rightmove, 'fetch', return_value=[advert(1)]), \
                patch.object(main.SOURCES['orent'], 'fetch', side_effect=RuntimeError('OpenRent outage')), \
                patch.object(sweep, 'step') as step:
            with self.assertRaises(RuntimeError):
                watch.Watcher(['rm', 'orent']).cycle()
        step.assert_called_once()
        self.assertIn('100', step.call_args.kwargs['alert_budgets'])

    def unseen_event(self):
        archive.upsert([advert(1)], 'head', at=at(0))
        for hour in (12, 24):
            sid = archive.start_sweep('rm', SCOPE, {}, at(hour))
            archive.finish_sweep(sid, True, at=at(hour+.5))
        d1.query('INSERT INTO saved (key, info) VALUES (?, ?)', 'rm:1',
                 json.dumps({'url': 'https://example.com/1', 'address': 'Home 1'}))

    def test_unseen_event_survives_failed_delivery_and_is_acknowledged_once(self):
        self.unseen_event()
        with patch.object(telegram, 'send_text', side_effect=RuntimeError('Telegram down')):
            sweep.notify_unseen()
        self.assertEqual(len(archive.pending_unseen('100')), 1)
        sweep.notify_unseen()
        sweep.notify_unseen()
        self.assertEqual(len(self.texts), 1)
        self.assertEqual(archive.pending_unseen('100'), [])

    def test_unseen_retry_coalesces_superseded_absence_episodes(self):
        self.unseen_event()
        archive.upsert([advert(1)], 'head', at=at(30))
        for hour in (36, 48):
            sid = archive.start_sweep('rm', SCOPE, {}, at(hour))
            archive.finish_sweep(sid, True, at=at(hour+.5))
        self.assertEqual(len(archive.pending_unseen('100')), 1)
        sweep.notify_unseen()
        sweep.notify_unseen()
        self.assertEqual(len(self.texts), 1)

    def test_one_failed_chat_does_not_block_another_unseen_notification(self):
        self.unseen_event()
        friend = users.User('200', name='Bea')
        users.save_params(friend, {})
        with users.use(friend):
            d1.query('INSERT INTO saved (key, info) VALUES (?, ?)', 'rm:1',
                     json.dumps({'url': 'https://example.com/1', 'address': 'Guest save'}))
        sent = []
        def owner_failure(*args, **kw):
            if users.current_chat() == '100':
                raise RuntimeError('Owner chat failed')
            sent.append(users.current_chat())
        with patch.object(telegram, 'send_text', side_effect=owner_failure):
            sweep.notify_unseen()
        self.assertEqual(sent, ['200'])
        self.assertEqual(len(archive.pending_unseen('100')), 1)
        self.assertEqual(archive.pending_unseen('200'), [])

    def test_whatif_excludes_old_settings_and_already_fitting_values(self):
        archive.record_verdicts('100', [('rm:1', 'rent_cap', 1500, None, None, None)], 'old-cap-1400')
        self.assertIn('Nothing has been judged', status.whatif_text(self.owner))
        rows = [{'outcome': 'rent_cap', 'rent': 1500}, {'outcome': 'rent_cap', 'rent': 1640}]
        counts = status.whatif_counts(rows, 1600, 40, 120)
        self.assertEqual(counts['rent'], {50: 1, 100: 1})

    def test_shared_archive_connection_restores_guest_database_and_rolls_back_partial_upsert(self):
        friend = users.User('200', name='Bea')
        with users.use(friend):
            expected = localdb.database_path()
            archive.upsert([advert(1)], 'head')
            self.assertEqual(localdb.database_path(), expected)
            sweep._put_meta('guest_only', 1)
        self.assertIsNone(sweep._meta('guest_only'))
        bad = advert(3)
        bad.description = object()
        with self.assertRaises(TypeError):
            archive.upsert([advert(2), bad], 'head')
        self.assertIsNone(archive.advert('rm:2'))

    def test_per_person_minimum_rent_is_applied_to_shared_inventory(self):
        friend = users.User('200', params={'minrent': 1200})
        self.archive_sweep([advert(1, 1100)])
        self.judge()
        self.judge(friend)
        self.assertEqual(archive.verdicts_since('100', 1)[0]['outcome'], 'matched')
        self.assertEqual(archive.verdicts_since('200', 1)[0]['outcome'], 'min_rent')
        with users.use(friend):
            self.assertEqual(main.load_seen(), {})

    def test_transient_detail_failure_is_retryable_but_recovery_is_silent(self):
        for source, module, real in (('rm', rightmove, REAL_RM_REFINE),
                                     ('otmr', otm_rent, REAL_OTM_REFINE)):
            with self.subTest(source=source):
                item = advert(1, source=source)
                sid = archive.start_sweep(source, SCOPE, {})
                archive.upsert([item], 'sweep', sid)
                archive.finish_sweep(sid, True)
                with patch.object(module, 'refine', real), \
                        patch.object(net, 'page', side_effect=ConnectionError('temporary details outage')):
                    result = main._run([source], False, False, rentals=True, watch=True, baseline=True,
                                       sweep=True, prefetched=([item], []))
                self.assertTrue(result['complete'])  # one advert degrades, not the source/person
                self.assertEqual(result['degraded'], 1)
                self.assertEqual(len(self.pending(sources=(source,))), 1)
                with patch.object(module, 'refine', return_value=True):
                    sweep.judge(self.owner, [source], 1000, clock=lambda: 0)
                self.assertEqual(self.pending(sources=(source,)), [])
                self.assertEqual(self.alerts, [])

    def test_definitive_detail_404_is_not_an_endless_retry(self):
        self.archive_sweep([advert(1)])
        with patch.object(rightmove, 'refine', REAL_RM_REFINE), \
                patch.object(net, 'page', side_effect=HTTPError('withdrawn', response=Mock(status_code=404))):
            self.judge()
        self.assertEqual(archive.verdicts_since('100', 1)[0]['outcome'], 'details')
        self.assertEqual(self.pending(), [])

    def test_expired_unseen_deadline_preserves_notification_for_next_slice(self):
        self.unseen_event()
        with patch.object(telegram, 'send_text') as send:
            sweep.notify_unseen(deadline=0, clock=lambda: 0)
        send.assert_not_called()
        self.assertEqual(len(archive.pending_unseen('100')), 1)
        sweep.notify_unseen()
        self.assertEqual(len(self.texts), 1)

    def test_scope_expansion_restarts_crawl_without_waiting_twelve_hours(self):
        old = {**SCOPE, 'max_rent': 1430}
        sid = archive.start_sweep('rm', old, sweep.initial_state(old))
        market = Market({1: 1550})
        with patch.object(rightmove, 'band_page', market.band_page):
            drive()
        last = archive.last_sweep('rm')
        self.assertGreater(last['id'], sid)
        self.assertEqual(last['scope']['max_rent'], 1760)
        self.assertTrue(last['complete'])
        with archive._db() as db:
            previous = dict(db.execute('SELECT * FROM sweeps WHERE id = ?', (sid,)).fetchone())
        self.assertFalse(previous['complete'])
        self.assertIn('scope expanded', previous['note'])

    def test_changed_service_default_rejudges_silently_without_alert_flood(self):
        self.archive_sweep([advert(1)])
        with patch.object(enrich, 'journey', return_value={'minutes':45, 'route':'Tube', 'station':'S', 'walk':5}):
            self.judge()
            self.assertEqual(archive.verdicts_since('100', 1)[0]['outcome'], 'travel')
            with patch.dict(users.DEFAULTS, {'MAX_TRAVEL_MINUTES':50}), \
                    patch.object(config, 'MAX_TRAVEL_MINUTES', 50):
                self.assertEqual(len(self.pending()), 1)
                self.assertEqual(self.judge(), {'rm':1})
        self.assertEqual(self.alerts, [])
        self.assertEqual(self.seen(), {'rm:1'})

    def test_destination_and_utilities_are_independent_with_restored_config(self):
        before = config.TARGET_NAME, config.TARGET_LAT, config.MAX_RENT, config.RENT_BILLS_ESTIMATE
        friend = users.User('200', params={'bills':50, 'destination':{
            'name':'Shoreditch', 'lat':51.523375, 'lng':-.075246}})
        self.archive_sweep([advert(1, 1200)])
        def journey(*args):
            return {'minutes':50 if config.TARGET_NAME == 'Shoreditch' else 25,
                    'route':'Tube', 'station':'S', 'walk':5}
        with patch.object(enrich, 'journey', side_effect=journey):
            self.judge()
            self.judge(friend)
        owner_row = archive.verdicts_since('100', 1)[0]
        friend_row = archive.verdicts_since('200', 1)[0]
        self.assertEqual((owner_row['outcome'], friend_row['outcome']), ('matched', 'travel'))
        self.assertNotEqual(owner_row['all_in'], friend_row['all_in'])
        self.assertEqual(before, (config.TARGET_NAME, config.TARGET_LAT, config.MAX_RENT, config.RENT_BILLS_ESTIMATE))
        with users.use(friend):
            self.assertEqual(main.load_seen(), {})

    def test_cloud_sale_pipeline_keeps_details_first_and_performs_no_archive_io(self):
        sale = Listing('lq', '1', 'https://example.com/1', 'Sale home', 1, 'Shared Ownership',
                       share_price=100000, share_pct=25, rent=200, service_charge=100, lat=51.52, lng=-.14)
        costs.compute(sale)
        seen = {'lq:1': main._record(sale, '2026-01-01', alerted=True)}
        order = []
        env = dict(os.environ)
        env.pop('STORAGE', None)
        with patch.dict(os.environ, env, clear=True), \
                patch.object(archive, '_db', side_effect=AssertionError('cloud archive I/O')) as db, \
                patch.object(main, 'load_seen', return_value=seen), patch.object(main, 'save_seen'), \
                patch.object(prefs, 'load', return_value={'saved':{}, 'hidden':[], 'hidden_dev':[], 'feedback':[]}), \
                patch.object(prefs, 'save'), patch.object(prefs, 'publish'), patch.object(main.mapgen, 'write'), \
                patch.object(main.SOURCES['lq'], 'refine', side_effect=lambda l: order.append('details') or True), \
                patch.object(enrich, 'journey', side_effect=lambda *a: order.append('journey') or {
                    'minutes':25, 'route':'Tube', 'station':'S', 'walk':5}):
            result = main._run(['lq'], False, False, watch=True, prefetched=([sale], []))
        self.assertEqual(order, ['details', 'journey'])
        self.assertEqual(result['matches'], 1)
        self.assertTrue(result['complete'])
        db.assert_not_called()
        self.assertEqual(self.alerts, [])

    def test_cloud_gate_order_and_archive_disabled_with_storage_actually_unset(self):
        env = dict(os.environ)
        env.pop('STORAGE', None)
        with patch.dict(os.environ, env, clear=True), patch.object(archive, '_db') as db, \
                patch.object(rightmove, 'refine', return_value=True) as refine:
            self.assertFalse(main.staged(advert(1)))
            self.assertIsNone(main.passes_basic(advert(1)))
            refine.assert_called_once()
            db.assert_not_called()
