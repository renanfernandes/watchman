#!/usr/bin/env python3
"""
Watchman Web — Browse, play, and download archived Blink clips.

Usage:
    python3 web.py
    python3 web.py --config /etc/watchman/watchman.conf --port 8080
"""

import argparse
import json
import logging
import socket
import subprocess
import threading
import time
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta, timezone as _tz
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo as _ZoneInfo
from flask import Flask, render_template, send_file, abort, request, redirect, url_for, jsonify
import io
import shutil
import zipfile
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError

def _local_tz():
    tz_file = Path("/etc/timezone")
    if tz_file.exists():
        return _ZoneInfo(tz_file.read_text().strip())
    return _ZoneInfo("UTC")

_LOCAL_TZ = _local_tz()

log = logging.getLogger("watchman-web")

app = Flask(__name__)
CONFIG_PATH = "watchman.conf"
RETENTION_STATE_FILE = Path("/tmp/watchman_retention_state.json")
RETENTION_HISTORY_FILE = Path("/tmp/watchman_retention_history.json")


@app.before_request
def _reject_cross_origin_writes():
    """Block cross-site request forgery on any state-changing request.

    Two independent layers, since either alone can have gaps:

    1. Origin/Referer check — browsers automatically attach an Origin header
       to POST/PUT/DELETE/PATCH requests, including plain HTML form
       submissions, so this catches forged requests from a different site
       with no changes needed to any form.

    2. Custom header check — every legitimate form on this site submits via
       fetch() with an X-Requested-With header (see the JS in index.html and
       settings.html). A plain HTML <form> — the classic CSRF vector, e.g. a
       hidden auto-submitting form on some other page — has no way to set a
       custom header at all, so this rejects anything that isn't going
       through our own JS, even if Origin/Referer were somehow spoofed.
    """
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return None

    origin = request.headers.get("Origin")
    source = origin
    if not source:
        # Older browsers/some HTTP clients may omit Origin — fall back to
        # Referer, which is also sent on real same-site form submissions.
        source = request.headers.get("Referer")

    if not source:
        # No Origin and no Referer on a state-changing request is unusual
        # enough (real browsers send at least one) to treat as suspicious.
        log.warning("Blocked %s %s — no Origin/Referer header present", request.method, request.path)
        abort(403)

    try:
        source_host = urlparse(source).netloc
    except Exception:
        source_host = ""

    if source_host != request.host:
        log.warning("Blocked %s %s — Origin/Referer host '%s' != request host '%s'",
                    request.method, request.path, source_host, request.host)
        abort(403)

    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        log.warning("Blocked %s %s — missing X-Requested-With header (not our own JS)",
                    request.method, request.path)
        abort(403)

    return None


def archive_dir() -> Path:
    """Return the configured archive directory for the current runtime."""
    cfg = load_config(CONFIG_PATH)
    return Path(cfg.get("ARCHIVE_DIR", "/home/watchman/archive")).expanduser()


def load_config(path: str) -> dict:
    """Read config file for archive dir and port."""
    config = {
        "ARCHIVE_DIR": "/home/watchman/archive",
        "WEB_HOST": "0.0.0.0",
        "WEB_PORT": "5000",
        "RETENTION_MODE": "off",
        "RETENTION_DAYS": "90",
        "RETENTION_ARCHIVE_DIR": "",
        "RETENTION_RUN_INTERVAL": "3600",
        "NEXTCLOUD_ENABLED": "no",
        "NEXTCLOUD_REMOTE": "",
        "NEXTCLOUD_RCLONE_PATH": "rclone",
        "NOTIFY_ENABLED": "no",
        "PUSHOVER_TOKEN": "",
        "PUSHOVER_USER": "",
        "VIDEO_PRELOAD": "none",
        "SETTLE_TIME": "120",
        "MIN_INTERVAL": "1800",
        "NET_WATCHDOG_ENABLED": "yes",
        "NET_WATCHDOG_TIMEOUT": "600",
    }
    if not Path(path).exists():
        return config
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()
    return config


def update_config(path: str, updates: dict[str, str]) -> None:
    """Update or append KEY=VALUE pairs in the config file."""
    cfg_path = Path(path)
    lines = cfg_path.read_text(encoding="utf-8").splitlines() if cfg_path.exists() else []
    remaining = dict(updates)
    new_lines = []

    for line in lines:
        raw = line.strip()
        replaced = False
        if raw and not raw.startswith("#") and "=" in raw:
            key, _value = raw.split("=", 1)
            key = key.strip()
            if key in remaining:
                new_lines.append(f"{key}={remaining.pop(key)}")
                replaced = True
        if not replaced:
            new_lines.append(line)

    for key, value in remaining.items():
        new_lines.append(f"{key}={value}")

    cfg_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def parse_positive_int(value: str, default: int) -> int:
    """Parse positive integer from config with fallback."""
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def retention_settings() -> dict:
    """Read retention settings from config file."""
    cfg = load_config(CONFIG_PATH)
    mode = cfg.get("RETENTION_MODE", "off").strip().lower()
    if mode not in {"off", "archive", "delete"}:
        mode = "off"
    days = parse_positive_int(cfg.get("RETENTION_DAYS", "90"), 90)
    return {
        "mode": mode,
        "days": days,
        "archive_dir": cfg.get("RETENTION_ARCHIVE_DIR", "").strip(),
        "interval": parse_positive_int(cfg.get("RETENTION_RUN_INTERVAL", "3600"), 3600),
    }


