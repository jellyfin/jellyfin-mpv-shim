"""Watched and favourite — the two flags a tile can toggle.

Split out of the single 1,154-line ``PlayerGateway``; see
``gateway/__init__.py`` for why the facade is composed rather than nested.
"""

import logging

from . import deps
from .base import GatewayCore

log = logging.getLogger("mpvtk_browser.gateway.userdata")


class UserDataMixin(GatewayCore):
    def set_watched(self, server_uuid, item_id, watched):
        """Mark played/unplayed, queueing it when there's no server.

        Returns True if the change was recorded somewhere. Offline the mark
        goes into the sync catalog for later replay — returning silently
        left the UI showing an optimistic tick that reverted on the next
        reload and never reached the server."""
        if not item_id:
            return False
        client = deps.clientManager.clients.get(server_uuid)
        if client is not None:
            try:
                client.jellyfin.item_played(item_id, bool(watched))
                return True
            except Exception:
                log.error("mpvtk set_watched failed", exc_info=True)
                return False
        return self._queue_offline_watched(server_uuid, item_id, watched)

    @staticmethod
    def _queue_offline_watched(server_uuid, item_id, watched):
        """Queue an offline watched mark.

        Only "watched" is representable: the pending queue is advance-only,
        so un-watching offline is dropped rather than silently half-applied.
        A series/season id fans out to its downloaded episodes."""
        from ...sync.db import STATUS_COMPLETE
        from ...sync.manager import syncManager

        db = getattr(syncManager, "db", None)
        if db is None or not watched:
            log.warning("Cannot change watched state for %s while offline.",
                        item_id)
            return False
        try:
            if db.is_complete(item_id):
                targets = [(item_id, server_uuid)]
            else:
                targets = [(r["item_id"], r["server_uuid"] or server_uuid)
                           for r in db.list(status=STATUS_COMPLETE)
                           if item_id in (r["series_id"], r["season_id"])]
            for target_id, target_server in targets:
                db.upsert_playstate(target_server, target_id, played=True)
                # The browser overlay and the watched-based delete read
                # userdata_json, not the pending queue — without this the
                # mark is invisible until the server syncs.
                db.update_userdata(target_id, played=True)
            if not targets:
                log.warning("Nothing downloaded matches %s; watched mark "
                            "not queued.", item_id)
            return bool(targets)
        except Exception:
            log.error("Failed to queue offline watched mark for %s",
                      item_id, exc_info=True)
            return False

    def set_favorite(self, server_uuid, item_id, favorite):
        """Returns True when the change was recorded. Favorites have no
        offline queue, so offline this is a refusal, not a silent no-op —
        the caller rolls its optimistic heart back."""
        client = deps.clientManager.clients.get(server_uuid)
        if client is None or not item_id:
            return False
        try:
            client.jellyfin.favorite(item_id, bool(favorite))
            return True
        except Exception:
            log.error("mpvtk set_favorite failed", exc_info=True)
            return False
