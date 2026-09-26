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

**The delete goes through `qa-admin`, and a refusal is loud.** `qa-user`
cannot delete what it just created: its policy has `EnableContentDeletion:
False`, so `DELETE /Items/<playlist>` answers 401. That was swallowed here,
so every run left its playlists behind and the server appended a "1" to the
next run's colliding name -- fifty-four had accumulated by 2026-09-18. The
cost was not disk: a playlist anybody can see makes Jellyfin materialise a
per-user `Playlists` **view**, which is why `test_account_policy` started
reporting that a restricted account "sees libraries they have no access to",
and why it passed on a fresh server and failed on the second run. Delete the
last playlist and that view disappears again, for every account. So a
cleanup that cannot work is worse than no cleanup, because it reads as one.
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

    @classmethod
    def tearDownClass(cls):
        """Prove the module left no litter, then close the admin session.

        The property, once, rather than the mechanics of each delete: a 2xx
        from `DELETE` is evidence and this is the check. It is here because
        the cost of getting it wrong is paid by *other* modules -- the
        per-user `Playlists` view only exists while some playlist does.

        Deliberately does **not** call `super()`: the base has no
        `tearDownClass` because the player is process-wide and outlives this
        class, and an unrevoked admin token leaves a session registered on
        the server for every run (`Session.stop` says why that matters).
        """
        admin = getattr(cls, "_admin", None)
        if admin is None:
            # Nothing was ever deleted through it, so nothing was created
            # either -- every creator registers its cleanup.
            return
        cls._admin = None
        try:
            left = cls._own_playlists(admin)
        finally:
            admin.stop()
        assert not left, (
            "this module left %d playlist(s) on the server: %s. While any "
            "playlist exists, Jellyfin materialises a per-user Playlists "
            "view for every account, and test_account_policy reads that as "
            "a restricted user seeing a library it may not."
            % (len(left), ", ".join(sorted(left))))

    @staticmethod
    def _own_playlists(admin):
        """Names of this module's playlists still on the server, as admin.

        An empty answer covers both clean states -- no litter, and no
        Playlists view at all because the last playlist went.
        """
        try:
            views = (admin._request(
                "/Users/%s/Views" % admin.user_id) or {}).get("Items", [])
            view = next((v for v in views
                         if v.get("CollectionType") == "playlists"), None)
            if view is None:
                return []
            items = (admin._request(
                "/Items?parentId=%s&userId=%s" % (view["Id"], admin.user_id))
                or {}).get("Items", [])
        except Exception:
            # Cannot tell is not the same as dirty, and failing the class on
            # a flaky read would be its own false alarm.
            return []
        return [i["Name"] for i in items
                if str(i.get("Name") or "").startswith("jms-e2e-")]

    @classmethod
    def _admin_session(cls):
        """A `qa-admin` session, made once per class and only when a cleanup
        actually needs it. `qa-admin` is the account that may delete (its
        `EnableContentDeletion` is True, measured); see the module docstring
        for why `self.session` is not."""
        admin = getattr(cls, "_admin", None)
        if admin is None:
            admin = cls._admin = _e2e.Session("qa-admin")
        return admin

    def _delete(self, item_id):
        """Remove a playlist, and **fail if the server refuses.**

        Loud on purpose. The litter this leaves does not stay inside this
        module -- it changes what *other* accounts see from the server, so a
        silent failure here is paid for by a different file's assertion. The
        message names the cause because the fix is not in this repository:
        it is either an admin credential this run can reach, or the account
        policy on the QA server.
        """
        if not item_id:
            return
        try:
            self._admin_session()._request("/Items/" + item_id,
                                           method="DELETE")
        except Exception as exc:
            raise AssertionError(
                "could not delete the playlist this test created (%s): %s. "
                "Left behind, it makes the server materialise a per-user "
                "Playlists view for every account, which is a different "
                "module's failure. Needs a qa-admin this run can log in as "
                "($JMS_E2E_ADMIN_PASSWORD or the published server file)."
                % (item_id, exc)) from exc

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
