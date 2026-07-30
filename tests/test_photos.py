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
        self.apikeys = []

    def get_item(self, item_id):
        return dict(self._item, Id=item_id)

    # Both mirror the apiclient's own signature, ApiKey spelling and
    # include_apikey default. The default is the point: it is what made a
    # photo url carry a token nobody passed, so a fixture that ignored the
    # argument would have agreed the bug was fixed while it was not.
    def download_url(self, item_id, include_apikey=True):
        self.calls.append(("download", item_id))
        self.apikeys.append(include_apikey)
        return "http://srv/Items/%s/Download%s" % (
            item_id, "?ApiKey=k" if include_apikey else "")

    def artwork(self, item_id, art, max_width, ext="jpg", index=None,
                include_apikey=True):
        self.calls.append(("artwork", item_id, art, max_width))
        self.apikeys.append(include_apikey)
        return "http://srv/Items/%s/Images/%s?MaxWidth=%d%s" % (
            item_id, art, max_width, "&ApiKey=k" if include_apikey else "")

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


class PhotoUrlAuthTest(unittest.TestCase):
    """A photo obeys the auth header like every other playback url.

    Driven through ``get_playback_url`` rather than ``_get_url_from_source``
    on purpose: the photo branch returns *above* the one that drops the
    token, so the tests that exercise the source url walk straight past it.
    That is how both of these paths kept sending a token in the query
    string after the header was installed for them -- mpv held the header
    and the url held the token, and every assertion in the suite was about
    one or the other.
    """

    def _url(self, header, **item):
        item.setdefault("Type", PHOTO_TYPE)
        parent = _Parent(item)
        v = Video("ph1", parent)
        v.auth_via_header = header
        return v.get_playback_url(), parent.client.jellyfin

    def test_a_downloaded_photo_drops_the_token(self):
        url, jf = self._url(True, Container="jpg", Path="/pics/a.jpg")
        self.assertNotIn("ApiKey", url)
        self.assertNotIn("api_key", url)
        self.assertEqual(jf.apikeys, [False])

    def test_a_converted_photo_drops_it_too(self):
        url, jf = self._url(True, Container="heic", Path="/pics/a.HEIC")
        self.assertNotIn("ApiKey", url)
        self.assertNotIn("api_key", url)
        self.assertEqual(jf.apikeys, [False])

    def test_a_downloaded_photo_keeps_it_without_the_header(self):
        """The fallback has to still work: no header means the url is the
        only thing carrying credentials, and a photo that 401s is a black
        window."""
        url, jf = self._url(False, Container="jpg", Path="/pics/a.jpg")
        self.assertIn("ApiKey=", url)
        self.assertEqual(jf.apikeys, [True])

    def test_a_converted_photo_keeps_it_without_the_header(self):
        url, jf = self._url(False, Container="heic", Path="/pics/a.HEIC")
        self.assertIn("ApiKey=", url)
        self.assertEqual(jf.apikeys, [True])

    def test_the_default_is_still_to_send_one(self):
        """``auth_via_header`` defaults to False, so a caller that never
        went through ``play()`` is unchanged rather than unauthenticated."""
        parent = _Parent({"Type": PHOTO_TYPE, "Container": "jpg"})
        v = Video("ph1", parent)
        self.assertFalse(v.auth_via_header)
        self.assertIn("ApiKey=", v.get_playback_url())


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
        # A live mpv: the method declines outright on a dead handle, because
        # writing a property to a destroyed libmpv segfaults rather than
        # raising (see the guard in _apply_auth_headers).
        pm._mpv_alive = True
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

    def _with_subs(self, *paths):
        video = self._video()
        video.item = dict(video.item, MediaSources=[{"MediaStreams": [
            {"Type": "Subtitle", "Path": p} for p in paths]}])
        return video

    def test_a_foreign_subtitle_host_blocks_the_header_entirely(self):
        """http-header-fields is GLOBAL -- it applies to every request mpv
        makes, and an IsExternalUrl subtitle is handed to sub_add unchanged.
        Setting it would send our token to whoever hosts that subtitle, and
        mpv has no per-URL header option, so the only safe answer is to fall
        back to a token in the stream URL."""
        from jellyfin_mpv_shim.player import PlayerManager

        video = self._with_subs("https://opensubtitles.example/x.srt")
        self.assertFalse(
            PlayerManager._apply_auth_headers(self._pm(), video))

    def test_a_subtitle_on_our_own_server_does_not(self):
        from jellyfin_mpv_shim.player import PlayerManager

        video = self._with_subs("https://example.com/subs/2.srt")
        self.assertTrue(PlayerManager._apply_auth_headers(self._pm(), video))

    def test_a_local_subtitle_path_does_not(self):
        """Most external subtitles are files on the server's disk; only an
        absolute http(s) Path becomes a URL mpv fetches."""
        from jellyfin_mpv_shim.player import PlayerManager

        video = self._with_subs("/media/movie/movie.en.srt")
        self.assertTrue(PlayerManager._apply_auth_headers(self._pm(), video))

    def test_the_check_is_answerable_before_playbackinfo(self):
        """It has to be: the header decides whether the stream URL carries a
        token, so it is applied before the URL is built. media_source is
        still None at that point."""
        video = self._with_subs("https://elsewhere.example/x.srt")
        self.assertIsNone(video.media_source)
        self.assertEqual(video.foreign_subtitle_hosts(),
                         {"elsewhere.example"})

    def test_the_header_is_the_non_legacy_scheme(self):
        from jellyfin_mpv_shim.player import PlayerManager

        pm = self._pm()
        video = self._video()
        self.assertTrue(PlayerManager._apply_auth_headers(pm, video))
        sent = pm._player.http_header_fields
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("Authorization: MediaBrowser "))
        self.assertIn('Token="T0KEN"', sent[0])

    def test_a_refusal_leaves_mpv_holding_nothing(self):
        """The guard's whole purpose, and what it silently failed to do.

        ``http-header-fields`` is a global, persistent option and mpv is not
        re-created between queue items, so the assertion that matters is not
        "did we return False" but "what is mpv left holding". Play something
        normally, auto-advance to an item whose subtitle is on a third-party
        host, and the refusal used to log, fall back to a token in the URL
        — and let mpv send the *previous* item's Authorization to that host
        anyway. Nothing failed; the evidence was in someone else's log.
        """
        from jellyfin_mpv_shim.player import PlayerManager

        pm = self._pm()
        self.assertTrue(
            PlayerManager._apply_auth_headers(pm, self._video()))
        self.assertTrue(pm._player.http_header_fields, "no header to leak")
        video = self._with_subs("https://opensubtitles.example/x.srt")
        self.assertFalse(PlayerManager._apply_auth_headers(pm, video))
        self.assertEqual(pm._player.http_header_fields, [],
                         "mpv still holds the previous item's token")

    def test_a_dead_mpv_is_not_touched_at_all(self):
        """`play()` runs this BEFORE `_play_media`, and `_ensure_mpv` — the
        re-open after an idle-quit or a crash — is inside `_play_media`. So
        the handle here can be a destroyed one, and libmpv answers a
        property write on one of those with a segfault, not an exception:
        the try/except around it cannot help. The integration matrix's
        idle-quit-and-reopen test crashed the interpreter on exactly this.
        """
        from jellyfin_mpv_shim.player import PlayerManager

        pm = self._pm()
        pm._mpv_alive = False
        touched = []

        class _Dead:
            def __setattr__(self, name, value):
                touched.append(name)

        pm._player = _Dead()
        self.assertFalse(
            PlayerManager._apply_auth_headers(pm, self._video()),
            "claimed a header a dead mpv never took")
        self.assertEqual(touched, [], "wrote to a destroyed mpv handle")

    def test_every_refusal_path_clears_it(self):
        """Swept rather than spot-checked: the clear is at the top for
        exactly this reason, so a refusal added later cannot reintroduce
        the leak by forgetting to clear on its way out."""
        from jellyfin_mpv_shim.player import PlayerManager

        refusals = {
            "no client": lambda: type("V", (), {"client": None})(),
            "no token": lambda: self._video(token=""),
            "header raises": lambda: self._video(raises=True),
            "foreign subtitle host":
                lambda: self._with_subs("https://elsewhere.example/x.srt"),
        }
        for why, make in refusals.items():
            pm = self._pm()
            PlayerManager._apply_auth_headers(pm, self._video())
            self.assertFalse(
                PlayerManager._apply_auth_headers(pm, make()),
                "%s should have been refused" % why)
            self.assertEqual(pm._player.http_header_fields, [],
                             "a token survived the %r refusal" % why)

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


