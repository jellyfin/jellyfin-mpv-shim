"""Editing playlists and collections on the server.

Split out of the single 1,154-line ``PlayerGateway``; see
``gateway/__init__.py`` for why the facade is composed rather than nested.
"""

from . import deps
from .base import GatewayCore


class EditingMixin(GatewayCore):
    def _edit(self, server_uuid, fn):
        """Run a playlist/collection edit. RAISES on failure.

        It used to log and return, which quietly defeated every caller's
        error path: a failed delete still ran the caller's success handler
        and navigated away from a playlist that still existed. Callers that
        don't care use _client_call, whose _safe still swallows."""
        client = deps.clientManager.clients.get(server_uuid)
        if client is None:
            raise RuntimeError("no server connection")
        fn(client.jellyfin)

    def playlist_move_many(self, server_uuid, playlist_id, moves):
        """Apply ``[(entry_id, new_index), ...]`` IN ORDER.

        A move is an absolute-index operation, so a batch only composes if
        each one lands before the next is computed. Firing them
        concurrently (one task each on a 4-worker pool) landed a different
        order on the server than the one shown. Raises on the first
        failure so the caller can resync."""
        client = deps.clientManager.clients.get(server_uuid)
        if client is None:
            raise RuntimeError("no server connection")
        for entry_id, index in moves:
            client.jellyfin.move_playlist_item(playlist_id, entry_id, index)

    def playlist_remove(self, server_uuid, playlist_id, entry_ids):
        self._edit(server_uuid,
                   lambda jf: jf.remove_playlist_items(playlist_id,
                                                       list(entry_ids)))

    def playlist_add(self, server_uuid, playlist_id, item_ids):
        self._edit(server_uuid,
                   lambda jf: jf.add_playlist_items(playlist_id,
                                                    list(item_ids)))

    def collection_add(self, server_uuid, collection_id, item_ids):
        self._edit(server_uuid,
                   lambda jf: jf.add_collection_items(collection_id,
                                                      list(item_ids)))

    def collection_remove(self, server_uuid, collection_id, item_ids):
        self._edit(server_uuid,
                   lambda jf: jf.remove_collection_items(collection_id,
                                                         list(item_ids)))

    def collection_new(self, server_uuid, name, item_ids):
        self._edit(server_uuid,
                   lambda jf: jf.new_collection(name, list(item_ids)))

    @staticmethod
    def edit_apis():
        """Playlist/collection editing needs apiclient >= 1.15. The edit
        affordances hide entirely when it's older, as the Tk browser does —
        otherwise they render and silently do nothing."""
        try:
            from jellyfin_apiclient_python.api import API
        except Exception:
            return False
        return all(hasattr(API, name) for name in (
            "add_playlist_items", "remove_playlist_items",
            "move_playlist_item", "new_collection", "add_collection_items",
            "remove_collection_items"))

    def playlist_new(self, server_uuid, name, item_ids, is_public=False):
        """Create a playlist. Private by default, as the Tk browser does —
        the server's own default is public, so omitting the flag published
        every playlist the user made to everyone on the server."""
        self._edit(server_uuid,
                   lambda jf: jf.new_playlist(name, list(item_ids),
                                              is_public=bool(is_public)))

    def playlist_delete(self, server_uuid, playlist_id):
        self._edit(server_uuid, lambda jf: jf.delete_item(playlist_id))

    def playlist_update(self, server_uuid, playlist_id, name=None,
                        is_public=None):
        self._edit(server_uuid,
                   lambda jf: jf.update_playlist(playlist_id, name=name,
                                                 is_public=is_public))
