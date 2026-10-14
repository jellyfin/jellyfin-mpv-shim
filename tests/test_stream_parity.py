"""What the server can deliver for bitmap subtitles and external audio, and
whether the shim asks for it.

All measured against Jellyfin 10.11.11 and 12.2 (stdjflib fixtures
`s-pgs-external`, `s-mks-vobsub`, `s-vobsub-external`, `x-external-audio`):

* An external `.sup` is `Encode` -- a burn-in transcode -- unless the profile
  offers `pgssub` as `External` (#811). Both versions then serve the raw file.
* A VobSub `.mks` sidecar is served raw only on 12.0+, through a `dvdsub`
  profile with `Container: mks`. **10.11 ignores `Container`**, so the same
  entry turns VobSub `.mks` and `.idx/.sub` -- two working burn-ins -- into
  sidecar URLs that answer 404 and 400.
* An external audio track is never direct-played: selecting one transcodes
  (`AudioIsExternal`). The server numbers external streams FIRST, so the first
  external track is index 0.
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

SERVER = "https://jf.example.invalid"

PGS_EXTERNAL = {"Format": "pgssub", "Method": "External"}
VOBSUB_MKS = {"Format": "dvdsub", "Method": "External", "Container": "mks"}

#: `x-external-audio` as both servers number it: the sidecars first.
EXTERNAL_AUDIO_STREAMS = [
    {"Index": 0, "Type": "Audio", "Codec": "aac", "Language": "deu",
     "IsExternal": True},
    {"Index": 1, "Type": "Audio", "Codec": "aac", "Language": "jpn",
     "IsExternal": True},
    {"Index": 2, "Type": "Video", "Codec": "h264"},
    {"Index": 3, "Type": "Audio", "Codec": "aac", "Language": "eng"},
    {"Index": 4, "Type": "Audio", "Codec": "aac", "Language": "fra"},
]


def _subtitle_profiles(**kw):
    from jellyfin_mpv_shim.utils import get_profile
    return get_profile(**kw)["SubtitleProfiles"]


class SubtitleProfileTest(unittest.TestCase):

    def test_an_external_pgs_sidecar_is_offered_as_one(self):
        """#811: without it the `.sup` the server picks by default is burnt
        in, so a file mpv plays directly is transcoded."""
        self.assertIn(PGS_EXTERNAL, _subtitle_profiles())

    def test_vobsub_mks_is_offered_to_a_12_server(self):
        for version in ("12.0.0", "12.2.0", "13.1.4"):
            with self.subTest(version=version):
                self.assertIn(VOBSUB_MKS,
                              _subtitle_profiles(server_version=version))

    def test_but_never_to_one_that_ignores_its_container(self):
        """No version, or one that does not parse, is treated as old: the
        cost of a wrong "old" is a burn-in, which works; the cost of a wrong
        "new" is no subtitles at all."""
        for version in ("10.11.11", "10.10.7", None, "", "unstable"):
            with self.subTest(version=version):
                profiles = _subtitle_profiles(server_version=version)
                self.assertFalse(
                    [p for p in profiles if p.get("Container")],
                    "a 10.x server answers this entry with a sidecar url "
                    "that 404s/400s instead of a working burn-in")


class _PlayInfoRecorder:
    """The real apiclient `get_play_info` over a recorded HTTP layer, so the
    assertion is on the body the server receives -- the apiclient's own
    filtering included."""

    def __init__(self, source):
        from jellyfin_apiclient_python.api import API

        self.source = source
        self.bodies = []
        self.get_play_info = API.get_play_info.__get__(self)

    def items(self, handler, action="GET", params=None, json=None):
        self.bodies.append(json)
        return {"MediaSources": [self.source]}

    def get_item(self, item_id, **kw):
        return self.item


def _video(source, version="12.2.0"):
    from jellyfin_mpv_shim.media import Video

    item = {"Type": "Movie", "Name": "x", "MediaSources": [source],
            "RunTimeTicks": 1}
    api = _PlayInfoRecorder(source)
    api.item = item
    client = NS(
        config=NS(data={"auth.server": SERVER, "auth.token": "t",
                        "auth.server-id": "sid"}),
        http=NS(_get_authenication_header=lambda: 'MediaBrowser Token="t"'),
        auth=NS(credentials=NS(get_credentials=lambda: {
            "Servers": [{"Version": version}]})),
        jellyfin=api,
    )
    parent = NS(client=client, is_local=True, item=item)
    v = Video("item1", parent)
    v.item = item
    v.explicit_tracks = True
    v.aid = v.sid = None
    v.track_memory = None
    return v, api


def _source(streams, **kw):
    src = {"Id": "src1", "MediaStreams": [dict(s) for s in streams],
           "SupportsDirectPlay": True, "SupportsDirectStream": True,
           "SupportsTranscoding": True, "Protocol": "File",
           "Path": "/m/x.mkv", "Container": "mkv", "RunTimeTicks": 1}
    src.update(kw)
    return src


class TheNegotiationTest(unittest.TestCase):

    def _negotiate(self, video):
        from jellyfin_mpv_shim.conf import settings
        with mock.patch.object(settings, "always_transcode", False), \
                mock.patch.object(settings, "language_config", None):
            video.get_playback_url()

    def test_index_zero_reaches_the_server(self):
        """The first external stream is index 0, and the apiclient's
        `if aid:` drops it -- so picking that track sends nothing, the server
        plays its default, and nothing errors."""
        for kind, field in (("aid", "AudioStreamIndex"),
                            ("sid", "SubtitleStreamIndex")):
            with self.subTest(kind):
                v, api = _video(_source(EXTERNAL_AUDIO_STREAMS))
                setattr(v, kind, 0)
                self._negotiate(v)
                self.assertEqual(api.bodies[0].get(field), 0)
                self.assertEqual(api.bodies[0].get("MediaSourceId"), "src1",
                                 "an index without its source is dropped")

    def test_none_still_sends_nothing(self):
        """The control: the server's own "nothing" is an absent field."""
        v, api = _video(_source(EXTERNAL_AUDIO_STREAMS))
        self._negotiate(v)
        self.assertNotIn("AudioStreamIndex", api.bodies[0])
        self.assertNotIn("SubtitleStreamIndex", api.bodies[0])

    def test_the_profile_knows_which_server_it_is_for(self):
        for version, offered in (("12.2.0", True), ("10.11.11", False)):
            with self.subTest(version=version):
                v, api = _video(_source(EXTERNAL_AUDIO_STREAMS), version)
                self._negotiate(v)
                subs = api.bodies[0]["DeviceProfile"]["SubtitleProfiles"]
                self.assertEqual(VOBSUB_MKS in subs, offered)


