"""The snapshot push_playstate hands the browser.

This is the seam between the player and the UI. The HUD tests inject a
playstate dict directly, so on their own they prove the *view* renders what
it is given and nothing about whether the player sends it — the exact shape
of "tested but never reaches the screen" this UI keeps producing. These
tests come at it from the other side.
"""

import sys
import unittest

sys.argv = ["test"]      # the app parses argv on first config-dir resolution

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
