# VPS rollout and soak

Status: **deployed; 24-hour soak completed on 2026-11-10**.
Current application: `65a223f`, branch `vps-rentals` (rental filtering and diagnostics).
Current version first started: 2026-11-09 07:26:21 UTC.
Restart-persistence check started: 2026-11-09 07:28:11 UTC.
Latest configuration restart: 2026-11-09 07:42:33 UTC (guest invitations disabled).
Completed uninterrupted 24-hour observation window: 2026-11-09 07:42:33 to
2026-11-10 07:42:33 UTC.

## 24-hour soak result

- Audited the full window after its end. The service remained active under
  `pisobot`, with **0 systemd restarts**, **92 completed rental cycles** and
  **0 failed cycles**. Latest observed cycle completed at 07:33:55 UTC with
  14 current matches.
- No portal block signals were logged. `portal_fetch` recorded **1,164 HTTP
  responses** total:

  | Host | HTTP 200 | HTTP 301 | Total |
  | --- | ---: | ---: | ---: |
  | Rightmove | 283 | 0 | 283 |
  | OnTheMarket | 283 | 0 | 283 |
  | OpenRent | 390 | 16 | 406 |
  | SpareRoom | 184 | 0 | 184 |
  | L&Q | 8 | 0 | 8 |
  | **Total** | **1,148** | **16** | **1,164** |

- Ten new/drop alert events were logged. A URL-level check found **no repeated
  alert URLs** in the window. This does not claim that cross-portal adverts
  with different URLs were deduplicated.
- Four transient Telegram warnings occurred: two polling/handling retries and
  two callback-acknowledgement failures. The watcher continued, recorded no
  failed rental cycles, and stayed active. These are worth keeping in mind;
  Telegram delivery/acknowledgement is not guaranteed by the clean scan count.
- Cgroup memory at the end of the audit was approximately **68 MiB**. This is
  a point-in-time reading, not a measured 24-hour peak or range.
- Conclusion: the application completed a clean 24-hour scan soak with
  sensible request volume and no observed portal blocking or duplicate alert
  URLs. Telegram emitted transient warnings; no external uptime monitor or
  full memory history is configured.

## Invitation correction

The configured invitation was a nickname, not the intended person's Telegram
username. With approval, cleared `TELEGRAM_INVITE_USERNAMES` and restarted the
service at the time above. No account had registered under that invitation.
Owner access remains configured; new guest admission is disabled until the
correct username is supplied. The previous environment was backed up privately
at `/var/backups/pisobot/pre-invitation-clear-20261109.env`.

## Rental filtering and diagnostics

- Deployed the approved first batch as `65a223f`: contextual text exclusions
  and visible ambiguity warnings, rental deduplication by exact `source:id`,
  accurate zero-estimated-bills labels, and per-profile/source filtering
  diagnostics persisted even for incomplete baselines.
- Budget semantics, search settings, source selection, baseline policy and
  notification timing are unchanged. Cross-portal copies may now appear rather
  than risking suppression of distinct homes with similar prices/map pins.
- All 218 tests passed locally and under VPS Python 3.12 as `pisobot`.
  The temporary validation worktree was removed.
- Private pre-upgrade backup:
  `/var/backups/pisobot/pre-funnel-20261109T072606Z/`.
  State and environment files were byte-for-byte unchanged before startup.
  Settings, saved/hidden choices, feedback and baseline fingerprints remained
  unchanged after both observed cycles; all previously seen IDs were retained.
- First filtering diagnostic persisted at 07:27:20 UTC: **11 visible matches
  plus 2 hidden**, compared with the previous version's latest 9 visible.
  Two new alerts were delivered and checkpointed. Restart filtering completed
  at 07:28:50 UTC with the same matches and **zero new/drop alerts**.
  Both cycles completed without source failures, logged errors, block signals
  or automatic restarts. Memory cgroup readings were approximately 32 MiB.
- The first scan's persisted pipeline counts were:

  | Source | Candidates | Cost rejected | Other rejected | Matched before hiding | Hidden |
  | --- | ---: | ---: | ---: | ---: | ---: |
  | Rightmove | 74 | 65 | 5 | 4 | 0 |
  | OnTheMarket | 63 | 54 | 8 | 1 | 0 |
  | OpenRent | 56 | 43 | 5 | 8 | 2 |
  | SpareRoom | 0 | 0 | 0 | 0 | 0 |
  | L&Q | 0 | 0 | 0 | 0 | 0 |
  | **Total** | **193** | **162** | **18** | **13** | **2** |

  Cost remains the largest measured pipeline gate. These counts exclude
  portal/adapter exclusions and inventory beyond the page cap; they do not
  measure the entire available market.
