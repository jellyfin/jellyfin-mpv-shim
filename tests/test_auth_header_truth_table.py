"""Where the Jellyfin access token may go, enumerated.

`--http-header-fields` is a **global, persistent** mpv option: it applies to
every request mpv makes, and mpv is not re-created between queue items. So
"send the token as a header" is not a per-URL decision even though the thing
it protects (one item's stream, one item's sidecars) is per-item. Everything
awkward about this subsystem follows from that.

There are two decisions and they are made at different times:

  A. `_apply_auth_headers`, BEFORE the url is built -- the url has to know
     whether to carry a token itself.
  B. the origin check, AFTER it -- the media's host is not known until the
     negotiation has run.

Both can end with mpv not carrying the header, and the consequence is the same
either way: our own sidecar urls, which `map_streams` builds without a token
on the assumption the header will cover them, have no credential at all.
`test_player_auth_scope` covers decision B. This file covers the invariant
across both, which is the thing neither was stated as.

See docs/auth-headers.md.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import unittest
from types import SimpleNamespace as NS
from unittest import mock

sys.argv = [sys.argv[0]]

SERVER = "https://jellyfin.example.invalid"
TOKEN = "SYNTHETIC_TEST_TOKEN"
STALE = "stale: from the previous item"

#: Ours, delivered as a sidecar. `map_streams` builds its url from DeliveryUrl
#: and adds no token, because the header was going to carry it.
OUR_SIDECAR = {"Type": "Subtitle", "Index": 2, "IsExternal": True,
               "IsExternalUrl": False, "DeliveryMethod": "External",
               "DeliveryUrl": "/Videos/1/Subtitles/2/Stream.srt"}

#: Somebody else's. `foreign_subtitle_hosts` reads **Path**, not DeliveryUrl:
#: it has to answer before PlaybackInfo, and the server sets IsExternalUrl
#: exactly when Path is an absolute http(s) URI (StreamInfo.cs:1264-1274).
THEIR_SIDECAR = {"Type": "Subtitle", "Index": 3, "IsExternal": True,
                 "IsExternalUrl": True, "DeliveryMethod": "External",
                 "Path": "https://subs.example.invalid/b.srt",
                 "DeliveryUrl": "https://subs.example.invalid/b.srt"}

OURS = {"SupportsDirectPlay": False, "SupportsDirectStream": True}
THEIRS = {"Protocol": "Http", "Path": "https://cdn.example.invalid/movie.mkv",
          "SupportsDirectPlay": True, "SupportsDirectStream": True}


def _video(source, streams, header=None, client=True, token=TOKEN):
    from jellyfin_mpv_shim.media import Video

    source = dict(source)
    source.setdefault("Id", "src")
    source["MediaStreams"] = [dict(s) for s in streams]
    item = {"Type": "Movie", "Name": "Thing", "MediaSources": [source]}

    def build_header():
        if header is Exception:
            raise RuntimeError("no client yet")
        return header

    cl = NS(
        config=NS(data={"auth.server": SERVER, "auth.token": token,
                        "auth.server-id": "sid"}),
        http=NS(_get_authenication_header=build_header),
        jellyfin=NS(get_item=lambda _id, **kw: item),
    ) if client else None
    parent = NS(client=cl, is_local=True, item=item)
    v = Video.__new__(Video)
    v.item_id = "item1"
    v.parent = parent
    v.client = cl
    v.item = item
    v.media_source = source
    v.srcid = None
    v.aid = None
    v.sid = 2
    v.explicit_tracks = True        # the negotiation is not what is under test
    v.auth_via_header = False
    v.is_tv = False
    v.is_photo = False
    v.playback_info = None
    v.intros = []
    v.intro_tried = True
    v.trs_ovr = None
    v.is_transcode = False
    v.direct_path = False
    v.play_method = None
    v.transcode_reasons = []
    v.subtitle_seq = {}
    v.subtitle_uid = {}
    v.subtitle_url = {}
    v.subtitle_enc = set()
    v.audio_seq = {}
    v.audio_uid = {}
    v.get_playback_url = lambda: (v.map_streams(),
                                  v._get_url_from_source())[1]
    return v


class _FakePlayer:
    """Refuses to be given a non-empty ``http_header_fields``.

    An empty one is still accepted, because clearing is exactly what the
    refusal path must still manage -- the option is global and the previous
    item's token is in it.
    """

    def __init__(self, refuse=False):
        object.__setattr__(self, "_refuse", False)
        self.http_header_fields = [STALE]
        object.__setattr__(self, "_refuse", refuse)

    def __setattr__(self, name, value):
        if name == "http_header_fields" and self._refuse and value:
            raise RuntimeError("mpv would not take it")
        object.__setattr__(self, name, value)


def _play(source, streams, alive=True, header='MediaBrowser Token="%s"' % TOKEN,
          client=True, refuse=False, token=TOKEN):
    """Run the real ordering and report what the token reached.

    Returns ``(installed, mpv_headers, media_url, sidecar_urls)``.
    """
    from jellyfin_mpv_shim.player import PlayerManager

    v = _video(source, streams, header=header, client=client, token=token)
    pm = PlayerManager.__new__(PlayerManager)
    pm._player = _FakePlayer(refuse=refuse)
    pm._mpv_alive = alive
    pm.should_send_timeline = False
    pm.start_time = 0.0
    pm._load_cancelled = False
    pm._start_in_progress = False
    pm._track_memory = None
    pm.menu = None
    seen = {}
    pm._play_media = lambda video, url, *a, **k: seen.update(
        url=url, subs=dict(video.subtitle_url),
        headers=" ".join(pm._player.http_header_fields or []))
    with mock.patch("jellyfin_mpv_shim.media.settings.direct_paths", True):
        pm.play(v)
    return (v.auth_via_header, seen.get("headers", ""), seen.get("url", ""),
            seen.get("subs", {}))


class WhenTheHeaderIsInstalledTest(unittest.TestCase):
    """Decision A. Every answer but the first is "no", and every "no" must
    also clear whatever the previous item left in the option."""

    def test_the_ordinary_case_installs_it(self):
        installed, headers, url, _subs = _play(OURS, [])
        self.assertTrue(installed)
        self.assertIn(TOKEN, headers)
        self.assertNotIn(STALE, headers)
        self.assertNotIn("ApiKey", url,
                         "the url carried a token as well as the header")

    def test_a_foreign_subtitle_refuses_it(self):
        installed, headers, url, _subs = _play(OURS, [THEIR_SIDECAR])
        self.assertFalse(installed)
        self.assertNotIn(TOKEN, headers)
        self.assertIn("ApiKey", url)

    @staticmethod
    def _video(server, *sub_paths):
        from jellyfin_mpv_shim.media import Video

        video = Video.__new__(Video)
        video.client = type("c", (), {
            "config": type("cfg", (), {"data": server})()})()
        video.item = {"MediaSources": [{"MediaStreams": [
            {"Type": "Subtitle", "Path": p} for p in sub_paths]}]}
        return video

    def test_a_server_url_it_cannot_parse_refuses_it(self):
        """The one failure the caller's guard cannot supply.

        `foreign_subtitle_hosts` swallows a bad `auth.server` rather than
        raising, so an empty answer never reaches `_apply_auth_headers` as an
        error -- it reaches it as "nothing foreign", and the token goes to
        mpv. Whoever cannot tell whose host it is has to say "foreign".
        """
        class Exploding(dict):
            def get(self, key, default=None):
                if key == "auth.server":
                    raise RuntimeError("no server recorded")
                return dict.get(self, key, default)

        video = self._video(Exploding(), "https://jf.example/s.srt")
        self.assertEqual(video.foreign_subtitle_hosts(), {"unknown"},
                         "a server url it cannot parse answered 'nothing "
                         "foreign', which installs the header")

    def test_a_malformed_port_refuses_it_the_same_way(self):
        """`urlparse` is lazy: this one raises at `.port`, not at the parse.
        Two failure sites, one answer -- they were on opposite sides of the
        try, and only one of them failed closed."""
        video = self._video({"auth.server": "http://box:notaport"},
                            "http://box/s.srt")
        self.assertEqual(video.foreign_subtitle_hosts(), {"box"})

    def test_our_own_sidecar_may_spell_out_the_default_port(self):
        """`https://h` and `https://h:443` are one origin everywhere else.
        Compared as raw ports they were two, so our own sidecar was called
        foreign -- and then got neither the header nor a token in its url."""
        for server, path in (('https://jf.example', 'https://jf.example:443/s.srt'),
                             ('https://jf.example:443', 'https://jf.example/s.srt'),
                             ('http://box', 'http://box:80/s.srt')):
            with self.subTest(server=server, path=path):
                self.assertEqual(self._video({"auth.server": server},
                                             path).foreign_subtitle_hosts(),
                                 set())

    def test_the_two_origin_tests_never_disagree(self):
        """`foreign_subtitle_hosts` kept its own copy of the tuple
        comparison while `docs/auth-headers.md` said there was one
        implementation. The copy is the thing this pins."""
        from jellyfin_mpv_shim.utils import same_origin

        server = "https://jf.example"
        paths = ["https://jf.example/s.srt", "https://jf.example:443/s.srt",
                 "https://jf.example:8920/s.srt", "http://jf.example/s.srt",
                 "https://other.example/s.srt", "https://other.example:443/s.srt"]
        for path in paths:
            with self.subTest(path):
                foreign = self._video({"auth.server": server},
                                      path).foreign_subtitle_hosts()
                self.assertEqual(bool(foreign), not same_origin(path, server),
                                 "the subtitle check and `same_origin` gave "
                                 "different answers for %s" % path)

    def test_a_dead_mpv_refuses_it(self):
        installed, _h, url, _s = _play(OURS, [], alive=False)
        self.assertFalse(installed)
        self.assertIn("ApiKey", url)

    def test_no_client_refuses_it(self):
        # Asked of the decision alone: a video with no client cannot build a
        # url either, so there is no play to run. The guard is for a
        # half-built client, where the answer must be "no header" rather than
        # an AttributeError out of a start.
        from jellyfin_mpv_shim.player import PlayerManager

        v = _video(OURS, [], client=False)
        pm = PlayerManager.__new__(PlayerManager)
        pm._player = _FakePlayer()
        pm._mpv_alive = True
        self.assertFalse(pm._apply_auth_headers(v))
        self.assertNotIn(STALE, " ".join(pm._player.http_header_fields or []))

    def test_an_unauthenticated_probe_refuses_it(self):
        """A header with no Token= in it. Claiming success here would strip a
        url that needs one."""
        installed, headers, url, _s = _play(
            OURS, [], header='MediaBrowser Client="x"')
        self.assertFalse(installed)
        self.assertNotIn(STALE, headers)
        self.assertIn("ApiKey", url)

    def test_a_header_that_cannot_be_built_refuses_it(self):
        installed, headers, url, _s = _play(OURS, [], header=Exception)
        self.assertFalse(installed)
        self.assertNotIn(STALE, headers)
        self.assertIn("ApiKey", url)

    def test_mpv_refusing_the_option_refuses_it(self):
        installed, _h, url, _s = _play(OURS, [], refuse=True)
        self.assertFalse(installed)
        self.assertIn("ApiKey", url)


class WhoeverCarriesItOurSidecarsAreCoveredTest(unittest.TestCase):
    """The invariant across both decisions, and the one nothing stated.

    `map_streams` builds our sidecar urls with no token because the header is
    going to cover them. So **every** path that ends with mpv not carrying the
    header owes those urls a credential — otherwise they are fetched with none
    at all and come back 401, i.e. no captions.

    `reauthorize_sidecars` was called from exactly one of them (the revoke,
    decision B). The tests that would have said so were stranded after a
    module-level `unittest.main()` in `test_player_auth_scope` and had never
    run.
    """

    def _sidecar(self, subs):
        return subs.get(2) or ""

    def test_a_foreign_subtitle_does_not_strand_our_own(self):
        """Decision A's most likely refusal, and the one guaranteed to hit
        this: the reason the header is refused is that the item HAS external
        subtitles, so there is always one of ours to strand."""
        _i, headers, _u, subs = _play(OURS, [OUR_SIDECAR, THEIR_SIDECAR])
        self.assertNotIn(TOKEN, headers)
        self.assertIn("ApiKey=", self._sidecar(subs),
                      "our own subtitle was left with no credential at all "
                      "because the header was declined rather than revoked")

    def test_and_the_foreign_one_still_gets_nothing(self):
        """The control. A fix that tokenized every sidecar would pass the test
        above and hand the token to subs.example.invalid."""
        _i, _h, _u, subs = _play(OURS, [OUR_SIDECAR, THEIR_SIDECAR])
        self.assertNotIn(TOKEN, subs.get(3) or "")

    def test_a_dead_mpv_does_not_strand_our_own(self):
        _i, _h, _u, subs = _play(OURS, [OUR_SIDECAR], alive=False)
        self.assertIn("ApiKey=", self._sidecar(subs))

    def test_an_unauthenticated_probe_does_not_strand_our_own(self):
        """A header with no Token= in it, but a session that has one.

        This asserted only that the url contained no "ApiKey=&", which
        `reauthorize_sidecars` cannot produce for ANY token -- it appends the
        parameter last, so nothing can follow it. The test passed with the
        whole invariant deleted, and its comment claimed the sidecar could not
        be credentialed when the fixture had a perfectly good token to give.
        """
        _i, _h, _u, subs = _play(OURS, [OUR_SIDECAR],
                                 header='MediaBrowser Client="x"')
        self.assertIn("ApiKey=" + TOKEN, self._sidecar(subs))

    def test_with_no_token_anywhere_the_url_is_left_alone(self):
        """The case the one above was *described* as. There is nothing to
        credential the sidecar with, so the requirement is only that it is
        left intact rather than mangled onto an empty parameter."""
        _i, _h, _u, subs = _play(OURS, [OUR_SIDECAR],
                                 header='MediaBrowser Client="x"', token="")
        self.assertEqual(self._sidecar(subs),
                         SERVER + OUR_SIDECAR["DeliveryUrl"])

    def test_mpv_refusing_the_option_does_not_strand_our_own(self):
        _i, _h, _u, subs = _play(OURS, [OUR_SIDECAR], refuse=True)
        self.assertIn("ApiKey=", self._sidecar(subs))

    def test_a_foreign_media_host_does_not_strand_our_own(self):
        """Decision B — the one path that was already wired. Kept here so the
        rule is stated once, over both decisions, rather than twice."""
        _i, headers, _u, subs = _play(THEIRS, [OUR_SIDECAR])
        self.assertNotIn(TOKEN, headers)
        self.assertIn("ApiKey=", self._sidecar(subs))

    def test_the_header_carrying_it_needs_no_url_token(self):
        """The other half, so a fix cannot simply tokenize everything: with
        the header installed the sidecar must stay clean, because that url
        can end up in a log or a `ps` line."""
        installed, _h, _u, subs = _play(OURS, [OUR_SIDECAR])
        self.assertTrue(installed)
        self.assertNotIn("ApiKey", self._sidecar(subs))


if __name__ == "__main__":
    unittest.main()
