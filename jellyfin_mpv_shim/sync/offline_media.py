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
import sys

from .. import conf
from ..conf import settings
from ..language_config import apply as apply_language_config
from ..media import Intro, Video, PLAY_DIRECT
from .db import ANY_SERVER
from .manager import SyncManager, syncManager

log = logging.getLogger("sync.offline_media")


def _asking_server(parent):
    """The content scope the caller is playing from: a ``ServerId``,
    ``ANY_SERVER``, or ``None``.

    Two hops, kept together because neither is useful alone: the live client
    gives our saved-login uuid, and the credential behind that uuid gives the
    server. The catalog scopes content by the server, so that a second
    account on the same box is not told the machine lacks a film it holds.

    **Resolved through `content_id_for`, not through `server_id_for`.** It is
    the one place a login becomes a server id, and reaching past it is how
    this file ended up holding a second copy of the rule -- which then kept
    the old two-way convention after the one place had grown a third answer.
    Fully offline there is no client and no uuid, so the answer is
    `ANY_SERVER`: nothing to confuse the row with, and the catalog is the
    only source of items there is. A uuid the registry cannot place answers
    `None`, and `None` now means no row matches.

    **Not the substitution gate, and it cannot be.** This answers
    `ANY_SERVER` both when nothing asked and when the lookup raised, so the
    two are indistinguishable in its return value; `_may_substitute` decides
    on `parent.client` first and only then asks this for the identity.

    **On the class, not on the module-level instance.** It is a static
    function of the saved-login registry and holds no manager state, and a
    test that swaps a stand-in in for `syncManager` must not be able to take
    the scoping rule out with it -- which is precisely what a fake without
    this method did: the lookup raised, the fallback answered unscoped, and
    the test named "another server's item does not play the local file"
    watched it play.
    """
    try:
        from ..clients import clientManager

        uuid = clientManager.uuid_for_client(getattr(parent, "client", None))
        return SyncManager.content_id_for(uuid)
    except Exception:
        log.debug("could not resolve the asking server", exc_info=True)
        return ANY_SERVER


def _may_substitute(item_id, parent, db):
    """May the downloaded copy of ``item_id`` stand in for what was asked?

    **A different question from "do we hold it", and it gets a stricter
    answer.** The catalog's content reads are deliberately permissive -- an
    unattributed row answers, because a download that cannot be shown to be
    somebody else's must stay visible and deletable rather than becoming an
    invisible file on disk. That is the right answer for *visibility*. Handing
    the same permission to substitution is what let one server's file play for
    another server's item, since Jellyfin derives an item id from the media's
    path with no server component in it (docs/jellyfin-api-notes.md 13b).

    So: **substitution requires an identical item id and an identical server
    id**, and nothing else counts -- not `ANY_SERVER`, not a login that
    resolves to no server, not a row with no server recorded.

    **Keyed on whether a client is asking, not on the scope value.**
    `_asking_server` answers `ANY_SERVER` for two different situations -- no
    client at all, and a client whose login lookup *raised* -- so a rule
    reading its answer cannot tell "nothing asked" from "I could not establish
    who asked", and would grant the second the ungated treatment meant for the
    first.

    Ungated when nothing asked (R-D): the item id came out of the catalog row
    itself, so there is no second candidate to confuse it with. `work_offline`
    counts as nothing asked -- it is the offline switch, and it is tested here
    beside `client is None` because "client present, working offline" is a
    state this code already expects; gating it would refuse the local copy and
    fall back to *streaming*, against the setting the user just turned on.

    """
    if getattr(parent, "client", None) is None or settings.work_offline:
        return True
    owner = _asking_server(parent)
    if owner is ANY_SERVER or not owner:
        # A real ServerId or nothing. `ANY_SERVER` here means the lookup could
        # not be made, which is less evidence of ownership than a completed
        # one, not more.
        return False
    if db.owner_of(item_id) != owner:
        return False
    if not _in_group():
        return True
    return _size_agrees_with_server(item_id, parent, db)


