# Operations

[Back to README](../README.md) | [Installation](installation.md) | [Storage](storage.md)

## Services and Logs

| Unit | Role |
| --- | --- |
| `watchman` | Root-run USB gadget and ingest service |
| `watchman-web` | Non-root Flask web service |
| `watchman-net` | Connectivity watchdog |
| `watchman-startup` | Boot diagnostics and notification |

**On the Pi:**

```bash
systemctl status watchman watchman-web watchman-net watchman-startup --no-pager
sudo journalctl -u watchman -n 100 --no-pager
sudo journalctl -u watchman-web -n 100 --no-pager
sudo journalctl -u watchman-net -n 100 --no-pager
```

Use `-f` with journalctl to follow a log. A successful one-shot startup service can be inactive after finishing; inspect its result rather than treating every inactive state as a failure. Logs and connectivity diagnostics may contain private details; redact before posting an issue. Do not publish full configuration files.

## Updates

The personal deployment script is not tracked on GitHub. The following is a code-only update for an **existing installation**, run on the Pi. It preserves installed configuration, service units, and service-user selection, and avoids rerunning system-wide setup changes. Read release notes first: updates that require new packages, units or migrations need additional steps.

First back up recordings separately using the [storage guide](storage.md#backups-and-restore). Keep a second SSH session available. In the repository:

```bash
cd "$HOME/watchman"
git status --short
git rev-parse HEAD
```

If there are local changes, resolve them deliberately before updating; do not reset or discard them. Record the current commit and create a private application/config snapshot:

```bash
umask 077
BACKUP_DIR="$HOME/watchman-backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
git rev-parse HEAD > "$BACKUP_DIR/source-commit.txt" &&
sudo systemctl stop watchman watchman-web &&
sudo tar -czf "$BACKUP_DIR/application-config.tgz" -C / opt/watchman etc/watchman &&
sudo chmod 600 "$BACKUP_DIR/application-config.tgz" &&
sudo tar -tzf "$BACKUP_DIR/application-config.tgz" > /dev/null
```

Proceed only if the backup succeeds. If any subsequent command fails, stop and inspect the error; do not assume the update completed. Restart the existing services or use rollback as appropriate.

```bash
git pull --ff-only &&
python3 -m py_compile watchman.py web.py &&
sudo install -m 755 watchman.py web.py scripts/net-watchdog.sh scripts/watchman-startup.sh /opt/watchman/ &&
sudo cp -R templates/. /opt/watchman/templates/ &&
sudo systemctl start watchman watchman-web &&
if systemctl is-enabled --quiet watchman-net; then
 sudo systemctl restart watchman-net
fi
```

Restart the network watchdog only if it is enabled for this installation. This procedure intentionally leaves `/etc/watchman/watchman.conf` untouched. Review new keys in the public example and add only the values needed to the installed config. It also leaves systemd units unchanged; compare relevant source unit changes before adopting them.

### Validate the Update

```bash
systemctl is-active watchman watchman-web
systemctl show watchman-web --property=User --value
sudo journalctl -u watchman-web -n 50 --no-pager
curl --fail --max-time 30 -o /dev/null http://127.0.0.1:5000/starred
```

Use the configured port. In the browser, check playback, archived playback, stars after a reload, and settings persistence. Opening clip pages can trigger retention; review the policy before testing. A successful HTTP response alone does not verify these workflows.

### Rollback

Set `BACKUP_DIR` to the actual snapshot directory, not a new timestamp. Inspect the archive listing and confirm it contains only the intended application/config paths. This restores those paths and overwrites changes made there since the snapshot; it does not undo Git history, package changes, boot changes, or video deletion.

```bash
sudo tar -tzf "${BACKUP_DIR:?Set BACKUP_DIR to the verified snapshot}/application-config.tgz"
```

After confirming the listing:

```bash
sudo systemctl stop watchman watchman-web &&
sudo tar -xzf "${BACKUP_DIR:?Set BACKUP_DIR to the verified snapshot}/application-config.tgz" -C / &&
sudo systemctl start watchman watchman-web
```

Restart the network watchdog if enabled, then repeat validation. Extraction does not remove files introduced by the failed update; inspect unexpected files separately. Keep the recorded commit for a deliberate later source update rather than using a destructive Git reset.

## Watchdogs and Reboots

The network watchdog defaults to pinging a public internet address. An internet outage can therefore reboot a Pi whose LAN is healthy. To disable it, set `NET_WATCHDOG_ENABLED=no` in the installed config and run:

```bash
sudo systemctl disable --now watchman-net
```

The hardware watchdog is separate (`watchdog.service` and `/etc/watchdog.conf`). Setup also adds a monthly root cron reboot. Inspect it with `sudo crontab -l`; to change it, use `sudo crontab -e` and edit only Watchman's marked entry, preserving unrelated jobs. Setup can re-add its entry on a later run.

## Troubleshooting

### Drive Not Recognized or Mount Failure

Check the USB data port/cable, power, boot overlay and logs first:

```bash
lsmod | grep -E 'dwc2|g_mass_storage'
sudo journalctl -u watchman -n 100 --no-pager
ls -lh /ghostdrive.bin
sudo losetup -l
```

Use the configured image path if different. The image has a partition table: mounting the whole image with a plain loop mount is not a valid general exFAT test. Ingest handles partition mapping and retries discovery with `partprobe`/`udevadm`.

> [!CAUTION]
> Never mount the image locally while it is exposed to Blink, and never run a second ingest process alongside the service. `--no-gadget` is not a dry-run flag: it skips disconnecting Blink but still mounts and moves recordings. Recreating the image destroys recordings still inside it. Preserve a backup and establish exclusive access before any filesystem repair or recreation.

### Archived Date Returns 404

Check the installed `RETENTION_ARCHIVE_DIR`, the actual date directory and MP4 files, mount availability, and service-user permissions. A yellow history dot can refer to deleted clips, not a surviving archive. Do not append duplicate config entries or assume a workstation config is active on the Pi.

### Settings or Stars Do Not Persist

Check the installed web service user, config permissions, and active-root permissions. Config requires write access; the active root must allow metadata files to be created/updated. Follow [installation permissions](installation.md#install-on-the-pi). The current star endpoint can report success despite a failed save; reload and inspect logs to verify.

### Restart Button Fails

Inspect web logs and `/etc/sudoers.d/watchman-restart`. Setup installs only the four exact Watchman restart commands for its chosen service user. Use `sudo visudo -f /etc/sudoers.d/watchman-restart` to repair that scoped rule; do not grant unrestricted passwordless sudo.

### Slow Pages or Unexpected Resets

Keep `VIDEO_PRELOAD=none`, check archive-mount latency, and inspect power and watchdog logs. A dashboard recursively scans storage; a slow share can delay it. Power failures are not guaranteed to be fixed by reducing browser requests. Clip-time conversion depends on the Pi's configured timezone; inspect `timedatectl` if display times are unexpected.

## Documentation Checks

**On a workstation or the Pi**, with Node.js 22 and npm available, from the repository root:

```bash
npm ci --prefix .github --ignore-scripts
npm --prefix .github run check
```

The first command installs lockfile-pinned documentation tools under `.github/node_modules`, not application dependencies on the Pi. GitHub Actions runs both checks on pushes and pull requests, including code changes that might remove linked files. Local-link checks cover repository paths and Markdown heading anchors; external website availability is not tested. These checks do not test Raspberry Pi hardware or application behavior.
