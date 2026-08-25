#!/usr/bin/env bash
# Watchman Setup
# Configures a Raspberry Pi Zero 2 W as a virtual USB drive for Blink Sync Module 2.
#
# What this does:
#   1. Installs required packages (exfatprogs, python3-flask, watchdog)
#   2. Disables WiFi power save via NetworkManager (prevents silent WiFi drops)
#   3. Configures USB gadget mode in boot files (config.txt + cmdline.txt)
#   4. Creates the GhostDrive virtual disk (6GB exFAT)
#   5. Installs Watchman service files
#   6. Enables auto-start on boot
#   7. Configures hardware watchdog (auto-reboot on system hang)
#   8. Installs network watchdog (auto-reboot on prolonged connectivity loss)
#   9. Schedules a monthly reboot (1st of the month at 03:00)
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

# The user who ran `sudo` — used for ARCHIVE_DIR, file ownership, and the
# web service's User= directive, so this works out of the box no matter
# what your actual login is (not just a user literally named "watchman").
# Falls back to "watchman" only if run as a true root login with no
# SUDO_USER set (e.g. logged in directly as root).
SERVICE_USER="${SUDO_USER:-watchman}"
TOTAL_STEPS=10
CURRENT_STEP=0

# Color helpers for clearer setup output.
if [ -t 1 ] && [ -n "${TERM:-}" ] && [ "$TERM" != "dumb" ]; then
    COLOR_GREEN=$'\033[32m'
    COLOR_YELLOW=$'\033[33m'
    COLOR_BLUE=$'\033[34m'
    COLOR_RESET=$'\033[0m'
else
    COLOR_GREEN=""
    COLOR_YELLOW=""
    COLOR_BLUE=""
    COLOR_RESET=""
fi

step_header() {
    CURRENT_STEP=$((CURRENT_STEP + 1))
    local title="$1"
    local width=24
    local filled=$(( CURRENT_STEP * width / TOTAL_STEPS ))
    local percent=$(( CURRENT_STEP * 100 / TOTAL_STEPS ))
    local bar=""
    local remainder=""

    if [ "$filled" -gt 0 ]; then
        bar=$(printf '%*s' "$filled" '' | tr ' ' '#')
    fi
    if [ "$filled" -lt "$width" ]; then
        remainder=$(printf '%*s' "$((width - filled))" '' | tr ' ' '─')
    fi

    printf '\n%s[%s%s] %3d%% %d/%d %s%s\n' "$COLOR_BLUE" "$bar" "$remainder" "$percent" "$CURRENT_STEP" "$TOTAL_STEPS" "$title" "$COLOR_RESET"
}

step_ok() {
    printf '%b\n' "${COLOR_GREEN}[OK]${COLOR_RESET} $1"
}

step_warn() {
    printf '%b\n' "${COLOR_YELLOW}[WARN]${COLOR_RESET} $1"
}

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
echo "Service user: $SERVICE_USER"
echo ""
step_header "Preparing installation"

# Create the service user on first-time installs only. This is conservative:
# if the user already exists we leave it alone, and if the requested name is
# invalid we fail safely rather than guessing.
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    echo "[0/9] Creating service user '$SERVICE_USER'..."
    if getent passwd "$SERVICE_USER" >/dev/null 2>&1; then
        echo "  User '$SERVICE_USER' already exists; continuing"
    else
        useradd --create-home --shell /bin/bash "$SERVICE_USER"
        echo "  Created user '$SERVICE_USER'"
    fi
    step_ok "Service user ready"
    echo ""
fi

# ── Step 1: Install dependencies ────────────────────────────────────────────

step_header "Installing dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-flask exfatprogs fdisk util-linux watchdog ffmpeg parted
# Enable persistent journal so logs survive reboots
mkdir -p /var/log/journal
chown root:systemd-journal /var/log/journal
chmod 2755 /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal
systemctl restart systemd-journald
step_ok "Dependencies installed"
echo ""

# ── Step 2: Disable WiFi power save ─────────────────────────────────────────────

step_header "Disabling WiFi power save..."
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/wifi-powersave-off.conf << 'NM_EOF'
[connection]
wifi.powersave = 2
NM_EOF
step_ok "WiFi power save disabled via NetworkManager"
echo ""

# ── Step 3: Configure boot for USB gadget mode ───────────────────────────

step_header "Configuring boot files for USB gadget mode..."
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

step_ok "Boot configured"
echo ""

# ── Step 4: Install Watchman files ──────────────────────────────────────────

step_header "Installing Watchman files..."
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR"

cp "$SCRIPT_DIR/watchman.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/web.py" "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/templates" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/scripts/net-watchdog.sh" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/scripts/watchman-startup.sh" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/watchman.py" "$INSTALL_DIR/web.py" "$INSTALL_DIR/net-watchdog.sh" "$INSTALL_DIR/watchman-startup.sh"

