"""Text for the /status and /whatif Telegram commands (VPS). Reads the archive and the watcher's
own records; makes no requests."""
import datetime as dt
import html
import importlib
import json
import logging
import os

from . import archive, config, users

WHATIF_DAYS = 7
NAMES = {"rm": "Rightmove", "otmr": "OnTheMarket"}
log = logging.getLogger(__name__)


def _primary(sql, *params):
    return users._primary(sql, *params)


def _ago(then, now):
    minutes = max(0, int((now - then).total_seconds() // 60))
    if minutes < 90:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago" if minutes < 48 * 60 else f"{minutes // 1440} days ago"


def _when(iso, now):
    then = dt.datetime.fromisoformat(iso)
    return f"{then:%d %b %H:%M} UTC ({_ago(then, now)})"


def status_text(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    meta = {r["name"]: r["value"] for r in _primary(
        "SELECT name, value FROM meta WHERE name IN ('watch_last_success', 'watch_failures', 'watch_degraded_adverts')")}
    lines = ["<b>📡 PisoBot status</b>"]
    if meta.get("watch_last_success"):
        lines.append(f"Last complete scan: {_when(meta['watch_last_success'], now)}")
    else:
        lines.append("Last complete scan: none recorded yet")
    failures = int(meta.get("watch_failures") or 0)
    if failures:
        lines.append(f"⚠️ {failures} consecutive incomplete scan(s)")

    funnel = _primary("SELECT created_at, stats FROM rental_funnel ORDER BY id DESC LIMIT 1")
    if funnel:
        stats = json.loads(funnel[0]["stats"])
        down = set(stats.get("source_failures") or [])
        lines.append(f"\n<b>Sources</b> (latest scan, {_when(funnel[0]['created_at'], now)})")
        for name, row in sorted((stats.get("sources") or {}).items()):
            lines.append(f"• {name}: ⚠️ failed" if name in down
                         else f"• {name}: {row.get('fetched', 0)} fetched · {row.get('matched', 0)} matched")

    degraded = int(meta.get("watch_degraded_adverts") or 0)
    if degraded:
        lines.append(f"⚠️ Latest cycle: {degraded} advert lookup(s) waiting or degraded; source scan is live.")
    chat_id = users.current_chat() or users.owner_chat() or "owner"
    work = archive.work_counts(chat_id)
    if work.get("intent") or work.get("backoff") or work.get("failed"):
        lines.append(f"\n<b>Your advert work</b>: {work.get('intent', 0)} pending notification intent(s), "
                     f"{work.get('backoff', 0)} lookup(s) in backoff, {work.get('failed', 0)} legacy failure(s), "
                     f"{work.get('blocked', 0)} intent(s) blocked by current criteria.")
        if work.get("failed"):
            lines.append("Previously exhausted lookups now retry automatically with a one-hour cooldown:")
            for row in archive.failed_work(chat_id):
                label = html.escape(str(row.get("address") or row["key"])[:60])
                url = html.escape(str(row.get("url") or ""), quote=True)
                home = f'<a href="{url}">{label}</a>' if url else label
                lines.append(f"• {home} — {row['failures']} attempts; slow retry scheduled")

    lines.append("\n<b>Deep sweep</b>")
    if config.DEEP_SWEEP_HOURS <= 0:
        lines.append("• off (DEEP_SWEEP_HOURS=0)")
    else:
        for source, name in NAMES.items():
            running, last = archive.running_sweep(source), archive.last_sweep(source)
            if last is not None:
                state = "complete" if last["complete"] else f"incomplete ({last['note'] or 'unfinished'})"
                recall = archive.recall(last)
                line = (f"• {name}: {state}, {_when(last['finished_at'], now)} · {last['requests']} requests · "
                        f"{last['adverts_seen']:,} adverts · {last['unseen_by_head']:,} the regular scan had not shown")
                line += (f"\n  regular-scan recall: <b>{recall:.0%}</b>" if recall is not None else "")
                lines.append(line)
            elif running is None:
                lines.append(f"• {name}: not run yet")
            if running is not None:
                st = running["state"]
                lines.append(f"• {name}: running, {running['requests']}/{config.DEEP_SWEEP_MAX_REQUESTS} requests, "
                             f"{len(st.get('queue', []))} price band(s) queued, {running['adverts_seen']:,} adverts so far")
    if os.environ.get("ALERT_TIMING") == "1":
        try:
            timing = importlib.import_module(".timing", __package__)
            extra = timing.status_lines(now)
            if type(extra) not in (list, tuple) or len(extra) > 20 or any(type(line) is not str for line in extra):
                raise ValueError("invalid optional status")
            lines.extend(extra)
        except Exception as ex:
            log.warning("optional timing status unavailable (%s)", type(ex).__name__)
    counts = archive.counts()
    lines.append(f"\n<b>Archive</b>: {counts.get('active', 0):,} active adverts · {counts.get('unseen', 0):,} unseen")
    return "\n".join(lines)


def whatif_counts(rows, rent, commute, crime):
    """How many of these verdicts were rejected for a limit by no more than the given margin."""
    def within(reason, field, current, limit):
        return sum(1 for r in rows if r["outcome"] == reason and r[field] is not None
                   and current < r[field] <= limit)
    return {
        "matched": sum(1 for r in rows if r["outcome"] in ("matched", "unsent")),
        "judged": len(rows),
        "rent": {50: within("rent_cap", "rent", rent, rent + 50), 100: within("rent_cap", "rent", rent, rent + 100)},
        "commute": {5: within("travel", "travel_minutes", commute, commute + 5),
                    10: within("travel", "travel_minutes", commute, commute + 10)},
        "crime": within("crime", "crime_score", crime, crime * 1.25),
    }


def whatif_text(person, at=None):
    values = users.effective(person)
    from .main import settings_key
    rows = archive.verdicts_since(person.chat_id, WHATIF_DAYS, at, settings=settings_key(person))
    if not rows:
        return ("<b>🔮 What if…</b>\nNothing has been judged for your current settings in the last 7 days yet. "
                "Try again after the next scan.")
    n = whatif_counts(rows, values["rent"], values["commute"], values["crime"])
    return "\n".join([
        f"<b>🔮 What if…</b> (last {WHATIF_DAYS} days: {n['judged']:,} adverts judged, {n['matched']:,} matched when judged)",
        f"• Rent cap £{values['rent'] + 50:,.0f} (+£50): <b>+{n['rent'][50]}</b>",
        f"• Rent cap £{values['rent'] + 100:,.0f} (+£100): <b>+{n['rent'][100]}</b>",
        f"• Commute {values['commute'] + 5} min (+5): <b>+{n['commute'][5]}</b>",
        f"• Commute {values['commute'] + 10} min (+10): <b>+{n['commute'][10]}</b>",
        f"• Crime limit {values['crime'] * 1.25:,.0f} (+25%): <b>+{n['crime']}</b>",
        "\n<i>Approximate increments, not lower or upper bounds; one limit at a time. Each advert is judged only until its first failing "
        "check, so adverts that fail two limits are not counted, some counted here may still fail a later "
        "check, and anything the bot never fetched is missing. This is a historical sample, not availability. "
        "Change a setting with /set to find out for "
        "certain.</i>",
    ])
