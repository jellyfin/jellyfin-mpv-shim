"""`.strm` shortcuts: where the source is, where the runtime is, and what a
client is handed when the server refuses one.

A `.strm` is a line of text where a media file would be. Jellyfin resolves the
*item* from the path exactly as it would an `.mkv` — same type, same NFO, same
artwork — and then plays a URL instead of the file. That split is the whole
reason these need a server to test: everything the shim reasons about looks
ordinary, and the media source underneath is remote.

**The runtime is the load-bearing difference, and it is why this module
exists.** A library scan never probes a shortcut (`FFProbeVideoInfo` gates on
`!IsShortcut || EnableRemoteContentProbe`), so a freshly scanned `.strm` item
carries no `RunTimeTicks` at all. The server learns it from the probe it runs
during the `PlaybackInfo` request, and that runtime lands on the **MediaSource**
— which is why `Video.get_duration` reads the source before the item, and why
reading only the item once left every `.strm` with no duration.

That is not a cosmetic gap. Jellyfin's `UserDataManager.UpdatePlayState` has a
branch for an item whose runtime it does not know:

    else if (!hasRuntime)
    {
        // If we don't know the runtime we'll just have to assume it was fully played
        data.Played = playedToCompletion = true;
        positionTicks = 0;
    }

So *one* progress report against a runtime-less item marks it watched and
throws the position away. `NoRuntimeResumeTest` pins that, because it is the
mechanism behind every "resume does not work on my .strm" report: it is not the
position being sent wrongly, it is the server declining to keep one.

Most of this module runs against stdjflib's **local-origin** stream files
(127.0.0.1:8410, served by `stdjflib serve`), which are remote to Jellyfin in
every way that matters here and cost nothing. What genuinely needs a host on
the internet is marked with `require_origin` and skips without one — see
`_strm`.

Contract tier — nothing here imports `player.py`.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402
import _strm  # noqa: E402
from _strm import strm_video  # noqa: E402

SERVER_HOST = (_e2e.SERVER.split("//")[-1].split("/")[0]
               if _e2e.SERVER else "")

#: The two URLs in the commented fixture that the parser must never settle on:
#: one inside the `#` header, one on the line below the real URL. Both are
#: `.invalid`, so a client that read either would be reaching for a hostname
#: the DNS RFC guarantees does not resolve.
DECOYS = ("decoy-in-a-comment", "second-url-never-read")


@_strm.require_origin(_strm.LOCAL_MOVIE)
@_e2e.require_server
class StrmSourceShapeTest(_e2e.E2ETestCase):
    """What the shim is handed for a stream file that resolves cleanly.

    Local origin: the questions here are about the *shape* of a remote source,
    and a loopback host answers all of them without depending on anybody.
    """

    def test_the_source_is_remote_and_names_the_origin(self):
        item = self.session.find(_strm.LOCAL_MOVIE, library="Movies")
        video = strm_video(self.session, item)
        source = video.media_source

        self.assertEqual(source.get("Protocol"), "Http",
                         "a .strm's media source should be remote")
        # The Path is the *origin*, not the stream file on disk. A client that
        # saw the .strm path here would be about to hand mpv a line of text.
        self.assertTrue(source.get("Path", "").startswith("http"),
                        "media source path is not a URL: %r"
                        % source.get("Path"))
        self.assertFalse(source.get("Path", "").endswith(".strm"),
                         "the media source is still the stream file itself")
        self.assertTrue(source.get("SupportsDirectPlay"))
        self.assertTrue(source.get("SupportsDirectStream"))

    def test_the_runtime_arrives_on_the_media_source(self):
        """`get_duration` finds it, wherever the server chose to put it.

        Deliberately **not** asserted as "the item DTO has no runtime": the
        probe that fills the MediaSource also persists the runtime onto the
        item, so that assertion passes on a freshly scanned library and fails
        for ever afterwards. A test that only holds until something has played
        once is worse than no test. What is stable — and what the shim
        depends on — is that the source carries it and `get_duration` finds
        it either way.
        """
        item = self.session.find(_strm.LOCAL_MOVIE, library="Movies")
        video = strm_video(self.session, item)

        source_ticks = video.media_source.get("RunTimeTicks")
        self.assertTrue(source_ticks,
                        "PlaybackInfo returned no runtime for a .strm; the "
                        "server's remote probe did not answer")
        self.assertAlmostEqual(video.get_duration(), source_ticks / 10000000,
                               places=3)

    def test_playback_goes_through_the_server_not_the_origin(self):
        """With `direct_paths` off (the default) the shim streams the origin
        *through* Jellyfin, which is what makes a remote source seekable with
        the same byte ranges as a local one."""
        item = self.session.find(_strm.LOCAL_MOVIE, library="Movies")
        video = strm_video(self.session, item)
        url = video.get_playback_url()
        origin = video.media_source.get("Path", "")

        self.assertIn(SERVER_HOST, url,
                      "playback URL does not point at the server under test")
        self.assertNotIn("8410", url,
                         "the shim handed mpv the origin directly while "
                         "direct paths are off (origin is %s)" % origin)
        self.assertFalse(video.is_transcode,
                         "a remote H.264 stream should direct play")

    def test_a_strm_episode_is_an_ordinary_episode(self):
        """The extension decides the item type; the target does not. An
        episode built from a stream file still carries its season and episode
        numbers, so the queue and the now-playing title work unchanged."""
        episode = _strm.local_episode(self.session)
        self.assertEqual(episode["Type"], "Episode")
        self.assertEqual(episode.get("ParentIndexNumber"), 1)

        video = strm_video(self.session, episode)
        self.assertTrue(video.is_tv)
        self.assertEqual(video.media_source.get("Protocol"), "Http")


@_strm.require_origin(_strm.COMMENTED)
@_e2e.require_server
class StrmParsingTest(_e2e.E2ETestCase):
    """The file format, read back off the resolved source.

    Catalogue origin, because the commented fixture is the one built out of
    exactly what `FetchShortcutInfo` tolerates and it lives on archive.org.
    """

    def test_only_the_first_url_in_the_file_is_read(self):
        """A `.strm` is one source, not a playlist.

        The fixture carries a decoy URL inside its `#` header and a second one
        below the real line. Anything reading the file itself — rather than
        taking the server's resolved source — lands on one of them.
        """
        item = self.session.find(_strm.COMMENTED, library="Movies")
        video = strm_video(self.session, item)

        path = video.media_source.get("Path", "")
        for decoy in DECOYS:
            self.assertNotIn(decoy, path,
                             "the source resolved to a decoy URL: %r" % path)
        # One source, not three.
        self.assertEqual(len(video.playback_info["MediaSources"]), 1,
                         "a .strm produced more than one media source")


@_strm.require_origin(_strm.VERSIONS)
@_e2e.require_server
class StrmVersionTest(_e2e.E2ETestCase):
    """A stream file grouped as an alternate version beside real media.

    Multi-version grouping compares filenames without their extensions, so a
    `.strm` sits in a version set next to an `.mkv` — and switching version
    switches between a local file and a URL. Source selection has to survive
    that, because the two sources disagree about almost everything, runtime
    included.

    Catalogue origin: this is the only fixture with a remote alternate.
    """

    def sources(self, video):
        return {s["Protocol"]: s for s in video.playback_info["MediaSources"]}

    def test_a_stream_file_is_a_version_beside_a_local_file(self):
        item = self.session.find(_strm.VERSIONS, library="Movies")
        video = strm_video(self.session, item)
        by_protocol = self.sources(video)

        self.assertEqual(set(by_protocol), {"File", "Http"},
                         "expected one local and one remote version, got %r"
                         % sorted(by_protocol))

    def test_a_stream_file_in_a_version_set_is_never_probed(self):
        """Why the duration below is missing, stated as its own fact.

        **The probe is gated on the item's path, not on the source.**
        `item.Path.EndsWith(".strm")` decides it, and a version set's
        `item.Path` is its *primary's* — an `.mkv` here — so the shortcut
        inside the set never qualifies, and naming it with `MediaSourceId`
        does not change the answer. A loose `.strm`, whose own path is the
        stream file, comes back probed; that is the contrast
        `StrmSourceShapeTest` draws.

        The missing `MediaStreams` matter as much as the missing runtime:
        with no codec information the source cannot match a direct-play
        profile, so every play of it is a transcode of a source whose length
        the server does not know, which is also why mpv is handed a growing
        HLS duration rather than a real one.

        Pinned because it is the root of everything the alternate-version
        tests work around — a server that probed these would make all of it
        go away, and that is worth finding out from a test rather than from
        a user.
        """
        item = self.session.find(_strm.VERSIONS, library="Movies")
        video = strm_video(self.session, item)
        remote = self.sources(video)["Http"]

        self.assertFalse(remote.get("RunTimeTicks"),
                         "the server has started probing stream files inside "
                         "version sets")
        self.assertFalse(remote.get("MediaStreams"),
                         "the alternate version now carries stream details; "
                         "it may direct play, and the workarounds built on "
                         "it not doing so should be revisited")

    def test_the_remote_version_can_be_asked_for_by_id(self):
        """`srcid` is how the detail page's version picker names a source."""
        item = self.session.find(_strm.VERSIONS, library="Movies")
        remote = self.sources(strm_video(self.session, item))["Http"]

        chosen = strm_video(self.session, item, srcid=remote["Id"])
        self.assertEqual(chosen.media_source["Id"], remote["Id"],
                         "asking for the remote version got a different one")
        self.assertEqual(chosen.media_source["Protocol"], "Http")

    def test_the_remote_version_never_borrows_the_local_one_s_duration(self):
        """The bug this module was written to find.

        The server hands the shim a wrong answer and an absent one: the
        alternate has no runtime (above), while the *Item* carries a
        perfectly good one — the local version's twelve seconds, against a
        ten-minute stream. The shim used to take the wrong one.

        Everything downstream then misfired at once: `_finished_at_eof`
        called the file finished twelve seconds in, the queue advanced off
        it, and the completion that got reported is what actually destroyed
        the resume position. That last step is why this reaches a user as
        "resume is broken on .strm" rather than as "the duration is wrong".

        No duration is the correct answer, and the consumers are built for
        it: both `_finished_at_eof` and `_check_stalled_finish` decline to
        place a position they cannot measure, which costs the near-end
        rescue and keeps every real end-of-file (mpv's own) working.
        """
        item = self.session.find(_strm.VERSIONS, library="Movies")
        remote = self.sources(strm_video(self.session, item))["Http"]
        chosen = strm_video(self.session, item, srcid=remote["Id"])

        local_seconds = (item.get("RunTimeTicks") or 0) / 10000000
        self.assertTrue(local_seconds,
                        "the local version has no runtime either, so this "
                        "test cannot tell a borrowed duration from none")
        self.assertNotAlmostEqual(
            chosen.get_duration() or 0.0, local_seconds, places=2,
            msg="the remote version is reporting the LOCAL file's duration "
                "(%.2fs); playing it will be called finished %.0f seconds in "
                "and the resume position wiped" % (local_seconds,
                                                   local_seconds))
        # Belt: if the server ever does probe alternates, this becomes a real
        # duration and the assertion above still holds — so accept either,
        # and say which happened rather than pretending to know.
        duration = chosen.get_duration()
        if duration is not None:
            self.assertGreater(
                duration, local_seconds,
                "the server started probing alternates, but returned "
                "something shorter than the local version")

    def test_an_episode_can_have_a_stream_version_too(self):
        """The same shape in the episode spelling, which resolves through a
        different resolver (`EpisodeResolver`, not `MovieResolver`)."""
        eps = self.session.episodes(_strm.STRM_SHOW, season=1)
        both = [e for e in eps if e["Name"] == "Something Happens"][0]
        video = strm_video(self.session, both)
        self.assertEqual(set(self.sources(video)), {"File", "Http"})