# Only install config if it doesn't already exist (don't overwrite user edits)
FRESH_CONFIG_INSTALL=0
if [ ! -f "$CONFIG_FILE" ]; then
    FRESH_CONFIG_INSTALL=1
    if [ -f "$SCRIPT_DIR/watchman.conf" ]; then
        cp "$SCRIPT_DIR/watchman.conf" "$CONFIG_FILE"
    elif [ -f "$SCRIPT_DIR/watchman.conf.example" ]; then
        cp "$SCRIPT_DIR/watchman.conf.example" "$CONFIG_FILE"
        echo "  WARNING: No watchman.conf found — installed from watchman.conf.example"
        echo "  Review $CONFIG_FILE and set your Pushover credentials before use."
    else
        echo "[ERROR] No config file found. Expected $SCRIPT_DIR/watchman.conf or watchman.conf.example"
        exit 1
    fi
    echo "  Config installed to $CONFIG_FILE"
else
    echo "  Config already exists at $CONFIG_FILE (not overwriting)"
fi

step_ok "Files installed"
echo ""

# On a fresh install only, point ARCHIVE_DIR at the detected user's actual
# home directory instead of the template's literal /home/watchman/archive.
# Only rewrites it if it's still the untouched template default — never
# touches a value you've already customized on a re-run.
if [ "$FRESH_CONFIG_INSTALL" -eq 1 ] && [ "$SERVICE_USER" != "watchman" ] \
   && grep -q "^ARCHIVE_DIR=/home/watchman/archive$" "$CONFIG_FILE"; then
    sed -i "s|^ARCHIVE_DIR=/home/watchman/archive$|ARCHIVE_DIR=/home/$SERVICE_USER/archive|" "$CONFIG_FILE"
    echo "  Set ARCHIVE_DIR=/home/$SERVICE_USER/archive in config"
fi

# Same deal for SERVICE_USER itself — the template ships with the key
# already present (SERVICE_USER=watchman), so the "add if missing" check
# below would never fire on a fresh install. Rewrite it directly instead.
if [ "$FRESH_CONFIG_INSTALL" -eq 1 ] && [ "$SERVICE_USER" != "watchman" ] \
   && grep -q "^SERVICE_USER=watchman$" "$CONFIG_FILE"; then
    sed -i "s|^SERVICE_USER=watchman$|SERVICE_USER=$SERVICE_USER|" "$CONFIG_FILE"
    echo "  Set SERVICE_USER=$SERVICE_USER in config"
fi

# Fallback for hand-made/older configs that don't have the key at all
if [ -f "$CONFIG_FILE" ] && ! grep -q "^SERVICE_USER=" "$CONFIG_FILE"; then
    {
        echo ""
        echo "# System user the web service runs as and archived files are"
        echo "# chowned to. Auto-detected from whoever ran setup.sh via sudo."
        echo "SERVICE_USER=$SERVICE_USER"
    } >> "$CONFIG_FILE"
    echo "  Added SERVICE_USER=$SERVICE_USER to config"
fi
echo ""

# ── Step 5: Create virtual disk ─────────────────────────────────────────────

step_header "Setting up GhostDrive..."

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
    bash "$SCRIPT_DIR/scripts/create_disk.sh" "$CONFIG_FILE"
fi

step_ok "GhostDrive ready"
echo ""

# ── Step 6: Create directories ──────────────────────────────────────────────

step_header "Creating directories..."

ARCHIVE_DIR="/home/$SERVICE_USER/archive"
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

mkdir -p "$ARCHIVE_DIR" "$MOUNT_POINT" /var/log/watchman "$ARCHIVE_DIR/.thumbnails"

if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    chown -R "$SERVICE_USER:$SERVICE_USER" "$ARCHIVE_DIR"
else
    echo "  WARNING: user '$SERVICE_USER' not found — leaving $ARCHIVE_DIR ownership as-is"
fi

step_ok "Directories created"
echo ""

# Backfill thumbnails and duration metadata for any clips already in the
# archive (e.g. upgrading an existing install to a version with this
# support). New clips get both automatically going forward via
# archive_video() in watchman.py.
echo "Checking for archived clips missing thumbnails/duration..."
BACKFILL_COUNT=0
while IFS= read -r -d '' video; do
    date_dir="$(basename "$(dirname "$video")")"
    stem="$(basename "${video%.mp4}")"
    thumb="$ARCHIVE_DIR/.thumbnails/$date_dir/$stem.jpg"
    meta="$ARCHIVE_DIR/.thumbnails/$date_dir/$stem.json"
    changed=0
    if [ ! -f "$thumb" ]; then
        mkdir -p "$(dirname "$thumb")"
        if ffmpeg -y -ss 1 -i "$video" -frames:v 1 -update 1 -vf scale=320:-1 "$thumb" \
            > /dev/null 2>&1; then
            changed=1
        fi
    fi
    if [ ! -f "$meta" ]; then
        mkdir -p "$(dirname "$meta")"
        duration=$(ffprobe -v error -show_entries format=duration -of \
            default=noprint_wrappers=1:nokey=1 "$video" 2>/dev/null)
        if [ -n "$duration" ]; then
            printf '{"duration": %s}' "$duration" > "$meta"
            changed=1
        fi
    fi
    [ "$changed" -eq 1 ] && BACKFILL_COUNT=$((BACKFILL_COUNT + 1))
