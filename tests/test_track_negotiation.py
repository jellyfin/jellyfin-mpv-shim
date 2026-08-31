"""Track choices must be resolved BEFORE PlaybackInfo, not after it.

`get_playback_url` posts `self.aid`/`self.sid` to `get_play_info`, and for a
transcode the server bakes the audio index it is given into `TranscodingUrl`.
`map_streams` -- where `language_config` was applied -- runs one line *after*
that call, so the rule was computed after the negotiation it exists to
influence. `configure_streams` cannot repair it either: it skips audio
selection on a transcode, correctly, because the audio is already encoded into
the stream.

The result was that `language_config` -- the one setting whose entire job is
to override the server's choice -- did nothing at all for any transcode, while
the HUD ticked the language it had chosen and the shim reported that index to
the server. Remembered episode tracks had the same shape one layer up.

The reach is what made it matter: `explicit_tracks` is set in exactly one
place (`gateway/playback.py`), so the broken path covers every grid-tile Play,
Continue Watching, Next Up, Play All, CLI --play, cast, and **every episode
after the first in a queue** -- `Media.get_next` does not forward the flag.

These tests assert the value **PlaybackInfo was asked with**, which is the
step the old coverage skipped: `tests/test_remote_playback.py` asserts
`video.aid` on the object afterwards, and the only transcode track test builds
`Media(explicit_tracks=True)`, returning before the rule ever runs.
"""

import sys
import unittest
from types import SimpleNamespace as NS
from unittest import mock

sys.argv = [sys.argv[0]]

SERVER = "https://jf.example.invalid"

STREAMS = [
    {"Index": 0, "Type": "Video", "Codec": "h264"},
    {"Index": 1, "Type": "Audio", "Codec": "aac", "Language": "eng"},
    {"Index": 2, "Type": "Audio", "Codec": "aac", "Language": "jpn"},
]


def _source(**kw):
    src = {"Id": "src1", "MediaStreams": [dict(s) for s in STREAMS],
           "SupportsDirectPlay": False, "SupportsDirectStream": False,
           "SupportsTranscoding": True,
           "TranscodingUrl": "/videos/1/master.m3u8?AudioStreamIndex=1",
           "DefaultAudioStreamIndex": 1, "RunTimeTicks": 1}
    src.update(kw)
    return src


class _Recorder:
    """Records what PlaybackInfo was asked with -- the assertion these tests
    exist for."""

    def __init__(self, source):
        self.source = source
        self.asked = []

    def get_play_info(self, item_id, profile, aid, sid, media_source_id=None):
        self.asked.append({"aid": aid, "sid": sid})
        return {"MediaSources": [self.source]}

    def get_item(self, item_id, **kw):
        return self.item


def _video(source, memory=None, explicit=False):
    from jellyfin_mpv_shim.media import Video

    item = {"Type": "Episode", "Name": "Ep", "MediaSources": [source],
            "RunTimeTicks": 1}
    api = _Recorder(source)
    api.item = item
    client = NS(
        config=NS(data={"auth.server": SERVER, "auth.token": "t",
                        "auth.server-id": "sid"}),
        http=NS(_get_authenication_header=lambda: 'MediaBrowser Token="t"'),
        jellyfin=api,
    )
    parent = NS(client=client, is_local=True, item=item)
    v = Video("ep1", parent)
    v.item = item
    v.explicit_tracks = explicit
    v.aid = v.sid = None
    v.track_memory = memory
    return v, api


def _rules(spec):
    from jellyfin_mpv_shim.language_config import parse_language_config
    return parse_language_config(spec)


class PlaybackInfoCarriesTheChoiceTest(unittest.TestCase):

    def _play(self, video):
        from jellyfin_mpv_shim.conf import settings
        with mock.patch.object(settings, "always_transcode", False):
            return video.get_playback_url()

    def test_language_config_reaches_playbackinfo(self):
        from jellyfin_mpv_shim.conf import settings

        v, api = _video(_source())
        with mock.patch.object(settings, "language_config",
                               _rules([{"alang": "jpn"}])):
            self._play(v)
        self.assertEqual(api.asked[0]["aid"], 2,
                         "PlaybackInfo was negotiated without the rule's "
                         "track, so a transcode carries the server's default "
                         "and the rule is inert")

    def test_no_rule_still_lets_the_server_choose(self):
        """The control, and it matters: resolving a default client-side and
        posting it would be a behaviour change. Posting nothing is what makes
        Jellyfin fall back to DefaultAudioStreamIndex itself, so client and
        server agree."""
        from jellyfin_mpv_shim.conf import settings

        v, api = _video(_source())
        with mock.patch.object(settings, "language_config", None):
            self._play(v)
        self.assertIsNone(api.asked[0]["aid"])

    def test_an_explicit_pick_is_what_gets_negotiated(self):
        """A deliberate selection outranks everything: the user consciously
        overrode it."""
        from jellyfin_mpv_shim.conf import settings

        v, api = _video(_source(), explicit=True)
        v.aid = 1
        with mock.patch.object(settings, "language_config",
                               _rules([{"alang": "jpn"}])):
            self._play(v)
        self.assertEqual(api.asked[0]["aid"], 1)


