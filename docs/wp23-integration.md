# WP2 hardening + WP3 integration candidate

**Draft for BOSS review only. Not a deployment approval.**

## Inputs and PR stack

- WP2 archive: `dee641e`, PR #2 (base `wp0-rent-cap`).
- Hardening: `fe24507`, PR #7 (base `wp2-archive`).
- WP3 spec: `daab6bd`, branch `wp3-action`.
- WP3 implementation: `f9ffb04`, PR #6 (base `wp3-action`).
- This branch: `wp23-integration`, based on `wp2-hardening`, with a **local Git merge**
  of `wp3-build`. Neither input PR nor any production branch has been merged on GitHub.

The local merge was textually clean. In particular it retains hardening's archive
invalidation/event queues, shared alert allowance, deadlines, report settlement and effective
settings fingerprints, while adding WP3's larger image snapshot and pre-send urgency marking.
The commute-zone implementation is **not included**; the existing worktree used to run these
checks happens to be named `PisoBot-zone`. Published `commute-zone` is preserved unchanged.

## Validation

All commands ran in the integration checkout, using the existing main-checkout Python virtualenv.

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/quique/repo/PisoBot/.venv/bin/python -m unittest discover -s tests
```

- Initial combined suites: **399 tests passed in 4.992s**.
- With four new integration boundary checks: **403 tests passed in 5.045s**.
- `git diff --check` and staged equivalent: passed.
- A `ResourceWarning` about an unclosed SQLite connection is printed during the suite;
  it is not being represented as a clean warning-free run. Its allocation source needs
  investigation before assigning it to production code rather than a test fixture.

New `tests/test_wp23_integration.py` exercises:

1. Owner/guest `/set urgent` and `/profile` commands preserve the hardened effective-settings
   fingerprint and existing search baseline, with independent profile values.
2. A price change invalidates both people's verdicts, while the restored advert retains WP3
   phone, full listing timestamp and 16 images; the recent-listing urgency rule still works.
3. A saved pipeline home receives one unseen notice in the **sweep-first serial ordering**:
   hardening's durable event is acknowledged and WP3 suppresses its redundant pipeline notice.
4. A Telegram exception after real urgency assignment leaves a durable `unsent` intent; retry
   retains the top marker and successfully settles without a subsequent duplicate.

These are offline checks with temporary state and mocked external operations. They do not prove
live Telegram behavior, throughput, hardware readiness, exactly-once delivery or all possible
concurrent notification orderings.

## Independent reviews in progress

The coordinator assigned cross-reviews so neither author approves their own work:

- **mid** reviews WP3 / PR #6, including interactions with its hardening.
- **multi-use** reviews hardening / PR #7 against the original independent findings.

Their reports will be in the BOSS scratchpad as `wp3-independent-review.md` and
`wp2-independent-rereview.md`, with findings posted to the respective PRs. At initial publication,
those reviews are **pending**. A green combined suite is not a substitute for them.

## Remaining gates

- Resolve independently reproduced review blockers and rerun combined tests after fixes.
- Review unseen-notification concurrency and backwards/stale pipeline callbacks explicitly.
- Verify real Telegram rendering/reply/album behavior through an owner-approved procedure;
  no live Telegram requests were made here.
- Verify Rightmove contact payload assumptions, fractional portal price-band behavior and
  historical archive reconciliation deliberately; no portal probes were made here.
- Review additive DB changes, state backup and rollback. No backup, state migration, VPS
  access, deployment or cutover was performed.
- At-least-once delivery and cooperative network deadlines remain the documented limits in
  `docs/wp2-hardening.md`.

Original checkout `/Users/quique/repo/PisoBot` remains untouched and clean on `wp3-action`.
No credentials or real user state were read for this integration.
