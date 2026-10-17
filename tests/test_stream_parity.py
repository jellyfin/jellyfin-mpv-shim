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
        """The first external stream is index 0, and an apiclient before
        1.20.2 (`if aid:`) drops it -- so picking that track sends nothing,
        the server plays its default, and nothing errors. This runs the
        installed apiclient, so it also fails an environment below the pin."""
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


#: An embedded PGS track (2) and an external .sup (0), as both servers number
#: them.
PGS_STREAMS = [
    {"Index": 0, "Type": "Subtitle", "Codec": "PGSSUB", "Language": "eng",
     "IsExternal": True},
    {"Index": 1, "Type": "Video", "Codec": "h264"},
    {"Index": 2, "Type": "Subtitle", "Codec": "PGSSUB", "Language": "fra"},
    {"Index": 3, "Type": "Audio", "Codec": "aac", "Language": "eng"},
]


class ExtractionOptOutTest(unittest.TestCase):
    """With `{pgssub, External}` offered, a transcode hands out an EMBEDDED
    PGS track as a sidecar the server extracts on fetch -- every subtitle
    stream in the file, into its cache. A profile entry cannot tell embedded
    from external, so the opt-out leaves the entry out of the one request
    whose subtitle is an embedded image track."""

    def _offered(self, sid, extract, default=None):
        from jellyfin_mpv_shim.conf import settings

        v, api = _video(_source(PGS_STREAMS,
                                DefaultSubtitleStreamIndex=default))
        v.sid = sid
        with mock.patch.object(settings, "extract_image_subtitles", extract), \
                mock.patch.object(settings, "always_transcode", False), \
                mock.patch.object(settings, "language_config", None):
            v.get_playback_url()
        return PGS_EXTERNAL in api.bodies[0]["DeviceProfile"]["SubtitleProfiles"]

    def test_opting_out_burns_an_embedded_track_in(self):
        self.assertFalse(self._offered(2, extract=False))

    def test_the_servers_own_default_counts_as_the_choice(self):
        self.assertFalse(self._offered(None, extract=False, default=2))

    def test_an_external_sup_stays_a_sidecar_either_way(self):
        """#811 is not what is being opted out of."""
        self.assertTrue(self._offered(0, extract=False))

    def test_opted_in_offers_it(self):
        self.assertTrue(self._offered(2, extract=True))

    def test_switching_to_an_extracted_track_mid_transcode_renegotiates(self):
        from jellyfin_mpv_shim.conf import settings

        v = _mapped(PGS_STREAMS)
        v.is_transcode = True
        v.subtitle_extracted = frozenset({2})
        for extract, restart in ((False, True), (True, False)):
            with self.subTest(extract=extract), \
                    mock.patch.object(settings, "extract_image_subtitles",
                                      extract):
                v.sid = 0
                self.assertEqual(v.set_streams(None, 2), restart)


#: x-external-audio-default: the server's default is the sidecar.
EXTERNAL_DEFAULT_STREAMS = [
    {"Index": 0, "Type": "Audio", "Codec": "aac", "Language": "spa",
     "IsExternal": True, "IsDefault": True},
    {"Index": 1, "Type": "Video", "Codec": "h264"},
    {"Index": 2, "Type": "Audio", "Codec": "aac", "Language": "eng",
     "IsDefault": True},
    {"Index": 3, "Type": "Audio", "Codec": "aac", "Language": "fra"},
    {"Index": 4, "Type": "Subtitle", "Codec": "subrip", "Language": "eng",
     "IsExternal": True},
]


class OfflineFallsThroughTest(unittest.TestCase):
    """Offline, a track that is not on disk is no answer at its level of the
    chain (docs/track-selection.md), and resolution moves to the next one.
    External audio is never downloaded; a sidecar subtitle may be missing."""

    def _offline(self, subs_on_disk=(), **source_kw):
        import os
        import tempfile
        from jellyfin_mpv_shim.sync.offline_media import OfflineVideo

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for index in subs_on_disk:
            open(os.path.join(tmp.name, "%d.srt" % index), "wb").close()
        v = OfflineVideo.__new__(OfflineVideo)
        v._source = _source(EXTERNAL_DEFAULT_STREAMS, **source_kw)
        v.media_source = None
        v._subs_dir = tmp.name
        v.item = {"MediaSources": [v._source]}
        v.srcid = None
        v.explicit_tracks = False
        v.aid = v.sid = None
        return v

    def _resolve(self, v, rules=None):
        from jellyfin_mpv_shim.conf import settings
        with mock.patch.object(settings, "language_config", rules):
            v.resolve_tracks_for_negotiation()
            v.media_source = dict(v._source)
            v.map_streams()

    def test_a_missing_default_falls_to_the_files_own(self):
        v = self._offline(DefaultAudioStreamIndex=0)
        self._resolve(v)
        self.assertEqual(v.aid, 2, "the HUD names a track mpv is not playing")

    def test_a_rule_naming_a_missing_track_does_not_match(self):
        from jellyfin_mpv_shim.language_config import parse_language_config
        v = self._offline(DefaultAudioStreamIndex=3)
        self._resolve(v, parse_language_config([{"alang": "spa"}]))
        self.assertEqual(v.aid, 3, "the next level, the source default")

    def test_the_memory_ranks_only_what_is_on_disk(self):
        streams = self._offline().source_for_track_rules()["MediaStreams"]
        self.assertEqual(sorted(s["Index"] for s in streams), [1, 2, 3])

    def test_an_explicit_pick_falls_through_only_where_it_is_missing(self):
        """The playable half of a pick stands; the other half takes the next
        level, the source default."""
        v = self._offline((4,), DefaultAudioStreamIndex=3)
        v.explicit_tracks = True
        v.aid, v.sid = 0, 4
        self._resolve(v)
        self.assertEqual((v.aid, v.sid), (3, 4))

    def test_a_sidecar_subtitle_counts_only_if_downloaded(self):
        for on_disk, sid in (((), None), ((4,), 4)):
            with self.subTest(on_disk=on_disk):
                v = self._offline(on_disk, DefaultSubtitleStreamIndex=4)
                self._resolve(v)
                self.assertEqual(v.sid, sid)


if __name__ == "__main__":
    unittest.main()
