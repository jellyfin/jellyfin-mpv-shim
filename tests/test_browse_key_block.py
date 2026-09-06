"""Blocking mpv's own keyboard shortcuts while the library is on screen.

#730. mpv is constructed with ``input_default_bindings=True`` for the life
of the process (``player.py`` ``_init_mpv``), so in the library every key
the shim has not taken is still mpv's. Measured against a real mpv with the
browser up, before any of this existed: `1` moved contrast 0.0 -> -1.0, `9`
moved the volume and `m` muted, with mpv's own OSD as the only feedback --
and that OSD is ASS, so it draws *underneath* the overlay bitmaps the
library is made of, which is what the report is a screenshot of.

Two halves, and they are tested in different places:

* **The block itself is one forced ``any_unicode`` binding**, which outranks
  every exact-key default across the printable range. That is a claim about
  mpv, so it is measured against a real one in
  ``tests/integration/test_mpvtk_browser.py``. What is here is the Python
  side: whether the flag is pushed, and when.
* **The volume keys the browser claims back** while music is playing, so
  `9`/`0`/`m` keep working -- through the shell rather than through mpv, so
  the now-playing bar's slider moves with them.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.conf import settings  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from tests._shell_harness import (  # noqa: E402
    FakeConfig,
    FakeController,
    FakeSource,
    _SyncPool,
)


class _Base(unittest.TestCase):
    def _block(self, enabled):
        saved = settings.browse_block_keys
        self.addCleanup(
            lambda: setattr(settings, "browse_block_keys", saved))
        settings.browse_block_keys = enabled

    def _browser(self, playing_audio=False):
        self.ctl = FakeController()
        b = MpvtkBrowser(app=mock.Mock(), source=FakeSource(),
                         controller=self.ctl, config=FakeConfig())
        b._pool = _SyncPool()
        b._now_playing = {"id": "t1", "volume": 100} if playing_audio else None
        return b


class ShellVolumeClaimTest(_Base):
    """Which keys the shell takes on top of the page's own."""

    def test_music_playing_claims_volume_and_mute(self):
        self._block(True)
        b = self._browser(playing_audio=True)
        self.assertEqual(set(b._shell_claimed_keys()), {"9", "0", "m"})

    def test_nothing_playing_claims_nothing(self):
        """The keys exist to serve the now-playing bar. With no bar there is
        nothing for them to move, and mpv's own volume is what the block is
        taking away in the first place."""
        self._block(True)
        b = self._browser(playing_audio=False)
        self.assertEqual(b._shell_claimed_keys(), ())

    def test_the_opt_out_gives_these_back_too(self):
        """Somebody who turned the block off asked for their own keyboard.
        Taking three keys out of it anyway would be the same interception
        with a smaller footprint."""
        self._block(False)
        b = self._browser(playing_audio=True)
        self.assertEqual(b._shell_claimed_keys(), ())

    def test_the_claim_reaches_the_renderer_beside_the_page_s_own(self):
        """Union, not replacement: a page that claims keys still gets them
        while music plays."""
        self._block(True)
        b = self._browser(playing_audio=True)
        b._browsing = True
        page = mock.Mock()
        page.claimed_keys = ("LEFT", "RIGHT")
        with mock.patch.object(b, "_page_for", return_value=page):
            b._claim_page_keys(b.route)
        pushed = set(b.app.claim_keys.call_args[0][0])
        self.assertEqual(pushed, {"LEFT", "RIGHT", "9", "0", "m"})


