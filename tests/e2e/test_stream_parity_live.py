"""Bitmap subtitle sidecars and external audio, against a real server.

`tests/test_stream_parity.py` has the measurements behind each case; these
check that the shim gets the server's answer all the way into mpv. Each test
asserts on what mpv actually opened, read back off `track-list`.

Not checked here: whether a PGS cue is *drawn*. mpv does not draw a cue from
a `.sup` sidecar when playback starts inside it -- nothing shows until the
next cue -- so a drawing assertion would have to seek to before a cue, and it
would be measuring mpv.
"""

import os
import sys
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

LIBRARY = "Test Media"


def _reasons(video):
    """The server's TranscodeReasons, for a failure message."""
    url = (video.media_source or {}).get("TranscodingUrl") or ""
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get(
        "TranscodeReasons")


def _major(version):
    try:
        return int(version.split(".")[0])
    except ValueError:
        return 0


class _Live(_e2e.E2ETestCase):

    def play(self, name, **media_kw):
        from jellyfin_mpv_shim.media import Media

        item = self.session.find(name, library=LIBRARY)
        media = Media(self.session.client, [item["Id"]],
                      user_id=self.session.user_id, **media_kw)
        video = media.video
        self.pm.play(video, is_initial_play=True)
        self.assertTrue(_e2e.wait_for(lambda: self.pm._player.duration),
                        "mpv never opened %r" % name)
        return video

    def tracks(self, kind):
        return [t for t in self.pm._player.track_list if t["type"] == kind]

    def selected(self, kind):
        return next((t for t in self.tracks(kind) if t.get("selected")), None)


@_e2e.require_server_and_mpv
class PgsSidecarTest(_Live):
    """#811: the `.sup` the server selects by default, as a sidecar."""

    def test_it_direct_plays_with_the_sidecar_loaded(self):
        video = self.play("PGS sidecar (.sup), default")
        self.assertFalse(video.is_transcode,
                         "the .sup made the server burn it in (#811): %s"
                         % _reasons(video))
        self.assertTrue(_e2e.wait_for(lambda: self.selected("sub")),
                        "no subtitle track was ever selected")
        sub = self.selected("sub")
        self.assertTrue(sub.get("external"), sub)
        self.assertEqual(sub.get("codec"), "hdmv_pgs_subtitle", sub)


@_e2e.require_server_and_mpv
class EmbeddedPgsTranscodeTest(_Live):
    """Given the External profile #811 needed, a transcode extracts embedded
    PGS as a sidecar instead of burning it in -- on 10.11 as well as 12.x
    (measured; the source read suggested 10.11 would still burn it in)."""

    def test_it_arrives_as_a_sidecar(self):
        from jellyfin_mpv_shim.media import Media

        item = self.session.find("PGS (Blu-ray image subtitles)",
                                 library=LIBRARY)
        sid = next(s["Index"] for s in self.session.api.get_item(item["Id"])
                   ["MediaSources"][0]["MediaStreams"]
                   if s["Type"] == "Subtitle")
        video = Media(self.session.client, [item["Id"]],
                      user_id=self.session.user_id, sid=sid,
                      explicit_tracks=True).video
        video.set_trs_override(None, True)
        self.pm.play(video, is_initial_play=True)
        self.assertTrue(_e2e.wait_for(lambda: self.pm._player.duration),
                        "mpv never opened the transcode")
        self.assertTrue(video.is_transcode)
        self.assertIn(sid, video.subtitle_url,
                      "embedded PGS was burnt in rather than extracted")
        self.assertTrue(_e2e.wait_for(lambda: self.selected("sub")),
                        "the extracted sidecar was never selected")
        sub = self.selected("sub")
        self.assertTrue(sub.get("external"), sub)
        self.assertEqual(sub.get("codec"), "hdmv_pgs_subtitle", sub)


@_e2e.require_server_and_mpv
class VobSubMksTest(_Live):
    """A 12.0 route; 10.x must keep the burn-in that works there."""

    def test_the_server_version_decides(self):
        new = _major(self.session.server_version()) >= 12
        video = self.play("VobSub in a .mks sidecar")
        if not new:
            self.assertTrue(
                video.is_transcode,
                "a 10.x server was offered the .mks route, which it answers "
                "with a sidecar url that 404s")
            return
        self.assertFalse(video.is_transcode,
                         "a 12.x server burnt in a VobSub .mks it can serve")
        self.assertTrue(_e2e.wait_for(lambda: self.selected("sub")),
                        "no subtitle track was ever selected")
        sub = self.selected("sub")
        self.assertTrue(sub.get("external"), sub)
        self.assertEqual(sub.get("codec"), "dvd_subtitle", sub)


