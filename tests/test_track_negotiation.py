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
        # media_source_id is recorded because the server needs it to honour
        # the index at all -- see TheSourceIsPinnedWithTheIndexTest.
        self.asked.append({"aid": aid, "sid": sid,
                           "srcid": media_source_id})
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
    # A real PlayerManager always has one; CLI mode sets it None.
    pm.menu = None
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


class TheSourceIsPinnedWithTheIndexTest(unittest.TestCase):
    """A stream index means nothing without the source it indexes into.

    Measured on Jellyfin 12.0: PlaybackInfo **silently ignores
    AudioStreamIndex unless MediaSourceId is sent with it**, and falls back to
    the source's DefaultAudioStreamIndex. Asking for six different tracks
    returned the default six times; adding the id returned each one. So track
    selection on a transcode was inert for every ordinary play, where `srcid`
    is None -- and the e2e test that should have caught it asked for the one
    index that happened to BE the default.
    """

    def _asked(self, video, api):
        from jellyfin_mpv_shim.conf import settings
        with mock.patch.object(settings, "always_transcode", False):
            video.get_playback_url()
        return api.asked[0]

    def test_asking_for_a_track_pins_the_source(self):
        from jellyfin_mpv_shim.conf import settings

        v, api = _video(_source())
        with mock.patch.object(settings, "language_config",
                               _rules([{"alang": "jpn"}])):
            asked = self._asked(v, api)
        self.assertEqual(asked["aid"], 2)
        self.assertEqual(asked["srcid"], "src1",
                         "an index was sent with no source to index into, "
                         "which the server drops")

    def test_asking_for_nothing_leaves_the_source_to_the_server(self):
        """Pinning unconditionally would stop the server choosing between the
        versions of a multi-version item, which is what it does when no track
        is requested."""
        from jellyfin_mpv_shim.conf import settings

        v, api = _video(_source())
        with mock.patch.object(settings, "language_config", None):
            asked = self._asked(v, api)
        self.assertIsNone(asked["aid"])
        self.assertIsNone(asked["srcid"])


class TheSourcePinIsOnlyForARealIndexTest(unittest.TestCase):
    """`sid = -1` is "no subtitles", not an index into a source.

    `_apply_remembered_tracks` sets it on every advance whose previous item
    had subtitles off, and `remember_subtitle_track` defaults on -- so keying
    the MediaSourceId pin off "not None" pinned `MediaSources[0]` for almost
    every play, taking the source choice away from the server on
    multi-version items. Which is the opposite of what the pin is for.
    """

    def _asked(self, video, api):
        from jellyfin_mpv_shim.conf import settings
        with mock.patch.object(settings, "always_transcode", False):
            video.get_playback_url()
        return api.asked[0]

    def test_subtitles_off_does_not_pin_the_source(self):
        from jellyfin_mpv_shim.conf import settings

        v, api = _video(_source())
        v.sid = -1                      # what the memory sets
        with mock.patch.object(settings, "language_config", None):
            asked = self._asked(v, api)
        self.assertEqual(asked["sid"], -1, "the choice must still be sent")
        self.assertIsNone(
            asked["srcid"],
            "a source was pinned for `no subtitles`, which indexes into "
            "nothing -- so the server can no longer choose between versions")

    def test_a_real_index_still_pins(self):
        from jellyfin_mpv_shim.conf import settings

        v, api = _video(_source())
        with mock.patch.object(settings, "language_config",
                               _rules([{"alang": "jpn"}])):
            asked = self._asked(v, api)
        self.assertEqual(asked["aid"], 2)
        self.assertEqual(asked["srcid"], "src1")


#: A second version of the same episode: same streams, lower resolution, and
#: -- the part that matters -- it can only be transcoded.
VERSION_2 = {"Id": "src2", "MediaStreams": [dict(s) for s in STREAMS],
             "SupportsDirectPlay": False, "SupportsDirectStream": False,
             "SupportsTranscoding": True,
             "TranscodingUrl": "/videos/1/v2.m3u8?AudioStreamIndex=1",
             "DefaultAudioStreamIndex": 1, "RunTimeTicks": 1}


class _MultiVersionRecorder(_Recorder):
    """PlaybackInfo the way the server answers an item with several versions.

    Measured on the QA server (`Pilot`, three versions): asking with no
    MediaSourceId returns **all three**; asking with one returns **that one**.
    The single-source `_Recorder` above cannot express that difference, and
    that is precisely why the pin below went unnoticed for a release -- with
    one version in the fixture, pinning it and leaving the choice open answer
    identically, so every test here passed either way.
    """

    def __init__(self, sources):
        self.sources = sources
        self.asked = []

    def get_play_info(self, item_id, profile, aid, sid, media_source_id=None):
        self.asked.append({"aid": aid, "sid": sid, "srcid": media_source_id})
        if media_source_id is None:
            return {"MediaSources": [dict(s) for s in self.sources]}
        return {"MediaSources": [dict(s) for s in self.sources
                                 if s["Id"] == media_source_id]}


