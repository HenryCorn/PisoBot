# Cutover, verification and rollback

## The invariant

**ONE host may scan and poll Telegram. Never both.** This includes manually
launched scanners, the Pi's import dry scan, old Actions schedules and any old
Worker webhook. Offline `--restore-test` does not scan and is safe.

Stop and **disable/fence** the VPS watcher, wait for it to exit, then take a **new
final export**. Only after that may the Pi import/run its dry scan and then be
enabled. A live rehearsal archive is not the final archive. Otherwise Telegram
pollers conflict, alerts duplicate, and saved/hidden choices diverge.

The script's final-export metadata proves what the source checks observed at
export time, not that another administrator cannot restart it afterwards.
`--source-stopped HOST` is your explicit acknowledgement that the named source
is **still fenced**. The handoff drop-in/marker prevents accidental systemd
starts; masking adds a second fence where supported. Neither prevents an
administrator deleting the fence or running Python manually. This kit cannot
establish a distributed lock. Keep one administrator in charge of the switch.

## Before the maintenance window

Complete the README through provisioning, encryption and the offline rehearsal.
Prepare the backup account/configuration and dedicated VPS rollback key now.
Verify both age recipients using synthetic data; never transfer the Pi master
private key to the VPS.
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
sudo install -d -m 755 /etc/systemd/system/pisobot-watch.service.d
sudo install -m 644 /opt/pisobot/migration/pisobot-vps-handoff.conf \
  /etc/systemd/system/pisobot-watch.service.d/handoff.conf
sudo systemctl daemon-reload
sudo systemctl cat pisobot-watch.service
# Additional fence, if supported by this unit layout:
sudo systemctl mask pisobot-watch.service
sudo systemctl show pisobot-watch.service \
  -p ActiveState -p SubState -p UnitFileState -p MainPID -p Result
