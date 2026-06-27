"""Offline download manager (main process).

Owns the catalog DB and a single background download worker. The browser drives
it over IPC (estimate / enqueue / delete) and receives change + progress pushes.
Downloads pull the original file via /Items/{id}/Download.
"""

import json
import logging
import os
import shutil
import threading
import time
import urllib.parse

import requests

from ..conf import settings
from ..conffile import confdir
from ..constants import APP_NAME
from .db import (SyncDB, STATUS_PENDING, STATUS_DOWNLOADING, STATUS_COMPLETE,
                 STATUS_ERROR)

log = logging.getLogger("sync.manager")

CHUNK = 1 << 20            # 1 MiB
PROGRESS_STEP = 4 << 20    # push progress every ~4 MiB


class SyncManager:
    def __init__(self):
        self.db = None
        self.root = None
        self.get_client = lambda server_uuid: None
        self.on_change = lambda: None
        self.on_progress = lambda item_id, downloaded, total: None

        self._worker = None
        self._wake = threading.Event()
        self._stop = False

    # -- lifecycle ---------------------------------------------------------

    def start(self, get_client):
        self.get_client = get_client
        self.root = settings.sync_path or os.path.join(confdir(APP_NAME), "offline")
        os.makedirs(self.root, exist_ok=True)
        self.db = SyncDB(os.path.join(self.root, "catalog.db"))
        # Recover rows interrupted mid-download on a previous run.
        for row in self.db.list(status=STATUS_DOWNLOADING):
            self.db.update(row["item_id"], status=STATUS_PENDING)
        self._stop = False
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def stop(self):
        self._stop = True
        self._wake.set()

    # -- queries (also used by the browser via IPC) ------------------------

    def downloaded_item_ids(self):
        return self.db.downloaded_item_ids() if self.db else set()

    def downloaded_series_ids(self):
        return self.db.downloaded_series_ids() if self.db else set()

    def state(self):
        """Lightweight snapshot the browser caches for indicators."""
        if not self.db:
            return {"items": [], "series": [], "total_bytes": 0}
        return {
            "items": sorted(self.db.downloaded_item_ids()),
            "series": sorted(i for i in self.db.downloaded_series_ids() if i),
            "total_bytes": self.db.total_size(),
        }

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
        added = 0
        for item in items:
            if not include_watched and (item.get("UserData") or {}).get("Played"):
                continue
            if self.db.is_complete(item.get("Id")):
                continue
            self._add_row(server_uuid, server_id, item)
            added += 1
        if added:
            log.info("Queued %d item(s) for offline download.", added)
            self._notify_change()
            self._wake.set()
        return added

    def delete_item(self, item_id):
        row = self.db.get(item_id)
        if not row:
            return
        self._remove_files(row)
        self.db.delete(item_id)
        self._notify_change()

    def delete_series(self, series_id):
        for row in self.db.list(series_id=series_id):
            self._remove_files(row)
            self.db.delete(row["item_id"])
        self._notify_change()

    # -- expansion / helpers ----------------------------------------------

    def _expand(self, api, item_id, item_type):
        try:
            if item_type == "Series":
                res = api.shows("/%s/Episodes" % item_id,
                                {"UserId": "{UserId}", "Fields": "MediaSources"})
                return (res or {}).get("Items", [])
            if item_type == "Season":
                season = api.get_item(item_id) or {}
                series_id = season.get("SeriesId")
                if not series_id:
                    return []
                res = api.shows("/%s/Episodes" % series_id,
                                {"UserId": "{UserId}", "SeasonId": item_id,
                                 "Fields": "MediaSources"})
                return (res or {}).get("Items", [])
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

    def _notify_change(self):
        try:
            self.on_change()
        except Exception:
            log.debug("sync on_change callback failed", exc_info=True)

    # -- worker ------------------------------------------------------------

    def _run(self):
        while not self._stop:
            row = None
            pending = self.db.list(status=STATUS_PENDING)
            if pending:
                row = pending[0]
            if row is None:
                self._wake.wait(5)
                self._wake.clear()
                continue
            self._download(row)

    def _download(self, row):
        item_id = row["item_id"]
        client = self.get_client(row["server_uuid"])
        if client is None:
            log.warning("No client for download %s; leaving pending.", item_id)
            self._wake.wait(10)
            return
        self.db.update(item_id, status=STATUS_DOWNLOADING)
        self._notify_change()
        log.info("Downloading %s…", row.get("name") or item_id)
        try:
            item = json.loads(row["item_json"] or "{}")
            source = json.loads(row["source_json"] or "{}")
            item_dir = self._item_dir(row)
            os.makedirs(item_dir, exist_ok=True)
            with open(os.path.join(item_dir, "item.json"), "w") as fh:
                json.dump(item, fh)
            with open(os.path.join(item_dir, "source.json"), "w") as fh:
                json.dump(source, fh)
            self._download_artwork(client, item, item_dir)
            self._download_subs(client, source, item_dir)

            media_path = os.path.join(item_dir, "media." + (row["ext"] or "mkv"))
            url = client.jellyfin.download_url(item_id)
            size = self._stream(url, media_path, item_id, row.get("size_bytes") or 0)

            rel = os.path.relpath(media_path, self.root)
            self.db.update(item_id, status=STATUS_COMPLETE, file_path=rel,
                           downloaded_bytes=size,
                           size_bytes=size or (row.get("size_bytes") or 0))
            log.info("Downloaded %s (%.1f MiB).", row.get("name") or item_id,
                     size / (1 << 20))
        except Exception:
            log.error("Download failed for %s", item_id, exc_info=True)
            self.db.update(item_id, status=STATUS_ERROR)
        self._notify_change()

    def _stream(self, url, dest, item_id, expected):
        verify = not settings.ignore_ssl_cert
        tmp = dest + ".part"
        resume = os.path.getsize(tmp) if os.path.exists(tmp) else 0
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
                        raise RuntimeError("sync stopped")
                    if not chunk:
                        continue
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if downloaded - last_push >= PROGRESS_STEP:
                        self.db.update(item_id, downloaded_bytes=downloaded)
                        try:
                            self.on_progress(item_id, downloaded, total)
                        except Exception:
                            pass
                        last_push = downloaded
        os.replace(tmp, dest)
        return downloaded

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

    def _download_subs(self, client, source, item_dir):
        server = client.config.data.get("auth.server", "")
        token = client.config.data.get("auth.token", "")
        verify = not settings.ignore_ssl_cert
        subs_dir = os.path.join(item_dir, "subs")
        for stream in source.get("MediaStreams") or []:
            if stream.get("Type") != "Subtitle" or not stream.get("IsExternal"):
                continue
            delivery = stream.get("DeliveryUrl")
            if not delivery:
                continue
            url = delivery if stream.get("IsExternalUrl") else (server + delivery)
            sep = "&" if "?" in url else "?"
            url = "%s%sapi_key=%s" % (url, sep, urllib.parse.quote(token))
            codec = stream.get("Codec") or "srt"
            try:
                os.makedirs(subs_dir, exist_ok=True)
                resp = requests.get(url, timeout=(10, 30), verify=verify)
                resp.raise_for_status()
                fname = "%s.%s" % (stream.get("Index"), codec)
                with open(os.path.join(subs_dir, fname), "wb") as fh:
                    fh.write(resp.content)
            except Exception:
                log.debug("Subtitle download failed for stream %s",
                          stream.get("Index"), exc_info=True)


syncManager = SyncManager()
