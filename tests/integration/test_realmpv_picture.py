"""The picture settings against a REAL mpv, per backend.

`tests/test_picture_processing.py` checks the tables and
`tests/integration/test_picture_options.py` checks what the player writes --
but both of those talk to a stand-in that records attribute writes, so
between them they prove the shim *says* the right thing and nothing about
whether mpv agrees.

That gap is exactly where these settings fail. A property mpv does not have
raises at the write, which the player catches and logs at debug; a value
outside mpv's range is refused the same way; and `--interpolation` without a
display-sync `--video-sync` is, in mpv's own words, silently disabled. In
every case the picture is unchanged and the setting reads as applied. So the
assertions here are **read back from mpv** rather than from the shim.

Per backend because the two write properties differently: libmpv sets them
through the C API in-process, python-mpv-jsonipc sends `set_property` over a
socket and coerces types on the way. A value the one accepts and the other
mangles would be invisible from either side alone.

Playback is a local ffmpeg clip, as in `test_realmpv_smoke` and for the same
reason: no server, no transcode, and a real decode so the video chain is
actually built.
"""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as h  # noqa: E402

from test_realmpv_smoke import (  # noqa: E402
    FakeClient, RealVideo, _import_real_player)


@h.require_real_mpv
class RealMpvPictureTest(unittest.TestCase):
    """Every assertion reads the property back off the live player."""

    @classmethod
    def setUpClass(cls):
        cls.player_module = _import_real_player()
        cls.pm = cls.player_module.playerManager
        import threading

        cls.pm.action_trigger = threading.Event()
        cls.pm.timeline_trigger = threading.Event()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.pm.terminate()
        except Exception:
            pass

    def setUp(self):
        import shutil
        import tempfile

        self.tmp = tempfile.mkdtemp(prefix="jms-pic-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clip = h.make_test_clip(os.path.join(self.tmp, "a.mp4"),
                                     duration=2)
        self.client = FakeClient()
        # Every test leaves the settings alone for the next one; the player
        # singleton is shared across the class and a leaked preset would
        # apply to whatever ran next.
        for key in self.PRESETS:
            was = getattr(self.player_module.settings, key)
            self.addCleanup(setattr, self.player_module.settings, key, was)
        self._quiesce()

    #: Every preset-driven setting, and the value of each that means "leave
    #: mpv alone".
    PRESETS = {"deband": "off", "tone_mapping": "auto",
               "render_quality": "default", "network_buffer": "default",
               "motion_interpolation": "off"}

    def _quiesce(self):
        """Put the player back to the values mpv started with.

        The player is a class-level singleton, so a preset applied by one
        test is still on the player when the next one runs -- and a test
        that snapshots "before" would then capture the previous test's
        values as the baseline. That is not hypothetical: it is how the
        first version of this file failed, with the product behaving
        correctly and the test measuring the wrong thing.

        One item played with every preset off is exactly the restore path,
        so this normalises through the code under test rather than around
        it -- if the restore is broken, these tests do not quietly start
        from a dirty player, they fail.
        """
        video = RealVideo(self.clip, self.client, item_id="quiesce")
        for key, off in self.PRESETS.items():
            setattr(self.player_module.settings, key, off)
        self.pm.play(video, is_initial_play=True)
        # A quiesce that never played would leave the player dirty and say
        # nothing; every baseline below would then be someone else's residue.
        self.assertIs(self.pm._video, video,
                      "the quiesce item did not play, so the player is not "
                      "in a known state")

    def _play(self, item_id="v", **settings):
        for key, value in settings.items():
            setattr(self.player_module.settings, key, value)
        video = RealVideo(self.clip, self.client, item_id=item_id)
        self.pm.play(video, is_initial_play=True)
        self.assertIs(self.pm._video, video,
                      "playback did not start, so nothing here is measuring "
                      "the picture settings")
        return video

    def _prop(self, name):
        return getattr(self.pm._player, name.replace("-", "_"))

    def test_a_deband_preset_reaches_mpv_and_reads_back(self):
        """Every parameter, read off the player. The four names differ from
        each other by a suffix, so a swapped one is easy to write, invisible
        in a diff, and silently caught-and-logged at the write."""
        self._play(deband="standard")
        self.assertTrue(self._prop("deband"))
        self.assertEqual(int(self._prop("deband-iterations")), 2)
        self.assertAlmostEqual(float(self._prop("deband-threshold")), 48, 3)
        # 14, not mpv's default 16: the radius falls as the iterations rise
        # (mpv's manual), and `standard` runs two passes.
        self.assertAlmostEqual(float(self._prop("deband-range")), 14, 3)
        self.assertAlmostEqual(float(self._prop("deband-grain")), 24, 3)

    def test_every_deband_preset_is_a_value_mpv_accepts(self):
        """The ranges are mpv's (`deband-grain` stops at 4096, `-range` at
        64), and a value outside one is refused at the write and swallowed.
        Walked across items, which is also how the settings are applied."""
        from jellyfin_mpv_shim.mpv_options import DEBAND_PRESETS

        for name in ("light", "standard", "strong"):
            with self.subTest(preset=name):
                self._play(item_id="d-" + name, deband=name)
                for prop, expected in DEBAND_PRESETS[name].items():
                    got = self._prop(prop)
                    if isinstance(expected, bool):
                        self.assertEqual(bool(got), expected, prop)
                    else:
                        self.assertAlmostEqual(float(got), float(expected),
                                               3, prop)

    def test_off_hands_back_the_value_mpv_started_with(self):
        """The restore, against a real player rather than a recorded dict.
        Three items, because the bug shape this guards is state feeding back
        into its own input -- a restore that saved OUR value would put the
        preset back for ever, and one on/off pair cannot tell that apart."""
        # From the snapshot the player took off a FRESH mpv, not off the
        # live player. Reading it live looks equivalent and is not: method
        # order is alphabetical, so a test that leaves `strong` applied runs
        # earlier, `_quiesce` restores through the very path under test, and
        # a BROKEN restore would leave those values in place for this
        # baseline to adopt -- after which writing `strong` and reading it
        # back passes. Deleting the restore loop entirely survived the
        # earlier version of this assertion.
        before = {p: self.pm._render_pristine[p] for p in
                  ("deband", "deband-iterations", "deband-threshold",
                   "deband-range", "deband-grain")}
        self._play(item_id="on", deband="strong")
        self.assertTrue(self._prop("deband"))
        for n in range(3):
            self._play(item_id="off-%d" % n, deband="off")
            for prop, was in before.items():
                got = self._prop(prop)
                if isinstance(was, bool):
                    self.assertEqual(bool(got), was, prop)
                else:
                    self.assertAlmostEqual(float(got), float(was), 3, prop)

    def test_render_quality_high_reaches_mpv(self):
        """The copy of mpv's own `high-quality` profile. `scale` is the half
        doing the visible work, and it is a choice from a long list -- so a
        typo is refused at the write and logged at debug, which is exactly
        the shape this file exists for."""
        self._play(render_quality="high")
        self.assertEqual(self._prop("scale"), "ewa_lanczossharp")
        self.assertAlmostEqual(float(self._prop("scale-antiring")), 0.6, 3)

    def test_render_quality_default_restores_the_scaler(self):
        was = self._prop("scale")
        self._play(item_id="hq", render_quality="high")
        self.assertEqual(self._prop("scale"), "ewa_lanczossharp")
        self._play(item_id="back", render_quality="default")
        self.assertEqual(self._prop("scale"), was)

    def test_a_tone_mapping_curve_reaches_mpv(self):
        """mpv's own vocabulary, so the failure mode is a name it does not
        know -- refused at the write, swallowed, and the setting reads as
        applied."""
        self._play(tone_mapping="bt.2390")
        self.assertEqual(str(self._prop("tone-mapping")), "bt.2390")

    def test_the_buffer_preset_reaches_the_demuxer_options(self):
        """ByteSize options: mpv parses "400MiB" on a command line, but a
        property write wants the number, and the two backends coerce
        differently. Read back as an integer for that reason."""
        self._play(network_buffer="large")
        self.assertEqual(int(self._prop("demuxer-max-bytes")),
                         400 * 1024 * 1024)
        self.assertAlmostEqual(float(self._prop("demuxer-readahead-secs")),
                               20, 3)

    def test_interpolation_brings_video_sync_with_it(self):
        """mpv: `--interpolation` "requires setting the --video-sync option
        to one of the display- modes, or it will be silently disabled". The
        one failure a stand-in genuinely cannot show, because the stand-in
        has no such rule."""
        self._play(motion_interpolation="smooth")
        self.assertTrue(self._prop("interpolation"))
        self.assertTrue(str(self._prop("video-sync")).startswith("display-"))
        self.assertEqual(str(self._prop("tscale")), "oversample")

    def test_nothing_is_written_while_every_preset_is_off(self):
        """The property that makes "set it in mpv.conf and leave the setting
        off" a supported configuration. Read from a real mpv, whose values
        here are its own defaults plus whatever this machine's mpv.conf
        says -- which is precisely what must survive.
        """
        watched = ("deband", "tone-mapping", "scale", "video-sync",
                   "demuxer-readahead-secs")
        # The player's fresh-mpv snapshot, for the reason spelled out in
        # test_off_hands_back_the_value_mpv_started_with: a live read here
        # would adopt a broken restore's residue as the baseline.
        before = {p: self.pm._render_pristine[p] for p in watched}
        for n in range(3):
            self._play(item_id="quiet-%d" % n, **self.PRESETS)
        for prop, was in before.items():
            self.assertEqual(str(self._prop(prop)), str(was), prop)


if __name__ == "__main__":
    unittest.main()
