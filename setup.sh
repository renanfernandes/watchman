#!/usr/bin/env bash
# Watchman Setup
# Configures a Raspberry Pi Zero 2 W as a virtual USB drive for Blink Sync Module 2.
#
# What this does:
#   1. Installs required packages (exfatprogs, python3-flask, watchdog)
#   2. Configures USB gadget mode in boot files (config.txt + cmdline.txt)
#   3. Creates the GhostDrive virtual disk (6GB exFAT)
#   4. Installs Watchman service files
#   5. Enables auto-start on boot
#   6. Configures hardware watchdog (auto-reboot on system hang)
#
# Usage: sudo bash setup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/watchman"
CONFIG_DIR="/etc/watchman"
CONFIG_FILE="$CONFIG_DIR/watchman.conf"

# ── Pre-checks ──────────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
    echo "[ERROR] Run as root: sudo bash setup.sh"
    exit 1
fi

# Detect boot directory (Bookworm = /boot/firmware, Bullseye = /boot)
if [ -f /boot/firmware/config.txt ]; then
    BOOT_DIR="/boot/firmware"
elif [ -f /boot/config.txt ]; then
    BOOT_DIR="/boot"
else
    echo "[ERROR] Cannot find boot config. Is this a Raspberry Pi?"
    exit 1
fi

echo "=== Watchman Setup ==="
echo "Boot dir:    $BOOT_DIR"
echo "Install dir: $INSTALL_DIR"
echo "Config:      $CONFIG_FILE"
echo ""

# ── Step 1: Install dependencies ────────────────────────────────────────────

echo "[1/7] Installing dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-flask exfatprogs watchdog
echo "[OK] Dependencies installed"
echo ""

# ── Step 2: Configure boot for USB gadget mode ──────────────────────────────

echo "[2/7] Configuring boot files for USB gadget mode..."

# config.txt — enable the dwc2 USB controller in peripheral (gadget) mode.
# The overlay MUST be in the [all] section so it applies to every Pi model.
# Remove any dwc2 overlay lines from model-specific sections first.
sed -i '/^\[cm4\]/,/^\[/{/dtoverlay=dwc2/d}' "$BOOT_DIR/config.txt"
sed -i '/^\[cm5\]/,/^\[/{/dtoverlay=dwc2/d}' "$BOOT_DIR/config.txt"

# Now ensure it's in [all]. Check if [all] section exists and has the line.
if grep -q "^\[all\]" "$BOOT_DIR/config.txt"; then
    if ! sed -n '/^\[all\]/,/^\[/p' "$BOOT_DIR/config.txt" | grep -q "dtoverlay=dwc2"; then
        sed -i '/^\[all\]/a # Enable USB gadget mode (added by Watchman setup)\ndtoverlay=dwc2,dr_mode=peripheral' "$BOOT_DIR/config.txt"
        echo "  Added dtoverlay=dwc2,dr_mode=peripheral to [all] section"
    else
        echo "  dtoverlay=dwc2 already in [all] section"
    fi
else
    echo "" >> "$BOOT_DIR/config.txt"
    echo "[all]" >> "$BOOT_DIR/config.txt"
    echo "# Enable USB gadget mode (added by Watchman setup)" >> "$BOOT_DIR/config.txt"
    echo "dtoverlay=dwc2,dr_mode=peripheral" >> "$BOOT_DIR/config.txt"
    echo "  Created [all] section with dtoverlay=dwc2,dr_mode=peripheral"
fi

# cmdline.txt — load dwc2 module at boot
if ! grep -q "modules-load=dwc2" "$BOOT_DIR/cmdline.txt"; then
    sed -i 's/rootwait/rootwait modules-load=dwc2/' "$BOOT_DIR/cmdline.txt"
    echo "  Added modules-load=dwc2 to cmdline.txt"
else
    echo "  modules-load=dwc2 already present in cmdline.txt"
fi

