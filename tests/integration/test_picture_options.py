"""Deinterlacing and motion interpolation, through a real PlayerManager.

Named apart from the unit module `tests/test_picture_processing.py` on
purpose: `unittest discover` keys non-package test modules by basename, so
two files sharing one collide in sys.modules and the second fails to
import.

The pure resolution is `tests/test_picture_processing.py`; what needs a
player is *when* the values are written, which is per item -- like `hwdec`,
so that changing either setting takes effect on the next thing you play
rather than the next launch. Nothing here can be asserted from the option
table, because the table does not know how many times it is read.

Both writes sit inside a broad ``except Exception`` in ``_play_media``: a
picture preference must never stop playback. That is also what makes a
missing property on the fake dangerous rather than merely unhelpful -- it
raises, gets swallowed, and the path under test silently does not run. The
starting values are on ``FakeMPV`` for that reason, ``video_sync`` most of
all, since it is READ before it is first written.
"""

import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as h  # noqa: E402

player_module = h.import_player_with_fake_mpv()


def make_video():
    from tests.integration.test_player_state_machine import FakeVideo

    video = FakeVideo()
    video.item = {"MediaType": "Video"}
    video.get_proper_title = lambda: "A Title"
    video.get_current_streams = lambda: (None, None)
    return video


class _Base(unittest.TestCase):
    def setUp(self):
        self.pm = h.build_player(player_module)
        self.pm.action_trigger = threading.Event()
        self.pm.timeline_trigger = threading.Event()

    def play(self, url="http://example.invalid/s.mkv"):
        """One item, start to finish. The duration is answered on a timer,
        which is what "the file opened" means to `_play_media`.

        Answered REPEATEDLY until the call returns, rather than once: these
        tests play three items in a row, and a single shot only reliably
        satisfies the first wait. Getting that wrong does not fail the
        test -- every property under test is written before the wait -- it
        quietly turns "three items played" into "three starts timed out",
        which is a weaker claim wearing the same words.
        """
        video = make_video()
        done = threading.Event()

        def answer():
            while not done.wait(0.02):
                try:
                    self.pm._player.fire_property("duration", 100.0)
                except Exception:
                    return

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        self.addCleanup(done.set)
        try:
            with mock.patch.object(player_module.settings,
                                   "playback_timeout", 2):
                self.pm._play_media(video, url, is_initial_play=True)
        finally:
            done.set()
            thread.join(timeout=1)
        self.assertIs(self.pm._video, video,
                      "the start did not complete, so this test is "
                      "measuring a failed load")
        return video

    def setting(self, name, value):
        patcher = mock.patch.object(player_module.settings, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)


