"""The snapshot push_playstate hands the browser.

This is the seam between the player and the UI. The HUD tests inject a
playstate dict directly, so on their own they prove the *view* renders what
it is given and nothing about whether the player sends it — the exact shape
of "tested but never reaches the screen" this UI keeps producing. These
tests come at it from the other side.
"""

import sys
import unittest
from unittest import mock

sys.argv = ["test"]      # the app parses argv on first config-dir resolution

from jellyfin_mpv_shim.conf import settings  # noqa: E402
from jellyfin_mpv_shim.player import PlayerManager  # noqa: E402


class _Player:
    """Just enough mpv for push_playstate to read through."""
    playback_abort = False
    playback_time = 12.0
    duration = 100.0
    pause = False
    volume = 80
    mute = False
    fullscreen = False
    demuxer_cache_state = None


class _EmptyQueue:
    """update() drains its task queue first; there is nothing to drain."""

    @staticmethod
    def empty():
        return True


class _Video:
    item_id = "v1"

    def __init__(self, item):
        self.item = item

    def get_duration(self):
        return 100.0


def snapshot(item):
    """Run the real push_playstate over a fake player and return the dict."""
    got = []
    pm = PlayerManager.__new__(PlayerManager)
    pm.on_playstate = got.append
    pm._video = _Video(item)
    pm._player = _Player()
    pm._hud_skip = None
    pm.repeat_mode = "none"
    PlayerManager.push_playstate(pm)
    assert got, "push_playstate produced nothing"
    return got[0]


EPISODE = {
    "Name": "Pilot", "Type": "Episode", "MediaType": "Video",
    "SeriesName": "The Show", "ParentIndexNumber": 1, "IndexNumber": 2,
}
MOVIE = {"Name": "The Movie", "Type": "Movie", "MediaType": "Video"}
PHOTO = {"Name": "A Photo", "Type": "Photo", "MediaType": "Photo"}
SONG = {"Name": "A Song", "Type": "Audio", "MediaType": "Audio",
        "Artists": ["A Band"], "Album": "An Album"}


class TestEpisodeContext(unittest.TestCase):
    def test_an_episode_carries_its_series_and_numbering(self):
        st = snapshot(EPISODE)
        self.assertEqual(st["title"], "Pilot")
        self.assertEqual(st["series_name"], "The Show")
        self.assertEqual(st["season"], 1)
        self.assertEqual(st["episode"], 2)

    def test_a_movie_carries_no_context(self):
        st = snapshot(MOVIE)
        self.assertEqual(st["series_name"], "")
        self.assertIsNone(st["season"])
        self.assertIsNone(st["episode"])

    def test_missing_numbering_is_none_not_zero(self):
        """Zero is a real season — Specials. None means the server didn't
        say, and the HUD must not render "S0E0" for it."""
        item = dict(EPISODE)
        del item["ParentIndexNumber"]
        del item["IndexNumber"]
        st = snapshot(item)
        self.assertIsNone(st["season"])
        self.assertIsNone(st["episode"])
        self.assertEqual(st["series_name"], "The Show")

    def test_season_zero_survives_as_zero(self):
        item = dict(EPISODE, ParentIndexNumber=0)
        self.assertEqual(snapshot(item)["season"], 0)


class TestOnlyEpisodesGetEpisodeContext(unittest.TestCase):
    """ParentIndexNumber/IndexNumber are generic ordinals. A MusicVideo puts
    disc and track there and is MediaType Video, so it reaches the HUD —
    and would have been captioned "S1E3"."""

    def test_a_music_video_is_not_labelled_like_an_episode(self):
        from jellyfin_mpv_shim.mpvtk_browser.hud import _episode_context
        st = snapshot({"Name": "The Video", "Type": "MusicVideo",
                       "MediaType": "Video", "ParentIndexNumber": 1,
                       "IndexNumber": 3, "Album": "An Album"})
        self.assertIsNone(st["season"])
        self.assertIsNone(st["episode"])
        self.assertEqual(_episode_context(st), "")

    def test_a_plain_video_with_ordinals_is_not_labelled_either(self):
        from jellyfin_mpv_shim.mpvtk_browser.hud import _episode_context
        st = snapshot({"Name": "Clip", "Type": "Video", "MediaType": "Video",
                       "ParentIndexNumber": 2, "IndexNumber": 7})
        self.assertEqual(_episode_context(st), "")

    def test_a_real_episode_still_is(self):
        from jellyfin_mpv_shim.mpvtk_browser.hud import _episode_context
        self.assertEqual(_episode_context(snapshot(EPISODE)),
                         "The Show   ·   S1E2")


