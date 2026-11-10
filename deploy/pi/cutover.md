# Cutover, verification and rollback

## The invariant

**ONE host may scan and poll Telegram. Never both.** This includes manually
launched scanners, the Pi's import dry scan, old Actions schedules and any old
Worker webhook. Offline `--restore-test` does not scan and is safe.

Stop and **disable** the VPS watcher, wait for it to exit, then take a **new
final export**. Only after that may the Pi import/run its dry scan and then be
enabled. A live rehearsal archive is not the final archive. Otherwise Telegram
pollers conflict, alerts duplicate, and saved/hidden choices diverge.

The script's final-export metadata proves what the source checks observed at
export time, not that another administrator cannot restart it afterwards.
`--source-stopped HOST` is your explicit acknowledgement that the named source
is **still fenced**. This kit cannot establish a distributed lock. Keep one
administrator in charge of the switch.

## Before the maintenance window

Complete the README through provisioning, encryption and the offline rehearsal.
Record the VPS hostname/architecture, application commit, most recent successful
cycle, and expected saved/hidden/profile state through Telegram. Do not change
settings or sources during migration. Do not introduce the rent-cap changes as
part of this switch. Both services must use the exact exported app commit.

Check time sync, available scratch/state space and the SSD missing-mount guard
on the Pi. Check the old Actions workflow remains disabled and the Worker
webhook remains inactive. Retain the old Worker/D1 resources; this migration is
between two local SQLite hosts, not a return to the cloud.

Tell everyone using the bot that it will pause. Telegram may queue updates;
copying the primary DB preserves its polling checkpoint and user registry.
Avoid pressing buttons or changing settings until the handoff completes.

## 1. Fence the VPS (VPS terminal)

Prefer to stop immediately after a complete scheduled cycle to minimize the
gap and make a zero-new-alert first Pi cycle likely.

```sh
hostname -s                         # confirm this is the VPS
sudo journalctl -u pisobot-watch --since '30 minutes ago' --no-pager
sudo systemctl disable --now pisobot-watch.service
sudo systemctl show pisobot-watch.service \
  -p ActiveState -p SubState -p UnitFileState -p MainPID -p Result
```

Required: `ActiveState=inactive`, `SubState=dead`, `UnitFileState=disabled`,
`MainPID=0`, normally `Result=success`. The unit allows up to 180 seconds for
shutdown. If it timed out/was killed or the last cycle failed, inspect the
journal before continuing; an abrupt stop just after Telegram accepts a message
can still duplicate that message before its local checkpoint.

Stop any manual scanners/schedulers as well. The final exporter also tries the
actual `watch.lock` and `run.lock`; a held lock is a hard failure. Do not remove
lock files to bypass it. **Keep the VPS disabled, even across a reboot.**

## 2. Make the authoritative export (VPS terminal)

Use a fresh filename for each attempt, and remember it. Replace the literal
hostname/architecture with the VPS inventory; no secret values go in arguments.

```sh
sudo env PISOBOT_EXPECT_HOST=REPLACE_WITH_VPS_HOSTNAME PISOBOT_EXPECT_ARCH=x86_64 \
  PISOBOT_AGE_RECIPIENTS=/root/pisobot.recipients \
  bash /opt/pisobot/migration/vps-export.sh \
  --final --archive /var/backups/pisobot/final-REPLACE_WITH_UTC_TIMESTAMP.age
```

Expect `quiesced=True; final=True`, a database count and the encrypted archive's
SHA256. Export checks the installed source service is inactive/disabled; it
does not stop it for you. It snapshots all owner/guest DBs, seen/cache JSON,
tiles and the environment. Keep the final archive and checksum for rollback.
Transfer **only** `.age` and `.age.sha256` to the Pi's private transfer folder
using SSH. Compare the digest to the one printed in this VPS terminal.

Do not start a "temporary" VPS scan while working on the Pi.

## 3. Import and validate (Pi terminal)

```sh
hostname -s                         # must be the Pi
systemctl is-enabled pisobot-watch # must say disabled (nonzero exit is normal)
sudo env PISOBOT_EXPECT_HOST=pisobot-pi \
  PISOBOT_AGE_IDENTITY=/etc/pisobot/backup.agekey \
  bash /opt/pisobot/migration/pi-import.sh \
  --archive /var/backups/pisobot/final-REPLACE_WITH_UTC_TIMESTAMP.age \
  --source-stopped REPLACE_WITH_VPS_HOSTNAME --dry-run
# Repeat exactly without --dry-run to perform the import.
```

