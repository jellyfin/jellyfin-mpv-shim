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

    def test_the_button_is_offered_with_seek_to_skip_turned_off(self):
        with mock.patch.object(settings, "skip_intro_on_seek", False):
            self.assertEqual(self._label("Intro"), "Skip Intro")

    def test_credits_get_their_own_label(self):
        with mock.patch.object(settings, "skip_intro_on_seek", False):
            self.assertEqual(self._label("Outro"), "Skip Credits")

    def test_no_live_segment_means_no_button(self):
        self.assertIsNone(snapshot(EPISODE)["skip_label"])


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
