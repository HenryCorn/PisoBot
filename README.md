# PisoBot

Telegram alerts for London shared-ownership / London Living Rent flats that fit:

- ≥ 1 bedroom (no studios, flat-shares, over-55/sheltered schemes)
- mortgage + rent + service charge ≤ **£1,600/month**, deposit ≤ **£20k** (⭐ when ≤ £10k)
- ≤ **40 min** by public transport to Oxford Circus (TfL Journey Planner, weekday 08:30)
- not in a high-crime spot: police.uk "safety" crimes within 500 m in the latest month ≤ **120**
  (town centres such as Shoreditch/Brixton score 150–250; quiet residential streets 30–90)

All thresholds live in `pisobot/config.py` and can be overridden with env vars of the same name.

## Sources

| Source | How it's read |
|---|---|
| ShareToBuy | server-rendered search (`/properties/page/N/?lat=…&radius=…&maxMonthlyCost=…`), cards' `data-*` attributes + per-property cost breakdown |
| L&Q | public JSON: `/wp-json/custom/developments`, `/wp-json/custom/development/{id}/properties` |
| NHG Homes | `/find-a-home?ownership=Shared` plot cards (rent & service charge estimated) |
| OnTheMarket | `__NEXT_DATA__` of `/for-sale/property/oxford-circus/?shared-ownership=true` + details page |
| Rightmove lettings (`rm`) | `__NEXT_DATA__` of `/property-to-rent/find.html` near Oxford Circus Station + `window.__PAGE_MODEL` details page |
| OnTheMarket lettings (`otmr`) | `__NEXT_DATA__` of `/to-rent/flats-apartments/oxford-circus-station/` + details page |
| Zoopla lettings (`zp`) | React Server Components stream (`self.__next_f`) of `/to-rent/flats/oxford-circus/` + details page; Decodo only |

