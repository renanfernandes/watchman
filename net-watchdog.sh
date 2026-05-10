#!/usr/bin/env bash
# net-watchdog.sh — Reboots the Pi if internet connectivity is lost for too long.
#
# Reads settings from /etc/watchman/watchman.conf.
# Set NET_WATCHDOG_ENABLED=no to disable without removing the service.

set -euo pipefail

CONFIG="/etc/watchman/watchman.conf"

# ── Defaults ─────────────────────────────────────────────────────────────────

NET_WATCHDOG_ENABLED="yes"
NET_WATCHDOG_HOST="8.8.8.8"
NET_WATCHDOG_TIMEOUT="300"   # seconds without connectivity before rebooting
NET_WATCHDOG_INTERVAL="30"   # seconds between each connectivity check

# ── Load config ───────────────────────────────────────────────────────────────

if [ -f "$CONFIG" ]; then
    while IFS='=' read -r key value; do
        key=$(echo "$key" | tr -d '\r' | xargs)
        value=$(echo "$value" | tr -d '\r' | xargs)
        case "$key" in
            NET_WATCHDOG_ENABLED)  NET_WATCHDOG_ENABLED="$value" ;;
            NET_WATCHDOG_HOST)     NET_WATCHDOG_HOST="$value" ;;
            NET_WATCHDOG_TIMEOUT)  NET_WATCHDOG_TIMEOUT="$value" ;;
            NET_WATCHDOG_INTERVAL) NET_WATCHDOG_INTERVAL="$value" ;;
        esac
    done < <(grep -v '^\s*#' "$CONFIG" | grep '=')
fi

if [ "$NET_WATCHDOG_ENABLED" != "yes" ]; then
    echo "Net watchdog disabled via config. Exiting."
    exit 0
fi

echo "Net watchdog started."
echo "  Host:      $NET_WATCHDOG_HOST"
echo "  Timeout:   ${NET_WATCHDOG_TIMEOUT}s"
echo "  Interval:  ${NET_WATCHDOG_INTERVAL}s"

# ── Main loop ─────────────────────────────────────────────────────────────────

offline_seconds=0

while true; do
    if ping -c 1 -W 5 "$NET_WATCHDOG_HOST" > /dev/null 2>&1; then
        if [ "$offline_seconds" -gt 0 ]; then
            echo "Connectivity restored after ${offline_seconds}s offline."
        fi
        offline_seconds=0
    else
        offline_seconds=$((offline_seconds + NET_WATCHDOG_INTERVAL))
        echo "No connectivity for ${offline_seconds}s (reboot threshold: ${NET_WATCHDOG_TIMEOUT}s)"

        if [ "$offline_seconds" -ge "$NET_WATCHDOG_TIMEOUT" ]; then
            echo "Connectivity lost for ${offline_seconds}s — rebooting now."
            /sbin/reboot
        fi
    fi

    sleep "$NET_WATCHDOG_INTERVAL"
done
