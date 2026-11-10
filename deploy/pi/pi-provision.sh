#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=deploy/pi/common.bash
source "$(dirname -- "$0")/common.bash"

STORAGE_CHOICE=""
while (( $# )); do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --root-disk) [[ -z "$STORAGE_CHOICE" ]] || die "Choose one storage option"; STORAGE_CHOICE=root-disk; shift ;;
        --state-uuid)
            [[ $# -ge 2 && -z "$STORAGE_CHOICE" ]] || die "--state-uuid requires one UUID and no other storage option"
            STORAGE_CHOICE="$2"; shift 2 ;;
        --help)
            note "Usage: pi-provision.sh [--state-uuid EXT4-UUID | --root-disk] [--dry-run]"
            note "First run requires a storage choice. Re-runs reuse it. Never formats a disk or starts the bot."
            exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done
guard_host
[[ "$ROLE" == pi ]] || die "Provisioning is Pi-only."
[[ -z "$TEST_ROOT" ]] || die "Provisioning cannot run in fake-state mode."
[[ "$STATE" == /var/lib/pisobot && "$ENV_FILE" == /etc/pisobot/pisobot.env &&
   "$APP" == /opt/pisobot/app && "$VENV" == /opt/pisobot/venv ]] || die "Provisioning uses the canonical VPS-compatible paths."
# shellcheck disable=SC1091
source /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || die "Use Ubuntu Server 24.04 LTS ARM64; see the OS/Python section."
if [[ -f /etc/pisobot/pi-storage ]]; then
    EXISTING="$(cat /etc/pisobot/pi-storage)"
    [[ -z "$STORAGE_CHOICE" || "$STORAGE_CHOICE" == "$EXISTING" ]] || die "Changing existing storage requires a separate stopped-state migration."
    STORAGE_CHOICE="$EXISTING"
fi
[[ -n "$STORAGE_CHOICE" ]] || die "Inspect lsblk; choose --state-uuid for SSD or acknowledge --root-disk."
[[ "$STORAGE_CHOICE" == root-disk ||
   "$STORAGE_CHOICE" =~ ^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$ ]] ||
    die "Expected an ext4 filesystem UUID, not a device name."
if (( DRY_RUN )); then
    note "Would require any existing watcher stopped/disabled, and a root-owned app checkout."
    note "Would install Ubuntu security/time/Python/age packages and configure unattended security updates (no automatic reboot)."
    note "Would create pisobot, mode-700 state/config; configure storage=$STORAGE_CHOICE without formatting."
    note "Would create a Python 3.12 venv, install requirements using ARM64 wheels and check Pillow JPEG/PNG."
    note "Would install the VPS unit plus Pi mount/import guards, migration kit and backup timer; leave BOTH disabled."
    exit 0
fi
guard_maintenance
if [[ "$(systemctl show pisobot-watch.service -p LoadState --value)" == loaded ]]; then
    guard_stopped disabled
fi
[[ ! -e "$STATE/.import-in-progress" ]] || die "Resolve the incomplete import before provisioning."
[[ -f "$APP/requirements.txt" && -f "$APP/deploy/vps/pisobot-watch.service" && -d "$APP/tests" ]] ||
    die "Install the source application commit at /opt/pisobot/app first."
[[ -z "$(find "$APP" \( ! -user root -o -perm /022 \) -print -quit)" ]] ||
    die "The application checkout must be root-owned and not group/world writable."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3.12 python3.12-venv \
    ca-certificates age git openssh-client unattended-upgrades systemd-timesyncd

if ! getent group pisobot >/dev/null; then
    groupadd --system pisobot
fi
if ! id pisobot >/dev/null 2>&1; then
    useradd --system --gid pisobot --home-dir /nonexistent --shell /usr/sbin/nologin pisobot
fi
[[ "$(id -gn pisobot)" == pisobot && "$(id -u pisobot)" -lt 1000 &&
   "$(getent passwd pisobot | cut -d: -f7)" == /usr/sbin/nologin ]] ||
    die "Existing pisobot account does not match the dedicated service account."
install -d -m 755 /opt/pisobot
install -d -m 700 /etc/pisobot
if [[ "$STORAGE_CHOICE" != root-disk ]]; then
    DEVICE="$(blkid -U "$STORAGE_CHOICE")"
    [[ -b "$DEVICE" && "$(blkid -s TYPE -o value "$DEVICE")" == ext4 ]] || die "SSD UUID must identify an existing ext4 filesystem."
    if mountpoint -q "$STATE"; then
        [[ "$(findmnt -rn -M "$STATE" -o UUID)" == "$STORAGE_CHOICE" ]] || die "A different disk is mounted at the state directory."
    else
        [[ ! -d "$STATE" || -z "$(find "$STATE" -mindepth 1 -maxdepth 1 -print -quit)" ]] ||
            die "State directory is not empty; refusing to hide existing state under a mount."
        if findmnt -rn -S "UUID=$STORAGE_CHOICE" >/dev/null; then
            die "SSD is mounted elsewhere; unmount it deliberately before provisioning."
        fi
    fi
    FSTAB_LINE="UUID=$STORAGE_CHOICE /var/lib/pisobot ext4 defaults,noatime,nofail,x-systemd.device-timeout=30s 0 2"
    OLD_LINE="$(awk '$1 !~ /^#/ && $2 == "/var/lib/pisobot" {print}' /etc/fstab)"
    [[ -z "$OLD_LINE" || "$OLD_LINE" == "$FSTAB_LINE" ]] || die "Existing fstab entry conflicts; review it manually."
    if [[ -z "$OLD_LINE" ]]; then
        printf '%s\n' "$FSTAB_LINE" >> /etc/fstab
    fi
    install -d -m 700 "$STATE"
    systemctl daemon-reload
    if ! mountpoint -q "$STATE"; then
        mount "$STATE"
    fi
    [[ "$(findmnt -rn -M "$STATE" -o UUID)" == "$STORAGE_CHOICE" ]] || die "SSD mount verification failed."
else
    if mountpoint -q "$STATE"; then
        die "--root-disk refuses a separate state mount; select its UUID instead."
    fi
    note "State will use the root filesystem. If it is on microSD, use a high-endurance card and nightly off-box backups."
fi
install -d -o pisobot -g pisobot -m 700 "$STATE"
printf '%s\n' "$STORAGE_CHOICE" > /etc/pisobot/pi-storage
chmod 600 /etc/pisobot/pi-storage

(
    umask 022
    if [[ ! -e "$VENV" ]]; then
        /usr/bin/python3.12 -m venv "$VENV"
    fi
    "$VENV/bin/python" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Existing venv is not Python 3.12"'
    "$VENV/bin/python" -m pip install --only-binary=:all: -r "$APP/requirements.txt"
    "$VENV/bin/python" -m pip check
)
cd -- "$APP"
runuser -u pisobot -- "$VENV/bin/python" - <<'PY'
from io import BytesIO
from PIL import Image, features
assert features.check("jpg") and features.check("zlib"), "Pillow JPEG/PNG support missing"
for fmt in ("JPEG", "PNG"):
    data = BytesIO()
    Image.new("RGB", (4, 4), "red").save(data, format=fmt)
    data.seek(0)
    with Image.open(data) as image:
        image.load()
        assert image.size == (4, 4)
print("Pillow JPEG/PNG encode/decode OK as pisobot")
PY

cat > /etc/apt/apt.conf.d/52pisobot-periodic <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
Unattended-Upgrade::Automatic-Reboot "false";
CONF
chmod 644 /etc/apt/apt.conf.d/52pisobot-periodic
systemctl enable --now apt-daily.timer apt-daily-upgrade.timer
systemctl enable --now systemd-timesyncd.service
timedatectl set-ntp true
timeout 120s systemctl start systemd-time-wait-sync.service
[[ "$(timedatectl show -p NTPSynchronized --value)" == yes ]] || die "Clock is not synchronized; fix NTP/network and re-run."

install -m 644 "$APP/deploy/vps/pisobot-watch.service" /etc/systemd/system/pisobot-watch.service
install -d -m 755 /etc/systemd/system/pisobot-watch.service.d
cat > /etc/systemd/system/pisobot-watch.service.d/pi.conf <<'UNIT'
[Unit]
Wants=time-sync.target systemd-time-wait-sync.service
After=time-sync.target
RequiresMountsFor=/var/lib/pisobot
ConditionPathExists=!/var/lib/pisobot/.import-in-progress
ConditionPathExists=!/var/lib/pisobot/HANDED-OFF-TO-PI

[Service]
ExecStartPre=/usr/bin/test ! -e /var/lib/pisobot/.import-in-progress
ExecStartPre=/usr/bin/test ! -e /var/lib/pisobot/HANDED-OFF-TO-PI
UNIT
if [[ "$STORAGE_CHOICE" != root-disk ]]; then
    cat >> /etc/systemd/system/pisobot-watch.service.d/pi.conf <<'UNIT'

[Unit]
BindsTo=var-lib-pisobot.mount
After=var-lib-pisobot.mount
ConditionPathIsMountPoint=/var/lib/pisobot
UNIT
fi
chmod 644 /etc/systemd/system/pisobot-watch.service.d/pi.conf
install -d -m 755 /opt/pisobot/migration
if [[ "$KIT_DIR" != /opt/pisobot/migration ]]; then
    for file in common.bash state.py pi-provision.sh vps-export.sh pi-import.sh nightly-backup.sh README.md cutover.md pisobot-backup.service pisobot-backup.timer pisobot-vps-handoff.conf; do
        install -m 644 "$KIT_DIR/$file" "/opt/pisobot/migration/$file"
    done
fi
install -m 644 "$KIT_DIR/pisobot-backup.service" "$KIT_DIR/pisobot-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl disable pisobot-watch.service
systemctl disable --now pisobot-backup.timer
note "PROVISION OK: watcher and backup timer are disabled. Configuration/state will come from encrypted import."
