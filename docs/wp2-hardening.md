# WP2 hardening — round 2 (PR #7)

**Draft; not a rollout approval.** This supersedes the round-1 liveness/baseline claims in
the original `docs/wp2-hardening.md`. Base of this round: `fe24507`; PR base remains
`wp2-archive`. No merge, deployment, production state operation or live market/API request
was performed.

## Independent re-review: finding-by-finding

| Finding | Fix | Executed regression evidence |
| --- | --- | --- |
| **F1 / P1 — quiet sweep absorbs head alerts** | New durable per-person/per-advert work provenance is separate from verdicts. A live notification promise is sticky across quiet runs, settings changes and advert revisions. A baseline cannot acknowledge it. Quiet evaluators deliver it under the shared allowance, or retain it pending without writing an alerted seen record. Non-baseline lookup provenance is persisted before external lookup I/O as well. | Ported reviewer **A1, A2, A3**. Added whole-cycle cap + quiet sweep + adverts falling off later head pages; zero-budget/settings-change baseline; storage-level refusal of silent acknowledgement; actual child hard exit during lookup; identical-criteria owner/guest provenance isolation. |
| **F2 / P1 — bad detail blocks a person's head baseline** | Local per-advert lookup/parser errors are degraded work, not source failure. A source-healthy head baseline completes and only its successfully evaluated silent members are recorded. The failed member's silent provenance survives global go-live, so recovery does not falsely announce an old baseline home. | Ported reviewer **B**. Whole-cycle tests assert both head/sweep markers go live with a bad member, later good new homes alert, recovery of the original member stays silent, and a parser exception affects only that advert. |
| **F3 / P2 — persistent detail suppresses healthcheck** | Heartbeat/completion measures successful source/loop work, independently of per-advert lookup quality. Degraded head counts are stored separately and shown in /status. Each failed advert has bounded retry/backoff and a visible terminal state; no per-advert error increments the global failed-cycle warning path. Genuine source, storage and delivery-channel failures remain operational failures. | Ported reviewer **C1** verifies all 3 heartbeats. Added controlled-clock whole-cycle test: backoff observed by head AND sweep, exactly 3 failed logical attempts, continued heartbeat/last-success updates, and /status displays the property and enrichment_failed. |
| **F4 / P2 — one retry keeps sweep quiet forever** | Global baseline drainage waits for unclassified inventory, not already classified retry/delivery work. Per-member silence is durable, so a failed initial member can retry after the source goes live without absorbing newer homes. Repeated failures/deadline yields terminate after the retry budget. Unattempted inventory outranks recurring lookup retries; confirmed delivery intents retain priority. Drainage is checked even when the last member consumed the slice. | Ported reviewer **C2**. Added a permanently over-budget advert case proving other inventory is classified, source goes live, newer work remains a live intent and is delivered (not silently consumed), while the bad member reaches a visible final state. |

All **seven original reviewer reproductions pass unchanged**, including their whole
`Watcher.cycle` compositions. They are also ported into
`tests/test_hardening_round2.py`, not merely run from the scratchpad.

## Lifecycle invariant and data model

An additive `advert_work` table in the **primary** archive DB is keyed by
`(chat_id, key)`. It records the facts/effective-settings context, notification obligation,
silent membership, failure count, next retry time and state. No private renter-profile
fields are stored in the shared market snapshots.

- `evaluating`: provenance recorded before external lookup I/O. Restarting a live-origin
  lookup under a quiet judge does not turn it into baseline inventory.
- `pending`: a due evaluation/delivery, with its notification/silence class retained.
- `backoff`: lookup failed/yielded; do not hammer it again from another path in the same interval.
- `failed`: budget exhausted, verdict `enrichment_failed`; visible in /status, no further
  automatic attempts for the same context. A live notification promise remains retained.
- `blocked`: a notification obligation remains, but current criteria reject the advert.
  It is not marked delivered/seen. A changed context can reconsider it.
- `done`: successfully acknowledged notification, intentionally silent baseline member,
  or a resolved non-notifying judgement.

