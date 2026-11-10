# PisoBot on a Raspberry Pi: migration kit

**The Pi can replace the VPS completely. You do not need both subscriptions/
machines for the current bot.** Keep Hostinger through cutover and a 24-hour
soak for easy rollback. Afterwards either keep it as an encrypted off-box
backup target, or use another off-box target and cancel it. A USB disk attached
to the same Pi is not an off-box backup.

The future design in `docs/blueprint.md`, section 2, is different: the VPS
would be the main host and the Pi an outbound-only home-connection fetch worker.
That would need both, but the worker is a proposal, not something this kit
implements. The blueprint is on `wp0-rent-cap` at the time of writing, not on
this kit's `vps-rentals` base. Do not delete VPS state/resources during migration.

**Only one machine may scan or poll Telegram at a time.** Provisioning and an
offline restore drill are safe while the VPS runs. A Pi dry-run scan must wait
until the VPS has stopped. Follow [cutover.md](cutover.md) for the actual switch.
None of these scripts enables the bot.

This kit has not been run on a Pi or against the VPS. The Pi is not available
yet. The integration suite is designed for real age encryption and live SQLite
WAL files, but a fake application and no systemd/network services. The review-fix
run used an age plumbing shim because age was unavailable: it does **not**
validate encryption or multi-recipient decryption. Hardware, packages, mount behavior,
real unit tests, portal access and the first live cycle still need validation
on the eventual Pi.

## 1. Buy and prepare the Pi

- Raspberry Pi **4 or 5, 4 GB**, a reputable power supply matched to the model,
  ventilation (active cooling for a Pi 5), and preferably wired Ethernet.
  A small UPS helps with both mains failures and SQLite durability.
- Prefer a **USB 3 SSD** with a reliable, adequately powered USB enclosure.
  Either boot everything from it, or boot from microSD and put state on a
  separate ext4 SSD partition. Check the Pi bootloader's USB-boot support if
  booting from SSD.
- This version rewrites an approximately **5 MB `cache.json` every 15 minutes**
  (roughly 480 MB/day before filesystem amplification), plus SQLite WAL,
  per-user state and map tiles. Do not place write-heavy state on a cheap card.
  The SSD option below is scripted, including a no-fallback mount dependency.
- **Without an SSD:** use a good high-endurance microSD, select `--root-disk`,
  keep free space, run nightly off-box backups and monitor I/O errors with
  `journalctl -k`. This is a workable interim installation, not protection
  against card failure. Do not disable SQLite journaling/fsync, move durable
  state to RAM, or lengthen the scan interval just to hide wear. Moving existing
  state to a later SSD is a separate, stopped-service migration; the provisioner
  will not mount over it.

### OS and Python: choose an explicit version

Checked against official sources on **2026-11-10**:

