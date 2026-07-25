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

    def on_downloads_changed(self, callback):
        """Subscribe to catalog changes. The browser polled a status blob
        and never refreshed its badges from it; syncManager has had a push
        hook all along (the Tk browser used it)."""
        from ...sync.manager import syncManager
        try:
            syncManager.on_change = callback
        except Exception:
            log.debug("could not subscribe to sync changes", exc_info=True)
