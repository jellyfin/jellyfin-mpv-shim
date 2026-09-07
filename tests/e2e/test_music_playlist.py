"""A music playlist is audio, and only the server knows it.

The shim decides between "keep the library up with the now-playing bar" and
"hand the window to a video" from whether the thing being started is audio.
It decided with a hardcoded container list::

    audio = t in ("MusicAlbum", "MusicArtist", "MusicGenre")

A **playlist** is in no such list, and cannot be: the same `Type` carries
either answer. So a music playlist launched down the video branch, which
clears `_browsing`, yields the window and puts the renderer in HUD mode --
and `phud_hide`, which fires when the pointer leaves the window, calls
`ui_suspend`. The library is what got suspended, because the library is what
was on screen. Reported as "the entire library UI blanks", with a flash of
the HUD loading before the music started as the tell.

The item knew all along: `Playlist.MediaType` is `PlaylistMediaType`, which
`PlaylistManager` computes from the contents (`MediaType.Audio` or
`.Video`). **That is the fact this file exists to pin.** The unit tests
assert that a DTO shaped `{"Type": "Playlist", "MediaType": "Audio"}`
launches as audio; nothing in them proves a real Jellyfin playlist IS that
shape, and a fix resting on an invented DTO is the failure mode this suite
is for.

Both playlists are created here and deleted on the way out -- the same
create/restore shape `set_policy` uses for the Live TV policy tests.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402


@_e2e.require_server
class MusicPlaylistShapeTest(_e2e.E2ETestCase):

    @classmethod
    def _make(cls, session, name, media_type, ids):
        made = session._request("/Playlists", method="POST", body={
            "Name": name, "Ids": ids, "UserId": session.user_id,
            "MediaType": media_type})
        return (made or {}).get("Id")

    def setUp(self):
        super().setUp()
        songs = self._ids("Audio", 3)
        self.assertTrue(songs, "the QA library has no audio to build a "
                               "playlist from")
        self.music_id = self._make(self.session, "jms-e2e-music", "Audio",
                                   songs)
        self.addCleanup(self._delete, self.music_id)
        self.song_ids = songs

    def _ids(self, item_type, limit):
        found = self.session.find_all(item_type=item_type, Limit=limit)
        items = found if isinstance(found, list) else found.get("Items", [])
        return [i["Id"] for i in items]

    def _delete(self, item_id):
        if item_id:
            try:
                self.session._request("/Items/" + item_id, method="DELETE")
            except Exception:
                pass

    def test_a_music_playlist_says_it_is_audio(self):
        """The premise of the whole fix, measured rather than assumed."""
        dto = self.session.api.get_item(self.music_id)
        self.assertEqual("Playlist", dto.get("Type"))
        self.assertEqual(
            "Audio", dto.get("MediaType"),
            "a music playlist did not report MediaType Audio, so the launch "
            "rule has nothing to read and the type list was right after all")

    def test_the_launch_rule_agrees_with_the_server(self):
        from jellyfin_mpv_shim.utils import launches_as_audio

        dto = self.session.api.get_item(self.music_id)
        self.assertTrue(
            launches_as_audio(dto),
            "a real music playlist launches down the video branch, which "
            "yields the window and lets the auto-hide blank the library")

    def test_a_video_playlist_is_not_audio(self):
        """The control, and the reason this is not "playlists are audio":
        one `Type`, both answers, and only `MediaType` separates them."""
        from jellyfin_mpv_shim.utils import launches_as_audio

        videos = self._ids("Movie", 2)
        if not videos:
            self.skipTest("no video items to build a video playlist from")
        vid = self._make(self.session, "jms-e2e-video", "Video", videos)
        self.addCleanup(self._delete, vid)
        dto = self.session.api.get_item(vid)
        self.assertEqual("Video", dto.get("MediaType"))
        self.assertFalse(
            launches_as_audio(dto),
            "a video playlist would keep the library up and never hand the "
            "window over")

    def test_the_playlist_resolves_to_its_tracks(self):
        """The smoke half: the ids the launch would queue are the songs that
        went in, in order. A playlist that resolves to nothing takes the
        `_open_item` branch instead and never plays at all."""
        found = self.session.find_all(parent_id=self.music_id)
        items = found if isinstance(found, list) else found.get("Items", [])
        self.assertEqual(
            self.song_ids, [i["Id"] for i in items],
            "the playlist did not resolve to the tracks it was built from")
        for item in items:
            self.assertEqual("Audio", item.get("MediaType"))


if __name__ == "__main__":
    unittest.main()