class PhotoReportingTest(unittest.TestCase):
    """A photo never went through PlaybackInfo, so it has no media source,
    no play session and nothing to report progress against.

    This crashed: stop() and play_next() both build timeline options, and
    both dereferenced video.playback_info["PlaySessionId"] three frames
    deep. Every caller already handles a None return -- the guard was
    simply missing.
    """

    def _pm(self, video):
        from jellyfin_mpv_shim.player import PlayerManager

        pm = PlayerManager.__new__(PlayerManager)
        pm._video = video
        return pm

    def _video(self, is_photo, playback_info):
        v = type("V", (), {})()
        v.is_photo = is_photo
        v.playback_info = playback_info
        v.item_id = "i1"
        return v

    def test_a_photo_reports_no_timeline(self):
        from jellyfin_mpv_shim.player import PlayerManager

        pm = self._pm(self._video(True, None))
        self.assertIsNone(
            PlayerManager.get_timeline_options(pm, video=pm._video))

    def test_anything_without_playback_info_reports_none_rather_than_raising(
            self):
        """The belt: whatever else reaches here without one is equally
        unreportable, and it used to be an AttributeError deep in stop()."""
        from jellyfin_mpv_shim.player import PlayerManager

        pm = self._pm(self._video(False, None))
        self.assertIsNone(
            PlayerManager.get_timeline_options(pm, video=pm._video))

    def test_no_video_is_still_none(self):
        from jellyfin_mpv_shim.player import PlayerManager

        pm = self._pm(None)
        self.assertIsNone(PlayerManager.get_timeline_options(pm))


