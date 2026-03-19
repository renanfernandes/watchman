---
title: "I Vibe Coded a Raspberry Pi to Replace a Subscription"
pubDate: 2026-03-19
description: "How I built Watchman: a Pi Zero 2 W USB gadget pipeline to archive Blink clips locally and unsubscribe from one more recurring fee"
author: "Renan"
image: "/images/posts/watchman-vibecode.jpg"
tags: ["raspberrypi", "python", "selfhosted", "blink", "homelab", "linux"]
---
Blink cameras are cheap (you can snap 2, or 3 cameras for less than 50 bucks on Sale at Amazon), they may not have the best quality of the world but get the job done. 
The problem is: Amazon keeps you on a Subscription jail if you want to access your recordings. And well, according to [Blink, or Amazon's Terms of Service](https://blinkforhome.com/terms-of-service): 
> You give us all permissions we need to use your Blink Recordings to do so. These permissions include, for example, the rights to copy your Blink Recordings, modify your Blink Recordings to generate clips, use information about your Blink Recordings to organize them on your behalf, and review your Blink Recordings to provide technical support.

Yeahhhh not sure how i feel about that.

Luckly there's a way around: You can buy a Blink Sync Module 2, store all your data locally and get rid of the Cloud/Subscription right? 
Not quite.. 1) The subscription is still required for instant playback anywhere, 2) you are still locked into the Amazon/Blink ecosystem and pretty dependent on their closed app, 3) you don't have a lot of control over the recordings and archiving, 4) sharing access with other users (spouse, child, friends) is a pain, and so go on.

So why not own my own footage, decide what to do with it, reduce the blast radius if something goes wrong and have one less subscription?

So, with an idea on my head and Github Copilot on my screen I decided to quickly write a solution that would fit my needs and hopefully, others ;)

The result is **Watchman**: a Raspberry Pi Zero 2 W that pretends to be a USB drive for Blink Sync Module 2, captures motion clips, archives them locally, and serves everything from a simple web UI.

Repository:

https://github.com/renanfernandes/watchman

Table of Contents
---

* [Why I Built This](#why-i-built-this)
* [Architecture Overview - Nerd Talk](#architecture-overview)
* [How USB Gadget Mode Makes This Work](#how-usb-gadget-mode-makes-this-work)
* [Ingest Pipeline (Step by Step)](#ingest-pipeline-step-by-step)
* [Web Layer and Safety Checks](#web-layer-and-safety-checks)
* [Service Model and Operations](#service-model-and-operations)
* [Tradeoffs and Next Steps](#tradeoffs-and-next-steps)

## Why I Built This
So, i started with one simple goal: stop renting access to my own data.

I did not want a huge NVR stack or a full blown solution like Unifi Protect. This is all running several thounsand miles from home and I wanted something cheap and reliable enough to just run.

This led to some requirements, like:
* Blink must always see valid USB storage
* Clips must be moved only after writes are finished
* Failures should recover automatically
* Browsing/downloading clips should be easy from the network. And if I want, expose it through a reverse proxy in the future.

The next sections will discuss what I implemented, how this was developed. Basically Nerd talk. If you just want to setup this on your own place head to my github repo and follow the setup instructions there: https://github.com/renanfernandes/watchman

## Architecture Overview - Nerd Talk

You can skip this whole section if you just want the solution. Again, Head toThe full data flow is:

Blink Camera -> Sync Module 2 -> Pi Zero 2 W (USB gadget mass storage) -> local archive -> Flask web UI

The control flow is split in two services:

* `watchman.py`: The brain. Handles USB gadget load/unload, mounting, clip ingestion, watchdog reset logic
* `web.py`: read-only-ish web interface for listing, streaming, and downloading archived clips

Supporting pieces:

* `watchman.conf`: centralized config (paths, timing, ports)
* `create_disk.sh`: creates/formats the file-backed USB container - Used during the setup process
* `setup.sh`: host setup + boot config + service install
* `watchman.service` and `watchman-web.service`: systemd service units

## How USB Gadget Mode Makes This Work

On Pi Zero, the USB OTG port can run in gadget mode. That means the Pi can present itself as a USB device, not only as a host. For this, you need a few teaks, such as enabling dwc2 and g_mass_storage.

* `dwc2` in boot config
* `g_mass_storage` at runtime, pointing to a file container (default `/ghostdrive.bin`)

At runtime the module is loaded with parameters like:

```bash
modprobe g_mass_storage file=/ghostdrive.bin removable=1 ro=0 stall=0 \
	idVendor=0x0781 idProduct=0x5571 iManufacturer=GhostDrive iProduct=USB_Drive
```

Using a file-backed container keeps the setup simple: easy to size, easy to mount via loop device, and easy to recreate if needed.

## Ingest Pipeline (Step by Step)

This is the core cycle in `watchman.py`:

1. Unload gadget module so Blink is temporarily disconnected.
2. Mount the container locally.
3. Find all `.mp4` files recursively.
4. Move clips to `ARCHIVE_DIR/YYYY-MM-DD`.
5. Handle filename collisions (`clip.mp4`, `clip_1.mp4`, etc.).
6. Unmount container.
7. Reload gadget module so Blink can continue writing.

Timing controls from config are important here:

* `SETTLE_TIME`: wait after writes settle before cycling
* `MIN_INTERVAL`: avoid ingest thrashing
* `WATCHDOG_THRESHOLD`: number of failures before USB reset path

When watchdog threshold is reached, Watchman does a fuller reset: unload gadget, toggle `dwc2`, reload gadget.

Observation: You may ask: Why are you disconnecting the module so frequently? Well, if you have a better solution, let me know. But the reason behind this was simply to avoid a situation where Blink is writting on the device while the script is making an operation. You may need to tweak the timing according to your needs.

## Web Layer and Safety Checks

The Flask app is intentionally straightforward:

* `/` shows available recording dates
* `/date/<date>` lists videos for a date
* `/video/<date>/<filename>` streams mp4
* `/download/<date>/<filename>` downloads mp4

I added basic but important protections:

* strict date pattern checks
* filename traversal filtering
* resolved path checks to ensure access stays inside archive root

It is built for trusted LAN usage, not as a fully hardened internet-facing media platform.

## Service Model and Operations

Both components run as systemd services and autostart on boot.

That gives me:

* restart behavior on failure
* simple status checks
* centralized logs in journald

Typical troubleshooting commands:

```bash
journalctl -u watchman -f
journalctl -u watchman-web -f
```


## Tradeoffs and Next Steps

Tradeoffs I accept today:

* single-node local archive
* LAN-focused security model
* lightweight indexing only

Next things I want to add:

* off-device replication (NAS/object storage)
* retention/cleanup policies
* Home Assistant integration
* basic health endpoint + metrics

This project removed one more recurring subscription from my life.

If a service gives me clear value, I will pay. If it charges rent on my own data, I would rather build.

Resist and unsubscribe.
