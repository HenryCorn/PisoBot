# PisoBot blueprint: from alert filter to flat-finding system

Status: proposal, 2026-11-10. Owner decision recorded: **£1,600 is the maximum advertised rent**
(not rent + bills). Two people use the bot; each must end up with a flat that suits them.

## Why a redesign

A search fails for one of four reasons. Today's bot only partly addresses the first two.

| Failure | Today | Evidence (2026-11-10) |
| --- | --- | --- |
| Never saw the flat (coverage) | 5-mile circle, 3 pages per portal, Zoopla absent | Rightmove holds 118 flats at 5 mi / ≤£1,600 and 1,268 at 10 mi. About half of sampled points 8–16 km out are within 40 min of Oxford Circus. |
| Saw it too late (latency) | 15-min polling, but OpenRent adverts wait 24 h | 165 one-bed flats ≤£1,750 are added per day within 10 mi on Rightmove alone. |
| Criteria silently impossible (calibration) | Hard gates, no feedback on what they cost | Latest VPS scan: 202 fetched, 162 rejected on cost, 17 matched. Owner budget is 1,800 all-in, so the rent cap is really £1,520; half of the cached market is priced £1,500–1,600. |
| Saw it, did not win it (action) | A card with Save/Hide | Nothing tracks enquiries, viewings or outcomes. |

The VPS (2 vCPU, 8 GB, 85 GB free) runs the service in about 70 MiB. Capacity is not a constraint.

## Target design

### 1. Market archive (the foundation)

Crawling stops being a per-person filter and becomes a shared record of the market.

- Ingest every advert that any user could plausibly want: 1–2 beds, rent up to about 125% of the
  highest user cap, anywhere inside the union of the users' commute zones.
- SQLite tables: `adverts` (one row per source:id, latest facts), `observations` (price, status
  and timestamp each time something changes), `homes` (adverts believed to be the same flat,
  across portals), `enrichment` (commute, crime, costs, extracted facts, keyed by home).
- Users become saved queries over the archive. Changing a setting re-queries stored data: no
  re-crawl, no baseline reset, instant `/report`.
- Rejections are stored per user with their reason, which is what makes what-if answers and
  near-miss lists possible.
- The 5 MB `cache.json` rewritten every cycle moves into the same database.

### 2. Crawl: head poll plus deep sweep

- **Head poll** (every 10–15 min): newest-first pages per source. Rightmove's newest sort is by
  last update, so price cuts resurface there too.
- **Deep sweep** (2–4 times a day): the full commute zone, sliced by price band so no query
  exceeds a portal's pagination cap. It finds what the head poll missed, detects adverts that
  were let or withdrawn, and measures time on market.
- **Coverage audit**: each sweep reports how many adverts the head poll had not already seen.
  That number is the bot's recall, measured rather than assumed.
- **Fetch nodes**: a source declares the egress it needs. Sources that block datacenter
  addresses are fetched by a small outbound-only worker on a home connection (the Pi) that pulls
  URLs from the VPS and posts pages back. Such sources ship disabled on the VPS until a node is
  online. See "Sources" for what local probes found.

### 3. Commute zone instead of a radius

- Precompute weekday-08:30 journey time from every rail/tube/DLR/Overground/Elizabeth station to
  each user's destination (one batch per destination, refreshed monthly).
- A home's commute estimate is walk to its nearest stations plus the station time. The exact TfL
  door-to-door call is made only for homes that pass the estimate.
