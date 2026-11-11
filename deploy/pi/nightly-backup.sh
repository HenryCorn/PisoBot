#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=deploy/pi/common.bash
source "$(dirname -- "$0")/common.bash"

while (( $# )); do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --help) note "Usage: nightly-backup.sh [--dry-run]; configure /etc/pisobot/backup.env as documented."; exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done
guard_host
[[ "$ROLE" == pi ]] || die "Nightly backup is a Pi-only command."
[[ -n "${PISOBOT_BACKUP_TARGET:-}" && -n "${PISOBOT_BACKUP_DIR:-}" ]] || die "Set backup SSH target and remote directory."
[[ "$PISOBOT_BACKUP_TARGET" =~ ^[a-zA-Z0-9][a-zA-Z0-9@._-]*$ ]] || die "Use a simple SSH host alias or user@hostname."
[[ "$PISOBOT_BACKUP_DIR" =~ ^/[a-zA-Z0-9_./-]+$ && "$PISOBOT_BACKUP_DIR" != *..* ]] || die "Use a simple absolute remote directory without spaces or '..'."
[[ -n "${PISOBOT_AGE_RECIPIENTS:-}" && -n "${PISOBOT_AGE_IDENTITY:-}" ]] || die "Configure backup encryption and restore-test identity."
if (( DRY_RUN )); then
    note "Would lock out overlapping backups, briefly stop the Pi watcher (not disable), snapshot and restart it."
    note "Would upload ONLY encrypted state/env + SHA256 to the configured off-box SSH target."
    note "Would download them again, decrypt/restore-test in scratch, mark remote success, optionally ping HEALTHCHECK_URL."
    note "Would leave retention decisions to the owner; never start the VPS watcher."
    exit 0
fi
[[ -z "$TEST_ROOT" ]] || die "Fake-state mode cannot contact backup targets or operate services."
need age
need ssh
need scp
guard_storage
guard_maintenance
[[ "$(systemctl show pisobot-watch.service -p ActiveState --value)" == active ]] || die "Watcher is not active; investigate rather than silently backing up a failed service."
[[ "$(systemctl show pisobot-watch.service -p UnitFileState --value)" == enabled ]] || die "Watcher must be enabled; do not back up during cutover."
make_work
RESTART=0
backup_cleanup() {
    local result=$?
    if (( RESTART )); then
        if ! systemctl start pisobot-watch.service; then
            note "ERROR: backup cleanup could not restart the Pi watcher; intervene now." >&2
            result=1
        fi
    fi
    cleanup
    exit "$result"
}
trap backup_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
NAME="pisobot-$(date -u +%Y%m%dT%H%M%SZ)-$$"
ARCHIVE="$WORK/$NAME.age"
RESTART=1
systemctl stop pisobot-watch.service
PISOBOT_HOST_ROLE=pi bash "$KIT_DIR/vps-export.sh" --quiesced --archive "$ARCHIVE"
systemctl start pisobot-watch.service
RESTART=0
systemctl is-active --quiet pisobot-watch.service || die "Watcher failed to resume after snapshot."
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=3)
REMOTE_ID="$(ssh "${SSH_OPTS[@]}" "$PISOBOT_BACKUP_TARGET" \
    "test -d '$PISOBOT_BACKUP_DIR' && test \"\$(stat -c %a '$PISOBOT_BACKUP_DIR')\" = 700 && cat /etc/machine-id")"
[[ "$REMOTE_ID" =~ ^[a-f0-9]{32}$ && "$REMOTE_ID" != "$(cat /etc/machine-id)" ]] ||
    die "Backup target must be a different Linux machine with a private mode-700 backup directory."
REMOTE="$PISOBOT_BACKUP_DIR/$NAME"
ssh "${SSH_OPTS[@]}" "$PISOBOT_BACKUP_TARGET" "umask 077; mkdir '$REMOTE'"
scp "${SSH_OPTS[@]}" "$ARCHIVE" "$ARCHIVE.sha256" "$PISOBOT_BACKUP_TARGET:$REMOTE/"
mkdir -m 700 "$WORK/readback"
scp "${SSH_OPTS[@]}" "$PISOBOT_BACKUP_TARGET:$REMOTE/$NAME.age" \
    "$PISOBOT_BACKUP_TARGET:$REMOTE/$NAME.age.sha256" "$WORK/readback/"
cmp -- "$ARCHIVE" "$WORK/readback/$NAME.age" || die "Off-box archive differs from uploaded ciphertext."
cmp -- "$ARCHIVE.sha256" "$WORK/readback/$NAME.age.sha256" || die "Off-box checksum differs from uploaded checksum."
bash "$KIT_DIR/pi-import.sh" --restore-test --archive "$WORK/readback/$NAME.age"
ssh "${SSH_OPTS[@]}" "$PISOBOT_BACKUP_TARGET" "umask 077; touch '$REMOTE/RESTORE-VERIFIED'"
"$PYTHON" "$KIT_DIR/state.py" healthcheck
note "NIGHTLY BACKUP OK: off-box readback, decryption, SQLite/JSON and hash checks passed."
