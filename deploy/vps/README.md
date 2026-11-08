# VPS rental service

The local service uses Rightmove (`rm`), OnTheMarket (`otmr`) and OpenRent
(`orent`). It runs as a dedicated `pisobot` user, uses SQLite and private JSON
state, and receives Telegram updates through outbound long polling. No web
server, GitHub Actions schedule, Cloudflare service or always-on browser is
required. Public accessibility does not grant scraping permission; review each
portal's terms before operating the service.

The existing cloud entry points are unchanged when `STORAGE` is unset. The VPS
must explicitly set `STORAGE=local` and `FETCH_CHAIN=plain`. A Decodo token is not
needed. OpenRent is opt-in and does not change the cloud's default sources.

## Files and credentials

- Code: `/opt/pisobot/app`, checked out at the deployment commit, root-owned.
- Python 3.12+ virtualenv: `/opt/pisobot/venv`; install `requirements.txt`.
- Service account: system user/group `pisobot`, no login shell.
- Configuration: `/etc/pisobot/pisobot.env`, root-owned, mode **600**.
  Copy the variable names from `pisobot.env.example`, and provide your own
  Telegram bot token, chat ID and TfL key. Never commit the populated file.
- State: `/var/lib/pisobot`, owned by `pisobot`, mode **700**.
  Contains `pisobot.db` (including WAL files while open), `seen.json`,
  `cache.json`, lock files and map tiles. Keep it across upgrades.

Do not copy an old cloud `seen.json` into a running service. An uninitialized
watcher records the first complete scan as a baseline and sends one summary,
not a card for every existing match. Source/enrichment failures prevent that
baseline from being marked complete.

## Validate before cutover

Run the unit suite without production environment variables:

```sh
cd /opt/pisobot/app
sudo -u pisobot /opt/pisobot/venv/bin/python -m unittest discover -s tests
```

Run a real scan using systemd to load the private configuration without putting
credentials in command arguments:

```sh
sudo systemd-run --wait --pipe --collect --unit=pisobot-check \
  -p User=pisobot -p Group=pisobot \
  -p WorkingDirectory=/opt/pisobot/app \
  -p EnvironmentFile=/etc/pisobot/pisobot.env \
  -p Environment=PYTHONDONTWRITEBYTECODE=1 \
  /opt/pisobot/venv/bin/python -m pisobot.watch --once --dry-run -v
```

This writes API caches but sends no Telegram messages, does not mark the watch
initialized, and does not clear a webhook. Stop any running watch service
before a manual scan; the process lock deliberately rejects concurrent runs.

## Cutover and operate

If reusing a cloud bot, first disable the old PisoBot Actions workflow and wait
for any in-flight run to finish. Export preferences/listing references before
switching if existing saved homes and old card buttons must continue to work.
Keep the old Worker and D1 data for rollback; do not delete them during cutover.

```sh
sudo install -m 644 deploy/vps/pisobot-watch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pisobot-watch
sudo systemctl status pisobot-watch --no-pager
sudo journalctl -u pisobot-watch --since '10 minutes ago' --no-pager
```

The poller clears and verifies the Telegram webhook on startup without dropping
pending updates. Only `TELEGRAM_CHAT_ID` is accepted. Test `/help`, `/saved`,
`/unhide`, Save/Hide/Hide building, `/rentals` and `/report` in that chat.
`/rentals` requests a fresh new/drop scan; `/report` requests a full rental
digest. Requests are coalesced while a scan is already pending/running.
`/reset` clears preferences, not listing history or scheduler state.

The scheduler defaults to 15 minutes after each completed cycle, with +/-25%
jitter. It forces fresh rental searches, reuses detail/enrichment caches, and
sends only new matches and price drops. Quiet hours silence notifications
rather than skip scans. Each successful alert is checkpointed immediately.
Telegram delivery and disk writes cannot form one transaction: an abrupt crash
after Telegram accepts a message but before its checkpoint can still duplicate
that message. Ordinary completed cycles and restarts do not resend it.

`RENT_REFRESH_MINUTES` replaces the cache interval; absent that variable,
`RENT_REFRESH_HOURS` remains supported. `RENT_MAX_PAGES=3` bounds each source
scan: at most ten search/metadata requests per cycle including OpenRent's
initial index (about 960 per day before runtime, jitter, details and redirects).
OpenRent details are cached for a day, but refreshed early when the search
card's rent or bills change. Its anonymous first-24-hour exclusivity window is
respected: **new OpenRent adverts cannot alert immediately during that window**.
This is **not** a complete crawl. Local scans
therefore do not automatically delete saved homes absent from capped results.
Monitor actual volume before increasing cadence or pagination.

After `WATCH_FAILURE_LIMIT` consecutive incomplete cycles the service attempts
one Telegram warning. It resets that warning state after a successful cycle.
An optional `HEALTHCHECK_URL` is called after successful cycles only. Use an
external monitor if desired; the service cannot notify Telegram during a total
network outage.

No public map is published in local mode. Unless you separately configure a
valid `MAP_URL`, rental digests omit the old GitHub Pages map link.

## Upgrade, rollback and remaining checks

Stop the service before changing the checkout, install changed requirements,
run tests and the dry run, then start it again. Never erase `/var/lib/pisobot`
as part of an upgrade.

To roll back to the cloud, stop and disable `pisobot-watch` first, restore the
previous Telegram webhook using your private token, then re-enable the old
PisoBot workflow. Local preference changes will need to be exported back to D1
if they must survive rollback. Never run both schedulers or pollers together.

Observe a 24-hour soak before considering the rollout fully validated:
request volume by portal, failure/recovery logs, RSS, callback operations,
absence of duplicate alerts, and a service restart. Backups and an off-box
restore drill remain separate operational work. Keep SSH/key/firewall changes
separate from application rollout; do not close existing unrelated listeners
on a shared VPS.
