# Archive hardening — round 3 (PR #7)

**Draft, pending independent re-review. Not approved for rollout.** Based on `6e21b11`.
This supersedes the round-2 report's terminal-retry and unconditional-upgrade-baseline policies.
No production deployment, state reset, live market/API request or PR merge was performed.

## Findings addressed

| Round-2 review finding | Change |
| --- | --- |
| F1 / P1: a two-cycle details/TfL outage permanently exhausts a new home | Active lookup failures never become terminal. The first retry waits 5 minutes, shared by head and sweep; later cooldowns increase to at most 1 hour. A successful lookup clears the consecutive failure count. Live versus silent provenance and delivery tokens survive outages. |
| F2 / P2: impossible notification jobs remain pending forever | Hidden adverts are suppressed before expensive lookups/intent creation, after duplicate-ID accounting. Existing hidden obligations are retired. A matched advert already seen with no current alert-worthy drop explicitly suppresses its obsolete obligation. Suppression clears the intent token but does not fabricate a new receipt in seen history. |
| F3 / P3: migration silently absorbs genuinely new head homes | A recognized legacy raw-params JSON marker, equal to current params, with an initialized watch and existing seen history is migrated without a quiet head baseline. Previously seen homes do not repeat; newly listed homes alert. Explicit `/set` resets, changed params and unknown markers remain conservative silent baselines. |
| F4 / P3: heartbeat hides enrichment outages | Source/loop heartbeat semantics remain separate, but persistent lookup degradation now sends an owner warning after `WATCH_FAILURE_LIMIT` consecutive cycles, once per incident, and a recovery notice when the current backlog clears. Failed warning/recovery delivery remains retryable. Active/current-context sweep-only retries are included; inactive/stale/other-person jobs do not hold the incident open. |
| F5 / P3: legacy jobs stay stuck | Superseded or already-seen notice conditions are explicitly suppressed. Round-2 `failed/enrichment_failed` rows become eligible again after a one-hour cooldown without needing changed advert facts. |

Additional scheduling fix: deadline-deferred work is not counted as a failed API call. It receives
a short cooldown without increasing its failure count, so an individually over-budget advert
cannot consume every following judging slice. Regressions retain new-home delivery assertions.
Existing delivery receipts are preserved when ordinary seen records are refreshed; only an
actual newer delivery writes a new token.

## Executed evidence

All tests use temporary state and mock external services. Python version: **3.14.8**.

- Full suite: **420 tests passed** (402 before this round).
- New `tests/test_hardening_round3.py`: **18 tests passed**.
- The same new file on an exported pre-fix `6e21b11`: **11 failures, 2 errors, 5 controls pass**.
  The two errors are new health-helper APIs absent before the fix; eleven tests reproduce
  incorrect observable behavior. This is not a claim that every test fails on old code.
- Original review script unchanged: **11 of 12 pass**. Its migration case simultaneously
  asserts `alerts == []` and `alerts == [('rm:3', 'new')]`, so it cannot pass a correct new-home
  notification. The ported test changes the first assertion to no repeats of rm:1/rm:2 and
  expects no new baseline announcement during the recognized live migration. The original
  review artifact is preserved unchanged.
- Older tests expecting an immediate retry now advance a virtual clock. Tests expecting a
  permanent failure after three attempts now assert bounded retry rate plus eventual recovery.
  The superseded-price test asserts explicit suppression while retaining the prior actual
  delivery receipt. Existing hidden-funnel expectations now count hidden exclusions before
  enrichment. No failure was hidden by removing a new-home or per-person-isolation assertion.
- `git diff --check`: passed.
- A known pre-existing unclosed SQLite fixture `ResourceWarning` still appears. Not a
  warning-free run. The separate evaluation venv also ran the suite successfully, but is
  **also Python 3.14.8**, not independent Python 3.12 coverage.

New checks cover the reviewer whole-cycle outage, hidden-building, superseded-drop, starvation,
legacy migration and send/crash controls; first-retry head/sweep separation; one warning and
recovery per incident; retry after warning-send failure; old terminal-job readmission; health
isolation; and ten consecutive outages with capped cooldown and stable intent identity.

## Independent re-review follow-up

The independent reviewer conditionally cleared the P1/P2 fixes, but reproduced a P3 false
alarm: cooperative deadline yields had `state=backoff` with zero API failures and were
counted as degraded. Fixed by counting only active/current-context retry rows with an actual
failure streak (or legacy failed state), and using that ledger count rather than the head
summary that also includes cooperative yields. A new whole-cycle regression proves repeated
zero-failure cooldowns do not trigger API-degradation warnings. Full suite after this fix:
**421 tests passed** on Python 3.14.8.

The owner notification explicitly says the count is **across all active searches**. This is
intentional shared-service operational monitoring, not a claim that the owner's personal
route is broken; per-person `/status` work remains separate. It does not disclose guest
addresses, renter profiles or settings.

## Policies and limits requiring review

- Cooldowns cap at one hour, not a promise of strict hourly delivery: watcher cadence,
  provider timeouts and per-person judging deadlines still affect when a due job executes.
- This does not add a provider-wide circuit breaker or prevent all distinct adverts from
  trying a failing provider once; existing paced networking/cooldowns remain responsible.
- Health warnings are based on unresolved degradation across consecutive completed cycles,
  not on a new external failure endpoint. A running source scan can still ping its heartbeat
  while lookup health is degraded, but the owner now receives a separate push warning.
- Suppression is deliberate current-condition cancellation, not exactly-once delivery.
  The usual crash-after-remote-send/before-local-ack duplicate window remains.
- Recognized pre-archive params migration assumes an initialized seen history and agreeing
  stored overrides. It cannot infer an unrecorded old environment-default change; review
  operator intent if defaults changed too. Unknown formats still rebaseline quietly.
- F6 overhead and F7 retention/pruning remain measurements/design follow-ups. No pruning
  was added: deleting history safely needs an explicit retention policy and migration plan.
- No automatic replay of historically unknowable notifications; no real user state was read.
- No Python 3.12 server-suite run or live deployment for this round. Independent re-review
  and a fresh integration with WP3 round 2 remain blockers; old PR #8 is not current evidence.
