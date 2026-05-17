#!/usr/bin/env bash
# Deploy Watchman to the Pi.
# Usage: bash deploy.sh [pi_host]
#   e.g. bash deploy.sh watchman@10.0.1.244

set -euo pipefail

HOST="${1:-watchman@10.2.0.5}"

RSYNC_SSH="ssh"

# Warn if Pushover keys are still placeholders
if grep -q "your_app_token_here\|your_user_key_here" watchman.conf 2>/dev/null; then
    echo "[WARN] watchman.conf still has placeholder Pushover keys — notifications won't work"
    echo "       Edit watchman.conf and set PUSHOVER_TOKEN and PUSHOVER_USER before deploying"
    echo ""
fi

echo "=== Deploying to $HOST ==="

echo "[1/3] Syncing files..."
rsync -avz -e "$RSYNC_SSH" --exclude='.git' --exclude='logs/' --exclude='.troubleshoot_done' ./ "$HOST:~/watchman/"

echo ""
echo "[2/3] Installing + restarting services..."
ssh "$HOST" 'sudo cp ~/watchman/watchman.py ~/watchman/web.py ~/watchman/net-watchdog.sh ~/watchman/watchman-startup.sh /opt/watchman/ \
  && sudo chmod +x /opt/watchman/net-watchdog.sh /opt/watchman/watchman-startup.sh \
  && sudo cp -r ~/watchman/templates /opt/watchman/ \
  && sudo cp ~/watchman/watchman.conf /etc/watchman/watchman.conf \
  && sudo cp ~/watchman/watchman.service ~/watchman/watchman-web.service ~/watchman/watchman-net.service ~/watchman/watchman-startup.service /etc/systemd/system/ \
  && sudo systemctl daemon-reload \
  && sudo systemctl enable watchman-startup.service watchman-net.service \
  && sudo systemctl restart watchman watchman-web \
  && sudo systemctl restart watchman-net 2>/dev/null || true'

echo ""
echo "[3/3] Checking services..."
ssh "$HOST" 'systemctl is-active watchman watchman-web watchman-net watchman-startup || true'

echo ""
echo "=== Deploy complete ==="