@_e2e.require_server_and_mpv
class ExternalAudioTest(_Live):
    """Embedded eng (default) and fra, sidecar deu and jpn. The server
    numbers the sidecars first: 0 deu, 1 jpn, 2 video, 3 eng, 4 fra."""

    NAME = "Two embedded, two external audio"

    def setUp(self):
        super().setUp()
        self._normalise_remembered_audio()
        self.video = self.play(self.NAME)
        audio = {s["Index"]: s for s in
                 self.video.media_source["MediaStreams"]
                 if s["Type"] == "Audio"}
        self.ext = sorted(i for i, s in audio.items() if s.get("IsExternal"))
        self.emb = sorted(i for i, s in audio.items()
                          if not s.get("IsExternal"))
        self.audio = audio
        self.assertEqual(len(self.ext), 2, audio)
        self.assertEqual(self.ext[0], 0,
                         "the first sidecar is no longer index 0, so this no "
                         "longer covers the apiclient dropping it")
        self.assertFalse(self.video.is_transcode,
                         "the default (embedded) track transcoded: %s"
                         % _reasons(self.video))

    def _normalise_remembered_audio(self):
        """The server remembers this account's last audio pick per item
        (`RememberAudioSelections`) and serves it back as
        `DefaultAudioStreamIndex`, so a run that ended on a sidecar track
        makes the next run's first start a transcode. While remembering is
        on the server only ever overwrites the memory, so report the file's
        own default over it (docs/testing.md section 11)."""
        item = self.session.find(self.NAME, library=LIBRARY)
        streams = self.session.api.get_item(item["Id"])["MediaSources"][0][
            "MediaStreams"]
        default = next(s["Index"] for s in streams if s["Type"] == "Audio"
                       and s.get("IsDefault") and not s.get("IsExternal"))
        self.session._request("/Sessions/Playing/Progress", "POST", {
            "ItemId": item["Id"], "AudioStreamIndex": default,
            "PositionTicks": 0, "IsPaused": True})
        self.assertTrue(_e2e.wait_for(
            lambda: self.session.api.get_item(item["Id"])["MediaSources"][0]
            .get("DefaultAudioStreamIndex") == default),
            "the server still remembers another audio track for this item")

    def _transcoded_index(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(
            self.pm._video.media_source.get("TranscodingUrl") or "").query)
        return query.get("AudioStreamIndex")

    def test_picking_a_sidecar_track_transcodes_it(self):
        for index in self.ext:
            with self.subTest(index=index,
                              lang=self.audio[index].get("Language")):
                self.pm.set_streams(index, None)
                self.assertTrue(
                    _e2e.wait_for(lambda: self.pm._player.duration),
                    "mpv never reopened")
                self.assertTrue(self.pm._video.is_transcode,
                                "picking a sidecar track did not renegotiate")
                self.assertEqual(self._transcoded_index(), [str(index)])

    def test_and_back_to_an_embedded_one_direct_plays_again(self):
        self.pm.set_streams(self.ext[0], None)
        self.assertTrue(self.pm._video.is_transcode)
        fra = self.emb[-1]
        self.pm.set_streams(fra, None)
        self.assertTrue(_e2e.wait_for(lambda: self.pm._player.duration),
                        "mpv never reopened")
        self.assertFalse(self.pm._video.is_transcode)
        self.assertTrue(_e2e.wait_for(lambda: self.selected("audio")))
        self.assertEqual(self.selected("audio").get("lang"),
                         self.audio[fra].get("Language"))

    def test_the_server_is_told_which_track_is_playing(self):
        self.pm.set_streams(self.ext[0], None)
        # Without this the report passes while mpv plays another track: the
        # index is reported whether or not anything delivered it.
        self.assertEqual(self._transcoded_index(), [str(self.ext[0])])
        self.pm.send_timeline()
        reported = _e2e.wait_for(
            lambda: ((self.session.my_session() or {}).get("PlayState") or {})
            .get("AudioStreamIndex") == self.ext[0])
        self.assertTrue(reported, (self.session.my_session() or {})
                        .get("PlayState"))


if __name__ == "__main__":
    unittest.main()
