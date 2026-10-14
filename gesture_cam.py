#!/usr/bin/env python3
"""Gesture Cam: webcam -> hand gestures -> effects behind you -> virtual camera.

Gestures (hold for a moment):
  thumbs up     -> fireworks
  peace sign    -> confetti
  rock-on / ILY -> hearts

How much, how often and how long each effect plays is set in the GUI
(gesture_cam_gui.py) and saved to ~/.config/gesture-cam/settings.json.
Running this file directly starts the camera without a window, using those settings.

Pick "Gesture Cam" as the camera in Discord. Effects can also be forced with
signals: SIGUSR1 = fireworks, SIGUSR2 = confetti (handy for hotkeys/testing).
"""

import argparse
import copy
import json
import math
import os
import random
import signal
import sys
import threading
import time
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
import pyvirtualcam
from mediapipe.tasks.python import BaseOptions, vision

HERE = Path(__file__).resolve().parent
OUTPUT = "/dev/video10"
WIDTH, HEIGHT, FPS = 1280, 720, 30
SETTINGS_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "gesture-cam/settings.json"

GESTURE_EFFECTS = {"Thumb_Up": "fireworks", "Victory": "confetti", "ILoveYou": "hearts"}
HOLD_FRAMES = 5        # consecutive recognitions needed before an effect fires
GESTURE_EVERY = 2      # run the gesture model on every Nth frame (15/s is plenty)
MIN_SCORE = 0.6
HELD_GRACE_S = 0.4     # a held gesture missed for this long still counts as held
MANUAL_HELD_S = 3.0    # how long a test/hotkey show lasts for an effect in "while held" mode
TRAIL_DECAY = 0.72     # how much of last frame's sparks stays, for firework streaks
GRAVITY = 260.0        # px/s^2, in effect-layer pixels
MAX_PARTICLES = 30000  # beyond this new bursts are skipped, to keep the frame rate up
SPARK_KERNEL = np.ones((3, 3), np.uint8)

# Per effect: what the "amount" slider means and its range, plus the defaults.
EFFECTS = {
    "fireworks": {"gesture": "👍 Thumbs up", "amount_label": "Rockets per burst", "amount_range": (1, 20)},
    "confetti": {"gesture": "✌️ Peace sign", "amount_label": "Pieces per burst", "amount_range": (20, 600)},
    "hearts": {"gesture": "🤟 I love you", "amount_label": "Hearts per burst", "amount_range": (5, 100)},
}
RATE_RANGE = (0.2, 8.0)       # bursts per second
DURATION_RANGE = (0.5, 20.0)  # seconds

DEFAULT_SETTINGS = {
    "camera": "/dev/v4l/by-id/usb-046d_HD_Pro_Webcam_C920-video-index0",
    "effects": {
        "fireworks": {"enabled": True, "while_held": True, "amount": 4, "rate": 3.0, "duration": 3.0},
        "confetti": {"enabled": True, "while_held": False, "amount": 220, "rate": 1.0, "duration": 1.0},
        "hearts": {"enabled": True, "while_held": False, "amount": 28, "rate": 1.0, "duration": 1.0},
    },
}

PALETTE = [(255, 80, 80), (80, 200, 255), (80, 255, 120), (255, 220, 60),
           (255, 120, 255), (255, 160, 60), (180, 120, 255), (255, 255, 255)]  # BGR-ish brights


def load_settings():
    """Saved settings merged over the defaults, so new keys always exist."""
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return settings
    if isinstance(saved.get("camera"), str):
        settings["camera"] = saved["camera"]
    for name, cfg in saved.get("effects", {}).items():
        if name in settings["effects"] and isinstance(cfg, dict):
            settings["effects"][name].update(cfg)
    return settings


def save_settings(settings):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2))
    tmp.replace(SETTINGS_FILE)


def list_cameras():
    """(device path, friendly name) for each real webcam, using stable by-id paths."""
    cams = []
    for link in sorted(Path("/dev/v4l/by-id").glob("*-video-index0")):
        name = link.name.removeprefix("usb-").removesuffix("-video-index0")
        name = name.split("_", 1)[-1].replace("_", " ") if "_" in name else name
        cams.append((str(link), name))
    return cams