@_strm.require_origin(_strm.PROBED_VERSIONS)
@_e2e.require_server
class ProbedVersionSetTest(_e2e.E2ETestCase):
    """A version set whose sources *both* carry a runtime.

    `StrmVersionTest` covers the set that goes unprobed, and every assertion
    it can make is about an **absence** — the shim must not borrow a number
    it was not given. That leaves the opposite half untested: when a source
    does carry its own runtime, the shim has to prefer it over the Item's.

    This fixture is what makes that askable. Naming the `.strm` exactly like
    its folder puts it in the *primary* slot, so `item.Path` ends in `.strm`,
    the probe gate fires, and both sources come back measured: a 30s remote
    primary and a 20s local alternate, against an Item that reports the
    primary's 30s. Three distinct numbers, so an assertion cannot pass by
    coincidence.
    """

    def sources(self, video):
        return {s["Protocol"]: s for s in video.playback_info["MediaSources"]}

    def test_a_stream_file_can_hold_the_primary_slot(self):
        """The escape hatch from the probe gate, pinned.

        Multi-version grouping picks the file named like the folder as
        primary. Make that the `.strm` and `item.Path` ends in `.strm`, which
        is the whole of what `MediaSourceManager.GetPlaybackMediaSources`
        tests — so the shortcut is probed and the set comes back complete.
        """
        item = self.session.find(_strm.PROBED_VERSIONS, library="Movies")
        video = _strm.strm_video(self.session, item)
        by_protocol = self.sources(video)
        self.assertEqual(set(by_protocol), {"Http", "File"})

        remote = by_protocol["Http"]
        self.assertEqual(
            remote["Id"], item["Id"],
            "the stream file is not the primary source here, so this fixture "
            "has degraded into a copy of the unprobed one")
        self.assertTrue(remote.get("RunTimeTicks"),
                        "the primary .strm was not probed")
        self.assertTrue(remote.get("MediaStreams"),
                        "the primary .strm came back with no stream details")

    def test_an_alternate_uses_its_own_runtime_not_the_item_s(self):
        """The assertion the unprobed set cannot make.

        The Item reports the primary's length. Playing the *alternate* must
        report the alternate's — a duration read off the Item would be a
        different, plausible number, which is exactly how the original bug
        stayed invisible.
        """
        item = self.session.find(_strm.PROBED_VERSIONS, library="Movies")
        local = self.sources(_strm.strm_video(self.session, item))["File"]

        chosen = _strm.strm_video(self.session, item, srcid=local["Id"])
        self.assertEqual(chosen.media_source["Id"], local["Id"])

        item_seconds = (item.get("RunTimeTicks") or 0) / 10000000
        source_seconds = local["RunTimeTicks"] / 10000000
        self.assertNotAlmostEqual(
            item_seconds, source_seconds, places=2,
            msg="both versions are the same length, so this test cannot tell "
                "which number the shim used")
        self.assertAlmostEqual(
            chosen.get_duration(), source_seconds, places=3,
            msg="the shim reported %.2fs for a %.2fs version; the Item says "
                "%.2fs, which is where that came from"
                % (chosen.get_duration() or -1, source_seconds, item_seconds))