def _in_group():
    """Is a SyncPlay group in progress? Never raises, and never imports the
    player to find out.

    **Asked through `sys.modules`, deliberately.** `player.py` builds its
    manager at module scope and its `__init__` ends with `_init_mpv()`, so
    *importing* it opens a real mpv window -- which a lazy import here would do
    inside any test that exercises substitution. If the player was never
    imported, there is no group.
    """
    try:
        player = sys.modules.get("jellyfin_mpv_shim.player")
        if player is None:
            return False
        return bool(player.playerManager.syncplay.in_group())
    except Exception:
        log.debug("could not tell whether a SyncPlay group is running",
                  exc_info=True)
        return False


def _size_agrees_with_server(item_id, parent, db):
    """Does the downloaded file match what the server holds *now*, by size?

    **Only asked inside a SyncPlay group, and that is the whole point.** An
    identical item id means an identical media path, so a matching id is
    unlikely to name a *different video* -- which is why ordinary playback does
    not pay for this. It could still be a different *file*: the server's copy
    may have been replaced since the download, and a different cut or encode
    has a different duration. Alone that is a cosmetic surprise; in a group,
    where every member's position is shared against one timeline, it desyncs
    everybody.

    **An answer that cannot be established counts as disagreement.** In a group
    there is by definition a server to stream from, so refusing costs a
    fallback rather than the film -- and an unverified file is exactly what a
    group cannot afford.
    """
    row = db.get(item_id) or {}
    local = row.get("size_bytes") or 0
    if not local:
        return False
    source_id = row.get("media_source_id")
    try:
        item = parent.client.jellyfin.get_item(item_id) or {}
        for source in item.get("MediaSources") or []:
            if source_id and source.get("Id") != source_id:
                continue
            remote = source.get("Size") or 0
            if remote:
                return int(remote) == int(local)
    except Exception:
        log.debug("could not ask %s for its current media size", item_id,
                  exc_info=True)
        return False
    return False


def still_substitutable(video):
    """Is the local file ``video`` is playing still an acceptable stand-in?

    **The same rule as `_may_substitute`, asked again later.** The factory
    decides once, when playback starts; this is for the things that can change
    *during* playback and make the earlier answer wrong. Joining a SyncPlay
    group is the case that exists: the size check does not apply until there is
    a group, so a film that began as a local copy can become one a group must
    not watch.

    Answers True for anything that is not a local substitution, so a caller can
    ask without knowing what it is holding -- and True when there is no catalog
    to check against, because this decides whether to *interrupt* playback and
    an unanswerable question is not grounds for that.
    """
    if not isinstance(video, OfflineVideo):
        return True
    db = syncManager.db
    if db is None:
        return True
    return _may_substitute(video.item_id, video.parent, db)


