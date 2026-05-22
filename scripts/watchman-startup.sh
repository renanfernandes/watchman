#!/usr/bin/env bash
# watchman-startup.sh — Runs at every boot.
#
# 1. Verifies and reapplies all reliability fixes (WiFi power save, services)
# 2. Collects diagnostic info from the previous boot
# 3. Sends a Pushover notification with a full status report

set -euo pipefail

CONFIG="/etc/watchman/watchman.conf"
HOSTNAME=$(hostname)

# ── Load config ───────────────────────────────────────────────────────────────

NOTIFY_ENABLED="yes"
PUSHOVER_TOKEN=""
PUSHOVER_USER=""

if [ -f "$CONFIG" ]; then
    while IFS='=' read -r key value; do
        key=$(echo "$key" | tr -d '\r' | xargs)
        value=$(echo "$value" | tr -d '\r' | xargs)
        case "$key" in
            NOTIFY_ENABLED)  NOTIFY_ENABLED="$value" ;;
            PUSHOVER_TOKEN)  PUSHOVER_TOKEN="$value" ;;
            PUSHOVER_USER)   PUSHOVER_USER="$value" ;;
        esac
    done < <(grep -v '^\s*#' "$CONFIG" | grep '=')
fi

# ── Pushover helper ───────────────────────────────────────────────────────────

notify() {
    local title="$1"
    local message="$2"
    local priority="${3:-0}"

    if [ "$NOTIFY_ENABLED" != "yes" ] || [ -z "$PUSHOVER_TOKEN" ] || [ -z "$PUSHOVER_USER" ]; then
        echo "[notify] Skipped (disabled or keys missing)"
        return 0
    fi

    curl -s \
        --form-string "token=${PUSHOVER_TOKEN}" \
        --form-string "user=${PUSHOVER_USER}" \
        --form-string "title=${title}" \
        --form-string "message=${message}" \
        --form-string "priority=${priority}" \
        https://api.pushover.net/1/messages.json > /dev/null 2>&1 || true
}

# ── Step 1: Verify + reapply WiFi power save fix ──────────────────────────────

WIFI_STATUS="unknown"
NM_CONF="/etc/NetworkManager/conf.d/wifi-powersave-off.conf"

if [ ! -f "$NM_CONF" ]; then
    echo "[startup] NM power save config missing — reapplying..."
    mkdir -p /etc/NetworkManager/conf.d
    cat > "$NM_CONF" << 'EOF'
[connection]
wifi.powersave = 2
EOF
    systemctl restart NetworkManager
    WIFI_STATUS="reapplied"
else
    WIFI_STATUS="ok"
fi

# Check kernel-level result (after NM has started)
sleep 5
if journalctl -b --no-pager 2>/dev/null | grep -q "brcmf_cfg80211_set_power_mgmt: power save disabled"; then
    WIFI_STATUS="disabled (confirmed)"
elif journalctl -b --no-pager 2>/dev/null | grep -q "brcmf_cfg80211_set_power_mgmt: power save enabled"; then
    WIFI_STATUS="WARNING: still enabled"
fi

echo "[startup] WiFi power save: $WIFI_STATUS"

# ── Step 2: Verify all services are running ───────────────────────────────────

SERVICES_STATUS=""
for svc in watchman watchman-web watchman-net; do
    state=$(systemctl is-active "$svc" 2>/dev/null || echo "inactive")
    SERVICES_STATUS="${SERVICES_STATUS}${svc}: ${state}\n"
    if [ "$state" != "active" ]; then
        echo "[startup] $svc not active — attempting restart..."
        systemctl restart "$svc" 2>/dev/null || true
    fi
done

echo -e "[startup] Services:\n$SERVICES_STATUS"

# ── Step 3: Collect previous boot diagnostics ─────────────────────────────────

PREV_BOOT_SUMMARY="(no persistent journal)"
if journalctl -b -1 --no-pager 2>/dev/null | grep -q "kernel"; then
    # Get last WiFi events + errors from previous boot
    WIFI_EVENTS=$(journalctl -b -1 --no-pager 2>/dev/null \
        | grep -iE "power_mgmt|wlan|disconn|brcmf" \
        | tail -10 || true)
    ERRORS=$(journalctl -b -1 --no-pager -p err 2>/dev/null \
        | tail -10 || true)
    PREV_BOOT_SUMMARY="WiFi events:\n${WIFI_EVENTS}\n\nErrors:\n${ERRORS}"
fi

# ── Step 4: Build and send notification ───────────────────────────────────────

BOOT_TIME=$(uptime -s 2>/dev/null || date)
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "unknown")

MESSAGE="Boot: ${BOOT_TIME}
IP: ${IP}

WiFi power save: ${WIFI_STATUS}

Services:
$(echo -e "$SERVICES_STATUS")
Prev boot log:
$(echo -e "$PREV_BOOT_SUMMARY" | head -20)"

notify "Watchman Online — ${HOSTNAME}" "$MESSAGE" 0

echo "[startup] Done. Notification sent."
