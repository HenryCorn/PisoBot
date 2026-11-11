#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
    echo "Usage: mac-pull-backup.sh SSH_HOST BACKUP_DIR AGE_RECIPIENTS AGE_IDENTITY [RETENTION_DAYS]" >&2
    exit 2
}
die() { echo "ERROR: $*" >&2; exit 1; }

[[ $# -ge 4 && $# -le 5 ]] || usage
HOST="$1"
DEST="$2"
RECIPIENTS="$3"
IDENTITY="$4"
RETENTION="${5:-14}"
[[ "$HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9@._-]*$ ]] || die "Use a simple SSH host alias or user@hostname."
[[ "$DEST" == /* ]] || die "Backup directory must be an absolute path."
[[ "$RETENTION" =~ ^[0-9]+$ ]] && (( RETENTION >= 1 && RETENTION <= 3650 )) || die "Retention must be 1-3650 days."
[[ -f "$RECIPIENTS" && ! -L "$RECIPIENTS" ]] || die "Age recipients file is missing or unsafe."
[[ -f "$IDENTITY" && ! -L "$IDENTITY" ]] || die "Age identity file is missing or unsafe."
command -v age >/dev/null || die "Install age first."
command -v gzip >/dev/null || die "gzip is required."
command -v python3 >/dev/null || die "Python 3 is required."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' ||
    die "Python 3.11 or newer is required."
command -v ssh >/dev/null || die "ssh is required."

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
HELPER="$SCRIPT_DIR/mac_backup.py"
mkdir -p "$DEST"
[[ ! -L "$DEST" ]] || die "Backup directory must not be a symlink."
DEST="$(cd "$DEST" && pwd -P)"
WORK="$(mktemp -d "$DEST/.pisobot-backup.XXXXXXXX")"
chmod 700 "$WORK"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

NAME="pisobot-$(date -u +%Y%m%dT%H%M%SZ).tar.gz.age"
[[ ! -e "$DEST/$NAME" && ! -e "$DEST/$NAME.sha256" ]] || die "Backup name already exists; retry shortly."

SSH_OPTS=(-T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=20
          -o ServerAliveInterval=30 -o ServerAliveCountMax=3)
ssh "${SSH_OPTS[@]}" "$HOST" 'sudo -n -u pisobot python3 - export' < "$HELPER" |
    gzip -1 -c |
    age --encrypt -R "$RECIPIENTS" -o "$WORK/$NAME"

mkdir -m 700 "$WORK/restore"
age --decrypt -i "$IDENTITY" "$WORK/$NAME" |
    gzip -dc |
    python3 "$HELPER" verify "$WORK/restore"

shasum -a 256 "$WORK/$NAME" | awk '{print $1}' > "$WORK/$NAME.sha256"
mv "$WORK/$NAME" "$DEST/$NAME"
mv "$WORK/$NAME.sha256" "$DEST/$NAME.sha256"
python3 "$HELPER" prune "$DEST" "$RETENTION"
echo "Encrypted backup verified: $DEST/$NAME"
