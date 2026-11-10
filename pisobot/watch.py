"""Single-process rental scheduler and Telegram long-polling service."""
import argparse
import datetime as dt
import json
import logging
import os
import queue
import random
import signal
import threading
import time

from requests import RequestException

from . import archive, cache, config, d1, main as pipeline, net, sweep, telegram, users

log = logging.getLogger(__name__)


def _meta(name):
    rows = d1.query("SELECT value FROM meta WHERE name = ?", name)
    return rows[0]["value"] if rows else None


def _put_meta(name, value):
    d1.query("INSERT INTO meta (name, value) VALUES (?, ?) "
             "ON CONFLICT(name) DO UPDATE SET value = excluded.value", name, str(value))


class Watcher:
    def __init__(self, sources=None, verbose=False):
        self.sources = sources if sources is not None else config.WATCH_SOURCES.split(",")
        if not self.sources or any(name not in pipeline.SOURCES for name in self.sources):
            raise ValueError("WATCH_SOURCES must contain registered rental sources")
        if any(name not in pipeline.LOCAL_RENTAL_SOURCES for name in self.sources):
            raise ValueError("VPS watch supports " + ", ".join(pipeline.LOCAL_RENTAL_SOURCES) + " only")
        if config.WATCH_INTERVAL_MIN <= 0 or config.WATCH_FAILURE_LIMIT < 1:
            raise ValueError("Watch interval and failure limit must be positive")
        self.verbose = verbose
        self.pending = queue.Queue(maxsize=1)
        self.guard = threading.Lock()
        self.running = False

    def request_run(self, kind, chat_id=None):
        """chat_id: who asked for a report; None is the owner. Scans always cover everyone."""
        if kind not in ("rentals", "report"):
            raise ValueError("Unknown run kind")
        with self.guard:
            if self.running or not self.pending.empty():
                return False
            self.pending.put_nowait((kind, chat_id))
            return True

    def cycle(self, kind="rentals", stop_event=None, dry_run=False, chat_id=None):
        """Share identical portal searches, then apply each person's criteria and delivery."""
        people = users.everyone()
        report_chat = str(chat_id) if chat_id is not None else people[0].chat_id
        totals, errors = {"matches": 0, "complete": True, "people": {}, "degraded": 0}, []
        alert_budgets = {p.chat_id: pipeline.new_alert_budget() for p in people}
        with pipeline.locked():
            for params, group in users.search_groups(people):
                if stop_event is not None and stop_event.is_set():
                    raise InterruptedError("PisoBot is stopping")
                for source in self.sources:
                    # Fast portals are re-searched every cycle; slow-changing sources keep
                    # their own refresh interval (RENT_REFRESH_MINUTES on the source).
                    if not hasattr(pipeline.SOURCES[source], "RENT_REFRESH_MINUTES"):
                        cache.put("rent_search", source, {})
                with users.criteria(params):
                    prefetched = pipeline.fetch_sources(self.sources, stop_event)
                if not dry_run:
                    pipeline.archive_adverts(prefetched[0])     # no requests: just remembers what was fetched
                for person in group:
                    if not person.owner and users.get(person.chat_id) is None:
                        continue
                    try:
                        with users.use(person), users.criteria(person.params):
                            result = self._person_cycle(kind if person.chat_id == report_chat else "rentals",
                                                        prefetched, stop_event, dry_run,
                                                        alert_budget=alert_budgets[person.chat_id])
                    except InterruptedError:
                        raise
                    except Exception as ex:
                        log.error("Rental cycle failed for %s (%s)", person.label, type(ex).__name__)
                        errors.append(ex)
                        totals["complete"] = False
                        continue
                    totals["people"][person.label] = result["matches"]
                    totals["matches"] += result["matches"]
                    totals["degraded"] += result.get("degraded", 0)
                    totals["complete"] = totals["complete"] and result["complete"]
            if not dry_run and sweep.enabled(self.sources):
                # After the head poll has alerted: a short, resumable slice of the deep sweep
                try:
                    sweep.step(self.sources, people, stop_event, self.verbose, alert_budgets=alert_budgets)
                except InterruptedError:
                    raise
                except Exception as ex:
                    log.error("Deep sweep step failed (%s)", type(ex).__name__)
        if errors:
            raise errors[0]
        if not totals["complete"]:
            raise RuntimeError("Rental cycle incomplete: source or enrichment failures")
        if not dry_run:
            _put_meta("watch_last_success", dt.datetime.now(dt.timezone.utc).isoformat())
            # Loop/source liveness is separate from lookup health. A successful heartbeat
            # must not hide an enrichment outage, including sweep-only inventory.
            retries = sum(archive.lookup_degraded(p.chat_id, pipeline.settings_key(p)) for p in people)
            totals["degraded"] = max(totals["degraded"], retries)
            self._lookup_health(totals["degraded"])
            healthcheck = os.environ.get("HEALTHCHECK_URL", "")
            if healthcheck:
                try:
                    response = net.session.get(healthcheck, timeout=10, verify=net.VERIFY)
                    response.raise_for_status()
                except RequestException as ex:
                    log.error("Healthcheck failed (%s)", type(ex).__name__)
        return totals

    def _person_cycle(self, kind, prefetched, stop_event, dry_run, alert_budget=None):
        person = users.current()
        settings = pipeline.settings_key(person)
        previous = _meta("watch_settings")
        # The deployed pre-archive watcher stored raw params JSON and authoritative seen
        # history. Recognize that specific format when the params still agree: otherwise
        # a quiet upgrade would swallow homes listed since the last live poll. Unknown
        # markers or explicit /set resets still take the conservative silent-baseline path.
        legacy_live = False
        if previous is not None and _meta("watch_initialized") and pipeline.load_seen():
            try:
                stored = json.loads(previous)
                legacy_live = isinstance(stored, dict) and stored == (person.params if person else {})
            except (TypeError, ValueError):
                pass
        first = not _meta("watch_initialized") or (previous != settings and not legacy_live)
        sweep_keys = set()
        if kind == "report" and not first and archive.enabled():
            # /report also lists the matches only the deep sweep has found
            shown = {l.key for l in prefetched[0]}
            extra = archive.matched_listings(person.chat_id if person else users.owner_chat(),
                                             sweep.swept(self.sources), exclude=shown)
            sweep_keys = {l.key for l in extra}
            prefetched = (list(prefetched[0]) + extra, prefetched[1])
        result = pipeline._run(self.sources, dry_run, self.verbose,
                               report=kind == "report" and not first, rentals=True,
                               watch=kind != "report" or first, baseline=first,
                               stop_event=stop_event, prefetched=prefetched,
                               sweep_keys=sweep_keys, alert_budget=alert_budget)
        if result["complete"] and not dry_run:
            _put_meta("watch_settings", settings)
            if first:
                _put_meta("watch_initialized", "1")
        return result

    def _lookup_health(self, degraded):
        """One push warning per sustained incident; transient send failure leaves it retryable."""
        _put_meta("watch_degraded_adverts", degraded)
        failures = int(_meta("watch_lookup_failures") or 0) + 1 if degraded else 0
        _put_meta("watch_lookup_failures", failures)
        if degraded and failures >= config.WATCH_FAILURE_LIMIT and _meta("watch_lookup_warned") != "1":
            try:
                telegram.send_text(f"⚠️ Advert lookups have been degraded for {failures} consecutive cycles "
                                   f"({degraded} waiting in the latest scan). Scans continue; failed lookups "
                                   "retry automatically with cooldowns capped at one hour. /status shows the backlog.")
            except Exception as ex:
                log.error("Lookup degradation warning failed (%s)", type(ex).__name__)
            else:
                _put_meta("watch_lookup_warned", "1")
        elif not degraded and _meta("watch_lookup_warned") == "1":
            try:
                telegram.send_text("✅ Advert lookups have recovered; the current retry backlog is clear.")
            except Exception as ex:
                log.error("Lookup recovery warning failed (%s)", type(ex).__name__)
            else:
                _put_meta("watch_lookup_warned", "0")

    def _failed(self):
        failures = int(_meta("watch_failures") or 0) + 1
        _put_meta("watch_failures", failures)
        if failures >= config.WATCH_FAILURE_LIMIT and _meta("watch_warned") != "1":
            try:
                telegram.send_text(f"Rental watch has had {failures} consecutive incomplete cycles. "
                                   "It will keep retrying; check the VPS journal.")
            except Exception as ex:
                log.error("Failure notification could not be delivered (%s)", type(ex).__name__)
            else:
                _put_meta("watch_warned", "1")

    def serve(self, stop_event, poll):
        poll_errors = []

        def listen():
            try:
                poll(self.request_run, stop_event)
                if not stop_event.is_set():
                    raise RuntimeError("Telegram polling stopped unexpectedly")
            except Exception as ex:
                log.error("Telegram polling stopped (%s)", type(ex).__name__)
                poll_errors.append(type(ex).__name__)
                stop_event.set()

        listener = threading.Thread(target=listen, name="telegram", daemon=True)
        listener.start()
        next_cycle = time.monotonic()
        try:
            while not stop_event.is_set():
                remaining = max(0, next_cycle - time.monotonic())
                try:
                    kind, chat_id = self.pending.get(timeout=min(remaining, 1))
                except queue.Empty:
                    if time.monotonic() < next_cycle:
                        continue
                    kind, chat_id = "rentals", None
                if stop_event.is_set():
                    break
                with self.guard:
                    self.running = True
                try:
                    result = self.cycle(kind, stop_event, chat_id=chat_id if kind == "report" else None)
                except InterruptedError:
                    if not stop_event.is_set():
                        raise
                    break
                except Exception as ex:
                    log.error("Rental cycle failed (%s)", type(ex).__name__)
                    self._failed()
                else:
                    _put_meta("watch_failures", "0")
                    _put_meta("watch_warned", "0")
                    log.info("Rental cycle complete: %s matches", result["matches"])
                finally:
                    with self.guard:
                        self.running = False
                next_cycle = time.monotonic() + config.WATCH_INTERVAL_MIN * 60 * random.uniform(0.75, 1.25)
        finally:
            stop_event.set()
            listener.join(timeout=40)
        if poll_errors:
            raise RuntimeError("Telegram listener failed; service restart required")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="one scan without starting Telegram polling")
    parser.add_argument("--dry-run", action="store_true", help="with --once: no Telegram sends or seen updates")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    if args.dry_run and not args.once:
        parser.error("--dry-run requires --once")
    if os.environ.get("STORAGE") != "local":
        parser.error("The VPS watcher requires STORAGE=local")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    with pipeline.run_lock("watch"):
        watcher = Watcher(verbose=args.verbose)
        if args.once:
            watcher.cycle(stop_event=stop, dry_run=args.dry_run)
        else:
            from . import bot
            watcher.serve(stop, bot.poll)


if __name__ == "__main__":
    main()