```

Required: `ActiveState=inactive`, `SubState=dead`, `UnitFileState=disabled` or `masked`,
`MainPID=0`, normally `Result=success`. The unit allows up to 180 seconds for
shutdown. If it timed out/was killed or the last cycle failed, inspect the
journal before continuing; an abrupt stop just after Telegram accepts a message
can still duplicate that message before its local checkpoint.

The installed VPS unit is normally a regular file under `/etc/systemd/system`;
`systemctl mask` can refuse to overwrite it. **Do not force masking, delete the
unit or replace it with a symlink.** In that case retain `disabled` and use the
installed handoff drop-in as the startup fence. Confirm `systemctl cat` includes
both `HANDED-OFF-TO-PI` and `.import-in-progress` conditions and `ExecStartPre`
guards at `/var/lib/pisobot`. Custom state paths require matching drop-ins.
Masking and real systemd condition behaviour must be verified on the day.

Stop any manual scanners/schedulers as well. The final exporter also tries the
actual `watch.lock` and `run.lock`; a held lock is a hard failure. Do not remove
lock files to bypass it. Missing lock files are refused, not created as root.
**Keep the VPS disabled/masked with its handoff drop-in, even across a reboot.**

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
SHA256. Export checks the installed source service is inactive and disabled/masked; it
does not stop it for you. It snapshots all owner/guest DBs, seen/cache JSON,
tiles and the environment. Keep the final archive and checksum for rollback.
Before snapshotting, **while holding both application locks**, it writes and
synchronizes `/var/lib/pisobot/HANDED-OFF-TO-PI` and its directory ancestry.
Encryption and publication run with that fence already in place; there is no
unlocked, unfenced interval after the authoritative snapshot.
The fence is fail-closed: it remains if snapshotting, synchronization,
encryption, publication or checksum generation fails. A marker is **not**
evidence of a completed archive. Keep both hosts stopped, resolve the error
and retry with a fresh output name without clearing the fence. To abandon an
export, follow the unchanged-source rollback procedure below only if the other
host has never scanned/accepted updates and all validation/backup jobs are
stopped. Otherwise use the full latest-state rollback, not a marker deletion.
See README "Failure procedure" for residual scratch/partial-output handling.

After a successful export, reconfirm `ActiveState=inactive`, `MainPID=0` and
disabled/masked status. Confirm the marker exists and the drop-in is loaded before allowing the Pi to
scan. If masking was unavailable, test the condition fence now, while the Pi
is still stopped: a VPS `systemctl start` must leave it inactive with
`ConditionResult=no`. If it becomes active, stop it and fix the fence before
continuing, then take a new final export. Do not treat exit code zero from
`systemctl start` as proof that the service started.
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
If the Pi already holds data, it refuses rather than silently replacing it.
After inspection, add `--replace-state` and
`PISOBOT_AGE_RECIPIENTS=/etc/pisobot/backup.recipients` to first retain a verified
encrypted safety copy under `/var/backups/pisobot`. Keep the first copy until a
verified off-box backup exists. A newer existing scan timestamp also requires
`--allow-older-archive`, deliberately accepting data loss; do not add it routinely.
The verified ciphertext, checksum and all containing directory ancestry are
`fsync`ed before any original-state move/import marker. Any sync error refuses
replacement with the original and fences intact. This ordering is tested with
injected failures, not real power cuts; actual storage durability remains an
on-hardware validation gate.

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
still inactive and disabled/masked with the handoff marker present. Ensure there
is no `.import-in-progress` or `HANDED-OFF-TO-PI` on the Pi.
The following is intentionally manual, not hidden in import:

```sh
sudo systemctl enable --now pisobot-watch.service
sudo systemctl is-active --quiet pisobot-watch.service  # must exit 0
sudo systemctl status pisobot-watch.service --no-pager
sudo systemctl show pisobot-watch -p ActiveState -p ConditionResult -p Result
sudo journalctl -u pisobot-watch --since '10 minutes ago' --no-pager
sudo systemctl show pisobot-watch -p MemoryCurrent -p NRestarts
```

`enable --now` can return success when a condition blocked startup. An inactive
unit, `ConditionResult=no` or "start condition failed" is **not** acceptance;
inspect the marker/mount guard and fix the cause, never bypass it.

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

**Immediately after this first clean live cycle**, run the manual off-box
backup from README section 5. Require `NIGHTLY BACKUP OK` (upload, download,
decrypt and restore verified), then enable the timer. Do this before the soak,
not hours later. Until it succeeds, the emergency fallback below can lose all
post-cutover changes.

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
Telegram buttons and absence of duplicate IDs. Confirm the already-enabled
backup timer continues producing verified off-box archives.
The prior VPS soak in `docs/soak.md` is not a Pi/home-network soak.

## Rollback to Hostinger

Keep the VPS's code, venv, fenced unit/drop-in, original state, private env,
final archive/checksum, age **public recipients** and dedicated root-only
rollback identity (never the Pi master identity). Do not remove unrelated VPS
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
sudo /opt/pisobot/venv/bin/python /opt/pisobot/migration/state.py \
  handoff /var/lib/pisobot clear
sudo systemctl unmask pisobot-watch.service
sudo systemctl daemon-reload
sudo systemctl enable --now pisobot-watch.service
sudo systemctl is-active --quiet pisobot-watch.service
sudo journalctl -u pisobot-watch --since '10 minutes ago' --no-pager
```

Stopping the backup service may run its cleanup/restart, so always stop the
watcher **after** it has finished. Do not race a backup or manual import.

**If the Pi has been live and is accessible:** the old VPS state is stale.
Do not simply start it (the explicitly lossy emergency exception is below).
First stop the Pi backup timer/service as above, then stop/disable the Pi
watcher and take a fresh final export:

```sh
# Pi:
sudo env PISOBOT_HOST_ROLE=pi PISOBOT_EXPECT_HOST=pisobot-pi \
  PISOBOT_AGE_RECIPIENTS=/etc/pisobot/backup.recipients \
  bash /opt/pisobot/migration/vps-export.sh \
  --final --archive /var/backups/pisobot/rollback-REPLACE_WITH_UTC_TIMESTAMP.age
```

Transfer only the ciphertext/checksum back to the VPS. Use its pre-provisioned
`/root/pisobot-rollback.agekey` (root-owned 600); the archive must have been
encrypted to the combined recipients file. Never transfer the master Pi
identity. If the rollback recipient was omitted, stop and arrange a trusted
restore/re-encryption environment instead of copying the master key to the VPS.

The import script can also restore onto the original Linux VPS **only when
explicitly given the VPS role and exact hostname/architecture**. Deploy the
snapshot's app commit and Python 3.12 venv there before import; leave the VPS
unit disabled/masked. `--replace-state` is mandatory for its retained state;
import first stores a verified encrypted copy on durable storage.