@_e2e.require_server
class RefusedStrmTest(_e2e.E2ETestCase):
    """The two fixtures the server declines, and what a client gets instead.

    Neither reaches anybody: the rtsp one names a loopback port with nothing
    behind it, and the local-path one names a file. Pressing play on either
    fetches nothing from anyone, which is why this class needs no network gate
    at all.
    """

    def test_an_rtsp_strm_keeps_its_protocol(self):
        """rtsp is one of the four schemes `FetchShortcutInfo` accepts, so
        this resolves — to a source with a protocol a client that assumed HTTP
        has never had to render. It offers neither direct play nor direct
        stream, so the shim falls through to the transcode URL rather than
        handing mpv something it cannot open."""
        item = self.session.find(_strm.RTSP, library="Movies")
        video = strm_video(self.session, item)
        source = video.media_source

        self.assertEqual(source.get("Protocol"), "Rtsp")
        self.assertFalse(source.get("SupportsDirectPlay"))
        self.assertFalse(source.get("SupportsDirectStream"))
        # A URL is still produced — the failure belongs to playback, where it
        # is reported, not to URL resolution, where it would be silent.
        url = video.get_playback_url()
        self.assertTrue(url, "no URL at all for an rtsp source")
        self.assertTrue(video.is_transcode)

    def test_a_strm_naming_a_local_path_resolves_to_the_stream_file(self):
        """Refused twice over — in the parser and again in
        `BaseItem.GetVersionInfo` — because honouring it would make a stream
        file a way to read any file on the server.

        What it resolves to is the awkward part, and the reason this is
        pinned: not an item with no source, but an item whose only source is
        the `.strm` **itself**, protocol File. Everything about the item looks
        playable and what a client would be asked to open is a line of text.
        """
        item = self.session.find(_strm.LOCAL_PATH, library="Movies")
        video = strm_video(self.session, item)
        source = video.media_source

        self.assertEqual(source.get("Protocol"), "File")
        self.assertTrue(source.get("Path", "").endswith(".strm"),
                        "expected the stream file itself, got %r"
                        % source.get("Path"))
        self.assertIsNone(video.get_duration(),
                          "a refused shortcut should have no duration")

    def test_an_audio_strm_never_becomes_remote(self):
        """`GetVersionInfo` does the shortcut substitution inside
        `item as Video`, with no branch for Audio — so a music `.strm`
        resolves as a track whose media source stays the text file on disk.

        Pinned rather than worked around: the shim cannot make this play, and
        if a future server grows the missing branch this test is how we find
        out rather than discovering it from a bug report.
        """
        album = self.session.find("Remote Sessions (2025)", library="Music",
                                  item_type="MusicAlbum")
        tracks = self.session.find_all(parent_id=album["Id"],
                                       item_type="Audio")
        self.assertTrue(tracks, "the Remote Sessions album has no tracks")
        video = strm_video(self.session, tracks[0])

        self.assertEqual(video.media_source.get("Protocol"), "File")
        self.assertTrue(video.media_source.get("Path", "").endswith(".strm"),
                        "an audio .strm gained a remote source: %r"
                        % video.media_source.get("Path"))


