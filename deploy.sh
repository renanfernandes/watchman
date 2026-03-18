#!/usr/bin/env bash
# Deploy Watchman to the Pi.
# Usage: bash deploy.sh [pi_host]
#   e.g. bash deploy.sh watchman@10.0.1.244

set -euo pipefail

HOST="${1:-watchman@10.0.1.244}"

echo "=== Deploying to $HOST ==="

echo "[1/3] Syncing files..."
rsync -avz --exclude='.git' ./ "$HOST:~/watchman/"

echo ""
echo "[2/3] Installing + restarting services..."
ssh "$HOST" 'sudo cp ~/watchman/watchman.py ~/watchman/web.py /opt/watchman/ && sudo cp -r ~/watchman/templates /opt/watchman/ && sudo cp ~/watchman/watchman.service ~/watchman/watchman-web.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart watchman watchman-web'

echo ""
echo "[3/3] Checking services..."
ssh "$HOST" 'systemctl is-active watchman watchman-web'

echo ""
echo "=== Deploy complete ==="
