#!/usr/bin/env python3
"""Gesture Cam window: turn the virtual camera on/off and tune each effect."""

import signal
import sys
import threading

import gi

TOO_OLD = ("Gesture Cam needs GTK 4, libadwaita 1.4 and GLib 2.80 or newer: "
           "Arch, Debian 13, Ubuntu 24.04, Fedora 40 or later. Run sudo ./setup.sh first.")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("GLibUnix", "2.0")  # GLib 2.80+
    from gi.repository import Adw, GLib, GLibUnix, Gtk  # noqa: E402
except (ValueError, ImportError):
    sys.exit(TOO_OLD)
if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 4):  # Adw.ToolbarView, Adw.SwitchRow
    sys.exit(TOO_OLD)

import gesture_cam as gc  # noqa: E402

APP_ID = "local.gesturecam.GestureCam"


def slider_row(title, subtitle, lo, hi, step, digits, value, on_change):
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
    scale.set_digits(digits)
    scale.set_draw_value(True)
    scale.set_value_pos(Gtk.PositionType.LEFT)
    scale.set_value(value)
    scale.set_hexpand(True)
    scale.set_size_request(260, -1)
    scale.set_valign(Gtk.Align.CENTER)
    scale.connect("value-changed", lambda s: on_change(round(s.get_value(), digits) if digits else int(s.get_value())))
    row.add_suffix(scale)
    return row


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Gesture Cam", default_width=600, default_height=820)
        self.settings = gc.load_settings()
        self.engine = gc.Engine(self.settings, on_status=self._status_from_thread)
        self._save_pending = False
        self._busy = False  # true while a start/stop is in progress

        self.toasts = Adw.ToastOverlay()
        page = Adw.PreferencesPage()
        self.toasts.set_child(page)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(self.toasts)
        self.set_content(view)

        # On/off and camera choice.
        top = Adw.PreferencesGroup()
        self.power = Adw.SwitchRow(title="Gesture Cam", subtitle="Off")
        self.power_handler = self.power.connect("notify::active", self._on_power)
        top.add(self.power)

        self.cameras = gc.list_cameras()
        names = Gtk.StringList.new([name for _, name in self.cameras] or ["No webcam found"])
        self.camera_row = Adw.ComboRow(title="Webcam", model=names)
        paths = [p for p, _ in self.cameras]
        if self.settings["camera"] in paths:
            self.camera_row.set_selected(paths.index(self.settings["camera"]))
        elif paths:
            self.settings["camera"] = paths[0]
        self.camera_row.connect("notify::selected", self._on_camera)
        top.add(self.camera_row)
        page.add(top)

        self.test_buttons = []
        effects = Adw.PreferencesGroup(title="Effects",
                                       description="Switch an effect on or off, and open it to change its settings.")
        for name, meta in gc.EFFECTS.items():
            effects.add(self._effect_row(name, meta))
        page.add(effects)

        self.connect("close-request", self._on_close)
        self._set_power(True)  # start straight away; that's why the app was opened

    # ----- effect settings -------------------------------------------------

    def _effect_row(self, name, meta):
        cfg = self.settings["effects"][name]
        gesture_ids = list(gc.GESTURES)
        row = Adw.ExpanderRow(title=name.capitalize(), subtitle=gc.GESTURES[cfg["gesture"]],
                              show_enable_switch=True, enable_expansion=cfg["enabled"])
        row.connect("notify::enable-expansion",
                    lambda r, _p: self._set(name, "enabled", r.get_enable_expansion()))

        test = Gtk.Button(label="Test", valign=Gtk.Align.CENTER, sensitive=False)
        test.add_css_class("flat")
        test.set_tooltip_text("Play it now, without the gesture")
        test.connect("clicked", lambda _b: self.engine.fire(name))
        row.add_suffix(test)
        self.test_buttons.append(test)

        gesture = Adw.ComboRow(title="Gesture", model=Gtk.StringList.new(list(gc.GESTURES.values())),
                               selected=gesture_ids.index(cfg["gesture"]))

        def on_gesture(r, _p):
            gid = gesture_ids[r.get_selected()]
            self._set(name, "gesture", gid)
            row.set_subtitle(gc.GESTURES[gid])

        gesture.connect("notify::selected", on_gesture)
        held = Adw.SwitchRow(title="Keep going while the gesture is held",
                             subtitle="Off: plays for the duration below", active=cfg["while_held"])
        lo, hi = meta["amount_range"]
        amount = slider_row("Amount", meta["amount_label"], lo, hi, 1 if hi <= 100 else 10, 0, cfg["amount"],
                            lambda v: self._set(name, "amount", v))
        rate = slider_row("Frequency", "Bursts per second", *gc.RATE_RANGE, 0.1, 1, cfg["rate"],
                          lambda v: self._set(name, "rate", v))
        duration = slider_row("Duration", "Seconds", *gc.DURATION_RANGE, 0.5, 1, cfg["duration"],
                              lambda v: self._set(name, "duration", v))
        duration.set_sensitive(not cfg["while_held"])
        held.connect("notify::active", lambda r, _p: (self._set(name, "while_held", r.get_active()),
                                                      duration.set_sensitive(not r.get_active())))
        for child in (gesture, held, amount, rate, duration):
            row.add_row(child)
        return row

    def _set(self, effect, key, value):
        self.settings["effects"][effect][key] = value  # the engine reads this live
        if not self._save_pending:
            self._save_pending = True
            GLib.timeout_add(500, self._save)

    def _save(self):
        self._save_pending = False
        gc.save_settings(self.settings)
        return False

    # ----- start / stop ----------------------------------------------------

    def _on_power(self, row, _pspec):
        if not self._busy:
            self._set_power(row.get_active())

    def _set_power(self, on, restart=False):
        """Start or stop the engine off the GUI thread (stopping can take a moment)."""
        self._busy = True
        self.power.set_sensitive(False)
        self.power.set_subtitle("Starting…" if on else "Stopping…")

        def work():
            if restart or not on:
                self.engine.stop()
            if on:
                self.engine.start()
            else:
                GLib.idle_add(self._show_status, "stopped", "Off")

        threading.Thread(target=work, daemon=True).start()

    def _on_camera(self, row, _pspec):
        if not self.cameras:
            return
        self.settings["camera"] = self.cameras[row.get_selected()][0]
        self._save()
        if self.engine.running or self.power.get_active():
            self._set_power(True, restart=True)

    def _status_from_thread(self, state, message):
        GLib.idle_add(self._show_status, state, message)

    def _show_status(self, state, message):
        self._busy = False
        self.power.set_sensitive(True)
        self.power.handler_block(self.power_handler)
        self.power.set_active(state == "running")
        self.power.handler_unblock(self.power_handler)
        self.power.set_subtitle(message)
        for b in self.test_buttons:
            b.set_sensitive(state == "running")
        if state == "error":
            self.toasts.add_toast(Adw.Toast(title=message, timeout=6))
        return False

    def _on_close(self, _win):
        self.engine.on_status = lambda *_: None
        self.engine.stop()
        if self._save_pending:
            self._save()
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.window = None

    def do_activate(self):
        if not self.window:
            self.window = Window(self)
            # Hotkeys: pkill -USR1 -f gesture_cam_gui.py (fireworks), -USR2 (confetti).
            GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self._fire, "fireworks")
            GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2, self._fire, "confetti")
        self.window.present()

    def _fire(self, effect):
        self.window.engine.fire(effect)
        return True


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