class ShellVolumeKeyTest(_Base):
    """What the claimed keys do when they arrive."""

    def _press(self, b, key):
        b._on_claimed_key(key)
        return [c for c in self.ctl.transport
                if c[0] in ("adjust_volume", "toggle_mute")]

    def test_the_keys_step_the_volume_mpv_s_own_way(self):
        """Same direction and same step as mpv's `9`/`0`, so the muscle
        memory the block took away still works."""
        self._block(True)
        b = self._browser(playing_audio=True)
        self.assertEqual(self._press(b, "9"), [("adjust_volume", (-2.0,))])
        self.assertEqual(self.ctl.volume_level, 98.0)
        b._on_claimed_key("0")
        self.assertEqual(self.ctl.volume_level, 100.0)

    def test_m_mutes(self):
        self._block(True)
        b = self._browser(playing_audio=True)
        self.assertEqual(self._press(b, "m"), [("toggle_mute", ())])
        self.assertTrue(self.ctl.muted)

    def test_repeated_presses_keep_moving(self):
        """The step is computed on the PLAYER thread, not from the pushed
        now-playing snapshot -- which arrives by playstate push, so under
        key repeat every press would otherwise compute from the same stale
        base and the volume would stick one notch down."""
        self._block(True)
        b = self._browser(playing_audio=True)
        for _ in range(4):
            b._on_claimed_key("9")
        self.assertEqual(self.ctl.volume_level, 92.0)

    def test_music_stopping_stops_the_keys(self):
        """The standing footgun of this shell: a claim is installed from a
        frame, the press arrives later, and the state read at BUILD time is
        whatever it was then. Read in the handler -- so a key still bound
        from the last frame does nothing once the bar is gone."""
        self._block(True)
        b = self._browser(playing_audio=True)
        b._now_playing = None
        before = list(self.ctl.transport)
        b._on_claimed_key("9")
        self.assertEqual(self.ctl.transport, before)
        self.assertEqual(self.ctl.volume_level, 100.0)

    def test_an_unclaimed_key_still_reaches_the_page(self):
        """The shell looks first, but only at its own three."""
        self._block(True)
        b = self._browser(playing_audio=True)
        page = mock.Mock()
        with mock.patch.object(b, "_page_for", return_value=page):
            b._on_claimed_key("LEFT")
        page.on_key.assert_called_once_with("LEFT")

    def test_a_volume_key_does_not_also_reach_the_page(self):
        self._block(True)
        b = self._browser(playing_audio=True)
        page = mock.Mock()
        with mock.patch.object(b, "_page_for", return_value=page):
            b._on_claimed_key("m")
        page.on_key.assert_not_called()


class PushTest(unittest.TestCase):
    """The flag the renderer acts on."""

    def _app(self):
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp

        app = MpvtkApp.__new__(MpvtkApp)
        app.backend = mock.Mock()
        return app

    def _pushed(self, enabled):
        saved = settings.browse_block_keys
        self.addCleanup(
            lambda: setattr(settings, "browse_block_keys", saved))
        settings.browse_block_keys = enabled
        app = self._app()
        app.push_browse_keys()
        return app.backend.command.call_args[0]

    def test_on(self):
        self.assertEqual(self._pushed(True),
                         ("script-message", "mpvtk-browse-keys", "yes"))

    def test_off(self):
        self.assertEqual(self._pushed(False),
                         ("script-message", "mpvtk-browse-keys", "no"))

    def test_it_is_pushed_at_startup(self):
        """`ui_resume` does not run on the app's first `mpvtk-active yes`
        (state.active begins true), so the push is what installs the block
        for the whole first browse session."""
        import inspect

        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp

        src = inspect.getsource(MpvtkApp._dispatch)
        self.assertIn("push_browse_keys()", src)


class LiveApplyTest(unittest.TestCase):
    """It has to take effect on the screen the user is looking at."""

    def _browser(self):
        cfg = FakeConfig()
        cfg.schema["browse_block_keys"] = "bool"
        cfg.values["browse_block_keys"] = True
        cfg.schema["player_name"] = "str"
        cfg.values["player_name"] = "x"
        b = MpvtkBrowser(app=mock.Mock(), source=FakeSource(),
                         controller=mock.Mock(), config=cfg)
        b._pool = _SyncPool()
        b.app.push_browse_keys.reset_mock()
        return b

    def test_saving_it_re_pushes(self):
        b = self._browser()
        b._set_setting("browse_block_keys", False)
        b.app.push_browse_keys.assert_called_once_with()

    def test_an_unrelated_setting_does_not(self):
        b = self._browser()
        b._set_setting("player_name", "Bud")
        b.app.push_browse_keys.assert_not_called()

    def test_it_is_not_marked_as_needing_a_restart(self):
        from jellyfin_mpv_shim.mpvtk_browser import config as cfg
        self.assertNotIn("browse_block_keys", cfg.RESTART_REQUIRED)


if __name__ == "__main__":
    unittest.main()
