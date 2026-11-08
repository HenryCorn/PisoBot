# Hostinger VPS rental access experiment

Date: 2026-11-08 UTC. Requests ran in the user's existing VPS SSH pane.
This is a bounded access experiment, not deployment or a full Phase 0 pipeline run.

## Method

Python requests, PisoBot's current User-Agent and Accept-Language headers, verified
TLS, environment proxies disabled, no Decodo, no retries or challenge solving.
One request at a time; subsequent probes spaced roughly 4-6 seconds apart.
No packages installed, services changed, repository code edited, or Telegram messages sent.

The original baseline made three search requests. This extension made thirteen
requests: four each to Rightmove and OnTheMarket, one each to OpenRent,
PrimeLocation and Foxtons searches, then one Foxtons content inspection and one
OpenRent detail request. No further requests were made to blocked Zoopla or
PrimeLocation endpoints.

## Existing sources

Rightmove and OnTheMarket used existing PisoBot search URLs and filters:
Oxford Circus, 5-mile radius, at least one bedroom, maximum rent GBP 1,600.
These are raw portal result counts, not listings passing all PisoBot filters.

| Source | Search page 1 | Search page 2 | Detail | Repeat |
|---|---|---|---|---|
| Rightmove | 200, 25 rows | 200, 25 rows; 24 new IDs | 200; decoded property ID matches search | 200, 25 rows |
| OnTheMarket | 200, 30 rows | 200, 30 rows; 30 new IDs | 200; decoded property ID matches search | 200, 30 rows |
| Zoopla | Initial baseline: 403, "Just a moment...", no listing data | Not attempted | Not attempted | Not attempted |

Rightmove returned the expected __NEXT_DATA__ search schema and flattened
window.__PAGE_MODEL detail schema. OnTheMarket returned the expected
initialReduxState results and property schemas. Both detail responses contained
the field names used by current adapters, including descriptions, floorplans,
letting information and size-related fields. This does not establish that every
field has a usable value.

Each repeat search occurred about 60 seconds after its corresponding first search.
The first baseline was approximately eight minutes earlier. This is not a
sustained-polling or rate-limit test, and not a 24-hour soak.

## Additional sources

These were broad London searches, not searches constrained to the target budget,
bedrooms or radius. Counts are links, not parsed Listing objects.

| Source | VPS observation | Integration implications |
|---|---|---|
| OpenRent | Search 200, 20 distinct property-detail links; one detail 200 | Plain HTML is accessible; new adapter and room/shared-let exclusions needed |
| Foxtons | Search 200 twice; actual property links, __NEXT_DATA__, and JSON-LD Product/ItemList types present | Potential additional agency adapter; property extraction not implemented or validated |
| PrimeLocation | 403, "Just a moment..." challenge | Not directly usable with this client |

The sampled OpenRent detail was GBP 2,400/month, outside the user's budget; it
was used only to test detail accessibility.

Foxtons includes `/cdn-cgi/challenge-platform/scripts/jsd/main.js` in a successful
200 listing page. The substring `challenge-platform` alone is not reliable proof
that a page is blocked. A future fetch chain must distinguish a challenge
interstitial from valid listing content containing a background detection script.

## Research and access constraints

- OpenRent's [terms](https://www.openrent.co.uk/terms), section 1.1, prohibit
  harvesting information and copying material without permission. Technical
  accessibility is not permission for continuous automated collection.
- OpenRent documents [syndication to Rightmove and OnTheMarket](https://help.openrent.co.uk/hc/en-gb/articles/360002266772-Which-major-portals-do-you-advertise-with).
  Added coverage is not measured; do not assume listings are unique.
- OpenRent offers [search email alerts](https://help.openrent.co.uk/hc/en-gb/articles/360018359252-How-do-the-search-bar-and-email-alerts-work).
- Foxtons' [terms](https://www.foxtons.co.uk/help/termsconditions) restrict
  copying/distribution, especially commercial use. Its
  [robots.txt](https://www.foxtons.co.uk/robots.txt) disallows some API and
  filtered-search paths. No public listing API was verified.
- PrimeLocation's [terms](https://www.primelocation.com/terms/) prohibit
  automated collection without consent. Its live VPS response was blocked
  despite the research tool being able to fetch its public search page.
- Zoopla's [developer documentation](https://developers.zoopla.co.uk/README.md)
  says the listings API is no longer publicly available; its other documented
  APIs serve agents/advertisers rather than personal listing search.
  [Saved-search email alerts](https://help.zoopla.co.uk/hc/en-gb/articles/360005969458-How-do-I-create-email-alerts-and-saved-searches)
  are an alternative. No challenge bypass was attempted.

## Recommendation and outstanding work

Use the existing Rightmove and OnTheMarket adapters as the technical starting
point for a direct-only rental deployment, subject to the owner's terms review.
Consider OpenRent next if access permission is resolved; Foxtons is another
technically accessible candidate. Keep Zoopla and PrimeLocation out of direct
polling until an approved working access route exists.

Outstanding: whole pipeline dry-run, matching/ranking and detail semantics,
new-source adapters, cross-source deduplication, filtered candidate searches,
Telegram interactions, restart persistence, and a 24-hour low-volume soak.
No deployment or background polling was started.

Evidence: vps-probe-results.json, vps-rightmove-deep.json, vps-otm-deep.json,
vps-extra-results.json and vps-content-results.json in this session's files directory.
