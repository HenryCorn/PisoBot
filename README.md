# PisoBot

Telegram alerts for London shared-ownership / London Living Rent flats that fit:

- ≥ 1 bedroom (no studios, flat-shares, over-55/sheltered schemes)
- mortgage + rent + service charge ≤ **£1,600/month**, deposit ≤ **£20k** (⭐ when ≤ £10k)
- ≤ **40 min** by public transport to Shoreditch High Street (TfL Journey Planner, weekday 08:30)
- not in a high-crime spot: police.uk "safety" crimes within 500 m in the latest month ≤ **120**
  (town centres such as Shoreditch/Brixton score 150–250; quiet residential streets 30–90)

All thresholds live in `pisobot/config.py` and can be overridden with env vars of the same name.

## Sources

| Source | How it's read |
|---|---|
| ShareToBuy | server-rendered search (`/properties/page/N/?lat=…&radius=…&maxMonthlyCost=…`), cards' `data-*` attributes + per-property cost breakdown |
| L&Q | public JSON: `/wp-json/custom/developments`, `/wp-json/custom/development/{id}/properties` |
| NHG Homes | `/find-a-home?ownership=Shared` plot cards (rent & service charge estimated) |
| OnTheMarket | `__NEXT_DATA__` of `/for-sale/property/shoreditch/?shared-ownership=true` + details page |

Monthly cost is recomputed the same way for every source: repayment mortgage on
(share price − deposit) at `MORTGAGE_RATE` over `MORTGAGE_YEARS`, deposit = max(minimum, £10k).
Values a source doesn't publish are estimated and flagged "est." in the alert.

## Setup

1. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID` (send `/start` to the bot, then `TELEGRAM_BOT_TOKEN=… python scripts/get_chat_id.py`)
   - `TFL_APP_KEY`
2. **Actions → PisoBot → Run workflow** to run immediately; **report** is enabled by default
   so the digest is sent after the listing checks.
   Automatic reports run at **06:30 and 18:00 UK time**, adjusting for daylight saving.
   The workflow must be on the default branch, with Actions enabled. GitHub's scheduler can
   start late, so these are target times, not guaranteed delivery times.

## What you get in Telegram

- **Twice-daily digest** at 06:30 and 18:00 UK (also first run, `/report`, or the workflow's
  *report* box): timestamp, matching/new/saved counts, cheapest price and fastest commute;
  the best 10 (score = cost 45% · commute 30% · crime 25%, max two per building);
  one card per listing with **three distinct photo thumbnails** above its details:
  monthly cost, deposit, mortgage, commute, crime and warnings. Each card has
  🔗 Open / ❤️ Save / 👎 Hide / 🔕 Hide building buttons in the same message.
  Missing photos are marked unavailable, never filled with repeated or unrelated photos.
  Saved homes already in the shortlist aren't repeated. Only the **five cheapest alternative
  locations** are shown, grouped by building with price ranges. The **map contains every
  listing**, so the report stays short even when hundreds of homes match.
  Estimated costs are marked throughout; unavailable sources are flagged in the summary.
  The 06:30 digest is silent under the existing 22:00–07:00 quiet hours.
- **Individual alerts** remain available for manual runs without *report*, with a photo,
  full financial breakdown and ❤️ Save · 👎 Hide · 🔕 Hide building · 🔗 Open buttons.
  There are no automatic two-hour alerts.
- **Saved homes** are tracked: you're told when the cost changes or the listing disappears.
- **Commands**: `/report`, `/saved`, `/unhide`, `/help`. Buttons and commands are picked up by the
  lightweight **five-minute button check** (GitHub can delay scheduled runs). Save means
  track the home; Hide means exclude it from subsequent reports. The selected card's buttons
  change to **Saved/Hidden** once processed, without removing its other controls.
  `/report` requests a full scan and digest at the next button check.
- **Map**: `docs/index.html`, published by GitHub Pages at https://henrycorn.github.io/PisoBot/ —
  enable once in *Settings → Pages → Deploy from a branch → main / docs*.

State lives in `state/` (seen listings, saved/hidden choices, API caches) and is committed by each run.
Button checks do not scrape listings or send digests unless `/report` was requested.
You can force a check with **Actions → PisoBot → Run workflow → preferences_only**.

## Local run

```sh
pip install -r requirements.txt
TFL_APP_KEY=… python -m pisobot.main --dry-run -v          # print matches, send nothing
TFL_APP_KEY=… python -m pisobot.main --sources stb,lq --dry-run
```

Set `INSECURE_SSL=1` only on networks that intercept TLS.

Check report formatting without fetching listings or sending Telegram messages:
`python -m unittest discover -s tests`