- The search area is the set of stations within the user's limit plus a margin, not a circle.
- Per-user extras: a second destination (for example an office and a partner's office), maximum
  changes, maximum walk, and an optional cycling time.

### 4. True cost

- Hard cap on advertised rent. The all-in figure is shown and ranked on, and may optionally be
  capped too.
- Council tax from the advert's band (Rightmove publishes it, with an "included" flag) and the
  borough's published rate; energy scaled by EPC band and floor area when known; water and
  broadband as flat estimates. Every estimated component stays labelled.
- Up-front cash: deposit plus first month, shown on the card.

### 5. Scoring, not gating

- Only true dealbreakers are hard (rent cap, bedrooms, explicit exclusions such as house shares).
- Everything else feeds a per-user score with visible reasons: all-in cost, commute friction,
  space (m², £/m²), value against the archive's median for that micro-area and size, crime,
  green space and daily amenities (OpenStreetMap), condition and light, outdoor space.
- **Stretch band**: homes just outside a soft limit are shown, labelled with what is over
  ("+4 min commute", "£40 over").
- Weights are learned from Save/Hide and reason chips with a small regularised model, replacing
  the hand-tuned taste rules. When there is too little signal, the bot asks an occasional
  "A or B?" question.
- **Feasibility report** (weekly, and on demand): how many homes per week fit the current
  criteria, how fast they go, and the cheapest relaxations ("+5 min commute adds 9 a week").

### 6. Understanding adverts

- A language model extracts structured facts from each description once, cached by content
  hash: furnished state, floor and lift, heating, bills, pets, outdoor space, minimum term,
  condition, and whether it is really a room, studio or short let. This replaces most of the
  keyword rules.
- For shortlisted homes only: read floor area from the floorplan image when the advert omits it,
  and rate light and condition from photos.
- Scam and bait signals for private-landlord sources: price far below the area median, stock
  photos, off-platform contact requests.

### 7. Telegram: decide and act

- **Card**: rent against cap, all-in, "listed 3 h ago" (the portal's date), commute, the top two
  reasons for and against, value against area median, move-in date against the user's date,
  agent name. Buttons: Save, Hide, Call agent, More photos (album on demand), Enquired.
- **Urgency tiers**: top-scoring new homes notify immediately, even in quiet hours if the user
  opts in; ordinary matches arrive normally; stretch-band homes are batched into a digest.
- **Pipeline**: New → Enquired → Viewing booked → Viewed → Offer → Won/Lost, with `/pipeline`,
  viewing reminders, a viewing checklist, notes and a rating after each viewing, and a "gone"
  notice when a saved home is let.
- **Enquiry helper**: a ready-to-send message built from the user's renter profile (move date,
  occupation, references ready). The bot drafts; the person sends.
- **Mini App** (Telegram web view served from the VPS): map of the commute zone with homes,
  sliders for settings with a live count of matching homes, shortlist comparison, pipeline board.
- **Onboarding interview** for each person: destination, dates, dealbreakers, trade-offs.

### 8. Two people

Each has an independent profile, pipeline and learned weights over the shared archive. Adding
the second person costs no extra crawling. Optional later: "within N minutes of each other".

### 9. Operations

- Service-level measures: time from portal listing to alert, head-poll recall, per-source health.
- Off-box backup of the database, an external heartbeat, and a restore drill (all still pending).
- The same code runs on the Pi; the fetch-node split means the move can be gradual.

## Sources

Working on the VPS: Rightmove, OnTheMarket, OpenRent (24 h delay for anonymous visitors),
SpareRoom whole flats, L&Q rent schemes. Probes from a home connection (see "Probe results")
add ShareToBuy and agents' own sites as fetch-node sources. Zoopla stays blocked without a
real browser or a paid fetch service; that is a separate decision.

Rightmove's pages state that automated use needs its permission. Wider crawling raises that
exposure; keep request volume modest and paced, and treat every portal the same way.

## Delivery order

| Phase | Scope | Unblocks |
| --- | --- | --- |
| 0 | Rent cap as the hard limit; council-tax-based bills; card shows rent and all-in | Immediately recovers the £1,520–1,600 band |
| 1 | Archive tables, users as queries, head poll + price-sliced deep sweep, coverage audit | Everything below |
| 2 | Station commute zone, stretch band, what-if and feasibility report | Search stops being a circle |
| 3 | New card, urgency tiers, pipeline, enquiry helper, onboarding | Acting on matches |
| 4 | Advert extraction with a language model, learned weights, cross-portal homes | Precision without losing recall |
| 5 | Mini App, fetch node on the Pi, extra sources, floorplan/photo reading | Reach and comfort |

## Work package 0 (ready to build)

Goal: the hard limit is advertised rent; bills are estimated from real data and shown, not
used to hide flats.

1. Add a per-user parameter `rent` (config `MAX_RENT`, default 1600, £300–£10,000): the maximum
   advertised monthly rent. Portal `maxPrice`/`max-price`/`prices_max` and every local
   `RENT_MIN <= rent <= cap` check use it instead of `MAX_MONTHLY_COST`.
2. `budget` becomes an optional all-in cap for rentals: unset by default, applied only when a
   person sets it. Sale-tenure behaviour (the cloud path) must not change.
3. Split the bills estimate into council tax and utilities:
   - Capture `livingCosts.councilTaxBand` and `councilTaxIncluded` from the Rightmove details
     page (and the equivalent from OnTheMarket if its details expose one).
   - Council tax = borough Band D for 2026/27 × the statutory band ratio (A 6/9, B 7/9, C 8/9,
     D 1, E 11/9, F 13/9, G 15/9, H 18/9), divided by 12. Take Band D figures from the official
     published table, record source and year next to the data, and fall back to a London-wide
     figure with band C when band or borough is unknown (labelled as estimated).
   - Utilities stay a per-user estimate (`bills` now means energy + water + broadband; choose a
     default so that band C + utilities stays near the old 280 total).
4. Cards and report lines lead with rent and then all-in, for example
   `£1,550 rent · ~£1,812 all-in (council tax B £131 · utilities ~£131)`. Ranking uses all-in.
5. Funnel diagnostics gain separate `rent_cap` and `all_in_cap` rejection reasons.
6. Settings migration: an existing stored `budget` must not silently become a rent cap. Leave
   stored values alone; `/settings` explains both parameters.
7. Tests for each rule above; the whole suite passes. No deployment and no VPS changes as part
   of this package.

## Work package 1 (ready to build after package 0)

Goal: the fast poll really sees new adverts, and the facts the portals already give us reach the
cards.

1. **OnTheMarket head poll is not newest-first.** `otm_rent.search` sends no sort parameter, so
   it reads the first three pages of the site's "recommended" order (90 of 312 results at 5 mi).
   Add `sort-field=update_date` (observed working, descending, mixes new and reduced adverts).
2. Capture free fields and store them on the listing: Rightmove `firstVisibleDate`,
   `listingUpdate` (reason and date), `bathrooms`, `formattedBranchName`; OnTheMarket
   `days-since-added-reduced`, `agent`, `bathrooms`, deposit from `fees-description`.
3. Cards show the portal's own age ("Added today", "Reduced yesterday") and the agent name.
4. An out-of-range Rightmove page (HTTP 200 without `__NEXT_DATA__`, seen at index 1008) must
   end pagination cleanly instead of raising "blocked or layout changed".

## Work package 2 (market archive and deep sweep)

Goal: the watcher stops seeing only the newest few pages inside a 5-mile circle, and every
decision it makes is remembered so a setting's cost and the search's completeness can be
measured. VPS only (`STORAGE=local`); the cloud path is untouched. Intent: blueprint sections 1
and 2.

Why: Rightmove holds 118 flats at 5 miles and 1,268 at 10 miles for rent up to £1,600, and about
half of locations 8-16 km out are within the 40-minute commute limit. Nothing is stored about
rejected flats.

1. **Archive** (`pisobot/archive.py`, primary SQLite database, added without changing
   `worker/schema.sql`): `adverts` (one row per `source:id`: latest facts, first/last seen, the
   portal's first-listed date, status active/unseen), `observations` (a row whenever rent or status
   changes), `verdicts` (per person and advert: latest outcome, `matched` or the first funnel
   rejection, with rent, all-in, travel minutes and crime score when known), `sweeps` (one row per
   deep sweep: scope, requests, adverts seen, adverts the head poll had never seen, complete or
   not). Every advert fetched in a local rental cycle is upserted and every per-person outcome
   recorded, with no extra network requests.
2. **Deep sweep** for Rightmove and OnTheMarket: a full crawl of `DEEP_SWEEP_MILES` (10) from
   `RENT_MIN` to 110% of the highest rent cap among users, in price bands that are halved
   whenever a band reports more results than the portal will page through (Rightmove: 42 pages,
   1,008 results; OnTheMarket: 34 pages, about 1,000 results, see the probe results). It runs inside the watcher every `DEEP_SWEEP_HOURS` (12; 0 disables) through
   the existing paced fetcher, resumes across cycles in slices of about two minutes so the
   15-minute head poll is not delayed, stops at `DEEP_SWEEP_MAX_REQUESTS` (150) per source per
   sweep, and records an incomplete sweep as incomplete.
3. **Evaluate sweep adverts for each person** so matches the head poll never saw are alerted.
   Gates are reordered for private rentals so coordinate-only checks (distance, commute from the
   cached TfL journey, crime) run before any details-page fetch. A person's radius is not a hard
   filter for sweep adverts; the commute limit is. One marked hook is left where a cheap
   commute-zone prefilter (`pisobot/zone.py`, `could_reach`, built elsewhere) can run before the
   TfL call.
4. **Coverage audit**: each completed sweep stores how many in-scope adverts the head poll had
   not already seen. `/status` shows last scan time, per-source health, the last sweep's numbers
   and the head-poll recall.
5. **Gone detection**: an advert inside a swept scope that is missing from two consecutive
   complete sweeps becomes `unseen` (not `gone`: absence is the only evidence, and it may have
   been let, withdrawn or re-priced above the swept range) with an observation. Incomplete sweeps
   never mark anything. A saved home that becomes unseen triggers a "no longer found in the
   search" message saying so.
6. **`/whatif`**: from the last 7 days of a person's verdicts, how many more adverts would have
   matched with rent cap +£50/+£100, commute +5/+10 minutes, or the crime limit +25%. Only the
   first rejection reason is stored, so the answer is approximate and says so.
7. **No flood**: a person's first completed sweep records the matches it finds silently and sends
   one summary with the count, pointing to `/report`; later sweeps alert normally, within
   `MAX_ALERTS_PER_RUN`.

## Work package 3 (act fast and keep track)

Goal: the bot stops at a card with Save and Hide. In London a good flat is often let within days,
and people lose flats because they enquired late or lost track. Help the two users act quickly and
follow each home from first sight to signed tenancy. Intent: section 7. VPS only (`STORAGE=local`);
the cloud path is untouched.

1. **Pipeline.** Per person and advert a stage: new, saved, enquired, viewing booked (date and
   time), viewed (rating 1-5 and a free-text note), offer made, won, lost (optional reason). Cards
   gain an Enquired button; a saved or enquired home's card and `/pipeline` move a home along with
   buttons, and a viewing time and notes are set by replying in text. `/pipeline` lists homes by
   stage with rent, commute and the next action. Viewing reminders: the evening before and two
   hours before. When an archived advert in someone's pipeline changes rent or becomes `unseen`
   (work package 2), tell them once.
2. **Urgency tiers.** A new match is `top` when its ranking score is in the best fifth of that
   person's matches over the last 14 days of verdicts, or the portal first listed it within the
   last 3 hours. Top matches carry a visible marker and notify with sound even in quiet hours only
   if the person turned that on (`/set urgent on`, default off). Ordinary matches behave as today.
3. **Renter profile and enquiry draft.** `/profile` stores a few facts per person: name to sign
   with, move-in date or window, occupation, household, pets, smoker, references ready, anything
   else. An Enquiry text button replies with a short message built from the profile and the advert
   (address, the advert's available date against the move-in window, a request for the earliest
   viewing slots). The bot never sends anything to agents or portals; it only drafts text.
4. **Contact.** Where a portal's details page publishes the agent's phone number (Rightmove
   `contactInfo`), store it and show agent name and number on the card as plain text. Telegram
   inline URL buttons do not accept `tel:` links, so there is no call button; a plain number in
   message text is tappable.
5. **More photos.** A button sends the advert's remaining photos as an album on demand (media
   groups take 2-10 items and cannot carry inline buttons, hence on demand).
