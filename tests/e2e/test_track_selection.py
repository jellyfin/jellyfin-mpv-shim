"""Picking an audio or subtitle track, all the way down to mpv and back up.

`media.map_streams` translates between two numbering schemes that only look
alike: Jellyfin's `MediaStream.Index`, which counts **every** stream in the
file including video and every external sidecar, and mpv's per-type track id,
which counts from 1 within each type and skips nothing it did not load. The
translation is a hand-rolled counter over position-within-type — the index is
never passed through — and getting it wrong reads as "the wrong language
plays" rather than as an error.

Nothing before this exercised it against a real file. A fake source fabricates
`MediaStreams`, so the mapping is checked against the very list that invented
it and comes out right by construction. The numbers only disagree when the
list is ffprobe's:

* **Nine subtitle languages** — the subtitle streams are Jellyfin indices
  2..10 (0 is video, 1 is audio) and mpv sub ids 1..9. Use the index as the
  sid and every track is off by one; nine languages is enough that the
  off-by-one lands on a real track every time rather than failing loudly.
* **External sidecars, four formats** — the four sidecars come back as indices
  **0..3** and the file's own audio as **5**, which mpv calls aid 1. Pass the
  index through and there is no such track.

Both were confirmed by mutation: index-as-id in the audio map fails
`test_the_audio_index_is_not_the_mpv_track_id`, and in the subtitle map fails
nine of the nine language subtests.

So every assertion here is on what mpv **actually selected** — the track's own
language and title, read back off `track-list` — rather than on the number
that was sent. And two of them go the other way: the server has to be told
which track is playing, or every other client shows the wrong one.

**Not covered:** the counter's "do not advance for an external stream" rule.
It only bites on a file that mixes external and embedded streams *of the same
type*, and no fixture here has one — the sidecar file's subtitles are all
external, so the embedded map stays empty either way, and the audio loop never
sees an external stream at all. Mutating that branch away leaves this file
green. It wants a fixture with an embedded subtitle *and* a sidecar.
"""

import os
import sys
import time
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

LIBRARY = "Test Media"
#: How long to let mpv apply a track change before reading `track-list`.
SETTLE = 0.6


class _TrackTest(_e2e.E2ETestCase):
    """A single item from Test Media, playing, with helpers to read mpv."""

    ITEM = None

    def setUp(self):
        super().setUp()
        self.item = self.session.find(self.ITEM, library=LIBRARY)
        media = _e2e.build_media(self.session, [self.item["Id"]])
        self.video = media.video
        self.video.get_playback_url()
        self.assertFalse(
            self.video.is_transcode,
            "%r came back as a transcode, so mpv's track ids are the "
            "*transcoder's* and this test is measuring something else"
            % self.ITEM)
        self.pm.play(self.video, is_initial_play=True)
        self.assertTrue(
            _e2e.wait_for(lambda: self.pm._player.duration),
            "mpv never opened %r" % self.ITEM)

    # -- reading mpv -------------------------------------------------------

    def _mpv_tracks(self, kind):
        return {t["id"]: t for t in self.pm._player.track_list
                if t["type"] == kind}

    def _streams(self, kind):
        return {s["Index"]: s for s in self.video.media_source["MediaStreams"]
                if s["Type"] == kind}

    def _selected(self, kind):
        """The track mpv is actually playing, as mpv describes it."""
        track_id = self.pm._player.aid if kind == "audio" else self.pm._player.sid
        if track_id in (False, None, "no"):
            return None
        return self._mpv_tracks(kind).get(track_id)


