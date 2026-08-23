"""The picture and buffering settings, through a real PlayerManager.

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

    def user_mpv_conf(self, **props):
        """Say that these are the values the user's own ``mpv.conf`` gave
        this mpv, and that nothing has written to it since.

        Not the same as setting the attribute on the fake. mpv reads
        ``mpv.conf`` at construction, so "the user's value" is by definition
        what the player saw in ``_init_mpv`` -- which is when the pristine
        snapshot is taken, and deliberately so: the shader pack writes
        several of these properties earlier in the play path than the
        settings do, so a snapshot taken at first write would record the
        pack's debanding as the user's and never be able to hand the real
        value back.

        A test that merely assigned the attribute afterwards would be
        modelling something else entirely -- a property changed behind the
        player's back mid-session -- while claiming to model a config file.
        """
        for prop, value in props.items():
            setattr(self.pm._player, prop, value)
        self.pm._render_written = set()
        self.pm._snapshot_render_pristine()


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
        self.user_mpv_conf(video_sync="display-tempo")
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
        """It is read once, before anything has written. Re-reading it on
        every item would save our own display-resample from item two
        onwards and make the restore a no-op."""
        self.user_mpv_conf(video_sync="audio")
        self.setting("motion_interpolation", "blend")
        for _ in range(3):
            self.play()
        self.assertEqual(self.pm._render_pristine["video-sync"], "audio")

    def test_switching_between_presets_moves_the_filter(self):
        self.setting("motion_interpolation", "smooth")
        self.play()
        self.assertEqual(self.pm._player.tscale, "oversample")
        with mock.patch.object(player_module.settings,
                               "motion_interpolation", "blend"):
            self.play()
        self.assertEqual(self.pm._player.tscale, "linear")


class DebandPerItemTest(_Base):
    """Debanding, which shares interpolation's apply path but has the one
    thing interpolation does not: something else writing the same
    properties. Every shader profile pulls in the pack's ``deband-default``
    group, so "off" here has to mean two different things at once -- do not
    write, and do not *unwrite* what the pack put there."""

    PARAMS = ("deband_iterations", "deband_threshold", "deband_range",
              "deband_grain")

    def test_a_preset_writes_the_flag_and_all_four_parameters(self):
        """`deband` is a separate flag from its parameters, so a preset that
        wrote only the numbers would be tuning a filter that never runs."""
        self.setting("deband", "standard")
        self.play()
        p = self.pm._player
        self.assertIs(p.deband, True)
        self.assertEqual(p.deband_iterations, 2)
        self.assertEqual(p.deband_threshold, 48)
        self.assertEqual(p.deband_range, 14)
        self.assertEqual(p.deband_grain, 24)

    def test_the_setting_is_written_on_every_item(self):
        """Like hwdec: a change has to reach the next thing played rather
        than the next launch."""
        self.setting("deband", "light")
        self.play()
        self.pm._player.deband = False          # something else moved it
        self.play()
        self.assertIs(self.pm._player.deband, True)

    def test_off_leaves_a_users_own_mpv_conf_entirely_alone(self):
        """The documented way to use a combination the presets do not offer:
        set the deband options in mpv.conf and leave this at off. Three
        items, because the failure shape is state feeding back into itself
        and one play cannot see it."""
        self.user_mpv_conf(deband=True, deband_threshold=20.0,
                           deband_grain=0.0, deband_range=8.0,
                           deband_iterations=3)
        self.setting("deband", "off")
        for _ in range(3):
            self.play()
        p = self.pm._player
        self.assertIs(p.deband, True)
        self.assertEqual(p.deband_threshold, 20.0)
        self.assertEqual(p.deband_grain, 0.0)
        self.assertEqual(p.deband_iterations, 3)

    def test_off_restores_every_parameter_a_stronger_preset_wrote(self):
        """Not just the ones the preset in effect at the time named. Somebody
        who went strong -> light -> off must get all four back, which is what
        `preset_keys` covering the union of the table is for."""
        self.user_mpv_conf(deband=False, deband_threshold=48.0,
                           deband_grain=32.0, deband_range=16.0,
                           deband_iterations=1)
        self.setting("deband", "strong")
        self.play()
        self.assertEqual(self.pm._player.deband_iterations, 4)
        with mock.patch.object(player_module.settings, "deband", "light"):
            self.play()
        with mock.patch.object(player_module.settings, "deband", "off"):
            self.play()
            self.play()
        p = self.pm._player
        self.assertIs(p.deband, False)
        self.assertEqual(p.deband_iterations, 1)
        self.assertEqual(p.deband_threshold, 48.0)
        self.assertEqual(p.deband_range, 16.0)
        self.assertEqual(p.deband_grain, 32.0)

    def test_off_does_not_undo_the_shader_pack(self):
        """The second meaning of off, and the one a lazier design gets
        wrong. Every shader profile turns debanding on through
        `default-setting-groups`; with this setting off we have no opinion,
        so the pack's value has to stand. A restore keyed on "is the setting
        off" rather than on "did we ever write it" would switch the pack's
        debanding off on the next item played -- and the user would be
        looking at an upscaler profile that had quietly stopped debanding.
        """
        self.setting("deband", "off")
        self.play()
        self.pm._player.deband = True          # the pack, loading a profile
        self.pm._player.deband_grain = 0.0
        for _ in range(3):
            self.play()
        self.assertIs(self.pm._player.deband, True)
        self.assertEqual(self.pm._player.deband_grain, 0.0)

    def test_off_does_not_undo_the_pack_AFTER_we_have_written_it(self):
        """The other half of "off means no opinion", and the one the
        never-written case above does not reach.

        Turning the setting ON marks the key as ours. Turning it back OFF
        then restores the *pristine* value -- and with a profile still
        loaded, nothing puts the pack's back: `apply_for_item` early-returns
        for an unchanged profile ("Already wearing it"), by design, so the
        pack does not rewrite its settings between items. The upscaler stays
        selected, its shaders stay loaded, and its debanding is gone for the
        rest of the session.
        """
        self.user_mpv_conf(deband=False, deband_grain=32.0)
        self.setting("deband", "light")
        self.play()                                  # now ours to take back

        # The pack, loading a profile mid-session.
        self.pm._player.deband = True
        self.pm._player.deband_grain = 0.0
        self._pretend_profile_loaded({"deband": True, "deband_grain": 0.0})

        with mock.patch.object(player_module.settings, "deband", "off"):
            self.play()
            self.play()
        self.assertIs(self.pm._player.deband, True,
                      "the pack's debanding was undone by a setting that "
                      "says it has no opinion")
        self.assertEqual(self.pm._player.deband_grain, 0.0)

    def test_a_stronger_setting_is_never_weakened_by_the_pack(self):
        """The pack's `deband-default` is threshold 32, range 12, grain 0 --
        WEAKER than `strong` on threshold and grain. A user who picked
        `strong` must keep it, whatever the profile brings with it.

        Both orders, because the pack reaches mpv by two routes and only one
        of them is the play path: `apply_for_item` runs earlier in
        `_play_media` than the settings do, and a profile picked from the
        menu mid-film writes straight into the live player and then calls
        `reapply_render_presets`.
        """
        pack = {"deband": True, "deband_threshold": 32.0,
                "deband_range": 12.0, "deband_grain": 0.0}
        strong = {"deband_threshold": 64.0, "deband_range": 12.0,
                  "deband_grain": 32.0, "deband_iterations": 4}

        # 1. Pack first, then the play path applies the setting.
        self.setting("deband", "strong")
        for prop, value in pack.items():
            setattr(self.pm._player, prop, value)
        self.play()
        for prop, want in strong.items():
            self.assertEqual(float(getattr(self.pm._player, prop)), want,
                             "%s was left at the pack's value" % prop)

        # 2. A profile picked mid-film: the pack writes, then reasserts.
        for prop, value in pack.items():
            setattr(self.pm._player, prop, value)
        self.pm.reapply_render_presets()
        for prop, want in strong.items():
            self.assertEqual(float(getattr(self.pm._player, prop)), want,
                             "%s stayed weakened after a profile load" % prop)

    def _pretend_profile_loaded(self, applied):
        """Attach a profile manager reporting ``applied`` as the settings the
        loaded profile currently holds -- which is what the real one records
        while a profile is on."""
        manager = mock.Mock()
        manager.current_profile = "artcnn"
        manager.applied_settings = dict(applied)
        menu = getattr(self.pm, "menu", None) or mock.Mock()
        menu.profile_manager = manager
        self.pm.menu = menu
        self.addCleanup(setattr, self.pm, "menu", None)

    def test_the_setting_outranks_the_pack_when_it_is_not_off(self):
        """The other direction: the setting is the user's explicit answer
        and the pack's is a bundle that arrived with an upscaler, so while
        the setting says something it wins. This is what
        `reapply_render_presets` is called for after every profile load and
        unload -- without it the pack's write is the last one until the next
        item."""
        self.setting("deband", "light")
        self.play()
        self.pm._player.deband_grain = 0.0     # the pack's value
        self.pm.reapply_render_presets()
        self.assertEqual(self.pm._player.deband_grain, 16)

    def test_a_dead_player_is_not_reasserted_into(self):
        """`reapply_render_presets` is reachable from the shader menu, which
        is reachable while mpv is being torn down."""
        self.pm._mpv_alive = False
        self.setting("deband", "strong")
        self.pm.reapply_render_presets()
        self.assertIs(self.pm._player.deband, False)


class ReassertLockTest(_Base):
    """The reasserts take the player lock.

    Both arrive from a thread that is not the one applying settings for the
    next item -- the shader menu's ``put_task`` on the action thread, and
    ``kb_kill_shader`` straight out of mpv's key handler -- and every
    property they write is written by ``_play_media`` too. Without the lock
    the two loops interleave and the item wears half of each.

    Asserted as "does not write while the lock is held" rather than by
    reading the decorator: the decorator is the mechanism, and a reassert
    that reached mpv by some other route would pass a decorator check and
    fail the user.
    """

    def _blocked(self, call, prop, value):
        """Run ``call`` on its own thread with ``_lock`` held, and report
        whether it wrote ``prop`` before the lock was released."""
        setattr(self.pm._player, prop, value)
        released = threading.Event()
        wrote_early = []
        done = threading.Event()

        def run():
            try:
                call()
            finally:
                done.set()

        with self.pm._lock:
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            # Long enough that an unlocked call would have finished: the
            # write is a handful of attribute sets on the fake.
            done.wait(0.5)
            wrote_early.append(getattr(self.pm._player, prop) != value)
            released.set()
        self.assertTrue(done.wait(5), "the reassert never completed")
        thread.join(5)
        return wrote_early[0]

    def test_a_render_reassert_waits_for_the_player_lock(self):
        self.setting("deband", "strong")
        self.play()
        self.assertFalse(
            self._blocked(self.pm.reapply_render_presets,
                          "deband_grain", 0.0),
            "reapply_render_presets wrote while another thread held _lock")
        self.assertEqual(self.pm._player.deband_grain, 32,
                         "it never wrote at all -- the test proved nothing")

    def test_a_deinterlace_reassert_waits_for_the_player_lock(self):
        self.play()
        self.pm.set_deinterlace(True)
        self.assertFalse(
            self._blocked(self.pm.reapply_deinterlace, "deinterlace", "no"),
            "reapply_deinterlace wrote while another thread held _lock")
        self.assertNotEqual(self.pm._player.deinterlace, "no",
                            "it never wrote at all -- the test proved nothing")

    def test_the_play_path_can_still_reassert_re_entrantly(self):
        """``_play_media`` holds ``_lock`` and reaches ``apply_for_item``,
        which reasserts. An ordinary Lock here would deadlock the whole of
        playback; ``_lock`` is an RLock and this is what says so."""
        self.setting("deband", "strong")
        with self.pm._lock:
            self.pm.reapply_render_presets()      # must not hang
        self.assertEqual(self.pm._player.deband_grain, 32)


class BufferPerItemTest(_Base):
    def test_a_preset_writes_the_demuxer_options(self):
        self.setting("network_buffer", "large")
        self.play()
        self.assertEqual(self.pm._player.demuxer_readahead_secs, 20)
        self.assertEqual(self.pm._player.demuxer_max_bytes, 400 * 1024 * 1024)

    def test_off_leaves_a_users_own_buffering_alone(self):
        self.user_mpv_conf(demuxer_readahead_secs=120.0,
                           demuxer_max_bytes=2 * 1024 * 1024 * 1024,
                           demuxer_max_back_bytes=10 * 1024 * 1024)
        self.setting("network_buffer", "default")
        for _ in range(3):
            self.play()
        self.assertEqual(self.pm._player.demuxer_readahead_secs, 120.0)
        self.assertEqual(self.pm._player.demuxer_max_bytes,
                         2 * 1024 * 1024 * 1024)


class OldMpvTest(_Base):
    """A build without one of the properties a preset writes.

    `hdr-peak-percentile` and `hdr-contrast-recovery` are mpv 0.37+, so this
    is a real build difference. The whole preset must not be lost over it --
    `scale` is the part doing the visible work.
    """

    def test_a_missing_property_costs_only_that_property(self):
        del self.pm._player.hdr_contrast_recovery
        self.user_mpv_conf()               # re-probe: it is gone now
        self.setting("render_quality", "high")
        self.play()
        self.assertEqual(self.pm._player.scale, "ewa_lanczossharp")
        self.assertEqual(self.pm._player.scale_antiring, 0.6)
        self.assertFalse(hasattr(self.pm._player, "hdr_contrast_recovery"))

    def test_a_property_this_mpv_never_had_is_not_invented_on_restore(self):
        """The restore writes back what the snapshot holds, and a property
        that could not be read has no value to write. Inventing one would
        be this app deciding what an option it cannot read should be."""
        del self.pm._player.hdr_peak_percentile
        self.user_mpv_conf()
        self.setting("render_quality", "high")
        self.play()
        with mock.patch.object(player_module.settings,
                               "render_quality", "default"):
            self.play()
        self.assertEqual(self.pm._player.scale, "lanczos")
        self.assertFalse(hasattr(self.pm._player, "hdr_peak_percentile"))


if __name__ == "__main__":
    unittest.main()
