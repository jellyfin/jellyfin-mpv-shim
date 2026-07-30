"""Photos, which are videos that happen to be still.

mpv is already a photo viewer: it holds an image for
``--image-display-duration`` (5s by default) and moves on. So the feature is
not a viewer, it is four small decisions about everything *around* playback
-- how the file is fetched, that it starts paused, that the seek controls go
away, and that a folder of them behaves like an album.
"""

import sys
import unittest

sys.argv = ["test"]

from jellyfin_mpv_shim.media import (  # noqa: E402
    _PHOTO_MAX_WIDTH, _SERVER_CONVERTED_IMAGES, Video)
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.components.labels import (  # noqa: E402
    placeholder_glyph)
from jellyfin_mpv_shim.mpvtk_browser.repository import (  # noqa: E402
    FOLDER_TYPES, PHOTO_TYPE, PLAYABLE_TYPES)

from tests._shell_harness import (  # noqa: E402
    FakeController, FakeSource, _SyncPool, build_scene)


class _Jellyfin:
    def __init__(self, item):
        self._item = item
        self.calls = []

    def get_item(self, item_id):
        return dict(self._item, Id=item_id)

    def download_url(self, item_id):
        self.calls.append(("download", item_id))
        return "http://srv/Items/%s/Download?api_key=k" % item_id

    def artwork(self, item_id, art, max_width, ext="jpg", index=None):
        self.calls.append(("artwork", item_id, art, max_width))
        return "http://srv/Items/%s/Images/%s?MaxWidth=%d" % (
            item_id, art, max_width)

    def get_play_info(self, *a, **kw):        # must never be reached
        raise AssertionError("a photo asked PlaybackInfo for a media source")


class _Client:
    def __init__(self, item):
        self.jellyfin = _Jellyfin(item)


class _Parent:
    is_local = True

    def __init__(self, item):
        self.client = _Client(item)


class PhotoUrlTest(unittest.TestCase):
    """A photo does not go through PlaybackInfo at all.

    That endpoint answers about MediaSources and a Photo has none, so there
    is nothing to negotiate, no play-session id and nothing to transcode.
    Confirmed against a live server: web fetches the file itself.
    """

    def _video(self, **item):
        item.setdefault("Type", PHOTO_TYPE)
        parent = _Parent(item)
        v = Video("ph1", parent)
        return v, parent.client.jellyfin

    def test_a_photo_is_marked_as_one(self):
        v, _jf = self._video()
        self.assertTrue(v.is_photo)

    def test_an_ordinary_video_is_not(self):
        v, _jf = self._video(Type="Movie")
        self.assertFalse(v.is_photo)

    def test_a_jpeg_is_downloaded_whole(self):
        v, jf = self._video(Container="jpg", Path="/pics/a.jpg")
        url = v.get_playback_url()
        self.assertIn("/Download", url)
        self.assertEqual(jf.calls[-1][0], "download")

    def test_heic_goes_through_the_server_converter(self):
        """mpv's ffmpeg often cannot decode HEIC, and finding out at decode
        time gives a black window rather than a fallback."""
        v, jf = self._video(Container="heic", Path="/pics/a.HEIC")
        url = v.get_playback_url()
        self.assertIn("/Images/Primary", url)
        self.assertEqual(jf.calls[-1],
                         ("artwork", "ph1", "Primary", _PHOTO_MAX_WIDTH))

    def test_the_extension_is_enough_when_the_container_is_missing(self):
        v, jf = self._video(Path="/pics/IMG_0001.HEIC")
        self.assertIn("/Images/Primary", v.get_playback_url())

    def test_raw_formats_go_the_same_way(self):
        for ext in ("cr2", "nef", "dng"):
            with self.subTest(ext=ext):
                v, _jf = self._video(Container=ext, Path="/p/a." + ext)
                self.assertIn("/Images/Primary", v.get_playback_url())

    def test_heic_is_in_the_converted_set(self):
        self.assertIn("heic", _SERVER_CONVERTED_IMAGES)


class PhotoTypeSetsTest(unittest.TestCase):
    def test_a_photo_album_is_a_folder(self):
        """A Home Videos directory holding both clips and images comes back
        as PhotoAlbum with IsFolder true. Without this it dead-ended."""
        self.assertIn("PhotoAlbum", FOLDER_TYPES)

    def test_photos_are_not_in_playable_types(self):
        """That set also drives the tile context menu, where Download,
        Add to Playlist and Mark Watched are meaningless or broken for a
        still image. Photos are routed on their own instead."""
        self.assertNotIn(PHOTO_TYPE, PLAYABLE_TYPES)


