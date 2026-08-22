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
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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


if __name__ == "__main__":
    unittest.main()
