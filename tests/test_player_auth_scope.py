"""The Jellyfin access token must not reach a host that is not the server.

``--http-header-fields`` is a **global** mpv option: it applies to every HTTP
request mpv makes. ``Video.foreign_subtitle_hosts`` already refuses the header
when a subtitle is hosted elsewhere -- but the *media* host was never checked,
and with ``direct_paths`` an HTTP ``.strm`` source (or an http path
substitution) is returned by ``_get_url_from_source`` unchanged. Playing one
sent the token to a third-party CDN.

The ordering is the whole difficulty: the header used to be decided in
``play()`` **before** ``get_playback_url()`` produced the URL whose host it
needs. See media.py's ``foreign_subtitle_hosts``, which records that inversion
as deliberate for the subtitle half.

``sync/manager.py`` has had the same-origin discipline all along, and its
comment predicted this exact defect: *"'true by construction today' is exactly
what stops being true when someone threads a server-supplied path through one
of these."*
"""

import sys
import unittest
from types import SimpleNamespace as NS
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

SERVER = "https://jellyfin.example.invalid"
TOKEN = "SYNTHETIC_TEST_TOKEN"


def _client(item):
    return NS(
        config=NS(data={"auth.server": SERVER, "auth.token": TOKEN,
                        "auth.server-id": "sid"}),
        http=NS(_get_authenication_header=lambda:
                'MediaBrowser Token="%s"' % TOKEN),
        jellyfin=NS(get_item=lambda _id, **kw: item),
    )


class _Parent:
    def __init__(self, item):
        self.client = _client(item)
        self.is_local = True
        self.item = item


def _video(source, streams=None):
    from jellyfin_mpv_shim.media import Video

    source = dict(source)
    source.setdefault("Id", "src")
    source.setdefault("MediaStreams", streams or [])
    item = {"Type": "Movie", "Name": "Thing", "MediaSources": [source]}
    parent = _Parent(item)
    v = Video("item1", parent)
    v.item = item
    v.media_source = source
    # The negotiation is not what is under test; the resolved URL is.
    v.get_playback_url = lambda: v._get_url_from_source()
    return v


class _FakePlayer:
    def __init__(self):
        self.http_header_fields = ["stale: from the previous item"]


def _pm():
    """A PlayerManager with only what play() touches, so the real ordering
    runs without opening an mpv window."""
    from jellyfin_mpv_shim.player import PlayerManager

    pm = PlayerManager.__new__(PlayerManager)
    pm._player = _FakePlayer()
    pm._mpv_alive = True
    pm.should_send_timeline = False
    pm.start_time = 0.0
    pm._load_cancelled = False
    pm._start_in_progress = False
    pm.played = []
    pm._play_media = lambda video, url, *a, **kw: pm.played.append(url)
    return pm


def _headers(pm):
    return " ".join(pm._player.http_header_fields or [])


class ForeignMediaHostTest(unittest.TestCase):

    FOREIGN = {"Protocol": "Http",
               "Path": "https://cdn.example.invalid/movie.mkv",
               "SupportsDirectPlay": True, "SupportsDirectStream": True}

    def test_a_foreign_stream_host_gets_no_token(self):
        v = _video(self.FOREIGN)
        pm = _pm()
        with mock.patch("jellyfin_mpv_shim.media.settings.direct_paths", True):
            pm.play(v)
        self.assertTrue(pm.played and
                        pm.played[0].startswith("https://cdn.example.invalid/"),
                        "the test did not actually reach the direct path")
        self.assertNotIn(TOKEN, _headers(pm),
                         "the access token was sent to a third-party host")
        self.assertFalse(v.auth_via_header)

    def test_the_stale_header_from_the_previous_item_is_cleared(self):
        """http-header-fields outlives the item it was set for, so declining
        to set one is not enough -- the last item's has to go."""
        v = _video(self.FOREIGN)
        pm = _pm()
        with mock.patch("jellyfin_mpv_shim.media.settings.direct_paths", True):
            pm.play(v)
        self.assertNotIn("stale", _headers(pm))

    def test_our_own_server_still_gets_the_token(self):
        """The other half. A fix that simply stopped sending the header would
        pass the test above and quietly put the token back in every URL."""
        v = _video({"SupportsDirectPlay": False, "SupportsDirectStream": True})
        pm = _pm()
        pm.play(v)
        self.assertTrue(pm.played[0].startswith(SERVER))
        self.assertIn(TOKEN, _headers(pm),
                      "the header stopped being used for our own server")
        self.assertTrue(v.auth_via_header)
        self.assertNotIn("ApiKey", pm.played[0],
                         "the url carries a token as well as the header")

    def test_a_direct_path_back_to_our_own_server_is_fine(self):
        v = _video({"Protocol": "Http",
                    "Path": SERVER + "/static/movie.mkv",
                    "SupportsDirectPlay": True, "SupportsDirectStream": True})
        pm = _pm()
        with mock.patch("jellyfin_mpv_shim.media.settings.direct_paths", True):
            pm.play(v)
        self.assertTrue(pm.played[0].startswith(SERVER))
        self.assertIn(TOKEN, _headers(pm))

    def test_a_foreign_subtitle_still_refuses_the_header(self):
        """The half that already worked, kept so the fix cannot lose it."""
        v = _video(
            {"SupportsDirectPlay": False, "SupportsDirectStream": True},
            streams=[{"Type": "Subtitle",
                      "Path": "https://subs.example.invalid/a.srt"}])
        pm = _pm()
        pm.play(v)
        self.assertNotIn(TOKEN, _headers(pm))
        self.assertIn("ApiKey", pm.played[0],
                      "with no header the url must carry the token itself")


if __name__ == "__main__":
    unittest.main()
