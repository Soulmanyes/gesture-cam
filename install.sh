#!/bin/sh
# Installs Gesture Cam for the current user, or updates it when run again:
#   - the first time, runs setup.sh with sudo (libraries, v4l2loopback and the virtual camera)
#   - creates the Python environment in .venv and installs its packages
#   - downloads MediaPipe's models and adds Gesture Cam to the app menu
# Without cloning the repo (installs to ~/.local/share/gesture-cam):
#   curl -fsSL https://raw.githubusercontent.com/Soulmanyes/gesture-cam/main/install.sh | sh
# The whole script is one { } block, so a download cut off midway runs nothing.
{
set -eu

SRC=$(cd "$(dirname "$0")" && pwd)
APPS=${XDG_DATA_HOME:-$HOME/.local/share}/applications

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

# run through curl | sh: the app isn't next to the script, so download it into its own folder.
# Unpacking over an earlier install keeps its .venv and models.
if [ ! -f "$SRC/gesture_cam_gui.py" ]; then
    dest=${XDG_DATA_HOME:-$HOME/.local/share}/gesture-cam
    mkdir -p "$dest"
    say "Downloading Gesture Cam to $dest"
    curl -fsSL https://github.com/Soulmanyes/gesture-cam/archive/refs/heads/main.tar.gz |
        tar -xz -C "$dest" --strip-components=1
    exec sh "$dest/install.sh" "$@" < /dev/tty
fi
cd "$SRC"

# Once the camera exists, setup is done. Running it again would overwrite the camera's
# modprobe config, along with any changes made to it since.
if ! grep -qs 'Gesture Cam' /etc/modprobe.d/v4l2loopback.conf || [ ! -e /dev/video10 ]; then
    say "Installing libraries and the virtual camera (asks for your password)"
    sudo sh ./setup.sh
fi

if [ ! -x .venv/bin/python ]; then
    say "Creating the Python environment"
    # system site-packages provides the GTK bindings
    python3 -m venv --system-site-packages .venv
fi
say "Installing the Python packages"
.venv/bin/pip install -q -r requirements.txt

sh ./get-models.sh

say "Adding Gesture Cam to the app menu"
mkdir -p "$APPS"
sed "s#^Exec=.*#Exec=$SRC/gesture-cam#" gesture-cam.desktop > "$APPS/gesture-cam.desktop"

say "Done. Open \"Gesture Cam\" from your app menu."
echo "Keep $SRC where it is: the app menu entry points into it."
}
