#!/usr/bin/env bash
# Run the full first-time Watchman setup only when the system does not
# already appear to have a prior installation.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "[ERROR] Run as root: sudo bash $(basename "$0")"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f /opt/watchman/watchman.py ] || [ -f /etc/systemd/system/watchman.service ]; then
    echo "Watchman already appears installed — skipping first-time setup."
    exit 0
fi

echo "=== First-time Watchman install ==="
exec bash "$REPO_ROOT/setup.sh"
