# Experimental station commute zones

This is a **standalone experiment**, not connected to `main.py`, `watch.py`, users,
portal searches or the production cache. Do not deploy it as a hard filter on the
strength of the current validation. The model constants were chosen before the
live build and were not tuned to meet the recall target.

## Follow-up

This module is intended to **order exact journey lookups**, not to hard-filter
adverts. Validation now also reports margins 15 and 20 for both cohorts. One
additional National Rail retry timed out at 40 seconds; the omission remains.
`national-rail-retry.json` records that attempt. Cumulative live traffic is now
998 calls, 7 failed attempts. Follow-up suite: `Ran 242 tests in 1.006s`, `OK`.
The original build figures below describe the initial experiment.

## Reproduce

From the worktree root (the existing virtualenv can be used without installing
anything):

```sh
/Users/quique/repo/PisoBot/.venv/bin/python -m pisobot.zone build \
  --dest 51.5152,-0.1419 --name oxford-circus
/Users/quique/repo/PisoBot/.venv/bin/python -m pisobot.zone build \
  --dest 51.523375,-0.075246 --name shoreditch-high-street
/Users/quique/repo/PisoBot/.venv/bin/python -m pisobot.zone validate
/Users/quique/repo/PisoBot/.venv/bin/python -m unittest discover -s tests
```

Run the builders **sequentially**, not concurrently: each client is paced with at
least 1.51 seconds between request starts (less than 40/minute), including failed
requests and discovery. HTTP has no automatic retries or redirects; only
`https://api.tfl.gov.uk` is called. `TFL_APP_KEY`, if present in the environment,
is sent as `app_key`, never printed or written into snapshots. Requests time out
after 40 seconds. No property portal, paid service or VPS access is involved.

A stopped build resumes from per-station atomic checkpoints; successful and
explicit no-route results are reused, errors retried on the next invocation.
All results share the snapshot's original next-weekday travel date until the
snapshot expires after 30 days. `--refresh` starts a new snapshot and refreshes
station discovery. Run monthly to renew it. An existing name cannot accidentally
be reused for different coordinates or a different build radius.

`--data-dir PATH` changes the build directory. Validation accepts `--destination
PATH.json` and `--cache PATH`. Its default cache is the **read-only** main
checkout's `../PisoBot/state/cache.json` in this worktree arrangement, otherwise
the checkout's own `state/cache.json`. It never populates or rewrites that cache.

## Data and provenance

- `stations.json`: TfL `StopPoint/Mode/{tube,dlr,overground,elizabeth-line}`,
  fetched on 10 November 2026. Per-mode raw point/group counts and fetch timestamps
  are in the file. `stationNaptan` groups entrances and platforms; modes are
  merged across endpoint responses. A station node's position is preferred,
  otherwise the mean of unique entrance coordinates is used. Nearby but distinct
  NaPTAN groups are **not** guessed to be the same station.
- `discovery.json`: initial discovery call count and failure notes. The first
  tube probe was reused as the tube response, not requested twice.
- National Rail: `StopPoint/Mode/national-rail` timed out twice at 40 seconds.
  It was therefore omitted rather than replaced with a non-TfL data source.
  The module supports it when usable, clipping its points to the approximate
  Greater London rectangle **51.28–51.70 N, -0.51–0.34 E** (not an exact borough
  polygon). The four other mode endpoints are required. The optional error is
  explicitly persisted, including in destination files. A failed optional mode
  is cached for 30 days to avoid a timeout on every resume; `--refresh` retries it.
- `oxford-circus.json`, `shoreditch-high-street.json`: destination coordinates,
  station table subset within **20 km**, travel date/time, per-station observation
  times, successful/no-route/error status, and per-invocation call/failure counts.
  Shoreditch High Street's coordinates are from the live TfL Overground response
  (`910GSHRDHST`), not a guessed neighbourhood centre.
- `validation.txt`: verbatim offline validation output. Explicit destination
  keys win over legacy keys at the same origin; the report gives entry and
  unique-point counts so duplicates do not distort the sample. Destinationless
  legacy entries are assumed to be Oxford Circus for the requested comparison,
  but that assumption is flagged and an explicit-destination-only cohort is
  also reported (see measured results below).
- `build-summary.json`: aggregate live calls/failures, cache SHA-256 and unit-test
  result. Test fixtures never contribute to these live-call counts.
- `stops/`: ignored local full StopPoint responses for discovery resume. These
  bulky API payloads are not required for offline estimation; a fresh machine
  fetches them once when building. `build.log` is also ignored; counters and
  safe failure codes are retained in the committed JSON snapshots.

Journey requests deliberately match `enrich.journey()`:

```
/Journey/JourneyResults/<station LAT to 5dp>,<station LON to 5dp>/to/<dest LAT>,<dest LON>
?date=<next weekday YYYYMMDD>&time=0830&timeIs=Departing
```

No mode restriction is added: the station-to-destination journey can use a bus or
walk, just like the current exact API. The fastest `duration` returned is kept.
These are coordinate-based trips from station pins, not platform-to-platform
running times. No extra entry-time correction has been fitted to the cache.

## Offline API

