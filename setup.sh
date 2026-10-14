#!/bin/sh
# One-time setup, run with sudo: installs the window's libraries and v4l2loopback, then creates the
# "Gesture Cam" virtual camera at /dev/video10, now and on every boot.
# Works on Arch (and Arch-based distros), Debian 13+, Ubuntu 24.04+ and Fedora 40+.
set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo: sudo $0" >&2
    exit 1
fi

. /etc/os-release
family="$ID $ID_LIKE"
case "$family" in
    *arch*)
        pacman -S --needed --noconfirm python-gobject gtk4 libadwaita v4l2loopback-dkms
        ;;
    *debian*|*ubuntu*)
        apt-get update
        apt-get install -y python3-venv python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
            v4l2loopback-dkms "linux-headers-$(uname -r)"
        ;;
    *fedora*)
        # v4l2loopback isn't in Fedora itself; it comes from RPM Fusion (free), which this enables.
        if ! rpm -q rpmfusion-free-release >/dev/null 2>&1; then
            echo "Enabling the RPM Fusion (free) repository for v4l2loopback..."
            dnf install -y "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm"
        fi
        dnf install -y python3-gobject gtk4 libadwaita akmod-v4l2loopback
        # akmods builds the module in the background after install; build it now so modprobe finds it.
        akmods --force --kernels "$(uname -r)"
        ;;
    *)
        echo "Sorry, $PRETTY_NAME isn't supported. Gesture Cam works on Arch, Debian 13+, Ubuntu 24.04+ and Fedora 40+." >&2
        exit 1
        ;;
esac

# exclusive_caps=1 is what makes Chromium/Electron apps (Discord, Vesktop) list the device.
echo 'options v4l2loopback video_nr=10 card_label="Gesture Cam" exclusive_caps=1' > /etc/modprobe.d/v4l2loopback.conf
echo 'v4l2loopback' > /etc/modules-load.d/v4l2loopback.conf

modprobe -r v4l2loopback 2>/dev/null || true
if ! modprobe v4l2loopback; then
    if command -v mokutil >/dev/null 2>&1 && mokutil --sb-state 2>/dev/null | grep -q enabled; then
        echo >&2
        echo "Secure Boot is on, and it blocked the camera driver because its signing key isn't trusted yet." >&2
        case "$family" in
            *fedora*) echo "Run: sudo mokutil --import /etc/pki/akmods/certs/public_key.der" >&2
                      echo "and choose a one-time password." >&2 ;;
            *) # Ubuntu's DKMS key lives with shim-signed; Debian's in /var/lib/dkms.
               key=/var/lib/dkms/mok.pub
               [ -f /var/lib/shim-signed/mok/MOK.der ] && key=/var/lib/shim-signed/mok/MOK.der
               echo "Run: sudo mokutil --import $key" >&2
               echo "and choose a one-time password." >&2 ;;
        esac
        echo "Then reboot. A blue screen (MOK manager) appears: pick 'Enroll MOK', 'Continue', 'Yes'," >&2
        echo "type that password and reboot. After that, run this script again." >&2
    fi
    exit 1
fi
ls -l /dev/video10
echo "Gesture Cam virtual camera is ready."
