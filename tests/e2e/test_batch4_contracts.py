"""Batch-4 claims that only a real server can confirm (docs/UI_FIXES_4.md).

Every one of these was measured by hand during the work and then written
into code or into a comment. That is exactly the class of belief that rots:
it is true of the server that was running that afternoon, and nothing in the
fast suite would notice it changing.

Each test names the claim it is defending and where that claim is relied on.
"""

import os
import sys
import unittest
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402


@_e2e.require_server
class FieldsMustBeAskedForTest(unittest.TestCase):
    """#4 and #14: three DTO fields the shim now depends on, none of which
    a list query returns unless it is named."""

    def setUp(self):
        self.session = _e2e.Session()
        self.addCleanup(self.session.stop)

    def _items(self, fields=None, **kw):
        params = dict(include_item_types="Movie", recursive=True, limit=3, **kw)
        if fields is not None:
            params["fields"] = fields
        return (self.session.api.get_user_items(**params) or {}).get(
            "Items", [])

    def test_can_delete_is_absent_until_asked_for(self):
        """The premise of `ItemActions.can_delete` treating absent as "no",
        and of putting CanDelete into all three field sets. If the server
        ever starts returning it unconditionally this test is the place
        that finds out."""
        plain = self._items()
        self.assertTrue(plain, "no movies in the library")
        for item in plain:
            self.assertIsNone(item.get("CanDelete"),
                              "CanDelete arrived without being asked for")
        asked = self._items(fields="CanDelete")
        for item in asked:
            self.assertIsNotNone(item.get("CanDelete"),
                                 "CanDelete was asked for and did not come")

    def test_the_grid_field_set_carries_what_the_tiles_draw(self):
        """#14. Search used to pass no fields at all, so every one of these
        was missing on every search result."""
        from jellyfin_mpv_shim.mpvtk_browser.repository import GRID_FIELDS

        items = self._items(fields=GRID_FIELDS)
        self.assertTrue(items)
        self.assertTrue(
            any(i.get("PrimaryImageAspectRatio") is not None for i in items),
            "no result carried an aspect ratio, so tiles cannot be shaped")
        for item in items:
            self.assertIsNotNone(item.get("CanDelete"))

    def test_search_itself_asks_for_them(self):
        """The fix, through the real LibrarySource rather than the fake."""
        source = self.session.library_source()
        self.addCleanup(source.stop)
        results = source.search(_e2e.SOURCE_UUID, "a")
        self.assertTrue(results, "search found nothing to check")
        self.assertTrue(
            any(r.get("PrimaryImageAspectRatio") is not None
                for r in results),
            "search results still carry no aspect ratio")

    def test_media_source_count_is_omitted_at_one(self):
        """Not a bug, and the tile renderer's comment says so — but it is
        the reason a version chip is absent for most of a library, and if
        the server changed its mind every tile would grow one."""
        items = self._items(fields="MediaSourceCount")
        singles = [i for i in items if i.get("MediaSourceCount") in (None, 1)]
        self.assertTrue(singles, "no single-version movie to check")
        for item in singles:
            self.assertNotEqual(
                item.get("MediaSourceCount"), 1,
                "the server started sending MediaSourceCount=1; the tile "
                "renderer treats absent as 'one version'")


@_e2e.require_server
class TranscodeSignalsTest(unittest.TestCase):
    """#10: where the play method and its reasons actually come from.

    Both readings the design first proposed were wrong, and both would have
    failed silently — a panel saying "Transcoding" with no reason, or
    calling a remux a transcode.
    """

    def setUp(self):
        self.session = _e2e.Session()
        self.addCleanup(self.session.stop)
        self.item = self._a_movie()

    def _a_movie(self):
        items = (self.session.api.get_user_items(
            include_item_types="Movie", recursive=True, limit=1,
            fields="MediaSources") or {}).get("Items") or []
        if not items:
            self.skipTest("no movie with media sources")
        return items[0]

    def _source_for(self, profile):
        info = self.session.api.get_play_info(
            self.item["Id"], profile) or {}
        sources = info.get("MediaSources") or []
        self.assertTrue(sources, "PlaybackInfo returned no media source")
        return sources[0]

    @staticmethod
    def _profile(dp_container, dp_vcodec, tp_vcodec, tp_acodec):
        return {
            "MaxStreamingBitrate": 200000000,
            "DirectPlayProfiles": [{"Type": "Video",
                                    "Container": dp_container,
                                    "VideoCodec": dp_vcodec}],
            "TranscodingProfiles": [{"Type": "Video", "Container": "mkv",
                                     "Protocol": "http",
                                     "VideoCodec": tp_vcodec,
                                     "AudioCodec": tp_acodec,
                                     "Context": "Streaming"}],
            "CodecProfiles": [], "SubtitleProfiles": [],
        }

    def _codecs(self):
        streams = self.item["MediaSources"][0].get("MediaStreams") or []
        video = next((s for s in streams if s.get("Type") == "Video"), None)
        audio = next((s for s in streams if s.get("Type") == "Audio"), None)
        if not video:
            self.skipTest("the fixture movie has no video stream")
        return (self.item["MediaSources"][0].get("Container") or "mkv",
                video.get("Codec"), (audio or {}).get("Codec") or "aac")

    def test_transcode_reasons_are_not_a_media_source_field(self):
        """The trap: reading them off the DTO — which is what the schema
        suggests — yields None every time, and the panel then says the file
        is being transcoded for no reason at all."""
        container, vcodec, acodec = self._codecs()
        src = self._source_for(self._profile(container, "nosuchcodec",
                                             "h264", acodec))
        self.assertIsNone(src.get("TranscodeReasons"),
                          "TranscodeReasons appeared on the MediaSource; "
                          "media.py reads it out of the TranscodingUrl")

    def test_they_are_in_the_transcoding_url_query(self):
        container, vcodec, acodec = self._codecs()
        src = self._source_for(self._profile(container, "nosuchcodec",
                                             "h264", acodec))
        url = src.get("TranscodingUrl") or ""
        self.assertTrue(url, "the server did not choose to transcode")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertIn("TranscodeReasons", query)

    def test_a_remux_names_the_source_codec_not_copy(self):
        """The other wrong reading: a remuxing URL does not say
        `VideoCodec=copy`, it names the codec the file already has — so the
        test is a comparison, not a keyword."""
        container, vcodec, acodec = self._codecs()
        src = self._source_for(self._profile("definitelynotacontainer",
                                             vcodec, vcodec, acodec))
        url = src.get("TranscodingUrl") or ""
        self.assertTrue(url, "the server did not choose to transcode")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        target = (query.get("VideoCodec") or [""])[0]
        self.assertNotEqual(target, "copy")
        self.assertIn(vcodec, target.split(","))

    def test_the_derivation_agrees_with_the_server(self):
        """End to end: our own transcode_play_method against real URLs."""
        from jellyfin_mpv_shim import media

        container, vcodec, acodec = self._codecs()
        remux = self._source_for(self._profile("definitelynotacontainer",
                                               vcodec, vcodec, acodec))
        method, reasons = media.transcode_play_method(
            remux, remux.get("TranscodingUrl"))
        self.assertEqual(method, media.PLAY_REMUX)
        self.assertTrue(reasons, "a transcode with no reason at all")

        # A target that is genuinely NOT the source codec. Asking for h264
        # on an h264 file is a remux, which is what this assertion caught
        # the first time it was written -- the fixture's codec is not
        # something to assume.
        other = "hevc" if (vcodec or "").lower() != "hevc" else "h264"
        recode = self._source_for(self._profile(container, "nosuchcodec",
                                                other, acodec))
        method, _r = media.transcode_play_method(
            recode, recode.get("TranscodingUrl"))
        self.assertEqual(
            method, media.PLAY_TRANSCODE,
            "source %r, asked the server for %r" % (vcodec, other))


