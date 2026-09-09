# Installation

[Back to README](../README.md) | [Configuration](configuration.md) | [Operations](operations.md)

## Prerequisites

- Raspberry Pi Zero 2 W is the reference setup. Use Raspberry Pi OS with systemd, `apt`, and Python 3.9 or later.
- Blink Sync Module 2 with local USB recording available, and Blink cameras compatible with that feature.
- MicroSD storage with room for the default 6 GiB disk image, the OS, and accumulated clips. A 16 GB card is a starting point, not a retention capacity guarantee.
- A USB data cable, suitable Pi power supply, network access, SSH, Git, and sudo access.
- Internet access for package installation. The enabled-by-default network watchdog also expects internet connectivity.

Watchman does not install on macOS. A Mac can be the SSH workstation, while the application and system services run on the Pi.

## Wiring

For the Zero 2 W, use the labels printed on the board:

| Pi port | Connection |
| --- | --- |
| `PWR IN` | Suitable power supply |
| `USB` | USB-A port on the Blink Sync Module 2, using a data cable |

The Sync Module needs its own power adapter. Do not use the Pi's power-only port for the data connection.

### Other Pi Models

Verify the exact board, operating system, USB controller, and port support device/gadget mode before purchasing hardware. The project's Pi 5 setup has used GPIO/HAT power to leave USB-C available for the Blink data link; that is a deployment-specific arrangement, not a universal wiring prescription.

Use the power/HAT manufacturer's guidance, including the specified battery count and voltage. Do not assume a USB splitter or isolator solves a power-negotiation problem. Earlier project notes attributed some boot hangs to power interaction, but those explanations and proposed isolators have not been independently validated here. Diagnose the actual board and supply before changing wiring.

## Install on the Pi

Log in as the intended service user, not directly as root. Setup selects `SUDO_USER`, falling back to `watchman` when it is absent.

```bash
git clone https://github.com/renanfernandes/watchman.git "$HOME/watchman"
cd "$HOME/watchman"
sudo bash setup.sh
```

A fresh GitHub clone has only the example config, which setup copies to `/etc/watchman/watchman.conf`. If a local `watchman.conf` exists, setup prefers it. Existing installed configuration and an existing disk image are preserved.

Before rebooting:

```bash
sudo nano /etc/watchman/watchman.conf
sudo chown root:"$(id -gn)" /etc/watchman/watchman.conf
sudo chmod 660 /etc/watchman/watchman.conf
test -w /etc/watchman/watchman.conf && echo "Config is writable"
sudo reboot
```

These permissions let the installing user, which setup selects for the web service, save settings without making credentials world-readable. For a different service user/group, use that user's actual group instead. A customized `SERVICE_USER` in an existing config does not override setup's choice of invoking user; use the same account consistently.

## System Changes

Setup performs more than application installation:

- Installs Python, Flask, exFAT/partition utilities, FFmpeg and watchdog packages.
- Installs application files under `/opt/watchman` and configuration under `/etc/watchman`.
- Creates the virtual disk if absent, plus clip and thumbnail directories.
- Adds `dtoverlay=dwc2,dr_mode=peripheral` in the boot configuration and `modules-load=dwc2` in the kernel command line as needed. Existing overlay entries can affect what is changed; inspect them when diagnosing boot problems.
- Enables the application services and installs scoped passwordless restart permissions for the web service user.
- Enables persistent system logs and disables NetworkManager Wi-Fi power saving.
- Configures and starts the hardware watchdog, and starts the network watchdog when enabled.
- Adds a root cron entry to reboot on the first day of each month at 03:00 local time.

> [!WARNING]
> Review these changes before running setup on a Pi used for other workloads. Setup is not a dry run and may replace system watchdog configuration. Keep a system backup and arrange a maintenance window.

For offline networks, review `NET_WATCHDOG_ENABLED` before installation by preparing a local config, or stop the network watchdog while configuring it. See [operations](operations.md#watchdogs-and-reboots).

The tracked [first-install wrapper](../scripts/first-time-install.sh) simply invokes setup when installation markers are absent. It is not a remote deployment tool. Run setup from the complete repository; copying that script alone elsewhere breaks its relative file paths.

## Validate After Reboot

**On the Pi:**

```bash
systemctl is-active watchman watchman-web
systemctl show watchman-web --property=User --value
sudo journalctl -u watchman -n 50 --no-pager
sudo journalctl -u watchman-web -n 50 --no-pager
curl --fail --max-time 30 -o /dev/null http://127.0.0.1:5000/starred
```

Use the configured port if different. Check that Blink recognizes the USB drive, trigger a test recording, and verify it appears after the configured settle/minimum-interval delays. Test playback and saving a setting from the browser. Inspect logs privately before sharing excerpts.

For recovery, follow [troubleshooting](operations.md#troubleshooting). Do not mount the backing image locally while Blink or the ingest service is using it.