Verdict + work-state updates are transactional. A verdict alone is not a receipt.
Each live obligation has a unique receipt identity. Actual sends/visible reports stamp that
identity into the per-user seen record; only that exact identity can recover an interrupted
database acknowledgement. Same price, a previous notification, or a silent baseline record
cannot prove that a DIFFERENT outstanding obligation was sent. Regressions cover price
reversion under a quiet run and an actual hard exit between seen persistence and DB ack.
`notify` cannot be cleared by ordinary baseline bookkeeping; the storage API rejects
`baseline=True, acknowledged=True`. Actual sends/reports and durable prior receipts use
explicit acknowledgement. Seen state is saved before acknowledging a delivered card.
A changed context resets lookup admission **without erasing a live notification promise**.

Retry policy: **3 consecutive failed logical lookup attempts**, first retry immediately,
then at least **5 minutes** before the final attempt. Both head and sweep obey the same
clock. Deadline yields count too, so one perpetually over-budget member cannot monopolise
work forever. Successful judgement resets the failure streak. New relevant facts,
reactivation, effective criteria or judgement-policy revision allow fresh lookup admission.
The fingerprint policy is now `wp2-hardening-2`.

This is not a promise of exactly-once Telegram delivery or a strict raw-HTTP-attempt cap:
an interrupted in-flight operation may need replay because its outcome is unknown, and
existing HTTP helpers can retry internally. No completed delivery intent is silently
converted into a baseline receipt.

## Lower-severity / inferred section of the re-review

| Concern | Disposition and evidence |
| --- | --- |
| Already delivered head/sweep matches rejudged after a later send fails | **Fixed in the normal receipt path.** A per-card receipt is committed immediately after durable seen persistence, not after the batch. Existing already-seen unchanged matches do not create new delivery intents. `test_completed_receipts_are_not_rejudged_after_another_send_fails` selects only the two actually undelivered members. Storage failure at the receipt itself can still require conservative re-evaluation; seen prevents an ordinary repeat send. |
| RM backlog starves OTM | **Mitigated explicitly.** Divide the person's remaining time across sources and rotate the starting source in durable per-person metadata. Tests show the other source gets work despite a slow RM member and starts first next time. |
| Fixed-order people / unseen queue only gets leftover time | **Mitigated explicitly.** Rotate starting people for judging and unseen delivery using primary-DB cursors; reserve 10% of the slice for unseen work before judging can consume it. Tests cover a slow first chat, an in-flight operation consuming the slice, and actual step ordering. In-flight operations cannot be pre-empted, so rotation bounds starvation across calls, not every-call latency. |
| Upward cap/minrent changes restart quiet generations | **Intentionally retained for honest coverage.** The new work ledger makes quiet-generation resets unable to consume existing live intents. The zero-budget + changed-settings regression proves this. New unclassified inventory still receives the intended initial silent treatment; the per-cycle pacing and per-generation search request cap remain. |
| No realistic poisoned upsert example | **No speculative recovery added.** Strict archive persistence remains necessary for real DB/I/O failures. No page is advanced or complete absence coverage claimed after a failed write. |

## Test evidence

Using only `/Users/quique/repo/PisoBot-review`, on `wp2-hardening`, with the main checkout's
existing virtualenv:

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/quique/repo/PisoBot/.venv/bin/python -m unittest discover -s tests
```

```text
Ran 402 tests in 8.935s

OK
```

This is **29 new round-2 tests** over the previous 373. The original reviewer script was
also rerun without edits:

```text
Ran 7 tests in 0.965s