@_e2e.require_server_and_mpv
class AudioTrackTest(_TrackTest):
    """Six audio tracks, five languages, one of them undetermined."""

    ITEM = "Six audio tracks"

    def test_every_audio_track_selects_the_one_it_names(self):
        streams = self._streams("Audio")
        self.assertGreaterEqual(len(streams), 6,
                                "this fixture is meant to have six audio "
                                "tracks; got %d" % len(streams))
        for uid, stream in sorted(streams.items()):
            with self.subTest(index=uid, title=stream.get("Title")):
                self.pm.set_streams(uid, None)
                time.sleep(SETTLE)
                track = self._selected("audio")
                self.assertIsNotNone(
                    track, "selecting audio stream %d left mpv playing no "
                    "audio at all" % uid)
                # Language and title together: language alone would let the
                # two English tracks (5.1 and commentary) pass for each other,
                # which is precisely the off-by-one this exists to catch.
                self.assertEqual(
                    (track.get("lang"), track.get("title")),
                    (stream.get("Language"), stream.get("Title")),
                    "asked for Jellyfin audio index %d (%s / %s) and mpv is "
                    "playing %s / %s" % (
                        uid, stream.get("Language"), stream.get("Title"),
                        track.get("lang"), track.get("title")))

    def test_the_servers_default_track_is_the_one_that_starts(self):
        """`DefaultAudioStreamIndex` is where the user's choice in another
        client lives. Ignoring it plays the wrong language from the first
        frame, with nothing on screen to explain why."""
        default = self.video.media_source.get("DefaultAudioStreamIndex")
        if default is None:
            self.skipTest("the server named no default audio stream")
        self.assertEqual(
            self.video.aid, default,
            "playback started on audio index %s, not the server's default %s"
            % (self.video.aid, default))
        track = self._selected("audio")
        stream = self._streams("Audio")[default]
        self.assertEqual((track.get("lang"), track.get("title")),
                         (stream.get("Language"), stream.get("Title")))

    def test_the_server_is_told_which_track_is_playing(self):
        """The round trip. Other clients read `AudioStreamIndex` off the
        session to show what is playing, and the resume path reads it back —
        so a track change that never reaches the server is invisible outside
        this window."""
        streams = sorted(self._streams("Audio"))
        pick = streams[-1]
        self.pm.set_streams(pick, None)
        self.pm.send_timeline()
        reported = _e2e.wait_for(
            lambda: ((self.session.my_session() or {}).get("PlayState") or {})
            .get("AudioStreamIndex") == pick)
        self.assertTrue(
            reported,
            "after switching to audio index %d the server still reports %r"
            % (pick, ((self.session.my_session() or {}).get("PlayState")
                      or {}).get("AudioStreamIndex")))


@_e2e.require_server_and_mpv
class EmbeddedSubtitleTest(_TrackTest):
    """Nine embedded subtitle languages — where the numbering diverges."""

    ITEM = "Nine subtitle languages"

    def test_the_index_offset_is_real(self):
        """A guard on the premise. If the fixture ever grows a stream layout
        where index and sid happen to coincide, every assertion below would
        pass with the mapping deleted and this file would be decorative."""
        subs = sorted(self._streams("Subtitle"))
        self.assertTrue(subs, "no subtitle streams")
        self.assertNotEqual(
            subs, sorted(self._mpv_tracks("sub")),
            "Jellyfin's subtitle indices %s are the same numbers as mpv's sub "
            "ids, so this fixture can no longer tell a correct mapping from "
            "no mapping at all" % subs)

    def test_every_subtitle_selects_the_language_it_names(self):
        streams = self._streams("Subtitle")
        self.assertGreaterEqual(len(streams), 9,
                                "expected nine subtitle languages, got %d"
                                % len(streams))
        for uid, stream in sorted(streams.items()):
            with self.subTest(index=uid, lang=stream.get("Language")):
                self.pm.set_streams(None, uid)
                time.sleep(SETTLE)
                track = self._selected("sub")
                self.assertIsNotNone(
                    track, "selecting subtitle stream %d turned subtitles off"
                    % uid)
                self.assertEqual(
                    track.get("lang"), stream.get("Language"),
                    "asked for Jellyfin subtitle index %d (%s) and mpv is "
                    "showing %s" % (uid, stream.get("Language"),
                                    track.get("lang")))

    def test_minus_one_turns_subtitles_off(self):
        """-1 is Jellyfin's "no subtitle", and it is not an index — reaching
        the map with it would either KeyError or select track -1."""
        first = sorted(self._streams("Subtitle"))[0]
        self.pm.set_streams(None, first)
        time.sleep(SETTLE)
        self.assertIsNotNone(self._selected("sub"))
        self.pm.set_streams(None, -1)
        time.sleep(SETTLE)
        self.assertIsNone(self._selected("sub"),
                          "subtitles are still on after selecting 'none'")