6. **Move-in fit.** With a move-in window in the profile and an available date on the advert, the
   card says whether it fits and by how many days it misses.
7. **Onboarding.** A new person's `/start` (and `/onboard`) asks destination, rent cap, commute
   limit, bedrooms, move-in window and profile facts one question at a time, with buttons where
   the answer is a choice, writing the same settings `/set` does. Resumable and skippable.

Constraints: `callback_data` stays within Telegram's 64 bytes; the durable update offset and the
allowlist in `bot.py` are unchanged.

## Probe results (2026-11-10, this Mac, plain HTTP, paced, no challenge solving)

Egress for the "home" column was a UK consumer ISP; the "VPN" column is an earlier run through
a VPN exit. The VPS column is from the 2026-11-08 deployment notes.

| Source | VPS | VPN | Home | Notes |
| --- | --- | --- | --- | --- |
| Zoopla | not tried plain | 403 | 403 | Cloudflare challenge on every path, robots.txt included. A home address alone does not unblock it. |
| PrimeLocation | – | 403 | 403 | Same Cloudflare challenge (same owner as Zoopla). |
| Home.co.uk | – | timeout | 403 | Cloudflare challenge on the one URL tried. |
| ShareToBuy rent search | 403 | 200 | 200 | Works off the VPS. Its robots.txt disallows `/properties/` for all agents. One London Living Rent home in range (£1,595). |
| Foxtons | – | 403 | 200 | Home address needed. `__NEXT_DATA__` with rent, beds, coordinates, photos. Filter parameter names not yet found. |
| Dexters | – | – | 200 | Server-rendered cards with rent, beds, postcode district; no coordinates. Search/filter URL not yet found. |
| Kinleigh Folkard & Hayward | – | – | needs JS | Listing data is loaded by script from a path its robots.txt disallows. |
| Gumtree | – | 200 | 200 | 30 cards a page, 2,917 London flats; area names only (no coordinates), filters untested, robots allows page 1 only. |