class PhotoGlyphTest(unittest.TestCase):
    """A first initial is useless where the name does not distinguish
    things: a Home Videos library is folders named 2019, 2020, Holiday."""

    def test_a_folder_says_folder(self):
        self.assertEqual(placeholder_glyph({"Type": "Folder",
                                            "Name": "2019"}), "▸")

    def test_a_photo_album_says_album(self):
        self.assertEqual(placeholder_glyph({"Type": "PhotoAlbum",
                                            "Name": "2019"}), "▣")

    def test_an_ordinary_item_still_gets_its_initial(self):
        self.assertEqual(placeholder_glyph({"Type": "Movie",
                                            "Name": "Arrival"}), "A")

    def test_music_keeps_its_note(self):
        self.assertEqual(placeholder_glyph({"Type": "MusicAlbum",
                                            "Name": "Kid A"}), "♪")


class PhotoOpensTheAlbumTest(unittest.TestCase):
    """Clicking a photo plays it with the rest of the album queued, so
    next/prev walk the folder and unpausing is a slideshow."""

    def _browser(self, items):
        src = FakeSource()
        src.grid_items = items
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "al1",
                    "title": "Album"})
        build_scene(b)
        return b

    ALBUM = [
        {"Id": "p1", "Name": "One", "Type": PHOTO_TYPE},
        {"Id": "v1", "Name": "Clip", "Type": "Video"},
        {"Id": "p2", "Name": "Two", "Type": PHOTO_TYPE},
        {"Id": "p3", "Name": "Three", "Type": PHOTO_TYPE},
    ]

    def test_opening_a_photo_queues_the_album_from_that_photo(self):
        b = self._browser(self.ALBUM)
        b._open_item(self.ALBUM[2])
        ids, _srv, start = b.controller.played[-1]
        self.assertEqual(ids, ["p1", "p2", "p3"])
        self.assertEqual(start, 1, "the queue did not start at the click")

    def test_videos_in_the_album_are_not_queued_with_the_photos(self):
        """They play as videos when clicked; a slideshow that stopped on a
        clip and waited for it would not be one."""
        b = self._browser(self.ALBUM)
        b._open_item(self.ALBUM[0])
        ids, _srv, _start = b.controller.played[-1]
        self.assertNotIn("v1", ids)

    def test_a_photo_with_no_list_behind_it_still_opens(self):
        b = self._browser([])
        b._open_item({"Id": "solo", "Name": "S", "Type": PHOTO_TYPE})
        ids, _srv, start = b.controller.played[-1]
        self.assertEqual((ids, start), (["solo"], 0))

    def test_a_photo_does_not_open_a_detail_page(self):
        b = self._browser(self.ALBUM)
        b._open_item(self.ALBUM[0])
        self.assertNotEqual(b.route.get("kind"), "detail")

    def test_a_photo_album_drills_into_a_grid(self):
        b = self._browser(self.ALBUM)
        b._open_item({"Id": "al2", "Name": "2020", "Type": "PhotoAlbum"})
        self.assertEqual(b.route.get("kind"), "grid")
        self.assertEqual(b.route.get("parent_id"), "al2")


# NOT covered here: that _play_media pauses once a photo has loaded.
# Reaching it needs a load to complete, which the fake mpv does not drive on
# its own -- and a test that scrapes player.py for the word "is_photo"
# asserts nothing about behaviour while breaking on any refactor. It is four
# lines, immediately after the success point, and it is manually verified.


if __name__ == "__main__":
    unittest.main()


