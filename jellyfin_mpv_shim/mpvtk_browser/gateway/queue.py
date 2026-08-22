"""The play queue.

Split out of the single 1,154-line ``PlayerGateway``; see
``gateway/__init__.py`` for why the facade is composed rather than nested.
"""

import logging
from .base import GatewayCore

log = logging.getLogger("mpvtk_browser.gateway.queue")


class QueueMixin(GatewayCore):
    def get_queue(self):
        from ...player import playerManager
        try:
            return playerManager.get_queue()
        except Exception:
            log.error("mpvtk get_queue failed", exc_info=True)
            return {"items": [], "current_id": None}

    def skip_to(self, playlist_item_id):
        self._act(lambda pm: pm.skip_to(playlist_item_id))

    def queue_remove(self, playlist_item_ids):
        """Remove from the playing queue. RAISES on failure.

        Not via _act, which logs and returns: the queue view passes an
        on_error and shows a message, and that path could never fire while
        this swallowed. The call site was "fixed" once without touching
        this, which is exactly why the failure stayed invisible — same
        reasoning as queue_reorder below."""
        from ...player import playerManager

        playerManager.queue_remove_many(list(playlist_item_ids))

    def queue_reorder(self, ordered_playlist_item_ids):
        """Reorder the playing queue. RAISES on failure — the queue view
        shows the new order optimistically and has to put it back."""
        from ...player import playerManager

        playerManager.queue_reorder(list(ordered_playlist_item_ids))

    def get_queue_ids(self):
        """Item ids of the playing queue, for "add queue to a playlist"."""
        from ...player import playerManager
        try:
            return list(playerManager.get_queue_ids())
        except Exception:
            log.error("mpvtk get_queue_ids failed", exc_info=True)
            return []

    def queue_items(self, server_uuid, item_ids):
        """Append items to the playing queue; if nothing plays, start them."""
        self._insert_items(server_uuid, item_ids, append=True)

    def queue_next_items(self, server_uuid, item_ids):
        """Queue items to play straight after the current one.

        Web's ``queuenext`` beside its ``queue``. The player has always been
        able to do this -- ``PlayNext`` from a remote lands in
        ``event_handler`` and takes the same path -- so this is the same
        insert with ``append=False``, which ``Media.insert_items`` splices in
        at ``seq + 1``.
        """
        self._insert_items(server_uuid, item_ids, append=False)

    def _insert_items(self, server_uuid, item_ids, append):
        from ...player import playerManager
        item_ids = list(item_ids)
        if not item_ids:
            return
        # RAISES on failure. "Add to play queue" is a button press, so its
        # failure has to reach the user; this used to swallow AND the caller
        # wrapped it in _client_call -> _safe, so a rejected queue-add was
        # doubly invisible. Same reasoning as download_enqueue below.
        if not playerManager.has_video():
            # Nothing to queue behind or in front of: either way the user
            # asked for these items, so play them.
            self.play_list(item_ids, server_uuid, 0)
            return
        video = playerManager.get_video()
        if video is not None:
            video.parent.insert_items(item_ids, append=append)
            playerManager.upd_player_hide()