class PhotoHudTest(unittest.TestCase):
    def _scene(self, is_photo):
        from tests._shell_harness import HudController
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from tests._shell_harness import build_scene as bs, ids as _ids

        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=HudController())
        b._browsing = False
        b.hud.shown = True
        b.hud.state = {"stopped": False, "is_audio": False,
                       "is_photo": is_photo, "title": "X",
                       "position": 1.0, "duration": 5.0, "paused": True}
        return _ids(bs(b, (1280, 720))[0])

    def test_a_photo_hides_the_ten_and_thirty_second_buttons(self):
        """mpv's duration for a still is --image-display-duration, i.e.
        when the NEXT photo arrives, so a 10s step either does nothing or
        skips the picture entirely."""
        video, photo = self._scene(False), self._scene(True)
        self.assertIn("hud-seek-back", video)
        self.assertIn("hud-seek-fwd", video)
        self.assertNotIn("hud-seek-back", photo)
        self.assertNotIn("hud-seek-fwd", photo)

    def test_but_keeps_the_ones_an_album_needs(self):
        """Pause is how you stop it moving on its own; prev/next is how you
        move through the album."""
        photo = self._scene(True)
        self.assertIn("hud-pp", photo)
        self.assertIn("hud-next", photo)
        self.assertIn("hud-prev", photo)