Actual import checks the checksum before decryption, authenticates/decrypts in
private scratch, checks all manifest hashes and SQLite/JSON integrity, and
requires a final, quiesced source snapshot plus matching application commit.
It replaces state, removes stale files rather than merging, and applies 700
directory/600 file modes and the correct root/pisobot ownership.

It then runs the application's **real unit suite without production env**:

```text
/opt/pisobot/venv/bin/python -m unittest discover -s tests
```

and uses a transient systemd unit to load the private env and run:

```text
/opt/pisobot/venv/bin/python -m pisobot.watch --once --dry-run
```

The latter makes real portal/enrichment requests from the home connection but
does not poll Telegram, send messages, clear a webhook or advance seen state.
It does write caches. Import verifies every `seen.json` is unchanged.
Incomplete source/enrichment results fail the import. `stbr` remains disabled
unless separately validated/approved: the home IP is not blanket permission to
enable new sources.

Do not continue unless it prints **`IMPORT OK`**. A failure leaves the watcher
disabled with an import marker and private recovery logs; use the README's
failure procedure. Never skip failing unit tests or the dry scan to enable it.

## 4. Start the Pi, then observe (Pi terminal)

Before enabling, look at the **VPS terminal again** and confirm its service is
still inactive/disabled. Ensure there is no `.import-in-progress` on the Pi.
The following is intentionally manual, not hidden in import:

```sh
sudo systemctl enable --now pisobot-watch.service
sudo systemctl status pisobot-watch.service --no-pager
sudo journalctl -u pisobot-watch --since '10 minutes ago' --no-pager
sudo systemctl show pisobot-watch -p MemoryCurrent -p NRestarts
```

The first live cycle should log **`Rental cycle complete: ... matches`**, and
for unchanged inventory/settings, **`matches to alert: 0`** for each person's
search. Existing matches must not be replayed as new cards and there should be
no fresh "Rental watch is live" baseline summary. This is the observable proof
that seen state and initialization came across, not merely that the process
started.

Zero is an acceptance check, **not a promise regardless of elapsed time**:
genuinely new adverts or real price drops during the pause can legitimately
alert. Inspect any nonzero IDs/reasons against the retained state and market
changes. A replay/baseline reset or duplicate existing IDs is a failure; stop
the Pi and investigate rather than deleting `seen.json` to get a clean log.
No application can eliminate the crash-between-Telegram-send-and-checkpoint
window completely.

Check `/help`, `/saved`, `/unhide`, `/settings`, a Save/Hide action (and its
reversal), then `/rentals`/`/report` sequentially. Check each admitted person's
independent settings and choices; do not reset profiles. Allow a complete cycle
between requests. Verify another cycle and a deliberate Pi service restart
preserve choices and do not replay alerts:

```sh
sudo systemctl restart pisobot-watch
sudo journalctl -u pisobot-watch --since '10 minutes ago' --no-pager
```

Observe **24 hours**: complete cycles, 403/429/source errors, reboot/restart
behavior, RSS, disk space, temperature/throttling, router/network interruptions,
Telegram buttons and absence of duplicate IDs. Test nightly off-box backup
with a successful download/decrypt/restore before enabling its timer.
The prior VPS soak in `docs/soak.md` is not a Pi/home-network soak.

## Rollback to Hostinger

Keep the VPS's code, venv, disabled unit, original state, private env, final
archive/checksum and the age **public recipient**. Do not remove unrelated VPS
services, change its firewall/listeners, or tear down Worker/D1 resources.

**If the Pi has never run a live cycle or accepted Telegram updates:** stop/
disable it anyway, ensure no manual dry scan remains, then the untouched VPS
state is still authoritative. Restart only the VPS:

If import was interrupted, first check
`systemctl status pisobot-migration-check.service --no-pager` on the Pi.
Stop it with `sudo systemctl stop pisobot-migration-check.service` if it is
still running, **before** starting the VPS. A completed/collected unit may no
longer exist.