What this changes:

- **Zoopla needs a real browser or a paid fetch service**, wherever it runs. A fetch node on
  the Pi is still useful, but for ShareToBuy and agents' own sites, not for Zoopla.
- **Agents' own sites are reachable from home** and often carry a flat before the portals do.
  Each needs its own small adapter; Foxtons is the easiest first one.

Rightmove (10 mi, ≤£1,600, 1,268 results):

- Paging stops at 42 pages (1,008 results); index 1008 returns a "couldn't find the place" page
  with HTTP 200. So 260 results are unreachable in one query.
- `minPrice` works: £1,500–1,600 alone holds 680 of the 1,268, so bands must be narrow near the
  cap (for example £25 steps above £1,400).
- `maxDaysSinceAdded=1` returns 98 and `=3` returns 120; `propertyTypes=flat&maxBedrooms=1`
  returns 1,081.
- Searching by postcode district was not worked out.

OnTheMarket (≤£1,600, flats): 312 results at 5 mi and 1,655 at 10 mi, 30 per page. See work
package 1 for the sort finding. Observed paging behaviour (radius 10, max-price 1600,
sort-field update_date, 2026-11-10):

- `min-price` works: `min-price=1500` gives `totalResults` 703, all priced 1500-1600.
- `totalResults` is present on normal pages (1,655 for the whole query), and
  `paginationControls` also carries a total-count key.
- One query serves 34 pages (about 1,000 results). Page 34 returns 30 items with
  `paginationControls.next` false although the total is 1,655; pages 50 and 56 return an empty
  list with `totalResults` None and no pagination keys. A missing next link therefore proves
  nothing: the deep sweep decides from the band's first-page total (split when it exceeds
  1,000), and treats reaching page 34, or an empty page past the first, as completion only when
  the adverts read equal that band's total.

TfL: one call to `StopPoint/Mode/tube` returns 1,751 points covering 345 stations with
coordinates (group by `stationNaptan`). Five keyless journey calls averaged 1.1 s with no rate
limiting seen; sustained throughput is untested. Other modes need the same call per mode.
