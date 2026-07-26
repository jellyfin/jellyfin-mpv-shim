"""Servers: listing, adding, removing, and the source built from them.

Split out of the single 1,154-line ``PlayerGateway``; see
``gateway/__init__.py`` for why the facade is composed rather than nested.
"""

import logging

from ...conf import settings
from . import deps
from .base import GatewayCore

log = logging.getLogger("mpvtk_browser.gateway.servers")


def _collect_servers():
    """Connected servers with tokens — what the browser browses with.
    The shape LibrarySource expects."""
    name_by_uuid = {
        cred.get("uuid"): cred.get("Name") or cred.get("address")
        for cred in list(deps.clientManager.credentials)
    }
    servers = []
    for uuid, client in list(deps.clientManager.clients.items()):
        cfg = client.config.data
        token = cfg.get("auth.token")
        user_id = cfg.get("auth.user_id")
        address = cfg.get("auth.server")
        if not (token and user_id and address):
            continue
        servers.append({
            "uuid": uuid,
            "name": name_by_uuid.get(uuid) or address,
            "address": address,
            "token": token,
            "user_id": user_id,
        })
    return servers


def _saved_servers_exist():
    """Are there saved accounts at all?

    Distinguishes "your server is down" from "you have not signed in yet" —
    the first wants the connecting screen's retry, the second the login
    form. Sending a first run to a failed-connect message would be nonsense,
    and sending a down server to the login form (which is what happened)
    loses the offline library."""
    try:
        return bool(list(deps.clientManager.credentials))
    except Exception:
        return False


class ServersMixin(GatewayCore):
    def list_servers(self):
        """Saved servers with a connection badge, for the Settings panel —
        the whole credential list, not just the connected ones _collect_servers
        returns (an offline server must still be removable)."""
        out = []
        for cred in list(deps.clientManager.credentials):
            uuid = cred.get("uuid")
            client = deps.clientManager.clients.get(uuid)
            out.append({
                "uuid": uuid,
                "name": cred.get("Name") or cred.get("address") or "?",
                "address": cred.get("address") or "",
                "username": cred.get("Username") or cred.get("username") or "",
                "connected": client is not None,
            })
        return out

    def remove_server(self, uuid):
        try:
            deps.clientManager.remove_client(uuid)
            return True
        except Exception:
            log.error("mpvtk remove_server failed", exc_info=True)
            return False

    def known_servers(self):
        """Server addresses any local user has already used — so a new user
        doesn't have to retype the URL. Addresses only; the URL alone grants
        nothing without credentials."""
        from ...users import userManager
        try:
            return userManager.known_servers()
        except Exception:
            log.debug("known_servers failed", exc_info=True)
            return []

    def quick_connect(self, server, code_callback, should_cancel):
        """Blocking Quick Connect login. ``code_callback(code)`` gets the
        user-facing code as soon as the server issues it; ``should_cancel()``
        is polled so the UI can abandon the wait."""
        try:
            return bool(deps.clientManager.login_with_quick_connect(
                server, code_callback=code_callback,
                should_cancel=should_cancel))
        except Exception as e:
            log.error("mpvtk quick connect failed: %s", e)
            return False

    def add_server(self, server, username, password):
        try:
            return bool(deps.clientManager.login(server, username, password))
        except Exception:
            log.error("mpvtk add_server failed", exc_info=True)
            return False

    def rebuild_source(self):
        from ..repository import LibrarySource
        servers = _collect_servers()
        if not servers:
            return None
        return LibrarySource(servers, deps.clientManager.device_id,
                             settings.player_name,
                             not settings.ignore_ssl_cert)

    def has_downloads(self):
        """Is there anything downloaded to browse?

        Cheap enough for a render path: a catalog count, no source built.
        The connecting screen gates its Work Offline button on this."""
        from ...sync.manager import syncManager
        try:
            if syncManager.downloaded_item_ids():
                return True
            db = getattr(syncManager, "db", None)
            return bool(db is not None and db.list_playlists())
        except Exception:
            log.debug("has_downloads failed", exc_info=True)
            return False

    def offline_source(self):
        """Browse the download catalog with no server, or None if there is
        nothing downloaded to browse (in which case the caller should fall
        back to the login screen rather than an empty library)."""
        from ...sync.manager import syncManager
        from ..repository import OfflineLibrarySource
        path = getattr(getattr(syncManager, "db", None), "path", None)
        if not path:
            return None
        try:
            source = OfflineLibrarySource(path)
            if not source.get_libraries("offline"):
                return None
        except Exception:
            log.error("mpvtk offline source failed", exc_info=True)
            return None
        return source

    def connect_and_rebuild(self):
        """Source to browse after a connect attempt: the live servers if any
        answered, else the download catalog. work_offline skips the attempt,
        so it always lands on the catalog."""
        if not settings.work_offline:
            try:
                deps.clientManager.connect_all()
            except Exception:
                log.error("mpvtk connect failed", exc_info=True)
        return self.rebuild_source() or self.offline_source()

    def retry_connect(self):
        """Reconnect from the offline banner. Returns a live source if a
        server answered, else None — the caller stays offline. Explicitly
        going back online clears work_offline, so the *next* launch isn't
        silently offline again (mirrors the Tk browser's banner retry)."""
        try:
            deps.clientManager.connect_all()
        except Exception:
            log.error("mpvtk retry connect failed", exc_info=True)
        source = self.rebuild_source()
        if source is not None and settings.work_offline:
            settings.work_offline = False
            settings.save()
        return source
