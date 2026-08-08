"""Local playback of downloaded items.

``OfflineVideo`` is a drop-in for ``media.Video`` that sources its metadata from
the offline catalog and plays the downloaded file directly — no get_item /
PlaybackInfo / transcode calls. ``offline_video_factory`` is registered with
``media.set_video_factory`` so the player's queue resolves each item to a local
or remote video independently.
"""

import glob
import json
import logging
import os

from .. import conf
from ..conf import settings
from ..language_config import apply as apply_language_config
from ..media import Intro, Video, PLAY_DIRECT
from .manager import syncManager

log = logging.getLogger("sync.offline_media")


def offline_video_factory(item_id, parent, aid=None, sid=None, srcid=None,
                          explicit_tracks=False):
    db = syncManager.db
    if db is None or not db.is_complete(item_id):
        return None
    # Use local when there's no live client (fully offline), or by preference.
    if getattr(parent, "client", None) is None or settings.work_offline \
            or settings.prefer_downloaded:
        # A COMPLETE row whose file was removed out-of-band still looks valid.
        # Verify the file is on disk before handing back a local video.
        row = db.get(item_id)
        file_path = row.get("file_path") if row else None
        if not (file_path and os.path.exists(
                os.path.join(syncManager.root, file_path))):
            log.warning("Download for %s is missing on disk (%s); "
                        "falling back to server if available.",
                        item_id, file_path)
            # Return None so build_video falls through to remote when a client
            # is available, or surfaces a clean "not downloaded" path (its own
            # None-handling) when fully offline.
            return None
        return OfflineVideo(item_id, parent, aid, sid, srcid,
                            explicit_tracks=explicit_tracks)
    return None


