#!/usr/bin/env bash
set -euo pipefail

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo "Source this library from a guarded migration command; do not execute it." >&2
    exit 1
fi

KIT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
umask 077
# shellcheck disable=SC2034
DRY_RUN=0
ROLE="${PISOBOT_HOST_ROLE:-pi}"
TEST_ROOT="${PISOBOT_TEST_ROOT:-}"
STATE="${PISOBOT_STATE_DIR:-/var/lib/pisobot}"
ENV_FILE="${PISOBOT_ENV_FILE:-/etc/pisobot/pisobot.env}"
APP="${PISOBOT_APP_DIR:-/opt/pisobot/app}"
VENV="${PISOBOT_VENV_DIR:-/opt/pisobot/venv}"
SCRATCH="${PISOBOT_TMP_DIR:-/run/pisobot-transfer}"
# Durable (never tmpfs) home for encrypted copies of state that an import replaced.
# shellcheck disable=SC2034
PREVIOUS_DIR="${PISOBOT_PREVIOUS_DIR:-/var/backups/pisobot}"
if [[ -x "$VENV/bin/python" ]]; then
    PYTHON="${PISOBOT_PYTHON:-$VENV/bin/python}"
else
    PYTHON="${PISOBOT_PYTHON:-python3}"
fi
WORK=""

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "Missing dependency: $1"; }
note() { printf '%s\n' "$*"; }
guard_host() {
    [[ -n "${PISOBOT_EXPECT_HOST:-}" ]] || die "Set PISOBOT_EXPECT_HOST to the intended literal hostname."
    [[ "$(hostname -s)" == "$PISOBOT_EXPECT_HOST" ]] || die "Wrong hostname; nothing changed."
    [[ "$ROLE" == pi || "$ROLE" == vps ]] || die "Host role must be pi or vps."
    need "$PYTHON"
    "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' ||
        die "Helper needs Python 3.11+; set PISOBOT_PYTHON to the service's Python 3.12."
    if [[ -n "$TEST_ROOT" ]]; then
        [[ "${PISOBOT_EXPECT_ARCH:-}" == "$(uname -m)" ]] || die "Wrong test architecture."
        "$PYTHON" "$KIT_DIR/state.py" test-paths "$TEST_ROOT" "$STATE" "$ENV_FILE" "$APP" "$VENV" "$SCRATCH"
        note "FAKE-STATE TEST MODE: no systemd, root operations, SSH or real scan."
        return
    fi
    [[ "$(uname -s)" == Linux ]] || die "Production commands require Linux."
    [[ $EUID -eq 0 ]] || die "Run as root using sudo."
    if [[ "$ROLE" == pi ]]; then
        [[ "$(uname -m)" == aarch64 ]] || die "Pi requires 64-bit aarch64, not 32-bit ARM."
        [[ -r /proc/device-tree/model ]] || die "Not a Raspberry Pi."
        grep -Eq 'Raspberry Pi (4|5) Model' /proc/device-tree/model || die "Requires a Raspberry Pi 4 or 5."
    else
        [[ -n "${PISOBOT_EXPECT_ARCH:-}" ]] || die "Set PISOBOT_EXPECT_ARCH from the VPS inventory (usually x86_64)."
        [[ "$(uname -m)" == "$PISOBOT_EXPECT_ARCH" ]] || die "Wrong VPS architecture."
        if [[ -r /proc/device-tree/model ]] && grep -q 'Raspberry Pi' /proc/device-tree/model; then
            die "VPS role refuses a Raspberry Pi; use PISOBOT_HOST_ROLE=pi deliberately."
        fi
    fi
}

guard_test_paths() {
    if [[ -n "$TEST_ROOT" ]]; then
        "$PYTHON" "$KIT_DIR/state.py" test-paths "$TEST_ROOT" "$@"
    fi
}

guard_maintenance() {
    [[ -n "$TEST_ROOT" ]] && return
    need flock
    exec 9>/run/lock/pisobot-maintenance.lock
    flock -n 9 || die "Another provisioning, import or backup job is running."
}

guard_stopped() {
    [[ -n "$TEST_ROOT" ]] && return
    local active loaded unit_state
    loaded="$(systemctl show pisobot-watch.service -p LoadState --value)"
    [[ "$loaded" == loaded || ( "${1:-}" == fenced && "$loaded" == masked ) ]] ||
        die "pisobot-watch.service is not installed."
    active="$(systemctl show pisobot-watch.service -p ActiveState --value)"
    [[ "$active" == inactive || "$active" == failed ]] || die "Stop pisobot-watch first (including any manual runs)."
    if [[ "${1:-}" == disabled ]]; then
        [[ "$(systemctl show pisobot-watch.service -p UnitFileState --value)" == disabled ]] ||
            die "Disable pisobot-watch first; enabled/masked/static/unknown are not accepted."
    elif [[ "${1:-}" == fenced ]]; then
        unit_state="$(systemctl show pisobot-watch.service -p UnitFileState --value)"
        [[ "$unit_state" == disabled || "$unit_state" == masked ]] ||
            die "Disable or mask pisobot-watch first; enabled/static/unknown are not accepted."
    fi
}

guard_storage() {
    [[ -n "$TEST_ROOT" || "$ROLE" == vps ]] && return
    [[ -f /etc/pisobot/pi-storage ]] || die "Run pi-provision.sh before using Pi state."
    local storage
    storage="$(cat /etc/pisobot/pi-storage)"
    if [[ "$storage" != root-disk ]]; then
        [[ "$(findmnt -rn -M "$STATE" -o UUID)" == "$storage" ]] ||
            die "Expected SSD is not mounted at the state directory; refusing SD fallback."
    fi
}

make_work() {
    "$PYTHON" "$KIT_DIR/state.py" scratch-path "$STATE" "$SCRATCH"
    install -d -m 700 "$SCRATCH"
    if [[ $EUID -eq 0 ]]; then
        chown root:root "$SCRATCH"
    fi
    WORK="$(mktemp -d "$SCRATCH/job.XXXXXXXX")"
    trap cleanup EXIT
}

cleanup() {
    if [[ -n "$WORK" ]]; then
        "$PYTHON" "$KIT_DIR/state.py" cleanup "$SCRATCH" "$WORK"
    fi
}

revision() {
    git -c safe.directory="$APP" -C "$APP" diff --quiet ||
        die "Application has uncommitted changes; snapshot a known deployment commit."
    git -c safe.directory="$APP" -C "$APP" diff --cached --quiet ||
        die "Application has staged changes; snapshot a known deployment commit."
    git -c safe.directory="$APP" -C "$APP" rev-parse --verify HEAD
}

verify_checksum() {
    "$PYTHON" "$KIT_DIR/state.py" checksum-check "$1" "$1.sha256"
}

decrypt_archive() {
    local archive="$1" destination="$2"
    [[ -n "${PISOBOT_AGE_IDENTITY:-}" ]] || die "Set PISOBOT_AGE_IDENTITY to a private age identity file."
    guard_test_paths "$PISOBOT_AGE_IDENTITY"
    "$PYTHON" "$KIT_DIR/state.py" private-file "$PISOBOT_AGE_IDENTITY"
    age --decrypt -i "$PISOBOT_AGE_IDENTITY" "$archive" |
        "$PYTHON" "$KIT_DIR/state.py" unpack "$destination"
}
