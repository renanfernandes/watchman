#!/usr/bin/env python3
"""
Watchman — Intercepts Blink Sync Module 2 motion clips.

Monitors a virtual USB drive (GhostDrive) for new .mp4 files,
moves them to a local archive, and reconnects the drive.

Usage:
    sudo python3 watchman.py                          # normal operation
    sudo python3 watchman.py --once --no-gadget       # test one cycle
    sudo python3 watchman.py --config /etc/watchman/watchman.conf
"""

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("watchman")


# ── Configuration ───────────────────────────────────────────────────────────

DEFAULTS = {
    "CONTAINER": "/ghostdrive.bin",
    "MOUNT_POINT": "/mnt/ghostdrive",
    "ARCHIVE_DIR": "/home/watchman/archive",
    "GADGET_MODULE": "g_mass_storage",
    "SETTLE_TIME": "30",
    "MIN_INTERVAL": "120",
    "WATCHDOG_THRESHOLD": "3",
}


def load_config(path: str) -> dict:
    """Read a KEY=VALUE config file. Lines starting with # are ignored."""
    config = dict(DEFAULTS)
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


# ── Shell helpers ───────────────────────────────────────────────────────────


def run(cmd: list[str], check=True, timeout=15):
    """Run a shell command, log it, and return the result."""
    log.debug("$ %s", " ".join(cmd))
    return subprocess.run(
        cmd, check=check, timeout=timeout, text=True, capture_output=True
    )


# ── USB gadget control ─────────────────────────────────────────────────────


def gadget_unload(module: str) -> bool:
    """Disconnect the virtual USB drive from the Blink module."""
    try:
        run(["modprobe", "-r", module])
        log.info("Gadget unloaded")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.error("Failed to unload gadget: %s", e)
        return False


def gadget_load(module: str, container: str) -> bool:
    """Reconnect the virtual USB drive so Blink sees it again."""
    try:
        run([
            "modprobe", module,
            f"file={container}",
            "removable=1",
            "ro=0",
            "stall=0",
            # Mimic a SanDisk Cruzer — known to work with Blink Sync Module 2.
            "idVendor=0x0781",
            "idProduct=0x5571",
            "iManufacturer=GhostDrive",
            "iProduct=USB_Drive",
        ])
        log.info("Gadget loaded")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.error("Failed to load gadget: %s", e)
        return False


def usb_reset(module: str, container: str) -> bool:
    """Full USB reset: unload everything, toggle dwc2, reload gadget."""
    log.warning("Watchdog triggered — performing full USB reset...")
    try:
        run(["modprobe", "-r", module], check=False)
        time.sleep(2)
        run(["modprobe", "-r", "dwc2"], check=False)
        time.sleep(2)
        run(["modprobe", "dwc2"])
        time.sleep(2)
        return gadget_load(module, container)
    except Exception as e:
        log.error("USB reset failed: %s", e)
        return False


# ── Mount / unmount ─────────────────────────────────────────────────────────


def mount_container(container: str, mount_point: str) -> bool:
    """Mount the container file locally (loop device)."""
    Path(mount_point).mkdir(parents=True, exist_ok=True)
    try:
        run(["mount", "-o", "loop", container, mount_point], timeout=20)
        log.info("Mounted %s at %s", container, mount_point)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.error("Mount failed: %s", e)
        return False


def unmount_container(mount_point: str) -> bool:
    """Unmount the container."""
    try:
        run(["umount", mount_point], check=False)
        log.info("Unmounted %s", mount_point)
        return True
    except Exception as e:
        log.error("Unmount failed: %s", e)
        return False


# ── File operations ─────────────────────────────────────────────────────────


def find_videos(mount_point: str) -> list[Path]:
    """Find all .mp4 files on the mounted drive."""
    root = Path(mount_point)
    if not root.exists():
        return []
    return list(root.rglob("*.mp4"))


def archive_video(src: Path, archive_dir: Path) -> bool:
    """Move a video file into the date-organized archive."""
    try:
        date_folder = archive_dir / datetime.now().strftime("%Y-%m-%d")
        date_folder.mkdir(parents=True, exist_ok=True)

        dest = date_folder / src.name
        counter = 1
        while dest.exists():
            dest = date_folder / f"{src.stem}_{counter}{src.suffix}"
            counter += 1

        shutil.move(str(src), str(dest))
        log.info("Archived: %s -> %s", src.name, dest)
        return True
    except Exception as e:
        log.error("Failed to archive %s: %s", src.name, e)
        return False


# ── Ingest cycle ────────────────────────────────────────────────────────────


