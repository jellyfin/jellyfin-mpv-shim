"""The Settings screen against the REAL config module and a REAL gateway.

`tests/test_shell_settings.py` drives this screen hard, but through a
`FakeConfig` with five settings and a `FakeController` that answers
everything. That is the right trade for a fast suite, and it leaves one
thing uncovered: the screen as the user meets it, with a hundred real
settings, real notes, and a controller that has to ask **mpv** some of the
questions the form asks it.

The dynamic parts are where that bites. `_dynamic_enum("audio_device")` asks
the gateway, which asks mpv for `audio-device-list`; `config.tray_available()`
reaches for the running UI; `_dynamic_note("hwdec")` parses the user's
`mpv.conf`. None of those exists in the fast suite, and all of them run on
every render of the Playback tab.

Per backend for the same reason the rest of the real-mpv legs are: the device
list arrives over a socket on one and through the C API on the other.

**This module owns nothing it did not create.** The real player is a
process-wide singleton that `test_realmpv_smoke` and `test_realmpv_picture`
share, and the whole-suite leg runs all three in one process -- so tearing it
down here took fourteen of their tests with it the first time this was
written. The same goes for the config: only `conf.config_path` is redirected,
for the one test that has to reach the disk, and it is put back.

Only the three schema-driven tabs are rendered. The other four are their own
screens (the server list, the download manager, the log tail) and start
pollers when they are drawn; a test that leaves those threads running is a
test that poisons whatever runs next.
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# ...and the repo root. Run as a script -- which the __main__ block at the
# bottom invites -- `sys.path[0]` is this directory and the root is on the
# path nowhere, so `jellyfin_mpv_shim` resolves to whatever is pip-installed:
# silently, and it *runs*, against the previous release. Measured once as a
# renderer.lua from a fortnight ago failing a test about this tree.
# run_integration.py is unaffected (it spawns -m unittest with cwd=root).
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
import _harness as h  # noqa: E402


@h.require_real_mpv
class RealSettingsScreenTest(unittest.TestCase):
    #: The tabs that are the schema-driven config form. The rest are other
    #: screens entirely; see the module docstring.
    FORM_TABS = ("general", "browse", "playback")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="jms-settings-")
        # The player is a shared singleton -- take it, do not make it, and
        # above all do not terminate it on the way out.
        from test_realmpv_smoke import _import_real_player

        # `_import_real_player` revives the shared singleton if a module
        # that ran earlier terminated it; without that this screen asks a
        # dead player for its audio devices and gets an empty list, which
        # reads exactly like a real answer.
        cls.player_module = _import_real_player()
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway
        from tests._shell_harness import FakeSource

        cls.browser = MpvtkBrowser(app=None, source=FakeSource(),
                                   controller=PlayerGateway())

    @classmethod
    def tearDownClass(cls):
        # Stop the browser's daemon pollers before the class goes away.
        try:
            cls.browser._shutdown_evt.set()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.browser._open_settings()
        # The browser -- and therefore its route dict -- is class-level, so
        # anything a test puts in the route is still there for the next one.
        # A live search query is the sharp case: it replaces the tab's
        # contents wholesale, so the following test renders the results
        # screen and asserts against a form that is not on display. That is
        # how the first version of this file failed.
        self.browser.route.pop("_q", None)
        self.browser.route["_tab"] = "general"
        self.browser.route["_advanced"] = False
        self.browser._restart_keys = set()

    def _scene(self):
        from jellyfin_mpv_shim.mpvtk.layout import layout

        nodes, handlers = layout(self.browser.build((1280, 720)), 1280, 720)
        return nodes, handlers

    def _ids(self, nodes):
        return {n.get("id") for n in nodes}

    def _texts(self, nodes):
        return " ".join(n.get("text", "") for n in nodes if n.get("text"))

    def test_every_tab_renders_with_the_real_config(self):
        """A hundred real settings, real notes, real dynamic enums. A tab
        that raises here takes the whole window with it -- the render loop
        has nowhere to put an exception."""
        for tab in self.FORM_TABS:
            with self.subTest(tab=tab):
                self.browser.route["_tab"] = tab
                self.browser.route["_advanced"] = True
                nodes, _h = self._scene()
                self.assertTrue(nodes, "%s rendered nothing" % tab)

    def test_the_audio_device_list_really_comes_from_mpv(self):
        """The one control whose options are neither a literal nor a file:
        the gateway asks mpv for `audio-device-list`. A backend that answers
        differently leaves this dropdown empty, and the fast suite cannot
        see that because it stubs the controller."""
        self.browser.route["_tab"] = "playback"
        nodes, _h = self._scene()
        self.assertIn("set-audio_device", self._ids(nodes))
        options = self.browser._dynamic_enum("audio_device")
        self.assertTrue(options, "mpv returned no audio devices")
        # The list this machine has is not something to assert on, so the
        # claim is its shape: a "let mpv decide" entry -- spelled as a None
        # value, not the string "auto" -- followed by whatever mpv found.
        # Summarised in the message rather than dumped: the real list runs
        # to seventy devices, and a failure that prints all of them is one
        # nobody reads.
        summary = "%d options, first three: %r" % (len(options), options[:3])
        self.assertIsNone(options[0][1],
                          "the default entry is not the one that lets mpv "
                          "choose (%s)" % summary)
        self.assertGreater(len(options), 1,
                           "mpv listed no devices at all (%s)" % summary)
        self.assertTrue(
            any(isinstance(v, str) and v for _l, v in options[1:]),
            "no device has a name to select it by (%s)" % summary)

    def test_search_finds_a_setting_on_another_tab(self):
        """Against the real corpus rather than a three-entry stand-in --
        which is the only way to know the notes are actually reachable."""
        self.browser.route["_tab"] = "general"
        self.browser.route["_q"] = "banding"
        nodes, _h = self._scene()
        self.assertIn("set-deband", self._ids(nodes))

    def test_a_restart_required_setting_is_marked(self):
        self.browser.route["_tab"] = "general"
        self.browser.route.pop("_q", None)
        nodes, _h = self._scene()
        self.assertIn("Requires restart", self._texts(nodes))

    def test_the_restart_banner_renders_against_the_real_settings(self):
        """The banner names the settings, through the real label table --
        which the fast suite cannot check, because its stand-in has one
        made-up key.

        **Nothing here writes a setting.** This process shares one settings
        object with every other module in the leg, and `_set_setting` ends
        in `settings.save()`, which writes to wherever `conf.config_path`
        currently points -- a global other modules legitimately repoint
        while they run. Two attempts to redirect it were both still wrong in
        the whole-suite leg, and a stubbed `save` was not even the one
        called; a test that has to win a race over where the app writes its
        config can write it somewhere real, and asserting a file hop is not
        worth that. The recording itself is unit-tested
        (`tests/test_shell_settings.py`), and the write path through the
        real config module by `tests/test_settings_nullable.py`.
        """
        from jellyfin_mpv_shim.mpvtk_browser import config as cfgmod

        key = sorted(cfgmod.RESTART_REQUIRED)[0]
        self.browser._restart_keys = {key}
        nodes, _h = self._scene()
        self.assertIn("banner-restart-dismiss", self._ids(nodes))
        self.assertIn(cfgmod.label_for(key), self._texts(nodes))


@h.require_real_mpv
class RealSettingsClickTest(unittest.TestCase):
    """A real pointer on the settings form.

    `RealSettingsScreenTest` above renders every tab and clicks nothing, and
    it cannot: it runs with `app=None` and calls `layout()` itself, so there
    is no renderer to press a button on and no repaint to observe.

    It asserts against the scene the RENDERER holds after a real click, not
    against a tree the test rebuilt -- CLAUDE.md's "a scene assertion is not
    a repaint assertion". That distinction is load-bearing here: measured, a
    fresh `build()` and the renderer's scene DO disagree when the toggle
    half-fails, and only the renderer's copy says what a user sees.

    **It does not pin the Checkbox repaint footgun, and the docstring says so
    because the control said so.** The intent was to catch a handler that
    writes the setting without asking for a repaint -- a `Checkbox` is
    Box-plus-tick coloured from `checked` and, unlike the renderer's
    optimistic Dropdown and TextBox, only a redraw moves its tick. But a
    control removing `set_status`'s `self.invalidate()` leaves this test
    green: a pointer press changes hover/pressed state in the renderer,
    which calls `request_render` on its own, so a click repaints whether or
    not the handler asks. **The footgun is therefore not reachable through
    the mouse at all** -- it needs a trigger with no pointer gesture behind
    it -- and a test that claimed otherwise would be claiming a guarantee it
    does not provide.

    The config directory is a throwaway -- `_harness.prime_args` redirects it
    at import, and this file's assertions would otherwise write the
    developer's own `conf.json`.
    """

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
        from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore, cache_dir
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway
        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore
        from tests._shell_harness import FakeSource
        from test_mpvtk_browser import _spawn_handle

        # **Give `settings` somewhere to save to, or this test asserts the
        # wrong thing and looks like a bug in the app.** `conf.config_path`
        # is None until something calls `load`, and `save()` raises
        # FileNotFoundError without it -- *after* `set_setting` has already
        # written the value in memory. The toggle then appears to work while
        # `_set_setting` never reaches its `set_status`, so nothing repaints
        # and the tick stays put: exactly the failure this test is looking
        # for, produced by the harness rather than by the code. Measured
        # before the load was added here.
        self.conf_dir = tempfile.mkdtemp(prefix="jms-setclick-")
        self.addCleanup(shutil.rmtree, self.conf_dir, ignore_errors=True)
        from jellyfin_mpv_shim.conf import settings as _settings
        _settings.load(os.path.join(self.conf_dir, "conf.json"))

        self.handle, ext = _spawn_handle()
        self.app = MpvtkApp.attach(self.handle, ext=ext)
        strips = (StripStore(mem_store=MemoryStore()) if self.app.in_process
                  else StripStore(cache_dir=cache_dir("mpvtk-setclick-")))
        self.browser = MpvtkBrowser(self.app, FakeSource(), strips=strips,
                                    controller=PlayerGateway())
        self._thread = threading.Thread(
            target=lambda: self.app.run(self.browser.build), daemon=True)
        self._thread.start()
        self.addCleanup(self._teardown)
        self.assertTrue(self.app.ready.wait(15), "renderer never came up")
        self.browser._open_settings()
        self.browser.route["_tab"] = "general"
        self.browser.route["_advanced"] = False
        self.browser.invalidate()
        self._wait(lambda: self._checkbox() is not None,
                   "the settings form never drew a checkbox")

    def _teardown(self):
        try:
            self.app.quit()
            self._thread.join(timeout=5)
        finally:
            try:
                self.browser.shutdown(free_bitmaps=False)
            except Exception:
                pass
            try:
                self.handle.terminate()
            except Exception:
                pass

    def _wait(self, pred, why, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            time.sleep(0.15)
        self.fail(why)

    def _state(self):
        return self.app.debug_state() or {}

    def _checkbox(self):
        """The first `set-<key>` row that is a real boolean setting.

        By key rather than by a hardcoded name so this does not become a
        test of one setting's continued existence; `set-adv` is excluded
        because it toggles the form itself rather than a config value.
        """
        from jellyfin_mpv_shim.conf import settings

        st = self._state()
        ww, wh = st.get("w") or 0, st.get("h") or 0
        for n in (self.app._nodes or []):
            nid = str(n.get("id") or "")
            if not nid.startswith("set-") or nid == "set-adv":
                continue
            key = nid[4:]
            if not isinstance(getattr(settings, key, None), bool):
                continue
            if not (n.get("w") and n.get("h")):
                continue
            cx, cy = int(n["x"] + n["w"] / 2), int(n["y"] + n["h"] / 2)
            if 0 <= cx < ww and 0 <= cy < wh:
                return nid, key, n
        return None

    def _tick_fills(self, nid):
        """The fills drawn inside the row -- the Box whose colour IS the
        checked state. The Box carries no id of its own (only the Checkbox
        row does), so it is found by geometry.

        The row is re-found by id on every call rather than captured once:
        saving raises a "Saved: <key>" toast, which repaints and moves the
        form under it, so a rect held from before the click describes a
        different part of the screen afterwards.
        """
        nodes = self.app._nodes or []
        row = next((n for n in nodes if n.get("id") == nid), None)
        if row is None:
            return None
        out = []
        for n in nodes:
            if not n.get("fill"):
                continue
            if (n.get("x", -1) >= row["x"] - 1
                    and n.get("y", -1) >= row["y"] - 1
                    and n["x"] + n.get("w", 0) <= row["x"] + row["w"] + 1
                    and n["y"] + n.get("h", 0) <= row["y"] + row["h"] + 1):
                out.append(n["fill"])
        return out

    def test_a_keyboard_activation_toggles_the_setting_and_shows_it(self):
        """The keyboard twin of the click test: a settings toggle reached
        by focus and ENTER, which is how a remote or a keyboard user gets
        there and which nothing else covers.

        **It was written to pin the Checkbox repaint footgun and it does
        not, so the docstring says so rather than the name implying it.**
        The footgun is real -- a Checkbox is Box-plus-tick coloured from
        `checked` and, unlike the renderer's optimistic Dropdown and
        TextBox, only a redraw moves its tick, so a handler that writes and
        asks for no repaint leaves the form showing the old value. The
        theory was that a keyless activation would expose it where a
        pointer press could not.

        Measured: a control removing `set_status`'s `self.invalidate()`
        leaves BOTH this and the click test green. `nav_activate` calls
        `request_render()` on every branch that activates something, and
        `on_mouse_up` does the same -- **the renderer repaints on any
        activation, pointer or key**. So this footgun cannot be reached
        from any input path at all; it can only bite a state change with
        nothing driving it -- a poller, a websocket event, a timer -- and a
        test for that belongs where such a change happens, asserting
        `invalidate` was called, per CLAUDE.md. Nothing on this screen has
        one, which is why no such test is added here.
        """
        from jellyfin_mpv_shim.conf import settings

        nid, key, row = self._checkbox()
        before_value = getattr(settings, key)
        before_fills = self._tick_fills(nid)
        self.addCleanup(setattr, settings, key, before_value)

        # Park focus, and let that repaint settle BEFORE the activation --
        # moving focus draws a ring, which is a repaint of its own and would
        # otherwise be the one this test mistook for the handler's.
        self.app.debug(cmd="nav", id=nid)
        self._wait(lambda: self._state().get("nav") == nid,
                   "focus never landed on %s" % nid)
        time.sleep(0.5)
        settled = self._tick_fills(nid)

        self.handle.command("keypress", "ENTER")
        self._wait(lambda: getattr(settings, key) != before_value,
                   "ENTER on the focused checkbox did not toggle %s" % key)
        self._wait(
            lambda: self._tick_fills(nid) not in (None, settled),
            "%s toggled to %r and the tick never moved -- the handler wrote "
            "the setting without asking for a repaint, and with no pointer "
            "gesture to repaint on its behalf the form now shows the wrong "
            "value" % (key, getattr(settings, key)))

    def test_a_real_click_toggles_the_setting_and_moves_the_tick(self):
        from jellyfin_mpv_shim.conf import settings

        nid, key, row = self._checkbox()
        before_value = getattr(settings, key)
        before_fills = self._tick_fills(nid)
        self.addCleanup(setattr, settings, key, before_value)

        cx = int(row["x"] + row["w"] / 2)
        cy = int(row["y"] + row["h"] / 2)
        self.handle.command("mouse", cx + 6, cy + 4)
        time.sleep(0.15)
        self.handle.command("mouse", cx, cy)
        self._wait(lambda: self._state().get("hover") == nid,
                   "the pointer never came to rest on %s" % nid)
        self.handle.command("keydown", "MBTN_LEFT")
        time.sleep(0.1)
        self.handle.command("keyup", "MBTN_LEFT")

        self._wait(lambda: getattr(settings, key) != before_value,
                   "a real click on %s did not toggle the setting" % key)
        # The half a scene assertion cannot make: the tick only moves on a
        # REDRAW, so this fails if the handler wrote the value and asked for
        # no repaint -- the form would sit there showing the old state.
        self._wait(
            lambda: self._tick_fills(nid) not in (None, before_fills),
            "%s toggled to %r but the renderer never redrew the tick -- the "
            "handler wrote the setting without asking for a repaint"
            % (key, getattr(settings, key)))


if __name__ == "__main__":
    unittest.main()