```sh
# VPS; Pi MUST remain stopped/disabled:
sudo env PISOBOT_HOST_ROLE=vps \
  PISOBOT_EXPECT_HOST=REPLACE_WITH_VPS_HOSTNAME PISOBOT_EXPECT_ARCH=x86_64 \
  PISOBOT_AGE_IDENTITY=/root/pisobot-rollback.agekey \
  PISOBOT_AGE_RECIPIENTS=/root/pisobot.recipients \
  bash /opt/pisobot/migration/pi-import.sh \
  --archive /var/backups/pisobot/rollback-REPLACE_WITH_UTC_TIMESTAMP.age \
  --source-stopped pisobot-pi --replace-state
# Only after IMPORT OK and reconfirming the Pi is fenced:
sudo systemctl unmask pisobot-watch.service
sudo systemctl daemon-reload
sudo systemctl enable --now pisobot-watch.service
sudo systemctl is-active --quiet pisobot-watch.service
```

Successful import clears the VPS handoff marker only after tests/dry scan
pass; failures retain both startup fences. Keep the drop-in installed.
Repeat the takeover checks on the VPS. Retain the dedicated rollback key
according to the offline-vault policy. Do not restart the Pi bot. Future
fetch-worker use must not run `pisobot.watch` or own independent saved/seen state
on the Pi; that split is a separate application change.

## Disaster recovery from a nightly backup

If the Pi is lost/unreachable, **fence it first** (power it off/disconnect it,
or otherwise prevent it returning online). An SSH timeout alone is not proof
it stopped polling. Stop/disable the VPS too while restoring. If you cannot
fence the old machine, do not enable a second poller; arrange fencing or a
deliberate bot-token rotation first.

Provision a replacement Pi or prepare the retained VPS at the archived code
commit. Fetch a `RESTORE-VERIFIED` archive/checksum. Use the dedicated rollback
identity on the VPS, or restore the Pi identity from the offline vault onto a
replacement Pi. Do not move the Pi master key to the VPS. Run `--restore-test`
first. Then run the normal
import command for the destination host, adding **`--recovery`** and
`--source-stopped ORIGINAL_PI_HOSTNAME`. This deliberately permits a quiesced
nightly snapshot whose source was enabled (but stopped) during backup; ordinary
cutover still requires `--final`. Live rehearsal snapshots are not accepted
even with `--recovery`.
For an occupied destination also supply `--replace-state` and its recipients
file. Add `--allow-older-archive` only after explicitly accepting the lost
changes. Unmask the VPS only after successful import and reconfirmed Pi fencing.

The tests and real dry scan are still mandatory; import still does not enable
anything. Review the data-loss window: changes/alerts since that backup may be
lost or replayed. A backup can preserve only its captured state, not recover
subsequent Save/Hide choices. Inform users and reconcile choices before
resuming. Re-establish off-box backups and the heartbeat after recovery.

### Pi dead and no usable off-box backup

This is an explicitly **lossy emergency rollback**, including failure before
the first off-box backup. Physically power off/disconnect the Pi and prevent it
returning; an SSH timeout is insufficient. Verify the retained VPS cutover
state is intact and has no `.import-in-progress` marker. If it is incomplete,
restore the retained final cutover archive using the guarded import/recovery
procedure instead; do not bypass the import marker.

Tell users that alerts can repeat and Save/Hide/settings changes since cutover
are lost. Once that loss is accepted and the Pi is fenced, use the deliberate
VPS `handoff ... clear`, unmask and start commands in the never-live rollback
block above. Confirm `is-active`, inspect the journal and reconcile choices.
Take and verify an off-box backup promptly. Never later reconnect/start the
old Pi watcher without a fresh, stopped-source migration of authoritative state.

## After a clean soak: keep or cancel Hostinger

**Pi-only is sufficient for the current application.** Leave the VPS disabled
through the initial soak and first off-box restore drill. Then either keep it
as the encrypted SSH backup target/future main-host option, or move backups to
another target, securely retain the migration artifacts/keys and cancel it.
Retaining a paid VPS indefinitely is not a technical requirement. No retired
VPS service or future Pi worker may remain a second Telegram poller.