```sh
# Pi, first:
sudo systemctl disable --now pisobot-backup.timer
sudo systemctl stop pisobot-backup.service
sudo systemctl disable --now pisobot-watch.service
# Verify inactive, disabled and MainPID=0; finish any manual validation job.

# VPS, only after the above:
sudo systemctl enable --now pisobot-watch.service
sudo journalctl -u pisobot-watch --since '10 minutes ago' --no-pager
```

Stopping the backup service may run its cleanup/restart, so always stop the
watcher **after** it has finished. Do not race a backup or manual import.

**If the Pi has been live:** the old VPS state is stale. Do not simply start it.
First stop the Pi backup timer/service as above, then stop/disable the Pi
watcher and take a fresh final export:

```sh
# Pi:
sudo env PISOBOT_HOST_ROLE=pi PISOBOT_EXPECT_HOST=pisobot-pi \
  PISOBOT_AGE_RECIPIENTS=/etc/pisobot/backup.recipients \
  bash /opt/pisobot/migration/vps-export.sh \
  --final --archive /var/backups/pisobot/rollback-REPLACE_WITH_UTC_TIMESTAMP.age
```

Transfer the ciphertext/checksum back to the VPS. Obtain the age identity from
your encrypted offline copy using a private, approved method; install it
temporarily as `/root/pisobot-restore.agekey`, root-owned 600. Never send it in
clear or store it alongside the off-box archives. If policy forbids the key on
the VPS, stop and arrange a trusted restore environment instead.

The import script can also restore onto the original Linux VPS **only when
explicitly given the VPS role and exact hostname/architecture**. Deploy the
snapshot's app commit and Python 3.12 venv there before import; leave the VPS
unit disabled. Preserve an encrypted copy of its old state if needed.

```sh
# VPS; Pi MUST remain stopped/disabled:
sudo env PISOBOT_HOST_ROLE=vps \
  PISOBOT_EXPECT_HOST=REPLACE_WITH_VPS_HOSTNAME PISOBOT_EXPECT_ARCH=x86_64 \
  PISOBOT_AGE_IDENTITY=/root/pisobot-restore.agekey \
  bash /opt/pisobot/migration/pi-import.sh \
  --archive /var/backups/pisobot/rollback-REPLACE_WITH_UTC_TIMESTAMP.age \
  --source-stopped pisobot-pi
# Only after IMPORT OK and reconfirming the Pi is fenced:
sudo systemctl enable --now pisobot-watch.service
```

Repeat the takeover checks on the VPS. Securely retire the temporary private
key copy according to your storage policy. Do not restart the Pi bot. Future
fetch-worker use must not run `pisobot.watch` or own independent saved/seen state
on the Pi; that split is a separate application change.

## Disaster recovery from a nightly backup

If the Pi is lost/unreachable, **fence it first** (power it off/disconnect it,
or otherwise prevent it returning online). An SSH timeout alone is not proof
it stopped polling. Stop/disable the VPS too while restoring. If you cannot
fence the old machine, do not enable a second poller; arrange fencing or a
deliberate bot-token rotation first.

Provision a replacement Pi or prepare the retained VPS at the archived code
commit. Fetch a `RESTORE-VERIFIED` archive/checksum and retrieve the private age
identity from the offline vault. Run `--restore-test` first. Then run the normal
import command for the destination host, adding **`--recovery`** and
`--source-stopped ORIGINAL_PI_HOSTNAME`. This deliberately permits a quiesced
nightly snapshot whose source was enabled (but stopped) during backup; ordinary
cutover still requires `--final`. Live rehearsal snapshots are not accepted
even with `--recovery`.

The tests and real dry scan are still mandatory; import still does not enable
anything. Review the data-loss window: changes/alerts since that backup may be
lost or replayed. A backup can preserve only its captured state, not recover
subsequent Save/Hide choices. Inform users and reconcile choices before
resuming. Re-establish off-box backups and the heartbeat after recovery.

## After a clean soak: keep or cancel Hostinger

**Pi-only is sufficient for the current application.** Leave the VPS disabled
through the initial soak and first off-box restore drill. Then either keep it
as the encrypted SSH backup target/future main-host option, or move backups to
another target, securely retain the migration artifacts/keys and cancel it.
Retaining a paid VPS indefinitely is not a technical requirement. No retired
VPS service or future Pi worker may remain a second Telegram poller.