def ingest(container: str, mount_point: str, archive_dir: str,
           module: str, no_gadget: bool) -> int:
    """
    Run one full ingest cycle:
      1. Unload gadget  (disconnect Blink's view of the drive)
      2. Mount container (local access to the files)
      3. Move .mp4 files to archive
      4. Unmount
      5. Reload gadget  (Blink sees the drive again)

    Returns number of files archived, or -1 on failure.
    """
    if not no_gadget:
        if not gadget_unload(module):
            return -1

    try:
        if not mount_container(container, mount_point):
            return -1

        videos = find_videos(mount_point)
        log.info("Found %d video(s)", len(videos))

        archived = 0
        for video in videos:
            if archive_video(video, Path(archive_dir)):
                archived += 1
        return archived

    finally:
        # Always clean up — unmount and reconnect gadget no matter what.
        unmount_container(mount_point)
        if not no_gadget:
            gadget_load(module, container)


# ── Main loop ───────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Watchman — Blink clip interceptor")
    parser.add_argument("--config", default="watchman.conf",
                        help="Path to config file")
    parser.add_argument("--once", action="store_true",
                        help="Run one ingest cycle and exit")
    parser.add_argument("--no-gadget", action="store_true",
                        help="Skip USB gadget operations (for testing)")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg = load_config(args.config)
    container = cfg["CONTAINER"]
    mount_point = cfg["MOUNT_POINT"]
    archive_dir = cfg["ARCHIVE_DIR"]
    module = cfg["GADGET_MODULE"]
    settle_time = int(cfg["SETTLE_TIME"])
    min_interval = int(cfg["MIN_INTERVAL"])
    watchdog_threshold = int(cfg["WATCHDOG_THRESHOLD"])

    # Must run as root for modprobe and mount.
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        log.error("Must run as root (sudo)")
        return 1

    if not Path(container).exists():
        log.error("Container not found: %s  (run setup.sh first)", container)
        return 1

    # Graceful shutdown on SIGTERM / SIGINT.
    running = True

    def handle_signal(sig, _frame):
        nonlocal running
        log.info("Shutting down (signal %s)...", sig)
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log.info("Watchman starting")
    log.info("Container:  %s", container)
    log.info("Archive:    %s", archive_dir)
    log.info("Settle: %ds | Min interval: %ds | Watchdog after: %d failures",
             settle_time, min_interval, watchdog_threshold)

    # Load gadget on startup so the USB drive is visible immediately.
    if not args.no_gadget:
        if not gadget_load(module, container):
            log.error("Failed to load gadget on startup")
            return 1
        log.info("USB drive is now visible to the connected device")

    # ── One-shot mode ───────────────────────────────────────────────────
    if args.once:
        result = ingest(container, mount_point, archive_dir, module, args.no_gadget)
        log.info("Done. Archived %d file(s)", max(0, result))
        return 0

    # ── Continuous loop ─────────────────────────────────────────────────
    last_mtime = Path(container).stat().st_mtime
    last_change = 0.0
    last_cycle = 0.0
    consecutive_failures = 0
    cycle_pending = False

    while running:
        try:
            now = time.time()
            current_mtime = Path(container).stat().st_mtime

            # Detect new writes to the container.
            if current_mtime != last_mtime:
                last_mtime = current_mtime
                last_change = now
                cycle_pending = True
                log.info("Write detected — waiting for activity to settle...")

            # Writes have settled and enough time has passed — run a cycle.
            if cycle_pending and (now - last_change) >= settle_time:
                if (now - last_cycle) < min_interval:
                    log.debug("Too soon for next cycle (min interval: %ds)", min_interval)
                    time.sleep(5)
                    continue

                result = ingest(container, mount_point, archive_dir,
                                module, args.no_gadget)
                last_cycle = time.time()
                cycle_pending = False

                if result >= 0:
                    consecutive_failures = 0
                    if result > 0:
                        log.info("Cycle done: %d file(s) archived", result)
                    else:
                        log.info("Cycle done: no new files")
                else:
                    consecutive_failures += 1
                    log.warning("Cycle failed (%d/%d before watchdog reset)",
                                consecutive_failures, watchdog_threshold)

                    if consecutive_failures >= watchdog_threshold:
                        usb_reset(module, container)
                        consecutive_failures = 0

                # Refresh mtime so we don't immediately re-trigger.
                last_mtime = Path(container).stat().st_mtime

            time.sleep(5)

        except FileNotFoundError:
            log.error("Container disappeared: %s", container)
            time.sleep(10)
        except Exception as e:
            log.exception("Unexpected error: %s", e)
            time.sleep(10)

    log.info("Watchman stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
