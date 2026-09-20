"""Servers: listing, adding, removing, and the source built from them.

Split out of the single 1,154-line ``PlayerGateway``; see
``gateway/__init__.py`` for why the facade is composed rather than nested.
"""

import logging

from ...conf import settings
from ...constants import OFFLINE_SERVER_UUID
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
        returns (an offline server must still be removable).

        ``problem`` says *why* a disconnected one is disconnected, which is
        what decides whether the row offers Retry or Sign In Again. None on a
        connected server and on one nothing has tried yet."""
        out = []
        for cred in list(deps.clientManager.credentials):
            uuid = cred.get("uuid")
            client = deps.clientManager.clients.get(uuid)
            try:
                problem = deps.clientManager.connection_problem(uuid)
            except Exception:
                log.debug("connection_problem failed", exc_info=True)
                problem = None
            try:
                on_lan = deps.clientManager.server_is_local(uuid)
            except Exception:
                log.debug("server_is_local failed", exc_info=True)
                on_lan = None
            out.append({
                "uuid": uuid,
                "name": cred.get("Name") or cred.get("address") or "?",
                "address": cred.get("address") or "",
                "username": cred.get("Username") or cred.get("username") or "",
                "connected": client is not None,
                "problem": problem,
                # None when nothing has connected to it, which is not the
                # same as False -- the icon falls back to reading the URL
                # rather than claiming the server is out on the internet.
                "local": on_lan,
            })
        return out

    def auto_download_on(self, server_uuid):
        """Is unattended downloading on for this saved login's account?

        Keyed on the account rather than on the uuid the Servers row carries,
        so two addresses for one server answer alike -- R14. The registry is the
        store, not the config: see `users.set_auto_download`.
        """
        from ...users import userManager
        try:
            return bool(userManager.auto_download_on(server_uuid))
        except Exception:
            log.debug("could not read the auto-download list", exc_info=True)
            return False

    def auto_download_logins(self):
        """Which saved logins have unattended downloading on, in one read.

        For a screen asking about several rows: `auto_download_on` takes the
        registry lock per call, and that lock is held across `save()`'s two
        durable writes and two directory fsyncs. Same answer per uuid, still
        keyed on the account -- `users.auto_download_logins` says why.
        """
        from ...users import userManager
        try:
            return set(userManager.auto_download_logins())
        except Exception:
            log.debug("could not read the auto-download list", exc_info=True)
            return set()

    def auto_download_any(self):
        """Is unattended downloading on for any account at all?

        What decides whether switching the feature on has to seed a server:
        empty means none, so without a seed it would come on and do nothing.
        """
        from ...users import userManager
        try:
            return bool(userManager.auto_download_accounts())
        except Exception:
            log.debug("could not read the auto-download list", exc_info=True)
            return False

    def set_auto_download(self, server_uuid, enabled):
        """Turn unattended downloading on or off for a login's account.

        Returns whether anything changed -- False for a login the registry
        cannot resolve to an account, which the caller shows as unchanged
        rather than reporting a save that did not happen.
        """
        from ...users import userManager
        try:
            return bool(userManager.set_auto_download(server_uuid, enabled))
        except Exception:
            log.error("could not persist the auto-download list",
                      exc_info=True)
            return False

    def switcher_servers(self):
        """Every saved server, in credential order, for the top-bar switcher.

        **Including the ones that are not connected**, which is the change
        from listing what the source holds. A server that did not answer
        simply vanished from the switcher, so the only evidence a machine had
        two servers configured was the Settings tab — and there was nothing to
        press to get the second one back.

        Names come from the credential rather than from the source, so an
        entry keeps its name while it is down; the source is still the
        authority for anything that will be *browsed*.
        """
        try:
            live = {sv["uuid"]: sv for sv in _collect_servers()}
            saved = self.list_servers()
        except Exception:
            # The caller is the render path, where an escape kills the UI,
            # and it falls back to the source's own list — which is the
            # behaviour this method replaced, so failing is a downgrade and
            # never a blank bar.
            log.debug("switcher_servers failed", exc_info=True)
            return []
        out = []
        for row in saved:
            row = dict(row)
            row["connected"] = row["uuid"] in live
            if row["connected"]:
                row["name"] = live[row["uuid"]].get("name") or row["name"]
            out.append(row)
        return out

    def retry_server(self, uuid):
        """Try to connect one saved server again.

        Returns ``(ok, problem)``: on failure ``problem`` is a CONNECT_*
        reason, which is what tells the caller whether to offer another Retry
        or to ask for the password. Blocking — the connect carries the
        apiclient's own timeouts — so call it off the loop thread.
        """
        cred = next((c for c in list(deps.clientManager.credentials)
                     if c.get("uuid") == uuid), None)
        if cred is None:
            return False, None
        try:
            if deps.clientManager.connect_client(cred):
                return True, None
        except Exception:
            log.error("mpvtk retry_server failed", exc_info=True)
        try:
            return False, deps.clientManager.connection_problem(uuid)
        except Exception:
            log.debug("connection_problem failed", exc_info=True)
            return False, None

    def reauthenticate(self, uuid, username, password, address=None):
        """Sign in again to a server we already have, **keeping its uuid**.

        Not "remove it and add it back": that was the only route the UI
        offered and it mints a new uuid, which is the identity the download
        catalog and the auto-download allow-list are written in. See
        ``ClientManager.reauthenticate``.

        Returns ``(ok, reason)`` like ``retry_server``: on failure ``reason``
        is a REAUTH_* constant or None, which is what lets the form tell a
        wrong address from a wrong password.
        """
        try:
            return deps.clientManager.reauthenticate(
                uuid, username, password, address=address)
        except Exception:
            log.error("mpvtk reauthenticate failed", exc_info=True)
            return False, None

    def reauthenticate_quick_connect(self, uuid, code_callback=None,
                                     should_cancel=None, address=None):
        """The passwordless half of ``reauthenticate``, same return."""
        try:
            return deps.clientManager.reauthenticate_with_quick_connect(
                uuid, code_callback=code_callback,
                should_cancel=should_cancel, address=address)
        except Exception:
            log.error("mpvtk quick connect reauth failed", exc_info=True)
            return False, None

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
            # Unscoped, and now said so: the question is whether there is
            # anything to browse offline at all, which is not about any one
            # server. The second of the two structurally-unscoped callers;
            # `None` here would now mean "a login that resolves to nothing"
            # and answer with nothing, which is the opposite question.
            from ...sync.db import ANY_SERVER
            if syncManager.downloaded_item_ids(ANY_SERVER):
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
            from ...users import userManager
            # Who is browsing, so the resume positions and ticks shown are
            # this profile's and not whoever downloaded the copy.
            source = OfflineLibrarySource(
                path, actor_on=userManager.actor_on,
                server_name=userManager.server_name_for)
            if not source.get_libraries(OFFLINE_SERVER_UUID):
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
