"""TrayManager command dispatch.

The pystray loop needs a real desktop, and it lives in a separate process
anyway (it needs its process's main thread, and pystray + libmpv in one
process segfaults with GNOME AppIndicator). What's testable here is the
parent side: how commands from that child are dispatched.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import multiprocessing
import os
import sys
import threading
import unittest
from unittest import mock

from jellyfin_mpv_shim import tray
from jellyfin_mpv_shim.tray import (
    TrayManager,
    backend_name,
    tray_unavailable_advice,
    tray_will_render,
    wants_x11_backend,
)
from tests import _tmpdirs


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
        from jellyfin_mpv_shim.tray import NO_WATCHER

        self.assertIs(True, tray_will_render("appindicator",
                                             sni=lambda: True,
                                             xembed=lambda: False))
        self.assertIs(False, tray_will_render("appindicator",
                                              sni=lambda: NO_WATCHER,
                                              xembed=lambda: False))

    def test_an_appindicator_falls_back_to_the_old_style_tray(self):
        """No D-Bus host is not no tray (#4).

        libappindicator and libayatana-appindicator both keep a
        GtkStatusIcon fallback and use it exactly when no
        StatusNotifierWatcher owns the name. So on i3 with i3bar's tray,
        xfce4-panel or tint2 -- most of X11 that is not KDE -- the icon
        docks and works, while the D-Bus probe alone says there is no tray
        and the app offers "Keep Running in Background" to somebody
        looking straight at their icon.
        """
        from jellyfin_mpv_shim.tray import NO_WATCHER

        self.assertIs(True, tray_will_render("appindicator",
                                             sni=lambda: NO_WATCHER,
                                             xembed=lambda: True))

    def test_a_watcher_with_no_host_is_worse_than_no_watcher(self):
        """The distinction the fallback turns on, and the one case where an
        XEmbed tray on the same desktop must NOT rescue the verdict.

        With nobody owning the name, libappindicator docks a GtkStatusIcon.
        With a watcher present but no host registered behind it, the item
        registers successfully, the fallback never starts, and nothing draws
        it -- so asking about XEmbed would turn an invisible icon into a
        confident yes. Reachable on an X11 Plasma/xfce session where a
        half-started watcher owns the name while xembedsniproxy owns the
        old-style selection.
        """
        self.assertIs(False, tray_will_render("appindicator",
                                              sni=lambda: False,
                                              xembed=lambda: True))

    def test_an_unanswerable_fallback_is_not_a_confident_no(self):
        # Without an X connection the fallback cannot be ruled out, and a
        # maybe must not read as a no.
        from jellyfin_mpv_shim.tray import NO_WATCHER

        self.assertIsNone(tray_will_render("appindicator",
                                           sni=lambda: NO_WATCHER,
                                           xembed=lambda: None))

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
        self.assertIsNone(tray_will_render("appindicator", sni=lambda: None,
                                           xembed=lambda: None))
        self.assertIsNone(tray_will_render("appindicator", sni=lambda: None,
                                           xembed=lambda: False))
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



class TestTrayTempDir(unittest.TestCase):
    """Where the tray child is allowed to put files, and who clears up.

    pystray's GTK backends publish the icon by writing it to a bare
    ``tempfile.mktemp()`` and unlinking it in a finalizer this child never
    reaches: ``_reset_inherited_signals`` restores SIGTERM to ``SIG_DFL``,
    so ``stop()``'s ``terminate()`` kills it outright. One 6 KB PNG per run
    of the app, for ever, under a name with nothing in it to say whose it
    was. The fix gives the child a directory the parent owns.
    """

    class _FakeProcess:
        """Enough of a Process for start()/stop(), started or not."""

        def __init__(self, queue, tmpdir=None):
            self.queue = queue
            self.tmpdir = tmpdir
            self.started = False
            self.pid = 1234

        def start(self):
            self.started = True

        def terminate(self):
            self.started = False

        def kill(self):
            self.started = False

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return self.started

    def _managed(self, process_cls=None):
        made = []

        def factory(queue, tmpdir=None):
            proc = (process_cls or self._FakeProcess)(queue, tmpdir)
            made.append(proc)
            return proc

        patcher = mock.patch.object(tray, "TrayProcess", factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        manager = TrayManager({})
        self.addCleanup(manager.stop)
        return manager, made

    def test_the_child_is_given_a_directory_the_parent_can_find_again(self):
        manager, made = self._managed()
        self.assertTrue(manager.start())
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].tmpdir, manager._tmpdir)
        self.assertTrue(os.path.isdir(manager._tmpdir))
        self.assertTrue(os.path.basename(manager._tmpdir)
                        .startswith("jms-tray-"),
                        "a leftover has to say whose it is")

    def test_stopping_removes_it(self):
        manager, _ = self._managed()
        manager.start()
        path = manager._tmpdir
        manager.stop()
        self.assertFalse(os.path.exists(path))

    def test_a_stop_with_no_child_still_clears_up(self):
        """stop() leaves no directory behind on either of its paths --
        the one that joins a child and the one that finds none."""
        manager, _ = self._managed()
        manager.start()
        path, manager._process = manager._tmpdir, None
        manager.stop()
        self.assertFalse(os.path.exists(path))

    def test_stopping_twice_is_safe(self):
        manager, _ = self._managed()
        manager.start()
        manager.stop()
        manager.stop()           # must not raise
        self.assertIsNone(manager._tmpdir)

    def test_a_child_that_never_started_leaves_nothing_behind(self):
        class Boom(self._FakeProcess):
            def start(self):
                raise OSError("no processes left")

        manager, made = self._managed(Boom)
        self.assertFalse(manager.start())
        self.assertFalse(os.path.exists(made[0].tmpdir))

    def test_a_directory_we_cannot_make_is_not_fatal(self):
        """The tray is optional; a full disk should cost the cleanup, not
        the icon. The child falls back to the system temp directory."""
        manager, made = self._managed()
        with mock.patch.object(tray.tempfile, "mkdtemp",
                               side_effect=OSError("no space")):
            self.assertTrue(manager.start())
        self.assertIsNone(manager._tmpdir)
        self.assertIsNone(made[0].tmpdir)

    def test_the_redirect_moves_the_temp_files_pystray_writes(self):
        """The property, not the mechanism: after the redirect, the call
        pystray makes for its icon path lands inside our directory."""
        import tempfile as tf

        self.addCleanup(setattr, tf, "tempdir", tf.tempdir)
        target = _tmpdirs.tmpdir(prefix="jms-trayredirect-")
        with mock.patch.dict(os.environ, {}, clear=False):
            tray._use_private_temp_dir(target)
            self.assertEqual(os.path.dirname(tf.mktemp()), target)
            self.assertEqual(os.path.dirname(tf.mkdtemp()), target)
            # GLib reads only the environment, and GDK writes there too.
            for name in ("TMPDIR", "TMP", "TEMP"):
                self.assertEqual(os.environ[name], target)

    def test_the_child_redirects_before_it_imports_pystray(self):
        """``run()`` for real, as far as the missing-pystray exit -- which
        is the point of the assertion: a redirect written after that import
        would never be reached here, and the ordering is what makes it
        cover the icon file. A blocked ``PIL`` is the shortest way to that
        exit, and the optional-dependency policy says it is a real state.
        """
        seen = []
        queue = mock.Mock()
        proc = tray.TrayProcess(queue, "/nowhere/jms-tray-test")
        with mock.patch.object(tray, "_reset_inherited_signals"), \
                mock.patch.object(tray, "_use_private_temp_dir",
                                  side_effect=seen.append), \
                mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.dict(sys.modules, {"PIL": None}):
            proc.run()
        self.assertEqual(seen, ["/nowhere/jms-tray-test"])
        queue.put.assert_called_with(("tray_died", None))

    def test_no_directory_means_no_redirect(self):
        """A child that was given nothing must leave the system temp
        directory alone rather than redirect at None."""
        seen = []
        proc = tray.TrayProcess(mock.Mock(), None)
        with mock.patch.object(tray, "_reset_inherited_signals"), \
                mock.patch.object(tray, "_use_private_temp_dir",
                                  side_effect=seen.append), \
                mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.dict(sys.modules, {"PIL": None}):
            proc.run()
        self.assertEqual(seen, [])

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


class TestTrayIconArtwork(unittest.TestCase):
    """The bitmap handed to pystray.

    Every backend takes this one image and produces the size IT wants -- an
    ICO frame set on win32, a LANCZOS downscale to the status bar thickness
    on darwin, a resize to the tray's request on xorg, a PNG on disk for the
    panel to scale on gtk/appindicator. So the source being larger than any
    panel is the supported direction, and the source being smaller is the one
    nothing can recover from: this shipped at 16px for years and was mush on
    every HiDPI panel and every KDE tray asking for 32 or 48.
    """

    def _icon(self):
        from PIL import Image

        from jellyfin_mpv_shim.utils import get_resource

        return Image.open(get_resource("systray.png"))

    def test_the_icon_is_big_enough_for_a_hidpi_panel(self):
        icon = self._icon()
        self.assertGreaterEqual(min(icon.size), 128)
        # ICO's ceiling is 256, and win32 goes through one.
        self.assertLessEqual(max(icon.size), 256)

    def test_it_is_square(self):
        """A tray slot is square, and every backend resizes to a square box;
        a non-square source comes out stretched on the ones that do not
        letterbox."""
        icon = self._icon()
        self.assertEqual(icon.size[0], icon.size[1])

    def test_it_has_a_transparent_background(self):
        """The mark, not the app logo. `logo.png` is the same artwork on an
        opaque dark plate, which in a light panel is a black square with a
        symbol in it -- and it is the obvious file to reach for."""
        icon = self._icon().convert("RGBA")
        corners = [icon.getpixel(p) for p in
                   ((0, 0), (icon.width - 1, 0), (0, icon.height - 1),
                    (icon.width - 1, icon.height - 1))]
        for pixel in corners:
            self.assertEqual(pixel[3], 0, "the tray icon carries a plate")

    def test_saving_it_as_an_ico_offers_every_panel_size(self):
        """What win32 actually does with it: pystray saves the image as an
        ICO and calls LoadImage(LR_DEFAULTSIZE), which picks the frame
        matching the system metric. Pillow writes every standard size UP TO
        the source, so a 16px source offered exactly one frame and Windows
        had nothing to pick."""
        import io

        from PIL import Image

        buf = io.BytesIO()
        self._icon().save(buf, format="ICO")
        buf.seek(0)
        sizes = set(Image.open(buf).info.get("sizes") or ())
        for want in ((16, 16), (32, 32), (48, 48)):
            self.assertIn(want, sizes)