class TestTheAudioBarIsUnaffected(unittest.TestCase):
    """The now-playing bar shares this payload and shows artist/album under
    the title, so the new keys must not disturb what it reads."""

    def test_a_song_still_reports_title_artist_and_album(self):
        st = snapshot(SONG)
        self.assertEqual(st["title"], "A Song")
        self.assertEqual(st["artist"], "A Band")
        self.assertEqual(st["album"], "An Album")
        self.assertTrue(st["is_audio"])

    def test_a_song_has_empty_context_rather_than_missing_keys(self):
        st = snapshot(SONG)
        self.assertEqual(st["series_name"], "")


class TestTheHudRendersWhatThePlayerSends(unittest.TestCase):
    """Join the two halves: feed a real snapshot to the HUD's formatter."""

    def test_the_context_line_comes_out_of_a_real_snapshot(self):
        from jellyfin_mpv_shim.mpvtk_browser.hud import _episode_context
        self.assertEqual(_episode_context(snapshot(EPISODE)),
                         "The Show   ·   S1E2")

    def test_a_movie_snapshot_yields_no_context_line(self):
        from jellyfin_mpv_shim.mpvtk_browser.hud import _episode_context
        self.assertEqual(_episode_context(snapshot(MOVIE)), "")


if __name__ == "__main__":
    unittest.main()


class TestTheServerTheItemCameFrom(unittest.TestCase):
    """The headless cast screen fetches the playing item to show it, and
    the browser's *selected* server is not necessarily where that item
    lives. Guessing it fetches the wrong thing, or nothing, on a
    multi-server setup — so the payload carries the real one.

    Asserted on the PRODUCER. The consumer tests feed a payload in by hand,
    so deleting this key upstream leaves them all green while the cast
    screen quietly stops updating."""

    def _snapshot_with_client(self, client, clients):
        import jellyfin_mpv_shim.clients as clients_mod

        real = clients_mod.clientManager
        fake = type("CM", (), {"clients": clients})()
        clients_mod.clientManager = fake
        self.addCleanup(lambda: setattr(clients_mod, "clientManager", real))

        got = []
        pm = PlayerManager.__new__(PlayerManager)
        pm.on_playstate = got.append
        video = _Video(MOVIE)
        video.client = client
        pm._video = video
        pm._player = _Player()
        pm._hud_skip = None
        pm.repeat_mode = "none"
        PlayerManager.push_playstate(pm)
        return got[0]

    def test_it_reports_the_uuid_of_the_playing_item_s_server(self):
        client = object()
        state = self._snapshot_with_client(
            client, {"srv-a": object(), "srv-b": client})
        self.assertEqual(state["server_uuid"], "srv-b")

    def test_an_unknown_client_is_none_rather_than_a_wrong_guess(self):
        state = self._snapshot_with_client(object(), {"srv-a": object()})
        self.assertIsNone(state["server_uuid"])

    def test_the_key_is_always_present(self):
        """The consumer reads it with .get(), but a key that comes and goes
        is how "it works on my machine" starts."""
        state = self._snapshot_with_client(object(), {})
        self.assertIn("server_uuid", state)


class TestPhotoFlag(unittest.TestCase):
    """The HUD hides its scrubber for a still.

    mpv reports a duration for a photo -- --image-display-duration, i.e.
    when the next one arrives -- which is real but not seekable, and a
    progress bar crawling across a picture reads as a video about to end.
    """

    def test_a_photo_is_flagged(self):
        self.assertTrue(snapshot(PHOTO)["is_photo"])

    def test_a_video_is_not(self):
        self.assertFalse(snapshot(MOVIE)["is_photo"])

    def test_the_key_is_always_present(self):
        self.assertIn("is_photo", snapshot(SONG))


