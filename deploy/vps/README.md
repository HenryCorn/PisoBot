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
pending updates. Only `TELEGRAM_CHAT_ID` (the owner) and admitted people are
accepted (see *Sharing* below). Test `/help`, `/saved`,
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
card's rent or bills-included flag changes. Its anonymous first-24-hour exclusivity window is
respected: **new OpenRent adverts cannot alert immediately during that window**.
If a card batch omits some IDs, OpenRent retries only those IDs once. If IDs
still disagree, it discards the partial snapshot and refreshes it once through
the same paced fetcher. A persistent mismatch still fails with missing/unexpected
ID diagnostics; it is never accepted as complete. At a three-page cap this can
add up to ten requests to a search in the worst recovery case.
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

## Rental filtering and diagnostics

Rental text exclusions use word boundaries and context: "bedroom in", "over
600 sq ft", "reserved parking" and a shared entrance do not mean a room,
age-restricted home or unavailable flat. Explicit restrictions and structured
room/studio fields still exclude a listing; ambiguous mentions show a
`Check advert` warning. A zero bills **estimate** is displayed as an estimate,
not as evidence that bills are included. Budgets remain rent plus bills.

Rental identity is `source:id`. Repeated IDs and previously alerted IDs remain
suppressed, with price-drop alerts preserved. Different IDs are no longer
merged solely because they share a map pin, bedroom count and similar rent.
Cross-portal copies may therefore appear; this is preferable to silently
hiding distinct homes. Sale deduplication is unchanged.

Every live local rental scan appends aggregate JSON to `rental_funnel` in that
person's SQLite database. Inspect the owner's latest records without loading
the secret environment:

```sh
sudo -u pisobot /opt/pisobot/venv/bin/python - <<'PY'
import json
import sqlite3
db = sqlite3.connect("file:/var/lib/pisobot/pisobot.db?mode=ro", uri=True)
try:
    for at, stats in db.execute(
        "SELECT created_at, stats FROM rental_funnel ORDER BY id DESC LIMIT 5"
    ):
        print(at, json.dumps(json.loads(stats), indent=2))
finally:
    db.close()
PY
```

Guest records live in their own `users/<chat_id>/pisobot.db`. Counts include
fetched candidates, the first rejection reason, matches before hiding, hidden
matches, source failures and whether filtering finished. `lqr`/`stbr` are
reported under their selected source names. These are **pipeline** counts:
portal-side filters, adapter exclusions and capped-away pages are not measured.
Detail/refinement refusals appear as `details`; transport/layout failures at
that stage may also contribute to that bucket.

Failed initial baselines, all-source failures and interrupted filtering retain
diagnostics; the existing complete-baseline requirement is unchanged. A row is
written before Telegram delivery, so it does not prove a successful whole
cycle. Dry runs, previews and cloud runs do not write these records. Records
contain no search preferences or listing descriptions and currently have no
automatic retention policy.

## Upgrade, rollback and remaining checks

Stop the service before changing the checkout, install changed requirements,
run tests and the dry run, then start it again. Never erase `/var/lib/pisobot`
as part of an upgrade.

To roll back to the cloud, stop and disable `pisobot-watch` first, restore the
previous Telegram webhook using your private token, then re-enable the old
PisoBot workflow. The Worker also requires its `TELEGRAM_WEBHOOK_SECRET` when
setting the webhook: `getWebhookInfo` does not reveal that secret. If it is
unavailable, rotate the Worker's secret and set the matching value on the
webhook before resuming the cloud bot. Local preference changes will need to be exported back to D1
if they must survive rollback. Never run both schedulers or pollers together.

Observe a 24-hour soak before considering the rollout fully validated:
request volume by portal, failure/recovery logs, RSS, callback operations,
absence of duplicate alerts, and a service restart. Backups and an off-box
restore drill remain separate operational work. Keep SSH/key/firewall changes
separate from application rollout; do not close existing unrelated listeners
on a shared VPS.

## Sharing and per-person searches

`TELEGRAM_INVITE_USERNAMES` (comma-separated Telegram usernames, case-insensitive,
`@` optional) admits people besides the owner, up to `MAX_USERS` in total
(default 2, owner included). An invited person opens a **private** chat with the
bot and sends `/start`; their numeric chat ID is then stored and later username
changes do not matter. The owner is notified, can list people with `/users` and
revoke access with `/removeuser <chat_id>`; their settings and files stay on disk.
Removal also prevents the same invitation from admitting them again. To restore
access deliberately, an administrator can set that registry row's `active` flag
back to `1`; changing the invitation environment variable alone does not undo removal.

Everyone has an independent search: parameters in the primary database's
`users` table, and their own SQLite database and `seen.json` under
`$PISOBOT_STATE_DIR/users/<chat_id>/` (saved/hidden/feedback, card buttons,
baseline and alerts). The owner keeps the original `pisobot.db`/`seen.json`.

`/settings` shows a person's parameters and `/set <name> <value>` changes one:
`rent` (maximum advertised rent, £/mo: the hard limit, default 1,600), `budget`
(optional cap on rent + estimated bills; off unless set, `/set budget off` clears
it; a `budget` stored before this split keeps meaning that all-in cap), `beds`,
`commute` (minutes), `crime`, `bills` (utilities estimate when not included;
council tax is estimated separately from the advert's band), `minrent`, `area` (m²), `radius` (1/3/5/10/15
miles round Oxford Circus) and `destination` (postcode, outcode, `lat,lng` or a
station/place name, resolved with postcodes.io / TfL).