OK
```

The ported tests retain the reviewer labels A1/A2/A3/B/C1/C2 plus control. Additional tests
cover per-person work isolation, direct storage invariants, interrupted lookups, backoff and
terminal states, successful recovery/reset, reactivation, delivery after head-page disappearance,
zero quotas/settings changes, parser errors, cooperative deadlines, fairness and dry runs.
Existing tests that expected an advert-level error to block the global baseline/heartbeat
were updated to the new explicitly requested policy; they still assert silent member recovery
and absence of duplicate sends.

All external portal/TfL/Telegram/healthcheck operations in these tests are fixtures/stubs.
No real property, chat, VPS or credential operations were performed. Only the requested
GitHub push and PR update access GitHub.

## Retained round-1 fixes

The original archive/revision, truthful coverage, report provenance/visible settlement,
cross-batch/person-cycle delivery allowance, request reservation before portal I/O,
current-settings what-if queries and durable per-person unseen-event queues remain.
Cloud/sale gate order, criteria, mortgage behaviour and disabled archive path remain covered
by the full suite. This branch does not import zone.py or use it as a hard filter.

## Files changed in this round

- `pisobot/archive.py`: additive work ledger, atomic provenance/verdict lifecycle, bounded
  retry clock, classified drainage, priority, sticky obligations and context admission resets.
- `pisobot/main.py`: lookup-start provenance, shared retry admission, per-member silence,
  explicit/per-card acknowledgement, local degraded-vs-source completion and safe parser errors.
- `pisobot/sweep.py`: work-aware go-live, correct silent-only summary counts, fair source/person
  scheduling and reserved/rotating unseen delivery.
- `pisobot/watch.py`: aggregate and persist degraded-advert diagnostics without suppressing a
  successful source heartbeat.
- `pisobot/status.py`: own-user pending/backoff/exhausted/blocked states and up to five exhausted
  advert links with the bounded attempt count.
- `tests/test_hardening_round2.py`: seven ported independent reproductions and twenty-two additional
  lifecycle/fairness/composition regressions.
- `tests/test_hardening.py`, `tests/test_sweep.py`, `tests/test_watch.py`: replace the obsolete
  whole-baseline-blocking expectations; reflect earlier per-card receipts.
- `docs/wp2-hardening.md`: this current report, replacing the inaccurate round-1 limit 4.

## Remaining limits / BOSS gates

1. **Independent re-review and rollout approval remain blockers.** In particular review this
   provenance/state transition model and rerun the independent checks after incorporation into
   the WP3 integration candidate. PR #7 remains draft and must not be merged automatically.
2. **Historical fe24507 state cannot be magically repaired.** Already-absorbed alerts left only
   `matched` + `seen.alerted` without a delivery log; that is not enough to distinguish a real
   send from the old quiet absorption. No production state was read/reset. BOSS must decide any
   historical reconciliation. Legacy retryable rows lacking provenance are conservatively
   adopted as live obligations rather than silently acknowledged; this may surface old work
   under the ordinary delivery cap and needs an approved migration decision.
3. **Terminal retry is explicit, not endless polling.** Exhausted same-context lookups stay visible
   and stop consuming attempts. Facts/reactivation/effective-settings/policy changes reset
   admission; an unchanged-context manual per-advert retry command/UI is not added here.
   A live obligation stays pending, including when it is currently blocked by criteria or a
   superseded price change is not currently deliverable. There is no implicit stale-intent
   cancellation: inventing a receipt would violate the requested invariant. A future explicit
   cancellation/reconciliation policy is BOSS's decision.
4. **Real incomplete coverage still cannot go live dishonestly.** A first covering generation can
   remain quiet if the portal/source crawl never completes; that is not one bad advert holding
   it open. Initial classified silent members intentionally do not receive later historical
   new-home alerts for the same context. After compatible coverage/classification, genuinely
   newer work is live even while old members are backing off/exhausted.
5. **At-least-once delivery and cooperative deadlines remain.** Remote acceptance before a local
   receipt can cause a repeat, and an in-flight HTTP/image/send operation can overrun a slice.
   Quotas are not consumed by silent members, retries or failed sends; they are shared across
   head/sweep batches for actual individual delivery. A slow attempt can delay a ready notice
   to the next slice, but cannot silently acknowledge it.
6. **Heartbeat now means source/loop health, with separate lookup-quality diagnostics.** A genuine
   source, storage or delivery-channel error still fails the cycle. Widespread external
   enrichment failure is visible as degraded/exhausted advert counts, not falsely claimed to
   be good enrichment just because the process heartbeat pings.
7. **WP3 fixes are outside this branch.** The independent WP3 blockers and duplicate notification
   ownership seam (pipeline-first unseen/rent notices) are not claimed fixed by this work.
   Integrating this new commit into PR #8 requires fresh combined tests/re-review; the old
   `6a77f33` candidate still contains the original round-1 regressions until updated.
8. Fractional portal-price contract, source cache freshness, additive-schema backup/WAL and
   rollback reconciliation remain operator/review gates from the prior report. Old code ignores
   this new provenance ledger; rollback must not be assumed to preserve notification semantics.
   No backup, migration of real state, deployment or zone integration was performed.