class Particles:
    """Small numpy particle system. Kinds: 0 rocket, 1 spark, 2 confetti, 3 heart."""

    def __init__(self):
        self.p = np.zeros((0, 11), np.float32)  # x y vx vy life max_life r g b size kind

    def add(self, rows):
        if rows:
            self.add_array(np.array(rows, np.float32))

    def add_array(self, arr):
        if len(self.p) < MAX_PARTICLES:
            self.p = np.vstack([self.p, arr])

    def __len__(self):
        return len(self.p)

    def step(self, dt, w, h):
        p = self.p
        if not len(p):
            return
        kind = p[:, 10]
        grav = np.where(kind == 1, GRAVITY * 0.6,
               np.where(kind == 0, GRAVITY * 0.35,
               np.where(kind == 2, GRAVITY * 0.15, -GRAVITY * 0.05)))
        p[:, 3] += grav * dt
        drag = np.where(kind == 1, 0.985, np.where(kind == 2, 0.97, 1.0))
        p[:, 2:4] *= drag[:, None] ** (dt * 60)
        # confetti and hearts sway sideways
        sway = (kind >= 2) * np.sin(p[:, 4] * 4 + p[:, 0] * 0.05) * 40
        p[:, 0] += (p[:, 2] + sway) * dt
        p[:, 1] += p[:, 3] * dt
        p[:, 4] -= dt

        # rockets burst when they stop rising or run out of fuel
        burst = (kind == 0) & ((p[:, 3] > -20) | (p[:, 4] <= 0))
        for x, y, *_rest, r, g, b, _s, _k in p[burst]:
            self.add_array(spark_burst(x, y, (r, g, b)))
        p = self.p[: len(p)]  # sparks were appended after the existing rows
        keep = ~burst & (p[:, 4] > 0) & (p[:, 1] < h + 40) & (p[:, 1] > -h)
        self.p = np.vstack([p[keep], self.p[len(p):]])

    def draw(self, color, alpha, sparks):
        """Draw either the rockets/sparks (sparks=True) or the confetti/hearts."""
        if sparks:
            self._draw_sparks(color, alpha)
            return
        for x, y, _vx, _vy, life, max_life, r, g, b, size, kind in self.p:
            if kind <= 1:
                continue
            fade = min(1.0, life / max(max_life * 0.4, 1e-3))
            c = (int(r * fade), int(g * fade), int(b * fade))
            a = int(255 * fade)
            cx, cy = int(x), int(y)
            if kind == 2:
                ang = life * 6 + x
                dx, dy = math.cos(ang) * size, abs(math.sin(ang * 1.3)) * size * 0.6 + 1
                pts = np.array([[cx - dx, cy - dy], [cx + dx, cy - dy],
                                [cx + dx, cy + dy], [cx - dx, cy + dy]], np.int32)
                cv2.fillConvexPoly(color, pts, c, cv2.LINE_AA)
                cv2.fillConvexPoly(alpha, pts, a, cv2.LINE_AA)
            else:
                draw_heart(color, alpha, cx, cy, size, c, a)


    def _draw_sparks(self, color, alpha):
        """Rockets and sparks, all at once with numpy: there can be tens of thousands."""
        p = self.p[self.p[:, 10] <= 1]
        h, w = alpha.shape
        x, y = p[:, 0].astype(np.int32), p[:, 1].astype(np.int32)
        inside = (x >= 0) & (x < w) & (y >= 0) & (y < h)
        p, x, y = p[inside], x[inside], y[inside]
        if not len(p):
            return
        fade = np.minimum(1.0, p[:, 4] / np.maximum(p[:, 5] * 0.4, 1e-3))
        order = np.argsort(fade)  # brightest written last, so it wins shared pixels
        dots_c = np.zeros_like(color)
        dots_a = np.zeros_like(alpha)
        dots_c[y[order], x[order]] = (p[order, 6:9] * fade[order, None]).astype(np.uint8)
        dots_a[y[order], x[order]] = (255 * fade[order]).astype(np.uint8)
        # Grow each dot into a small round spark: widen, then soften the square edges.
        dots_c = cv2.GaussianBlur(cv2.dilate(dots_c, SPARK_KERNEL), (5, 5), 0.9)
        dots_a = cv2.GaussianBlur(cv2.dilate(dots_a, SPARK_KERNEL), (5, 5), 0.9)
        cv2.max(color, cv2.multiply(dots_c, 2), dst=color)  # x2 keeps the blurred cores bright
        cv2.max(alpha, cv2.multiply(dots_a, 2), dst=alpha)


