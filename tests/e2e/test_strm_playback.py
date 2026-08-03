"""Playing a `.strm`, and holding a resume position across it.

`test_strm_source` covers what the shim is *handed* for a stream file. This is
the half that needs a real mpv: the media is fetched from an origin, through
the server, and seeked in — and a resume position is the round trip that has to
survive all of it.

**Most of this is offline.** `LocalOriginPlaybackTest` plays stdjflib's
local-origin stream file (127.0.0.1:8410, served by `stdjflib serve`), which
Jellyfin treats as genuinely remote — `Protocol=Http`, probed over HTTP, direct
play — while depending on nobody.

Two classes still need a host on the internet, and it is a length problem
rather than a preference:

* `StrmResumeTest` needs an item over `MinResumeDurationSeconds` (300). The
  local clips are 30 seconds, so the server would discard any position they
  held and the failure would read as a shim bug. The catalogue fixture is ~11
  minutes.
* `AlternateVersionTest` needs a `.strm` grouped as an alternate version, and
  only the catalogue fixtures have one.

Both skip when their origin is unreachable — somebody else's host being slow is
not a defect in this client. If stdjflib grows a long local clip and a
local-origin version set, both classes move over unchanged.

Everything here uses the stream fixtures only, so it shares no playstate with
any other module in this suite.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402
import _strm  # noqa: E402
from _strm import strm_media  # noqa: E402


@_strm.require_origin(_strm.LOCAL_MOVIE)
@_e2e.require_server_and_mpv
class LocalOriginPlaybackTest(_e2e.E2ETestCase):
    """A stream file plays, and starts where it is told to.

    Thirty seconds, served from loopback. Too short to hold a resume position
    — that is `StrmResumeTest`'s job — but long enough to prove the seek
    itself, which is the part that is remote-specific: with `direct_paths`
    off the shim streams through Jellyfin, which range-requests the origin.
    (stdjflib's origin server answers 206 for exactly this reason; a handler
    that ignored `Range` would let playback start and every seek silently do
    nothing.)
    """

    SEEK_TO = 15.0      # half way into a 30s clip

    def setUp(self):
        super().setUp()
        self.item = self.session.find(_strm.LOCAL_MOVIE, library="Movies")
        self.session.reset_played(self.item["Id"])
        self.addCleanup(self.session.reset_played, self.item["Id"])

    def test_a_stream_file_plays(self):
        video = strm_media(self.session, [self.item["Id"]]).video
        self.assertIsNotNone(video, "Media built no video for a .strm")
        self.pm.play(video, is_initial_play=True)

        self.assertTrue(self.pm._player.duration,
                        "mpv never reported a duration for the stream")
        self.assertFalse(video.is_transcode,
                         "a remote H.264 stream should direct play")
        advanced = self.pump_until(
            lambda: (self.pm._player.playback_time or 0) > 1.0, timeout=45)
        self.assertTrue(advanced, "playback never advanced")

        # The server sees it, under the item we think we are playing.
        self.pm.send_timeline()
        playing = _e2e.wait_for(
            lambda: (self.session.my_session() or {}).get("NowPlayingItem"))
        self.assertTrue(playing, "the server never saw this device playing")
        self.assertEqual(playing["Id"], self.item["Id"])

    def test_playing_from_an_offset_starts_there(self):
        """A position handed to `play` is where playback begins.

        Remote or not, this is a byte-range seek against the server, which
        proxies one to the origin — that is what makes a remote source
        seekable at all.
        """
        video = strm_media(self.session, [self.item["Id"]]).video
        self.pm.play(video, self.SEEK_TO, is_initial_play=True)

        position = self.pm._player.playback_time
        self.assertIsNotNone(position,
                             "mpv reported no position after a resume start")
        self.assertGreater(
            position, self.SEEK_TO - 5,
            "playback started at %.1fs instead of the %.1fs resume point"
            % (position, self.SEEK_TO))
        # ...and it is genuinely playing from there, not parked on one frame.
        advanced = self.pump_until(
            lambda: (self.pm._player.playback_time or 0) > position + 1,
            timeout=45)
        self.assertTrue(advanced,
                        "playback did not advance past the resume point")


@_strm.require_origin(_strm.LONG_MOVIE)
@_e2e.require_server_and_mpv
class StrmResumeTest(_e2e.E2ETestCase):
    """A stream file holds a resume position like any other item.

    It can only do so because the shim asks for `PlaybackInfo` before it
    reports anything, which is what makes the server probe the origin and
    learn a runtime — see `test_strm_source.NoRuntimeResumeTest` for what
    happens to an item whose runtime the server never learns.

    **This needs a long item, and that is the server's rule rather than a
    choice.** Jellyfin discards a resume position for anything shorter than
    `MinResumeDurationSeconds` (300, confirmed against this server's
    `/System/Configuration`) and clamps to `MinResumePct` 5% /
    `MaxResumePct` 90%. `LONG_MOVIE` is stdjflib's 400-second local-origin
    clip, built for exactly this and the only item in the library that can
    hold a position at all; the usable window is 20s–360s.
    """

    SEEK_TO = 200.0     # half way: clear of the 5% floor and the 90% cap

    def setUp(self):
        super().setUp()
        self.item = self.session.find(_strm.LONG_MOVIE, library="Movies")
        self.session.reset_played(self.item["Id"])
        self.addCleanup(self.session.reset_played, self.item["Id"])

    def test_stopping_midway_leaves_a_resume_position(self):
        video = strm_media(self.session, [self.item["Id"]]).video
        self.assertIsNotNone(video, "Media built no video for a .strm")
        self.pm.play(video, is_initial_play=True)
        self.assertTrue(self.pm._player.duration,
                        "mpv never reported a duration for the stream")
        self.assertGreater(
            video.get_duration() or 0, 300,
            "this fixture is too short to hold a resume position at all")

        self.pm.seek(self.SEEK_TO, absolute=True, exact=True)
        arrived = self.pump_until(
            lambda: (self.pm._player.playback_time or 0) >= self.SEEK_TO - 5,
            timeout=60)
        self.assertTrue(arrived, "mpv never reached the seek target")
        self.pm.send_timeline()
        self.pm.stop()

        ticks = _e2e.wait_for(
            lambda: self.session.user_data(self.item["Id"])
            .get("PlaybackPositionTicks"))
        self.assertTrue(ticks, "a .strm recorded no resume position")
        seconds = ticks / 10000000
        self.assertGreater(seconds, self.SEEK_TO - 60,
                           "resume position is well short of where we stopped")
        self.assertLess(seconds, self.SEEK_TO + 60,
                        "resume position is well past where we stopped")
        self.assertFalse(
            self.session.user_data(self.item["Id"]).get("Played"),
            "a .strm stopped in the middle was marked watched")


@_strm.require_origin(_strm.LOCAL_MOVIE)
@_e2e.require_server_and_mpv
class RemoteTrackSelectionTest(_e2e.E2ETestCase):
    """Starting a stream file with the track the library browser picked.

    The browser's detail page resolves the audio and subtitle defaults itself
    so its pickers can show what will actually play, and sends them with the
    play — `explicit_tracks`. That index is a real stream index off the same
    media source, so it can only be honoured if the client mapped that source
    to mpv's numbering.

    It did not, for anything remote: `map_streams` returned before doing any
    of its work unless the source was `Protocol=File`, which was written long
    before stream files and reads as "not a local file, so no real tracks".
    A `.strm` that direct plays is a real container reaching mpv either from
    the origin or proxied through the server, and its tracks line up exactly
    as a local file's do. With the maps empty, starting one with an explicit
    aid raised `KeyError` from the middle of `_play_media` and playback never
    started. Playing the same item from anywhere that sends no track (the
    search results' play button) was unaffected, because the early return
    also skipped the defaulting that would have chosen one.

    The unit half is `tests/test_remote_playback.MapStreamsTest`; this is the
    half that proves the numbering is right against a real probed source.
    """

    def setUp(self):
        super().setUp()
        self.item = self.session.find(_strm.LOCAL_MOVIE, library="Movies")
        source = (self.session.api.get_item(self.item["Id"])
                  .get("MediaSources") or [{}])[0]
        self.aid = source.get("DefaultAudioStreamIndex")
        self.assertIsNotNone(
            self.aid,
            "the fixture's source names no default audio stream, so this "
            "cannot reproduce what the browser sends")

    def test_playing_with_the_browser_s_audio_index_selects_that_track(self):
        from jellyfin_mpv_shim.media import Media
        media = Media(self.session.client, [self.item["Id"]],
                      user_id=self.session.user_id,
                      aid=self.aid, explicit_tracks=True)
        video = media.video
        self.assertIsNotNone(video, "Media built no video for a .strm")

        self.pm.play(video, is_initial_play=True)      # KeyError lived here
        self.assertTrue(self.pm._player.duration,
                        "mpv never opened the stream")
        self.assertFalse(video.is_transcode,
                         "the source transcoded, so mpv's track ids are the "
                         "transcoder's and this measures nothing")

        self.assertEqual(video.aid, self.aid,
                         "the explicit index was not the one played")
        self.assertIn(self.aid, video.audio_seq,
                      "a remote source came back with no audio mapping")
        # And mpv is playing the track that mapping names, not merely some
        # track it opened with.
        self.assertEqual(self.pm._player.aid, video.audio_seq[self.aid])
        selected = next((t for t in self.pm._player.track_list
                         if t["type"] == "audio" and t.get("selected")), None)
        self.assertIsNotNone(selected, "mpv is playing no audio at all")
        self.assertEqual(selected["id"], video.audio_seq[self.aid])

    def test_playing_with_no_track_takes_the_server_s_default(self):
        """Started from anywhere that sends no track -- the search results'
        play button, a queue advancing -- a stream file gets the same track it
        would get from the detail page.

        `map_streams` applies `DefaultAudioStreamIndex` (where the user's
        choice in another client lives) after language_config and before
        anything else, and the early return skipped that too: remote sources
        were left on `aid=None` and whatever mpv happened to open with. The
        two entry points then disagreed about the same item.

        Track memory is turned off for this: it would arrive at the same
        number from the previous item and hide whether the default was read
        at all.
        """
        from jellyfin_mpv_shim.conf import settings
        with mock.patch.object(settings, "remember_audio_track", False):
            video = strm_media(self.session, [self.item["Id"]]).video
            self.pm.play(video, is_initial_play=True)

        self.assertEqual(
            video.aid, self.aid,
            "a stream file started on audio index %r, not the server's "
            "default %r" % (video.aid, self.aid))
        self.assertEqual(self.pm._player.aid, video.audio_seq[self.aid],
                         "mpv is not playing the default track")
        self.pm.send_timeline()
        self.assertTrue(
            _e2e.wait_for(
                lambda: ((self.session.my_session() or {}).get("PlayState")
                         or {}).get("AudioStreamIndex") == self.aid),
            "the server was told a different track was playing")

    def test_a_forced_transcode_leaves_the_track_to_the_server(self):
        """The same source re-encoded: mpv gets one track and the map, which
        is now built for a remote source like any other, must stay unused.

        This is the case the old `Protocol=File` check was thought to be
        guarding. It is `is_transcode` in `configure_streams` that guards it,
        for remote and local alike -- see
        `test_track_selection.TranscodedTrackTest`, which pins the same rule
        on a fixture with six audio tracks to choose between.
        """
        video = strm_media(self.session, [self.item["Id"]]).video
        video.set_trs_override(None, True)
        self.pm.play(video, is_initial_play=True)
        self.assertTrue(video.is_transcode,
                        "the server direct played it, so this measures "
                        "nothing about a transcode")
        self.assertTrue(_e2e.wait_for(lambda: self.pm._player.duration),
                        "mpv never opened the transcoded stream")

        tracks = [t for t in self.pm._player.track_list if t["type"] == "audio"]
        self.assertEqual(len(tracks), 1,
                         "expected one transcoded audio track, got %d"
                         % len(tracks))
        self.assertEqual(self.pm._player.aid, tracks[0]["id"],
                         "the source's stream map was applied to a transcode")
        self.assertTrue(
            self.pump_until(lambda: (self.pm._player.playback_time or 0) > 1.0,
                            timeout=60),
            "the transcoded stream never advanced")


@_strm.require_origin(_strm.VERSIONS)
@_e2e.require_server_and_mpv
class AlternateVersionTest(_e2e.E2ETestCase):
    """A stream file grouped as an alternate version beside real media.

    Measured against 12.0, and worse than it first looks. The stream file in a
    version set is **never probed**: the probe is gated on the *item's* path
    ending in `.strm`, and a version set's `item.Path` is its **primary's** —
    an `.mkv` here — so the shortcut inside it never qualifies, and pinning
    `MediaSourceId` to it does not help. It therefore comes back with no
    `RunTimeTicks` *and* `MediaStreams: []`, and with no codec information it
    can never match a direct-play profile either. Every play of it is a
    transcode of a source whose length the server does not know, so mpv is
    handed an HLS playlist that grows as segments are produced instead of a
    length.

    Meanwhile the *Item* carries a perfectly good runtime — the local
    version's, which is a third of the real stream's.

    The shim used to take that as the duration of what it was playing.
    `_finished_at_eof` then called the file finished almost immediately, the
    queue advanced off it, and the completion that got reported is what
    actually destroyed the resume position. That is why this reaches a user
    as "resume is broken on .strm" rather than as "the duration is wrong".
    See `Video.get_duration`.

    What the shim can fix is its own arithmetic, and that is all this class
    asserts. The rest — a version that always transcodes and cannot be placed
    on a timeline — is the server's, and pinned in `test_strm_source`.
    """

    def setUp(self):
        super().setUp()
        self.item = self.session.find(_strm.VERSIONS, library="Movies")
        self.local_seconds = (self.item.get("RunTimeTicks") or 0) / 10000000
        self.assertTrue(
            self.local_seconds,
            "the version fixture has no runtime on the item, so a borrowed "
            "duration would be indistinguishable from none")
        self.session.reset_played(self.item["Id"])
        self.addCleanup(self.session.reset_played, self.item["Id"])

    def remote_source_id(self):
        probe = _strm.strm_video(self.session, self.item)
        for source in probe.playback_info["MediaSources"]:
            if source.get("Protocol") == "Http":
                return source["Id"]
        raise AssertionError("the version fixture has no remote source")

    def test_the_remote_version_is_not_finished_at_the_local_one_s_length(self):
        media = strm_media(self.session, [self.item["Id"]],
                           srcid=self.remote_source_id())
        video = media.video
        # The source is resolved inside play(), not before it.
        self.pm.play(video, is_initial_play=True)
        self.assertEqual(video.media_source["Protocol"], "Http",
                         "not playing the remote version")

        self.assertIsNone(
            video.get_duration(),
            "the shim adopted a duration for a version the server did not "
            "probe; if that is the local file's %.1fs it will call this "
            "finished %.0f seconds in" % (self.local_seconds,
                                          self.local_seconds))
        # Deliberately no assertion about mpv's duration. This source always
        # transcodes (no MediaStreams to match a profile against), and the
        # server does not know how long it is, so what mpv reports is a
        # growing HLS estimate rather than a length — 28s, then 132s, then
        # 169s, measured. Asserting on it would be asserting on when the
        # sample was taken.

        # Play past the local version's whole length, then some.
        target = self.local_seconds + 8
        played_past = self.pump_until(
            lambda: (self.pm._player.playback_time or 0) > target,
            timeout=90)
        self.assertTrue(played_past,
                        "playback never reached %.1fs" % target)

        # Still on the same item: no phantom end-of-file, no advance.
        self.assertIsNotNone(self.pm._video, "playback stopped by itself")
        self.assertEqual(
            self.pm._video.item_id, self.item["Id"],
            "the queue moved off the remote version at the local version's "
            "length")
        self.assertFalse(
            self.pm._reached_eof,
            "the shim declared end-of-file on a stream still playing")

    def test_a_near_end_finish_is_declined_without_a_duration(self):
        """The judgement underneath the test above, asked directly.

        `_finished_at_eof` is what turns a position into "this is over", and
        the completion it authorises is what wipes a resume position. With no
        duration it must decline — for any position, including ones far past
        the sibling version's length.
        """
        video = _strm.strm_video(self.session, self.item,
                                 srcid=self.remote_source_id())

        for position in (self.local_seconds - 1, self.local_seconds + 1, 600.0):
            self.assertFalse(
                self.pm._finished_at_eof(video, position),
                "a position of %.1fs was called the end of a stream whose "
                "length the shim does not know" % position)


if __name__ == "__main__":
    unittest.main()
