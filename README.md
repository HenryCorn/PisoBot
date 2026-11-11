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

**Private rentals are on demand only**: scheduled runs and `/report` cover shared ownership;
`/rentals` in Telegram (or the workflow's *rentals* box, `python -m pisobot.main --rentals`) searches the
three lettings sources and sends a separate rentals report, with its own map at `docs/rentals.html`.
It never counts as the 06:30/18:00 report. The hard limit is the advertised monthly rent, `MAX_RENT` (£1,600); the portals are searched with
that maximum price. Bills are estimated and shown, not used to hide flats: council tax comes from the
advert's band and the borough's 2026-27 Band D (`pisobot/council_tax.py`, MHCLG official statistics; the
London average and band C when the band or borough is unknown, labelled `~`), and utilities from
`RENT_BILLS_ESTIMATE` (£130: energy, water, broadband), each omitted when the advert says it is
included. Cards read `£1,550 rent · ~£1,812 all-in (council tax B £131 · utilities ~£131)`. A person can
also cap that all-in figure (`budget`, off by default); the deposit is the advertised one, else 5 weeks' rent. Rooms filed as flats (shared
kitchen/bathroom, ensuite rooms, co-living, < `RENT_MIN_AREA_M2`, rent < `RENT_MIN`) and short or student
lets are dropped. Portals block GitHub's runners, so with `DECODO_AUTH_TOKEN` set these pages are fetched
through [Decodo's web data API](https://dashboard.decodo.com/web-data/playground) from UK residential IPs.
Each page is one Decodo request (free plan: 2,000; ~25 pages per `/rentals`), so a repeat `/rentals`
within `RENT_REFRESH_HOURS` (2) reuses the last search, and details pages are cached forever.

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
- **Commands**: `/report`, `/rentals`, `/saved`, `/unhide`, `/help`, answered instantly by the Worker.
  Save means track the home; Hide means exclude it from subsequent reports. The selected
  card's buttons change to **Saved/Hidden** straight away, without removing its other controls.
  `/report` starts a full scan and digest immediately; it arrives a few minutes later.
  `/rentals` does the same for private rentals only.
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

## Optional: neighbourhood facts

The local OSM neighbourhood tile and cached `/area <postcode or district>` overview are off unless
`PLACE_FACTS=1`. Build the local Greater London index with `pip install -r requirements-place.txt`
and `python scripts/build_place_index.py`; the builder stores OSM attribution in the index metadata.
TfL and Defra refreshes run only in the foundation's supervised optional worker and are cached in
`optional.db`; area summaries are postcode-district aggregates of sampled archived adverts, refreshed
every 30 days. Crime comparisons use cached matched-advert scores, not London-wide population risk.

## Optional: language-model reading of rental adverts

`pisobot/extract.py` can ask a small Claude model (`EXTRACT_MODEL`, default `claude-haiku-5-5`) whether a
rental advert is really a whole flat, and to extract furnished state, floor and lift, heating, bills,
pets, outdoor space, minimum term, condition, red flags (each with the quoted phrase) and a one-line
summary. It is **off** unless `ANTHROPIC_API_KEY` is set (VPS, `STORAGE=local`; `pip install -r
requirements-extract.txt`). With no key, or on any error, timeout or when `EXTRACT_DAILY_LIMIT` requests
have been used that day, the keyword rules decide exactly as before.

The model may rescue a rules-rejected advert only when it says `whole_flat` with high confidence and a
contiguous quote from the description explicitly supports self-contained accommodation (not just a
private entrance), with no explicit shared-accommodation contradiction. Empty, invented, metadata-only,
negated or unsupported evidence preserves the rules' verdict. It may
reject a kept advert only for `room_in_shared`, `short_let`, `student_only` or `age_restricted` with high
confidence and a phrase that really occurs in the advert; anything in between keeps the rules' verdict and
adds a "Check advert" note. Refinement and full cost checks precede extraction; the production pipeline
also checks travel/crime before paying for extraction. Adverts are escaped inside a data boundary with
explicit untrusted-input instructions. Neither delimiters nor quote matching eliminate prompt injection
or establish that an advert is truthful.

Deposit amounts are compared in code, not by model arithmetic: the usual
[English tenancy-deposit benchmark](https://www.gov.uk/private-renting/deposits) of five weeks (six at
annual rent of at least £50,000), with a £1 rounding allowance. A larger amount prompts a terms check,
not a legal verdict; applicability depends on the tenancy. Existing source logic can estimate the
deposit field, so it is not independently verified advertised evidence. Model amount-only deposit alarms and summaries
discussing deposits are dropped. Explicit non-refundable deposit wording can still be flagged.
Deposit-replacement offers get a neutral "check fees and liability" note, never a blanket safety or
legality endorsement. Other supported warnings, including payment before viewing, remain.

Results are cached in the local SQLite database by a hash of the input, model and prompt version; a
512-entry in-memory LRU retains only hot results. Eviction reuses SQLite without another paid request.
Failures are not cached. The request lock is deliberately retained across the HTTP call to preserve
same-instance deduplication and request-slot accounting; it serializes concurrent callers. It is not a
cross-process spending lock. Every disagreement is stored in
`extraction_disagreements`. Only advert text and public listing fields are sent: never an address,
a name, a chat id or a setting.

First-party standard [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing),
verified 2026-11-10, is model-specific: **Haiku 5.5** costs $0.10 input / $0.50 output per million tokens
up to 100K prompt tokens, with $0.01 cache reads and $0.125 five-minute cache writes. Above 100K,
including cached input, those rates are $0.50 / $2.50 / $0.05 / $0.625. **Haiku 4.5** instead costs
$1 / $5 / $0.10 / $1.25. The evaluator uses the selected model, refuses dollar projections for unpriced
model IDs, and does not infer a family price. These are estimates, not invoice reconciliation or a
dollar spending cap: `EXTRACT_DAILY_LIMIT` limits attempted requests, including failures.

The existing cached evaluation used truncated search-card text and establishes neither production
accuracy nor successful filtering. Confidence is not forced higher. The changed prompt invalidates
old cache keys; full-text quality remains unverified. `scripts/eval_extract.py` can make paid calls:
run its model half only under a separately authorized budget. A one-off test ceiling does not authorize
production activation.
