"""Music, played for real — the vertical the suite never had.

Every other content type in this library is played end to end somewhere in
`tests/e2e/`: video, audiobooks, books, photos, comics, Live TV. **Audio was
not**, in any of them. Music reached the contract tier only — `test_route_walk`
renders an album page and `test_source_conformance` checks the DTOs — so
nothing had ever asked the real player to play a real track.

That is a gap with a specific shape, because music is the one type that plays
WITHOUT taking the library off screen:

    A video picture is what takes the library away; music does not. Audio
    keeps `_video` set and keeps the browser up -- that is what the
    now-playing bar is for.

`tests/test_library_showing.py` pins that as a truth table, and it has to
build the item itself. This is the half a unit test structurally cannot make:
whether the **server's own** Audio DTO still satisfies `_item_is_audio`. That
predicate reads two fields and accepts either spelling — if Jellyfin ever
stopped sending both, every one of those unit rows would keep passing and the
library would go dark behind the now-playing bar for real.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

SERVER_HOST = (_e2e.SERVER.split("//")[-1].split("/")[0]
               if _e2e.SERVER else "")


@_e2e.require_server_and_mpv
class MusicPlaybackTest(_e2e.E2ETestCase):

    def setUp(self):
        super().setUp()
        # An album with at least two tracks, so the advance below is an
        # advance and not a restart. Chosen by content rather than by name:
        # which albums exist is a property of the fixture library, and
        # naming one here would make this test a test of that name.
        self.tracks = None
        for album in self.session.find_all(library="Music",
                                           item_type="MusicAlbum"):
            tracks = self.session.find_all(item_type="Audio",
                                           parent_id=album["Id"])
            if len(tracks) >= 2:
                self.album, self.tracks = album, tracks
                break
        if not self.tracks:
            self.skipTest("no album with two tracks to play")
        ids = [t["Id"] for t in self.tracks[:2]]
        self.session.reset_played(*ids)
        self.addCleanup(self.session.reset_played, *ids)

    def test_a_track_plays_and_the_server_sees_it(self):
        first = self.tracks[0]
        media = _e2e.build_media(self.session, [first["Id"]])
        video = media.video
        self.assertIsNotNone(video, "Media built nothing for an audio track")

        url = video.get_playback_url()
        self.assertIn(SERVER_HOST, url,
                      "playback URL does not point at the server under test")

        self.pm.play(video, is_initial_play=True)
        self.assertIs(self.pm._video, video)
        self.assertTrue(
            self.pm._player.duration and self.pm._player.duration > 0,
            "real mpv never reported a duration for the audio stream")

        self.pm.send_timeline()
        playing = _e2e.wait_for(
            lambda: (self.session.my_session() or {}).get("NowPlayingItem"))
        self.assertTrue(playing, "the server never saw this device playing")
        self.assertEqual(playing["Id"], first["Id"],
                         "the server thinks we are playing something else")

    def test_the_library_stays_up_while_music_plays(self):
        """The seam, against the server's own DTO.

        `_library_showing()` is what four call sites ask before handing BACK,
        a nav command or the browse-fullscreen state to the library, and the
        answer for audio must be yes *while something is playing*. The unit
        table builds its own item; this asks the same question about the one
        Jellyfin actually sent.
        """
        first = self.tracks[0]
        media = _e2e.build_media(self.session, [first["Id"]])
        self.pm.play(media.video, is_initial_play=True)

        self.assertIsNotNone(
            self.pm._video,
            "audio must keep `_video` set -- if it stops, the now-playing "
            "bar has nothing to draw and this seam disappears")
        self.assertTrue(
            self.pm._current_is_audio(),
            "the server's Audio DTO no longer satisfies `_item_is_audio`: it "
            "carries neither MediaType nor Type == 'Audio'. Every row of "
            "tests/test_library_showing.py still passes, because they build "
            "the item themselves -- and the library is now dark behind the "
            "now-playing bar. Item: %r"
            % ({k: media.video.item.get(k) for k in ("MediaType", "Type")},))
        self.assertTrue(
            self.pm._library_showing(),
            "the library went away when music started, so BACK and the "
            "mouse's back button are dead for the whole of playback")

    def test_the_queue_advances_to_the_next_track(self):
        """Seeked to the end rather than waited out: a track is 20s, and the
        thing under test is the advance, not the decoder."""
        ids = [t["Id"] for t in self.tracks[:2]]
        media = _e2e.build_media(self.session, ids)
        self.pm.play(media.video, is_initial_play=True)

        duration = self.pm._player.duration
        self.assertTrue(duration and duration > 2,
                        "no usable duration to seek within (%r)" % duration)
        self.pm.seek(duration - 1.5)

        advanced = self.pump_until(
            lambda: self.pm._video is not None
            and self.pm._video.item_id == ids[1],
            timeout=45)
        self.assertTrue(advanced, "the queue did not advance to the next "
                                  "track on EOF")
        # ...and it is still audio, so the library is still up on the other
        # side of the advance. A queue that advances INTO a state where the
        # browser is gone would be the same bug one item later.
        self.assertTrue(
            self.pm._library_showing(),
            "the library went away when the next track started")


if __name__ == "__main__":
    unittest.main()