def _mapped(streams):
    from jellyfin_mpv_shim.media import Video

    v = Video.__new__(Video)
    v.media_source = _source(streams)
    v.item = {}
    v.explicit_tracks = True
    v.aid = v.sid = None
    v.map_streams()
    return v


class ExternalAudioTest(unittest.TestCase):

    def test_an_external_track_has_no_mpv_id_in_direct_play(self):
        """mpv only has the file's own tracks. Mapping a sidecar onto the
        counter gave deu, jpn and eng all aid 1, so picking deu played eng."""
        v = _mapped(EXTERNAL_AUDIO_STREAMS)
        self.assertEqual(v.audio_seq, {3: 1, 4: 2})

    def test_picking_one_renegotiates(self):
        v = _mapped(EXTERNAL_AUDIO_STREAMS)
        v.aid = 3
        v.is_transcode = False
        self.assertTrue(v.set_streams(0, None),
                        "only the server can deliver a sidecar track, and "
                        "only in a transcode")

    def test_and_an_embedded_one_does_not(self):
        """The control: switching between the file's own tracks is mpv's job."""
        v = _mapped(EXTERNAL_AUDIO_STREAMS)
        v.aid = 3
        v.is_transcode = False
        self.assertFalse(v.set_streams(4, None))

    def test_leaving_the_transcode_for_an_embedded_track_renegotiates(self):
        v = _mapped(EXTERNAL_AUDIO_STREAMS)
        v.aid = 0
        v.is_transcode = True
        self.assertTrue(v.set_streams(3, None))

    def test_offline_has_the_same_numbering(self):
        """The second implementation of the counter, where the sidecar is not
        even on disk."""
        from jellyfin_mpv_shim.sync.offline_media import OfflineVideo

        v = OfflineVideo.__new__(OfflineVideo)
        v.media_source = _source(EXTERNAL_AUDIO_STREAMS)
        v._subs_dir = "/nonexistent"
        v.item = {}
        v.explicit_tracks = True
        v.aid = v.sid = None
        v.map_streams()
        self.assertEqual(v.audio_seq, {3: 1, 4: 2})
        v.aid, v.is_transcode = 3, False
        self.assertFalse(v.set_streams(0, None),
                         "offline there is no server to renegotiate with")


class DownloadedSidecarTest(unittest.TestCase):
    """The downloader names a sidecar after the format, and a bitmap one
    used to fall through to `.srt` -- a binary PGS file under a text
    subtitle's extension."""

    def _download(self, stream):
        import os
        import tempfile
        from jellyfin_mpv_shim.sync import manager as mod

        class _Resp:
            content = b"payload"

            def raise_for_status(self):
                pass

        client = NS(
            config=NS(data={"auth.server": SERVER, "auth.token": "t"}),
            http=NS(_get_authenication_header=lambda: 'MediaBrowser Token="t"'),
            jellyfin=NS(subtitle_url=lambda *a, **kw: SERVER + "/built"))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(mod.requests, "get",
                               lambda *a, **kw: _Resp()):
            mod.SyncManager._download_subs(
                mod.SyncManager.__new__(mod.SyncManager), client, "i1",
                {"Id": "s1", "MediaStreams": [stream]}, tmp.name)
        return sorted(os.listdir(os.path.join(tmp.name, "subs")))

    def _stream(self, codec, served):
        return {"Type": "Subtitle", "IsExternal": True, "Index": 0,
                "Codec": codec,
                "DeliveryUrl": "/Videos/i1/s1/Subtitles/0/0/Stream." + served}

    def test_pgs_keeps_a_pgs_extension(self):
        self.assertEqual(self._download(self._stream("PGSSUB", "pgssub")),
                         ["0.sup"])

    def test_vobsub_arrives_as_matroska(self):
        self.assertEqual(self._download(self._stream("DVDSUB", "dvdsub")),
                         ["0.mks"])

    def test_text_is_named_as_before(self):
        """The control."""
        self.assertEqual(self._download(self._stream("subrip", "srt")),
                         ["0.srt"])


if __name__ == "__main__":
    unittest.main()