class TestSkipButtonIsIndependentOfSeekToSkip(unittest.TestCase):
    """The Skip Intro *button* and seek-to-skip are two different features
    that happen to call the same verb.

    ``skip_intro_on_seek`` is off by default: hijacking a seek the user
    asked for is opt-in. The button is not -- it is gated on
    ``skip_intro_enable`` (default on) and surfaces through this payload's
    ``skip_label``. Turning the seek behaviour off must not take the button
    with it, which is the regression this pins; nothing else asserts the
    two are separate.
    """

    def _label(self, seg_type):
        got = []
        pm = PlayerManager.__new__(PlayerManager)
        pm.on_playstate = got.append
        pm._video = _Video(EPISODE)
        pm._player = _Player()
        pm._hud_skip = type("Seg", (), {"type": seg_type})()
        pm.repeat_mode = "none"
        PlayerManager.push_playstate(pm)
        return got[0]["skip_label"]

    def test_the_two_labels_are_distinct(self):
        """The payload half: ``_hud_skip`` carries the live segment and its
        type picks the wording. Whether that segment gets SET is decided in
        ``update()`` — see TestTheButtonSurvivesSeekToSkipBeingOff, which is
        where the coupling this class used to claim actually lives.
        """
        self.assertEqual(self._label("Intro"), "Skip Intro")
        self.assertEqual(self._label("Outro"), "Skip Credits")

    def test_no_live_segment_means_no_button(self):
        self.assertIsNone(snapshot(EPISODE)["skip_label"])


class TestTheButtonSurvivesSeekToSkipBeingOff(unittest.TestCase):
    """Seek-to-skip is opt-in; the Skip button is not, and turning the first
    off must not take the second with it.

    Driven through the real ``update()``, because that is where ``_hud_skip``
    is decided. The previous version of this patched ``skip_intro_on_seek``
    around ``push_playstate``, which never reads it — the setting appears
    nowhere in ``player_reporting.py`` — so it asserted the labels and
    guaranteed nothing about the coupling its docstring named. Injecting the
    regression (making the button honour the seek setting) left the whole
    suite green.
    """

    class _Intro:
        type = "Intro"
        has_triggered = False

    def _pm(self):
        pm = PlayerManager.__new__(PlayerManager)
        pm.evt_queue = _EmptyQueue()
        pm._pump_trickplay = lambda: None
        pm.push_playstate = lambda: None
        pm._hud_skip = None
        pm.is_in_intro = False
        pm._last_intro_msg_time = 0
        pm.mpvtk_active = True
        pm._osc_style_resolved = "mpvtk"
        pm.syncplay = type("S", (), {"is_enabled": staticmethod(
            lambda: False)})()
        pm._player = _Player()
        pm._video = _Video(EPISODE)
        pm._video.get_current_intro = lambda _t: (False, self._Intro())
        # The tail of update() polls for a lost EOF; give it the state that
        # makes it a no-op, so this test is about the branch above it and
        # update() still runs to the end unguarded.
        pm.last_update = type("T", (), {"restart": staticmethod(
            lambda: None)})()
        pm.is_paused = lambda: False
        pm.should_send_timeline = False
        return pm

    def _hud_skip_after_update(self, **flags):
        pm = self._pm()
        patches = [mock.patch.object(settings, k, v)
                   for k, v in flags.items()]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        PlayerManager.update(pm)
        return pm._hud_skip

    def test_the_button_is_offered_with_seek_to_skip_turned_off(self):
        self.assertIsNotNone(
            self._hud_skip_after_update(skip_intro_on_seek=False,
                                        segment_intro="ask"),
            "turning seek-to-skip off took the button with it")

    def test_turning_the_button_itself_off_does_remove_it(self):
        """The other direction, or the test above passes on a button that
        can never be turned off at all."""
        self.assertIsNone(
            self._hud_skip_after_update(skip_intro_on_seek=True,
                                        segment_intro="off",
                                        segment_outro="off"))

    def test_always_skips_instead_of_offering(self):
        """The third state the booleans could only express as a pair."""
        self.assertIsNone(
            self._hud_skip_after_update(segment_intro="always"))


