#!/usr/bin/env python3
"""
Watchman Web — Browse, play, and download archived Blink clips.

Usage:
    python3 web.py
    python3 web.py --config /etc/watchman/watchman.conf --port 8080
"""

import argparse
import json
from datetime import date as _date
from pathlib import Path
from flask import Flask, render_template, send_file, abort, request, redirect, url_for
import io
import zipfile

app = Flask(__name__)
ARCHIVE_DIR = Path("/home/watchman/archive")


def load_config(path: str) -> dict:
    """Read config file for archive dir and port."""
    config = {"ARCHIVE_DIR": "/home/watchman/archive", "WEB_HOST": "0.0.0.0", "WEB_PORT": "5000"}
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
            time_str = t.replace("-", ":")
    if len(parts) >= 2:
        camera = parts[1].replace("-", " ").replace("_", " ")
    return {"name": filename, "time": time_str, "camera": camera}


# ── Routes ──────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    """Redirect to today's recordings if they exist, otherwise show landing page."""
    today = _date.today().strftime("%Y-%m-%d")
    if (ARCHIVE_DIR / today).is_dir():
        return redirect(url_for("by_date", date_str=today))
    return render_template("index.html",
                           dates=list_dates(), current_date=None, videos=[],
                           dates_map=json.dumps(all_dates_map()))


@app.route("/date/<date_str>")
def by_date(date_str: str):
    """List videos for a specific date."""
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
                           dates_map=json.dumps(all_dates_map()))


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
        for filename in filenames:
            p = ARCHIVE_DIR / date_str / filename
            if p.exists() and p.suffix == ".mp4":
                p.unlink()
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
    ARCHIVE_DIR = Path(cfg["ARCHIVE_DIR"])
    host = cfg.get("WEB_HOST", "0.0.0.0")
    port = args.port or int(cfg.get("WEB_PORT", "5000"))

    app.run(host=host, port=port)
