![commit](https://img.shields.io/github/last-commit/renanfernandes/watchman)
# Watchman

Watchman is a elegant solution to a problem I had: How to properly access and manage the recordings from my Blink Cameras without an active subscription? 
Watchman makes a Raspberry Pi (in my case the Zero 2 W) acts as a virtual USB drive ("GhostDrive") for a
Blink Sync Module 2. Intercepts motion-triggered `.mp4` clips and archives them
locally for remote access — no Blink subscription needed.

## How It Works

```
Blink Camera → Sync Module 2 → [Pi Zero 2W as USB Drive] → Archive → Web UI
```

1. The Pi presents itself as a USB flash drive to the Blink Sync Module
2. Blink writes motion clips (`.mp4`) to the "drive"
3. Watchman detects the writes, briefly disconnects the drive, moves the files
   to a local archive, and reconnects
4. A web interface lets you browse, play, and download clips from anywhere on
   your network

## What's in the Box

| File | Purpose |
|---|---|
| `watchman.py` | Main service — detects, ingests, and archives clips |
| `web.py` | Web interface — browse/play/download archived videos |
| `templates/index.html` | Web UI template |
| `watchman.conf` | All configuration in one place |
| `create_disk.sh` | Creates the 6GB exFAT virtual disk |
| `setup.sh` | Full automated setup (deps, boot, disk, services) |
| `watchman.service` | systemd unit for the ingest service |
| `watchman-web.service` | systemd unit for the web interface |

## Hardware

- Raspberry Pi Zero 2 W (or any Raspberry Pi, honestly)
- MicroSD card (16GB+ recommended)
- USB data cable (micro-USB to USB-A) connecting Pi to Blink Sync Module 2
- Power supply for the Pi

**Important:** Connect the Pi's **data** micro-USB port (not the power port) to
the Blink Sync Module's USB port.

## Quick Setup

```bash
# 1. Clone this repo onto your Pi
git clone <your-repo-url> ~/watchman
cd ~/watchman

# 2. Run the setup script
sudo bash setup.sh

# 3. Reboot to activate USB gadget mode
sudo reboot
```

After reboot, both services start automatically:
- **Watchman** monitors and archives clips
- **Web UI** available at `http://<pi-ip>:5000`

## Boot Configuration (What setup.sh Does)

The Pi needs two boot file changes to act as a USB gadget device. `setup.sh`
handles this automatically, but here's exactly what it does:

### `/boot/config.txt` (or `/boot/firmware/config.txt` on Bookworm)

Adds this line to enable the **dwc2** USB controller overlay:

```
dtoverlay=dwc2
```

This tells the Pi's hardware to use the DesignWare USB 2.0 controller in
"gadget mode" — meaning the Pi can pretend to be a USB device (like a flash
drive) instead of being a USB host.

### `/boot/cmdline.txt` (or `/boot/firmware/cmdline.txt` on Bookworm)

Adds `modules-load=dwc2` after `rootwait`:

```
... rootwait modules-load=dwc2 ...
```

This loads the dwc2 kernel module at boot so gadget mode is available
immediately when Watchman starts.

### Why These Changes?

By default, the Pi Zero 2 W's USB port works in host mode (for keyboards,
mice, etc.). Gadget mode flips it around so the Pi *itself* appears as a USB
device to whatever it's plugged into — in our case, the Blink Sync Module 2
sees a "USB flash drive."

## Manual Setup (Step by Step)

If you prefer to set things up manually instead of using `setup.sh`:

### 1. Install dependencies

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-flask exfatprogs
```

### 2. Edit boot files

```bash
# Add to /boot/config.txt (or /boot/firmware/config.txt):
echo "dtoverlay=dwc2" | sudo tee -a /boot/config.txt

# Add modules-load=dwc2 to cmdline.txt:
sudo sed -i 's/rootwait/rootwait modules-load=dwc2/' /boot/cmdline.txt
```

### 3. Create the virtual disk

```bash
sudo bash create_disk.sh
```

Or manually:

```bash
sudo dd if=/dev/zero of=/ghostdrive.bin bs=1M count=6144 status=progress
sudo mkfs.exfat -n GHOSTDRIVE /ghostdrive.bin
```

### 4. Test it

```bash
# Load the gadget module manually
sudo modprobe g_mass_storage file=/ghostdrive.bin removable=1 ro=0 stall=0

# Plug the Pi into the Blink Sync Module — it should recognize a USB drive

# Test one ingest cycle (without touching the gadget)
sudo python3 watchman.py --once --no-gadget --verbose
```

### 5. Install services

```bash
sudo cp watchman.service watchman-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now watchman.service watchman-web.service
```

## Configuration

All settings live in `watchman.conf` (installed to `/etc/watchman/watchman.conf`):

| Setting | Default | Description |
|---|---|---|
| `CONTAINER` | `/ghostdrive.bin` | Path to the virtual disk file |
| `CONTAINER_SIZE_MB` | `6144` | Disk size (only used during creation) |
| `MOUNT_POINT` | `/mnt/ghostdrive` | Temporary mount location |
| `ARCHIVE_DIR` | `/home/pi/archive` | Where videos are permanently stored |
| `GADGET_MODULE` | `g_mass_storage` | Kernel module name |
| `SETTLE_TIME` | `30` | Seconds to wait after last write before cycling |
| `MIN_INTERVAL` | `120` | Minimum seconds between ingest cycles |
| `WATCHDOG_THRESHOLD` | `3` | Failures before attempting USB reset |
| `WEB_HOST` | `0.0.0.0` | Web server bind address |
| `WEB_PORT` | `5000` | Web server port |

## Web Interface

Browse to `http://<pi-ip>:5000` to:

- **Browse** recordings by date
- **Play** clips directly in your browser
- **Download** individual clips

## Checking Logs

```bash
# Watchman ingest service
sudo journalctl -u watchman -f

# Web interface
sudo journalctl -u watchman-web -f
```

## Troubleshooting

**Blink doesn't recognize the drive:**
- Make sure you're using the Pi's **data** USB port (not power)
- Verify gadget module is loaded: `lsmod | grep g_mass_storage`
- Check dwc2 is loaded: `lsmod | grep dwc2`
- Try manually: `sudo modprobe g_mass_storage file=/ghostdrive.bin removable=1 ro=0 stall=0`

**Mount fails:**
- Check the container exists: `ls -la /ghostdrive.bin`
- Verify exFAT support: `sudo mount -o loop /ghostdrive.bin /mnt/ghostdrive`
- Recreate if corrupt: `sudo bash create_disk.sh`

**Service won't start:**
- Check logs: `sudo journalctl -u watchman -n 50`
- Verify config: `cat /etc/watchman/watchman.conf`
- Test manually: `sudo python3 /opt/watchman/watchman.py --once --verbose`

## Future Plans

- Ship recordings to a remote storage server 
- Home Assistant integration (notifications + video feed)

## License
MIT