class TestReportingIsWiredIn(unittest.TestCase):
    """``push_playstate`` and the timeline reports live in ``ReportingMixin``.

    Everything above drives them through ``PlayerManager``, which is what
    makes it worth stating outright that the inheritance is what puts them
    there — a mixin that stopped being inherited would take session reporting
    and the now-playing bar with it, silently.
    """

    def test_the_player_inherits_it_and_the_methods_resolve_to_it(self):
        from jellyfin_mpv_shim.player_reporting import ReportingMixin

        self.assertTrue(issubclass(PlayerManager, ReportingMixin))
        for name in ("push_playstate", "get_timeline_options", "send_timeline",
                     "send_timeline_initial", "send_timeline_stopped",
                     "_report_stopped_offline", "upd_player_hide"):
            self.assertIs(getattr(PlayerManager, name),
                          getattr(ReportingMixin, name),
                          "%s is no longer the mixin's" % name)

    def test_the_timeline_methods_still_take_the_timeline_lock(self):
        """``_tl_lock``, not ``_lock``: server round trips happen under it and
        ``_lock`` is held for the whole of a playback start."""
        import inspect

        from jellyfin_mpv_shim.player_reporting import ReportingMixin

        for name in ("send_timeline", "send_timeline_stopped",
                     "_session_playing_safe"):
            src = inspect.getsource(getattr(ReportingMixin, name))
            self.assertIn('synchronous("_tl_lock")', src,
                          "%s lost its timeline lock" % name)

    def test_importing_it_pulls_in_neither_player_nor_a_backend(self):
        # Same guard as player_audio: the backend globals and the Discord
        # flag are read per call, and hoisting them to module scope breaks
        # the integration harness's fake-mpv swap.
        import subprocess

        code = ("import sys; sys.argv=[sys.argv[0]];"
                "import jellyfin_mpv_shim.player_reporting;"
                "bad=[m for m in ('jellyfin_mpv_shim.player','mpv',"
                "'python_mpv_jsonipc') if m in sys.modules];"
                "print(','.join(bad))")
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "",
                         "player_reporting captured the backend at import time")


class TestVolumeAndMuteReachTheUI(unittest.TestCase):
    """#618: the HUD's mute button took up to a second and a half to change.

    Its icon reads the playstate snapshot, and nothing pushed one when the
    volume or the mute flag moved -- the only thing that did was the
    browser's 1s ticker. Pause never had the problem because ``pause`` has
    been observed all along, which is why the two felt so different.
    """

    def test_both_properties_are_observed(self):
        import inspect

        src = inspect.getsource(PlayerManager._bind_mpv_handlers)
        for prop in ("mute", "volume"):
            self.assertIn('self._observe("%s"' % prop, src,
                          "%s is not observed, so the UI only learns about "
                          "it on the next tick" % prop)

    def test_the_handler_pushes_a_snapshot(self):
        got = []
        pm = PlayerManager.__new__(PlayerManager)
        pm.on_playstate = got.append
        pm._video = _Video({"Name": "x"})
        pm._player = _Player()
        pm._hud_skip = None
        pm.repeat_mode = "none"
        PlayerManager._on_volume_change(pm, "mute", True)
        self.assertTrue(got, "the observer pushed no playstate")
        self.assertIn("muted", got[0])

    def test_it_does_not_go_through_the_timeline_thread(self):
        """timeline_handle() would also POST progress to the server, which
        is not what a volume nudge is worth. Read the code, not the
        docstring -- which says the same thing and would satisfy a naive
        substring check on its own."""
        import inspect

        src = inspect.getsource(PlayerManager._on_volume_change)
        body = src.split('"""')[2]
        self.assertNotIn("timeline_handle", body)
        self.assertIn("push_playstate", body)


class TestChapterJump(unittest.TestCase):
    """Where a previous/next-chapter jump lands. One rule, two callers: the
    HUD's chapter buttons and the mouse's back/forward buttons (#614)."""

    CHAPTERS = [{"time": 0.0}, {"time": 40.0}, {"time": 80.0}]

    def target(self, pos, direction):
        from jellyfin_mpv_shim.player import chapter_target
        return chapter_target(self.CHAPTERS, pos, direction)

    def test_back_restarts_the_chapter_you_are_in(self):
        self.assertEqual(self.target(50.0, -1), 40.0)

    def test_back_within_the_grace_goes_to_the_one_before(self):
        """Every player does this, and it is what makes a double-press
        mean "no, the previous one"."""
        self.assertEqual(self.target(41.0, -1), 0.0)

    def test_back_from_the_first_chapter_goes_to_the_start(self):
        self.assertEqual(self.target(1.0, -1), 0.0)

    def test_forward_takes_the_next_boundary(self):
        self.assertEqual(self.target(50.0, 1), 80.0)

    def test_forward_from_the_last_chapter_has_nowhere_to_go(self):
        """None, not the end of the file: the caller does nothing rather
        than seeking to the credits."""
        self.assertIsNone(self.target(90.0, 1))

    def test_landing_on_a_boundary_does_not_seek_to_where_you_are(self):
        self.assertEqual(self.target(40.0, 1), 80.0)

    def test_there_is_no_dead_zone_before_a_boundary(self):
        """`> pos + 0.5` meant the last half second of every chapter was a
        stretch where the forward button did nothing at all -- half a second
        of real playback, and the reason it reads as "the button sometimes
        doesn't work". A position from mpv is a float and is never exactly a
        boundary, so `> pos` covers the equality case on its own."""
        self.assertEqual(self.target(39.6, 1), 40.0)
        self.assertEqual(self.target(39.999, 1), 40.0)

    def test_a_file_with_no_chapters_answers_nothing_useful(self):
        from jellyfin_mpv_shim.player import chapter_target
        self.assertIsNone(chapter_target([], 10.0, 1))
        self.assertEqual(chapter_target([], 10.0, -1), 0.0)