class OfflineVideo(Video):
    def __init__(self, item_id, parent, aid=None, sid=None, srcid=None,
                 explicit_tracks=False):
        # Deliberately does NOT call super().__init__ (that hits the server).
        self.item_id = item_id
        self.parent = parent
        self.client = parent.client  # may be None when fully offline
        self.aid = aid
        self.sid = sid
        self.srcid = srcid
        self.explicit_tracks = explicit_tracks

        row = syncManager.db.get(item_id)
        if not row or not row.get("file_path"):
            raise ValueError("No local download for %s" % item_id)
        self.item = json.loads(row.get("item_json") or "{}")
        self._source = json.loads(row.get("source_json") or "{}")
        self._server_uuid = row.get("server_uuid")
        self._local_path = os.path.join(syncManager.root, row["file_path"])
        self._item_dir = os.path.dirname(self._local_path)
        self._subs_dir = os.path.join(self._item_dir, "subs")
        self._trickplay = None
        tp_json = os.path.join(self._item_dir, "trickplay.json")
        if os.path.exists(tp_json):
            try:
                with open(tp_json) as fh:
                    self._trickplay = json.load(fh)
            except Exception:
                self._trickplay = None

        self.is_tv = self.item.get("Type") == "Episode"
        self.subtitle_seq = {}
        self.subtitle_uid = {}
        self.subtitle_url = {}
        self.subtitle_enc = set()
        self.audio_seq = {}
        self.audio_uid = {}
        self.is_transcode = False
        # A downloaded copy is the most direct play there is: a file on this
        # disk, opened by us. Set here rather than in get_playback_url alone
        # because the playback-info screen may be asked before a url has been
        # requested, and unlike the online Video there is no decision pending
        # -- it is a local file whatever anyone's profile says.
        self.play_method = PLAY_DIRECT
        self.transcode_reasons = []
        self.direct_path = True
        self.trs_ovr = None
        # Stubbed so the timeline/stop reporting code paths don't blow up.
        self.playback_info = {"PlaySessionId": "", "MediaSources": [self._source]}
        self.media_source = None
        self.intros = []
        # Not tried yet -- unlike the rest of this class's stubs, there IS
        # something to read (see get_intro). Player.get_playback_url calls it
        # behind conf.any_segment_wanted(), same as online.
        self.intro_tried = False

    def get_playback_url(self, video_bitrate=None, force_transcode=False):
        self.media_source = dict(self._source)
        self.media_source["Path"] = self._local_path
        self.media_source["Protocol"] = "File"
        self.media_source["SupportsDirectPlay"] = True
        self.is_transcode = False
        # Where Video.get_playback_url does it, and behind the same gate:
        # skippable segments are cached with the download (see get_intro).
        if conf.any_segment_wanted():
            self.get_intro(self.media_source.get("Id"))
        self.map_streams()
        log.info("Playing local file: %s", self._local_path)
        return self._local_path

    def map_streams(self):
        """Local-sidecar variant of Video.map_streams (no server references)."""
        self.subtitle_seq = {}
        self.subtitle_uid = {}
        self.subtitle_url = {}
        self.subtitle_enc = set()
        self.audio_seq = {}
        self.audio_uid = {}

        source = self.media_source or self._source
        streams = source.get("MediaStreams") or []

        index = 1
        for stream in streams:
            if stream.get("Type") != "Audio":
                continue
            self.audio_uid[index] = stream["Index"]
            self.audio_seq[stream["Index"]] = index
            if not stream.get("IsExternal"):
                index += 1

        index = 1
        for sub in streams:
            if sub.get("Type") != "Subtitle":
                continue
            if sub.get("IsExternal"):
                # External: downloaded sidecar (named <index>.<fmt>); match it
                # regardless of extension.
                matches = glob.glob(os.path.join(
                    self._subs_dir, "%s.*" % sub.get("Index")))
                if matches:
                    self.subtitle_url[sub["Index"]] = matches[0]
            else:
                # Embedded in the downloaded original file. The cached source
                # (from get_item) often lacks DeliveryMethod, so we key off
                # IsExternal rather than DeliveryMethod == "Embed".
                self.subtitle_uid[index] = sub["Index"]
                self.subtitle_seq[sub["Index"]] = index
                index += 1

        # A deliberate selection in the library browser is final (see
        # Video.map_streams): use the chosen aid/sid as-is.
        if self.explicit_tracks:
            return

        rule_aid, rule_sid = apply_language_config(
            settings.language_config, source, self.item)
        if rule_aid is not None:
            self.aid = rule_aid
        if rule_sid is not None:
            self.sid = rule_sid
        user_aid = source.get("DefaultAudioStreamIndex")
        user_sid = source.get("DefaultSubtitleStreamIndex")
        if user_aid is not None and self.aid is None:
            self.aid = user_aid
        if user_sid is not None and self.sid is None:
            self.sid = user_sid

    def set_played(self, watched=True):
        # The local catalog gets it either way -- see _mirror_locally.
        self._mirror_locally(played=watched or None)
        if self.client is not None:
            try:
                self.client.jellyfin.item_played(self.item_id, watched)
                return
            except Exception:
                log.warning("Failed to report watched online; queueing.",
                            exc_info=True)
        # Offline: only queue advances (watched), never un-watches.
        if watched:
            try:
                syncManager.db.upsert_playstate(self._server_uuid, self.item_id,
                                                played=True)
                # The stored userdata was already updated above, online or
                # off -- this branch is only the replay queue.
            except Exception:
                log.debug("Failed to queue offline playstate", exc_info=True)

    def record_offline_progress(self, position_ticks, finished=False):
        """Record playback progress on a downloaded item.

        Two halves, and only the second is conditional.

        **The local catalog is written whichever way the network is going.**
        It used to be written only offline, on the reasoning that the
        timeline reports progress when there is a server -- which is true
        and beside the point: the timeline reports to the *server*, and the
        catalog is what the app reads when the server is not there. So
        watching a downloaded episode online left the catalog saying
        unwatched at position 0, and that is what you were shown the next
        time you opened it on a train. It also silently broke "delete
        watched downloads", which reads `userdata_json` with no server
        fallback (unlike the auto-download reaper, which has one).

        **The replay queue is still offline-only**, because that is what it
        is for: a list of changes the server has not been told about. Adding
        to it while online would queue a write that has already happened.
        """
        self._mirror_locally(position_ticks=position_ticks,
                             played=True if finished else None)
        if self.client is not None:
            return  # online: the timeline has already told the server
        try:
            syncManager.db.upsert_playstate(
                self._server_uuid, self.item_id,
                position_ticks=position_ticks,
                played=True if finished else None)
            # As above: the stored userdata is _mirror_locally's job now,
            # and it has already run.
        except Exception:
            log.debug("Failed to queue offline progress", exc_info=True)

    def _mirror_locally(self, played=None, position_ticks=None):
        """Merge progress into the catalog's stored userdata. Never raises.

        Advance-only, which is `db.update_userdata`'s rule and the right one
        here: the local copy is a floor, not a mirror. A client that has
        been offline cannot rewind where another device reached, and an
        online rewind does not propagate -- deliberate, and the reason this
        is not simply "keep the two in step".
        """
        if played is None and position_ticks is None:
            return
        try:
            syncManager.db.update_userdata(self.item_id, played=played,
                                           position_ticks=position_ticks)
        except Exception:
            log.debug("Failed to mirror playstate into the catalog",
                      exc_info=True)

    def terminate_transcode(self):
        pass  # nothing to tear down for a local file

    def get_intro(self, media_source_id):
        """Media segments cached beside the file at download time.

        Same shape as the online path, read from segments.json instead of
        asked of the server. Every type is on disk, because what is stored
        outlives the settings that were set when it was written -- turning
        Recap on months later must not mean re-downloading -- and the
        filtering happens HERE, on the way in.

        On the way in, specifically, because that is where online filters
        too: ``include_segment_types=conf.wanted_segment_types()`` means an
        "off" type never reaches ``self.intros`` at all. ``segment_action``
        at playback is a weaker, later layer -- ``get_current_intro`` picks
        the FIRST segment covering the position and ``player.update`` sets
        ``is_in_intro`` for whatever it returns, before consulting the
        action. Letting an "off" type in would therefore give a downloaded
        file two behaviours a streamed one cannot have: a forward seek
        eaten by a Commercial the viewer turned off (``skip_intro_on_seek``),
        and an off-typed segment masking a wanted one where the two overlap,
        so the Skip button never appears.

        An item downloaded before this shipped simply has no file, which is
        the same as an item with no segments.
        """
        if self.intro_tried:
            return
        self.intro_tried = True
        path = os.path.join(self._item_dir, "segments.json")
        try:
            with open(path) as fh:
                segments = json.load(fh)
        except (OSError, ValueError):
            return
        wanted = conf.wanted_segment_types()
        for seg in segments or ():
            try:
                if seg["Type"] not in wanted:
                    continue
                self.intros.append(Intro(seg["Type"],
                                         seg["StartTicks"] / 10000000,
                                         seg["EndTicks"] / 10000000))
            except (KeyError, TypeError):
                log.debug("Skipping malformed offline segment %r", seg)

    # -- trickplay (scrubbing previews) -----------------------------------

    def get_bif(self, prefer_width=320):
        return self._trickplay["data"] if self._trickplay else None

    def get_hls_tile_images(self, width, count):
        if not self._trickplay:
            return
        tp_dir = os.path.join(self._item_dir, "trickplay",
                              str(self._trickplay["width"]))
        for i in range(count):
            try:
                with open(os.path.join(tp_dir, "%d.jpg" % i), "rb") as fh:
                    yield fh.read()
            except OSError:
                return

    def get_chapters(self):
        return None  # chapter-image previews need the server; skip offline