| Image | Default Python | This kit |
| --- | --- | --- |
| Current Raspberry Pi OS Lite **64-bit**, Debian 13 Trixie (2026-10-06 image) | 3.13 (Debian's `python3` package is 3.13.5) | Not the validated service runtime; provisioner refuses it |
| Older Raspberry Pi OS Bookworm | 3.11 | Also not the validated runtime |
| **Ubuntu Server 24.04 LTS, preinstalled `arm64+raspi` image** | **3.12** | Supported path for this kit |

Use Raspberry Pi Imager's **Ubuntu Server 24.04 LTS 64-bit** selection, or the
latest 24.04 point-release **preinstalled-server-arm64+raspi.img.xz** from the
[24.04 image directory](https://cdimage.ubuntu.com/releases/24.04/release/).
Verify the image checksum using Ubuntu's published SHA256SUMS/signature.
Do not choose the generic ARM server ISO, Ubuntu Core, a 32-bit image, or
whatever newer Ubuntu release Imager calls "latest". Ubuntu supports both
Pi 4 and Pi 5 on 24.04; normal LTS maintenance runs through May 2029.

The provisioner uses Ubuntu's `/usr/bin/python3.12` and `python3.12-venv`.
It does **not** replace `/usr/bin/python3`, add an Ubuntu PPA to Raspberry Pi OS,
or build an unmanaged Python. Staying on Raspberry Pi OS would require a
separately maintained Python 3.12 or an explicit application validation on
3.13; that is not this kit's supported path.

Sources: [Raspberry Pi OS releases](https://www.raspberrypi.com/software/operating-systems/),
[Debian Trixie Python](https://packages.debian.org/trixie/python3),
[Ubuntu Python versions](https://ubuntu.com/developers/docs/reference/availability/python/),
[Ubuntu Pi support and installation](https://ubuntu.com/hardware/docs/boards/how-to/ubuntu_supported/raspberry-pi/),
[Ubuntu lifecycle](https://ubuntu.com/about/release-cycle).

### First boot

In Imager configure a unique hostname, e.g. **pisobot-pi**, your administrator
account and SSH public key. Wait for cloud-init to finish. On the Pi:

```sh
hostname -s
uname -m                         # must say aarch64
cat /etc/os-release              # Ubuntu, VERSION_ID="24.04"
sudo apt-get update
sudo apt-get full-upgrade
sudo reboot
```

Use the literal hostname from this inventory in `PISOBOT_EXPECT_HOST` below.
Do **not** set it dynamically to `$(hostname)` in production: that defeats the
wrong-host check. Scripts also check architecture and the Pi device-tree model.

The bot needs **no inbound Internet port**, port forwarding, webhook, domain
name or web server. Telegram uses outbound HTTPS long polling. A changing home
IP/CGNAT is fine. Allow outbound DNS, HTTPS, NTP and SSH to your backup target.
Keep administrative SSH LAN-only or behind an already configured private VPN;
do not expose it on the router for this migration. An outage delays alerts;
after the connection returns the service retries. Use an external heartbeat
monitor to detect a disconnected or powered-off Pi.

## 2. Transfer the reviewed kit and the same application commit

All commands below are instructions to run **later**, not operations this kit
has performed. Replace the example hosts/usernames before running anything.
There is deliberately no command to read the populated environment file.

On the laptop with the local `pi-migration` branch:

```sh
git archive --format=tar --output=/tmp/pisobot-pi-kit.tar pi-migration deploy/pi
scp /tmp/pisobot-pi-kit.tar admin@pisobot-pi:~/
scp /tmp/pisobot-pi-kit.tar admin@YOUR_VPS_SSH_HOST:~/
```

On **each host**, unpack into a separate kit directory. Do not switch the
running VPS application checkout just to obtain the kit:

```sh
mkdir -p ~/pisobot-kit
tar -xf ~/pisobot-pi-kit.tar -C ~/pisobot-kit
sudo install -d -m 755 /opt/pisobot/migration
sudo cp ~/pisobot-kit/deploy/pi/* /opt/pisobot/migration/
sudo chown -R root:root /opt/pisobot/migration
sudo chmod -R go-w /opt/pisobot/migration
```

On the **VPS**, record only this non-secret inventory; leave its service running:

```sh
hostname -s
uname -m
sudo git -c safe.directory=/opt/pisobot/app -C /opt/pisobot/app rev-parse HEAD
sudo /opt/pisobot/venv/bin/python --version
sudo du -sh /var/lib/pisobot
```

On the **Pi**, install that exact application commit, not a moving branch:

```sh
sudo apt-get install -y git
sudo install -d -m 755 /opt/pisobot
sudo git clone https://github.com/HenryCorn/PisoBot.git /opt/pisobot/app
sudo git -C /opt/pisobot/app checkout --detach REPLACE_WITH_VPS_COMMIT
```

If the repository is private, use your existing authorized checkout/transfer
method; never put a GitHub access token in a URL. Keep code root-owned,
readable by `pisobot`, not group/world writable. Keep kit and app separate.
Export records the app commit; import refuses a different commit or dirty
tracked files. Do not combine a migration with the other agent's application
changes. Never copy an x86 virtualenv onto ARM.

## 3. Select storage and provision (Pi only)

Inspect disks without changing them:

```sh
lsblk -o NAME,SIZE,MODEL,TRAN,FSTYPE,UUID,MOUNTPOINTS
```

For a separate USB SSD state partition, identify its **ext4 filesystem UUID**.
It must already be formatted, contain no unrelated data, and not be mounted
elsewhere. Formatting destroys data; this kit intentionally never selects or
formats a disk for you. Have a knowledgeable person prepare a new ext4 partition
if needed. Do not guess `/dev/sda` or erase a disk containing files.

```sh
sudo env PISOBOT_EXPECT_HOST=pisobot-pi \
  bash /opt/pisobot/migration/pi-provision.sh --state-uuid REPLACE_WITH_EXT4_UUID --dry-run
sudo env PISOBOT_EXPECT_HOST=pisobot-pi \
  bash /opt/pisobot/migration/pi-provision.sh --state-uuid REPLACE_WITH_EXT4_UUID
```

The script mounts the chosen UUID directly at `/var/lib/pisobot`, persists an
idempotent fstab entry with `noatime`, and adds `RequiresMountsFor`/`BindsTo` plus
a mount condition to the watcher. `nofail` lets the Pi boot for repair without
the SSD; it **does not** let the watcher silently write to the underlying SD.
Missing/wrong mounts also block imports and backups. Test this on the Pi before
cutover: power down, disconnect the SSD, boot, confirm the watcher cannot start,
then power down and reconnect. Do not hot-unplug a mounted state disk.

If booting the whole OS from SSD, or temporarily using only high-endurance SD:

```sh
sudo env PISOBOT_EXPECT_HOST=pisobot-pi \
  bash /opt/pisobot/migration/pi-provision.sh --root-disk --dry-run
sudo env PISOBOT_EXPECT_HOST=pisobot-pi \
  bash /opt/pisobot/migration/pi-provision.sh --root-disk
```

The choice is saved in `/etc/pisobot/pi-storage`. Re-running without a storage
argument reuses it; a different choice is refused. Re-provisioning refuses an
active/enabled watcher and will not format, wipe or overlay existing state.

Provisioning creates:

| Path | Owner / mode |
| --- | --- |
| `/opt/pisobot/app`, `/opt/pisobot/venv`, `/opt/pisobot/migration` | root; readable/executable as appropriate, not service-writable |
| `/etc/pisobot` | root, 700 |
| `/etc/pisobot/pisobot.env` (only after import) | root, 600 |
| `/var/lib/pisobot`, including user directories | pisobot:pisobot, 700 |
| State files after import | pisobot:pisobot, 600 |

It installs `requirements.txt` in a **Python 3.12 venv**, requiring binary wheels
(including ARM64 Pillow wheels), runs `pip check`, and actually encodes/decodes
JPEG and PNG **as `pisobot`**. A missing wheel/codec is a hard failure, not a
silently incomplete install. If a future Pillow release lacks a suitable
wheel, investigate its support first. An explicit source-build fallback on
Ubuntu 24.04 is:

```sh
sudo apt-get install -y build-essential python3.12-dev libjpeg-dev zlib1g-dev \
  libfreetype6-dev liblcms2-dev libwebp-dev libtiff-dev libopenjp2-7-dev
sudo sh -c 'umask 022; /opt/pisobot/venv/bin/python -m pip install --no-binary=Pillow -r /opt/pisobot/app/requirements.txt'
# Re-run provisioning: already-installed compatible dependencies are retained,
# and the service-user codec check must pass.
```

Provisioning also enables `systemd-timesyncd`, waits up to 120 seconds for
synchronization, and enables Ubuntu's unattended security updates via the
apt timers/default Ubuntu security origins. It **disables automatic reboots**.
If NTP cannot synchronize, fix network/DNS/time access and re-run. Check:

```sh
timedatectl status
systemctl status systemd-timesyncd --no-pager
systemctl list-timers apt-daily.timer apt-daily-upgrade.timer --no-pager
sudo unattended-upgrade --dry-run
systemctl is-enabled pisobot-watch pisobot-backup.timer  # both disabled (nonzero exit is expected)
```

Review `/var/run/reboot-required` after updates and schedule reboots yourself.
Distro security updates do not upgrade Python packages in the venv: handle those
as stopped-service upgrades with tests. Optionally bound journald storage (e.g.
`SystemMaxUse=64M` in a local journald drop-in). Do not clear useful failure logs.

## 4. Encryption and a rehearsal export

Use an age identity dedicated to migration/backups. On the **Pi**:

```sh
sudo sh -c 'umask 077; age-keygen -o /etc/pisobot/backup.agekey'
sudo sh -c 'umask 077; age-keygen -y /etc/pisobot/backup.agekey > /etc/pisobot/backup.recipients'
sudo chmod 600 /etc/pisobot/backup.agekey
```

Do not overwrite an existing identity. Put a second copy of the **private key**
in your encrypted offline password/file vault before relying on backups. Losing
the Pi and its only private key makes encrypted backups useless. The nightly
restore drill needs a root-readable identity on the Pi; an offline second key
can also be added as another public recipient.

Transfer only the **public** `backup.recipients` file to
`/root/pisobot.recipients` on the VPS, using your established SSH access.
The VPS must never receive this master private key. Do not display, source, email or commit
`pisobot.env`, an identity, or a decrypted state directory.

On the **VPS**, install `age` if absent using its OS package manager (e.g.
`sudo apt-get install age` on Ubuntu/Debian), and create a private output folder:

```sh
sudo install -d -m 700 /var/backups/pisobot
```

Prepare a **dedicated rollback identity on the VPS**, without overwriting an
existing one:

```sh
sudo sh -c 'umask 077; age-keygen -o /root/pisobot-rollback.agekey'
sudo sh -c 'umask 077; age-keygen -y /root/pisobot-rollback.agekey > /root/pisobot-rollback.recipient'
```

Transfer only `pisobot-rollback.recipient` to the Pi through your approved
administrator transfer procedure. Add its public recipient line to
`/etc/pisobot/backup.recipients` using `sudoedit`, retaining the Pi's recipient.
Copy that combined **public** recipients file back to `/root/pisobot.recipients`
on the VPS. Keep the rollback private key root-owned 600 on the VPS and a copy
in the offline vault. Never move `/etc/pisobot/backup.agekey` to the VPS.

Before cutover, use synthetic data to verify that **both identities separately**
decrypt an archive encrypted to the combined recipients file. This was not
validated locally with real age. A compromised VPS can still decrypt any
archive addressed to its rollback key; separate keys avoid moving the master,
not this trust risk. Prefer a separate ciphertext-only backup host. A backup
account must never be able to read the VPS root-only rollback identity.

Then take the rehearsal export:

```sh
sudo env PISOBOT_EXPECT_HOST=REPLACE_WITH_VPS_HOSTNAME PISOBOT_EXPECT_ARCH=x86_64 \
  PISOBOT_AGE_RECIPIENTS=/root/pisobot.recipients \
  bash /opt/pisobot/migration/vps-export.sh \
  --archive /var/backups/pisobot/rehearsal.age --dry-run
# Same command without --dry-run takes the live rehearsal export.
```

Use the architecture actually inventoried, not `x86_64` if it differs.
By default **nothing is stopped**. Every SQLite database, including every
`users/<chat_id>/pisobot.db`, uses SQLite's online backup API and an integrity
check. JSON, user directories, tile files and the environment are included.
WAL/SHM/journal sidecars, `.pending` files, locks, handoff markers and the
`tmp` + eight-character files used for atomic seen writes are excluded. There is
never a raw copy of a live SQLite database.

Quiesced exports require existing `watch.lock` and `run.lock`; they never create
root-owned replacements. If missing, stop all scanners and have the operator
recreate them as `pisobot`, mode 600, before retrying. Never unlink a held lock.
With both locks held, the exporter removes only SQLite sidecars its read-only
open created; live exports never remove them. On the actual Pi, check
`ls -l /var/lib/pisobot` after a quiesced export (remaining sidecars must be
`pisobot`-owned), restart the watcher and verify it is active.

A live export is individually SQLite-consistent, **not an atomic transaction
across different databases, cache and seen files**. It is for rehearsal only.
Only a stopped `--final` snapshot is accepted for ordinary import. Nightly
backups briefly stop the Pi for the same cross-file consistency.

Export creates an age-encrypted tar stream and a **digest-only**
`rehearsal.age.sha256` sidecar, prints the ciphertext SHA256, and never prints
configuration values. Copy only those two files to the Pi over SSH. Check the
printed checksum over a trusted channel: SHA256 detects corruption, not an
attacker who replaces both archive and sidecar. Age authenticates ciphertext,
but a public recipient key is not proof of who created an archive.

Use a private transfer folder on each machine (`sudo install -d -m 700
/var/backups/pisobot`). Do not loosen permissions to make `scp` work: use the
existing administrator/root transfer procedure or a mode-700 administrator
staging folder, then move files with sudo.

On the **Pi**, an offline rehearsal is safe while the VPS runs:

```sh
sudo env PISOBOT_EXPECT_HOST=pisobot-pi \
  PISOBOT_AGE_IDENTITY=/etc/pisobot/backup.agekey \
  bash /opt/pisobot/migration/pi-import.sh \
  --archive /var/backups/pisobot/rehearsal.age --restore-test
```

It verifies checksum, authenticated decryption, archive paths/types, manifest
hashes, all SQLite databases and JSON. It restores only into private scratch
and deletes that copy. **It does not scan, poll, run application tests, or
overwrite production state.** Proceed to [cutover.md](cutover.md) for final
export, import, actual unit tests/dry scan and enabling the Pi.

## 5. Nightly off-box backups (after successful cutover)

The provided implementation targets another **Linux machine over SSH**; it can
be the retained Hostinger VPS. Set up a dedicated unprivileged backup account,
a mode-700 destination directory owned by it, key-only SSH and a verified host
key in root's `known_hosts` on the Pi. This implementation requires a **plain
shell account**, not SFTP-only, `rrsync` or a forced-command key: it invokes
`test`, GNU `stat -c`, `cat /etc/machine-id`, `umask`, `mkdir`, `touch`, and
bidirectional `scp` (including SFTP support for modern scp). Keep it
unprivileged, without sudo, and limit forwarding/access as appropriate without
blocking those operations. An SFTP-only transport is deferred. Do not share the
Pi's age identity with the backup server. Do not disable SSH host-key checking.

Before cutover, check the read-only part from the Pi as the same SSH user:

```sh
sudo ssh -o BatchMode=yes -o StrictHostKeyChecking=yes pisobot-backup@YOUR_BACKUP_HOST \
  'test -d /srv/pisobot-backups && stat -c %a /srv/pisobot-backups && cat /etc/machine-id'
```

Expect mode `700` and an ID different from the Pi. The first manual backup must
also prove write, upload, download and restore access; this probe alone cannot.

The target's machine ID must differ from the Pi's. This catches accidental
backups to itself; it does not prove a separate physical failure domain. A NAS
in the same house needs an additional off-site copy for theft/fire protection.
If you cancel Hostinger, provision another SSH backup target first (or replace
this transport with a separately tested object-storage backup implementation).

Create `/etc/pisobot/backup.env` using `sudoedit`, root-owned mode 600:

```ini
PISOBOT_EXPECT_HOST=pisobot-pi
PISOBOT_AGE_RECIPIENTS=/etc/pisobot/backup.recipients
PISOBOT_AGE_IDENTITY=/etc/pisobot/backup.agekey
PISOBOT_BACKUP_TARGET=pisobot-backup@YOUR_BACKUP_HOST
PISOBOT_BACKUP_DIR=/srv/pisobot-backups
# Optional, a separate DAILY backup monitor:
# HEALTHCHECK_URL=https://YOUR_MONITOR/PRIVATE_BACKUP_CHECK_ID
```

These are systemd environment assignments, **not a shell script to source**.
For a nonstandard SSH port/key, put an SSH host alias in root's SSH config and
use that alias for `PISOBOT_BACKUP_TARGET`. No secrets belong in command arguments.

Take the first manual backup **immediately after the first clean live cycle**,
before the 24-hour soak. Have the account/configuration ready before cutover.

```sh
sudo chmod 600 /etc/pisobot/backup.env
# Preview with the non-secret settings (does not contact the target):
sudo env PISOBOT_EXPECT_HOST=pisobot-pi \
  PISOBOT_AGE_RECIPIENTS=/etc/pisobot/backup.recipients \
  PISOBOT_AGE_IDENTITY=/etc/pisobot/backup.agekey \
  PISOBOT_BACKUP_TARGET=pisobot-backup@YOUR_BACKUP_HOST \
  PISOBOT_BACKUP_DIR=/srv/pisobot-backups \
  bash /opt/pisobot/migration/nightly-backup.sh --dry-run
sudo systemctl start pisobot-backup.service
sudo journalctl -u pisobot-backup.service --no-pager
# Only after NIGHTLY BACKUP OK:
sudo systemctl enable --now pisobot-backup.timer
systemctl list-timers pisobot-backup.timer --no-pager
```

At approximately 03:30 UTC nightly (plus up to 15 minutes jitter), the script:

1. Requires an enabled/active Pi watcher; serializes maintenance with `flock`.
2. Briefly stops it, takes a quiesced encrypted snapshot, and restarts it
   **before** remote transfers. Its cleanup also attempts restart on failure.
3. Uploads only ciphertext/checksum into a new private remote folder. Downloads
   them again and byte-compares them to the upload.
4. Decrypts and restores the downloaded copy privately, checks hashes, SQLite
   integrity and JSON, then writes `RESTORE-VERIFIED` on the remote.
5. Optionally pings `HEALTHCHECK_URL` only after success, without logging its
   value. A ping failure is reported as a failed backup job.

No automatic pruning is performed. Keep at least 7 daily and 4 weekly copies,
monitor remote space and remove old **named, verified** backup folders only
after newer copies have passed a restore drill. A failed transfer/test may
leave an incomplete remote folder without `RESTORE-VERIFIED`; investigate it.
Power loss/SIGKILL cannot run cleanup; the enabled watcher returns on reboot.
Check the watcher and external monitor after any interrupted backup.

The application already pings the existing `HEALTHCHECK_URL` from
`pisobot.env` after successful live cycles (not dry scans). Set that to a
15-minute-cycle monitor with a generous grace period for scan runtime, jitter
and brief backups. Use a **different monitor URL** for the optional nightly job,
using the same variable name in `backup.env`. Reusing one URL would allow a
backup to mask a broken watcher. The backup unit deliberately does not load
`pisobot.env`. No heartbeat URL/token is printed by the kit.

A nightly restore test checks recoverable data, not application compatibility.
Perform a stopped-service disaster-recovery drill periodically as described in
[cutover.md](cutover.md#disaster-recovery-from-a-nightly-backup).

## Script controls, failure recovery and local checks

All shell entry points use `set -euo pipefail`, accept `--dry-run`, refuse a
wrong hostname/architecture, and make no changes in dry-run mode. `common.bash`
is a sourced library and refuses direct execution. `state.py` is its private
standard-library helper, not a standalone administration command.

`--dry-run` prints a plan, **not** a validation of packages, credentials or
source availability. `pi-import.sh --dry-run` does not run the application's
`--once --dry-run`; actual import does. No export/import starts or enables a
watcher. The backup script restarts only the previously active Pi watcher.

Exports never overwrite existing archive names. Use a new timestamped name.
Successful imports may be repeated while both hosts remain stopped: they
replace state instead of merging, so stale WAL/user files cannot survive.
An occupied destination is refused without **`--replace-state`**. That flag
requires `PISOBOT_AGE_RECIPIENTS` and first writes, decrypts and verifies an
encrypted safety copy at `PISOBOT_PREVIOUS_DIR` (default `/var/backups/pisobot`,
root-only 700, not tmpfs/ramfs or inside state/scratch). The selected identity
must decrypt this copy as well as the incoming archive. Keep the copy and
checksum until the next verified off-box nightly backup; the **first** copy
holds the original state, later retries copy the state left by earlier attempts.

If existing `watch_last_success` is later than the incoming archive's creation
time, import refuses unless **`--allow-older-archive`** accompanies
`--replace-state`. That is deliberate acceptance of lost/replayed changes,
not a routine retry flag. An unknown scan timestamp warns; it cannot prove the
archive is newer or compare every intervening button action.

Restored env files accept only application variable names, with `STORAGE=local`,
`FETCH_CHAIN=plain` and the matching state path. `PYTHONPATH`, `LD_PRELOAD`,
`INSECURE_SSL` and unknown names are refused without logging values.
The installed `pisobot-vps-handoff.conf` fences the old VPS after final export;
see cutover.md for installation, masking and rollback. The Pi drop-in also
honours the marker when exporting back. Successful import clears a handoff
marker only after validation; failed import keeps it.
Re-provisioning refuses an active watcher. Stop the backup timer/service before
maintenance; never run provisioning/import and backup concurrently.

Scratch defaults to root-only `/run/pisobot-transfer` (normally tmpfs). Allow
space for the snapshot, encrypted archive and previous state; check `df -h /run`
and `du -sh /var/lib/pisobot`. The extractor caps unpacked archives at 1 GiB.
For larger state, explicitly raise `PISOBOT_MAX_ARCHIVE_BYTES` and set
`PISOBOT_TMP_DIR` to an ample, root-only private SSD directory **outside** state.
Plaintext exists only locally in private scratch, never in the transferred
archive. Normal cleanup unlinks it; this is not guaranteed secure erasure on
flash, and tmpfs can be swapped. Use encrypted local storage if that threat
matters.

### Failure procedure

On import failure after installation begins, the service stays disabled and
`.import-in-progress` blocks startup. The cleanup removes plaintext scratch
state/env copies and retains only validation logs. The replaced state survives
in the printed durable `replaced-*.age` copy, **not in `/run`**. Pre-install
failures normally remove the entire job. SIGKILL/power loss cannot run cleanup:
inspect and remove residual plaintext privately after recovery. Logs can also
contain sensitive application output; do not paste them into issues.

1. Confirm both hosts are still stopped. If import was interrupted, inspect
   `systemctl status pisobot-migration-check.service --no-pager`; if that
   transient scan is still running, stop it with
   `sudo systemctl stop pisobot-migration-check.service` before doing anything
   else. A completed/collected unit may no longer exist. Verify the retained
   encrypted copy/checksum with `--restore-test`; `/run` does not survive reboot.
2. Fix the cause (NTP, code commit, dependencies, portal access, storage).
   Do not "fix" a failed source by treating it as an empty successful scan.
3. If retrying from the known encrypted final archive, remove **only**
   `/var/lib/pisobot/.import-in-progress` after that review, then re-run import
   with `--replace-state` and the recipients file. To return to the original
   state instead, select the first encrypted safety copy, add `--recovery`,
   and acknowledge its recorded source hostname. Use `--allow-older-archive`
   only after accepting the loss of later changes.
   If a crash left `/etc/pisobot/.pisobot.env.import`, inspect and remove only
   that stale staging file too. Never remove state or WAL files from a running
   service.
4. Use the rollback checklist instead if returning to the VPS. Keep retained
   recovery jobs until success; then remove only their exact `job.*` directory,
   not the scratch root. Keep the encrypted original until the next verified
   off-box backup. If incomplete state cannot itself be snapshotted, stop and
   arrange operator-assisted recovery; do not erase it to bypass the guard.

Export/import paths are overridable for testing and controlled recovery:
`PISOBOT_STATE_DIR`, `PISOBOT_ENV_FILE`, `PISOBOT_APP_DIR`,
`PISOBOT_VENV_DIR`, `PISOBOT_TMP_DIR`, `PISOBOT_PREVIOUS_DIR`,
`PISOBOT_ARCHIVE`, `PISOBOT_PYTHON`.
Helpers default to the service venv's Python when present, otherwise `python3`
(3.11+ required; the service itself still requires 3.12).
Production provisioning deliberately fixes the canonical VPS paths. A custom
import destination also requires a matching `PISOBOT_STATE_DIR` in the archived
environment and an appropriate service configuration; it is not a way to move
only half of a deployment.

Offline regression suite, from this checkout, with Python 3.12+ and `age`:

```sh
for script in deploy/pi/*.sh deploy/pi/*.bash; do bash -n "$script" || exit; done
if command -v shellcheck >/dev/null; then shellcheck -x -P . deploy/pi/*.sh deploy/pi/*.bash; fi
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s deploy/pi -p 'test_*.py' -v
```

Without age, run the stdlib-only helper/guard regressions:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s deploy/pi -p test_state.py -v
```

`test_state.py` covers replacement/fingerprints, cleanup, allowlisted env,
WAL/sidecar handling, absent/held locks and stubbed systemctl states.
`test_kit.py` requires age/age-keygen for encryption validation; a plumbing shim
is not an encryption test. No test contacts Telegram, portals or a live host.

The suite creates/deletes dedicated `pisobot-kit-test-*` folders under this
directory, keeps SQLite WAL connections open during export, preserves fake
saved/hidden and per-user state, exercises actual age encryption/import,
checks modes/re-runs, and rejects corrupt archives, wrong identities,
hosts/commits, held locks, unsafe archive paths/links and failed validation.
Its `PISOBOT_TEST_ROOT` escape hatch is confined to a marked private directory,
still checks hostname/architecture, never calls systemd/SSH, and labels archives
test-only. Production import rejects them. Do not use this mode with real data.

### Verify on the actual hosts (still untested)

OS/image dates and package availability above are planning assumptions, not a
provisioning result. Verify Ubuntu 24.04 arm64 on Pi 4/5, Python 3.12/venv,
age `-R`/`-i` and both recipients, ARM64 Pillow wheels/codecs, NTP, unattended
updates, device-tree detection (Pi 400/CM4 are not accepted), USB boot and Imager
cloud-init. Confirm the application commit is reachable and scratch has room.
The write-rate estimate excludes filesystem amplification and extra users.

Exercise SSD missing-mount/reboot guards, ext4 `lost+found`, sidecar ownership,
the handoff/import conditions and `systemctl is-active` after every start.
Masking an `/etc/systemd/system` regular unit may fail; never replace/delete
the unit to force it. Verify the installed drop-in and marker fence instead.
Check the durable encrypted copy survives reboot and memory-backed paths are
refused. Remote shell/scp/readback, real application tests/dry scan, Telegram
takeover, rollback, disaster recovery and the 24-hour soak all remain untested.