def nextcloud_settings() -> dict:
    """Read Nextcloud/rclone sync settings from config file."""
    cfg = load_config(CONFIG_PATH)
    enabled = cfg.get("NEXTCLOUD_ENABLED", "no").strip().lower() in {"yes", "true", "1"}
    return {
        "enabled": enabled,
        "remote": cfg.get("NEXTCLOUD_REMOTE", "").strip(),
        "rclone_path": cfg.get("NEXTCLOUD_RCLONE_PATH", "rclone").strip() or "rclone",
    }


def notify_settings() -> dict:
    """Read Pushover notification settings from config file."""
    cfg = load_config(CONFIG_PATH)
    enabled = cfg.get("NOTIFY_ENABLED", "no").strip().lower() in {"yes", "true", "1"}
    return {
        "enabled": enabled,
        "token": cfg.get("PUSHOVER_TOKEN", "").strip(),
        "user": cfg.get("PUSHOVER_USER", "").strip(),
    }


def video_settings() -> dict:
    """Read video playback settings (preload behavior) from config file."""
    cfg = load_config(CONFIG_PATH)
    preload = cfg.get("VIDEO_PRELOAD", "none").strip().lower()
    # "auto" is disabled for now (see save_video_settings) — treat any
    # existing config value of "auto" as "none" rather than rendering it.
    if preload not in {"none", "metadata"}:
        preload = "none"
    return {"preload": preload}


def timing_settings() -> dict:
    """Read ingest timing and network watchdog settings from config file."""
    cfg = load_config(CONFIG_PATH)
    return {
        "settle_time": parse_positive_int(cfg.get("SETTLE_TIME", "120"), 120),
        "min_interval": parse_positive_int(cfg.get("MIN_INTERVAL", "1800"), 1800),
        "net_watchdog_enabled": cfg.get("NET_WATCHDOG_ENABLED", "yes").strip().lower() in {"yes", "true", "1"},
        "net_watchdog_timeout": parse_positive_int(cfg.get("NET_WATCHDOG_TIMEOUT", "600"), 600),
    }


def get_services_status() -> str:
    """Return `systemctl status` output for Watchman's core services."""
    try:
        result = subprocess.run(
            ["systemctl", "status", "watchman.service", "watchman-web.service", "--no-pager"],
            capture_output=True, text=True, timeout=10,
        )
        output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
        return output.strip() or "No status output returned."
    except Exception as e:
        return f"Could not retrieve service status: {e}"


def pushover_notify(title: str, message: str, priority: int = 0) -> None:
    """Send a push notification via Pushover, if enabled and configured."""
    settings = notify_settings()
    if not settings["enabled"] or not settings["token"] or not settings["user"]:
        return
    try:
        data = urlencode({
            "token": settings["token"],
            "user": settings["user"],
            "title": f"Watchman | {title}",
            "message": f"Host: {socket.gethostname()}\n\n{message}",
            "priority": priority,
        }).encode()
        req = Request("https://api.pushover.net/1/messages.json", data=data)
        urlopen(req, timeout=10)
    except URLError as e:
        log.warning("Pushover notification failed: %s", e)
    except Exception as e:
        log.warning("Pushover notification error: %s", e)