class DeinterlacePerItemTest(_Base):
    def test_the_setting_is_written_on_every_item(self):
        """Not only at construction. A change has to reach the next thing
        played, and an item that inherited the previous one's value is the
        bug this shape exists to prevent."""
        self.setting("deinterlace_auto", False)
        self.play()
        self.assertEqual(self.pm._player.deinterlace, "no")
        self.setting("deinterlace_auto", True)
        self.play()
        self.assertEqual(self.pm._player.deinterlace, "auto")

    def test_the_session_force_outlives_a_queue_advance(self):
        """Three items, because that is the claim: a season whose
        interlacing is not flagged is one answer, not one per episode. A
        one-item test passes on a force that is cleared by the next load."""
        self.setting("deinterlace_auto", False)
        self.pm.set_deinterlace(True)
        for episode in range(3):
            with self.subTest(episode=episode):
                self.play()
                self.assertEqual(self.pm._player.deinterlace, "yes")

    def test_returning_to_the_library_ends_it(self):
        self.setting("deinterlace_auto", False)
        self.pm.set_deinterlace(True)
        self.play()
        self.assertEqual(self.pm._player.deinterlace, "yes")
        self.pm.clear_deinterlace_override()
        self.play()
        self.assertEqual(self.pm._player.deinterlace, "no")

    def test_both_ways_out_of_playback_end_it(self):
        """Returning to the library is one. Closing the window is the
        other, and it does NOT go through the first: it reaches the player
        by way of `on_minimize` [iw]. An override set mid-episode and never
        returned from would otherwise outlive the session that set it --
        and since mpv only goes away when the window closes, that is the
        last moment anything is watching.

        Driven through the gateway rather than by calling the player
        method, because what is under test is that BOTH doors are wired to
        it; calling it directly asserts only that it works when called.
        """
        from jellyfin_mpv_shim.mpvtk_browser.gateway.playback import (
            PlaybackMixin)

        for door in ("on_browse_enter", "on_minimize"):
            with self.subTest(door=door):
                self.setting("deinterlace_auto", False)
                self.pm.set_deinterlace(True)
                self.play()
                self.assertEqual(self.pm._player.deinterlace, "yes")
                with mock.patch(
                        "jellyfin_mpv_shim.player.playerManager", self.pm):
                    getattr(PlaybackMixin(), door)()
                self.play()
                self.assertEqual(self.pm._player.deinterlace, "no",
                                 "%s left the force set" % door)

    def test_clearing_it_falls_back_to_the_setting_not_to_off(self):
        """The two are different when `deinterlace_auto` is on, and reading
        "no override" as "off" would turn the setting off for anyone who had
        ever touched the menu."""
        self.setting("deinterlace_auto", True)
        self.pm.set_deinterlace(True)
        self.pm.clear_deinterlace_override()
        self.play()
        self.assertEqual(self.pm._player.deinterlace, "auto")

    def test_the_row_reports_what_mpv_is_doing_not_the_mode(self):
        """Under `auto` the mode says only "decide per file". Reading it as
        the answer left the row unticked while mpv was deinterlacing a
        flagged file -- and since the row toggles against what it reports,
        pressing it then did nothing visible."""
        self.setting("deinterlace_auto", True)
        self.play()
        self.assertEqual(self.pm._player.deinterlace, "auto")
        self.pm._player.deinterlace_active = True      # a flagged file
        self.assertEqual(self.pm.deinterlace_state(), (True, True))
        self.pm._player.deinterlace_active = False     # a progressive one
        self.assertEqual(self.pm.deinterlace_state(), (False, True))

    def test_the_menu_can_hand_the_decision_back_to_the_setting(self):
        """Two presses, three states. A two-state toggle over a three-state
        value meant that with `deinterlace_auto` on, unticking left a hard
        "no" for the rest of the session -- so a later episode that IS
        flagged played un-deinterlaced [reviewer]. Driven through the
        gateway, because the cycle lives there."""
        from jellyfin_mpv_shim.mpvtk_browser.gateway.hud import HudMixin

        self.setting("deinterlace_auto", True)
        self.play()
        self.assertEqual(self.pm._player.deinterlace, "auto")
        gw = HudMixin()
        with mock.patch("jellyfin_mpv_shim.player.playerManager", self.pm):
            self.pm._player.deinterlace_active = True   # a flagged file
            gw.toggle_deinterlace()                     # -> force off
            self.assertEqual(self.pm._player.deinterlace, "no")
            gw.toggle_deinterlace()                     # -> back to auto
            self.assertEqual(self.pm._player.deinterlace, "auto")
        self.assertFalse(self.pm.deinterlace_forced())

    def test_the_force_can_turn_deinterlacing_off_as_well_as_on(self):
        """`False` was unreachable: the toggle mapped "off" onto "let the
        setting decide", so with `deinterlace_auto` on there was no way to
        stop mpv deinterlacing a file its flag gets wrong."""
        self.setting("deinterlace_auto", True)
        self.pm.set_deinterlace(False)
        self.play()
        self.assertEqual(self.pm._player.deinterlace, "no")
        self.pm.set_deinterlace(True)
        self.assertEqual(self.pm._player.deinterlace, "yes")

    def test_an_old_mpv_is_only_asked_about_auto_once(self):
        """The discovery is cached like `_lua_works`. Without it an old
        build re-attempts and re-logs on every item played, forever."""
        self.setting("deinterlace_auto", True)
        attempts = []
        real = type(self.pm._player).__setattr__

        def count_auto(obj, name, value):
            if name == "deinterlace" and value == "auto":
                attempts.append(value)
                raise ValueError("option not found")
            real(obj, name, value)

        with mock.patch.object(type(self.pm._player), "__setattr__",
                               count_auto):
            for _ in range(3):
                self.play()
        self.assertEqual(len(attempts), 1, attempts)
        self.assertEqual(self.pm._player.deinterlace, "no")

    def test_the_toggle_applies_without_waiting_for_the_next_item(self):
        """It is offered while you are watching the thing it is for."""
        self.setting("deinterlace_auto", False)
        self.play()
        self.pm.set_deinterlace(True)
        self.assertEqual(self.pm._player.deinterlace, "yes")

    def test_an_mpv_without_auto_gets_no_rather_than_yes(self):
        """mpv < 0.38 refuses `auto`. Falling back to `yes` would turn a
        setting the user cannot have into a *worse* one they did not ask
        for -- deinterlacing every progressive file in the library."""
        self.setting("deinterlace_auto", True)
        real = type(self.pm._player).__setattr__

        def refuse_auto(obj, name, value):
            if name == "deinterlace" and value == "auto":
                raise ValueError("option not found")
            real(obj, name, value)

        with mock.patch.object(type(self.pm._player), "__setattr__",
                               refuse_auto):
            self.play()
        self.assertEqual(self.pm._player.deinterlace, "no")

    def test_a_refusal_does_not_stop_playback(self):
        video = None
        real = type(self.pm._player).__setattr__

        def refuse(obj, name, value):
            if name == "deinterlace":
                raise ValueError("nope")
            real(obj, name, value)

        with mock.patch.object(type(self.pm._player), "__setattr__", refuse):
            video = self.play()
        self.assertIs(self.pm._video, video,
                      "a picture preference stopped playback")