done < <(find "$ARCHIVE_DIR" -mindepth 2 -maxdepth 2 -name "*.mp4" -not -path "*/.thumbnails/*" -print0 2>/dev/null)

if [ "$BACKFILL_COUNT" -gt 0 ]; then
    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        chown -R "$SERVICE_USER:$SERVICE_USER" "$ARCHIVE_DIR/.thumbnails"
    fi
    echo "  Updated $BACKFILL_COUNT existing clip(s) with thumbnail/duration data"
else
    echo "  No backfill needed"
fi
echo ""

# ── Step 7: Install systemd services ────────────────────────────────────────

step_header "Installing systemd services..."

cp "$SCRIPT_DIR/services/watchman.service" /etc/systemd/system/
# watchman-web.service runs as a non-root user for least-privilege — swap
# in whoever setup.sh detected instead of the repo's placeholder "watchman"
# user, which won't exist on most systems and would fail to start.
sed "s/^User=.*/User=$SERVICE_USER/" "$SCRIPT_DIR/services/watchman-web.service" \
    > /etc/systemd/system/watchman-web.service
cp "$SCRIPT_DIR/services/watchman-startup.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable watchman.service watchman-web.service watchman-startup.service

# Let SERVICE_USER restart (only) Watchman's own services without a
# password — this is what powers the "Restart" buttons on the web UI's
# Settings page. Scoped to exact commands only, nothing broader.
SUDOERS_FILE="/etc/sudoers.d/watchman-restart"
cat > "$SUDOERS_FILE" <<EOF
$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart watchman.service
$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart watchman-web.service
$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart watchman-net.service
$SERVICE_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart watchman-startup.service
EOF
chmod 440 "$SUDOERS_FILE"
if ! visudo -c -f "$SUDOERS_FILE" > /dev/null 2>&1; then
    echo "  WARNING: generated sudoers file failed validation — removing it."
    echo "  Web UI restart buttons will not work until this is fixed manually."
    rm -f "$SUDOERS_FILE"
fi

step_ok "Services installed and enabled"
echo ""

# ── Step 8: Configure hardware watchdog ─────────────────────────────────────

step_header "Configuring hardware watchdog..."

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

step_ok "Hardware watchdog configured (60s timeout)"
echo ""

# ── Step 9: Install network watchdog ────────────────────────────────────────

step_header "Installing network watchdog..."

# Read NET_WATCHDOG_ENABLED from config
NET_WATCHDOG_ENABLED="yes"
if [ -f "$CONFIG_FILE" ]; then
    while IFS='=' read -r key value; do
        key=$(echo "$key" | tr -d '\r' | xargs)
        value=$(echo "$value" | tr -d '\r' | xargs)
        [ "$key" = "NET_WATCHDOG_ENABLED" ] && NET_WATCHDOG_ENABLED="$value"
    done < <(grep -v '^\s*#' "$CONFIG_FILE" | grep '=')
fi

cp "$SCRIPT_DIR/scripts/net-watchdog.sh" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/net-watchdog.sh"
cp "$SCRIPT_DIR/services/watchman-net.service" /etc/systemd/system/
systemctl daemon-reload

if [ "$NET_WATCHDOG_ENABLED" = "yes" ]; then
    systemctl enable --now watchman-net.service
    step_ok "Network watchdog enabled and started"
else
    systemctl disable watchman-net.service 2>/dev/null || true
    step_ok "Network watchdog installed but disabled (NET_WATCHDOG_ENABLED=no)"
fi
echo ""

# ── Step 9: Schedule monthly reboot ─────────────────────────────────────────

step_header "Scheduling monthly reboot..."

CRON_JOB="0 3 1 * * /sbin/reboot"
CRON_MARKER="# Watchman: monthly reboot"

# Install into root's crontab if not already present
if crontab -l 2>/dev/null | grep -qF "$CRON_MARKER"; then
    echo "  Monthly reboot already scheduled"
else
    ( crontab -l 2>/dev/null; echo ""; echo "$CRON_MARKER"; echo "$CRON_JOB" ) | crontab -
    echo "  Added: $CRON_JOB (1st of every month at 03:00)"
fi
echo ""

# ── Done ────────────────────────────────────────────────────────────────────

echo ""
echo "${COLOR_GREEN}=== Setup Complete ===${COLOR_RESET}"
echo ""
echo "  Config:    $CONFIG_FILE"
echo "  Container: $CONTAINER"
echo "  Archive:   $ARCHIVE_DIR"
echo "  Web UI:    http://<pi-ip>:5000"
echo ""
echo "  Next steps:"
echo "    1. Reboot the Pi to activate USB gadget mode"
echo "    2. Check the services with: sudo systemctl status watchman watchman-web"
echo "    3. Open the web UI once the Pi is back online"
echo ""
