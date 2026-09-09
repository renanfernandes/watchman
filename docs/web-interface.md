# Web Interface

[Back to README](../README.md) | [Storage](storage.md) | [Configuration](configuration.md)

## Pages

| URL | Purpose |
| --- | --- |
| `/` | Today's clips when available, otherwise date navigation |
| `/date/YYYY-MM-DD` | Clips for a recording date, with search and camera filters |
| `/starred` | Available starred videos grouped by date |
| `/dashboard` | Storage totals, activity indicators and service status |
| `/settings` | Retention, integrations, playback and service controls |

## Browsing and Playback

The date browser orders clips newest displayed time first. Search matches camera, time, or filename; camera filtering can be combined with search. With the default `VIDEO_PRELOAD=none`, videos load on play while thumbnails and cached durations are available separately. Older clips without cached metadata may lack a poster or duration.

The calendar uses a red dot for active clips and yellow for archived/deleted history. Dates discovered in the configured retention directory can also appear yellow. A yellow dot is **not proof that a playable clip still exists**: history-only dates can return 404 after deletion or when storage is unavailable.

When a date is loaded from retention storage, the page displays **Archived videos**. The current fallback operates at date-directory level: if the active date directory still exists, archived clips for the same date are not merged into that listing. The sidebar's date list is also based on active storage.

Individual playback and downloads can resolve active or retention files. Archived deletion and bulk actions are not fully supported: those routes still target active storage. Avoid using those controls for retention copies until that limitation is fixed.

## Stars

Select the star beside a clip to toggle its bookmark. A selected star is yellow. Open **Starred** to see available bookmarked clips across dates, including retention copies. Unstarring removes the bookmark, not the video.

> [!IMPORTANT]
> Stars do not exempt clips from retention or manual deletion, and do not make a backup. All users share the same starred collection.

Bookmarks are stored as date/filename keys in `ARCHIVE_DIR/.starred.json`. They normally continue to resolve after a move to retention storage if the filename is unchanged. A collision rename during archiving does not update the bookmark. Missing clips are hidden from the list, but stale bookmarks may still contribute to its displayed total.

The current save implementation can log a write failure while the browser appears successful. Reload the date page to verify important bookmarks, and check web logs if they disappear. After unstarring, page/group totals may require a refresh.

## Dashboard

| Metric | Meaning |
| --- | --- |
| Clips / Clip size | MP4 count and summed file sizes under active storage |
| Archived clips / Archived size | MP4 count and summed file sizes under configured retention storage |
| Newest clip | Latest active date directory |
| Last sync / Last import | Activity timestamps, with file-time fallbacks when activity state is unavailable |
| Disk usage | Used space on the Pi's root filesystem, not the external archive disk |

Sizes exclude thumbnails and other non-MP4 metadata. Active and retention scans are recursive; use separate, non-nested roots to avoid overlapping totals. An unset or missing retention path can display zero, so zero is not a mount-health check. Service status comes from systemd; large or slow storage scans can delay the page.

## Settings

Use Settings to configure retention and estimate its impact, run cleanup, configure Nextcloud and notifications, select preload behavior, and inspect/restart Watchman's services. Review [retention behavior](storage.md#retention) before enabling deletion.

Keep `none` preload for low-power devices or busy recording days. `metadata` and `auto` can produce many simultaneous video requests. Reducing browser traffic does not fix an inadequate power supply.