```python
from pisobot import zone

oxford = zone.load("oxford-circus")  # load once, pass the object in hot loops
answer = zone.estimate(51.5, -0.1, oxford)
if answer is not None:
    print(answer.minutes, answer.station.name, answer.walk_minutes)
keep_for_exact_check = zone.could_reach(51.5, -0.1, oxford, limit=40, margin=10)
shape = zone.portal_zone(oxford, limit=40, margin=10)
# shape.stations is a frozenset of Station objects with id/name/lat/lon/modes.
# shape.centre is (lat, lon), shape.radius_km includes walking catchment disks.
```

Walking is **4.8 km/h**, multiplying straight-line Haversine distance by **1.25**
for detours: 15.625 minutes per straight-line kilometre. `estimate` minimises
walking + cached station time over the **nearest five groups**. The returned
station is the estimated access station, not a guarantee of the first vehicle in
the exact TfL route (station-origin journeys may themselves start with a walk).

An unknown nearby station result makes the estimate unknown (`None`), rather
than using only the other stations and falsely rejecting a home. `could_reach`
keeps unknowns, stale snapshots and points outside the 20 km build domain. A
failure at a distant station does not disable all filtering. An optional mode
omission is a **model coverage limitation**, not a failed nearby journey: it does
not disable validation/filtering over the available modes. Always inspect the
mode errors before integrating this into a hard gate.

The portal helper returns the stations whose journey time is <= limit + margin,
plus an enclosing circle of their remaining-time walking catchments. This is a
bounding search hint, **not an isochrone polygon**. Its `complete` flag is false
for stale data, incomplete station journeys or omitted modes; `missing_modes`
lists omitted endpoints. The radius is not rounded to portal-specific allowed
mile values; that belongs in a future integration.

## Accuracy limits

The model is not a mathematical lower bound on true commute time. Bus-first
routes, National Rail gaps, river/road barriers, multiple NaPTAN groups at an
interchange, station exits, and departure-time-dependent waiting all matter.
The cache's dates are not recorded, whereas the new snapshots have an explicit
weekday date. Exact cache locations are also rounded to about 100 m.

Validation reports mean absolute error, nearest-rank 90th-percentile absolute
error, and actual prefilter confusion counts at margins 0, 5 and 10. Unknown
estimates are excluded from error magnitudes but retained in filter denominators
and kept by the prefilter. Empty/no-route exact cache entries would be skipped,
not labelled unreachable. The target is <2% discarded truly reachable points
and >50% discarded unreachable points at one of the tested margins. A failed
target is reported as such, not hidden by changing constants or picking a margin
after seeing the labels.

## Measured results, 10 November 2026

Both builds completed a first pass and one retry of their failed station. They
use 11 November 2026, 08:30 Departing. Discovery contains **566** merged groups:
345 tube, 60 DLR, 154 Overground and 57 Elizabeth-line groups before cross-mode
merging (these counts overlap).

| Live calls | Attempts | Failed attempts | Final usable journeys |
| --- | ---: | ---: | ---: |
| StopPoint discovery | 6 | 2 | 4 usable mode responses |
| Oxford Circus | 501 | 2 | 499 of 500 groups |
| Shoreditch High Street | 490 | 2 | 488 of 489 groups |
| **Total** | **997** | **6** | |

National Rail accounts for the two discovery timeouts. Camden Town Underground
(`940GZZLUCTN`, coordinate origin `51.53929,-0.14274`) returned HTTP 404 for each
destination on both attempts. It remains an explicit retryable error; it was
not reclassified as no-route, removed or replaced with a guessed time. Its
presence makes each journey snapshot incomplete; none of the validation points
had it in their nearest five groups, so there were no unknown estimates there.

**The target was not met.** With all 545 entries processed as requested, there
are 380 unique origins after 165 qualified results supersede legacy duplicates.
Mean absolute error is 6.78 minutes; 90th percentile is 16.30 minutes. At margin
10, 9.68% of truly reachable origins are wrongly discarded, while only 44.85%
of unreachable origins are correctly discarded. The constants remain unchanged.

**Ground-truth caveat:** 285 cache entries have no destination in their key;
247 of them end their recorded route on Overground (Windrush), which suggests
Shoreditch rather than Oxford Circus. The summary cache has no final-leg
coordinates or travel dates with which to verify that. We retain the requested
assumed-Oxford comparison rather than silently deleting or relabelling them,
and additionally show the 260 explicitly Oxford-keyed results. That cohort has
mean absolute error 3.67 minutes and 90th percentile 8.72 minutes. Margin 10
wrongly discards 2/113 reachable origins (1.77%) but correctly discards only
52/147 unreachable origins (35.37%), so **it still fails the combined target**.
No extra live exact journeys were requested to repair the historical labels.

The requested cohort's 18 false discards at margin 10 include **17 bus-first**
exact routes and one DLR-first route. Missing feeder buses, not just missing
rail coverage or a single walking constant, are an important limitation. Future
work should improve the access model and collect clearly destination/date-keyed
holdout journeys before using this as a hard prefilter. No production behaviour
was changed, and Shoreditch accuracy remains unvalidated because there is no
explicitly Shoreditch-keyed exact validation cohort in the supplied cache.

Full unit suite, main checkout's virtualenv with this worktree as the working
directory:

```text
Ran 242 tests in 0.942s

OK
```