Further per-person preferences, all off by default and applied only to that person's matches:
`studios` (`on`, or a minimum published floor area in m², e.g. `28`; studios are labelled on the
card; a person who wants studios gets their own portal search, asking for 0-bedroom flats, because
adapters otherwise filter studios before personal rules run), `furnished` (`any`, `furnished`,
`unfurnished`; applied only when the advert states it, otherwise the card says furnishing is not
stated), `maxwalk` (minutes to the first station or stop), `maxchanges` (changes on the commute, 0 =
direct) and `areas` (`/set areas exclude N7 SE15`, `/set areas include E5 E8`; districts must be in
the London district table). Each has its own funnel rejection reason (`studio`, `studio_area`,
`furnishing`, `walk`, `changes`, `area_excluded`, `area_outside`) and, like any search setting,
triggers the quiet re-baseline. `/set <name> default`
restores one, `/set all default` everything. A change clears only that person's
baseline: the next scan re-records their matches with one summary instead of
alerting every newly matching home; `/report` shows them all.

Each cycle shares portal searches only when rent, radius, minimum bedrooms and
minimum rent are identical (the all-in `budget` is applied locally and never splits a search). Different portal filters receive separate searches,
each bounded by `RENT_MAX_PAGES`. A single wide union query would let one person's
newer/expensive listings displace the other's affordable inventory under that cap.
Details and enrichment caches are shared, but additional qualifying homes can
still require new detail/TfL/police requests. Two different searches can roughly
double search-page traffic (up to twenty search/metadata requests per cycle at
three pages), so measure volume again after enabling sharing.

Searches and the cheap distance pre-filter stay centred on Oxford Circus;
`destination` changes only the commute target. The pre-filter expands to cover
a larger configured portal radius; it does not retain a hidden 12 km cap on a
10/15-mile search. The default owner's search and existing baseline are preserved.
The local `main --rentals` CLI also uses the owner's saved settings and performs
a fresh search; `watch --once --dry-run` exercises everyone's searches.

`/report` goes only to whoever asked. If another run is busy, the bot asks them
to retry rather than claiming their report is queued. One person's delivery or
search failure counts as a failed cycle but does not stop the others' searches.
After deployment, keep the old soak observations and start a new 24-hour
observation window for this version, including a settings change during a scan,
independent Save/Hide actions, and a service restart without duplicate alerts.

## Market archive and deep sweep

Every advert a scan fetches is remembered in the primary database (tables `adverts`,
`observations`, `verdicts`, `sweeps`, created on first use; `worker/schema.sql` is not
changed). Each person's outcome for each advert (`matched`, or the first check it
failed) is stored with the numbers behind it: rent, all-in, commute minutes, crime score.

After each scan a short slice (`DEEP_SWEEP_STEP_SECONDS`, 120 by default, half crawling and
half judging) advances the **deep sweep** of Rightmove and OnTheMarket: all rent from
`RENT_MIN` to 110% of the highest rent cap within `DEEP_SWEEP_MILES`, in price bands that
are halved whenever a band has more results than the portal will page through. A sweep
starts every `DEEP_SWEEP_HOURS` (12; `0` turns it off), resumes across scans, goes through
the same paced fetcher, and stops at `DEEP_SWEEP_MAX_REQUESTS` (150) per source, recorded
as incomplete. One sweep of each source is about 75 requests at current volumes.

Archived adverts are judged for each person with their own limits; a person's radius does
not apply, the commute limit does. A person's first completed sweep records its matches
without alerting and sends one summary; later sweeps alert as usual. An advert inside a
sweep's scope that is missing from two complete sweeps in a row is marked `unseen` (never
from an incomplete sweep, and not "gone": it may have been let, withdrawn or re-priced
above the swept range), and a saved home that becomes unseen gets a "no longer found in
the search" message saying so. A sweep band counts as read only when the adverts read
reach the portal's own total for that band, so a portal that silently stops paging makes
the sweep incomplete rather than looking complete.

`/status` shows the last scan, per-source health and the last sweep with the head-poll
recall; `/whatif` estimates what loosening rent, commute or crime would add, from the last
seven days of that person's verdicts.

## Sources

`WATCH_SOURCES` may contain `rm` (Rightmove), `otmr` (OnTheMarket), `orent`
(OpenRent), `sr` (SpareRoom whole-property lets), `stbr` (ShareToBuy London Living
Rent / Rent to Buy) and `lqr` (L&Q London Living Rent / Rent to Buy). All are
fetched directly when `FETCH_CHAIN=plain`; Zoopla stays cloud-only because it needs Decodo.
Probe each source from the deployment host before enabling it. On 2026-11-08,
the Hostinger VPS received HTTP 200 from SpareRoom and L&Q but HTTP 403 from
ShareToBuy's search: leave `stbr` disabled there until direct access is available
or a separately approved egress is configured. A blocked source makes cycles
incomplete; it must not be treated as an empty successful result.

- `sr` reads up to `RENT_MAX_PAGES` pages of `/flats-to-rent/london` (about 11
  cards each, London-wide) every cycle; SpareRoom's filter parameters are
  disallowed by its robots.txt, so filtering is local. Advert pages need
  JavaScript, so locations come from a full postcode quoted in the card text or
  else the postcode-district centre; the card then says the location (and so
  commute and crime) is approximate.
- `stbr`/`lqr` change slowly: they refresh every 3 hours, cached per filter set,
  instead of being re-searched every cycle. They are costed like private lets
  (rent + bills estimate, 5 weeks' deposit) and cards say income caps apply
  (check each advert's income, residency and other eligibility rules; the bot
  does not verify eligibility). Search and optional detail/gallery requests
  use the same paced fetcher and `portal_fetch` logging as the other VPS sources.
- Not built: Gumtree (listings only render with JavaScript; robots allows only
  the first page), build-to-rent operators (cheapest London 1-bed found was
  about £2,370/mo; most have no machine-readable unit data), Facebook.