@_e2e.require_server_and_mpv
class ExternalSubtitleTest(_TrackTest):
    """Four sidecars in four formats, which mpv has to be *given*.

    An external subtitle is not in the file, so there is no track to select
    until `sub_add` fetches one from the server. That makes three things
    testable that embedded subtitles cannot show: the fetch happens, it is
    remembered so that returning to a track does not fetch it again, and the
    URL it fetches is one that carries our access token — which is why
    `foreign_subtitle_hosts` exists.
    """

    ITEM = "External sidecars, four formats"

    def test_the_audio_index_is_not_the_mpv_track_id(self):
        """The clearest case of the two schemes disagreeing.

        The sidecars occupy Jellyfin indices 0..3 and the video is 4, so the
        file's single audio track is index **5** — while mpv, which knows
        nothing of the sidecars, calls that same track aid 1. Pass the index
        through and mpv is asked for a track that does not exist.
        """
        audio = self._streams("Audio")
        self.assertEqual(len(audio), 1, "expected a single audio track")
        uid = next(iter(audio))
        self.assertGreater(
            uid, 1, "the fixture's audio stream is index %d, so index and "
            "track id coincide and this file no longer tells a correct "
            "mapping from no mapping at all" % uid)
        self.pm.set_streams(uid, None)
        time.sleep(SETTLE)
        track = self._selected("audio")
        self.assertIsNotNone(
            track, "Jellyfin audio index %d selected no mpv track — the "
            "index was passed through as a track id" % uid)
        self.assertEqual(track.get("lang"), audio[uid].get("Language"))

    def test_each_sidecar_is_fetched_and_shown(self):
        streams = self._streams("Subtitle")
        self.assertGreaterEqual(len(streams), 4,
                                "expected four sidecars, got %d" % len(streams))
        for uid, stream in sorted(streams.items()):
            with self.subTest(index=uid, codec=stream.get("Codec")):
                self.assertEqual(
                    stream.get("DeliveryMethod"), "External",
                    "this fixture's subtitle %d is no longer delivered as an "
                    "external file" % uid)
                self.pm.set_streams(None, uid)
                time.sleep(SETTLE)
                track = self._selected("sub")
                self.assertIsNotNone(
                    track, "sidecar %d (%s) was never loaded into mpv"
                    % (uid, stream.get("Codec")))
                self.assertTrue(
                    track.get("external"),
                    "sidecar %d selected an embedded track instead" % uid)
                self.assertIn(
                    uid, self.pm.external_subtitles,
                    "sidecar %d is shown but not remembered, so returning to "
                    "it would fetch it again" % uid)

    def test_returning_to_a_sidecar_does_not_fetch_it_twice(self):
        """`external_subtitles` exists for this. Without it every visit adds
        another copy of the same track to mpv's list, and the subtitle menu
        grows a duplicate each time you switch back."""
        uids = sorted(self._streams("Subtitle"))
        first, second = uids[0], uids[1]
        self.pm.set_streams(None, first)
        time.sleep(SETTLE)
        was = self.pm._player.sid
        self.pm.set_streams(None, second)
        time.sleep(SETTLE)
        before = len(self._mpv_tracks("sub"))
        self.pm.set_streams(None, first)
        time.sleep(SETTLE)
        self.assertEqual(
            len(self._mpv_tracks("sub")), before,
            "going back to a sidecar added another copy of it to mpv")
        self.assertEqual(self.pm._player.sid, was,
                         "going back to a sidecar landed on a different track")

    def test_the_sidecar_urls_are_ours(self):
        """`--http-header-fields` is global to mpv, so a sidecar hosted
        elsewhere would receive our access token. The pre-check is on the
        item, before PlaybackInfo, because the header is set before the
        stream URL exists."""
        self.assertEqual(
            self.video.foreign_subtitle_hosts(), set(),
            "this fixture is meant to host all four sidecars on the server "
            "under test")
        host = _e2e.SERVER.split("//")[-1].split("/")[0]
        for uid, url in self.video.subtitle_url.items():
            with self.subTest(index=uid):
                self.assertIn(
                    host, url,
                    "sidecar %d would be fetched from somewhere other than "
                    "the server: %s" % (uid, url))

    def test_the_server_is_told_which_subtitle_is_showing(self):
        pick = sorted(self._streams("Subtitle"))[-1]
        self.pm.set_streams(None, pick)
        self.pm.send_timeline()
        reported = _e2e.wait_for(
            lambda: ((self.session.my_session() or {}).get("PlayState") or {})
            .get("SubtitleStreamIndex") == pick)
        self.assertTrue(
            reported,
            "after switching to subtitle index %d the server still reports %r"
            % (pick, ((self.session.my_session() or {}).get("PlayState")
                      or {}).get("SubtitleStreamIndex")))


