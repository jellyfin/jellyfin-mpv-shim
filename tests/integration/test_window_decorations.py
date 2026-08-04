"""Whether a REAL mpv will answer about this window's decorations.

Named for the question rather than for the feature: the UI half
(buttons, drag) is ``tests/test_window_controls.py`` and
``tests/e2e/test_window_controls.py``. Discovery also refuses two
modules with one basename under ``tests/``.

The unit tests (``tests/test_window_controls.py``) answer "given that mpv
says X, does the UI do Y". They cannot answer the question this file exists
for: **does mpv actually say anything?**

The whole feature hangs off reading three mpv properties and issuing two
commands, and every one of them is a place the two backends differ. libmpv
turns an unknown attribute into a property read via ``__getattr__`` and
raises its own error type; jsonipc round-trips over a socket and raises
another. ``_read_decorations`` swallows both and returns None, and None
means "leave the window alone" -- so a property that stopped being readable
does not raise, does not log, and does not fail a unit test. It silently
turns the feature off *forever*, on whichever backend broke.

That is the failure mode worth a real-mpv test: the controls are for a
desktop most of this client's users are not on, so nobody would notice for a
release or two.

Deliberately no server and no browser here. This is the player's contract
with mpv; the whole chain (real server, real mpv, real renderer, real
composited scene) is ``tests/e2e/test_window_controls.py``.

**X11 is what makes this testable.** mpv honours ``--border`` there, so the
property can be driven both ways at runtime and the answer checked against
it. On GNOME Wayland the compositor is the one that decides, which is
exactly why the shim asks mpv rather than the environment -- and also why
that direction cannot be asserted from a test harness.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import _harness as h  # noqa: E402

from test_mpvtk_browser import _spawn_handle  # noqa: E402


class _Window:
    """The real ``WindowMixin`` methods over one owned mpv handle.

    Composed rather than reimplemented: the whole point is to run the code
    that ships. ``_observe`` is borrowed off ``PlayerManager`` for the same
    reason -- it is the piece that has to register a BOUND METHOD on both
    backends, which is where this breaks on exactly one of them.
    """

    def __init__(self, handle):
        from jellyfin_mpv_shim.player_window import WindowMixin

        class _W(WindowMixin):
            pass

        self._impl = _W()
        self._impl._player = handle
        self._impl._mpv_alive = True
        self._impl.on_decorations_changed = None
        self._handle = handle

    def __getattr__(self, name):
        return getattr(self._impl, name)

    def __setattr__(self, name, value):
        if name in ("_impl", "_handle"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._impl, name, value)


@h.require_real_mpv
class WindowDecorationsOnRealMpv(unittest.TestCase):
    """The player's window-decoration reads, against a live mpv."""

    #: Its OWN mpv, not the module-level singleton.
    #:
    #: The singleton is shared with every other real-mpv module in the
    #: process and is dead by the time this one runs in a combined suite --
    #: which showed up as eleven tests that skipped themselves on jsonipc
    #: and told nobody. A test that can silently stop running is worse than
    #: no test, and this file exists precisely because the feature under it
    #: fails silently.
    #:
    #: `_SPAWN_OPTS` already asks for `force_window`, so the window is up as
    #: soon as the handle is, and nothing here needs the player's lifecycle.
    #: The methods under test are real ones, borrowed off the real classes
    #: and given this handle -- see `_Window` below.
    @classmethod
    def setUpClass(cls):
        # conffile reaches get_args(), which would otherwise parse unittest's
        # own argv and exit.
        h.prime_args()
        cls.handle, _ext = _spawn_handle()
        cls.win = _Window(cls.handle)
        cls.ready = cls._wait_for_window(cls.handle)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.handle.terminate()
        except Exception:
            pass

    @staticmethod
    def _wait_for_window(handle, timeout=25.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if handle.osd_width:
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def setUp(self):
        from jellyfin_mpv_shim.conf import settings

        if not self.ready:
            self.skipTest("mpv brought no window up, so it has no decorations "
                          "to report. Needs a display (run under xvfb).")
        self.settings = settings
        prior = settings.window_controls
        self.addCleanup(setattr, settings, "window_controls", prior)
        settings.window_controls = "auto"
        # One window across the class, so put the border back for the next
        # test rather than leaving it wherever this one finished.
        self.addCleanup(self._set_border, True)

    def _set_border(self, value, timeout=10.0):
        """Drive `border` and wait for mpv to report it back.

        Written and then re-read rather than assumed: on Wayland mpv
        overwrites this with whatever the compositor granted, and a test that
        trusted its own write would be asserting on its own input.
        """
        self.handle.border = value
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if bool(self.handle.border) is value:
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    # -- the reads ---------------------------------------------------------

    def test_border_is_readable_at_all(self):
        """The linchpin. Unreadable means _read_decorations returns None,
        which means "leave the window alone" -- so the feature turns itself
        off silently and permanently on this backend."""
        self.assertIsNotNone(
            self.win._read_decorations(),
            "mpv would not answer about this window's decorations on the %s "
            "backend, so the window controls can never appear on it"
            % h.BACKEND)

    def test_a_decorated_window_is_reported_as_decorated(self):
        self.assertTrue(self._set_border(True), "mpv would not take border=yes")
        self.assertIs(self.win._read_decorations(), True)
        self.assertFalse(self.win.window_controls_wanted(),
                         "a window with a title bar was offered a second one")

    def test_dropping_the_border_asks_for_our_own_title_bar(self):
        """The condition the feature exists for. GNOME Wayland reaches it
        because mutter implements no xdg-decoration and mpv writes
        border=false itself; here we reach it by asking, which drives the
        same property through the same read."""
        self.assertTrue(self._set_border(False), "mpv would not take border=no")
        self.assertIs(self.win._read_decorations(), False)
        self.assertTrue(self.win.window_controls_wanted())

    def test_the_answer_follows_mpv_rather_than_sticking(self):
        # Both directions, on one window: a cached first read would pass the
        # two tests above and still never update on a live compositor.
        self.assertTrue(self._set_border(False))
        self.assertTrue(self.win.window_controls_wanted())
        self.assertTrue(self._set_border(True))
        self.assertFalse(self.win.window_controls_wanted())

    def test_the_snapshot_the_ui_actually_reads(self):
        self.assertTrue(self._set_border(False))
        state = self.win.window_chrome_state()
        self.assertEqual(set(state), {"controls", "maximized"})
        self.assertIs(state["controls"], True)
        self.assertIsInstance(state["maximized"], bool)

    def test_the_setting_still_overrides_a_live_mpv(self):
        self.assertTrue(self._set_border(True))
        self.settings.window_controls = "always"
        self.assertTrue(self.win.window_controls_wanted())
        self.settings.window_controls = "never"
        self.assertTrue(self._set_border(False))
        self.assertFalse(self.win.window_controls_wanted())

    # -- the observer is NOT here, on purpose ------------------------------
    #
    # "does a `border` change reach the callback" was written here, and had
    # to go: on libmpv, registering an observer on a raw handle and then
    # terminating it segfaults the interpreter on the way out (rc=139),
    # unregistering first included. The app never meets that -- it drops the
    # whole handle through PlayerManager -- but a test harness holding its own
    # handle does, and a leg that segfaults after passing is a failing leg.
    #
    # What that test was actually for is the bound-method hazard in the
    # registration itself, and that is not a question about a live mpv: it is
    # a question about which API is called. It moved to
    # tests/test_mpv_observe.py, which drives both backend SHAPES with fakes
    # and can assert the thing a real mpv cannot -- that the decorator known
    # to reject bound methods is not the one being used.

    # -- the commands the buttons and the drag depend on -------------------

    def test_maximize_round_trips_through_mpv(self):
        before = bool(self.handle.window_maximized)
        self.assertTrue(self.win.toggle_window_maximized())
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if bool(self.handle.window_maximized) is not before:
                break
            time.sleep(0.1)
        self.assertIsNot(bool(self.handle.window_maximized), before,
                         "the maximize button does not move the window")

    def test_minimize_is_accepted(self):
        # Whether a WM actually iconifies is not ours (there is none under
        # xvfb); that mpv takes the property is.
        self.assertTrue(self.win.minimize_window())

    def test_the_drag_command_exists_on_this_mpv(self):
        """``begin-vo-dragging`` is mpv 0.38+. On an older build the drag is
        simply dead -- the bar looks draggable and is not -- so knowing which
        it is here is worth more than the pcall that hides it at runtime."""
        commands = {c.get("name") for c in
                    (self.handle.command_list or [])}
        if "begin-vo-dragging" not in commands:
            self.skipTest("mpv %s predates begin-vo-dragging (0.38+); the "
                          "title bar cannot be dragged on this build"
                          % getattr(self.handle, "mpv_version", "?"))
        # Deliberately NOT issued here. ``begin_window_drag`` routes through
        # ``_window_command``, which imports ``player`` for its error tuple --
        # and importing that module constructs a whole second mpv at module
        # scope, which then segfaults the interpreter on the way out. The
        # question this test asks is whether the command exists on this
        # build; the two-line wrapper around it is unit-tested.

    def test_an_absent_property_is_not_an_error(self):
        """`title-bar` is mpv 0.38+, and its absence has to read as "this
        build only has `border`" rather than "there is no title bar" -- the
        latter would put controls on every window on every older mpv."""
        self.assertTrue(self._set_border(True))
        self.assertIs(self.win._read_decorations(), True,
                      "an older mpv without `title-bar` was read as having "
                      "no title bar at all")


if __name__ == "__main__":
    unittest.main()