class TestMouseChapterNavIsOptional(unittest.TestCase):
    """#614: the thumb buttons are easy to hit by accident on some mice, so
    this is opt-in even though most players bind them."""

    def test_it_is_off_by_default(self):
        self.assertFalse(settings.mouse_chapter_nav)

    def test_the_binding_is_behind_the_setting(self):
        import inspect

        src = inspect.getsource(PlayerManager._bind_mpv_handlers)
        self.assertIn("settings.mouse_chapter_nav", src)
        self.assertIn("MBTN_BACK", src)
        self.assertIn("MBTN_FORWARD", src)


class TestNoPlayerControls(unittest.TestCase):
    """#615: "no controls" is a Player Controls Style, not a switch beside
    it. The old `enable_osc` only ever reached mpv's own OSC, so it did
    nothing under the default style and then silently blanked the controls
    if you later chose the mpv one."""

    def _pm(self, style):
        pm = PlayerManager.__new__(PlayerManager)
        pm._osc_style_resolved = style
        return pm

    def test_every_other_style_has_controls(self):
        for style in ("mpvtk", "mpv", "default"):
            self.assertTrue(self._pm(style).osc_enabled, style)

    def test_none_does_not(self):
        self.assertFalse(self._pm("none").osc_enabled)

    def test_the_hud_declines_to_engage(self):
        from jellyfin_mpv_shim.mpvtk_browser.gateway.hud import HudMixin
        import jellyfin_mpv_shim.player as player_mod

        gw = HudMixin()
        with mock.patch.object(player_mod, "playerManager",
                               self._pm("none")):
            self.assertFalse(gw.use_hud())
        with mock.patch.object(player_mod, "playerManager",
                               self._pm("mpvtk")):
            self.assertTrue(gw.use_hud())

    def test_the_skip_prompt_falls_back_to_the_osd(self):
        """The HUD's Skip button is the mpvtk surface for "ask" mode. With
        no HUD the OSD "Seek to Skip" prompt is the one left, and it is
        already what any non-mpvtk style gets -- so this is a check that
        the branch keys off the style rather than off having a browser."""
        import inspect

        src = inspect.getsource(PlayerManager.update)
        self.assertIn('== "mpvtk"', src)

    def test_the_setting_is_gone_rather_than_migrated(self):
        self.assertFalse(hasattr(settings, "enable_osc"))


