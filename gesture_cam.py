#!/usr/bin/env python3
"""Gesture Cam: webcam -> hand gestures -> effects behind you -> virtual camera.

Default gestures (hold for a moment):
  thumbs up     -> fireworks
  peace sign    -> confetti
  rock-on / ILY -> hearts
  thumbs down   -> rain
  pointing up   -> balloons
  open palm     -> snow
  OK sign       -> sparkles
  shaka         -> bubbles
  both hands open -> lasers

Which gesture plays which effect, and how much, how often and how long, is set in the GUI
(gesture_cam_gui.py) and saved to ~/.config/gesture-cam/settings.json.
Running this file directly starts the camera without a window, using those settings.

Pick "Gesture Cam" as the camera in Discord. The same picture also goes straight into PipeWire as a
camera named "Gesture Cam", for apps that find cameras through PipeWire (GNOME Snapshot, OBS, browsers):
they don't list the v4l2loopback device. Effects can also be forced with
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

# Every gesture the MediaPipe model knows, with the name shown in the GUI.
GESTURES = {
    "Thumb_Up": "👍 Thumbs up",
    "Thumb_Down": "👎 Thumbs down",
    "Victory": "✌️ Peace sign",
    "ILoveYou": "🤟 I love you",
    "Pointing_Up": "☝️ Pointing up",
    "Open_Palm": "✋ Open palm",
    "Closed_Fist": "✊ Fist",
    # Not in the model: worked out from two hands, or from finger positions (see detect_gestures).
    "Two_Thumbs_Up": "👍👍 Two thumbs up",
    "Two_Thumbs_Down": "👎👎 Two thumbs down",
    "Two_Open_Palms": "🙌 Both hands open",
    "OK": "👌 OK sign",
    "Shaka": "🤙 Shaka / call me",
}
HOLD_FRAMES = 5        # consecutive recognitions needed before an effect fires
GESTURE_EVERY = 2      # run the gesture model on every Nth frame (15/s is plenty)
MIN_SCORE = 0.6
HELD_GRACE_S = 0.4     # a held gesture missed for this long still counts as held
MANUAL_HELD_S = 3.0    # how long a test/hotkey show lasts for an effect in "while held" mode
TRAIL_DECAY = 0.72     # how much of last frame's sparks stays, for firework streaks
GRAVITY = 260.0        # px/s^2, in effect-layer pixels
MAX_PARTICLES = 30000  # beyond this new bursts are skipped, to keep the frame rate up
SPARK_KERNEL = np.ones((3, 3), np.uint8)
SNOW_KERNEL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (4, 4))

# Per effect: what the "amount" slider means and its range, plus the defaults.
EFFECTS = {
    "fireworks": {"amount_label": "Rockets per burst", "amount_range": (1, 20)},
    "confetti": {"amount_label": "Pieces per burst", "amount_range": (20, 600)},
    "hearts": {"amount_label": "Hearts per burst", "amount_range": (5, 100)},
    "rain": {"amount_label": "Raindrops per burst", "amount_range": (10, 400)},
    "balloons": {"amount_label": "Balloons per burst", "amount_range": (1, 40)},
    "snow": {"amount_label": "Snowflakes per burst", "amount_range": (10, 400)},
    "sparkles": {"amount_label": "Sparkles per burst", "amount_range": (5, 150)},
    "bubbles": {"amount_label": "Bubbles per burst", "amount_range": (2, 80)},
    "lasers": {"amount_label": "Beams per burst", "amount_range": (1, 12)},
}
RATE_RANGE = (0.2, 8.0)       # bursts per second
DURATION_RANGE = (0.5, 20.0)  # seconds

DEFAULT_SETTINGS = {
    "camera": "/dev/v4l/by-id/usb-046d_HD_Pro_Webcam_C920-video-index0",
    "effects": {
        "fireworks": {"gesture": "Thumb_Up", "enabled": True, "while_held": True,
                      "amount": 4, "rate": 3.0, "duration": 3.0},
        "confetti": {"gesture": "Victory", "enabled": True, "while_held": False,
                     "amount": 220, "rate": 1.0, "duration": 1.0},
        "hearts": {"gesture": "ILoveYou", "enabled": True, "while_held": False,
                   "amount": 28, "rate": 1.0, "duration": 1.0},
        "rain": {"gesture": "Thumb_Down", "enabled": True, "while_held": True,
                 "amount": 60, "rate": 6.0, "duration": 3.0},
        "balloons": {"gesture": "Pointing_Up", "enabled": True, "while_held": False,
                     "amount": 6, "rate": 1.5, "duration": 2.0},
        "snow": {"gesture": "Open_Palm", "enabled": True, "while_held": False,
                 "amount": 80, "rate": 2.0, "duration": 4.0},
        "sparkles": {"gesture": "OK", "enabled": True, "while_held": False,
                     "amount": 30, "rate": 4.0, "duration": 2.0},
        "bubbles": {"gesture": "Shaka", "enabled": True, "while_held": False,
                    "amount": 12, "rate": 2.0, "duration": 3.0},
        "lasers": {"gesture": "Two_Open_Palms", "enabled": True, "while_held": True,
                   "amount": 4, "rate": 1.0, "duration": 3.0},
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
            if cfg.get("gesture") not in GESTURES:
                settings["effects"][name]["gesture"] = DEFAULT_SETTINGS["effects"][name]["gesture"]
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


# Particle kinds. Each row is: x y vx vy life max_life r g b size kind
ROCKET, SPARK, CONFETTI, HEART, RAIN, BALLOON, SNOW, SPARKLE, BUBBLE, LASER = range(10)
# Per kind:              rocket spark confetti heart  rain balloon snow sparkle bubble laser
KIND_GRAVITY = np.array([0.35,  0.6,  0.15,   -0.05, 1.0, -0.03,  0.0, 0.0,   -0.02, 0.0], np.float32) * GRAVITY
KIND_DRAG = np.array([1.0,      0.985, 0.97,   1.0,   1.0, 1.0,    1.0, 1.0,   1.0,   1.0], np.float32)
KIND_SWAY = np.array([0,        0,    40,     40,    0,   25,     30,  0,     20,    0], np.float32)  # px/s
TRAIL_KINDS = (ROCKET, SPARK, LASER)  # drawn into the fading buffer, so they leave streaks


class Particles:
    """Small numpy particle system; see the kind constants above.
    Lasers don't move: x is where the beam starts, `size` its middle angle, vx its sweep speed."""

    def __init__(self):
        self.p = np.zeros((0, 11), np.float32)

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
        k = kind.astype(np.intp)
        moving = kind != LASER
        p[:, 3] += KIND_GRAVITY[k] * dt
        p[:, 2:4] *= KIND_DRAG[k, None] ** (dt * 60)
        # confetti, hearts, balloons, snow and bubbles sway sideways
        sway = KIND_SWAY[k] * np.sin(p[:, 4] * 4 + p[:, 0] * 0.05)
        p[:, 0] += np.where(moving, p[:, 2] + sway, 0) * dt
        p[:, 1] += np.where(moving, p[:, 3], 0) * dt
        p[:, 4] -= dt

        # rockets burst when they stop rising or run out of fuel
        burst = (kind == ROCKET) & ((p[:, 3] > -20) | (p[:, 4] <= 0))
        for x, y, *_rest, r, g, b, _s, _k in p[burst]:
            self.add_array(spark_burst(x, y, (r, g, b)))
        p = self.p[: len(p)]  # sparks were appended after the existing rows
        # drop what has fallen out of the bottom; things rising from below the frame stay
        keep = ~burst & (p[:, 4] > 0) & ((p[:, 1] < h + 40) | (p[:, 3] < 0)) & (p[:, 1] > -h)
        self.p = np.vstack([p[keep], self.p[len(p):]])

    def draw(self, color, alpha, trails):
        """Draw the kinds that leave streaks (trails=True) or all the others."""
        kind = self.p[:, 10]
        if trails:
            self._draw_dots(color, alpha, (kind == ROCKET) | (kind == SPARK), SPARK_KERNEL, 0.9)
        else:
            self._draw_dots(color, alpha, kind == SNOW, SNOW_KERNEL, 1.2)
        # the rest one by one; rockets, sparks and snow were drawn above, all at once
        one_by_one = (np.isin(kind, TRAIL_KINDS) == trails) & ~np.isin(kind, (ROCKET, SPARK, SNOW))
        for x, y, vx, vy, life, max_life, r, g, b, size, kind in self.p[one_by_one]:
            fade = min(1.0, life / max(max_life * 0.4, 1e-3))
            c = (int(r * fade), int(g * fade), int(b * fade))
            a = int(255 * fade)
            cx, cy = int(x), int(y)
            if kind == CONFETTI:
                ang = life * 6 + x
                dx, dy = math.cos(ang) * size, abs(math.sin(ang * 1.3)) * size * 0.6 + 1
                pts = np.array([[cx - dx, cy - dy], [cx + dx, cy - dy],
                                [cx + dx, cy + dy], [cx - dx, cy + dy]], np.int32)
                cv2.fillConvexPoly(color, pts, c, cv2.LINE_AA)
                cv2.fillConvexPoly(alpha, pts, a, cv2.LINE_AA)
            elif kind == HEART:
                draw_heart(color, alpha, cx, cy, size, c, a)
            elif kind == RAIN:
                # a streak pointing back along the drop's motion
                tail = (int(x - vx * 0.04), int(y - vy * 0.04))
                cv2.line(color, (cx, cy), tail, c, 1, cv2.LINE_AA)
                cv2.line(alpha, (cx, cy), tail, int(a * 0.8), 1, cv2.LINE_AA)
            elif kind == BALLOON:
                draw_balloon(color, alpha, cx, cy, size, c, a, life)
            elif kind == SPARKLE:
                # grows then shrinks over its life, like a twinkle
                tw = math.sin(math.pi * (1 - life / max_life))
                draw_sparkle(color, alpha, cx, cy, size * tw, (int(r * tw), int(g * tw), int(b * tw)), int(255 * tw))
            elif kind == BUBBLE:
                draw_bubble(color, alpha, cx, cy, size, c, a)
            elif kind == LASER:
                ang = size + 0.55 * math.sin(life * vx)
                length = 2 * alpha.shape[0]
                end = (int(x + math.cos(ang) * length), int(y + math.sin(ang) * length))
                cv2.line(color, (cx, cy), end, c, 2, cv2.LINE_AA)
                cv2.line(alpha, (cx, cy), end, a, 2, cv2.LINE_AA)

    def _draw_dots(self, color, alpha, select, kernel, blur):
        """Round dots for many particles at once with numpy: there can be tens of thousands."""
        p = self.p[select]
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
        dots_c = cv2.GaussianBlur(cv2.dilate(dots_c, kernel), (5, 5), blur)
        dots_a = cv2.GaussianBlur(cv2.dilate(dots_a, kernel), (5, 5), blur)
        cv2.max(color, cv2.multiply(dots_c, 2), dst=color)  # x2 keeps the blurred cores bright
        cv2.max(alpha, cv2.multiply(dots_a, 2), dst=alpha)


