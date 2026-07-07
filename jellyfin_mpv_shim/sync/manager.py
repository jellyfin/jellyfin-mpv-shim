"""Offline download manager (main process).

Owns the catalog DB and a single background download worker. The browser drives
it over IPC (estimate / enqueue / delete) and receives change + progress pushes.
Downloads pull the original file via /Items/{id}/Download.
"""

import json
import logging
import math
import os
import shutil
import threading
import time
import urllib.parse

import requests

from ..conf import settings
from ..conffile import confdir
from ..constants import APP_NAME
from ..i18n import _
from ..utils import get_profile
from .db import (SyncDB, STATUS_PENDING, STATUS_DOWNLOADING, STATUS_COMPLETE,
                 STATUS_ERROR)

log = logging.getLogger("sync.manager")

CHUNK = 1 << 20            # 1 MiB
PROGRESS_STEP = 4 << 20    # push progress every ~4 MiB
PLAYSTATE_INTERVAL = 30    # replay offline playstate at least this often (s)
STOP_JOIN_TIMEOUT = 10     # how long stop() waits for the worker to unwind (s)


class _Stopped(Exception):
    """Raised inside the worker when the app is shutting down mid-download."""


class _Cancelled(Exception):
    """Raised inside the worker when the active download is being deleted."""


def _sub_format(codec):
    """Map a subtitle codec to the format extension the server should serve."""
    c = (codec or "").lower()
    if c in ("ass", "ssa"):
        return "ass"
    if c in ("vtt", "webvtt"):
        return "vtt"
    if c in ("sub", "subviewer", "microdvd"):
        return "sub"
    return "srt"  # subrip and unknowns -> srt