- `portal_fetch` counts were 18 on the first scan (Rightmove 4, OnTheMarket 3,
  OpenRent 9, SpareRoom 2) and 12 after restart (3, 3, 4, 2 respectively).
  L&Q used its persisted cache. Enrichment/images are not all covered by this
  counter. A full daily request-volume measurement remains pending.

## Multi-user upgrade

- Reviewed and merged `f2da3b5`, then fixed integration issues in `c991598`.
  All 185 tests passed locally and under the VPS's Python 3.12 as `pisobot`.
- Identical portal filters share a search; different filters have separate
  three-page caps. This preserves independent coverage instead of letting a
  wider union query displace the owner's inventory. Two different searches
  can roughly double search-page traffic; detail/enrichment caches are shared.
- Fixed concurrent settings/baseline and report-recipient races, durable access
  revocation, the zero-bills detail-cache case, and radius/destination handling.
  The local rental CLI also honours the owner's persisted settings.
- Before stopping the original service, validated the new code in a temporary
  VPS worktree. That checkout was removed after validation.
- Private pre-upgrade backup:
  `/var/backups/pisobot/pre-multiuser-20261108T215236Z/`.
  It contains state, the environment file, test output and a revision manifest;
  it is root-only and is not an off-box backup.
- Verified the cutover preserved eight saved homes, fourteen hidden homes,
  one hidden building, six feedback records, 392 card references and four seen
  records. No owner preferences were reset for the upgrade.
- First upgraded cycle succeeded at 21:53:15 UTC: one visible match plus three
  hidden matches, zero new/drop alerts, zero failures. The old owner baseline
  was retained, not replayed.
- Telegram command menus were registered, the webhook remains cleared, and
  live `/settings` output was delivered. The owner confirmed `/settings`,
  `/set` and Save work through Telegram.
- Service is enabled at boot; environment/database/seen files remain mode 600.
  A deliberate restart at 22:07 UTC left the service running with no automatic
  restarts, but its scan hit the previously observed OpenRent metadata-ID
  mismatch. The pending baseline for changed owner settings was correctly
  withheld rather than treating an incomplete scan as success.
- OpenRent recovery was added in `f6f2541` and `5ab6968` (191 tests passed).
  Diagnostics identified an intermittently omitted card whose anonymous detail
  page was available and whose advert was older than the exclusive-access
  window. A partial batch now gets one targeted retry for missing IDs; a
  remaining mismatch refreshes the entire snapshot once, discarding partial
  results. Malformed, duplicate, unexpected or persistently missing IDs still
  fail explicitly. No anonymous-access restriction was bypassed.
- The changed-settings baseline completed at 22:42:15 UTC. The first recovery
  version failed again after a restart; the subsequent version completed scans
  at 22:53:56 and, after restart, 22:55:20 UTC. Both latter scans had nine
  visible matches plus two hidden, zero new/drop alerts and zero failures.
- At initial verification only the owner was active; real invited-user
  onboarding and independent feedback still need observation after `/start`.

## Additional rental sources

- Reviewed `41c5002` and `3c6aea5`, then integrated fixes as `219349e`.
  All 207 tests passed locally and under VPS Python 3.12 as `pisobot`.
  Temporary validation worktrees were removed.
- Corrected scheme radius/minimum-rent filtering and cache isolation, retained
  three-hour caches across watcher cycles/restarts, paced rental HTTP requests,
  made postcode/layout failures explicit, and exposed approximate SpareRoom
  location/commute/crime estimates in Telegram.
- Direct VPS probes: SpareRoom and L&Q returned HTTP 200; ShareToBuy search
  returned HTTP 403. With approval, enabled
  `WATCH_SOURCES=rm,otmr,orent,sr,lqr`. **ShareToBuy (`stbr`) remains disabled**;
  no Decodo or alternative egress was enabled.
- The read-only staged source probe returned one SpareRoom candidate and zero
  L&Q candidates under the owner's settings. Immediate cached repeats made no
  requests. These are source candidates, not additional final matches.
- Private pre-upgrade backup:
  `/var/backups/pisobot/pre-more-sources-20261108T231011Z/`.
  The cutover preserved eight saved homes, fourteen hidden homes, one hidden
  building, six feedback records, 401 card references, thirteen seen records
  and all registered users' parameters.
- First complete cycle: 23:11:11 UTC. Restart check completed: 23:12:28 UTC.
  Both had nine visible matches plus two hidden, zero listing alerts and zero
  failures; the baseline was retained. No automatic restarts occurred.
