# VPS rollout and soak

Status: **deployed; 24-hour soak is still pending**.
Initial service start: 2026-11-08 19:33 UTC.
Earliest 24-hour observation point: 2026-11-09 19:33 UTC.
Application code at rollout: `e837682`, branch `vps-rentals`.

## Confirmed during deployment

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

- One OpenRent response failed the strict metadata-ID completeness check during
  the initial observation period. It was recorded as a source/cycle failure,
  not silently treated as an empty successful result. A later bounded probe and
  full service scan succeeded, retaining saved/seen state. Watch this during
  the soak; repeated occurrences need further response diagnostics.
- A manual report coincided with a scheduled cycle and the single-run lock
  rejected the competing run. This was reported as a failed cycle. Prefer the
  bot's `/report` trigger over launching an independent live CLI process.
- OpenRent's anonymous first-24-hour exclusive window is respected. Its new
  adverts are therefore delayed; they are not near-real-time during that window.
- Scans are capped at three pages/metadata batches per source. OpenRent also
  needs an initial index request: up to ten search/metadata requests per cycle.
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