class SyncManager:
    def __init__(self):
        self.db = None
        self.root = None
        self.get_client = lambda server_uuid: None
        self.on_change = lambda: None
        self.on_progress = lambda item_id, name, downloaded, total: None

        self._worker = None
        self._wake = threading.Event()
        self._stop = False
        # Coordinates the worker with deletes of the item it is actively
        # downloading: the worker owns cleanup so files/rows can't be yanked
        # out from under an in-flight write.
        self._active_lock = threading.Lock()
        self._active_item = None
        self._cancelled = set()
        self._last_playstate = 0.0
        # item_id -> (last downloaded size, consecutive no-progress short reads).
        # A short read normally leaves the row pending to resume; but a server
        # that cleanly truncates at the same offset every time would resume
        # from the same size forever. In-memory (a restart is a fair fresh
        # attempt), escalated to STATUS_ERROR after a few stalls.
        self._short_read_stalls = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self, get_client):
        self.get_client = get_client
        self.root = settings.sync_path or os.path.join(confdir(APP_NAME), "offline")
        self._open_and_run()

    def _open_and_run(self):
        """Open the catalog at self.root and (re)start the download worker.

        Shared by start() and relocate() so re-pointing at a new folder goes
        through exactly the same recover/reconcile path as a fresh launch.
        """
        os.makedirs(self.root, exist_ok=True)
        self.db = SyncDB(os.path.join(self.root, "catalog.db"))
        # Recover rows interrupted mid-download on a previous run.
        for row in self.db.list(status=STATUS_DOWNLOADING):
            self.db.update(row["item_id"], status=STATUS_PENDING)
        # Reconcile the catalog with what is actually on disk (best-effort).
        try:
            self._reconcile_disk()
        except Exception:
            log.debug("Startup disk reconcile failed.", exc_info=True)
        self._stop = False
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def relocate(self, new_path):
        """Move the download tree to new_path and re-point the manager at it.

        Returns (ok, message): message is a user-facing string to surface when
        ok is False (or empty on success). Refuses while a download is actively
        transferring, so nothing is moved out from under an open write. On any
        move failure the downloads are left untouched at the old location and
        the manager resumes there.
        """
        old_root = self.root
        if new_path:
            new_root = os.path.abspath(os.path.expanduser(new_path))
        else:
            new_root = os.path.join(confdir(APP_NAME), "offline")
        if old_root and os.path.abspath(old_root) == new_root:
            return True, ""
        with self._active_lock:
            if self._active_item is not None:
                return False, _("Can't change the download folder while a "
                                "download is in progress. Wait for it to finish, "
                                "then try again.")
        have_downloads = os.path.isdir(old_root) and bool(os.listdir(old_root))
        if have_downloads and os.path.exists(os.path.join(new_root, "catalog.db")):
            return False, _("That folder already contains downloads. Choose an "
                            "empty folder.")
        try:
            os.makedirs(new_root, exist_ok=True)
        except OSError:
            return False, _("Can't create that folder. Check the path and its "
                            "permissions.")
        # Stop the worker and close the catalog so nothing is open mid-move.
        self.stop()
        try:
            self._move_tree(old_root, new_root)
        except Exception:
            log.error("Failed to move download folder from %r to %r",
                      old_root, new_root, exc_info=True)
            self.root = old_root
            self._open_and_run()  # resume where the downloads still are
            return False, _("Moving the downloads failed. They were left in "
                            "place; the download folder was not changed.")
        self.root = new_root
        self._open_and_run()
        return True, ""

    def _move_tree(self, old_root, new_root):
        """Move every entry from old_root into new_root (created by the caller).

        Per-entry shutil.move so it works across drives (copy+delete). Skips any
        name that already exists in the destination rather than clobber it.
        """
        if not os.path.isdir(old_root):
            return
        for name in os.listdir(old_root):
            dest = os.path.join(new_root, name)
            if os.path.exists(dest):
                continue
            shutil.move(os.path.join(old_root, name), dest)
        # Drop the now-empty old folder (best-effort; harmless if it lingers).
        try:
            os.rmdir(old_root)
        except OSError:
            pass

    def stop(self):
        self._stop = True
        self._wake.set()
        # Join the worker so it isn't killed mid-write, then close the catalog.
        # The chunk loop polls self._stop every chunk and every wait() is woken,
        # so this returns quickly.
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=STOP_JOIN_TIMEOUT)
            if worker.is_alive():
                log.warning("Download worker did not stop within %ds.",
                            STOP_JOIN_TIMEOUT)
        if self.db is not None:
            try:
                self.db.close()
            except Exception:
                log.debug("Closing catalog on stop failed.", exc_info=True)

    # -- queries (also used by the browser via IPC) ------------------------

    def downloaded_item_ids(self):
        return self.db.downloaded_item_ids() if self.db else set()

    def downloaded_series_ids(self):
        return self.db.downloaded_series_ids() if self.db else set()

    def state(self):
        """Snapshot the browser caches for indicators + the status bar."""
        if not self.db:
            return {"items": [], "series": [], "total_bytes": 0, "active": 0,
                    "downloading": None}
        rows = self.db.list()
        items = [r["item_id"] for r in rows if r["status"] == STATUS_COMPLETE]
        series = sorted({r["series_id"] for r in rows
                         if r["status"] == STATUS_COMPLETE and r["series_id"]})
        total = sum(r["downloaded_bytes"] or 0 for r in rows)
        active = sum(1 for r in rows
                     if r["status"] in (STATUS_PENDING, STATUS_DOWNLOADING))
        downloading = next((r["name"] for r in rows
                            if r["status"] == STATUS_DOWNLOADING), None)
        return {"items": items, "series": series, "total_bytes": total,
                "active": active, "downloading": downloading}

    # -- estimate / enqueue / delete --------------------------------------

    def estimate(self, server_uuid, item_id, item_type):
        client = self.get_client(server_uuid)
        if not client:
            return {"count": 0, "total_bytes": 0, "watched_count": 0}
        items = self._expand(client.jellyfin, item_id, item_type)
        total = sum(self._source_size(i) for i in items)
        watched = sum(1 for i in items if (i.get("UserData") or {}).get("Played"))
        already = sum(1 for i in items if self.db.is_complete(i.get("Id")))
        return {"count": len(items), "total_bytes": total,
                "watched_count": watched, "already_count": already}

    def enqueue(self, server_uuid, item_id, item_type, include_watched=False):
        client = self.get_client(server_uuid)
        if not client:
            return 0
        server_id = client.config.data.get("auth.server-id")
        items = self._expand(client.jellyfin, item_id, item_type)
        # For a playlist, capture which items already existed before this
        # download so ownership (what a later "delete playlist" may remove) goes
        # only to items this playlist actually pulls down — see _record_playlist.
        pre_existing = ({i.get("Id") for i in items if self.db.get(i.get("Id"))}
                        if item_type == "Playlist" else set())
        added = 0
        members = []  # item ids that will be present offline, in playlist order
        for item in items:
            iid = item.get("Id")
            if self.db.is_complete(iid):
                members.append(iid)  # already downloaded → still a member
                continue
            if not include_watched and (item.get("UserData") or {}).get("Played"):
                continue
            self._add_row(server_uuid, server_id, item)
            members.append(iid)
            added += 1
        if item_type == "Playlist":
            self._record_playlist(server_uuid, server_id, client.jellyfin,
                                  item_id, members, pre_existing)
        if added:
            log.info("Queued %d item(s) for offline download.", added)
            self._notify_change()
            self._wake.set()
        return added

    def _record_playlist(self, server_uuid, server_id, api, playlist_id,
                         member_ids, pre_existing):
        """Persist a downloaded playlist and its membership. An item is `owned`
        by this playlist if this download is what pulls it in (it wasn't already
        in the catalog), or it was already owned by this playlist on a prior
        download. Items that pre-existed from another route stay unowned so a
        later playlist delete leaves them (and their original grouping) intact."""
        if not member_ids:
            # Nothing supported/available offline — drop any stale record so an
            # emptied playlist doesn't linger in the offline UI.
            self.db.delete_playlist(playlist_id)
            return
        try:
            name = (api.get_item(playlist_id) or {}).get("Name") or "Playlist"
        except Exception:
            log.debug("Failed to fetch playlist name for %s", playlist_id,
                      exc_info=True)
            name = "Playlist"
        already_owned = self.db.playlist_owned_ids(playlist_id)
        # A playlist may list the same item twice; membership is keyed by
        # item_id, so keep the first position and drop later duplicates.
        entries, seen = [], set()
        for iid in member_ids:
            if iid in seen:
                continue
            seen.add(iid)
            owned = iid in already_owned or iid not in pre_existing
            entries.append((iid, len(entries), owned))
        self.db.upsert_playlist(playlist_id, server_id, server_uuid, name)
        self.db.replace_playlist_items(playlist_id, entries)

    def _cancel_if_active(self, item_id):
        """If the worker is downloading `item_id`, flag it for cancellation and
        let the worker do the file/row cleanup. Returns True if it was active."""
        with self._active_lock:
            if self._active_item == item_id:
                self._cancelled.add(item_id)
                return True
        return False

    def delete_item(self, item_id):
        # Drop any short-read stall bookkeeping so it can't linger for a
        # deleted item (the worker's finally only clears _cancelled).
        self._short_read_stalls.pop(item_id, None)
        if self._cancel_if_active(item_id):
            self._notify_change()
            return
        row = self.db.get(item_id)
        if not row:
            return
        self._remove_files(row)
        self.db.delete(item_id)
        self._notify_change()

    def delete(self, item_id=None, series_id=None, season_id=None,
               watched_only=False, playlist_id=None):
        """Flexible delete: a single item, a season, a whole series, a
        playlist's downloads, and/or only watched items within that scope."""
        if item_id:
            self.delete_item(item_id)
            return
        if playlist_id:
            self._delete_playlist(playlist_id, watched_only=watched_only)
            return
        rows = self.db.list(series_id=series_id) if series_id else self.db.list()
        removed = 0
        for row in rows:
            if season_id and row.get("season_id") != season_id:
                continue
            if watched_only:
                try:
                    userdata = json.loads(row.get("userdata_json") or "{}")
                except ValueError:
                    userdata = {}
                if not userdata.get("Played"):
                    continue
            if self._cancel_if_active(row["item_id"]):
                removed += 1
                continue
            self._remove_files(row)
            self.db.delete(row["item_id"])
            removed += 1
        if removed:
            self._notify_change()

    def _delete_playlist(self, playlist_id, watched_only=False):
        """Delete a downloaded playlist. Only the items this playlist *owns*
        (pulled down itself) are removed from disk; items that were already
        downloaded another way stay put. The playlist record is then dropped."""
        owned = self.db.playlist_owned_ids(playlist_id)
        for item_id in owned:
            if watched_only:
                row = self.db.get(item_id)
                try:
                    played = bool(json.loads(
                        (row or {}).get("userdata_json") or "{}").get("Played"))
                except ValueError:
                    played = False
                if not played:
                    continue
            self.delete_item(item_id)  # removes files + row, cleans membership
        if not watched_only:
            self.db.delete_playlist(playlist_id)
        self._notify_change()

    # -- expansion / helpers ----------------------------------------------

    def _expand(self, api, item_id, item_type):
        try:
            if item_type == "Series":
                res = api.get_episodes(item_id, fields="MediaSources")
                return (res or {}).get("Items", [])
            if item_type == "Season":
                season = api.get_item(item_id) or {}
                series_id = season.get("SeriesId")
                if not series_id:
                    return []
                res = api.get_episodes(series_id, season_id=item_id,
                                       fields="MediaSources")
                return (res or {}).get("Items", [])
            if item_type == "Playlist":
                res = api.get_playlist_items(item_id, fields="MediaSources")
                items = (res or {}).get("Items", [])
                # Playlists can mix in music/other entries; only download the
                # types the browser surfaces (mirrors PLAYLIST_SUPPORTED_TYPES).
                supported = {"Movie", "Episode", "Video"}
                return [i for i in items if i.get("Type") in supported]
            item = api.get_item(item_id)
            return [item] if item else []
        except Exception:
            log.error("Failed to expand %s (%s)", item_id, item_type, exc_info=True)
            return []

    @staticmethod
    def _source_size(item):
        sources = item.get("MediaSources") or []
        return (sources[0].get("Size") or 0) if sources else 0

    def _add_row(self, server_uuid, server_id, item):
        source = (item.get("MediaSources") or [{}])[0]
        ext = (source.get("Container") or "mkv").split(",")[0]
        self.db.upsert({
            "item_id": item["Id"],
            "server_id": server_id,
            "server_uuid": server_uuid,
            "type": item.get("Type"),
            "name": item.get("Name"),
            "series_id": item.get("SeriesId"),
            "series_name": item.get("SeriesName"),
            "season_id": item.get("SeasonId"),
            "parent_index": item.get("ParentIndexNumber"),
            "index_number": item.get("IndexNumber"),
            "media_source_id": source.get("Id"),
            "file_path": None,
            "ext": ext,
            "size_bytes": source.get("Size") or 0,
            "downloaded_bytes": 0,
            "status": STATUS_PENDING,
            "runtime_ticks": item.get("RunTimeTicks"),
            "item_json": json.dumps(item),
            "source_json": json.dumps(source),
            "userdata_json": json.dumps(item.get("UserData") or {}),
            "added_at": int(time.time()),
        })

    def _item_dir(self, row):
        return os.path.join(self.root, row.get("server_id") or "server",
                            row["item_id"])

    def _remove_files(self, row):
        try:
            shutil.rmtree(self._item_dir(row), ignore_errors=True)
        except Exception:
            log.debug("Failed to remove files for %s", row.get("item_id"),
                      exc_info=True)

    def _reconcile_disk(self):
        """Best-effort startup sweep to keep the catalog and the file store in
        agreement (S12):

        * a row marked COMPLETE whose media file has vanished is re-queued
          (PENDING) so it downloads again;
        * an on-disk per-item directory with no catalog row is removed.

        The shared ``series``/``season`` artwork caches are left alone — they
        aren't per-item download dirs and may be referenced by rows in a state
        this sweep doesn't touch.
        """
        rows = self.db.list()
        known = {}  # server_dir -> set(item_id)
        for row in rows:
            server_dir = row.get("server_id") or "server"
            known.setdefault(server_dir, set()).add(row["item_id"])
            if row["status"] != STATUS_COMPLETE:
                continue
            file_path = row.get("file_path")
            full = os.path.join(self.root, file_path) if file_path else None
            if not full or not os.path.exists(full):
                log.warning("Downloaded file missing for %s; re-queuing.",
                            row.get("name") or row["item_id"])
                self.db.update(row["item_id"], status=STATUS_PENDING,
                               downloaded_bytes=0, file_path=None)

        try:
            server_dirs = os.listdir(self.root)
        except OSError:
            return
        for server_dir in server_dirs:
            base = os.path.join(self.root, server_dir)
            if not os.path.isdir(base):
                continue  # e.g. catalog.db and its WAL sidecars
            item_ids = known.get(server_dir, set())
            try:
                children = os.listdir(base)
            except OSError:
                continue
            for child in children:
                if child in ("series", "season"):
                    continue  # shared artwork caches, not item dirs
                child_path = os.path.join(base, child)
                if not os.path.isdir(child_path) or child in item_ids:
                    continue
                log.warning("Removing orphaned download dir: %s", child_path)
                shutil.rmtree(child_path, ignore_errors=True)

    def _notify_change(self):
        try:
            self.on_change()
        except Exception:
            log.debug("sync on_change callback failed", exc_info=True)

    # -- worker ------------------------------------------------------------

    def _run(self):
        error_streak = 0
        while not self._stop:
            # Consume the wake signal up front. It used to be cleared only in
            # the idle branch, which is unreachable while a pending row
            # exists — so _download's no-client wait() returned instantly and
            # one queued download against an unreachable server busy-spun
            # this loop at full speed.
            self._wake.clear()
            try:
                # Replay offline playstate on its own cadence — not only when the
                # queue is idle — so one pending download for an unreachable
                # server can't starve watched-state sync for a reachable one.
                now = time.monotonic()
                if now - self._last_playstate >= PLAYSTATE_INTERVAL:
                    self._last_playstate = now
                    self._sync_playstate()
                row = None
                pending = self.db.list(status=STATUS_PENDING)
                if pending:
                    row = pending[0]
                if row is None:
                    self._wake.wait(5)
                    continue
                self._download(row)
                error_streak = 0
            except Exception:
                # The worker must survive anything (disk full, DB errors —
                # note the error path's own db.update can raise again on a
                # full disk); back off so a persistent failure can't spin.
                error_streak += 1
                log.exception("Download worker iteration failed.")
                self._wake.wait(min(60, 5 * error_streak))

    def _sync_playstate(self):
        """Replay offline playstate once a server is reachable — advancing only:
        mark watched if the server hasn't, and push a later resume position."""
        pending = self.db.list_playstate()
        if not pending:
            return
        done = []
        for entry in pending:
            client = self.get_client(entry.get("server_uuid"))
            if client is None:
                continue  # still offline for this server
            try:
                server_ud = client.jellyfin.get_userdata_for_item(
                    entry["item_id"]) or {}
                update = {}
                if entry.get("played") and not server_ud.get("Played"):
                    update["Played"] = True
                local_pos = entry.get("position_ticks") or 0
                if local_pos > (server_ud.get("PlaybackPositionTicks") or 0):
                    update["PlaybackPositionTicks"] = local_pos
                if update:
                    client.jellyfin.update_userdata_for_item(entry["item_id"],
                                                             update)
                done.append(entry["id"])
            except Exception:
                log.debug("Failed to replay playstate %s", entry.get("id"),
                          exc_info=True)
        if done:
            self.db.clear_playstate(done)
            log.info("Synced %d offline playstate change(s) to the server.",
                     len(done))

    def _download(self, row):
        item_id = row["item_id"]
        client = self.get_client(row["server_uuid"])
        if client is None:
            log.warning("No client for download %s; leaving pending.", item_id)
            self._wake.wait(10)
            return
        with self._active_lock:
            if item_id in self._cancelled:
                # Deletion requested before we got here — honour it and skip.
                self._cancelled.discard(item_id)
                self._remove_files(row)
                self.db.delete(item_id)
                self._notify_change()
                return
            self._active_item = item_id
        # A delete may have raced in just before we marked the item active (it
        # would have taken the direct path and removed the row). If the row is
        # gone, don't resurrect it.
        if not self.db.get(item_id):
            self._remove_files(row)
            with self._active_lock:
                self._active_item = None
            return
        self.db.update(item_id, status=STATUS_DOWNLOADING)
        self._notify_change()
        log.info("Downloading %s…", row.get("name") or item_id)
        try:
            item = json.loads(row["item_json"] or "{}")
            source = json.loads(row["source_json"] or "{}")
            # Prefer the PlaybackInfo MediaSource: it has DeliveryMethod /
            # DeliveryUrl and full stream details the plain item manifest omits.
            pb_source = self._playback_source(client, item_id, row)
            if pb_source:
                source = pb_source
            item_dir = self._item_dir(row)
            os.makedirs(item_dir, exist_ok=True)
            with open(os.path.join(item_dir, "item.json"), "w") as fh:
                json.dump(item, fh)
            with open(os.path.join(item_dir, "source.json"), "w") as fh:
                json.dump(source, fh)
            self._download_artwork(client, item, item_dir)
            self._download_subs(client, item_id, source, item_dir)
            self._download_trickplay(client, item_id, source, item_dir)
            if item.get("Type") == "Episode" and item.get("SeriesId"):
                self._download_series_art(client, row.get("server_id"),
                                          item["SeriesId"])
                if item.get("SeasonId"):
                    self._download_season_art(client, row.get("server_id"),
                                              item["SeasonId"])

            media_path = os.path.join(item_dir, "media." + (row["ext"] or "mkv"))
            tmp = media_path + ".part"
            url = client.jellyfin.download_url(item_id)
            expected = row.get("size_bytes") or 0
            size, total = self._stream(url, media_path, item_id, row.get("name"),
                                       expected)

            # Never record a short/truncated response as complete: keep the
            # .part and leave the row pending so a later pass resumes it. Don't
            # clobber the known size_bytes with the short length. But if the
            # response keeps ending short at the same offset (no forward
            # progress), give up rather than retry forever.
            if total and size < total:
                last_size, stalls = self._short_read_stalls.get(item_id, (-1, 0))
                stalls = stalls + 1 if size <= last_size else 0
                if stalls >= 3:
                    log.error("Download of %s repeatedly ended short at %d of "
                              "%d bytes; marking failed.",
                              row.get("name") or item_id, size, total)
                    self._short_read_stalls.pop(item_id, None)
                    self.db.update(item_id, status=STATUS_ERROR,
                                   downloaded_bytes=size)
                else:
                    log.error("Download of %s ended short (%d of %d bytes); "
                              "leaving pending to resume.",
                              row.get("name") or item_id, size, total)
                    self._short_read_stalls[item_id] = (size, stalls)
                    self.db.update(item_id, status=STATUS_PENDING,
                                   downloaded_bytes=size)
                self._notify_change()
                return
            self._short_read_stalls.pop(item_id, None)

            # Commit point: promote the .part and mark complete atomically with a
            # final cancellation check under the active lock, so a delete that
            # lands after the last chunk (S4) is honoured instead of being lost
            # to a COMPLETE row. Clearing _active_item here means any delete that
            # arrives after the commit takes the direct path against the now
            # fully-downloaded item rather than the deferred-cancel path.
            rel = os.path.relpath(media_path, self.root)
            with self._active_lock:
                if item_id in self._cancelled:
                    raise _Cancelled()
                os.replace(tmp, media_path)
                self.db.update(item_id, status=STATUS_COMPLETE, file_path=rel,
                               downloaded_bytes=size,
                               size_bytes=size or expected,
                               media_source_id=source.get("Id") or row.get("media_source_id"),
                               source_json=json.dumps(source))
                self._active_item = None
            log.info("Downloaded %s (%.1f MiB).", row.get("name") or item_id,
                     size / (1 << 20))
        except _Cancelled:
            log.info("Download cancelled (deleted): %s", row.get("name") or item_id)
            self._remove_files(row)
            self.db.delete(item_id)
        except _Stopped:
            # App is quitting mid-download: leave it pending so it resumes next
            # launch (the .part file is kept), rather than poisoning it to error.
            log.info("Download interrupted by shutdown: %s", item_id)
            self.db.update(item_id, status=STATUS_PENDING)
        except requests.HTTPError as exc:
            # An HTTP status the server returned. 5xx/429 are transient (server
            # busy) — keep the row PENDING to resume from the .part. 4xx means
            # the item is gone or forbidden — permanent, mark ERROR.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and (status >= 500 or status == 429):
                log.warning("Download of %s got HTTP %s; will resume.",
                            row.get("name") or item_id, status)
                self.db.update(item_id, status=STATUS_PENDING)
                self._notify_change()
                raise
            log.error("Download of %s failed with HTTP %s.",
                      row.get("name") or item_id, status)
            self.db.update(item_id, status=STATUS_ERROR)
        except requests.RequestException as exc:
            # Transient: a dropped connection or read timeout. Keep the row
            # PENDING so the .part resumes (resume offset is read from the file
            # on disk), and re-raise so _run's error backoff throttles the
            # retry instead of hot-looping.
            log.warning("Download of %s interrupted (%s); will resume.",
                        row.get("name") or item_id, exc)
            self.db.update(item_id, status=STATUS_PENDING)
            self._notify_change()
            raise
        except Exception:
            log.error("Download failed for %s", item_id, exc_info=True)
            self.db.update(item_id, status=STATUS_ERROR)
        finally:
            with self._active_lock:
                self._active_item = None
                self._cancelled.discard(item_id)
        self._notify_change()

    def _stream(self, url, dest, item_id, name, expected):
        """Download `url` to `dest`.part, resuming a partial file where possible.

        Returns ``(downloaded, total)``. The caller promotes the .part to `dest`
        (see _download's commit point) — this only fills the .part so a final
        cancellation check can still discard it. `total` is the best-known full
        size (size_bytes or Content-Length) for the short-read guard, or 0.
        """
        tmp = dest + ".part"
        resume = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        # A prior run may have died between the stream finishing and the
        # promotion, leaving a full-size .part. Re-requesting with
        # Range: bytes=<full>- makes the server answer 416; instead, promote
        # what's already on disk (S6).
        if expected and resume >= expected:
            return expected, expected
        try:
            return self._stream_request(url, tmp, item_id, name, expected, resume)
        except requests.HTTPError as exc:
            resp = getattr(exc, "response", None)
            if resp is None or resp.status_code != 416:
                raise
            # Range not satisfiable. If the .part already matches the expected
            # size it really is complete; otherwise it's stale/over-long — drop
            # it and restart the download from the beginning (S6).
            if expected and resume == expected:
                return expected, expected
            log.info("Resume offset rejected (416); restarting %s from scratch.",
                     name or item_id)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return self._stream_request(url, tmp, item_id, name, expected, 0)

    def _stream_request(self, url, tmp, item_id, name, expected, resume):
        verify = not settings.ignore_ssl_cert
        headers = {"Range": "bytes=%d-" % resume} if resume else {}
        with requests.get(url, stream=True, headers=headers, verify=verify,
                          timeout=(10, 60)) as resp:
            if resume and resp.status_code == 200:
                resume = 0  # server ignored Range; restart cleanly
            resp.raise_for_status()
            total = expected or (int(resp.headers.get("Content-Length", 0)) + resume)
            downloaded = resume
            last_push = downloaded
            mode = "ab" if resume else "wb"
            with open(tmp, mode) as fh:
                for chunk in resp.iter_content(CHUNK):
                    if self._stop:
                        raise _Stopped()
                    if item_id in self._cancelled:
                        raise _Cancelled()
                    if not chunk:
                        continue
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if downloaded - last_push >= PROGRESS_STEP:
                        self.db.update(item_id, downloaded_bytes=downloaded)
                        try:
                            self.on_progress(item_id, name, downloaded, total)
                        except Exception:
                            pass
                        last_push = downloaded
        return downloaded, total

    def _download_trickplay(self, client, item_id, source, item_dir):
        """Download trickplay (scrubbing preview) tiles for offline use."""
        api = client.jellyfin
        try:
            full = api.get_item(item_id, fields="Trickplay") or {}
        except Exception:
            return
        manifest = (full.get("Trickplay") or {}).get(source.get("Id")) or {}
        widths = []
        for key in manifest.keys():
            try:
                widths.append(int(key))
            except ValueError:
                pass
        if not widths:
            return
        prefer = settings.thumbnail_preferred_size or 320
        width = min(widths, key=lambda w: abs(w - prefer))
        data = manifest[str(width)]
        try:
            tiles = math.ceil(
                data["ThumbnailCount"] / data["TileWidth"] / data["TileHeight"])
        except Exception:
            return

        verify = not settings.ignore_ssl_cert
        tp_dir = os.path.join(item_dir, "trickplay", str(width))
        os.makedirs(tp_dir, exist_ok=True)
        for i in range(tiles):
            url = api.trickplay_tile_url(item_id, width, i, source.get("Id"))
            try:
                resp = requests.get(url, timeout=(10, 30), verify=verify)
                resp.raise_for_status()
                with open(os.path.join(tp_dir, "%d.jpg" % i), "wb") as fh:
                    fh.write(resp.content)
            except Exception:
                log.debug("Trickplay tile %d failed for %s", i, item_id,
                          exc_info=True)
                return
        with open(os.path.join(item_dir, "trickplay.json"), "w") as fh:
            json.dump({"width": width, "data": data}, fh)
        log.debug("Downloaded %d trickplay tiles for %s.", tiles, item_id)

    def _download_series_art(self, client, server_id, series_id):
        """Cache series poster/backdrop so offline series tiles + the series page
        have artwork (episodes only carry their own images)."""
        series_dir = os.path.join(self.root, server_id or "server", "series",
                                  series_id)
        poster = os.path.join(series_dir, "poster.jpg")
        backdrop = os.path.join(series_dir, "backdrop.jpg")
        if os.path.exists(poster) and os.path.exists(backdrop):
            return
        api = client.jellyfin
        verify = not settings.ignore_ssl_cert
        os.makedirs(series_dir, exist_ok=True)
        jobs = []
        if not os.path.exists(poster):
            jobs.append((poster, api.artwork(series_id, "Primary", 600)))
        if not os.path.exists(backdrop):
            jobs.append((backdrop, api.artwork(series_id, "Backdrop", 1280)))
        for path, url in jobs:
            try:
                resp = requests.get(url, timeout=(10, 30), verify=verify)
                resp.raise_for_status()
                with open(path, "wb") as fh:
                    fh.write(resp.content)
            except Exception:
                log.debug("Series art failed: %s", url, exc_info=True)

    def _playback_source(self, client, item_id, row):
        """Resolve the full MediaSource via PlaybackInfo (metadata only)."""
        try:
            info = client.jellyfin.get_play_info(
                item_id, get_profile(is_remote=False), is_playback=False,
                media_source_id=row.get("media_source_id"))
        except Exception:
            log.debug("PlaybackInfo failed for %s; using item manifest.",
                      item_id, exc_info=True)
            return None
        sources = (info or {}).get("MediaSources") or []
        if not sources:
            return None
        msid = row.get("media_source_id")
        return next((s for s in sources if s.get("Id") == msid), sources[0])

    def _download_season_art(self, client, server_id, season_id):
        """Cache season poster so offline season tiles have artwork."""
        season_dir = os.path.join(self.root, server_id or "server", "season",
                                  season_id)
        poster = os.path.join(season_dir, "poster.jpg")
        if os.path.exists(poster):
            return
        os.makedirs(season_dir, exist_ok=True)
        verify = not settings.ignore_ssl_cert
        try:
            resp = requests.get(client.jellyfin.artwork(season_id, "Primary", 600),
                                timeout=(10, 30), verify=verify)
            resp.raise_for_status()
            with open(poster, "wb") as fh:
                fh.write(resp.content)
        except Exception:
            log.debug("Season art failed for %s", season_id, exc_info=True)

    def _download_artwork(self, client, item, item_dir):
        api = client.jellyfin
        tags = item.get("ImageTags") or {}
        jobs = []
        if "Primary" in tags:
            jobs.append(("poster.jpg", api.artwork(item["Id"], "Primary", 600)))
        if item.get("BackdropImageTags"):
            jobs.append(("backdrop.jpg", api.artwork(item["Id"], "Backdrop", 1280)))
        if "Thumb" in tags:
            jobs.append(("thumb.jpg", api.artwork(item["Id"], "Thumb", 600)))
        verify = not settings.ignore_ssl_cert
        for name, url in jobs:
            try:
                resp = requests.get(url, timeout=(10, 30), verify=verify)
                resp.raise_for_status()
                with open(os.path.join(item_dir, name), "wb") as fh:
                    fh.write(resp.content)
            except Exception:
                log.debug("Artwork %s failed for %s", name, item.get("Id"),
                          exc_info=True)

    def _download_subs(self, client, item_id, source, item_dir):
        """Fetch every external subtitle as a sidecar (subs/<index>.<fmt>).

        The cached source (from get_item) usually has no DeliveryUrl, so we build
        the subtitle stream URL ourselves. Embedded subtitles ride along inside
        the downloaded original file and need no sidecar.
        """
        server = client.config.data.get("auth.server", "").rstrip("/")
        token = client.config.data.get("auth.token", "")
        verify = not settings.ignore_ssl_cert
        media_source_id = source.get("Id") or item_id
        subs_dir = os.path.join(item_dir, "subs")
        for stream in source.get("MediaStreams") or []:
            if stream.get("Type") != "Subtitle" or not stream.get("IsExternal"):
                continue
            index = stream.get("Index")
            if index is None:
                continue
            fmt = _sub_format(stream.get("Codec"))
            delivery = stream.get("DeliveryUrl")
            if delivery:
                base = delivery if stream.get("IsExternalUrl") else (server + delivery)
                sep = "&" if "?" in base else "?"
                url = "%s%sapi_key=%s" % (base, sep, urllib.parse.quote(token))
            else:
                url = client.jellyfin.subtitle_url(
                    item_id, media_source_id, index, fmt)
            try:
                os.makedirs(subs_dir, exist_ok=True)
                resp = requests.get(url, timeout=(10, 30), verify=verify)
                resp.raise_for_status()
                with open(os.path.join(subs_dir, "%s.%s" % (index, fmt)), "wb") as fh:
                    fh.write(resp.content)
                log.debug("Downloaded subtitle stream %s (%s).", index, fmt)
            except Exception:
                log.debug("Subtitle download failed for stream %s",
                          index, exc_info=True)


syncManager = SyncManager()