def draw_heart(color, alpha, cx, cy, s, c, a):
    s = max(3, int(s))
    lobe = max(2, s // 2)
    tri = np.array([[cx - s, cy], [cx + s, cy], [cx, cy + int(s * 1.3)]], np.int32)
    for img, col in ((color, c), (alpha, a)):
        cv2.circle(img, (cx - lobe, cy), lobe, col, -1, cv2.LINE_AA)
        cv2.circle(img, (cx + lobe, cy), lobe, col, -1, cv2.LINE_AA)
        cv2.fillConvexPoly(img, tri, col, cv2.LINE_AA)


def spark_burst(x, y, rgb):
    """Sparks of one exploding rocket, as an array of particle rows."""
    n = random.randint(70, 110)
    ang = 2 * np.pi * np.arange(n) / n + np.random.uniform(-0.05, 0.05, n)
    v = random.uniform(140, 220) * np.random.uniform(0.6, 1.0, n)
    life = np.random.uniform(1.0, 1.6, n)
    col = np.where((np.random.random(n) < 0.8)[:, None], rgb, random.choice(PALETTE))
    out = np.empty((n, 11), np.float32)
    out[:, 0], out[:, 1] = x, y
    out[:, 2], out[:, 3] = np.cos(ang) * v, np.sin(ang) * v
    out[:, 4] = out[:, 5] = life
    out[:, 6:9] = col
    out[:, 9] = np.random.uniform(2.5, 4.0, n)
    out[:, 10] = 1
    return out


def spawn(effect, w, h, amount):
    """One burst of an effect: `amount` rockets, confetti pieces or hearts."""
    rows = []
    if effect == "fireworks":
        for _ in range(amount):
            x = random.uniform(0.05, 0.95) * w
            fuel = random.uniform(1.0, 1.5)
            rows.append([x, h + random.uniform(10, 40), random.uniform(-25, 25),
                         -random.uniform(0.7, 1.0) * h * 0.95, fuel, fuel, *random.choice(PALETTE), 2.5, 0])
    elif effect == "confetti":
        for _ in range(amount):
            life = random.uniform(3.0, 5.0)
            rows.append([random.uniform(0, w), random.uniform(-h * 0.8, 0), random.uniform(-30, 30),
                         random.uniform(40, 120), life, life, *random.choice(PALETTE),
                         random.uniform(3, 6), 2])
    elif effect == "hearts":
        for _ in range(amount):
            life = random.uniform(3.0, 4.5)
            pink = random.choice([(150, 60, 255), (120, 40, 230), (180, 110, 255)])
            rows.append([random.uniform(0, w), h + random.uniform(10, h * 0.3), random.uniform(-15, 15),
                         -random.uniform(90, 160), life, life, *pink, random.uniform(8, 16), 3])
    return rows


class CameraError(Exception):
    pass


def open_camera(path):
    # OpenCV's V4L2 backend wants an index, so resolve by-id links to /dev/videoN.
    dev = Path(path).resolve().name
    if not dev.startswith("video"):
        raise CameraError(f"{path} is not a camera")
    cap = cv2.VideoCapture(int(dev[5:]), cv2.CAP_V4L2)
    # MJPG has to be set before the size, or the C920 falls back to slow YUYV.
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FPS)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not cap.isOpened():
        raise CameraError("Could not open the webcam. Is Discord/Vesktop using it? Switch its camera off first.")
    return cap


class LatestFrame(threading.Thread):
    """Reads the camera in the background so processing never waits on it."""

    def __init__(self, cap):
        super().__init__(daemon=True)
        self.cap = cap
        self.cond = threading.Condition()
        self.frame, self.seq = None, 0
        self.stopped = False

    def run(self):
        while not self.stopped:
            ok, frame = self.cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            with self.cond:
                self.frame, self.seq = frame, self.seq + 1
                self.cond.notify()

    def next(self, after_seq, timeout=2.0):
        """Wait for a frame newer than after_seq; (None, seq) if none came in time."""
        with self.cond:
            if not self.cond.wait_for(lambda: self.seq > after_seq, timeout):
                return None, after_seq
            return self.frame, self.seq