def _multi_version_video(sources):
    from jellyfin_mpv_shim.media import Video

    item = {"Type": "Episode", "Name": "Ep",
            "MediaSources": [dict(s) for s in sources], "RunTimeTicks": 1}
    api = _MultiVersionRecorder(sources)
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
    v.explicit_tracks = False
    v.aid = v.sid = None
    v.track_memory = None
    return v, api


class MultiVersionSourcePinTest(unittest.TestCase):
    """What the source pin costs an item that has several versions.

    **Accepted for 3.0.0, not a fix waiting to happen** -- see
    `docs/do-not-fix.md` F36. Asking for a track pins `MediaSources[0]`, and
    the server then answers with that source alone. Jellyfin's own
    `SortMediaSources` puts the queried item's source first and otherwise
    sorts by **descending video width**
    (`Emby.Server.Implementations/Library/MediaSourceManager.cs`), so [0] is
    the version you clicked, else the highest-resolution one. That is a
    reasonable default and the shim direct-plays most formats, which is why
    this is accepted rather than fixed before the release.

    What it costs is the two fallbacks below, and both only bite when the
    highest-resolution version is the one this client cannot play -- which is
    the exact setup people keep dual versions for. Weighed against losing the
    remembered audio/subtitle track on every episode advance, which is what
    dropping the pin would cost, and the pin wins.

    So these tests assert the **current** behaviour, including where it fails.
    If a fix lands they are expected to fail: read F36, then update them.
    """

    #: The previous episode, shaped so _rank_stream matches on language.
    PREV = {"MediaStreams": [dict(s) for s in STREAMS]}

    def _play(self, video):
        from jellyfin_mpv_shim.conf import settings
        with mock.patch.object(settings, "always_transcode", False):
            return video.get_playback_url()

    def test_a_remembered_track_pins_the_first_version(self):
        """The accepted cost, stated plainly."""
        from jellyfin_mpv_shim.conf import settings

        v, api = _multi_version_video([_source(), VERSION_2])
        pm = _pm(memory=(self.PREV, 2, None))
        with mock.patch.object(settings, "language_config", None), \
                mock.patch.object(settings, "remember_audio_track", True), \
                mock.patch.object(settings, "remember_subtitle_track", False), \
                mock.patch.object(settings, "always_transcode", False):
            pm.play(v)
        self.assertEqual(api.asked[0]["srcid"], "src1")
        self.assertEqual(
            len(v.playback_info["MediaSources"]), 1,
            "the server was asked for one version and answered with one, so "
            "get_best_media_source has nothing left to choose between")

    def test_the_remembered_track_is_what_the_pin_buys(self):
        """And the reason it is kept: the server ignores AudioStreamIndex
        without a MediaSourceId, so without the pin the advance plays the
        server's default while the HUD claims the remembered track."""
        from jellyfin_mpv_shim.conf import settings

        v, api = _multi_version_video([_source(), VERSION_2])
        pm = _pm(memory=(self.PREV, 2, None))
        with mock.patch.object(settings, "language_config", None), \
                mock.patch.object(settings, "remember_audio_track", True), \
                mock.patch.object(settings, "remember_subtitle_track", False), \
                mock.patch.object(settings, "always_transcode", False):
            pm.play(v)
        self.assertEqual(api.asked[0]["aid"], 2)
        self.assertIsNotNone(api.asked[0]["srcid"],
                             "an index with no source to index into is one "
                             "the server drops")

    def test_the_unplayable_retry_still_covers_an_unpinned_play(self):
        """The control, and the thing being traded away. With no track asked
        for, every version is on offer and the `len(MediaSources) > 1` retry
        in get_playback_url can walk to a playable one."""
        from jellyfin_mpv_shim.conf import settings

        dead = _source(SupportsTranscoding=False, TranscodingUrl=None)
        v, api = _multi_version_video([dead, VERSION_2])
        with mock.patch.object(settings, "language_config", None):
            url = self._play(v)
        self.assertIsNone(api.asked[0]["srcid"])
        self.assertIsNotNone(
            url, "the retry did not reach the second version")
        self.assertEqual(v.media_source["Id"], "src2")

    def test_a_pinned_play_has_no_retry_left(self):
        """The same item, the same dead first version, one remembered track --
        and the retry is now unreachable, because the list it walks has one
        entry. This is the failure F36 describes; it is asserted so that a fix
        cannot land silently.
        """
        from jellyfin_mpv_shim.conf import settings

        dead = _source(SupportsTranscoding=False, TranscodingUrl=None)
        v, api = _multi_version_video([dead, VERSION_2])
        v.aid = 2                       # what the memory would have set
        with mock.patch.object(settings, "language_config", None):
            url = self._play(v)
        self.assertEqual(api.asked[0]["srcid"], "src1")
        self.assertIsNone(
            url,
            "the retry reached a playable version -- which is the wanted "
            "behaviour, so F36 has been fixed and this test should now "
            "assert it rather than the failure")


if __name__ == "__main__":
    unittest.main()
