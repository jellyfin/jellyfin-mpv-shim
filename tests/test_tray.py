"""TrayManager command dispatch.

The pystray loop needs a real desktop, and it lives in a separate process
anyway (it needs its process's main thread, and pystray + libmpv in one
process segfaults with GNOME AppIndicator). What's testable here is the
parent side: how commands from that child are dispatched.
"""

import multiprocessing
import threading
import unittest

from jellyfin_mpv_shim.tray import TrayManager, wants_x11_backend


class TestTrayDispatch(unittest.TestCase):
    def test_dispatches_known_commands(self):
        seen = []
        m = TrayManager({"show": lambda: seen.append("show"),
                         "quit": lambda: seen.append("quit")})
        m.dispatch("show")
        m.dispatch("quit")
        self.assertEqual(seen, ["show", "quit"])

    def test_unknown_command_is_ignored(self):
        TrayManager({}).dispatch("does_not_exist")   # must not raise

    def test_handler_exception_does_not_propagate(self):
        def boom():
            raise RuntimeError("nope")

        m = TrayManager({"show": boom})
        m.dispatch("show")          # swallowed, so the pump survives

    def test_ready_marks_the_tray_available(self):
        m = TrayManager({})
        m.dispatch("ready")
        self.assertTrue(m.available)
        self.assertTrue(m.ready.is_set())

    def test_tray_died_is_not_available_but_still_unblocks(self):
        m = TrayManager({})
        m.dispatch("tray_died")
        self.assertFalse(m.available)
        # ready is set either way, so nothing waiting on the tray can hang
        # when pystray/AppIndicator is missing.
        self.assertTrue(m.ready.is_set())

    def test_stop_without_start_is_safe(self):
        TrayManager({}).stop()


class TestTrayMenuShape(unittest.TestCase):
    """The menu is built inside the child process, so it cannot be exercised
    here -- but the source can be checked for the one property that is easy to
    drop by accident."""

    def _menu_lines(self):
        """The MenuItem lines only -- comments mention these names too."""
        import inspect

        from jellyfin_mpv_shim import tray

        src = inspect.getsource(tray.TrayProcess.run)
        return [ln.strip() for ln in src.splitlines()
                if ln.strip().startswith("MenuItem(")]

    def test_show_library_browser_is_the_default_click_action(self):
        # Clicking the icon should reopen the window. Without default=True the
        # only way back to the app is right-click -> menu, which reads as the
        # tray icon being inert.
        entry = [ln for ln in self._menu_lines()
                 if "Show Library Browser" in ln]
        self.assertTrue(entry, "the Show Library Browser entry is gone")
        self.assertIn(
            "default=True", entry[0],
            "The tray's Show Library Browser item is no longer the default "
            "action; clicking the icon will do nothing on the backends that "
            "support a primary click (win32, gtk, xorg).")

    def test_only_one_default_item(self):
        # pystray takes the first default item; a second one is dead config
        # and a sign someone meant to move it.
        defaults = [ln for ln in self._menu_lines() if "default=True" in ln]
        self.assertEqual(len(defaults), 1)


class TestTrayPump(unittest.TestCase):
    def test_pump_drains_the_queue_and_honours_halt(self):
        seen = threading.Event()
        m = TrayManager({"show": seen.set})
        m._queue = multiprocessing.Queue()
        thread = threading.Thread(target=m._pump, daemon=True)
        thread.start()
        try:
            m._queue.put(("show", None))
            self.assertTrue(seen.wait(3), "pump did not dispatch")
        finally:
            m._halt.set()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()


class TestX11BackendGate(unittest.TestCase):
    """Which sessions get GDK_BACKEND=x11 forced on the tray process.

    #506 forced it everywhere to dodge a pystray crash on GNOME Wayland;
    #646 is the cost of that. On Wayfire the forced backend leaves the
    indicator reporting visible=True while it silently never registers with
    the StatusNotifierWatcher — no icon, no error. Both halves of the
    condition therefore have to hold.
    """

    def test_gnome_wayland_is_the_case_it_is_for(self):
        self.assertTrue(wants_x11_backend(
            {"XDG_CURRENT_DESKTOP": "GNOME", "WAYLAND_DISPLAY": "wayland-0"}))

    def test_gnome_is_named_several_ways(self):
        # XDG_CURRENT_DESKTOP is a colon-separated list, and distributions
        # prefix it: matching it exactly missed Ubuntu entirely.
        for desktop in ("GNOME", "ubuntu:GNOME", "GNOME-Classic:GNOME",
                        "gnome"):
            with self.subTest(desktop=desktop):
                self.assertTrue(wants_x11_backend(
                    {"XDG_CURRENT_DESKTOP": desktop,
                     "XDG_SESSION_TYPE": "wayland"}))

    def test_gnome_on_x11_needs_nothing_forced(self):
        self.assertFalse(wants_x11_backend(
            {"XDG_CURRENT_DESKTOP": "GNOME", "XDG_SESSION_TYPE": "x11",
             "DISPLAY": ":0"}))

    def test_another_compositor_keeps_its_own_backend(self):
        # The #646 report: Wayfire, whose StatusNotifierWatcher works fine.
        self.assertFalse(wants_x11_backend(
            {"XDG_CURRENT_DESKTOP": "wlroots", "WAYLAND_DISPLAY": "wayland-1",
             "XDG_SESSION_TYPE": "wayland"}))
        for desktop in ("KDE", "sway", "Hyprland", "LXQt:wlroots"):
            with self.subTest(desktop=desktop):
                self.assertFalse(wants_x11_backend(
                    {"XDG_CURRENT_DESKTOP": desktop,
                     "WAYLAND_DISPLAY": "wayland-1"}))

    def test_an_empty_environment_forces_nothing(self):
        # A bare login, a container, an unset session: nothing is known to be
        # broken, so change nothing.
        self.assertFalse(wants_x11_backend({}))
        self.assertFalse(wants_x11_backend({"WAYLAND_DISPLAY": "wayland-0"}))