class Engine:
    """Runs the camera pipeline in a thread. `settings` is read live every frame,
    so changes made in the GUI apply straight away."""

    def __init__(self, settings, on_status=print, on_effect=None):
        self.settings = settings
        self.on_status = on_status   # called with (state, message), state in running/stopped/error
        self.on_effect = on_effect   # called with the effect name when a show starts
        self._stop = threading.Event()
        self._manual = []
        self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_safe, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None

    def fire(self, effect):
        """Play an effect now, as if its gesture was shown (test button, hotkey)."""
        self._manual.append(effect)

    def _run_safe(self):
        try:
            self._run()
            self.on_status("stopped", "Off")
        except CameraError as e:
            self.on_status("error", str(e))
        except Exception as e:  # keep the GUI alive and show what broke
            self.on_status("error", f"{type(e).__name__}: {e}")

    def _run(self):
        if not Path(OUTPUT).exists():
            raise CameraError(f"Virtual camera {OUTPUT} not found. Run: sudo {HERE}/setup-loopback.sh")
        cap = open_camera(self.settings["camera"])
        reader = LatestFrame(cap)
        try:
            ok, frame = cap.read()
            if not ok:
                raise CameraError("The webcam opened but sends no picture.")
            reader.start()
            self._loop(reader, frame)
        finally:
            reader.stopped = True
            reader.join(timeout=1)
            cap.release()

    def _loop(self, reader, frame):
        h, w = frame.shape[:2]
        fw, fh = w // 2, h // 2  # effects are drawn at half resolution and upscaled

        recognizer = vision.GestureRecognizer.create_from_options(vision.GestureRecognizerOptions(
            base_options=BaseOptions(model_asset_path=str(HERE / "models/gesture_recognizer.task")),
            running_mode=vision.RunningMode.VIDEO, num_hands=2))
        segmenter = vision.ImageSegmenter.create_from_options(vision.ImageSegmenterOptions(
            base_options=BaseOptions(model_asset_path=str(HERE / "models/selfie_segmenter.tflite")),
            running_mode=vision.RunningMode.VIDEO, output_confidence_masks=True))

        particles = Particles()
        trail_c = np.zeros((fh, fw, 3), np.uint8)
        trail_a = np.zeros((fh, fw), np.uint8)
        black = np.zeros_like(frame)

        # Per effect: a timed show runs until show_until, a held one until held_until.
        show_until = dict.fromkeys(EFFECTS, 0.0)
        held_until = dict.fromkeys(EFFECTS, 0.0)
        next_burst = dict.fromkeys(EFFECTS, 0.0)
        was_active = dict.fromkeys(EFFECTS, False)

        streak_gesture, streak = None, 0
        frame_no, seq = 0, 0
        mask = None
        t0 = time.monotonic()
        last_t = t0

        with pyvirtualcam.Camera(w, h, FPS, device=OUTPUT, fmt=pyvirtualcam.PixelFormat.BGR) as vcam:
            self.on_status("running", f"Running ({w}x{h}). Pick \"Gesture Cam\" as your camera in Discord.")
            while not self._stop.is_set():
                frame, seq = reader.next(seq)
                if frame is None:
                    raise CameraError("The webcam stopped sending pictures.")
                now = time.monotonic()
                dt = min(now - last_t, 0.1)
                last_t = now
                ts = int((now - t0) * 1000)
                effects_cfg = self.settings["effects"]

                small = cv2.resize(frame, (fw, fh), interpolation=cv2.INTER_AREA)
                mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(small, cv2.COLOR_BGR2RGB))

                # Gesture -> effect, after holding it for HOLD_FRAMES recognitions.
                frame_no += 1
                if frame_no % GESTURE_EVERY == 0:
                    res = recognizer.recognize_for_video(mp_img, ts)
                    seen = None
                    for hand in res.gestures:
                        if hand and hand[0].category_name in GESTURE_EFFECTS and hand[0].score >= MIN_SCORE:
                            seen = hand[0].category_name
                            break
                    streak = streak + 1 if seen and seen == streak_gesture else (1 if seen else 0)
                    streak_gesture = seen
                    effect = GESTURE_EFFECTS.get(seen)
                    cfg = effects_cfg.get(effect)
                    if cfg and cfg["enabled"]:
                        if cfg["while_held"]:
                            if streak >= HOLD_FRAMES or now < held_until[effect]:
                                held_until[effect] = now + HELD_GRACE_S
                        elif streak == HOLD_FRAMES and now >= show_until[effect]:
                            show_until[effect] = now + cfg["duration"]

                while self._manual:
                    effect = self._manual.pop(0)
                    cfg = effects_cfg[effect]
                    length = MANUAL_HELD_S if cfg["while_held"] else cfg["duration"]
                    show_until[effect] = max(show_until[effect], now + length)

                # Each active effect fires a burst every 1/rate seconds.
                for effect, cfg in effects_cfg.items():
                    active = now < show_until[effect] or now < held_until[effect]
                    if active and not was_active[effect]:
                        next_burst[effect] = now
                        if self.on_effect:
                            self.on_effect(effect)
                    was_active[effect] = active
                    if active and now >= next_burst[effect]:
                        particles.add(spawn(effect, fw, fh, int(cfg["amount"])))
                        next_burst[effect] = now + 1.0 / max(cfg["rate"], 0.05)

                if len(particles) or trail_a.any():
                    particles.step(dt, fw, fh)
                    # Sparks go into a fading buffer so they leave streaks.
                    # Truncate rather than round, so faint pixels actually reach zero.
                    trail_c = (trail_c * TRAIL_DECAY).astype(np.uint8)
                    trail_a = (trail_a * TRAIL_DECAY).astype(np.uint8)
                    particles.draw(trail_c, trail_a, sparks=True)
                    color, alpha = trail_c.copy(), trail_a.copy()
                    particles.draw(color, alpha, sparks=False)
                    # Soft glow around the sparks, blurred at quarter size because it is cheap there.
                    q = (fw // 2, fh // 2)
                    glow = cv2.resize(cv2.GaussianBlur(cv2.resize(color, q), (0, 0), 2), (fw, fh))
                    glow_a = cv2.resize(cv2.GaussianBlur(cv2.resize(alpha, q), (0, 0), 2), (fw, fh))
                    color = cv2.add(color, glow)
                    alpha = cv2.max(alpha, glow_a)

                    # Person mask, so the effects only show behind you.
                    seg = segmenter.segment_for_video(mp_img, ts)
                    person = cv2.GaussianBlur(seg.confidence_masks[0].numpy_view(), (0, 0), 1.5)
                    mask = person if mask is None else 0.5 * mask + 0.5 * person
                    # The effect layer is premultiplied (colours already fade with alpha),
                    # so darken the background by alpha and add the colour on top.
                    behind = 1.0 - np.clip(mask, 0, 1)
                    a = alpha.astype(np.float32) * (1.0 / 255) * behind
                    a = cv2.resize(a, (w, h), interpolation=cv2.INTER_LINEAR)
                    color = (color * behind[..., None]).astype(np.uint8)
                    color = cv2.resize(color, (w, h), interpolation=cv2.INTER_LINEAR)
                    if black.shape != frame.shape:
                        black = np.zeros_like(frame)
                    out = cv2.add(cv2.blendLinear(frame, black, 1.0 - a, a), color)
                else:
                    mask = None
                    out = frame

                vcam.send(out)


def main():
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    done = threading.Event()

    def status(state, msg):
        print(msg, flush=True)
        if state != "running":
            done.set()

    engine = Engine(load_settings(), on_status=status, on_effect=lambda e: print(f"{e}!", flush=True))
    signal.signal(signal.SIGUSR1, lambda *_: engine.fire("fireworks"))
    signal.signal(signal.SIGUSR2, lambda *_: engine.fire("confetti"))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: done.set())
    engine.start()
    while not done.wait(0.5):  # short waits so signal handlers get to run
        pass
    engine.stop()


if __name__ == "__main__":
    main()
