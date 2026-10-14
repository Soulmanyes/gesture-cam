#!/bin/sh
# One-time setup: install v4l2loopback and create the "Gesture Cam" virtual camera
# at /dev/video10, now and on every boot. Run with sudo.
set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo: sudo $0" >&2
    exit 1
fi

pacman -S --needed --noconfirm v4l2loopback-dkms

# exclusive_caps=1 is what makes Chromium/Electron apps (Discord, Vesktop) list the device.
echo 'options v4l2loopback video_nr=10 card_label="Gesture Cam" exclusive_caps=1' > /etc/modprobe.d/v4l2loopback.conf
echo 'v4l2loopback' > /etc/modules-load.d/v4l2loopback.conf

modprobe -r v4l2loopback 2>/dev/null || true
modprobe v4l2loopback
ls -l /dev/video10
echo "Gesture Cam virtual camera is ready."