@_e2e.require_server
class NoRuntimeResumeTest(_e2e.E2ETestCase):
    """**The mechanism behind "resume does not work on my .strm".**

    Not a shim assertion — a pinned *server* rule, in the same spirit as the
    `MinResumeDurationSeconds` note in the README. It is here because without
    it the shim-side resume tests read as if the shim were free to fix this,
    and it is not: when Jellyfin does not know an item's runtime it marks the
    item fully played on the first progress report and stores no position.

    Every `.strm` is in that state until something asks for `PlaybackInfo`,
    and stays in it for ever if the server's remote probe cannot reach the
    origin — which is the normal condition for the third-party hosts stream
    files point at. So the shim's obligation is to make sure the probe has
    happened *before* it reports anything, and `PlaybackInfoPrecedesReporting`
    below is that assertion.

    Uses the rtsp fixture because it is the one playable-looking item whose
    runtime the server can never learn (nothing is listening on that port),
    and it needs neither a player nor a network.
    """

    def setUp(self):
        super().setUp()
        self.item = self.session.find(_strm.RTSP, library="Movies")
        self.session.reset_played(self.item["Id"])
        self.addCleanup(self.session.reset_played, self.item["Id"])

    def test_the_server_discards_a_position_it_cannot_place(self):
        item_id = self.item["Id"]
        self.assertIsNone(
            self.session.api.get_item(item_id).get("RunTimeTicks"),
            "this fixture is supposed to have no runtime; if the server "
            "learned one, the rule below is being tested against the wrong "
            "item")

        report = {"ItemId": item_id, "PlaySessionId": "e2e-no-runtime",
                  "CanSeek": True, "IsPaused": False,
                  "PlayMethod": "Transcode"}
        self.session.api.session_playing(dict(report, PositionTicks=0))
        self.session.api.session_progress(
            dict(report, PositionTicks=30 * 10000000))

        played = _e2e.wait_for(
            lambda: self.session.user_data(item_id).get("Played") or None)
        self.assertTrue(
            played,
            "the server kept a runtime-less item unwatched; "
            "UserDataManager.UpdatePlayState may have grown a branch we "
            "should now be taking advantage of")
        self.assertEqual(
            self.session.user_data(item_id).get("PlaybackPositionTicks"), 0,
            "a runtime-less item unexpectedly held a resume position")


@_strm.require_origin(_strm.LOCAL_MOVIE)
@_e2e.require_server
class PlaybackInfoPrecedesReportingTest(_e2e.E2ETestCase):
    """The shim's side of the rule above: **ask before you report.**

    `PlaybackInfo` is what makes the server probe the origin, and the probe is
    what gives the item a runtime — measured against 12.0: an item whose
    `RunTimeTicks` was null carries the probed runtime immediately afterwards.
    So as long as the shim resolves its URL before it opens a session, a
    `.strm` whose origin *is* reachable can hold a resume position like
    anything else.

    This is an ordering test, not a value test, which is why it does not care
    what the runtime turns out to be.
    """

    def test_resolving_the_url_teaches_the_server_the_runtime(self):
        episode = _strm.local_episode(self.session)

        video = strm_video(self.session, episode)
        self.assertTrue(video.get_duration(),
                        "the shim resolved a URL without learning a duration")

        # The server now agrees, which is what lets a later progress report
        # be placed rather than discarded.
        self.assertTrue(
            self.session.api.get_item(episode["Id"]).get("RunTimeTicks"),
            "PlaybackInfo did not leave the server holding a runtime; every "
            "progress report for this item will mark it fully played")


if __name__ == "__main__":
    unittest.main()