def draw_sparkle(color, alpha, cx, cy, s, c, a):
    """Four-pointed star."""
    s = max(1.0, s)
    long, short = s, s * 0.22
    pts = np.array([[cx, cy - long], [cx + short, cy - short], [cx + long, cy], [cx + short, cy + short],
                    [cx, cy + long], [cx - short, cy + short], [cx - long, cy], [cx - short, cy - short]], np.int32)
    for img, col in ((color, c), (alpha, a)):
        cv2.fillPoly(img, [pts], col, cv2.LINE_AA)


def draw_bubble(color, alpha, cx, cy, s, c, a):
    s = max(3, int(s))
    rim_a = int(a * 0.75)
    shine = (cx - s // 3, cy - s // 3)
    for img, col, sh in ((color, tuple(int(v * 0.75) for v in c), tuple(int(a * 0.9) for _ in range(3))),
                         (alpha, rim_a, a)):
        cv2.circle(img, (cx, cy), s, col, 1, cv2.LINE_AA)
        cv2.ellipse(img, shine, (max(1, s // 4), max(1, s // 6)), -40, 0, 360, sh, -1, cv2.LINE_AA)


def draw_heart(color, alpha, cx, cy, s, c, a):
    s = max(3, int(s))
    lobe = max(2, s // 2)
    tri = np.array([[cx - s, cy], [cx + s, cy], [cx, cy + int(s * 1.3)]], np.int32)
    for img, col in ((color, c), (alpha, a)):
        cv2.circle(img, (cx - lobe, cy), lobe, col, -1, cv2.LINE_AA)
        cv2.circle(img, (cx + lobe, cy), lobe, col, -1, cv2.LINE_AA)
        cv2.fillConvexPoly(img, tri, col, cv2.LINE_AA)


def draw_balloon(color, alpha, cx, cy, s, c, a, life):
    s = max(4, int(s))
    axes = (s, int(s * 1.2))
    bottom = cy + axes[1]
    # wavy string first, so the balloon covers its top end
    string = np.array([[cx + int(3 * math.sin(life * 3 + i)), bottom + i * s // 3] for i in range(7)], np.int32)
    knot = np.array([[cx, bottom - 1], [cx - 3, bottom + 4], [cx + 3, bottom + 4]], np.int32)
    shine = (cx - s // 3, cy - s // 2)
    grey = tuple(int(v * a / 255) for v in (200, 200, 200))
    for img, col, str_col, shine_col in ((color, c, grey, tuple(int(a * 0.9) for _ in range(3))),
                                         (alpha, a, int(a * 0.7), a)):
        cv2.polylines(img, [string], False, str_col, 1, cv2.LINE_AA)
        cv2.fillConvexPoly(img, knot, col, cv2.LINE_AA)
        cv2.ellipse(img, (cx, cy), axes, 0, 0, 360, col, -1, cv2.LINE_AA)
        cv2.ellipse(img, shine, (max(1, s // 5), max(2, s // 3)), 30, 0, 360, shine_col, -1, cv2.LINE_AA)


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
    out[:, 10] = SPARK
    return out


def spawn(effect, w, h, amount):
    """One burst of an effect: `amount` rockets, confetti pieces or hearts."""
    rows = []
    if effect == "fireworks":
        for _ in range(amount):
            x = random.uniform(0.05, 0.95) * w
            fuel = random.uniform(1.0, 1.5)
            rows.append([x, h + random.uniform(10, 40), random.uniform(-25, 25),
                         -random.uniform(0.7, 1.0) * h * 0.95, fuel, fuel, *random.choice(PALETTE), 2.5, ROCKET])
    elif effect == "confetti":
        for _ in range(amount):
            life = random.uniform(3.0, 5.0)
            rows.append([random.uniform(0, w), random.uniform(-h * 0.8, 0), random.uniform(-30, 30),
                         random.uniform(40, 120), life, life, *random.choice(PALETTE),
                         random.uniform(3, 6), CONFETTI])
    elif effect == "hearts":
        for _ in range(amount):
            life = random.uniform(3.0, 4.5)
            pink = random.choice([(150, 60, 255), (120, 40, 230), (180, 110, 255)])
            rows.append([random.uniform(0, w), h + random.uniform(10, h * 0.3), random.uniform(-15, 15),
                         -random.uniform(90, 160), life, life, *pink, random.uniform(8, 16), HEART])
    elif effect == "rain":
        for _ in range(amount):
            life = random.uniform(1.5, 2.5)
            blue = random.choice([(255, 200, 150), (240, 180, 120), (255, 220, 190)])
            rows.append([random.uniform(-0.1, 1.1) * w, random.uniform(-h * 0.3, 0), random.uniform(-30, -10),
                         random.uniform(380, 520), life, life, *blue, 1, RAIN])
    elif effect == "balloons":
        for _ in range(amount):
            life = random.uniform(6.0, 8.0)
            rows.append([random.uniform(0.05, 0.95) * w, h + random.uniform(30, h * 0.4), random.uniform(-10, 10),
                         -random.uniform(60, 110), life, life, *random.choice(PALETTE[:-1]),
                         random.uniform(14, 22), BALLOON])
    elif effect == "snow":
        for _ in range(amount):
            life = random.uniform(6.0, 9.0)
            rows.append([random.uniform(0, w), random.uniform(-h * 0.5, 0), random.uniform(-8, 8),
                         random.uniform(30, 70), life, life, 245, 240, 235, 1, SNOW])
    elif effect == "sparkles":
        for _ in range(amount):
            life = random.uniform(0.6, 1.3)
            gold = random.choice([(120, 220, 255), (200, 240, 255), (255, 255, 255), (160, 200, 255)])
            rows.append([random.uniform(0, w), random.uniform(0, h), 0, 0, life, life, *gold,
                         random.uniform(4, 11), SPARKLE])
    elif effect == "bubbles":
        for _ in range(amount):
            life = random.uniform(5.0, 8.0)
            tint = random.choice([(255, 230, 200), (255, 200, 240), (220, 255, 230)])
            rows.append([random.uniform(0.05, 0.95) * w, h + random.uniform(10, h * 0.3), random.uniform(-10, 10),
                         -random.uniform(40, 90), life, life, *tint, random.uniform(6, 16), BUBBLE])
    elif effect == "lasers":
        for _ in range(amount):
            life = random.uniform(2.0, 3.0)
            beam = random.choice([(80, 255, 80), (80, 80, 255), (255, 80, 255), (255, 200, 60)])
            # starts at the bottom edge, points up, sweeps side to side (see Particles)
            rows.append([random.uniform(0.1, 0.9) * w, h - 1, random.uniform(1.5, 3.0) * random.choice((-1, 1)),
                         0, life, life, *beam, -math.pi / 2 + random.uniform(-0.4, 0.4), LASER])
    return rows


def _hand_shape(landmarks, aspect):
    """Which fingers are stretched out, from the 21 hand landmarks (MediaPipe numbering)."""
    pts = np.array([[lm.x * aspect, lm.y] for lm in landmarks])
    dist = lambda i, j: float(np.linalg.norm(pts[i] - pts[j]))  # noqa: E731
    size = dist(0, 9)  # wrist to middle knuckle
    out = {name: dist(tip, 0) > dist(pip, 0) * 1.15
           for name, tip, pip in (("index", 8, 6), ("middle", 12, 10), ("ring", 16, 14), ("pinky", 20, 18))}
    # thumb out: its tip is well away from the pinky knuckle and from the index knuckle
    out["thumb"] = dist(4, 17) > 0.9 * size and dist(4, 5) > 0.4 * size
    out["pinch"] = dist(4, 8) < 0.35 * size  # thumb tip touching index tip
    return out


def detect_gestures(res, aspect):
    """Gesture names in this frame, most specific first: two-hand gestures, then the
    OK/shaka shapes worked out from landmarks, then the model's own label for each hand."""
    labels = [hand[0].category_name if hand and hand[0].score >= MIN_SCORE else "None" for hand in res.gestures]
    found = []
    if labels.count("Thumb_Up") == 2:
        found.append("Two_Thumbs_Up")
    if labels.count("Thumb_Down") == 2:
        found.append("Two_Thumbs_Down")
    if labels.count("Open_Palm") == 2:
        found.append("Two_Open_Palms")
    for label, landmarks in zip(labels, res.hand_landmarks):
        # The model has no OK or shaka; it calls them nothing, or mistakes a shaka for
        # "I love you". The finger rules are strict, so when they match they win.
        if label not in ("None", "ILoveYou"):
            continue
        f = _hand_shape(landmarks, aspect)
        if f["pinch"] and f["middle"] and f["ring"] and f["pinky"]:
            found.append("OK")
        elif f["thumb"] and f["pinky"] and not (f["index"] or f["middle"] or f["ring"]):
            found.append("Shaka")
    found += [label for label in labels if label != "None"]
    return found

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


class PipeWireCamera:
    """Offers the output frames as a "Gesture Cam" camera in PipeWire.

    v4l2loopback with exclusive_caps=1 (needed for Discord) only looks like a camera while something
    writes to it, and WirePlumber decides once at boot, so PipeWire apps never list /dev/video10.
    This is a second, PipeWire-native copy of the same picture. Without GStreamer's PipeWire plugin
    it does nothing, and only /dev/video10 works.
    """

    def __init__(self, w, h):
        self.src = self.pipeline = None
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            Gst.init(None)
            self.Gst = Gst
            # Frames arrive as I420, converted by OpenCV in send(): GStreamer's videoconvert from BGR
            # only manages a few frames a second at 720p, which froze the picture in Snapshot.
            # leaky-type=downstream drops old frames instead of queueing them if PipeWire falls behind.
            self.pipeline = Gst.parse_launch(
                f"appsrc name=src is-live=true do-timestamp=true format=time leaky-type=downstream max-buffers=2 "
                f"caps=video/x-raw,format=I420,width={w},height={h},framerate={FPS}/1 "
                f'! pipewiresink mode=provide client-name="Gesture Cam" '
                f'stream-properties="props,media.class=Video/Source,media.role=Camera"')
        except Exception as e:  # missing typelib or plugin: carry on with the v4l2loopback camera only
            print(f"PipeWire camera not available: {e}", file=sys.stderr)
            self.pipeline = None
            return
        self.src = self.pipeline.get_by_name("src")
        self.pipeline.set_state(Gst.State.PLAYING)

    def send(self, frame):
        if self.src is not None:
            i420 = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420)
            self.src.emit("push-buffer", self.Gst.Buffer.new_wrapped(i420.tobytes()))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.pipeline is not None:
            self.pipeline.set_state(self.Gst.State.NULL)
            self.pipeline = self.src = None


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
            raise CameraError(f"Virtual camera {OUTPUT} not found. Run: sudo {HERE}/setup.sh")
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

        with pyvirtualcam.Camera(w, h, FPS, device=OUTPUT, fmt=pyvirtualcam.PixelFormat.BGR) as vcam, \
                PipeWireCamera(w, h) as pwcam:
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
                    # Which effects each gesture plays, from the settings (several may share one).
                    wanted = {}
                    for effect, cfg in effects_cfg.items():
                        if cfg["enabled"]:
                            wanted.setdefault(cfg["gesture"], []).append(effect)
                    seen = next((gst for gst in detect_gestures(res, fw / fh) if gst in wanted), None)
                    streak = streak + 1 if seen and seen == streak_gesture else (1 if seen else 0)
                    streak_gesture = seen
                    for effect in wanted.get(seen, []):
                        cfg = effects_cfg[effect]
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
                    particles.draw(trail_c, trail_a, trails=True)
                    color, alpha = trail_c.copy(), trail_a.copy()
                    particles.draw(color, alpha, trails=False)
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
                pwcam.send(out)


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
