#!/usr/bin/env bash
set -euo pipefail
PISOBOT_HOST_ROLE="${PISOBOT_HOST_ROLE:-vps}"
# shellcheck source=deploy/pi/common.bash
source "$(dirname -- "$0")/common.bash"

ARCHIVE="${PISOBOT_ARCHIVE:-}"
FINAL=0
QUIESCED=0
while (( $# )); do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --final) FINAL=1; QUIESCED=1; shift ;;
        --quiesced) QUIESCED=1; shift ;;
        --archive) [[ $# -ge 2 ]] || die "--archive needs a path"; ARCHIVE="$2"; shift 2 ;;
        --help)
            note "Usage: vps-export.sh --archive /absolute/file.age [--final|--quiesced] [--dry-run]"
            note "Required: PISOBOT_EXPECT_HOST, PISOBOT_EXPECT_ARCH (VPS), PISOBOT_AGE_RECIPIENTS."
            note "--final fences source state under the application locks, even if export later fails."
            note "Install the handoff drop-in first; see cutover.md for failed-export recovery."
            exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done
guard_host
[[ "$ARCHIVE" == /*.age ]] || die "Choose an absolute .age archive path."
[[ -n "${PISOBOT_AGE_RECIPIENTS:-}" ]] || die "Set PISOBOT_AGE_RECIPIENTS to an age public recipients file."
guard_test_paths "$ARCHIVE" "$PISOBOT_AGE_RECIPIENTS"
if (( DRY_RUN )); then
    note "Would verify host/storage and source commit; stop/disable NOTHING."
    note "Would require inactive source for --quiesced, and disabled/masked source for --final."
    note "For --final, would persist HANDED-OFF-TO-PI under application locks before snapshotting."
    note "Would retain that startup fence on later failure; see cutover.md for recovery."
    note "Would snapshot ALL SQLite databases via online backup, copy JSON/users/tiles and private env."
    note "Would encrypt to $ARCHIVE using the recipients file; publish a digest-only .sha256 sidecar."
    note "Would omit WAL/SHM/journal/lock/pending files. No credentials read in --dry-run."
    exit 0
fi
need age
need git
guard_storage
[[ ! -e "$ARCHIVE" && ! -L "$ARCHIVE" && ! -e "$ARCHIVE.sha256" && ! -L "$ARCHIVE.sha256" ]] ||
    die "Output already exists; choose a fresh name (no overwrite)."
[[ -d "$(dirname -- "$ARCHIVE")" && ! -L "$(dirname -- "$ARCHIVE")" ]] || die "Create the private output directory first."
[[ -f "$PISOBOT_AGE_RECIPIENTS" ]] || die "Recipients file is missing."
[[ ! -e "$STATE/.import-in-progress" ]] || die "State has an incomplete import; resolve it first."
if (( FINAL )); then
    guard_stopped fenced
    note "FINAL EXPORT: a fail-closed handoff fence is established under application locks."
    note "It remains on any later failure; do not restart either host without the cutover.md recovery checks."
elif (( QUIESCED )); then
    guard_stopped
else
    note "LIVE EXPORT: each SQLite DB is consistent; DB/JSON files are NOT one cross-file transaction."
    note "This rehearsal snapshot cannot be used for cutover. Use --final after stopping and disabling."
fi
make_work
"$PYTHON" "$KIT_DIR/state.py" snapshot "$STATE" "$ENV_FILE" "$WORK/snapshot" \
    "$(revision)" "$PISOBOT_EXPECT_HOST" "$QUIESCED" "$FINAL"
"$PYTHON" "$KIT_DIR/state.py" pack "$WORK/snapshot" |
    age --encrypt -R "$PISOBOT_AGE_RECIPIENTS" -o "$WORK/export.age"
# noclobber also protects a simultaneous export to the same filename.
(set -C; cat "$WORK/export.age" > "$ARCHIVE")
"$PYTHON" "$KIT_DIR/state.py" checksum-write "$ARCHIVE"
if (( FINAL )); then
    note "Source marked HANDED-OFF-TO-PI; keep its handoff drop-in installed until deliberate rollback."
fi
note "Encrypted export complete. Transfer only the .age archive and .sha256 sidecar."
