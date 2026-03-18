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

if [ -f "$CONTAINER" ]; then
    echo "[WARN] Container already exists: $CONTAINER"
    read -p "Overwrite? (y/N) " -r
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
fi

# ── Create and format ──────────────────────────────────────────────────────

echo "[1/3] Creating ${CONTAINER_SIZE_MB}MB container..."
dd if=/dev/zero of="$CONTAINER" bs=1M count="$CONTAINER_SIZE_MB" status=progress

echo ""
echo "[2/3] Formatting as exFAT..."
mkfs.exfat -n "GHOSTDRIVE" "$CONTAINER"

echo ""
echo "[3/3] Verifying mount..."
TMPDIR=$(mktemp -d)
mount -o loop "$CONTAINER" "$TMPDIR"
touch "$TMPDIR/.ghostdrive_ok" && rm "$TMPDIR/.ghostdrive_ok"
umount "$TMPDIR"
rmdir "$TMPDIR"

echo ""
echo "[OK] GhostDrive ready: $CONTAINER"