class InterpolationPerItemTest(_Base):
    def test_a_preset_writes_all_three_properties(self):
        self.setting("motion_interpolation", "smooth")
        self.play()
        p = self.pm._player
        self.assertTrue(p.interpolation)
        self.assertEqual(p.video_sync, "display-resample")
        self.assertEqual(p.tscale, "oversample")

    def test_off_leaves_a_users_own_mpv_conf_entirely_alone(self):
        """Every one of these is a property somebody may have set in their
        own mpv.conf, and OFF IS THE DEFAULT -- so an off that wrote its
        idea of "not interpolating" would reach out on the first item and
        turn off frame blending the user configured themselves.

        `interpolation` is the one that was wrong: it was written
        unconditionally while `video-sync` was carefully preserved, which
        left the worst pair -- display-resample's cost with the feature it
        exists for switched off, and no setting here that put it back.
        """
        self.pm._player.video_sync = "display-resample-vdrop"
        self.pm._player.interpolation = True
        self.pm._player.tscale = "catmull_rom"
        self.setting("motion_interpolation", "off")
        for _ in range(3):
            self.play()
        self.assertEqual(self.pm._player.video_sync, "display-resample-vdrop")
        self.assertTrue(self.pm._player.interpolation,
                        "we turned off interpolation we never turned on")
        self.assertEqual(self.pm._player.tscale, "catmull_rom")

    def test_off_still_undoes_what_we_did_write(self):
        """The other half, and why "off writes nothing" is not the whole
        rule: once a preset HAS been applied, off has to put back what was
        there rather than leaving ours behind."""
        self.pm._player.interpolation = False
        self.pm._player.video_sync = "audio"
        self.setting("motion_interpolation", "smooth")
        self.play()
        self.assertTrue(self.pm._player.interpolation)
        with mock.patch.object(player_module.settings,
                               "motion_interpolation", "off"):
            self.play()
        self.assertFalse(self.pm._player.interpolation)
        self.assertEqual(self.pm._player.video_sync, "audio")

    def test_every_property_a_preset_wrote_is_restored(self):
        """Not just `video-sync`. Somebody who used the high-quality preset
        and then switched off was left with its `tscale` for the life of
        the mpv -- inert while interpolation is off, and wrong the moment
        anything else turned it on."""
        self.pm._player.tscale = "oversample"
        self.pm._player.video_sync = "audio"
        self.setting("motion_interpolation", "hq")
        self.play()
        self.assertEqual(self.pm._player.tscale, "mitchell")
        with mock.patch.object(player_module.settings,
                               "motion_interpolation", "off"):
            self.play()
        self.assertEqual(self.pm._player.tscale, "oversample")

    def test_turning_it_off_restores_what_was_there_before_we_wrote(self):
        """Not mpv's default: the value this user had. Over three plays,
        because the bug shape is state feeding back into itself -- a restore
        that saved OUR value would put display-resample back for ever, and a
        single on/off pair cannot tell the two apart."""
        self.pm._player.video_sync = "display-tempo"
        self.setting("motion_interpolation", "hq")
        self.play()
        self.assertEqual(self.pm._player.video_sync, "display-resample")
        with mock.patch.object(player_module.settings,
                               "motion_interpolation", "off"):
            self.play()
            self.assertEqual(self.pm._player.video_sync, "display-tempo")
            self.play()
            self.assertEqual(self.pm._player.video_sync, "display-tempo")

    def test_the_saved_value_survives_several_items_of_playback(self):
        """It is saved once, on the first write. Re-reading it on every item
        would save our own display-resample from item two onwards and make
        the restore a no-op."""
        self.pm._player.video_sync = "audio"
        self.setting("motion_interpolation", "blend")
        for _ in range(3):
            self.play()
        self.assertEqual(self.pm._interp_saved["video-sync"], "audio")

    def test_switching_between_presets_moves_the_filter(self):
        self.setting("motion_interpolation", "smooth")
        self.play()
        self.assertEqual(self.pm._player.tscale, "oversample")
        with mock.patch.object(player_module.settings,
                               "motion_interpolation", "blend"):
            self.play()
        self.assertEqual(self.pm._player.tscale, "linear")


if __name__ == "__main__":
    unittest.main()