echo "[OK] Boot configured"
echo ""

# ── Step 3: Install Watchman files ──────────────────────────────────────────

echo "[3/7] Installing Watchman..."
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR"

cp "$SCRIPT_DIR/watchman.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/web.py" "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/templates" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/watchman.py" "$INSTALL_DIR/web.py"

# Only install config if it doesn't already exist (don't overwrite user edits)
if [ ! -f "$CONFIG_FILE" ]; then
    cp "$SCRIPT_DIR/watchman.conf" "$CONFIG_FILE"
    echo "  Config installed to $CONFIG_FILE"
else
    echo "  Config already exists at $CONFIG_FILE (not overwriting)"
fi

echo "[OK] Files installed"
echo ""

# ── Step 4: Create virtual disk ─────────────────────────────────────────────

echo "[4/7] Setting up GhostDrive..."

# Source config values
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

if [ -f "$CONTAINER" ]; then
    echo "  Container already exists: $CONTAINER (skipping)"
else
    bash "$SCRIPT_DIR/create_disk.sh" "$CONFIG_FILE"
fi

echo "[OK] GhostDrive ready"
echo ""

# ── Step 5: Create directories ──────────────────────────────────────────────

echo "[5/7] Creating directories..."

ARCHIVE_DIR="/home/watchman/archive"
MOUNT_POINT="/mnt/ghostdrive"
if [ -f "$CONFIG_FILE" ]; then
    while IFS='=' read -r key value; do
        key=$(echo "$key" | tr -d '\r' | xargs)
        value=$(echo "$value" | tr -d '\r' | xargs)
        case "$key" in
            ARCHIVE_DIR) ARCHIVE_DIR="$value" ;;
            MOUNT_POINT) MOUNT_POINT="$value" ;;
        esac
    done < <(tr -d '\r' < "$CONFIG_FILE" | grep -v '^\s*#' | grep '=')
fi

mkdir -p "$ARCHIVE_DIR" "$MOUNT_POINT"

if id -u watchman >/dev/null 2>&1; then
    chown watchman:watchman "$ARCHIVE_DIR"
fi

echo "[OK] Directories created"
echo ""

# ── Step 6: Install systemd services ────────────────────────────────────────

echo "[6/7] Installing systemd services..."

cp "$SCRIPT_DIR/watchman.service" /etc/systemd/system/
cp "$SCRIPT_DIR/watchman-web.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable watchman.service watchman-web.service

echo "[OK] Services installed and enabled"
echo ""

# ── Step 7: Configure hardware watchdog ─────────────────────────────────────

echo "[7/7] Configuring hardware watchdog..."

# Enable the hardware watchdog timer in boot config
if ! grep -q "dtparam=watchdog=on" "$BOOT_DIR/config.txt"; then
    sed -i '/^\[all\]/a dtparam=watchdog=on' "$BOOT_DIR/config.txt"
    echo "  Added dtparam=watchdog=on to config.txt"
else
    echo "  Hardware watchdog already enabled in config.txt"
fi

# Write watchdog config — reboot if system is unresponsive for 60 seconds
cat > /etc/watchdog.conf << 'WATCHDOG_EOF'
watchdog-device = /dev/watchdog
max-load-1 = 24
watchdog-timeout = 60
WATCHDOG_EOF

systemctl enable watchdog
systemctl start watchdog

echo "[OK] Hardware watchdog configured (60s timeout)"
echo ""

# ── Done ────────────────────────────────────────────────────────────────────

echo "=== Setup Complete ==="
echo ""
echo "  Config:    $CONFIG_FILE"
echo "  Container: $CONTAINER"
echo "  Archive:   $ARCHIVE_DIR"
echo "  Web UI:    http://<pi-ip>:5000"
echo ""
echo "  REBOOT REQUIRED to activate USB gadget mode."
echo "  After reboot, both services start automatically."
echo ""
echo "  sudo reboot"