def nextcloud_upload_and_verify(folder: Path, year: str, month: str) -> bool:
    """
    Ship one date folder's clips to Nextcloud via rclone, using the same
    year/month/day naming the archive already uses, then verify the remote
    copy matches before retention is allowed to delete anything locally.

    rclone creates the remote year/month/day folders automatically if they
    don't exist yet. Returns True only when the upload is confirmed complete
    (or when Nextcloud sync isn't enabled, so retention behaves as before).
    """
    settings = nextcloud_settings()
    if not settings["enabled"]:
        return True

    if not settings["remote"]:
        log.error("NEXTCLOUD_ENABLED=yes but NEXTCLOUD_REMOTE is not set — skipping upload for %s", folder.name)
        pushover_notify("Nextcloud Config Error",
                 "Status: Failed\nNEXTCLOUD_REMOTE is not set", priority=1)
        return False

    rclone = settings["rclone_path"]
    remote_path = f"{settings['remote']}/{year}/{month}/{folder.name}"

    try:
        subprocess.run(
            [rclone, "copy", str(folder), remote_path, "--checksum"],
            check=True, timeout=3600, capture_output=True, text=True,
        )
    except FileNotFoundError:
        log.error("rclone binary not found (%s) — cannot ship %s to Nextcloud", rclone, folder.name)
        pushover_notify("Nextcloud Upload Failed",
                 f"Status: Failed\nFolder: {folder.name}\nReason: rclone binary not found", priority=1)
        return False
    except subprocess.TimeoutExpired:
        log.error("rclone copy timed out for %s", folder.name)
        pushover_notify("Nextcloud Upload Failed",
                 f"Status: Failed\nFolder: {folder.name}\nReason: rclone copy timed out", priority=1)
        return False
    except subprocess.CalledProcessError as e:
        log.error("rclone copy failed for %s: %s", folder.name, e.stderr.strip() if e.stderr else e)
        pushover_notify("Nextcloud Upload Failed",
                 f"Status: Failed\nFolder: {folder.name}\nReason: rclone copy failed", priority=1)
        return False

    # Double-check: compare local files against what actually landed on
    # Nextcloud (size + checksum) before this folder is allowed to be deleted.
    try:
        check = subprocess.run(
            [rclone, "check", str(folder), remote_path, "--one-way"],
            timeout=600, capture_output=True, text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.error("rclone check failed for %s: %s", folder.name, e)
        pushover_notify("Nextcloud Verify Failed",
                 f"Status: Failed\nFolder: {folder.name}\nReason: rclone check failed", priority=1)
        return False

    if check.returncode != 0:
        log.warning(
            "Nextcloud verification failed for %s (rclone check exit %d) — will retry next run: %s",
            folder.name, check.returncode, check.stderr.strip() if check.stderr else "",
        )
        pushover_notify("Nextcloud Verify Failed",
                 f"Status: Failed\nFolder: {folder.name}\nRetry: Next retention run", priority=1)
        return False

    log.info("Verified %s uploaded to Nextcloud at %s", folder.name, remote_path)
    return True


def should_run_retention(interval_seconds: int) -> bool:
    """Gate automatic retention runs to avoid heavy work on every request."""
    now = _datetime.now(_tz.utc).timestamp()
    if not RETENTION_STATE_FILE.exists():
        return True
    try:
        payload = json.loads(RETENTION_STATE_FILE.read_text(encoding="utf-8"))
        last_run = float(payload.get("last_run", 0))
        return (now - last_run) >= interval_seconds
    except Exception:
        return True


def mark_retention_run() -> None:
    """Persist retention run timestamp."""
    try:
        RETENTION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        RETENTION_STATE_FILE.write_text(
            json.dumps({"last_run": _datetime.now(_tz.utc).timestamp()}),
            encoding="utf-8",
        )
    except Exception:
        pass


def load_retention_history() -> dict:
    """Load per-date archive/delete history used by calendar indicators."""
    if not RETENTION_HISTORY_FILE.exists():
        return {}
    try:
        payload = json.loads(RETENTION_HISTORY_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def save_retention_history(data: dict) -> None:
    """Persist per-date archive/delete history."""
    try:
        RETENTION_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        RETENTION_HISTORY_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def record_retention_event(date_str: str, action: str, count: int = 1) -> None:
    """Record that clips from a date were archived/deleted from the active archive."""
    if action not in {"archived", "deleted"} or count <= 0:
        return
    history = load_retention_history()
    day = history.setdefault(date_str, {"archived": 0, "deleted": 0})
    day[action] = int(day.get(action, 0)) + count
    save_retention_history(history)


def move_with_unique_name(src: Path, dst_dir: Path) -> bool:
    """Move file to destination directory without overwriting existing files."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    counter = 1
    while dst.exists():
        dst = dst_dir / f"{src.stem}_{counter}{src.suffix}"
        counter += 1
    # Use shutil.move so archiving works across filesystems/mount points.
    shutil.move(str(src), str(dst))
    return True


def run_retention_cleanup(force: bool = False) -> dict:
    """Apply retention policy: archive or delete old clips by recording date."""
    settings = retention_settings()
    result = {"moved": 0, "deleted": 0, "errors": 0, "checked": 0, "mode": settings["mode"]}

    if settings["mode"] == "off":
        return result
    if not ARCHIVE_DIR.exists():
        return result
    if not force and not should_run_retention(settings["interval"]):
        return result

    today = _date.today()
    target_dir = Path(settings["archive_dir"]).expanduser() if settings["archive_dir"] else None

    if settings["mode"] == "archive":
        if not target_dir:
            result["errors"] += 1
            mark_retention_run()
            return result
        try:
            same_path = target_dir.resolve() == ARCHIVE_DIR.resolve()
        except OSError:
            result["errors"] += 1
            mark_retention_run()
            return result
        if same_path:
            result["errors"] += 1
            mark_retention_run()
            return result

    date_events: dict[str, dict[str, int]] = {}

    try:
        archive_folders = list(ARCHIVE_DIR.iterdir())
    except OSError:
        result["errors"] += 1
        mark_retention_run()
        return result

    for folder in archive_folders:
        if not folder.is_dir():
            continue
        parts = folder.name.split("-")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            continue
        try:
            folder_date = _date.fromisoformat(folder.name)
        except ValueError:
            continue

        age_days = (today - folder_date).days
        if age_days <= settings["days"]:
            continue

        try:
            clips = list(folder.iterdir())
        except OSError:
            result["errors"] += 1
            continue

        # Before permanently deleting anything, ship this folder's footage
        # to Nextcloud and verify it landed intact. If it's not enabled,
        # this is a no-op and behaves exactly as before.
        if settings["mode"] == "delete":
            year, month, _day = folder.name.split("-")
            if not nextcloud_upload_and_verify(folder, year, month):
                log.warning(
                    "Skipping deletion of %s — Nextcloud upload not verified yet",
                    folder.name,
                )
                result["errors"] += 1
                continue

        for clip in clips:
            if clip.suffix != ".mp4" or not clip.is_file():
                continue
            result["checked"] += 1
            try:
                if settings["mode"] == "delete":
                    clip.unlink()
                    result["deleted"] += 1
                    day = date_events.setdefault(folder.name, {"archived": 0, "deleted": 0})
                    day["deleted"] += 1
                elif settings["mode"] == "archive" and target_dir is not None:
                    move_with_unique_name(clip, target_dir / folder.name)
                    result["moved"] += 1
                    day = date_events.setdefault(folder.name, {"archived": 0, "deleted": 0})
                    day["archived"] += 1
            except Exception:
                result["errors"] += 1

        try:
            if folder.exists() and not any(folder.iterdir()):
                folder.rmdir()
        except Exception:
            result["errors"] += 1

    mark_retention_run()
    for date_str, events in date_events.items():
        if events["archived"] > 0:
            record_retention_event(date_str, "archived", events["archived"])
        if events["deleted"] > 0:
            record_retention_event(date_str, "deleted", events["deleted"])

    if result["deleted"] > 0 or result["moved"] > 0 or result["errors"] > 0:
        parts = []
        if result["deleted"] > 0:
            parts.append(f"{result['deleted']} deleted")
        if result["moved"] > 0:
            parts.append(f"{result['moved']} archived to {target_dir}")
        if result["errors"] > 0:
            parts.append(f"{result['errors']} error(s)")
        pushover_notify(
            "Retention Cleanup",
            f"Status: {'Completed with errors' if result['errors'] else 'Complete'}\n"
            + "\n".join(parts),
            priority=1 if result["errors"] > 0 else 0,
        )

    return result


def retention_cleanup_estimate(days: int) -> dict:
    """Estimate how many files/bytes are eligible for retention cleanup."""
    today = _date.today()
    cutoff_date = today - _timedelta(days=days)
    result = {
        "days": days,
        "files": 0,
        "bytes": 0,
        "mb": 0.0,
        "gb": 0.0,
        "size_text": "0.00 MB",
        "cutoff_date": cutoff_date.isoformat(),
        "oldest_clip_date": None,
        "oldest_clip_age_days": None,
        "oldest_eligible_date": None,
        "oldest_eligible_age_days": None,
    }
    if not ARCHIVE_DIR.exists():
        return result

    try:
        archive_folders = list(ARCHIVE_DIR.iterdir())
    except OSError:
        return result

    for folder in archive_folders:
        if not folder.is_dir():
            continue
        parts = folder.name.split("-")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            continue
        try:
            folder_date = _date.fromisoformat(folder.name)
        except ValueError:
            continue

        age_days = (today - folder_date).days

        try:
            clips = [
                clip for clip in folder.iterdir()
                if clip.suffix == ".mp4" and clip.is_file()
            ]
        except OSError:
            continue

        if not clips:
            continue

        if result["oldest_clip_age_days"] is None or age_days > int(result["oldest_clip_age_days"]):
            result["oldest_clip_date"] = folder_date.isoformat()
            result["oldest_clip_age_days"] = age_days

        if age_days <= days:
            continue

        if result["oldest_eligible_age_days"] is None or age_days > int(result["oldest_eligible_age_days"]):
            result["oldest_eligible_date"] = folder_date.isoformat()
            result["oldest_eligible_age_days"] = age_days

        for clip in clips:
            result["files"] += 1
            try:
                result["bytes"] += clip.stat().st_size
            except OSError:
                continue

    result["mb"] = round(result["bytes"] / (1024 * 1024), 2)
    result["gb"] = round(result["bytes"] / (1024 * 1024 * 1024), 2)
    if result["mb"] >= 1024:
        result["size_text"] = f"{result['gb']:.2f} GiB"
    else:
        result["size_text"] = f"{result['mb']:.2f} MB"
    return result


# ── Helpers ─────────────────────────────────────────────────────────────────


def list_dates():
    """Get all recording dates grouped by year > month, newest first."""
    import calendar
    if not ARCHIVE_DIR.exists():
        return []

    items = []
    for folder in ARCHIVE_DIR.iterdir():
        if not folder.is_dir():
            continue
        parts = folder.name.split("-")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            continue
        year, month, _ = parts
        count = sum(1 for f in folder.iterdir() if f.suffix == ".mp4")
        items.append({"name": folder.name, "year": year, "month": month, "count": count})

    items.sort(key=lambda x: x["name"], reverse=True)

    grouped = {}
    for item in items:
        y, m = item["year"], item["month"]
        grouped.setdefault(y, {}).setdefault(m, []).append(item)

    result = []
    for year in sorted(grouped, reverse=True):
        months = []
        for month in sorted(grouped[year], reverse=True):
            months.append({
                "month": month,
                "month_name": calendar.month_name[int(month)],
                "days": grouped[year][month],
            })
        result.append({"year": year, "months": months})
    return result


def all_dates_map() -> dict:
    """Return {date_str: clip_count} for every date folder."""
    result = {}
    if not ARCHIVE_DIR.exists():
        return result
    for folder in ARCHIVE_DIR.iterdir():
        if not folder.is_dir():
            continue
        parts = folder.name.split("-")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            continue
        result[folder.name] = sum(1 for f in folder.iterdir() if f.suffix == ".mp4")
    return result


def calendar_status_map() -> dict:
    """Return date status map for calendar: current clips and retained history."""
    counts = all_dates_map()
    history = load_retention_history()
    result: dict[str, dict] = {}

    for date_str, count in counts.items():
        result[date_str] = {"count": count, "had_old": False}

    for date_str, events in history.items():
        if date_str in result:
            continue
        archived = int(events.get("archived", 0))
        deleted = int(events.get("deleted", 0))
        if archived > 0 or deleted > 0:
            result[date_str] = {"count": 0, "had_old": True}

    return result


def human_size(num_bytes: int) -> str:
    """Format a byte count for display."""
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(num_bytes)
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.1f} {units[unit_index]}"


def format_activity_timestamp(value: Optional[str]) -> str:
    """Render an ISO timestamp as a compact human-readable string."""
    if not value:
        return "Never"
    try:
        dt = _datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value


def load_activity_state() -> dict:
    """Read the latest ingest/import timestamps for the dashboard."""
    state_path = Path("/tmp/watchman_activity_state.json")
    state = {"last_sync": None, "last_import": None}

    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {"last_sync": None, "last_import": None}

    archive_path = archive_dir()
    if archive_path.exists():
        clips = [p for p in archive_path.rglob("*.mp4") if p.is_file()]
        if clips:
            latest_mtime = max((p.stat().st_mtime for p in clips if p.exists()), default=None)
            if latest_mtime is not None:
                timestamp = _datetime.fromtimestamp(latest_mtime).isoformat()
                if not state.get("last_import"):
                    state["last_import"] = timestamp
                if not state.get("last_sync"):
                    state["last_sync"] = timestamp

    return state


def dashboard_stats() -> dict:
    """Collect a small summary of the archive state for the dashboard."""
    archive_path = archive_dir()
    clip_files = []
    if archive_path.exists():
        clip_files = [p for p in archive_path.rglob("*.mp4") if p.is_file()]

    total_bytes = sum(p.stat().st_size for p in clip_files if p.exists())
    date_count = 0
    latest_date = None
    if archive_path.exists():
        date_dirs = [p for p in archive_path.iterdir() if p.is_dir() and safe_date(p.name)]
        date_dirs.sort(key=lambda p: p.name, reverse=True)
        date_count = len(date_dirs)
        if date_dirs:
            latest_date = date_dirs[0].name

    disk_usage = shutil.disk_usage("/")
    activity = load_activity_state()
    return {
        "clip_count": len(clip_files),
        "size_text": human_size(total_bytes),
        "date_count": date_count,
        "latest_date": latest_date,
        "disk_usage_text": human_size(disk_usage.used),
        "last_sync": format_activity_timestamp(activity.get("last_sync")),
        "last_import": format_activity_timestamp(activity.get("last_import")),
    }


def recent_clips(limit: int = 8) -> list[dict]:
    """Return the most recently modified clips for the dashboard."""
    items = []
    archive_path = archive_dir()
    if not archive_path.exists():
        return items

    for clip_path in archive_path.rglob("*.mp4"):
        if not clip_path.is_file():
            continue
        try:
            stat = clip_path.stat()
        except OSError:
            continue
        items.append({
            "path": clip_path,
            "name": clip_path.name,
            "date": clip_path.parent.name,
            "size_text": human_size(stat.st_size),
            "mtime": stat.st_mtime,
        })

    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items[:limit]


def service_statuses() -> list[dict]:
    """Check a few core services and return friendly status rows."""
    services = ["watchman", "watchman-web", "watchman-net", "watchman-startup"]
    rows = []
    for name in services:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            state = (result.stdout or "").strip().lower()
            label = "active" if state == "active" else "inactive" if state in {"inactive", "failed", "dead"} else "unknown"
            rows.append({
                "name": name,
                "state": label,
                "state_label": state or "unknown",
            })
        except Exception:
            rows.append({"name": name, "state": "unknown", "state_label": "unknown"})
    return rows


def safe_date(date_str: str) -> bool:
    """Validate date string is exactly YYYY-MM-DD format."""
    return (
        len(date_str) == 10
        and all(c in "0123456789-" for c in date_str)
    )


def safe_filename(filename: str) -> bool:
    """Block path traversal in filenames."""
    return ".." not in filename and "/" not in filename and "\\" not in filename


def parse_video_meta(filename: str, ref_date: Optional[_date] = None, date_str: Optional[str] = None) -> dict:
    """Extract time, camera name, and (if available) duration for a clip.

    ref_date should be the clip's actual recording date (not a fixed stand-in
    date), since the UTC->local conversion depends on which DST era applies —
    using a fixed winter date would always resolve to standard time even for
    clips recorded during daylight saving time, shifting the displayed time
    by an hour.

    Duration is read from a small JSON sidecar generated by ffprobe at ingest
    time (see probe_video_meta() in watchman.py) — this gives the same
    "know before you press play" info that preload="metadata" would, without
    ever making the browser request the video file itself.
    """
    stem = Path(filename).stem  # e.g. "13-38-41_DoorbellFront_001"
    parts = stem.split("_", 2)
    time_str = ""
    camera = ""
    if len(parts) >= 1:
        t = parts[0]  # "13-38-41"
        if len(t) == 8 and t[2] == "-" and t[5] == "-" and t.replace("-", "").isdigit():
            d = ref_date or _date.today()
            utc_dt = _datetime(d.year, d.month, d.day, int(t[0:2]), int(t[3:5]), int(t[6:8]), tzinfo=_tz.utc)
            time_str = utc_dt.astimezone(_LOCAL_TZ).strftime("%H:%M:%S")
    if len(parts) >= 2:
        camera = parts[1].replace("-", " ").replace("_", " ")

    duration_str = ""
    if date_str:
        meta_path = ARCHIVE_DIR / ".thumbnails" / date_str / f"{stem}.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                secs = int(round(float(meta.get("duration") or 0)))
                if secs > 0:
                    duration_str = f"{secs // 60}:{secs % 60:02d}"
            except Exception:
                pass

    return {"name": filename, "time": time_str, "camera": camera, "duration": duration_str}


# ── Routes ──────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    """Redirect to today's recordings if they exist, otherwise show landing page."""
    run_retention_cleanup()
    settings = retention_settings()
    today = _date.today().strftime("%Y-%m-%d")
    if (ARCHIVE_DIR / today).is_dir():
        return redirect(url_for("by_date", date_str=today))
    return render_template("index.html",
                           dates=list_dates(), current_date=None, videos=[],
                           dates_map=json.dumps(calendar_status_map()),
                           settings=settings,
                           video_preload=video_settings()["preload"],
                           status_message=request.args.get("msg", ""))


@app.route("/date/<date_str>")
def by_date(date_str: str):
    """List videos for a specific date."""
    run_retention_cleanup()
    settings = retention_settings()
    if not safe_date(date_str):
        abort(400)

    date_dir = ARCHIVE_DIR / date_str
    if not date_dir.exists() or not date_dir.is_dir():
        abort(404)
    if not date_dir.resolve().is_relative_to(ARCHIVE_DIR.resolve()):
        abort(403)

    try:
        folder_date = _date.fromisoformat(date_str)
    except ValueError:
        folder_date = _date.today()

    all_videos = sorted(
        (parse_video_meta(f.name, folder_date, date_str) for f in date_dir.iterdir() if f.suffix == ".mp4"),
        key=lambda v: (v["time"] or "", v["name"]),
        reverse=True,
    )

    search_query = (request.args.get("q", "") or "").strip().lower()
    selected_camera = (request.args.get("camera", "") or "").strip()

    filtered_videos = []
    for video in all_videos:
        if selected_camera and (video.get("camera") or "").lower() != selected_camera.lower():
            continue
        if search_query:
            haystack = " ".join([
                video.get("name", ""),
                video.get("camera", ""),
                video.get("time", ""),
            ]).lower()
            if search_query not in haystack:
                continue
        filtered_videos.append(video)

    camera_options = sorted(
        {video["camera"] for video in all_videos if video.get("camera")},
        key=str.lower,
    )

    return render_template("index.html",
                           dates=list_dates(), current_date=date_str, videos=filtered_videos,
                           dates_map=json.dumps(calendar_status_map()),
                           settings=settings,
                           video_preload=video_settings()["preload"],
                           status_message=request.args.get("msg", ""),
                           video_total_count=len(all_videos),
                           video_visible_count=len(filtered_videos),
                           search_query=search_query,
                           selected_camera=selected_camera,
                           camera_options=camera_options)


@app.route("/dashboard")
def dashboard_page():
    """Show a lightweight operational overview of archive health and services."""
    stats = dashboard_stats()
    return render_template(
        "dashboard.html",
        archive_stats=stats,
        services=service_statuses(),
        recent_clips=recent_clips(),
    )


@app.route("/settings")
def settings_page():
    """Show retention, Nextcloud, notification, and video settings page."""
    settings = retention_settings()
    estimate = retention_cleanup_estimate(settings["days"])
    nc_settings = nextcloud_settings()
    notif_settings = notify_settings()
    vid_settings = video_settings()
    timing = timing_settings()
    return render_template(
        "settings.html",
        settings=settings,
        estimate=estimate,
        nextcloud=nc_settings,
        notify=notif_settings,
        video=vid_settings,
        timing=timing,
        status_message=request.args.get("msg", ""),
    )


@app.route("/settings/retention/estimate")
def retention_estimate_api():
    """Return retention cleanup estimate for a given day threshold."""
    days = parse_positive_int(request.args.get("days", "90"), 90)
    return jsonify(retention_cleanup_estimate(days))


@app.route("/settings/retention", methods=["POST"])
def save_retention_settings():
    """Persist retention policy settings."""
    mode = request.form.get("retention_mode", "off").strip().lower()
    if mode not in {"off", "archive", "delete"}:
        abort(400)

    days = parse_positive_int(request.form.get("retention_days", "90"), 90)
    archive_dir = request.form.get("retention_archive_dir", "").strip()

    if mode == "archive":
        if not archive_dir:
            return redirect(url_for("settings_page", msg="Archive mode requires a destination folder."))
        target = Path(archive_dir).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            return redirect(url_for("settings_page", msg="Could not create archive destination folder."))

    try:
        update_config(
            CONFIG_PATH,
            {
                "RETENTION_MODE": mode,
                "RETENTION_DAYS": str(days),
                "RETENTION_ARCHIVE_DIR": archive_dir,
            },
        )
    except OSError:
        return redirect(
            url_for(
                "settings_page",
                msg=(
                    "Could not save settings (check write permissions for config file: "
                    f"{CONFIG_PATH})."
                ),
            )
        )
    return redirect(url_for("settings_page", msg="Retention settings saved."))


@app.route("/settings/retention/run", methods=["POST"])
def run_retention_now():
    """Manually trigger retention policy."""
    settings = retention_settings()
    if settings["mode"] == "off":
        return redirect(
            url_for(
                "settings_page",
                msg="Retention mode is Off. Set mode to Archive/Delete and save before running cleanup.",
            )
        )

    result = run_retention_cleanup(force=True)
    moved = int(result.get("moved", 0))
    deleted = int(result.get("deleted", 0))
    checked = int(result.get("checked", 0))
    errors = int(result.get("errors", 0))

    if moved == 0 and deleted == 0 and errors == 0:
        msg = (
            f"No clips older than {settings['days']} days were found "
            f"for mode={settings['mode']}."
        )
    else:
        msg = (
            f"Retention run complete (mode={settings['mode']}, days={settings['days']}): "
            f"checked={checked}, moved={moved}, deleted={deleted}, errors={errors}"
        )
    return redirect(url_for("settings_page", msg=msg))


@app.route("/settings/nextcloud", methods=["POST"])
def save_nextcloud_settings():
    """Persist Nextcloud/rclone sync settings."""
    enabled = "yes" if request.form.get("nextcloud_enabled") == "on" else "no"
    remote = request.form.get("nextcloud_remote", "").strip()
    rclone_path = request.form.get("nextcloud_rclone_path", "").strip() or "rclone"

    try:
        update_config(
            CONFIG_PATH,
            {
                "NEXTCLOUD_ENABLED": enabled,
                "NEXTCLOUD_REMOTE": remote,
                "NEXTCLOUD_RCLONE_PATH": rclone_path,
            },
        )
    except OSError:
        return redirect(
            url_for(
                "settings_page",
                msg=f"Could not save settings (check write permissions for config file: {CONFIG_PATH}).",
            )
        )
    return redirect(url_for("settings_page", msg="Nextcloud settings saved."))


@app.route("/settings/notifications", methods=["POST"])
def save_notification_settings():
    """Persist Pushover notification settings."""
    enabled = "yes" if request.form.get("notify_enabled") == "on" else "no"
    token = request.form.get("pushover_token", "").strip()
    user = request.form.get("pushover_user", "").strip()

    try:
        update_config(
            CONFIG_PATH,
            {
                "NOTIFY_ENABLED": enabled,
                "PUSHOVER_TOKEN": token,
                "PUSHOVER_USER": user,
            },
        )
    except OSError:
        return redirect(
            url_for(
                "settings_page",
                msg=f"Could not save settings (check write permissions for config file: {CONFIG_PATH}).",
            )
        )
    return redirect(url_for("settings_page", msg="Notification settings saved."))


@app.route("/settings/notifications/test", methods=["POST"])
def test_notification():
    """Send a test Pushover notification using the currently saved settings."""
    settings = notify_settings()
    if not settings["enabled"]:
        return redirect(url_for("settings_page", msg="Notifications are disabled — enable and save first."))
    if not settings["token"] or not settings["user"]:
        return redirect(url_for("settings_page", msg="Set and save your Pushover token/user key first."))

    pushover_notify("Test Notification", "Status: Test successful")
    return redirect(url_for("settings_page", msg="Test notification sent — check your device."))


@app.route("/settings/video", methods=["POST"])
def save_video_settings():
    """Persist video preload setting."""
    preload = request.form.get("video_preload", "none").strip().lower()
    # "auto" is disabled in the UI (root cause of a separate crash still
    # unconfirmed pending a hardware fix) — reject it server-side too, since
    # the disabled <option> attribute alone wouldn't stop a direct POST.
    if preload not in {"none", "metadata"}:
        preload = "none"

    try:
        update_config(CONFIG_PATH, {"VIDEO_PRELOAD": preload})
    except OSError:
        return redirect(
            url_for(
                "settings_page",
                msg=f"Could not save settings (check write permissions for config file: {CONFIG_PATH}).",
            )
        )
    return redirect(url_for("settings_page", msg="Video settings saved."))


@app.route("/settings/timing", methods=["POST"])
def save_timing_settings():
    """Persist ingest timing and network watchdog settings."""
    settle_time = parse_positive_int(request.form.get("settle_time", "120"), 120)
    min_interval = parse_positive_int(request.form.get("min_interval", "1800"), 1800)
    net_watchdog_enabled = "yes" if request.form.get("net_watchdog_enabled") == "on" else "no"
    net_watchdog_timeout = parse_positive_int(request.form.get("net_watchdog_timeout", "600"), 600)

    try:
        update_config(
            CONFIG_PATH,
            {
                "SETTLE_TIME": str(settle_time),
                "MIN_INTERVAL": str(min_interval),
                "NET_WATCHDOG_ENABLED": net_watchdog_enabled,
                "NET_WATCHDOG_TIMEOUT": str(net_watchdog_timeout),
            },
        )
    except OSError:
        return redirect(
            url_for(
                "settings_page",
                msg=f"Could not save settings (check write permissions for config file: {CONFIG_PATH}).",
            )
        )
    return redirect(url_for("settings_page", msg="Timing & Watchdog settings saved."))


# Only these exact units can be restarted from the web UI — never accept an
# arbitrary service name here.
RESTARTABLE_SERVICES = {"watchman", "watchman-web", "watchman-net", "watchman-startup"}


@app.route("/services/status")
def services_status_api():
    """Return live `systemctl status` output, fetched on demand by the button."""
    return get_services_status(), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/services/restart/<service>", methods=["POST"])
def restart_service(service: str):
    """Restart one of Watchman's own systemd services."""
    if service not in RESTARTABLE_SERVICES:
        abort(400)
    unit = f"{service}.service"

    def _do_restart():
        # Small delay so this HTTP response (and the redirect) actually
        # reaches the browser before the process — possibly this very one,
        # for watchman-web — gets restarted.
        time.sleep(1)
        try:
            subprocess.run(["sudo", "-n", "systemctl", "restart", unit],
                            check=True, timeout=30, capture_output=True)
        except Exception as e:
            log.error("Failed to restart %s: %s", unit, e)

    threading.Thread(target=_do_restart, daemon=True).start()
    return redirect(url_for("settings_page", msg=f"Restarting {unit}…"))


@app.route("/thumbnail/<date_str>/<filename>")
def serve_thumbnail(date_str: str, filename: str):
    """Serve a cached thumbnail image for a clip, or a placeholder if missing."""
    if not safe_date(date_str) or not safe_filename(filename):
        abort(400)

    thumb_path = ARCHIVE_DIR / ".thumbnails" / date_str / (Path(filename).stem + ".jpg")
    if not thumb_path.resolve().is_relative_to(ARCHIVE_DIR.resolve()):
        abort(403)
    if not thumb_path.exists():
        abort(404)

    return send_file(thumb_path, mimetype="image/jpeg")


@app.route("/video/<date_str>/<filename>")
def serve_video(date_str: str, filename: str):
    """Stream a video for in-browser playback."""
    if not safe_date(date_str) or not safe_filename(filename):
        abort(400)

    video_path = ARCHIVE_DIR / date_str / filename
    if not video_path.resolve().is_relative_to(ARCHIVE_DIR.resolve()):
        abort(403)
    if not video_path.exists() or video_path.suffix != ".mp4":
        abort(404)

    return send_file(video_path, mimetype="video/mp4")


@app.route("/download/<date_str>/<filename>")
def download_video(date_str: str, filename: str):
    """Download a video file."""
    if not safe_date(date_str) or not safe_filename(filename):
        abort(400)

    video_path = ARCHIVE_DIR / date_str / filename
    if not video_path.resolve().is_relative_to(ARCHIVE_DIR.resolve()):
        abort(403)
    if not video_path.exists() or video_path.suffix != ".mp4":
        abort(404)

    return send_file(video_path, as_attachment=True)


@app.route("/delete/<date_str>/<filename>", methods=["POST"])
def delete_video(date_str: str, filename: str):
    """Delete a single video file."""
    if not safe_date(date_str) or not safe_filename(filename):
        abort(400)

    video_path = ARCHIVE_DIR / date_str / filename
    if not video_path.resolve().is_relative_to(ARCHIVE_DIR.resolve()):
        abort(403)
    if not video_path.exists() or video_path.suffix != ".mp4":
        abort(404)

    video_path.unlink()
    record_retention_event(date_str, "deleted", 1)
    date_dir = ARCHIVE_DIR / date_str
    if date_dir.exists() and not any(date_dir.iterdir()):
        date_dir.rmdir()

    return redirect(url_for("by_date", date_str=date_str))


@app.route("/bulk/<date_str>", methods=["POST"])
def bulk_action(date_str: str):
    """Bulk delete or download selected videos."""
    if not safe_date(date_str):
        abort(400)

    action = request.form.get("action")
    filenames = request.form.getlist("selected")

    if not filenames:
        return redirect(url_for("by_date", date_str=date_str))

    for filename in filenames:
        if not safe_filename(filename):
            abort(400)
        if not (ARCHIVE_DIR / date_str / filename).resolve().is_relative_to(ARCHIVE_DIR.resolve()):
            abort(403)

    if action == "delete":
        deleted_count = 0
        for filename in filenames:
            p = ARCHIVE_DIR / date_str / filename
            if p.exists() and p.suffix == ".mp4":
                p.unlink()
                deleted_count += 1
        if deleted_count > 0:
            record_retention_event(date_str, "deleted", deleted_count)
        date_dir = ARCHIVE_DIR / date_str
        if date_dir.exists() and not any(date_dir.iterdir()):
            date_dir.rmdir()
        return redirect(url_for("by_date", date_str=date_str))

    elif action == "download":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename in filenames:
                p = ARCHIVE_DIR / date_str / filename
                if p.exists() and p.suffix == ".mp4":
                    zf.write(p, filename)
        buf.seek(0)
        return send_file(buf, as_attachment=True,
                         download_name=f"watchman_{date_str}.zip",
                         mimetype="application/zip")

    abort(400)


# ── Entry point ─────────────────────────────────────────────────────────────


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Watchman Web")
    parser.add_argument("--config", default="watchman.conf")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    CONFIG_PATH = args.config
    ARCHIVE_DIR = Path(cfg["ARCHIVE_DIR"])
    RETENTION_STATE_FILE = ARCHIVE_DIR / ".retention_state.json"
    RETENTION_HISTORY_FILE = ARCHIVE_DIR / ".retention_history.json"
    host = cfg.get("WEB_HOST", "0.0.0.0")
    port = args.port or int(cfg.get("WEB_PORT", "5000"))

    app.run(host=host, port=port)
