# WP2 hardening after independent review

Branch: `wp2-hardening`, based on `dee641e`. Draft PR base: `wp2-archive`.
Review: [WP2 independent review](https://github.com/HenryCorn/PisoBot/pull/2#issuecomment-6764005268).

**Do not deploy or merge without BOSS review.** This is a local/offline hardening change,
not a production rollout. No zone estimates are used as a hard filter.

## Review findings: fixed / deferred

| Review finding | Status | Change and regression evidence |
| --- | --- | --- |
| P1 #1: matched verdict committed before delivery | Fixed | `unsent` is a durable delivery intent, written before publish/send/seen I/O. Only successful seen persistence, explicit silent baselining, visible report delivery, or no-alert/hidden suppression settles it. DB failure prevents delivery. Telegram errors, later-phase errors and actual child hard exits are covered. |
| P1 #2: advert changes never invalidate verdicts | Fixed | Decision-fact changes and reactivation invalidate every person's verdict transactionally with the advert update. Seen/rent history is retained for correct drop alerts. Per-person utilities/estimated-cost fields do not spuriously revise shared facts. |
| P1 #3: failed archive writes claim coverage | Fixed | Crawl uses strict archive writes; a failed write cannot advance the page/read count or finish a sweep. The reserved request remains charged. Retry reads the same page; incomplete work never contributes absence misses. |
| P1 #4: reports reject outer-zone sweep matches | Fixed | Report extras carry per-listing sweep provenance into the gates. Their small head-poll radius is not reapplied. Actual report-path regression checks the result and stored verdict, not just input keys. |
| P1 #5: premature silent baseline | Fixed | Retryable rows cannot be hidden by invocation-local skips when testing drainage. A compatible completed scope/generation is required, with no newer running generation. Scope expansion starts a new crawl immediately, instead of using an old narrower completion or waiting 12 hours. Found counters reset with settings generation. Failure/recovery, restarts between batches and cap-expansion regressions stay silent. |
| P1 #6: alert allowance resets per batch | Fixed | One mutable allowance per person per watcher cycle is shared by the head poll and every batch/source. Excess intents stay pending. Tests drain 51 matches under cap 2 without loss/duplicates and cover cross-source/head+sweep sharing. |
| P1 #7: whole slow batch overruns time slice | Fixed | Deadline passed into the per-advert loop, checked before the next advert, crime lookup, details and delivery. A deferred judgement remains retryable. Slice orchestration and unseen-message draining also respect the overall deadline. Simulated 30-second budget now stops after three 10-second lookups, not all 25. |
| P2 #8: crash erases consumed request budget | Fixed | Reserve each attempt in SQLite with an immediate transaction **before** network I/O. A hard exit after page commit keeps the charge. Replay is idempotent and consumes budget; exhaustion is reported incomplete, never free extra requests. Actual `os._exit` test starts from 149 and cannot issue a request beyond 150. |
| P2 #9: fingerprint ignores defaults/policy | Fixed | Hash immutable effective per-person defaults/overrides/destination plus a judgement-policy version and sweep radius. Do not read another person's temporarily mutated config as defaults. Head and sweep baselines use the same fingerprint. Unknown legacy head markers receive one silent rebaseline without clearing history. |
| P2 #10: unrelated head failure blocks healthy sweeps | Fixed | Run the independently bounded sweep/retry step even when another head source/person failed. Head health still reports/raises the failure; healthy matches and queues can progress. |
| P2 #11: unseen message lost after status commit | Fixed for new transitions | Persist an event in the same transaction as the unseen transition and track acknowledgement per person. Retry every slice; a failed chat does not block the other. Superseded absence episodes are coalesced. Tests cover failed delivery, retry/ack, multiple chats and deadline yield. Historical pre-hardening transitions are not backfilled automatically (see limits). |
| P2 #12: what-if counts stale/already-fitting rows | Fixed | Use only the current settings fingerprint; require current limit < metric <= relaxed limit. Label the sample historical and approximate, not lower/upper bounds or current availability. |

No confirmed review finding #1–12 is deliberately left unfixed for future code work.
The external-service/upgrade limitations below remain explicit.

## Additional correctness fixes found while hardening

- A truncated local rental digest must not mark every candidate as delivered. Only top
  cards, all visible saved entries and the formatter's limited rental alternatives are
  acknowledged. Unshown new/drop matches remain `unsent`; the next scan sends them without
  repeating ones actually shown. Cloud/sale report semantics are unchanged.
- Enforce each person's `minrent` when judging the shared archive, not just when portals
  originally fetch it.
- Distinguish temporary Rightmove/OnTheMarket details failures from genuine detail
  exclusions on the local path. Temporary failures remain retryable and silent during
  initial baselining; definitive HTTP 404/410 exclusions are not retried indefinitely.
  Source refine's boolean API and cloud rejection outcomes remain unchanged.
- The zone hook is still a no-op, with its comment corrected to BOSS's **ordering only**
  decision. This PR neither imports zone.py nor changes lookup ordering.

## File-by-file

- `pisobot/archive.py`: decision-fact invalidation; current-settings verdict queries;
  durable request reservation; additive unseen event/delivery tables and latest-event query.
- `pisobot/main.py`: durable delivery intents and settlement; report provenance/visible
  acknowledgements; shared budget; per-advert/stage deadlines; effective fingerprint;
  local min-rent and transient-detail outcomes.
- `pisobot/sweep.py`: compatible baseline generations; truthful persistence/resume;
  shared allowance/deadlines; immediate scope expansion; retryable per-person unseen events.
- `pisobot/watch.py`: shared cycle budget; healthy-source sweep independence; provenance;
  fingerprinted silent policy migration.
- `pisobot/status.py`: current-settings, strictly additional what-if increments and honest labels.
- `pisobot/sources/rightmove.py`, `pisobot/sources/otm_rent.py`: ephemeral detail-failure signal,
  not a persisted market fact.
- `tests/test_hardening.py`: 40 new offline regressions ported/extended from the review repros.
- `tests/test_sweep.py`, `tests/test_users.py`: update three obsolete baseline/fingerprint/
  what-if expectations and the legacy migration expectation. No production failure is
  hidden by weakening the new regressions.
- `docs/wp2-hardening.md`: this versioned implementation/test/operations report.

## Tests

From `/Users/quique/repo/PisoBot-review`, using the main checkout's existing virtualenv:

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/quique/repo/PisoBot/.venv/bin/python -m unittest discover -s tests
```

Printed result:

```text
Ran 373 tests in 4.508s

OK
```

The previous suite had 333 tests. The 40 additions include actual child hard exits before
delivery, after seen persistence, and after page persistence; simulated-clock deadline tests;
two-person limits/destinations/utilities/database routing; atomic rollback; lost-send retry;
truncated reports; silent recovery and scope expansion; and an actual-STORAGE-unset shared-
ownership pipeline test verifying details-before-journey and zero archive DB access.

All fixtures block real network requests. No live portals, TfL, Telegram, VPS, deployment
or secret operations were performed. Only the explicitly requested GitHub push/draft-PR
publication is permitted for this task.

## Remaining limits / deployment blockers for BOSS

1. **Independent re-review and rollout approval remain blockers.** In particular review the
   changed delivery lifecycle, baseline migration, and combined head+sweep cap against WP3.
   This PR is draft and must remain unmerged.
2. **At-least-once, not exactly-once Telegram delivery.** A crash after Telegram accepts but
   before seen/DB acknowledgement can duplicate a message. The durable intent prevents
   permanent loss; no Telegram idempotency key exists here. The after-seen crash test does
   avoid duplication. Manual multi-message reports have the same acknowledgement ambiguity.
3. **Deadlines are cooperative.** They stop the next advert/lookup stage, not an already
   running HTTP retry, image fetch or Telegram operation. Existing network timeout/retry
   policies still bound those operations. This is not a strict 120-second process kill or
   a new TfL requests-per-minute limiter; no sustained live throughput claim is made.
4. **Conservative initial baselines can take time.** Failed initial enrichment/details rows
   remain quiet until resolved or removed from active inventory. This prioritizes no flood;
   new outer-zone notices can be delayed while an initial generation drains. The summary
   counter is best effort across the narrow crash window after seen persistence but before
   its counter update; delivery state remains safe even if that count under-reports.
5. **No automatic historical notification replay.** Experimental databases from unhardened
   WP2 may already contain unseen transitions with no event, or matched verdicts whose old
   delivery outcome is unknowable. They are not blindly replayed, avoiding a stale flood.
   The new policy fingerprint silently rejudges archive eligibility. If such a database is
   used, BOSS must decide any historical recovery/reconciliation explicitly.
6. **Fractional price-band contract remains unverified/deferred.** The review's separate
   synthetic £1,380.50 gap risk is not one of its confirmed P1/P2 findings. Integer portal
   price-band splitting is unchanged. Confirm whether each portal rounds/buckets monthly
   rent, or design conservative overlapping bands, before claiming penny-exact coverage.
   No live calls were made to guess that contract.
7. **Upgrade/rollback needs a consistent state backup and a reviewed plan.** The local
   schema change is additive and `worker/schema.sql` is untouched, but the new `deferred`
   outcome and event queues are not understood by old WP2. Do not roll back and assume pending
   work remains scheduled. Preserve the primary DB, its WAL consistently, and all per-user
   state/seen files using the approved operator procedure; no backup/deployment was run here.

The unchanged cloud/sale gates and mortgage path are regression-tested. No state was reset,
no user preferences were migrated into another person's profile, and no market requests
were made to validate assumptions.
