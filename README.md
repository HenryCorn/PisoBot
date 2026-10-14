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
2. **Actions → PisoBot → Run workflow** to run immediately. It then runs every 2 h from 06:17 to 22:17 UTC.

## What you get in Telegram

- **Instant alerts** (every 2 h, 07:17–23:17 UK; silent 22:00–07:00): photo, cost breakdown,
  **mortgage needed / deposit / term**, share & full value, lease, council tax band, floor,
  commute (time, route, boarding station), crime score, warnings (short lease, high service charge).
  Buttons: ❤️ Save · 👎 Hide · 🔕 Hide building · 🔗 Open.
- **Daily report** at 08:00 UK (also first run, `/report`, or the workflow's *report* box):
  a numbered photo collage of the best 10 (score = cost 45% · commute 30% · crime 25%, max two per
  building), their details with ❤️/👎 buttons, saved homes, then everything else grouped by building.
- **Saved homes** are tracked: you're told when the cost changes or the listing disappears.
- **Commands**: `/report`, `/saved`, `/unhide`, `/help`. Buttons and commands are picked up by the
  next run (GitHub Actions has no always-on server).
- **Map**: `docs/index.html`, published by GitHub Pages at https://henrycorn.github.io/PisoBot/ —
  enable once in *Settings → Pages → Deploy from a branch → main / docs*.

State lives in `state/` (seen listings, saved/hidden choices, API caches) and is committed by each run.

## Local run

```sh
pip install -r requirements.txt
TFL_APP_KEY=… python -m pisobot.main --dry-run -v          # print matches, send nothing
TFL_APP_KEY=… python -m pisobot.main --sources stb,lq --dry-run
```

Set `INSECURE_SSL=1` only on networks that intercept TLS.
