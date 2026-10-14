# Gesture Cam

Webcam effects for Discord, triggered by hand gestures. The effects play behind you:
the person in the frame is cut out with MediaPipe's selfie segmenter, so they never cover your face.

| Gesture (hold ~⅓ s) | Effect    |
|---------------------|-----------|
| 👍 thumbs up        | fireworks |
| ✌️ peace sign        | confetti  |
| 🤟 "I love you"     | hearts    |

## Setup (once)

```sh
sudo ./setup-loopback.sh
```

This installs `v4l2loopback-dkms` and creates the virtual camera `/dev/video10`,
named "Gesture Cam". It also adds config so the camera comes back after every reboot.

Then create the Python environment and add the app to your launcher:

```sh
python3 -m venv --system-site-packages .venv   # system site-packages provides the GTK bindings
.venv/bin/pip install -r requirements.txt
cp gesture-cam.desktop ~/.local/share/applications/   # edit Exec= if the repo is not in ~/claude/gesture-cam
```

The window needs `python-gobject`, `gtk4` and `libadwaita` from pacman.

## Use

1. Make sure Discord or Vesktop is not using your webcam. Turn your camera off there, or switch it away from the webcam.
   Gesture Cam needs the real webcam for itself.
2. Open **Gesture Cam** from your app launcher. It turns the camera on straight away.
   Keep the window open while you use it: closing it turns the camera off.
3. In Discord or Vesktop, go to Settings > Voice & Video > Camera and pick **Gesture Cam**.
   It only shows up while Gesture Cam is on. Restart Discord the first time, so it sees the new device.

## Settings

The window has a section for each effect:

- **Enabled**: turns the gesture on or off.
- **Keep going while the gesture is held**: the effect plays for as long as you hold the gesture.
  When this is off, the effect plays for **Duration** seconds instead.
- **Amount**: how many rockets, confetti pieces or hearts each burst has.
- **Frequency**: bursts per second while the effect plays.
- **Test**: plays the effect now, without the gesture.

The **Webcam** menu picks which camera to use. Changes apply immediately and are saved to
`~/.config/gesture-cam/settings.json`.

## Without the window

`./gesture-cam --headless` runs the camera without a window, using the saved settings. Stop it with Ctrl+C.

Effects can also be fired from a hotkey, in either mode:

```sh
pkill -USR1 -f 'python gesture_cam'   # fireworks
pkill -USR2 -f 'python gesture_cam'   # confetti
```

## Files

- `gesture_cam_gui.py`: the window (GTK 4 / libadwaita).
- `gesture_cam.py`: the camera and effects engine, also runnable on its own.
- `gesture-cam`: the launcher script. `gesture-cam.desktop` is the app menu entry.
- `models/`: MediaPipe models (`gesture_recognizer.task`, `selfie_segmenter.tflite`).
- `.venv/`: the Python environment (mediapipe, opencv-contrib-python, numpy, pyvirtualcam).
  It also uses the system's GTK bindings (`python-gobject`).
