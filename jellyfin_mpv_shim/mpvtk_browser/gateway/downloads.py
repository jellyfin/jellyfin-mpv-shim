"""The offline catalog: estimates, enqueue, status, deletion.

Split out of the single 1,154-line ``PlayerGateway``; see
``gateway/__init__.py`` for why the facade is composed rather than nested.
"""

import logging
from .base import GatewayCore

log = logging.getLogger("mpvtk_browser.gateway.downloads")


class DownloadsMixin(GatewayCore):
    def download_estimate(self, server_uuid, item_id, item_type):
        from ...sync.manager import syncManager
        # RAISES. Returning a zero estimate made a *failure* indistinguishable
        # from "already fully downloaded": the dialog gates its Download
        # button on count and rendered "Nothing left to download." instead,
        # so a server error told the user the item was already on disk and
        # withheld the one control that would have retried.
        return syncManager.estimate(server_uuid, item_id, item_type)

    def download_enqueue(self, server_uuid, item_id, item_type,
                         include_watched=False):
        """Raises on failure. "Download" is a button press whose failure the
        user has to see — swallowed, a rejected enqueue looked exactly like a
        queued one and the item simply never appeared."""
        from ...sync.manager import syncManager
        syncManager.enqueue(server_uuid, item_id, item_type,
                            include_watched=include_watched)

    def list_downloads(self):
        """The downloads manager's display tree. Reaching the sync db is this
        layer's job; the grouping is in ``downloads.group_downloads``."""
        from ..downloads import group_downloads
        from ...sync.manager import syncManager
        db = getattr(syncManager, "db", None)
        if db is None:
            return []
        try:
            rows = db.list()
            playlists = db.list_playlists()
            owned = db.playlist_ownership()
        except Exception:
            log.error("mpvtk list_downloads failed", exc_info=True)
            return []

        def items_of(playlist_id):
            try:
                return db.playlist_item_rows(playlist_id)
            except Exception:
                # One unreadable playlist collapses to empty rather than
                # taking the whole downloads list down with it.
                log.warning("playlist rows unreadable: %s", playlist_id,
                            exc_info=True)
                return []

        return group_downloads(rows, playlists, items_of, owned)

    def delete_download(self, item_id=None, series_id=None, season_id=None,
                        playlist_id=None, watched_only=False):
        """Delete one item, a season, a series, or a playlist's downloads.

        ``watched_only`` keeps unwatched items — the "reclaim space on a
        finished show" gesture the Tk browser has.

        Raises on failure. It used to catch-and-log, which silently defeated
        every caller's on_error — including views.py's "The download could not
        be removed.", an error message that could never be shown. Same reason
        _edit, queue_reorder and playlist_move_many raise.
        """
        from ...sync.manager import syncManager
        syncManager.delete(item_id=item_id, series_id=series_id,
                           season_id=season_id, playlist_id=playlist_id,
                           watched_only=watched_only)

    def download_status(self):
        """Global download progress for the status bar:
        ``{"pending": n, "name": str, "percent": int|None}``, or None when
        nothing is outstanding."""
        from ..downloads import progress_summary
        from ...sync.manager import syncManager
        from ...sync.db import STATUS_COMPLETE
        db = getattr(syncManager, "db", None)
        if db is None:
            return None
        try:
            rows = [r for r in db.list()
                    if (r.get("status") or "") != STATUS_COMPLETE]
        except Exception:
            return None
        return progress_summary(rows)

    def download_activity(self):
        """(active, pending) counts — the downloads view polls this so it can
        refresh itself while a download runs."""
        from ...sync.manager import syncManager
        db = getattr(syncManager, "db", None)
        if db is None:
            return (0, 0)
        try:
            from ...sync.db import STATUS_COMPLETE
            rows = db.list()
            pending = sum(1 for r in rows
                          if (r.get("status") or "") != STATUS_COMPLETE)
            return (pending, len(rows))
        except Exception:
            return (0, 0)

    def downloaded_ids(self):
        """(item ids, series ids, season ids, playlist ids).

        Neither a playlist nor a season is ever itself a downloads row —
        playlists live in their own table, and a season is expanded into its
        episodes — so without the last two sets a fully downloaded playlist
        or season could never read as downloaded."""
        from ...sync.manager import syncManager
        try:
            db = getattr(syncManager, "db", None)
            playlists = set()
            if db is not None:
                playlists = {p["playlist_id"] for p in db.list_playlists()}
            return (set(syncManager.downloaded_item_ids()),
                    set(syncManager.downloaded_series_ids()),
                    set(syncManager.downloaded_season_ids()),
                    playlists)
        except Exception:
            return (set(), set(), set(), set())

    # -- books -------------------------------------------------------------
    #
    # A book is the one library item this app cannot render (see
    # ``books.py``), so its downloaded copy is not an offline convenience —
    # it is the only way to read the thing at all. That makes two questions
    # the rest of the download API never had to answer: where is the file,
    # and would the desktop open it.

    def book_download_state(self, item_id):
        """``(status, path)`` for one item's download.

        ``status`` is a catalog status (``"complete"``, ``"downloading"``,
        ``"pending"``, ``"error"``) or ``None`` when nothing is queued;
        ``path`` is the absolute file, and only ever set alongside
        ``"complete"``.

        The path is checked on disk rather than trusted from the row. A
        catalog that says complete and a file that is gone is a real state —
        the store can be moved, pruned or cleaned by hand — and the reader
        this feeds would otherwise be handed a path that does not exist,
        which every desktop reports as a corrupt book.
        """
        import os
        from ...sync.manager import syncManager
        from ...sync.db import STATUS_COMPLETE
        db = getattr(syncManager, "db", None)
        if db is None:
            return (None, None)
        try:
            row = db.get(item_id)
        except Exception:
            log.debug("book_download_state failed for %s", item_id,
                      exc_info=True)
            return (None, None)
        if not row:
            return (None, None)
        status = row.get("status") or None
        rel = row.get("file_path")
        if status != STATUS_COMPLETE or not rel:
            return (status, None)
        path = os.path.join(syncManager.root or "", rel)
        return (status, path) if os.path.exists(path) else (status, None)

    def open_downloaded_file(self, item_id):
        """Hand this item's downloaded file to the desktop. ``(ok, method)``.

        Lives here rather than in the page because it is the shell reaching
        outside the process, which is what this gateway is for — and because
        the path it needs comes from the catalog, which the browser has no
        business opening itself.
        """
        from ...system_open import open_path
        _status, path = self.book_download_state(item_id)
        if not path:
            return (False, None)
        return open_path(path)

    def delete_downloads(self, item_ids):
        """Delete several downloads by id.

        The scoped deletes (series/season/playlist) cannot express "these
        files": an audiobook read from a folder is N rows with no server-side
        object joining them, so the caller names them. Raises, like its
        single-item sibling — a failed delete that looks like a success is
        the bug ``delete_download`` was fixed for.
        """
        from ...sync.manager import syncManager
        for one in item_ids or ():
            if one:
                syncManager.delete(item_id=one)

    def on_downloads_changed(self, callback):
        """Subscribe to catalog changes. The browser polled a status blob
        and never refreshed its badges from it; syncManager has had a push
        hook all along (the Tk browser used it)."""
        from ...sync.manager import syncManager
        try:
            syncManager.on_change = callback
        except Exception:
            log.debug("could not subscribe to sync changes", exc_info=True)
