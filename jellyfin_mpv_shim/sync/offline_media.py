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

from ..conf import settings
from ..language_config import apply as apply_language_config
from ..media import Video
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
        self.trs_ovr = None
        # Stubbed so the timeline/stop reporting code paths don't blow up.
        self.playback_info = {"PlaySessionId": "", "MediaSources": [self._source]}
        self.media_source = None
        self.intros = []
        self.intro_tried = True

    def get_playback_url(self, video_bitrate=None, force_transcode=False):
        self.media_source = dict(self._source)
        self.media_source["Path"] = self._local_path
        self.media_source["Protocol"] = "File"
        self.media_source["SupportsDirectPlay"] = True
        self.is_transcode = False
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
                # Keep the stored userdata in sync so watched-based delete
                # reflects offline playback (it reads userdata_json, not the
                # pending queue).
                syncManager.db.update_userdata(self.item_id, played=True)
            except Exception:
                log.debug("Failed to queue offline playstate", exc_info=True)

    def record_offline_progress(self, position_ticks, finished=False):
        """Queue resume position (and watched, if finished) made while offline."""
        if self.client is not None:
            return  # online: the timeline already reports progress
        try:
            syncManager.db.upsert_playstate(
                self._server_uuid, self.item_id,
                position_ticks=position_ticks,
                played=True if finished else None)
            # Mirror into the stored userdata so resume position and (on
            # finish) watched state survive without a server round-trip and
            # feed watched-based delete correctly.
            syncManager.db.update_userdata(
                self.item_id, played=finished,
                position_ticks=position_ticks)
        except Exception:
            log.debug("Failed to queue offline progress", exc_info=True)

    def terminate_transcode(self):
        pass  # nothing to tear down for a local file

    def get_intro(self, media_source_id):
        return  # no intro/credits detection for offline playback

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