- `portal_fetch` counts for those two cycles (all direct, HTTP 200):

  | Host | First cycle | After restart |
  | --- | ---: | ---: |
  | Rightmove | 3 | 3 |
  | OnTheMarket | 3 | 3 |
  | OpenRent | 4 | 4 |
  | SpareRoom | 2 | 2 |
  | L&Q | 1 | 0 (cached) |
  | **Total** | **13** | **12** |

- These counts exclude API enrichment/image requests. The staged SpareRoom
  probe additionally needed two postcode lookups. Service cgroup memory was
  about 31-34 MiB at the two checks. State remains owned by `pisobot`, directory
  mode 700, database/cache/seen mode 600; the environment file is root-owned
  mode 600 and the service is enabled at boot.

## Original single-user rollout

Initial service start: 2026-11-08 19:33 UTC.
Application code at original rollout: `e837682`.

- 157 unit tests passed on both the development host and VPS Python 3.12,
  including the original cloud-path tests.
- Real direct-only dry run succeeded with local SQLite: Rightmove 71,
  OnTheMarket 49, OpenRent 44 parsed candidates; 4 final matches.
  Detail pages, TfL and police enrichment were exercised.
- Existing Telegram/TfL credentials were transferred encrypted to a key held
  only on the VPS. Existing preferences and old card references were migrated
  from D1 without transferring Cloudflare credentials.
- Environment file and runtime database/JSON files are mode 600; state
  directory is mode 700. `pisobot-watch` runs as the non-root `pisobot` user,
  is enabled at boot and restarts on failure.
- The old PisoBot Actions workflow is disabled. Telegram's Worker webhook is
  cleared; the local poller handles updates. Old Worker/D1 resources were
  retained for rollback, not deleted.
- One initial baseline summary was sent; existing matches were not flooded
  as individual alerts. A full report with four photo/map cards was delivered
  separately. The user confirmed the report and a real Save-button update;
  Telegram's update checkpoint and the new saved choice persisted in SQLite.
- A service restart preserved the four seen matches and emitted zero duplicate
  listing alerts or repeat baseline summaries.
- A subsequent successful scan at 19:52 UTC returned Rightmove 72,
  OnTheMarket 49 and OpenRent 45 candidates, still 4 matches. Failure count
  returned to zero.
- Snapshot around 19:57 UTC: process RSS about 51 MiB, service cgroup memory
  about 34 MiB, no automatic restarts.
- Migration artifacts/private encryption keys were removed after consumption;
  the one-time migration workflow was disabled and its remote branch removed.

## Observed limitations and events

- OpenRent has intermittently omitted a requested card despite HTTP 200.
  Bounded recovery now logs omissions and preserves strict completeness checks.
  Watch both recovery frequency and persistent failures during the soak;
  healthy initial cycles do not establish long-term reliability.
- A manual report coincided with a scheduled cycle and the single-run lock
  rejected the competing run. This was reported as a failed cycle. Prefer the
  bot's `/report` trigger over launching an independent live CLI process.
- OpenRent's anonymous first-24-hour exclusive window is respected. Its new
  adverts are therefore delayed; they are not near-real-time during that window.
- Fast scans are capped at three pages/metadata batches per source. OpenRent
  also needs an index request: the four enabled fast sources can use up to
  thirteen search/metadata requests per search group before detail requests.
  OpenRent's bounded recovery can add up to ten requests in its worst case.
  L&Q refreshes every three hours per filter set; its development/plot requests
  are additional. Different users' portal filters produce separate searches.
  Actual daily traffic has **not** yet been measured. Detail caching and
  sequential requests limit repeated work; redirects also count as requests.
- Only seven crime reference locations were cached at the observation point.
  Cards correctly show unrated crime bands until there are at least twenty;
  numeric crime filtering remains active.
- PisoBot opens no inbound port. The shared VPS already had public listeners
  on 22, 80 and 443; unrelated Traefik/services were left untouched. The broader
  SSH/firewall/Tailscale hardening checklist was not completed in this rollout.
- Off-box backups, restore drill and an external health monitor remain pending.

## Complete after the observation period

```sh
sudo systemctl status pisobot-watch --no-pager
sudo journalctl -u pisobot-watch --since '24 hours ago' --no-pager
sudo journalctl -u pisobot-watch --since '24 hours ago' -o cat --no-pager \
  | grep 'portal_fetch' | sort | uniq -c
sudo systemctl show pisobot-watch -p MemoryCurrent -p NRestarts
```

Count `portal_fetch` records by host and HTTP status (the raw lines include
timestamps). Check for repeated 403/429 responses, source parsing failures,
unexpected restarts, duplicate alerts and state growth. Record the actual
24-hour counts and memory range here; do not infer them from the brief rollout.
See `deploy/vps/README.md` for configuration, operation and rollback.