class AuthHeaderTest(unittest.TestCase):
    """The token travels in a header, not a query string.

    Only ``Authorization: MediaBrowser Token="…"`` is un-gated on the
    server; X-Emby-Token and api_key both sit behind
    EnableLegacyAuthorization, which is off from Jellyfin v12
    (AuthorizationContext.GetAuthorizationInfoFromDictionary).
    """

    def _pm(self, applied=True):
        from jellyfin_mpv_shim.player import PlayerManager

        pm = PlayerManager.__new__(PlayerManager)
        pm._player = type("P", (), {})()
        return pm

    def _video(self, token="T0KEN", raises=False):
        item = {"Type": "Movie", "Name": "M"}
        parent = _Parent(item)

        class _HTTP:
            def _get_authenication_header(self):
                if raises:
                    raise RuntimeError("nope")
                if not token:
                    return 'MediaBrowser Client="c"'
                return 'MediaBrowser Client="c", Token="%s"' % token

        parent.client.http = _HTTP()
        parent.client.config = type("C", (), {"data": {
            "auth.token": token or "",
            "auth.server": "https://example.com"}})()
        return Video("v1", parent)

    def test_the_header_is_the_non_legacy_scheme(self):
        from jellyfin_mpv_shim.player import PlayerManager

        pm = self._pm()
        video = self._video()
        self.assertTrue(PlayerManager._apply_auth_headers(pm, video))
        sent = pm._player.http_header_fields
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("Authorization: MediaBrowser "))
        self.assertIn('Token="T0KEN"', sent[0])

    def test_a_client_with_no_token_is_not_claimed_as_authenticated(self):
        """Claiming success would strip a token off a URL that needs one."""
        from jellyfin_mpv_shim.player import PlayerManager

        self.assertFalse(
            PlayerManager._apply_auth_headers(self._pm(), self._video(token="")))

    def test_a_broken_header_falls_back_rather_than_raising(self):
        """mpv has had http-header-fields for over a decade, so this should
        not happen -- but the cost of being wrong is that nothing plays."""
        from jellyfin_mpv_shim.player import PlayerManager

        self.assertFalse(PlayerManager._apply_auth_headers(
            self._pm(), self._video(raises=True)))

    def test_mpv_refusing_the_option_falls_back(self):
        from jellyfin_mpv_shim.player import PlayerManager

        pm = self._pm()

        class _P:
            def __setattr__(self, name, value):
                raise RuntimeError("no such option")

        pm._player = _P()
        self.assertFalse(PlayerManager._apply_auth_headers(pm, self._video()))

    def test_the_url_drops_the_token_once_the_header_is_set(self):
        video = self._video()
        video.media_source = {"SupportsDirectStream": True, "Id": "src1",
                              "Protocol": "Http", "SupportsDirectPlay": False}
        video.auth_via_header = True
        url = video._get_url_from_source()
        self.assertNotIn("ApiKey", url)
        self.assertNotIn("api_key", url)

    def test_and_keeps_it_when_the_header_could_not_be_set(self):
        video = self._video()
        video.media_source = {"SupportsDirectStream": True, "Id": "src1",
                              "Protocol": "Http", "SupportsDirectPlay": False}
        video.auth_via_header = False
        url = video._get_url_from_source()
        self.assertIn("ApiKey=", url)
        self.assertNotIn("api_key=", url)


class SubtitleSidecarAuthTest(unittest.TestCase):
    """The sidecar download is issued by us, so the token is a header --
    except when the sidecar is somewhere else entirely."""

    def _download(self, stream):
        from jellyfin_mpv_shim.sync.manager import SyncManager
        import jellyfin_mpv_shim.sync.manager as mod

        seen = []

        class _Resp:
            content = b"sub"

            def raise_for_status(self):
                pass

        def fake_get(url, **kw):
            seen.append((url, kw.get("headers")))
            return _Resp()

        class _HTTP:
            def _get_authenication_header(self):
                return 'MediaBrowser Client="c", Token="T0KEN"'

        client = type("C", (), {})()
        client.config = type("Cfg", (), {"data": {
            "auth.server": "https://example.com",
            "auth.token": "T0KEN"}})()
        client.http = _HTTP()
        client.jellyfin = type("J", (), {
            "subtitle_url": staticmethod(
                lambda *a, **kw: "https://example.com/built")})()

        real_get, mod.requests.get = mod.requests.get, fake_get
        real_mk, mod.os.makedirs = mod.os.makedirs, lambda *a, **kw: None
        real_open = mod.open if hasattr(mod, "open") else None
        try:
            import builtins
            import io
            real_builtin_open = builtins.open
            builtins.open = lambda *a, **kw: io.BytesIO()
            try:
                SyncManager._download_subs(
                    SyncManager.__new__(SyncManager), client, "i1",
                    {"Id": "s1", "MediaStreams": [stream]}, "/tmp/x")
            finally:
                builtins.open = real_builtin_open
        finally:
            mod.requests.get = real_get
            mod.os.makedirs = real_mk
            del real_open
        return seen

    OURS = {"Type": "Subtitle", "IsExternal": True, "Index": 2,
            "Codec": "srt", "DeliveryUrl": "/Videos/1/Subtitles/2/Stream.srt"}

    def test_our_own_server_gets_the_header_and_a_clean_url(self):
        seen = self._download(self.OURS)
        self.assertEqual(len(seen), 1)
        url, headers = seen[0]
        self.assertNotIn("ApiKey", url)
        self.assertNotIn("api_key", url)
        self.assertIn('Token="T0KEN"', headers["Authorization"])

    def test_a_foreign_host_gets_no_credentials_at_all(self):
        """IsExternalUrl does NOT mean third-party -- it means the stream's
        Path was already an absolute URI, so the server handed that over
        instead of proxying it (StreamInfo.cs:1264-1274). It is often the
        same host. So the test is the origin, not the flag."""
        stream = dict(self.OURS, IsExternalUrl=True,
                      DeliveryUrl="https://opensubtitles.example/x.srt")
        url, headers = self._download(stream)[0]
        self.assertEqual(url, "https://opensubtitles.example/x.srt")
        self.assertFalse(headers)

    def test_an_external_url_on_our_own_server_still_gets_the_header(self):
        """The case that made the old flag-based rule wrong: a plugin or a
        co-located path on the very server we are logged in to."""
        stream = dict(self.OURS, IsExternalUrl=True,
                      DeliveryUrl="https://example.com/plugin/subs/2.srt")
        url, headers = self._download(stream)[0]
        self.assertEqual(url, "https://example.com/plugin/subs/2.srt")
        self.assertIn('Token="T0KEN"', headers["Authorization"])

    def test_a_downgrade_to_http_is_not_the_same_origin(self):
        """Sending a bearer token over plain http hands it to anyone on the
        path, however familiar the hostname looks."""
        stream = dict(self.OURS, IsExternalUrl=True,
                      DeliveryUrl="http://example.com/subs/2.srt")
        _url, headers = self._download(stream)[0]
        self.assertFalse(headers)

    def test_a_different_port_is_not_the_same_origin(self):
        stream = dict(self.OURS, IsExternalUrl=True,
                      DeliveryUrl="https://example.com:8920/subs/2.srt")
        _url, headers = self._download(stream)[0]
        self.assertFalse(headers)