**Private rentals** count rent + bills against the same £1,600 budget. Bills are estimated at
`RENT_BILLS_ESTIMATE` (£280: council tax, energy, water, broadband) unless the advert says they are
included; the deposit is the advertised one, else 5 weeks' rent. Rooms filed as flats (shared
kitchen/bathroom, ensuite rooms, co-living, < `RENT_MIN_AREA_M2`, rent < `RENT_MIN`) and short or student
lets are dropped. Portals block GitHub's runners, so with `DECODO_AUTH_TOKEN` set these pages are fetched
through [Decodo's web data API](https://dashboard.decodo.com/web-data/playground) from UK residential IPs.
Each page is one Decodo request (free plan: 2,000), so searches refresh every `RENT_REFRESH_HOURS` (11.5,
i.e. once per report), hourly runs reuse the cached results, and details pages are cached forever.

Monthly cost is recomputed the same way for every source: repayment mortgage on
(share price − deposit) at `MORTGAGE_RATE` over `MORTGAGE_YEARS`, deposit = max(minimum, £10k).
Values a source doesn't publish are estimated and flagged "est." in the alert.
Advert rent/service-charge figures preserve pence and are converted from explicitly stated
annual or weekly amounts to monthly amounts. Service charges **over £250/month** and leases
**under 90 years** are flagged. Original lease terms with a stated commencement year are
reduced by elapsed calendar years; the exact anniversary should be confirmed by a solicitor.
Floor area uses published total internal area (sq ft converted to m²), never room dimensions.
Price/m² uses the **100% full market value**, not the share price.
Source floorplan images are read from ShareToBuy downloads, OnTheMarket's `floorplans` data,
and L&Q's plot-specific floorplan slider; missing facts remain unknown.
L&Q sometimes marks withdrawn plots as available in its API; 404/410 plot pages are
rejected, while temporary optional-detail failures keep the API listing with unknown facts.

## Setup

1. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID` (send `/start` to the bot, then `TELEGRAM_BOT_TOKEN=… python scripts/get_chat_id.py`)
   - `TFL_APP_KEY`
   - `DECODO_AUTH_TOKEN` (API key from the [Decodo Playground](https://dashboard.decodo.com/web-data/playground); for local runs put it in `.env`)
   - `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_D1_DATABASE_ID`, `CLOUDFLARE_API_TOKEN`
     (custom token with *Account → D1 → Edit*)
2. **Telegram Worker** (free Cloudflare account). Buttons and commands go to a Cloudflare
   Worker that records them in D1 instantly; runs read them from there.

   ```sh
   cd worker
   npx wrangler login
   npx wrangler d1 create pisobot            # copy database_id into wrangler.toml
   npx wrangler d1 execute pisobot --remote --file=schema.sql
   npx wrangler secret put TELEGRAM_BOT_TOKEN
   npx wrangler secret put TELEGRAM_CHAT_ID
   npx wrangler secret put TELEGRAM_WEBHOOK_SECRET   # any random string
   npx wrangler secret put GITHUB_TOKEN      # fine-grained, this repo only, Actions: Read and write
   npx wrangler deploy
   curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=<WORKER_URL>&secret_token=<WEBHOOK_SECRET>"
   ```

   On networks that intercept TLS, set `NODE_OPTIONS=--use-system-ca` for wrangler.
3. **Report times.** GitHub's own cron is throttled and can run late, so reports are
   triggered by two [cron-job.org](https://cron-job.org) jobs at **06:30 and 18:00**
   (time zone *Europe/London*), each sending
   `POST https://api.github.com/repos/HenryCorn/PisoBot/actions/workflows/pisobot.yml/dispatches`
   with headers `Authorization: Bearer <GitHub token>`, `Accept: application/vnd.github+json`,
   `X-GitHub-Api-Version: 2022-11-28` and body `{"ref":"main","inputs":{"scheduled":"true"}}`
   (a `204` response means it worked). An hourly GitHub run catches up a missed slot,
   and a successful report records its delivery time so later checks do not repeat it.
4. **Actions → PisoBot → Run workflow** runs a full report immediately.

## What you get in Telegram

- **Twice-daily digest** at 06:30 and 18:00 UK (also first run, `/report`, or the workflow's
  *report* box): timestamp, matching/new/saved counts, cheapest price and fastest commute;
  the best 10 (score = cost 45% · commute 30% · crime 25%, max two per building);
  one card per listing with a **1200×500 map/floorplan header** above its details:
  monthly cost, deposit, mortgage, commute, crime and warnings. Each card has
  🔗 Open / ❤️ Save / 👎 Hide / 🔕 Hide building buttons in the same message.
  The left 40% shows Oxford Circus (A) and the source's listing location (B), north-up,
  with bounds fitted to both pins. The connector is explicitly a **straight line, not a route**.
  The right 60% contains the complete floorplan (no cropping), or a hero photo when
  no usable floorplan image is available. Missing panels are marked unavailable.
  Source coordinates may be approximate development pins or postcode centroids.
  Map tiles are attributed to OpenStreetMap, requested only for rendered cards with a
  PisoBot User-Agent, cached for seven days in `.cache/map-tiles`, and retained through
  GitHub Actions cache. No paid map API key is required.
  Saved homes already in the shortlist aren't repeated. Only the **five cheapest alternative
  locations** are shown, grouped by building with price ranges. The **map contains every
  listing**, so the report stays short even when hundreds of homes match.
  Estimated costs are marked throughout; unavailable sources are flagged in the summary.
  The 06:30 digest is silent under the existing 22:00–07:00 quiet hours.
  Card titles translate outcodes to broad London area names. Costs show M (modelled
  mortgage), R (unowned-share rent) and SC separately; `~`/`est.` marks estimated inputs.
  Cards include share %, full market value, share price, lease (`TBC` if unknown),
  floor area/value per m², and the walk to the first boarding point.
  Description tags highlight balcony/terrace, parking, lift, affirmative EWS1 evidence,
  cladding mentions and explicit pet bans; these are advert cues, not verified guarantees.
  Crime colours are **tertiles of the latest month's sampled London search locations**
  at the same 500 m radius, including locations rejected by the crime filter.
  This is **not an official London-wide or population-adjusted risk percentile**.
  At least 20 distinct ~100 m cache locations are needed; otherwise cards say *Unrated*.
  The static legend and explanations live in `/help`, not each report.
  Scheduled runs with **zero new homes** send just a **silent one-line summary**, without
  repeating unchanged cards. Price changes still get updated cards; manual reports and
  `/report` intentionally return the full digest even when nothing is new.
- **Individual alerts** remain available for manual runs without *report*, with a photo,
  full financial breakdown and ❤️ Save · 👎 Hide · 🔕 Hide building · 🔗 Open buttons.
  There are no automatic two-hour alerts.
- **Saved homes** are tracked: you're told when the cost changes or the listing disappears.
- **Commands**: `/report`, `/saved`, `/unhide`, `/help`, answered instantly by the Worker.
  Save means track the home; Hide means exclude it from subsequent reports. The selected
  card's buttons change to **Saved/Hidden** straight away, without removing its other controls.
  `/report` starts a full scan and digest immediately; it arrives a few minutes later.
- **Map**: `docs/index.html`, published by GitHub Pages at https://henrycorn.github.io/PisoBot/ —
  enable once in *Settings → Pages → Deploy from a branch → main / docs*.

Saved/hidden choices, the last report time and the listings that cards refer to live in
Cloudflare D1. Seen listings and API caches live in `state/` and are committed by each run
that changes them. Scheduled checks do not scrape listings or send digests unless the latest
UK report slot has not been delivered. An all-source fetch failure fails the
run rather than recording an empty report as delivered. Action logs identify whether a
report was due, which header panels rendered, and when unchanged cards were suppressed.

## Local run

```sh
pip install -r requirements.txt
TFL_APP_KEY=… python -m pisobot.main --dry-run -v          # print matches, send nothing
TFL_APP_KEY=… python -m pisobot.main --sources stb,lq --dry-run
```

Set `INSECURE_SSL=1` only on networks that intercept TLS.

Check report formatting without fetching listings or sending Telegram messages:
`python -m unittest discover -s tests`
