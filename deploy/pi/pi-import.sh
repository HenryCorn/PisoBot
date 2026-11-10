#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=deploy/pi/common.bash
source "$(dirname -- "$0")/common.bash"

ARCHIVE="${PISOBOT_ARCHIVE:-}"
SOURCE_HOST=""
RESTORE_TEST=0
RECOVERY=0
while (( $# )); do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --restore-test) RESTORE_TEST=1; shift ;;
        --recovery) RECOVERY=1; shift ;;
        --archive) [[ $# -ge 2 ]] || die "--archive needs a path"; ARCHIVE="$2"; shift 2 ;;
        --source-stopped) [[ $# -ge 2 ]] || die "--source-stopped needs a hostname"; SOURCE_HOST="$2"; shift 2 ;;
        --help)
            note "Usage: pi-import.sh --archive /absolute/file.age --source-stopped SOURCE [--recovery] [--dry-run]"
            note "Or: pi-import.sh --archive /absolute/file.age --restore-test [--dry-run]"
            note "Required: PISOBOT_EXPECT_HOST, PISOBOT_AGE_IDENTITY. Never enables the service."
            exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done
guard_host
[[ "$ARCHIVE" == /*.age ]] || die "Choose an absolute .age archive path."
guard_test_paths "$ARCHIVE"
if (( ! RESTORE_TEST )); then
    [[ -n "$SOURCE_HOST" ]] || die "Acknowledge the fenced source with --source-stopped HOST."
fi
if (( DRY_RUN )); then
    note "Would verify encrypted SHA256, decrypt privately and check all hashes/SQLite/JSON."
    if (( RESTORE_TEST )); then
        note "Would discard the restored scratch copy; no production writes, tests, network or scans."
    else
        note "Would require destination stopped/disabled, source fenced, matching app commit and mounted state."
        note "Would replace (not merge) state/env, set owner/modes, run isolated unit tests and one dry-run scan."
        note "Would retain failed imports/logs privately; never enable/start pisobot-watch."
    fi
    exit 0
fi
need age
if (( ! RESTORE_TEST )); then
    guard_maintenance
    guard_stopped disabled
    guard_storage
fi
verify_checksum "$ARCHIVE"
make_work
decrypt_archive "$ARCHIVE" "$WORK/restored"
if (( RESTORE_TEST )); then
    "$PYTHON" "$KIT_DIR/state.py" verify "$WORK/restored"
    note "Restore-only drill complete; application compatibility and home-network access were not tested."
    exit 0
fi
MODE=final
(( ! RECOVERY )) || MODE=recovery
"$PYTHON" "$KIT_DIR/state.py" verify "$WORK/restored" "$MODE" "$SOURCE_HOST" "$(revision)"
"$PYTHON" "$KIT_DIR/state.py" env-check "$WORK/restored/env/pisobot.env" "$STATE"
[[ -x "$VENV/bin/python" ]] || die "Provision the virtualenv first."
if [[ -z "$TEST_ROOT" ]]; then
    "$VENV/bin/python" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 required"'
    [[ "$(timedatectl show -p NTPSynchronized --value)" == yes ]] || die "Wait for time synchronization first."
fi

# Failed validation must never look like a completed import. Keep prior state and
# diagnostics in the private job directory; the marker also gates Pi startup.
import_cleanup() {
    local result=$?
    if (( result != 0 )); then
        note "IMPORT FAILED: service remains disabled. Private recovery files/logs retained at $WORK" >&2
    else
        cleanup
    fi
}
trap import_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PYTHON" "$KIT_DIR/state.py" install "$WORK/restored" "$STATE" "$ENV_FILE"
UID_STATE="$(id -u)"
GID_STATE="$(id -g)"
if [[ -z "$TEST_ROOT" ]]; then
    UID_STATE="$(id -u pisobot)"
    GID_STATE="$(id -g pisobot)"
fi
"$PYTHON" "$KIT_DIR/state.py" permissions "$STATE" "$ENV_FILE" "$UID_STATE" "$GID_STATE"
cd -- "$APP"
if [[ -n "$TEST_ROOT" ]]; then
    env -i PATH="$PATH" HOME="$TEST_ROOT" PYTHONDONTWRITEBYTECODE=1 \
        "$VENV/bin/python" -m unittest discover -s tests > "$WORK/unit-tests.log" 2>&1
    env -i PATH="$PATH" HOME="$TEST_ROOT" PYTHONDONTWRITEBYTECODE=1 STORAGE=local PISOBOT_STATE_DIR="$STATE" \
        "$VENV/bin/python" -m pisobot.watch --once --dry-run > "$WORK/dry-run.log" 2>&1
else
    runuser -u pisobot -- env -i PATH=/usr/bin:/bin HOME=/nonexistent PYTHONDONTWRITEBYTECODE=1 \
        "$VENV/bin/python" -m unittest discover -s tests > "$WORK/unit-tests.log" 2>&1
    systemd-run --quiet --wait --pipe --collect --unit=pisobot-migration-check \
        -p User=pisobot -p Group=pisobot -p "WorkingDirectory=$APP" \
        -p "EnvironmentFile=$ENV_FILE" -p Environment=PYTHONDONTWRITEBYTECODE=1 \
        -p UMask=0077 -p NoNewPrivileges=true \
        "$VENV/bin/python" -m pisobot.watch --once --dry-run > "$WORK/dry-run.log" 2>&1
fi
"$PYTHON" "$KIT_DIR/state.py" seen-check "$WORK/restored/state" "$STATE"
"$PYTHON" "$KIT_DIR/state.py" permissions "$STATE" "$ENV_FILE" "$UID_STATE" "$GID_STATE"
rm -- "$STATE/.import-in-progress"
note "IMPORT OK: unit tests and dry-run scan passed; seen state unchanged; permissions applied."
note "pisobot-watch remains disabled. Follow cutover.md before enabling either host."
