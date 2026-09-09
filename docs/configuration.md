# Configuration

[Back to README](../README.md) | [Installation](installation.md) | [Storage](storage.md)

## Configuration Source

The installed services explicitly read `/etc/watchman/watchman.conf`. Editing a workstation copy does not change the running Pi. Settings are plain `KEY=value` lines, not YAML or a shell script: avoid shell expansion, inline comments, and wrapping values in quotes. Keep one entry per key.

The table below describes values shipped in [watchman.conf.example](../watchman.conf.example), not every Python or shell fallback. Existing installations retain their own values. In particular, ingest fallback timings differ from the example, and the web fallback for retention days is 90 rather than the example's 30.

| Key | Example Value | Meaning |
| --- | --- | --- |
| `CONTAINER` | `/ghostdrive.bin` | Virtual disk image |
| `CONTAINER_SIZE_MB` | `6144` | Image size used during creation, not a live resize setting |
| `MOUNT_POINT` | `/mnt/ghostdrive` | Temporary local ingest mount |
| `ARCHIVE_DIR` | `/home/watchman/archive` | Active clips and persistent metadata; setup adjusts the default for its service user |
| `SERVICE_USER` | `watchman` | Clip ownership user; setup selects the web unit user from its invoking account |
| `GADGET_MODULE` | `g_mass_storage` | USB mass-storage module |
| `CONNECTIVITY_LOG` | `/var/log/watchman/connectivity.jsonl` | Ingest connectivity diagnostics |
| `SETTLE_TIME` | `120` | Seconds without writes before ingest |
| `MIN_INTERVAL` | `1800` | Minimum seconds between ingest cycles |
| `RECONNECT_COOLDOWN` | `120` | Ignore write activity briefly after reconnecting |
| `WATCHDOG_THRESHOLD` | `3` | Ingest failures before a full USB reset |
| `WEB_HOST` | `0.0.0.0` | Web bind address; all interfaces by default |
| `WEB_PORT` | `5000` | Web port |
| `RETENTION_MODE` | `off` | `off`, `archive`, or `delete` |
| `RETENTION_DAYS` | `30` | Only recording dates strictly older than this threshold qualify |
| `RETENTION_ARCHIVE_DIR` | Empty | Destination root for retained copies; also used for browsing those copies |
| `RETENTION_RUN_INTERVAL` | `3600` | Minimum seconds between request-triggered cleanup runs |
| `NEXTCLOUD_ENABLED` | `no` | Upload and verify before retention deletion |
| `NEXTCLOUD_REMOTE` | Empty | rclone remote/path, such as `nextcloud:Watchman` |
| `NEXTCLOUD_RCLONE_PATH` | `rclone` | rclone executable |
| `NET_WATCHDOG_ENABLED` | `yes` | Enable internet-connectivity watchdog |
| `NET_WATCHDOG_HOST` | `8.8.8.8` | Ping target |
| `NET_WATCHDOG_TIMEOUT` | `600` | Offline threshold in seconds; detection also depends on the check interval |
| `NET_WATCHDOG_INTERVAL` | `300` | Seconds between checks |
| `NOTIFY_ENABLED` | `yes` | Enable Pushover; requires valid credentials |
| `PUSHOVER_TOKEN` | Placeholder | Pushover application token |
| `PUSHOVER_USER` | Placeholder | Pushover user key |
| `VIDEO_PRELOAD` | `none` | `none`, `metadata`, or `auto` |

## Editing and Restarts

Use `/settings` for supported fields, or edit the installed config on the Pi:

```bash
sudo nano /etc/watchman/watchman.conf
```

Most web settings are reread per request. Restart the web service for startup settings such as `ARCHIVE_DIR`, `WEB_HOST`, and `WEB_PORT`; ingest timing changes require restarting ingest. Restart the network watchdog after changing its configuration.

```bash
sudo systemctl restart watchman watchman-web
sudo systemctl restart watchman-net
```

Run restarts during a maintenance window. Changing paths does not migrate existing files. Changing `SERVICE_USER` alone does not change the installed systemd unit or filesystem permissions.

## Pushover

Create a Pushover application token and obtain your user key from [Pushover](https://pushover.net). Enter them in the Settings page or installed config, set `NOTIFY_ENABLED=yes`, and use **Send test notification**. Set it to `no` when not configured.

Notifications cover ingest success/failure, watchdog/reset events, startup diagnostics, retention results and Nextcloud failures. Credentials must remain private; never put real tokens in the example file or GitHub issues.

## Access and Permissions

The web service needs read/write access to the active archive and config, and suitable access to retention storage. See [installation permissions](installation.md#install-on-the-pi) and [mount checks](storage.md#external-or-network-storage).

There is no authentication or per-user isolation. The Origin/Referer and custom-header checks on mutating HTTP methods are CSRF defenses, not access control. Cleanup can also run on ordinary clip-page GET requests. Do not describe browsing as entirely read-only when retention is enabled.

For remote access, prefer an authenticated VPN or an HTTPS reverse proxy with authentication. Preserve the expected host handling when proxying and test settings writes; a host mismatch can produce a 403. Never forward port 5000 directly to the public internet.
