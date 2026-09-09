# Storage and Retention

[Back to README](../README.md) | [Web interface](web-interface.md) | [Operations](operations.md)

## Storage Layout

| Location | Contents |
| --- | --- |
| `CONTAINER` | Partitioned exFAT backing image presented to Blink |
| `ARCHIVE_DIR/YYYY-MM-DD/*.mp4` | Active recordings |
| `ARCHIVE_DIR/.thumbnails/YYYY-MM-DD/` | Thumbnail JPGs and duration JSON sidecars |
| `ARCHIVE_DIR/.starred.json` | Shared date/filename bookmarks |
| `ARCHIVE_DIR/.retention_history.json` | Per-date archived/deleted counters |
| `ARCHIVE_DIR/.retention_state.json` | Last retention-run timestamp |
| `RETENTION_ARCHIVE_DIR/YYYY-MM-DD/*.mp4` | Long-term retention copies |
| `/tmp/watchman_activity_state.json` | Transient ingest/import timestamps; not durable across reboots |

The installed web entry point sets the persistent metadata paths under `ARCHIVE_DIR`. Changing that root requires migrating metadata as well as videos. Retention moves MP4s only; thumbnails and bookmarks remain in the active root.

## Retention

- `off`: leave active clips untouched.
- `archive`: move eligible clips to the configured destination, retaining the date directory structure.
- `delete`: permanently remove eligible active clips, optionally after Nextcloud verification.

Eligibility uses the date-directory name, not modification time. With a threshold of 30, a date exactly 30 days old is not yet eligible. Destination collisions receive a numeric filename suffix.

Automatic cleanup runs when `/` or a date page is requested, subject to `RETENTION_RUN_INTERVAL`. It is **not a cron job or background timer**: an idle web interface does not guarantee cleanup runs. Settings also provides a manual cleanup action. Cleanup and network uploads can make the triggering request slow.

> [!WARNING]
> Delete mode includes starred clips. Without Nextcloud enabled, deletion has no offsite-verification prerequisite. Archive mode moves files rather than retaining a second local copy. Neither stars nor retention replace a backup.

Only active storage is subject to this cleanup. Retention copies are not automatically expired by the same policy. If you change the retention destination, old destinations are not searched automatically.

## External or Network Storage

Mount the destination persistently before enabling archive mode. Configure the mount using the operating system's supported mechanism, and give the web service user appropriate permissions. Keep active and retention roots separate and non-nested.

**On the Pi**, substitute your actual mount point:

```bash
findmnt --mountpoint /mnt/archive
df -h /mnt/archive
namei -l /mnt/archive
```

Check access as the installed web service user as well as from your SSH account. A writable ordinary directory is not proof the intended filesystem is mounted. Current code does not enforce a mount dependency or verify the filesystem identity; an absent mount can cause moves into the underlying local directory. Leave retention off until mount and access checks pass.

## Nextcloud

Nextcloud integration applies to **retention delete mode**, not continuous synchronization or archive mode. Watchman runs `rclone copy --checksum` for each eligible date directory, then `rclone check --one-way`. A failed upload or check skips deletion for that date so a later cleanup can retry.

Remote paths are `REMOTE/<year>/<month>/YYYY-MM-DD`. Hash availability depends on the remote backend; do not assume every server supports a common checksum or that this is a full independent restore test. Manual clip deletion does not invoke this upload safeguard.

**On the Pi:**

```bash
sudo apt-get install -y rclone
systemctl show watchman-web --property=User --value
```

Log in as that service user and run:

```bash
rclone config
rclone lsd nextcloud:
```

Configure WebDAV, vendor Nextcloud, and the URL `https://<server>/remote.php/dav/files/<username>/`. Use a Nextcloud app password and enter it directly into rclone's prompt. Configuring the remote as root alone does not make it available to the non-root web service.

Set these values in Settings or the installed config:

```ini
NEXTCLOUD_ENABLED=yes
NEXTCLOUD_REMOTE=nextcloud:Watchman
```

Verify the remote works under the service account before selecting `RETENTION_MODE=delete`. Upload/check subprocess timeouts are one hour and ten minutes respectively. Test restoring a sample clip independently before relying on this workflow.

## Backups and Restore

Back up both clip roots, including hidden files in the active root, the installed configuration, and the service user's rclone configuration if used. Store credential-bearing backups privately on separate storage. Thumbnail/duration caches can be regenerated, but bookmark and retention history cannot be reconstructed completely from videos alone.

For a consistent filesystem backup, stop ingest and web services during a maintenance window. This creates a recording-ingest interruption; Blink may continue writing into its virtual disk until it fills.

```bash
sudo systemctl stop watchman watchman-web
```

Use your backup tool to copy the configured archive roots and metadata to independent storage, preserving permissions and hidden files. For example, after setting `ACTIVE_ROOT` and `BACKUP_ROOT` to verified paths:

```bash
sudo rsync -a -- "${ACTIVE_ROOT:?Set ACTIVE_ROOT to the configured clip directory}/" "${BACKUP_ROOT:?Set BACKUP_ROOT to verified independent storage}/active/"
```

Repeat for retention storage if configured; verify file counts and checksums with your backup tool. Do not copy or locally mount the live backing image as though it were an idle filesystem. A disk-image backup additionally requires disconnecting Blink's access and verifying the image is unused.

To restore, stop the services, restore the files to their configured roots, and restore ownership/access for the installed service user. Check that `.starred.json` is present and the retention mount is correct, then restart:

```bash
sudo systemctl start watchman watchman-web
```

Validate a recent clip, an archived clip, and a bookmarked clip. See [operations](operations.md#updates) for application/configuration snapshots and code rollback; those snapshots do not back up footage.
