#!/usr/bin/env python3
"""Gesture Cam window: turn the virtual camera on/off and tune each effect."""

import signal
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GLibUnix", "2.0")
from gi.repository import Adw, GLib, GLibUnix, Gtk  # noqa: E402

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
        super().__init__(application=app, title="Gesture Cam", default_width=560, default_height=820)
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
        for name, meta in gc.EFFECTS.items():
            page.add(self._effect_group(name, meta))

        self.connect("close-request", self._on_close)
        self._set_power(True)  # start straight away; that's why the app was opened

    # ----- effect settings -------------------------------------------------

    def _effect_group(self, name, meta):
        cfg = self.settings["effects"][name]
        group = Adw.PreferencesGroup(title=f"{meta['gesture']}  →  {name.capitalize()}")

        test = Gtk.Button(label="Test", valign=Gtk.Align.CENTER, sensitive=False)
        test.add_css_class("flat")
        test.set_tooltip_text("Play it now, without the gesture")
        test.connect("clicked", lambda _b: self.engine.fire(name))
        group.set_header_suffix(test)
        self.test_buttons.append(test)

        enabled = Adw.SwitchRow(title="Enabled", active=cfg["enabled"])
        held = Adw.SwitchRow(title="Keep going while the gesture is held",
                             subtitle="Off: plays for the duration below", active=cfg["while_held"])
        lo, hi = meta["amount_range"]
        amount = slider_row("Amount", meta["amount_label"], lo, hi, 1 if hi <= 100 else 10, 0, cfg["amount"],
                            lambda v: self._set(name, "amount", v))
        rate = slider_row("Frequency", "Bursts per second", *gc.RATE_RANGE, 0.1, 1, cfg["rate"],
                          lambda v: self._set(name, "rate", v))
        duration = slider_row("Duration", "Seconds", *gc.DURATION_RANGE, 0.5, 1, cfg["duration"],
                              lambda v: self._set(name, "duration", v))

        def refresh(*_):
            on = enabled.get_active()
            for row in (held, amount, rate):
                row.set_sensitive(on)
            duration.set_sensitive(on and not held.get_active())

        enabled.connect("notify::active", lambda r, _p: (self._set(name, "enabled", r.get_active()), refresh()))
        held.connect("notify::active", lambda r, _p: (self._set(name, "while_held", r.get_active()), refresh()))
        refresh()
        for row in (enabled, held, amount, rate, duration):
            group.add(row)
        return group

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
