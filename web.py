#!/usr/bin/env python3
"""
Watchman Web — Browse, play, and download archived Blink clips.

Usage:
    python3 web.py
    python3 web.py --config /etc/watchman/watchman.conf --port 8080
"""

import argparse
import json
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta, timezone as _tz
from pathlib import Path
from zoneinfo import ZoneInfo as _ZoneInfo
from flask import Flask, render_template, send_file, abort, request, redirect, url_for, jsonify
import io
import shutil
import zipfile

def _local_tz():
    tz_file = Path("/etc/timezone")
    if tz_file.exists():
        return _ZoneInfo(tz_file.read_text().strip())
    return _ZoneInfo("UTC")

_LOCAL_TZ = _local_tz()

app = Flask(__name__)
ARCHIVE_DIR = Path("/home/watchman/archive")
CONFIG_PATH = "watchman.conf"
RETENTION_STATE_FILE = Path("/tmp/watchman_retention_state.json")
RETENTION_HISTORY_FILE = Path("/tmp/watchman_retention_history.json")


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


def safe_date(date_str: str) -> bool:
    """Validate date string is exactly YYYY-MM-DD format."""
    return (
        len(date_str) == 10
        and all(c in "0123456789-" for c in date_str)
    )


def safe_filename(filename: str) -> bool:
    """Block path traversal in filenames."""
    return ".." not in filename and "/" not in filename and "\\" not in filename


def parse_video_meta(filename: str) -> dict:
    """Extract time and camera name from Blink filename (HH-MM-SS_Camera_NNN.mp4)."""
    stem = Path(filename).stem  # e.g. "13-38-41_DoorbellFront_001"
    parts = stem.split("_", 2)
    time_str = ""
    camera = ""
    if len(parts) >= 1:
        t = parts[0]  # "13-38-41"
        if len(t) == 8 and t[2] == "-" and t[5] == "-" and t.replace("-", "").isdigit():
            utc_dt = _datetime(2000, 1, 1, int(t[0:2]), int(t[3:5]), int(t[6:8]), tzinfo=_tz.utc)
            time_str = utc_dt.astimezone(_LOCAL_TZ).strftime("%H:%M:%S")
    if len(parts) >= 2:
        camera = parts[1].replace("-", " ").replace("_", " ")
    return {"name": filename, "time": time_str, "camera": camera}


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

    videos = sorted(
        (parse_video_meta(f.name) for f in date_dir.iterdir() if f.suffix == ".mp4"),
        key=lambda v: v["name"],
        reverse=True,
    )
    return render_template("index.html",
                           dates=list_dates(), current_date=date_str, videos=videos,
                           dates_map=json.dumps(calendar_status_map()),
                           settings=settings,
                           status_message=request.args.get("msg", ""))


@app.route("/settings")
def settings_page():
    """Show retention settings page."""
    settings = retention_settings()
    estimate = retention_cleanup_estimate(settings["days"])
    return render_template(
        "settings.html",
        settings=settings,
        estimate=estimate,
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
