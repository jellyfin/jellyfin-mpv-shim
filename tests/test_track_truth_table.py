"""The whole of track selection, enumerated.

Four things decide the audio and subtitle track a queue item plays with, and
they are spread across three modules:

  1. an explicit pick in the library browser  (`explicit_tracks`, media.py)
  2. `language_config`                        (resolve_tracks_for_negotiation)
  3. the previous episode's track             (_apply_remembered_tracks, player.py)
  4. the source's own defaults                (map_streams)

Each was added on its own and each is tested on its own. What had no test at
all is the **order between them**, which is the only place the interesting
bugs are: every case here is a pair of features that individually work.

The tables below are the contract. `_resolve` runs the real pipeline --
`PlayerManager.play` -> `resolve_tracks_for_negotiation` -> memory ->
`get_playback_url` -> `map_streams` -- and returns what PlaybackInfo was asked
for, which is the value that actually reaches the server and gets baked into
a transcode. Asserting `video.aid` afterwards is what the old coverage did and
is exactly the assertion a negotiation-order bug passes.

Read `docs/track-selection.md` alongside this; it holds the same two tables in
prose and the reasoning for each rule.
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

#: Audio 1=eng 2=jpn, subtitles 3=eng 4=jpn. Indices are deliberately not
#: 0-based and not contiguous per type: a stream index is an index into the
#: SOURCE, not into the per-type list, and code that confuses the two passes
#: on a source where they happen to agree.
ENG_AUDIO, JPN_AUDIO, ENG_SUB, JPN_SUB = 1, 2, 3, 4

FULL = [
    {"Index": 0, "Type": "Video", "Codec": "h264"},
    {"Index": ENG_AUDIO, "Type": "Audio", "Codec": "aac", "Language": "eng",
     "DisplayTitle": "English"},
    {"Index": JPN_AUDIO, "Type": "Audio", "Codec": "aac", "Language": "jpn",
     "DisplayTitle": "Japanese"},
    {"Index": ENG_SUB, "Type": "Subtitle", "Codec": "subrip", "Language": "eng",
     "DisplayTitle": "English", "IsExternal": False, "DeliveryMethod": "Embed"},
    {"Index": JPN_SUB, "Type": "Subtitle", "Codec": "subrip", "Language": "jpn",
     "DisplayTitle": "Japanese", "IsExternal": False, "DeliveryMethod": "Embed"},
]

#: The same item with no subtitle track at all. This is the shape that makes
#: `prev_sid is None` mean "there was nothing to pick" rather than "the user
#: turned subtitles off", and telling those two apart is the whole of
#: SubtitleMemoryTest below.
NO_SUBS = [s for s in FULL if s["Type"] != "Subtitle"]


def _source(streams=None, **kw):
    src = {"Id": "src1",
           "MediaStreams": [dict(s) for s in (streams or FULL)],
           "SupportsDirectPlay": False, "SupportsDirectStream": False,
           "SupportsTranscoding": True,
           "TranscodingUrl": "/videos/1/master.m3u8", "RunTimeTicks": 1}
    src.update(kw)
    return src


class _Recorder:
    def __init__(self, source):
        self.source = source
        self.asked = []

    def get_play_info(self, item_id, profile, aid, sid, media_source_id=None):
        self.asked.append({"aid": aid, "sid": sid, "srcid": media_source_id})
        return {"MediaSources": [self.source]}

    def get_item(self, item_id, **kw):
        return self.item


def _rules(spec):
    from jellyfin_mpv_shim.language_config import parse_language_config
    return parse_language_config(spec)


#: The Dubbed/Subbed dropdown, translated the way conf does it. Used rather
#: than a hand-written rule list wherever a case is about the *feature* --
#: "stop setting dubbed/subbed per episode" -- because the presets are what
#: almost every user of language_config actually has.
def _preset(name, lang="eng"):
    from jellyfin_mpv_shim.language_config import preset_rules
    return _rules(preset_rules(name, lang))


def _video(source, explicit=False):
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
    v = Video.__new__(Video)
    v.item_id = "ep1"
    v.parent = parent
    v.client = client
    v.item = item
    v.aid = v.sid = v.srcid = None
    v.explicit_tracks = explicit
    v.auth_via_header = False
    v.is_tv = True
    v.is_photo = False
    v.media_source = None
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
    return v, api


def _pm(memory=None):
    from jellyfin_mpv_shim.player import PlayerManager

    pm = PlayerManager.__new__(PlayerManager)
    pm._player = NS(http_header_fields=[])
    pm._mpv_alive = True
    pm.should_send_timeline = False
    pm.start_time = 0.0
    pm._load_cancelled = False
    pm._start_in_progress = False
    pm._track_memory = memory
    pm.menu = None
    pm._play_media = lambda video, url, *a, **kw: None
    return pm


def _resolve(source, rules=None, memory=None, explicit=False,
             remember_audio=True, remember_sub=True, aid=None, sid=None,
             initial=False, apply_memory=True):
    """Run the real pipeline once and report ``(aid, sid)`` as negotiated.

    ``memory`` is ``(prev_source, prev_aid, prev_sid)`` or None for the first
    item of a queue. ``aid``/``sid`` seed an explicit browser pick.
    """
    from jellyfin_mpv_shim.conf import settings

    v, api = _video(source, explicit=explicit)
    v.aid, v.sid = aid, sid
    pm = _pm(memory=memory)
    with mock.patch.object(settings, "language_config", rules), \
            mock.patch.object(settings, "remember_audio_track", remember_audio), \
            mock.patch.object(settings, "remember_subtitle_track", remember_sub):
        # is_initial_play defaults False, which is what every episode after
        # the first in a queue gets -- and the only shape memory applies in.
        pm.play(v, is_initial_play=initial, apply_memory=apply_memory)
    asked = api.asked[0]
    # A negotiated value must never disagree with the object: that divergence
    # is the HUD and the progress report naming a track the stream is not
    # carrying, which is how both of the ordering bugs presented.
    #
    # None is the one legal difference, in one direction. Posting no index is
    # what makes Jellyfin apply its own DefaultAudioStreamIndex, so the source
    # default is deliberately NOT resolved before the negotiation -- it is
    # filled in afterwards, by `map_streams`, for the UI. So "asked None, ended
    # at 2" is the documented case and "asked 2, ended at 1" is a bug.
    for got, want, kind in ((asked["aid"], v.aid, "aid"),
                            (asked["sid"], v.sid, "sid")):
        assert got is None or got == want, (
            "PlaybackInfo was asked for %s=%r but the video ended at %r"
            % (kind, got, want))
    return v.aid, v.sid


class SubtitleMemoryTest(unittest.TestCase):
    """`prev_sid is None` is not "the user turned subtitles off".

    -1 is. It is what the OSD menu's "None" entry and the HUD's "Off" entry
    both send (menu.py, osc_bridge.py), and it is a decision. `None` is what
    `video.sid` holds when nothing ever resolved one -- the previous episode
    had no subtitle track, or no rule matched and the source named no default.

    `_apply_remembered_tracks` treated them as the same thing and forced -1
    for both, which overwrites a language_config rule that *did* resolve a
    subtitle for this episode. The audio half of the same method has always
    guarded on `prev_aid is not None`; this is that guard, missing.

    The reach: with a Subbed/Dubbed preset set, one episode in the season
    with no subtitle track turns subtitles off for every episode after it --
    which is the whole thing the preset exists to stop you doing by hand.
    """

    PREV_WITH_SUBS = {"MediaStreams": [dict(s) for s in FULL]}
    PREV_NO_SUBS = {"MediaStreams": [dict(s) for s in NO_SUBS]}

    def test_nothing_resolved_last_time_leaves_the_rule_alone(self):
        aid, sid = _resolve(
            _source(), rules=_preset("subbed"),
            memory=(self.PREV_NO_SUBS, JPN_AUDIO, None))
        self.assertEqual(aid, JPN_AUDIO)
        self.assertEqual(sid, ENG_SUB,
                         "an episode with no subtitle track turned subtitles "
                         "off for the rest of the season, over the top of the "
                         "language rule that had just chosen one")

    def test_subtitles_off_by_hand_is_still_carried(self):
        """The other direction, and the reason the branch exists at all."""
        aid, sid = _resolve(
            _source(), rules=_preset("subbed"),
            memory=(self.PREV_WITH_SUBS, JPN_AUDIO, -1))
        self.assertEqual(sid, -1,
                         "subtitles the user switched off came back on")

    def test_a_remembered_subtitle_still_outranks_the_rule(self):
        aid, sid = _resolve(
            _source(), rules=_preset("subbed"),
            memory=(self.PREV_WITH_SUBS, JPN_AUDIO, JPN_SUB))
        self.assertEqual(sid, JPN_SUB)

    def test_an_unmatchable_remembered_subtitle_falls_back_to_the_rule(self):
        """Already true for a real index that ranks nothing, and the case the
        `None` half should have behaved like all along."""
        prev = {"MediaStreams": [
            {"Index": 9, "Type": "Subtitle", "Codec": "pgssub",
             "Language": "fre", "DisplayTitle": "French"}]}
        aid, sid = _resolve(_source(), rules=_preset("subbed"),
                            memory=(prev, None, 9))
        self.assertEqual(sid, ENG_SUB)

    def test_the_source_default_now_reaches_an_episode_after_a_subless_one(self):
        """A deliberate behaviour change, written down because it is the only
        one: with no rule, a previous episode that resolved no subtitle used
        to force -1 and so suppress this episode's own
        DefaultSubtitleStreamIndex. It no longer does, which is what a fresh
        play of this episode would have done and what the audio side has
        always done with DefaultAudioStreamIndex."""
        aid, sid = _resolve(
            _source(DefaultSubtitleStreamIndex=ENG_SUB), rules=None,
            memory=(self.PREV_NO_SUBS, JPN_AUDIO, None))
        self.assertEqual(sid, ENG_SUB)

    def test_but_an_off_the_user_chose_still_suppresses_it(self):
        """The control for the case above: -1 is a decision and outranks the
        source default, so this pair is what keeps the fix from having simply
        deleted the carry-forward."""
        aid, sid = _resolve(
            _source(DefaultSubtitleStreamIndex=ENG_SUB), rules=None,
            memory=(self.PREV_WITH_SUBS, JPN_AUDIO, -1))
        self.assertEqual(sid, -1)

    def test_with_no_rule_nothing_resolved_last_time_means_no_subtitles(self):
        """The `None` case with nothing else to say still ends at "off" --
        via `configure_streams`, which reads None and -1 the same way. The fix
        must not switch subtitles ON for someone who has never had them."""
        aid, sid = _resolve(_source(), rules=None,
                            memory=(self.PREV_NO_SUBS, JPN_AUDIO, None))
        self.assertIsNone(sid)


class TheHeuristicCarriesAPickForwardTest(unittest.TestCase):
    """How a deliberate pick reaches the NEXT episode.

    Not by forwarding `explicit_tracks` -- `Media.get_next` does not, and
    should not. That flag travels with the raw stream index the user picked,
    which indexes into the item it was picked on; applied to the next episode
    it is stale by construction, and stream indices are not stable across
    items (an episode with one more audio track renumbers everything after
    it).

    The memory carries the *choice* instead, and `_rank_stream` re-matches it
    against this item's own streams by language, title, codec and position.
    That is a better statement of what the user meant, so
    `_apply_remembered_tracks` is deliberately the one step of the chain that
    does not check `explicit_tracks`. See docs/track-selection.md section 4.
    """

    #: The previous episode, with the audio streams in the OPPOSITE order --
    #: so an index carried over verbatim lands on the wrong language and only
    #: a re-match by language can get this right.
    PREV_SWAPPED = {"MediaStreams": [
        {"Index": 0, "Type": "Video", "Codec": "h264"},
        {"Index": 1, "Type": "Audio", "Codec": "aac", "Language": "jpn",
         "DisplayTitle": "Japanese"},
        {"Index": 2, "Type": "Audio", "Codec": "aac", "Language": "eng",
         "DisplayTitle": "English"},
    ]}

    def test_the_pick_is_re_matched_not_carried_as_an_index(self):
        # The user picked index 1 last episode, which was Japanese there.
        # Here index 1 is English, and Japanese is 2.
        aid, _sid = _resolve(_source(), rules=None,
                             memory=(self.PREV_SWAPPED, 1, None))
        self.assertEqual(aid, JPN_AUDIO,
                         "the previous item's stream index was applied "
                         "verbatim to a source that numbers them differently")

    def test_it_survives_several_advances(self):
        """Three advances, not one: the memory is re-captured from each item,
        so a re-match that drifts by one position per episode ends up
        somewhere else entirely and a single-step test cannot see it."""
        from jellyfin_mpv_shim.conf import settings

        memory = (self.PREV_SWAPPED, 1, None)
        for _ in range(3):
            v, api = _video(_source())
            pm = _pm(memory=memory)
            with mock.patch.object(settings, "language_config", None), \
                    mock.patch.object(settings, "remember_audio_track", True), \
                    mock.patch.object(settings, "remember_subtitle_track", True):
                pm.play(v)
            self.assertEqual(v.aid, JPN_AUDIO)
            memory = (v.media_source, v.aid, v.sid)


class PrecedenceTest(unittest.TestCase):
    """The order between the four deciders, as one table per stream type.

    Audio and subtitles are resolved independently and the tables differ in
    exactly one row (a remembered "off"), so they are written out separately
    rather than as one table with exceptions.
    """

    PREV = {"MediaStreams": [dict(s) for s in FULL]}

    # (name, kwargs, expected aid)
    AUDIO_CASES = [
        ("an explicit pick stands on the item it was made for",
         dict(explicit=True, aid=ENG_AUDIO, rules=_preset("subbed"),
              memory=(PREV, JPN_AUDIO, None), initial=True), ENG_AUDIO),
        ("an explicit pick stands across a restart",
         dict(explicit=True, aid=ENG_AUDIO, rules=_preset("subbed"),
              memory=(PREV, JPN_AUDIO, None), apply_memory=False), ENG_AUDIO),
        ("memory wins over the rule",
         dict(rules=_preset("subbed"), memory=(PREV, ENG_AUDIO, None)),
         ENG_AUDIO),
        ("memory off leaves the rule",
         dict(rules=_preset("subbed"), memory=(PREV, ENG_AUDIO, None),
              remember_audio=False), JPN_AUDIO),
        ("no memory leaves the rule",
         dict(rules=_preset("subbed")), JPN_AUDIO),
        ("nothing remembered leaves the rule",
         dict(rules=_preset("subbed"), memory=(PREV, None, None)), JPN_AUDIO),
        ("the rule wins over the source default",
         dict(rules=_preset("subbed"),
              source=_source(DefaultAudioStreamIndex=ENG_AUDIO)), JPN_AUDIO),
        ("the source default applies with no rule",
         dict(source=_source(DefaultAudioStreamIndex=JPN_AUDIO)), JPN_AUDIO),
        ("nothing at all posts nothing, and the server chooses",
         dict(), None),
    ]

    SUBTITLE_CASES = [
        ("an explicit pick stands on the item it was made for",
         dict(explicit=True, sid=JPN_SUB, rules=_preset("subbed"),
              memory=(PREV, None, ENG_SUB), initial=True), JPN_SUB),
        ("an explicit pick stands across a restart",
         dict(explicit=True, sid=JPN_SUB, rules=_preset("subbed"),
              memory=(PREV, None, ENG_SUB), apply_memory=False), JPN_SUB),
        ("an explicit off wins over everything",
         dict(explicit=True, sid=-1, rules=_preset("subbed")), -1),
        ("memory wins over the rule",
         dict(rules=_preset("subbed"), memory=(PREV, None, JPN_SUB)), JPN_SUB),
        ("a remembered off wins over the rule",
         dict(rules=_preset("subbed"), memory=(PREV, None, -1)), -1),
        ("memory off leaves the rule",
         dict(rules=_preset("subbed"), memory=(PREV, None, -1),
              remember_sub=False), ENG_SUB),
        ("nothing remembered leaves the rule",
         dict(rules=_preset("subbed"), memory=(PREV, None, None)), ENG_SUB),
        ("the rule wins over the source default",
         dict(rules=_preset("subbed"),
              source=_source(DefaultSubtitleStreamIndex=JPN_SUB)), ENG_SUB),
        ("the source default applies with no rule",
         dict(source=_source(DefaultSubtitleStreamIndex=ENG_SUB)), ENG_SUB),
        ("nothing at all posts nothing, and the server chooses",
         dict(), None),
    ]

    def test_audio_precedence(self):
        for name, kwargs, expected in self.AUDIO_CASES:
            with self.subTest(name):
                kwargs = dict(kwargs)
                source = kwargs.pop("source", None) or _source()
                self.assertEqual(_resolve(source, **kwargs)[0], expected)

    def test_subtitle_precedence(self):
        for name, kwargs, expected in self.SUBTITLE_CASES:
            with self.subTest(name):
                kwargs = dict(kwargs)
                source = kwargs.pop("source", None) or _source()
                self.assertEqual(_resolve(source, **kwargs)[1], expected)


class TheTwoImplementationsAgreeTest(unittest.TestCase):
    """`OfflineVideo` reimplements `resolve_tracks_for_negotiation` and
    `map_streams` -- it has no server to negotiate with and no sidecar urls to
    build from a DeliveryUrl. A second implementation of a precedence chain is
    where the precedence stops being the same, so the tables above are run
    against it too.

    This is the same class of gap as the one that made the subtitle bug
    invisible: each implementation had tests, the pair had none.
    """

    def _offline(self, source, rules, memory, remember_sub=True,
                 remember_audio=True, explicit=False, aid=None, sid=None,
                 initial=False, apply_memory=True):
        from jellyfin_mpv_shim.conf import settings
        from jellyfin_mpv_shim.sync.offline_media import OfflineVideo

        v = OfflineVideo.__new__(OfflineVideo)
        v.item_id = "ep1"
        v.item = {"Type": "Episode", "Name": "Ep", "MediaSources": [source]}
        v._source = source
        v.client = None
        v.parent = NS(client=None)
        v.aid, v.sid, v.srcid = aid, sid, None
        v.explicit_tracks = explicit
        v.media_source = None
        v.subtitle_seq = {}
        v.subtitle_uid = {}
        v.subtitle_url = {}
        v.subtitle_enc = set()
        v.audio_seq = {}
        v.audio_uid = {}
        v._subs_dir = "/nonexistent"

        pm = _pm(memory=memory)
        with mock.patch.object(settings, "language_config", rules), \
                mock.patch.object(settings, "remember_audio_track",
                                  remember_audio), \
                mock.patch.object(settings, "remember_subtitle_track",
                                  remember_sub):
            # The order play() runs them in, minus the negotiation there is
            # none of offline. is_initial_play clears the memory; a restart
            # passes apply_memory=False.
            v.resolve_tracks_for_negotiation()
            if memory is not None and apply_memory and not initial:
                pm._apply_remembered_tracks(v)
            v.media_source = dict(source)
            v.map_streams()
        return v.aid, v.sid

    PREV_NO_SUBS = {"MediaStreams": [dict(s) for s in NO_SUBS]}
    PREV = {"MediaStreams": [dict(s) for s in FULL]}

    def test_the_audio_table_holds_offline(self):
        for name, kwargs, expected in PrecedenceTest.AUDIO_CASES:
            with self.subTest(name):
                self.assertEqual(self._case(kwargs)[0], expected)

    def test_the_subtitle_table_holds_offline(self):
        for name, kwargs, expected in PrecedenceTest.SUBTITLE_CASES:
            with self.subTest(name):
                self.assertEqual(self._case(kwargs)[1], expected)

    def _case(self, kwargs):
        """One row of the shared tables, against the offline implementation.

        The class used to run three hand-written cases while its docstring --
        and docs/track-selection.md section 4 -- said it ran the tables. The
        claim was the whole justification for the class existing.
        """
        kwargs = dict(kwargs)
        source = kwargs.pop("source", None) or _source()
        return self._offline(
            source, kwargs.get("rules"), kwargs.get("memory"),
            remember_audio=kwargs.get("remember_audio", True),
            remember_sub=kwargs.get("remember_sub", True),
            explicit=kwargs.get("explicit", False),
            aid=kwargs.get("aid"), sid=kwargs.get("sid"),
            initial=kwargs.get("initial", False),
            apply_memory=kwargs.get("apply_memory", True))

    def test_a_downloaded_episode_keeps_the_language_rule_too(self):
        aid, sid = self._offline(_source(), _preset("subbed"),
                                 (self.PREV_NO_SUBS, JPN_AUDIO, None))
        self.assertEqual((aid, sid), (JPN_AUDIO, ENG_SUB))

    def test_a_downloaded_episode_still_carries_subtitles_off(self):
        aid, sid = self._offline(_source(), _preset("subbed"),
                                 (self.PREV, JPN_AUDIO, -1))
        self.assertEqual(sid, -1)

    def test_memory_still_outranks_the_rule_offline(self):
        aid, sid = self._offline(_source(), _preset("subbed"),
                                 (self.PREV, ENG_AUDIO, JPN_SUB))
        self.assertEqual((aid, sid), (ENG_AUDIO, JPN_SUB))


if __name__ == "__main__":
    unittest.main()