@_e2e.require_server
class PreviousEpisodeLookupTest(unittest.TestCase):
    """#6: the lookup that lets prev step past the start of a Next Up queue.

    Two server behaviours are load-bearing and neither is documented:
    `StartItemId` is inclusive (which is *why* the queue has nothing
    behind it), and `AdjacentTo` is not the neighbour query it looks like.
    """

    def setUp(self):
        self.session = _e2e.Session()
        self.addCleanup(self.session.stop)
        self.series = self._a_series()

    def _a_series(self):
        for candidate in (self.session.api.get_user_items(
                include_item_types="Series", recursive=True,
                limit=25) or {}).get("Items") or []:
            eps = (self.session.api.get_episodes(candidate["Id"]) or {}).get(
                "Items") or []
            if len(eps) >= 4:
                return candidate, eps
        self.skipTest("no series with four episodes")

    def test_start_item_id_is_inclusive(self):
        """The cause of #650: a Next Up start builds its queue this way, so
        the episode you started on is the first entry and there is nothing
        behind it."""
        _series, eps = self.series
        third = eps[2]
        queue = (self.session.api.get_episodes(
            _series["Id"], start_item_id=third["Id"]) or {}).get("Items") or []
        self.assertTrue(queue)
        self.assertEqual(queue[0]["Id"], third["Id"])

    def test_the_full_listing_is_what_the_widen_indexes_into(self):
        _series, eps = self.series
        ids = [e["Id"] for e in eps]
        self.assertEqual(len(set(ids)), len(ids), "duplicate episode ids")
        self.assertGreater(ids.index(eps[2]["Id"]), 0,
                           "the third episode is not after the first")


@_e2e.require_server
class PhotoOrientationTest(unittest.TestCase):
    """#8's one unanswered question: does the image endpoint leave the EXIF
    orientation tag on a picture it has already rotated?

    If it does, mpv rotates a second time and every such photo is wrong.
    Skips cleanly on a library with no photos rather than pretending.
    """

    def setUp(self):
        self.session = _e2e.Session()
        self.addCleanup(self.session.stop)

    def test_a_served_photo_does_not_carry_a_rotation_we_would_apply_twice(self):
        photos = (self.session.api.get_user_items(
            include_item_types="Photo", recursive=True, limit=10) or {}).get(
                "Items") or []
        if not photos:
            self.skipTest("no photos in the library")
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        import io

        checked = 0
        for photo in photos:
            url = self.session.api.artwork(photo["Id"], "Primary", 1920)
            try:
                # The image endpoint is AllowAnonymous, so no header is
                # needed -- and using urllib directly keeps this off the
                # apiclient, which has no "give me the bytes" call.
                with urllib.request.urlopen(url, timeout=20) as resp:
                    raw = resp.read()
            except Exception:
                continue
            try:
                with Image.open(io.BytesIO(raw)) as img:
                    tag = (img.getexif() or {}).get(274)
            except Exception:
                continue
            checked += 1
            self.assertIn(
                tag, (None, 0, 1),
                "the image endpoint returned a rotated picture that still "
                "declares orientation %r; mpv would rotate it again" % (tag,))
        if not checked:
            self.skipTest("no photo could be fetched and decoded")


if __name__ == "__main__":
    unittest.main()