def offline_video_factory(item_id, parent, aid=None, sid=None, srcid=None,
                          explicit_tracks=False):
    db = syncManager.db
    # Two questions, deliberately separate: do we hold a finished copy at all,
    # and may it stand in for what was asked. The first is a content read and
    # is permissive by design; the second is `_may_substitute`, which is not.
    if db is None or not db.is_complete(item_id, server_id=ANY_SERVER):
        return None
    if not _may_substitute(item_id, parent, db):
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
        # The Jellyfin server, which is what identifies a person together
        # with their account. The row's `server_uuid` is deliberately NOT
        # kept: it names whoever downloaded the copy, and every use of it
        # here was a viewing filed against the wrong person. See
        # `_acting_login`.
        self._content_server_id = row.get("content_server_id")
        self._local_path = os.path.join(syncManager.root, row["file_path"])
        self._item_dir = os.path.dirname(self._local_path)
        self._subs_dir = os.path.join(self._item_dir, "subs")
        self._trickplay = None
        tp_json = os.path.join(self._item_dir, "trickplay.json")
        if os.path.exists(tp_json):
            try:
                with open(tp_json, encoding="utf-8") as fh:
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

    def resolve_tracks_for_negotiation(self):
        """Settle language_config from the LOCAL source, before playback.

        Nothing here is negotiated -- there is no PlaybackInfo and no
        transcode -- so this is not about getting ahead of the server. It is
        about ORDER relative to the remembered track, which `play()` applies
        immediately after calling this: the rule first, the carried-over
        choice over it.

        This was a no-op, on the reasoning that `map_streams` below applies
        the rule anyway. That inverted the precedence for downloaded items:
        `map_streams` runs from `get_playback_url`, i.e. AFTER `play()` has
        applied the memory, so the rule overwrote the track the user picked on
        the previous episode. Setting `_tracks_resolved` here is what stops it
        running twice.
        """
        if self.explicit_tracks or self._tracks_resolved:
            return
        source = self.media_source or self._source
        if not source:
            return
        self._tracks_resolved = True
        rule_aid, rule_sid = apply_language_config(
            settings.language_config, source, self.item)
        if rule_aid is not None:
            self.aid = rule_aid
        if rule_sid is not None:
            self.sid = rule_sid

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

        # Skipped once resolve_tracks_for_negotiation has run, exactly as
        # Video.map_streams does: `play()` applies the remembered track AFTER
        # that hook and BEFORE this, so re-running the rule here would
        # overwrite the choice the user carried over from the last episode.
        if not self._tracks_resolved:
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

    def _acting_login(self):
        """The saved login this playback is happening *under*, or None.

        Deliberately not the row's own ``server_uuid``, which is why that
        is no longer read at all: it is the login that *downloaded* the
        copy, and on a shared machine that is a different person from the
        one watching. Offline there is no client and so no login at all:
        the right answer, because the actor is then resolved from the row's
        server and the active profile.
        """
        try:
            from ..clients import clientManager
            return clientManager.uuid_for_client(self.client)
        except Exception:
            log.debug("could not resolve the acting login", exc_info=True)
            return None

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
                actor = syncManager.actor_of(
                    acting_login=self._acting_login(),
                    server_id=self._content_server_id)
                syncManager.db.upsert_playstate(
                    self.item_id, actor=actor, played=True)
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
        watched downloads", which reads the catalog with no server fallback
        (unlike the auto-download reaper, which has one).

        **The replay queue is still offline-only**, because that is what it
        is for: a list of changes the server has not been told about. Adding
        to it while online would queue a write that has already happened.
        """
        self._mirror_locally(position_ticks=position_ticks,
                             played=True if finished else None)
        if self.client is not None:
            return  # online: the timeline has already told the server
        try:
            actor = syncManager.actor_of(
                acting_login=self._acting_login(),
                server_id=self._content_server_id)
            syncManager.db.upsert_playstate(
                self.item_id, actor=actor,
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
            # Whoever is playing -- see `_acting_login`. Offline there is
            # no client to name them, so `actor_of` falls back to the active
            # local profile's account on this row's server, and to NO_ACTOR
            # when even that is missing, which records locally and never
            # queues.
            actor = syncManager.actor_of(
                acting_login=self._acting_login(),
                server_id=self._content_server_id)
            syncManager.db.update_userdata(self.item_id, actor=actor,
                                           played=played,
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
            with open(path, encoding="utf-8") as fh:
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

    def get_hls_tile_images(self, width, count, start=0):
        """``count`` tile mosaics from tile index ``start``.

        Same signature as ``media.Video``'s, and it has to be: the TrickPlay
        worker asks for the tiles a window falls in, so an offline item whose
        method still counted from zero answered a mid-film window with the
        beginning of the film. See docs/artwork-pipeline.md section 11.
        """
        if not self._trickplay:
            return
        tp_dir = os.path.join(self._item_dir, "trickplay",
                              str(self._trickplay["width"]))
        for i in range(start, start + count):
            try:
                with open(os.path.join(tp_dir, "%d.jpg" % i), "rb") as fh:
                    yield fh.read()
            except OSError:
                return

    def get_chapters(self):
        return None  # chapter-image previews need the server; skip offline
