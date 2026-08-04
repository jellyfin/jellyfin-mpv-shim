"""TrayManager command dispatch.

The pystray loop needs a real desktop, and it lives in a separate process
anyway (it needs its process's main thread, and pystray + libmpv in one
process segfaults with GNOME AppIndicator). What's testable here is the
parent side: how commands from that child are dispatched.
"""

import multiprocessing
import threading
import unittest

from jellyfin_mpv_shim.tray import (
    TrayManager,
    backend_name,
    tray_unavailable_advice,
    tray_will_render,
    wants_x11_backend,
)


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

    def test_availability_follows_the_tray_host_both_ways(self):
        # The child watches the StatusNotifier host appear and vanish, so
        # availability is no longer a one-shot answer: an autostarted copy
        # that lost the race with the shell extension must be able to say so
        # later, and a shell restart must be able to take it back.
        m = TrayManager({})
        m.dispatch("tray_died", "not_rendered")
        self.assertFalse(m.available)
        m.dispatch("ready")
        self.assertTrue(m.available)
        m.dispatch("tray_died", "watcher_gone")
        self.assertFalse(m.available)

    def test_death_reason_is_optional(self):
        # The pump forwards whatever the child sent; older reasons (and the
        # import-failure path) carry no param at all.
        TrayManager({}).dispatch("tray_died")


class TestTrayWillRender(unittest.TestCase):
    """Whether an icon pystray happily created is one anybody can see.

    This is the gap the bug lived in: on GNOME with libayatana-appindicator
    installed but no AppIndicator extension, pystray builds the indicator,
    sets ``visible = True``, raises nothing -- and no icon appears. The app
    then treated the tray as a way back to itself and hid behind it.
    """

    def test_native_backends_are_never_probed(self):
        probed = []
        for backend in ("win32", "darwin"):
            with self.subTest(backend=backend):
                self.assertIs(True, tray_will_render(
                    backend, sni=lambda: probed.append("sni"),
                    xembed=lambda: probed.append("xembed")))
        self.assertEqual(probed, [], "a native tray was probed over D-Bus")

    def test_appindicator_asks_the_session_bus(self):
        self.assertIs(False, tray_will_render("appindicator",
                                              sni=lambda: False))
        self.assertIs(True, tray_will_render("appindicator",
                                             sni=lambda: True))

    def test_xembed_backends_ask_x11(self):
        for backend in ("gtk", "xorg"):
            with self.subTest(backend=backend):
                self.assertIs(False, tray_will_render(
                    backend, xembed=lambda: False))
                self.assertIs(True, tray_will_render(
                    backend, xembed=lambda: True))

    def test_dummy_backend_renders_nothing(self):
        self.assertIs(False, tray_will_render("dummy"))

    def test_unanswerable_is_none_and_never_false(self):
        # None means "could not tell", and callers must read it as yes. A
        # probe that cannot run (no PyGObject, no X connection, a backend we
        # have never heard of) must not take a working tray away from
        # someone -- only a confident False may change behaviour.
        self.assertIsNone(tray_will_render("appindicator", sni=lambda: None))
        self.assertIsNone(tray_will_render("gtk", xembed=lambda: None))
        self.assertIsNone(tray_will_render(""))
        self.assertIsNone(tray_will_render("some_future_backend"))


class TestBackendName(unittest.TestCase):
    def test_reads_the_module_pystray_picked(self):
        # pystray exposes its choice only as the module Icon came from.
        class Icon:
            pass

        Icon.__module__ = "pystray._appindicator"
        self.assertEqual(backend_name(Icon), "appindicator")
        Icon.__module__ = "pystray._win32"
        self.assertEqual(backend_name(Icon), "win32")

    def test_unreadable_module_is_empty_not_an_error(self):
        # Which routes to tray_will_render's None -- "could not tell" -- and
        # so leaves behaviour exactly as it was before this check existed.
        self.assertEqual(backend_name(object()), "")
        self.assertIsNone(tray_will_render(backend_name(object())))

    def test_every_name_it_can_produce_is_classified_or_unknown(self):
        # The real backends pystray ships. Anything here that came back None
        # would silently keep the old, wrong behaviour on that desktop.
        from jellyfin_mpv_shim import tray

        for backend in ("appindicator", "gtk", "xorg", "win32", "darwin",
                        "dummy"):
            with self.subTest(backend=backend):
                self.assertIsNot(
                    None,
                    tray.tray_will_render(backend, sni=lambda: True,
                                          xembed=lambda: True),
                    "pystray backend %r is not classified" % backend)


class TestTrayAdvice(unittest.TestCase):
    """The message has to name the way out. "No tray" on its own reads as a
    broken install on the one desktop where it is the default state."""

    def test_gnome_is_told_about_the_extension(self):
        msg = tray_unavailable_advice({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"})
        self.assertIn("AppIndicator", msg)
        self.assertIn("Allow Background", msg)

    def test_elsewhere_gets_the_generic_way_out(self):
        msg = tray_unavailable_advice({"XDG_CURRENT_DESKTOP": "KDE"})
        self.assertNotIn("GNOME", msg)
        self.assertIn("Allow Background", msg)

    def test_no_environment_still_answers(self):
        self.assertIn("Allow Background", tray_unavailable_advice({}))

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
