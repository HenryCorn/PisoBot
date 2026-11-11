#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=deploy/pi/common.bash
source "$(dirname -- "$0")/common.bash"

ARCHIVE="${PISOBOT_ARCHIVE:-}"
SOURCE_HOST=""
RESTORE_TEST=0
RECOVERY=0
REPLACE_STATE=0
ALLOW_OLDER=0
COPY=""
while (( $# )); do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --restore-test) RESTORE_TEST=1; shift ;;
        --recovery) RECOVERY=1; shift ;;
        --replace-state) REPLACE_STATE=1; shift ;;
        --allow-older-archive) ALLOW_OLDER=1; shift ;;
        --archive) [[ $# -ge 2 ]] || die "--archive needs a path"; ARCHIVE="$2"; shift 2 ;;
        --source-stopped) [[ $# -ge 2 ]] || die "--source-stopped needs a hostname"; SOURCE_HOST="$2"; shift 2 ;;
        --help)
            note "Usage: pi-import.sh --archive /absolute/file.age --source-stopped SOURCE [--recovery] [--dry-run]"
            note "       [--replace-state [--allow-older-archive]]   (only when the destination already holds state)"
            note "Or: pi-import.sh --archive /absolute/file.age --restore-test [--dry-run]"
            note "Required: PISOBOT_EXPECT_HOST, PISOBOT_AGE_IDENTITY. Never enables the service."
            note "--replace-state also needs PISOBOT_AGE_RECIPIENTS: the existing state is first stored as a"
            note "verified encrypted copy in \$PISOBOT_PREVIOUS_DIR (default /var/backups/pisobot)."
            exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done
(( REPLACE_STATE || ! ALLOW_OLDER )) || die "--allow-older-archive only applies together with --replace-state."
(( ! RESTORE_TEST || ! REPLACE_STATE )) || die "--replace-state does not apply to --restore-test (it never installs)."
guard_host
[[ "$ARCHIVE" == /*.age ]] || die "Choose an absolute .age archive path."
guard_test_paths "$ARCHIVE"
if (( ! RESTORE_TEST )); then
    guard_test_paths "$PREVIOUS_DIR"
    [[ -n "$SOURCE_HOST" ]] || die "Acknowledge the fenced source with --source-stopped HOST."
fi
if (( DRY_RUN )); then
    note "Would verify encrypted SHA256, decrypt privately and check all hashes/SQLite/JSON."
    if (( RESTORE_TEST )); then
        note "Would discard the restored scratch copy; no production writes, tests, network or scans."
    else
        note "Would require destination stopped/disabled, source fenced, matching app commit and mounted state."
        note "Would refuse a destination that already holds state unless --replace-state; then would first store a"
        note "verified encrypted copy of it under $PREVIOUS_DIR (needs PISOBOT_AGE_RECIPIENTS) and refuse an"
        note "archive older than the existing state unless --allow-older-archive."
        note "Would replace (not merge) state/env, set owner/modes, run isolated unit tests and one dry-run scan."
        note "On failure would keep only the logs (plaintext copies are scrubbed); never enable/start pisobot-watch."
    fi
    exit 0
fi
need age
if (( ! RESTORE_TEST )); then
    guard_maintenance
    if [[ "$ROLE" == vps ]]; then
        guard_stopped fenced
    else
        guard_stopped disabled
    fi
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
REVISION="$(revision)"
"$PYTHON" "$KIT_DIR/state.py" verify "$WORK/restored" "$MODE" "$SOURCE_HOST" "$REVISION"
"$PYTHON" "$KIT_DIR/state.py" env-check "$WORK/restored/env/pisobot.env" "$STATE"
[[ -x "$VENV/bin/python" ]] || die "Provision the virtualenv first."
if [[ -z "$TEST_ROOT" ]]; then
    "$VENV/bin/python" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 required"'
    [[ "$(timedatectl show -p NTPSynchronized --value)" == yes ]] || die "Wait for time synchronization first."
fi

# Replacing existing state is the one irreversible step. Store a verified encrypted copy on
# durable storage (never the tmpfs scratch) first, and refuse to move the live state back in
# time without an explicit flag. Every failure here happens before anything is changed.
INSTALL_REPLACE=0
INSTALL_FP=""
store_existing_state() {
    local fstype
    [[ -n "${PISOBOT_AGE_RECIPIENTS:-}" && -f "$PISOBOT_AGE_RECIPIENTS" ]] ||
        die "--replace-state needs PISOBOT_AGE_RECIPIENTS (an age public recipients file) for the safety copy."
    guard_test_paths "$PISOBOT_AGE_RECIPIENTS"
    "$PYTHON" "$KIT_DIR/state.py" durable-path "$PREVIOUS_DIR" "$STATE" "$SCRATCH"
    # Fingerprint BEFORE the snapshot: a write after this point makes the install refuse.
    INSTALL_FP="$("$PYTHON" "$KIT_DIR/state.py" fingerprint "$STATE")"
    "$PYTHON" "$KIT_DIR/state.py" snapshot "$STATE" "$ENV_FILE" "$WORK/existing" "$REVISION" \
        "$PISOBOT_EXPECT_HOST" 1 0 ||
        die "Cannot take a verified copy of the existing state; nothing changed."
    "$PYTHON" "$KIT_DIR/state.py" replace-check "$WORK/existing" "$WORK/restored" "$ALLOW_OLDER"
    install -d -m 700 "$PREVIOUS_DIR"
    if [[ $EUID -eq 0 ]]; then
        chown root:root "$PREVIOUS_DIR"
    fi
    if [[ -z "$TEST_ROOT" ]]; then
        need findmnt
        fstype="$(findmnt -no FSTYPE -T "$PREVIOUS_DIR")" || die "Cannot determine the filesystem of $PREVIOUS_DIR."
        [[ "$fstype" != tmpfs && "$fstype" != ramfs ]] ||
            die "$PREVIOUS_DIR is memory-backed; set PISOBOT_PREVIOUS_DIR to durable storage."
    fi
    COPY="$PREVIOUS_DIR/replaced-$(date -u +%Y%m%dT%H%M%SZ)-$$.age"
    "$PYTHON" "$KIT_DIR/state.py" pack "$WORK/existing" |
        age --encrypt -R "$PISOBOT_AGE_RECIPIENTS" -o "$WORK/existing.age"
    # noclobber: never overwrite an earlier copy.
    (set -C; cat "$WORK/existing.age" > "$COPY")
    # A copy that cannot be decrypted and verified is not a copy: remove it and refuse.
    if ! { "$PYTHON" "$KIT_DIR/state.py" checksum-write "$COPY" &&
           verify_checksum "$COPY" &&
           decrypt_archive "$COPY" "$WORK/existing-check" &&
           "$PYTHON" "$KIT_DIR/state.py" verify "$WORK/existing-check" > /dev/null; }; then
        rm -f -- "$COPY" "$COPY.sha256"
        die "The encrypted copy of the existing state could not be verified; nothing changed."
    fi
    "$PYTHON" "$KIT_DIR/state.py" persist "$COPY" "$COPY.sha256" ||
        die "Cannot synchronize the verified safety copy and directory ancestry; nothing replaced. Keep services stopped and resolve storage errors before retrying."
    "$PYTHON" "$KIT_DIR/state.py" discard "$WORK" existing existing.age existing-check
    INSTALL_REPLACE=1
    note "Existing state stored as a verified encrypted copy: $COPY (keep it until the next verified nightly backup)."
}
STATE_STATUS="$("$PYTHON" "$KIT_DIR/state.py" state-status "$STATE")"
if [[ "$STATE_STATUS" == occupied ]]; then
    (( REPLACE_STATE )) || die "Destination state already holds data; nothing changed. Re-run with --replace-state to replace it (a verified encrypted copy is stored first)."
    store_existing_state
else
    note "Destination state is empty; nothing to preserve."
fi

# Failed validation must never look like a completed import. The marker stays and also gates
# Pi startup. Only the logs are kept: plaintext copies of state and secrets must not linger in
# /run, and the previous state is already stored as a verified encrypted copy.
import_cleanup() {
    local result=$?
    if (( result != 0 )); then
        "$PYTHON" "$KIT_DIR/state.py" retain "$WORK" unit-tests.log dry-run.log ||
            note "WARNING: could not scrub plaintext copies; delete $WORK manually." >&2
        note "IMPORT FAILED: service remains disabled. Only the logs are kept, at $WORK" >&2
        if [[ -e "$STATE/.import-in-progress" ]]; then
            note "The state directory is marked incomplete (.import-in-progress); follow README \"Failure procedure\" before retrying." >&2
        fi
        if [[ -n "$COPY" ]]; then
            note "The state that was replaced is stored encrypted at $COPY (check it with --restore-test)." >&2
        fi
    else
        cleanup
    fi
}
trap import_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PYTHON" "$KIT_DIR/state.py" install "$WORK/restored" "$STATE" "$ENV_FILE" "$INSTALL_REPLACE" "$INSTALL_FP"
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
"$PYTHON" "$KIT_DIR/state.py" handoff "$STATE" clear
rm -- "$STATE/.import-in-progress"
note "IMPORT OK: unit tests and dry-run scan passed; seen state unchanged; permissions applied."
if [[ -n "$COPY" ]]; then
    note "Replaced state remains available only as the encrypted copy $COPY; delete it after the next verified nightly backup."
fi
note "pisobot-watch remains disabled. Follow cutover.md before enabling either host."