def _pm(memory=None):
    """A PlayerManager with only what play() touches."""
    from jellyfin_mpv_shim.player import PlayerManager

    pm = PlayerManager.__new__(PlayerManager)
    pm._player = NS(http_header_fields=[])
    pm._mpv_alive = True
    pm.should_send_timeline = False
    pm.start_time = 0.0
    pm._load_cancelled = False
    pm._start_in_progress = False
    pm._track_memory = memory
    pm.started = []
    pm._play_media = lambda video, url, *a, **kw: pm.started.append(url)
    return pm


class RememberedTracksReachNegotiationTest(unittest.TestCase):
    """The same defect one layer up.

    `_apply_remembered_tracks` ran inside `_play_media`, i.e. after the url had
    been negotiated and loaded. For a transcode that cannot work: the audio is
    already encoded into the stream and `configure_streams` skips it, so the
    next episode played the server's default while `video.aid` -- and
    therefore the HUD and the progress report -- claimed the remembered one.
    """

    #: The previous episode's source, shaped so _rank_stream can match on
    #: language + codec + relative position.
    PREV = {"MediaStreams": [dict(s) for s in STREAMS]}

    def test_a_remembered_audio_track_is_negotiated(self):
        from jellyfin_mpv_shim.conf import settings

        v, api = _video(_source())
        pm = _pm(memory=(self.PREV, 2, None))
        with mock.patch.object(settings, "language_config", None), \
                mock.patch.object(settings, "remember_audio_track", True), \
                mock.patch.object(settings, "remember_subtitle_track", False):
            pm.play(v)
        self.assertEqual(api.asked[0]["aid"], 2,
                         "the remembered track was applied after the url was "
                         "negotiated, so a transcode plays the server's "
                         "default while the UI claims otherwise")

    def test_memory_outranks_language_config(self):
        """Precedence, which the old order gave for free: the rule ran in
        map_streams and memory overwrote it afterwards. Both now run before
        the negotiation, so the order between them has to be kept
        deliberately."""
        from jellyfin_mpv_shim.conf import settings

        v, api = _video(_source())
        pm = _pm(memory=(self.PREV, 1, None))     # remembered english
        with mock.patch.object(settings, "language_config",
                               _rules([{"alang": "jpn"}])), \
                mock.patch.object(settings, "remember_audio_track", True), \
                mock.patch.object(settings, "remember_subtitle_track", False):
            pm.play(v)
        self.assertEqual(api.asked[0]["aid"], 1,
                         "language_config overwrote a track the user had "
                         "carried over from the previous episode")

    def test_a_restart_keeps_the_track_the_user_just_picked(self):
        """The same bug in the opposite direction. Picking a track calls
        set_streams then restarts; the pick reached the negotiation, and then
        the rule overwrote `video.aid`, so the stream carried one track while
        the UI and the server were told another."""
        from jellyfin_mpv_shim.conf import settings

        v, api = _video(_source())
        v.set_streams(1, None)          # the user picks english
        pm = _pm()
        with mock.patch.object(settings, "language_config",
                               _rules([{"alang": "jpn"}])):
            pm.play(v)
        self.assertEqual(api.asked[0]["aid"], 1)
        self.assertEqual(v.aid, 1,
                         "the rule overwrote the track the user just chose")


class ConstructedWithoutInitTest(unittest.TestCase):
    """Two production paths build a Video without running `Video.__init__`.

    `OfflineVideo.__init__` deliberately does not call super() (that would hit
    the server), and every test helper here uses `__new__`. Track state
    therefore cannot live only in `__init__`: an instance attribute alone made
    `map_streams` raise AttributeError on offline playback, which the online
    tests could not see.
    """

    def test_the_resolved_flag_exists_without_init(self):
        from jellyfin_mpv_shim.media import Video

        v = Video.__new__(Video)
        self.assertFalse(v._tracks_resolved)

    def test_map_streams_survives_it(self):
        from jellyfin_mpv_shim.media import Video

        v = Video.__new__(Video)
        v.media_source = _source()
        v.item = {}
        v.explicit_tracks = False
        v.aid = v.sid = None
        v.map_streams()          # must not raise

    def test_offline_playback_does_not_run_the_negotiation_hook(self):
        """OfflineVideo negotiates nothing and applies the rule from the
        *local* source in its own map_streams, so the base hook would be a
        second answer to a settled question. play() calls it unconditionally,
        so the override is what keeps offline playback off that path."""
        from jellyfin_mpv_shim.media import Video
        from jellyfin_mpv_shim.sync.offline_media import OfflineVideo

        self.assertIsNot(OfflineVideo.resolve_tracks_for_negotiation,
                         Video.resolve_tracks_for_negotiation)


if __name__ == "__main__":
    unittest.main()
