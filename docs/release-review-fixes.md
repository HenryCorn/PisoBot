# Final release seam corrections (PR #17)

**Pending independent re-review. Not a rollout approval.** Base: `24c5f41`.
Production remains `47e1eb6`, with guarded AI and 300 new requests/day.

The independent final review exercised compositions beyond the green 604-test source suite.
This change ports its ten methods into `tests/test_release_review_seams.py` and fixes the
four reproduced classes without changing live preferences, AI policy or production state.

## Findings and changes

1. **Repeat unseen episodes after sweep-only or filtered reappearance.** The durable event is
   always passed through shared episode acknowledgement (`tracker.once`), even if the old
   saved-home display flag is still true. The display flag is not a delivery receipt. Both
   unseen -> sweep-only-active -> unseen and unseen -> filtered-head-active -> unseen now
   notify for the second episode; delivered episode dedupe is retained.
2. **Poisoned first sweep batch starves later healthy inventory.** A completed `_run` batch
   that failed only at sender delivery attaches its persisted batch result to the original
   exception. `judge` records the failure, skips that invocation's processed keys, and uses
   remaining time/attempt allowance on later batches/sources. It still raises the delivery
   failure at the end so cycle health stays incomplete. Storage/evaluation failures receive
   no marker and stop that judgement path. Pending ordering places already-failed delivery
   work behind unattempted inventory, so a failed prefix cannot win every later cycle.
3. **Unseen-only output failures report a healthy cycle.** `notify_unseen` retains per-person
   events and collects errors while continuing other people. Both step drains aggregate the
   errors into the existing cycle result. Errors from eager end-of-crawl notification and
   other propagated crawl exceptions are also retained, including when a failed call consumes
   the remaining deadline. The watcher therefore withholds a healthy heartbeat/last-success
   update and uses its existing incomplete-cycle warning policy.
4. **Permanent pipeline rejections evade the per-tick cap.** `notify_changes(limit=...)` bounds
   attempted messages, not just successes; its return value still counts actual successful
   deliveries. A per-person non-search cursor rotates attempted rows so a permanently rejected
   prefix cannot starve the rest on every tick. Failed messages are never acknowledged as sent,
   and the cursor does not alter search settings/fingerprints.

## Executed evidence

- Before fixes, the independent ten-method harness had **five failing assertions/subcases**
  across the four classes; prior source suite was green. Reviewer artifacts remain unchanged.
- Ported harness after fixes: all ten methods pass.
- Added two further methods: rejected pipeline prefix rotates across all 15 homes without
  fabricated sent acknowledgements or fingerprint changes; a crawl output/storage exception
  remains unhealthy when the deadline has already been consumed.
- Full offline suite: **616 tests pass** (604 before this correction).
- Existing 13 adverse whole-cycle/crash controls and shipping preferences/AI seam tests remain
  in the full suite. Real HTTP remains stubbed/blocked by the fixtures; no paid calls, real
  credentials or live state used.
- `git diff --check` passes. Existing unclosed SQLite fixture ResourceWarnings remain; this is
  not a warning-free claim.

## Limits and operator gates

- Completed batch metadata is in-process exception context, not a new durable queue. Actual
  delivery obligations remain in the existing SQLite ledger and seen receipts; a hard process
  exit still relies on those records, not the exception attribute.
- A failure at a storage boundary is not treated as a finished batch or suppressed as a sender
  error. Successful earlier sends may retain the documented at-least-once acknowledgement gap.
- Notice leases and shared acknowledgements retain their existing fencing and crash semantics.
  This is not an exactly-once Telegram delivery guarantee.
- Sweep deadline checks remain cooperative; in-flight I/O cannot be pre-empted by this change.
- First-baseline summary messages retain the existing best-effort delivery/counter behavior;
  the durable listing intents and episode notifications are not a claim that every auxiliary
  summary is transactionally queued.
- Initial migration/restart/restore rehearsal is separate. A plain old-code checkout on a
  new state database is NOT a proven lossless rollback; preserve state/offsets and follow the
  coordinator's operator plan rather than assuming additive tables are sufficient.
- Re-run the exact new candidate through independent review and retargeted rehearsal before
  any server rollout. No application deployment or Pi cutover was performed here.