class TestMediaSegmentTypes(unittest.TestCase):
    """#575: intros and credits were two pairs of booleans; the server
    publishes five kinds of segment and jellyfin-web offers three actions
    for each."""

    def test_every_type_has_a_setting_and_an_action(self):
        from jellyfin_mpv_shim import conf

        for seg_type, key in conf.SEGMENT_SETTINGS.items():
            with self.subTest(seg_type):
                self.assertTrue(hasattr(settings, key))
                self.assertIn(conf.segment_action(seg_type),
                              conf.SEGMENT_ACTIONS)

    def test_intros_and_credits_still_ask_by_default(self):
        self.assertEqual(settings.segment_intro, "ask")
        self.assertEqual(settings.segment_outro, "ask")

    def test_the_new_three_are_off_by_default(self):
        """A segment skipped out from under you is worse than one you have
        to skip yourself, and these are far less common."""
        for key in ("segment_commercial", "segment_preview",
                    "segment_recap"):
            self.assertEqual(getattr(settings, key), "off", key)

    def test_an_unknown_type_is_left_alone(self):
        """A newer server, or a hand-edited value. Skipping something we
        cannot name is the one unrecoverable answer."""
        from jellyfin_mpv_shim import conf

        self.assertEqual(conf.segment_action("Unknown"), "off")
        self.assertEqual(conf.segment_action("SomethingNew"), "off")

    def test_only_wanted_types_are_requested(self):
        from jellyfin_mpv_shim import conf

        with mock.patch.object(settings, "segment_intro", "ask"), \
                mock.patch.object(settings, "segment_outro", "off"), \
                mock.patch.object(settings, "segment_recap", "always"):
            self.assertEqual(sorted(conf.wanted_segment_types()),
                             ["Intro", "Recap"])
            self.assertTrue(conf.any_segment_wanted())

    def test_nothing_wanted_means_no_request_at_all(self):
        from jellyfin_mpv_shim import conf

        patches = [mock.patch.object(settings, k, "off")
                   for k in conf.SEGMENT_SETTINGS.values()]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.assertFalse(conf.any_segment_wanted())
        self.assertEqual(conf.wanted_segment_types(), [])

    def test_each_type_names_itself_in_its_labels(self):
        """"Skip Recap" is not "Skip Intro" -- and whole phrases, because a
        language that inflects the verb with its object cannot fix a
        sentence assembled after translation."""
        from jellyfin_mpv_shim.media import segment_labels

        seen = {t: segment_labels(t)[0]
                for t in ("Intro", "Outro", "Commercial", "Preview", "Recap")}
        self.assertEqual(len(set(seen.values())), 5, seen)
        # An unknown type still has something to put on the button.
        self.assertTrue(segment_labels("Whatever")[0])


class TestSegmentSettingsMigration(unittest.TestCase):
    """The four booleans carried a real choice, so unlike the other
    migrations this one is about preserving it rather than overriding it."""

    def _migrated(self, **old):
        from jellyfin_mpv_shim.conf import Settings

        cfg = Settings()
        cfg.config_version = 1
        cfg._migrate(old)
        return cfg

    def test_always_wins_over_ask(self):
        cfg = self._migrated(skip_intro_always=True, skip_intro_enable=True)
        self.assertEqual(cfg.segment_intro, "always")

    def test_ask_carries_over(self):
        cfg = self._migrated(skip_credits_always=False,
                             skip_credits_enable=True)
        self.assertEqual(cfg.segment_outro, "ask")

    def test_off_carries_over(self):
        cfg = self._migrated(skip_intro_always=False,
                             skip_intro_enable=False,
                             skip_credits_always=False,
                             skip_credits_enable=False)
        self.assertEqual(cfg.segment_intro, "off")
        self.assertEqual(cfg.segment_outro, "off")

    def test_a_config_without_the_old_keys_keeps_the_defaults(self):
        cfg = self._migrated()
        self.assertEqual(cfg.segment_intro, "ask")
        self.assertEqual(cfg.segment_outro, "ask")

    def test_a_quoted_boolean_is_read_the_way_the_schema_read_it(self):
        """The keys being migrated were declared `bool`, which in this
        schema means adv_bool -- and adv_bool says the STRINGS "false"/"no"/
        "0"/"off" are False while Python's bool() says all four are True.

        A hand-edited or templated conf.json that quotes its booleans would
        otherwise migrate "skip_intro_always": "false" to `always`: silently
        skipping intros for someone who had asked to be prompted, with the
        source keys dropped on the next save so the evidence goes too.
        """
        for false_ish in ("false", "no", "0", "off", "False", False, 0):
            with self.subTest(value=false_ish):
                cfg = self._migrated(skip_intro_always=false_ish,
                                     skip_intro_enable="true")
                self.assertEqual(cfg.segment_intro, "ask",
                                 "%r read as always" % (false_ish,))
                cfg = self._migrated(skip_intro_always=false_ish,
                                     skip_intro_enable=false_ish)
                self.assertEqual(cfg.segment_intro, "off",
                                 "%r read as ask" % (false_ish,))

    def test_a_quoted_true_still_migrates(self):
        """The other direction, or the check above is just a way of never
        migrating anything."""
        cfg = self._migrated(skip_intro_always="true")
        self.assertEqual(cfg.segment_intro, "always")
        cfg = self._migrated(skip_credits_enable="yes")
        self.assertEqual(cfg.segment_outro, "ask")

    def test_it_stamps_the_version_so_it_runs_once(self):
        from jellyfin_mpv_shim.conf import CONFIG_VERSION

        cfg = self._migrated(skip_intro_enable=False)
        self.assertEqual(cfg.config_version, CONFIG_VERSION)
