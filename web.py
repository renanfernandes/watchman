#!/usr/bin/env python3
"""
Watchman Web — Browse, play, and download archived Blink clips.

Usage:
    python3 web.py
    python3 web.py --config /etc/watchman/watchman.conf --port 8080
"""

import argparse
from pathlib import Path
from flask import Flask, render_template, send_file, abort

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
    """Get all recording dates with video counts, newest first."""
    if not ARCHIVE_DIR.exists():
        return []
    dates = []
    for folder in sorted(ARCHIVE_DIR.iterdir(), reverse=True):
        if folder.is_dir():
            count = sum(1 for f in folder.iterdir() if f.suffix == ".mp4")
            dates.append({"name": folder.name, "count": count})
    return dates


def safe_date(date_str: str) -> bool:
    """Validate date string is exactly YYYY-MM-DD format."""
    return (
        len(date_str) == 10
        and all(c in "0123456789-" for c in date_str)
    )


def safe_filename(filename: str) -> bool:
    """Block path traversal in filenames."""
    return ".." not in filename and "/" not in filename and "\\" not in filename


# ── Routes ──────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    """Landing page — list all recording dates."""
    return render_template("index.html",
                           dates=list_dates(), current_date=None, videos=[])


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

    videos = sorted(f.name for f in date_dir.iterdir() if f.suffix == ".mp4")
    return render_template("index.html",
                           dates=list_dates(), current_date=date_str, videos=videos)


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