@_e2e.require_server_and_mpv
class NoAudioTrackTest(_TrackTest):
    """A video with no audio at all: the maps are empty and nothing may
    assume otherwise. `configure_streams` indexes `audio_seq[audio_uid]`
    directly, so a stale aid carried in from the previous item is a KeyError
    rather than a shrug."""

    ITEM = "Video with no audio track"

    def test_playing_a_silent_file_selects_no_audio(self):
        self.assertEqual(self._streams("Audio"), {},
                         "this fixture is supposed to have no audio track")
        self.assertEqual(self.video.audio_seq, {})
        self.assertIsNone(self._selected("audio"))
        # And it is genuinely playing, rather than having failed to open.
        self.assertTrue(self.pm._player.duration)

    def test_nothing_invents_a_track_for_it(self):
        """No audio streams, so no default to adopt and nothing to select.

        The defaulting `map_streams` does -- language_config, then the
        server's `DefaultAudioStreamIndex` -- has to come out empty-handed
        here rather than settling on some index that would then be reported
        to the server as playing.
        """
        self.assertIsNone(self.video.aid)
        self.assertFalse([t for t in self.pm._player.track_list
                          if t["type"] == "audio"],
                         "mpv found an audio track in a silent file")

    def test_a_stale_audio_index_does_not_abort_the_start(self):
        """The shrug this class's docstring asks for.

        An aid can arrive from outside the file entirely: the previous item's
        remembered track, or a browser page that resolved a default against a
        media source since swapped. Nothing about a silent film makes that
        index mappable, and raising from the middle of `_play_media` leaves
        playback half-started -- mpv's own (absent) audio track is the answer.
        """
        from jellyfin_mpv_shim.media import Media
        media = Media(self.session.client, [self.item["Id"]],
                      user_id=self.session.user_id,
                      aid=3, explicit_tracks=True)
        self.pm.play(media.video, is_initial_play=True)     # used to KeyError
        self.assertTrue(self.pm._player.duration,
                        "a stale audio index stopped the file from opening")
        self.assertIsNone(self._selected("audio"))


