#!/usr/bin/env bash
# Creates the GhostDrive virtual disk — a 6GB exFAT container file.
# The Blink Sync Module 2 will see this as a USB flash drive.
#
# Usage: sudo bash create_disk.sh [config_file]

set -euo pipefail

CONFIG_FILE="${1:-watchman.conf}"

# ── Load config or use defaults ─────────────────────────────────────────────

CONTAINER="/ghostdrive.bin"
CONTAINER_SIZE_MB=6144

if [ -f "$CONFIG_FILE" ]; then
    while IFS='=' read -r key value; do
        key=$(echo "$key" | tr -d '\r' | xargs)
        value=$(echo "$value" | tr -d '\r' | xargs)
        case "$key" in
            CONTAINER) CONTAINER="$value" ;;
            CONTAINER_SIZE_MB) CONTAINER_SIZE_MB="$value" ;;
        esac
    done < <(tr -d '\r' < "$CONFIG_FILE" | grep -v '^\s*#' | grep '=')
fi

echo "=== GhostDrive Creator ==="
echo "Container: $CONTAINER"
echo "Size:      ${CONTAINER_SIZE_MB}MB"
echo ""

# ── Safety check ────────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
    echo "[ERROR] Run as root: sudo bash create_disk.sh"
    exit 1
fi

if ! command -v mkfs.exfat >/dev/null 2>&1; then
    echo "[ERROR] mkfs.exfat not found. Install with: sudo apt-get install -y exfatprogs"
    exit 1
fi

if ! command -v sfdisk >/dev/null 2>&1; then
    echo "[ERROR] sfdisk not found. Install with: sudo apt-get install -y fdisk"
    exit 1
fi

if ! command -v losetup >/dev/null 2>&1; then
    echo "[ERROR] losetup not found. Install with: sudo apt-get install -y util-linux"
    exit 1
fi

if [ -f "$CONTAINER" ]; then
    echo "[WARN] Container already exists: $CONTAINER"
    read -p "Overwrite? (y/N) " -r
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
fi

# ── Create, partition, and format ──────────────────────────────────────────

echo "[1/4] Creating ${CONTAINER_SIZE_MB}MB container..."
dd if=/dev/zero of="$CONTAINER" bs=1M count="$CONTAINER_SIZE_MB" status=progress

echo ""
echo "[2/4] Creating MBR partition table..."
# Create a DOS/MBR label with a single primary partition that spans the image.
# Type 0x07 improves compatibility for exFAT-like removable media.
printf ',,7,*\n' | sfdisk --label dos "$CONTAINER"

echo ""
echo "[3/4] Attaching loop device and formatting partition as exFAT..."
LOOPDEV=$(losetup --find --show -P "$CONTAINER")
PARTITION="${LOOPDEV}p1"

cleanup() {
    losetup -d "$LOOPDEV" 2>/dev/null || true
}
trap cleanup EXIT

for _ in $(seq 1 10); do
    [ -b "$PARTITION" ] && break
    sleep 1
done

if [ ! -b "$PARTITION" ]; then
    echo "[ERROR] Partition device not found: $PARTITION"
    exit 1
fi

mkfs.exfat -n "GHOSTDRIVE" "$PARTITION"

echo ""
echo "[4/4] Verifying mount..."
TMPDIR=$(mktemp -d)
mount "$PARTITION" "$TMPDIR"
touch "$TMPDIR/.ghostdrive_ok" && rm "$TMPDIR/.ghostdrive_ok"
umount "$TMPDIR"
rmdir "$TMPDIR"

echo ""
echo "[OK] GhostDrive ready: $CONTAINER"
