"""SyncPlay groups and state.

Split out of the single 1,154-line ``PlayerGateway``; see
``gateway/__init__.py`` for why the facade is composed rather than nested.
"""

import logging

from . import deps
from .base import GatewayCore

log = logging.getLogger("mpvtk_browser.gateway.syncplay")


class SyncPlayMixin(GatewayCore):
    def get_sync_groups(self, server_uuid=None):
        """Active SyncPlay groups, tagged with the server they live on.

        ``server_uuid=None`` asks every connected server. A group belongs to
        one server, and the dialog used to list only the selected one's — so
        with two servers signed in, half your groups were invisible and the
        only way to reach them was to switch servers first.
        """
        if server_uuid is not None:
            targets = [(server_uuid, deps.clientManager.clients.get(server_uuid))]
        else:
            targets = list(deps.clientManager.clients.items())
        names = {c.get("uuid"): (c.get("Name") or c.get("address"))
                 for c in list(deps.clientManager.credentials)}
        out = []
        for uuid, client in targets:
            if client is None:
                continue
            try:
                groups = client.jellyfin.get_sync_play() or []
            except Exception:
                # One unreachable server must not hide the others' groups.
                log.error("mpvtk get_sync_groups failed for %s", uuid,
                          exc_info=True)
                continue
            for g in groups:
                out.append({"id": g.get("GroupId"),
                            "name": g.get("GroupName") or "Group",
                            "participants": g.get("Participants") or [],
                            "server_uuid": uuid,
                            "server_name": names.get(uuid) or ""})
        return out

    def sync_state(self):
        """The joined group as ``{"group_id", "server_uuid", "can_resume"}``,
        or None.

        The dialog had no idea which group you were in: every group looked
        joinable and Leave was always offered, including when there was
        nothing to leave. ``can_resume`` is the halted case -- stopping
        playback steps out of the group's content without leaving the group,
        so there has to be a way back in."""
        from ...player import playerManager
        try:
            sp = playerManager.syncplay
            # in_group: a halted session is exactly the one this dialog most
            # needs to describe -- it is where Leave and Resume live.
            if not sp.in_group():
                return None
            client = sp.client
            uuid = next((u for u, c in deps.clientManager.clients.items()
                         if c is client), None)
            if uuid is None:
                # The identity lookup missed — a reconnect can replace the
                # client object while syncplay still holds the old one.
                # Reporting the group with server_uuid=None made the dialog
                # fall back to the BROWSER's selected server, which is the
                # wrong-server bug this whole thing exists to fix. Better to
                # admit we do not know which server it is on.
                log.debug("syncplay client is not a known server; "
                          "reporting no group")
                return None
            return {"group_id": sp.current_group, "server_uuid": uuid,
                    "following": sp.following, "can_resume": sp.can_resume()}
        except Exception:
            log.debug("sync_state failed", exc_info=True)
            return None

    def _sync(self, server_uuid, fn):
        client = deps.clientManager.clients.get(server_uuid)
        if client is None:
            return
        try:
            fn(client.jellyfin)
        except Exception:
            log.error("mpvtk syncplay action failed", exc_info=True)
            raise

    def sync_join(self, server_uuid, group_id):
        self._sync(server_uuid, lambda jf: jf.join_sync_play(group_id))

    def sync_new(self, server_uuid):
        """Create a group.

        ``new_sync_play_v2``, never ``new_sync_play``: the latter is the
        pre-10.7 call and posts no body, and ``NewGroupRequestDto.GroupName``
        is required -- so every "New Group" from this dialog came back 400
        while the identical button on the OSD menu (which has always used v2)
        worked. Nothing to do with there being no playback session."""
        from ...syncplay import default_group_name

        client = deps.clientManager.clients.get(server_uuid)
        if client is None:
            return
        name = default_group_name(client)
        self._sync(server_uuid, lambda jf: jf.new_sync_play_v2(name))

    def sync_leave(self, server_uuid):
        self._sync(server_uuid, lambda jf: jf.leave_sync_play())

    def sync_resume(self):
        """Play the group's content again after stopping (web's
        ``resumeGroupPlayback``). Through run_action like every other playback
        start -- it loads media, and the player's lock is held for all of it."""
        self._act(lambda pm: pm.syncplay.resume_group_playback())

    def sync_active(self):
        """True when a SyncPlay group is currently joined -- membership, not
        playback, which is what the chrome's SyncPlay button reflects."""
        from ...player import playerManager
        try:
            return bool(playerManager.syncplay.in_group())
        except Exception:
            return False