class ThumbnailAuthTest(unittest.TestCase):
    """Images are the highest-volume first-party traffic here, so a token in
    their query strings is a token in the access log of every one of them --
    and it means Jellyfin cannot sit behind a proxy that rejects
    unauthenticated requests, because every tile would 401.
    """

    def _store(self, origins):
        import tempfile
        from jellyfin_mpv_shim.mpvtk_browser.thumbnails import ThumbnailStore

        store = ThumbnailStore.__new__(ThumbnailStore)
        store._auth = {}
        store.set_auth(origins)
        del tempfile
        return store

    HDR = 'MediaBrowser Client="c", Token="T0KEN"'

    def test_our_own_server_gets_the_header(self):
        store = self._store({("https", "example.com", None): self.HDR})
        got = store._headers_for("https://example.com/Items/1/Images/Primary")
        self.assertEqual(got, {"Authorization": self.HDR})

    def test_another_host_gets_nothing(self):
        store = self._store({("https", "example.com", None): self.HDR})
        self.assertEqual(
            store._headers_for("https://elsewhere.example/Items/1/Images/X"),
            {})

    def test_a_downgrade_to_http_gets_nothing(self):
        store = self._store({("https", "example.com", None): self.HDR})
        self.assertEqual(
            store._headers_for("http://example.com/Items/1/Images/X"), {})

    def test_a_second_server_gets_its_own_token(self):
        """One store serves every connected server, and a token only ever
        goes to the server it came from."""
        other = 'MediaBrowser Client="c", Token="OTHER"'
        store = self._store({("https", "a.example", None): self.HDR,
                             ("https", "b.example", None): other})
        self.assertEqual(store._headers_for("https://b.example/i")[
            "Authorization"], other)

    def test_signing_out_revokes_the_token(self):
        """set_auth replaces wholesale rather than merging, so a server the
        user has left stops receiving its old token."""
        store = self._store({("https", "example.com", None): self.HDR})
        store.set_auth({})
        self.assertEqual(store._headers_for("https://example.com/i"), {})

    def test_a_junk_url_is_not_an_exception(self):
        store = self._store({("https", "example.com", None): self.HDR})
        self.assertEqual(store._headers_for("not a url"), {})

    def test_image_urls_no_longer_carry_the_token(self):
        from jellyfin_mpv_shim.mpvtk_browser.repository import LibrarySource

        seen = {}

        class _Api:
            def image_url(self, item_id, image_type="Primary", index=None,
                          tag=None, max_width=None, fill_width=None,
                          fill_height=None, quality=90, include_apikey=True):
                seen["include_apikey"] = include_apikey
                return "https://example.com/img"

        src = LibrarySource.__new__(LibrarySource)
        src._conns = {"srv": type("C", (), {"api": _Api()})()}
        src.image_url("srv", "i1", "Primary", "t", 150, 225, fill=True)
        self.assertIs(seen["include_apikey"], False)
        src.image_url("srv", "i1", "Primary", "t", 150)
        self.assertIs(seen["include_apikey"], False)