@_e2e.require_server_and_mpv
class TranscodedTrackTest(_e2e.E2ETestCase):
    """Choosing an audio track on a stream the server is re-encoding.

    A transcode carries **one** re-encoded audio track, so mpv's ids are the
    *transcoder's* and the source's stream map does not describe what mpv
    opened. The choice is made server-side instead: the index goes out with
    `PlaybackInfo` and comes back in the `TranscodingUrl`.

    What keeps that straight is the `is_transcode` gate in
    `configure_streams`, and it is worth being explicit about which guard does
    what. `map_streams` also used to return early for anything but
    `Protocol=File`, which reads like transcode protection and never was one:
    the commonest transcode there is -- a local file the server re-encodes --
    is `Protocol=File`, so that map has always been built and never applied.
    Delete the `is_transcode` gate and `test_mpv_is_not_told_to_switch_tracks`
    fails; the protocol check could not have caught it.
    """

    ITEM = "Six audio tracks"

    def setUp(self):
        super().setUp()
        self.item = self.session.find(self.ITEM, library=LIBRARY)
        streams = (self.session.api.get_item(self.item["Id"])
                   ["MediaSources"][0]["MediaStreams"])
        audio = [s for s in streams if s["Type"] == "Audio"]
        self.assertGreater(
            len(audio), 1,
            "this needs a fixture with more than one audio track, or asking "
            "for a particular one proves nothing")
        # The last one: never the server's default, so a client that quietly
        # ignored the request would come back with a different number.
        self.pick = audio[-1]

    def play_transcoded(self, aid):
        """Play the fixture with `aid`, forced through the transcoder.

        `set_trs_override(None, True)` is what the player's own
        force-transcode retry does, so this is the app's path rather than a
        hand-built URL.
        """
        from jellyfin_mpv_shim.media import Media
        media = Media(self.session.client, [self.item["Id"]],
                      user_id=self.session.user_id,
                      aid=aid, explicit_tracks=True)
        video = media.video
        video.set_trs_override(None, True)
        self.pm.play(video, is_initial_play=True)
        self.assertTrue(video.is_transcode,
                        "the server direct played it, so nothing here is "
                        "about a transcode")
        self.assertTrue(_e2e.wait_for(lambda: self.pm._player.duration),
                        "mpv never opened the transcoded stream")
        return video

    def test_the_requested_track_is_the_one_the_server_transcodes(self):
        video = self.play_transcoded(self.pick["Index"])
        query = urllib.parse.parse_qs(urllib.parse.urlparse(
            video.media_source.get("TranscodingUrl") or "").query)
        self.assertEqual(
            query.get("AudioStreamIndex"), [str(self.pick["Index"])],
            "asked to transcode audio index %d (%s / %s) and the server is "
            "sending %r" % (self.pick["Index"], self.pick.get("Language"),
                            self.pick.get("Title"),
                            query.get("AudioStreamIndex")))

    def test_mpv_is_not_told_to_switch_tracks(self):
        """The map exists and must stay unused.

        `audio_seq` maps index 6 to mpv track 6; the transcode has exactly one
        audio track, so applying it asks for a track that is not there.
        """
        video = self.play_transcoded(self.pick["Index"])
        self.assertIn(self.pick["Index"], video.audio_seq,
                      "the source's map is missing, so this cannot show that "
                      "the map is deliberately not applied")
        tracks = [t for t in self.pm._player.track_list if t["type"] == "audio"]
        self.assertEqual(len(tracks), 1,
                         "a transcode should carry one audio track, got %d"
                         % len(tracks))
        self.assertEqual(
            self.pm._player.aid, tracks[0]["id"],
            "mpv is playing audio track %r, which is not the only track it "
            "has (%r) -- the source's stream map was applied to a transcode"
            % (self.pm._player.aid, tracks[0]["id"]))
        # ...and it is genuinely running, not stalled on a track it cannot find.
        self.assertTrue(
            self.pump_until(lambda: (self.pm._player.playback_time or 0) > 1.0),
            "the transcode never advanced")

    def test_the_server_is_told_which_track_is_playing(self):
        """Other clients read `AudioStreamIndex` off the session. For a
        transcode it is the only description of what is playing that exists,
        since the stream itself no longer carries the track's identity."""
        self.play_transcoded(self.pick["Index"])
        self.pm.send_timeline()
        reported = _e2e.wait_for(
            lambda: ((self.session.my_session() or {}).get("PlayState") or {})
            .get("AudioStreamIndex") == self.pick["Index"])
        self.assertTrue(
            reported,
            "the server reports audio index %r for a transcode of index %d"
            % (((self.session.my_session() or {}).get("PlayState") or {})
               .get("AudioStreamIndex"), self.pick["Index"]))


if __name__ == "__main__":
    unittest.main()
