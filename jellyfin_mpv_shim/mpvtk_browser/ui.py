"""mpvtk browser as a ``user_interface`` — the in-process launcher.

Exposes the same small surface ``mpv_shim.main`` expects (``start``,
``login_servers``, ``stop``, ``open_player_menu``, ``stop_callback``) as
``cli_mgr``, but instead of a separate window in a child process it
attaches the mpvtk UI to the player's own mpv window (main process, next
to ``playerManager``).

Lifecycle: open the window immediately on a spinner (``enter_browse`` →
``force_window`` + OSC off), connect to servers in the background, then
swap in the live data source. A tile click on a playable item yields the
window to playback + the OSC; when playback stops (``on_playstate``) the
browser takes the window back.
"""

import logging
import os
import threading
import time

from ..clients import clientManager
from ..conf import settings
from ..i18n import _

log = logging.getLogger("mpvtk_browser.ui")


def _collect_servers():
    """Connected servers with tokens — what the browser browses with.
    The shape LibrarySource expects."""
    name_by_uuid = {
        cred.get("uuid"): cred.get("Name") or cred.get("address")
        for cred in list(clientManager.credentials)
    }
    servers = []
    for uuid, client in list(clientManager.clients.items()):
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
        return bool(list(clientManager.credentials))
    except Exception:
        return False


class _PlayerController:
    """Bridges the browser to the player: playback + browse/play window
    state. Imports player/event_handler lazily so the browser package
    stays independent of them for unit tests."""

    def on_browse_enter(self):
        from ..player import playerManager
        # mpvtk_active tells the player the in-window UI is on screen — it
        # gates the idle-quit and makes `q` return to the library.
        playerManager.mpvtk_active = True
        # Logo-free, free-resizing browse window (not force_window()'s menu
        # splash) — removes the Jellyfin icon and stops aspect-ratio snapping.
        playerManager.set_browse_window(True)
        playerManager.enable_osc(False)

    def on_minimize(self):
        """Drop to the windowless state. set_browse_window(False) releases
        force_window when nothing is playing; if a cast is in flight it
        leaves the picture alone, which is exactly the behaviour we want."""
        from ..player import playerManager
        # Clearing mpvtk_active un-gates the idle quit, so a minimized app
        # eventually drops mpv entirely and gives back its memory and GPU
        # context. It comes back on the next play or when the tray reopens
        # the library (see UserInterface.on_mpv_recreated).
        playerManager.mpvtk_active = False
        playerManager.enable_osc(settings.enable_osc)
        playerManager.set_browse_window(False)

    def cancel_load(self):
        """Abandon a playback start still in flight. Takes no player lock, so
        it is safe from the loop thread while the load holds one."""
        from ..player import playerManager
        try:
            return playerManager.cancel_load()
        except Exception:
            log.error("could not cancel the load", exc_info=True)
            return False

    def retry_playback(self, force_transcode=False):
        """Re-attempt the start that just failed, optionally forcing the
        server to transcode. Returns immediately — the player queues the
        replay onto the action thread rather than loading here."""
        from ..player import playerManager
        return playerManager.retry_failed_playback(force_transcode)

    def get_last_server(self):
        """uuid of the server the active user last browsed, or None.

        Only a hint — the server may have been removed or failed to connect
        since, so the browser falls back when it isn't in the live list.
        """
        from ..users import userManager
        try:
            return userManager.get_last_server()
        except Exception:
            log.debug("could not read last server", exc_info=True)
            return None

    def set_last_server(self, server_uuid):
        """Remember the browsed server for the active user. Best-effort:
        failing to persist a preference must never break navigation."""
        from ..users import userManager
        try:
            userManager.set_last_server(server_uuid)
        except Exception:
            log.debug("could not persist last server", exc_info=True)

    def use_hud(self):
        """Whether video playback uses the in-window playback HUD.
        Reads the player's RESOLVED style (settings may hold the legacy
        "jellyfin" alias, and fallbacks may have applied)."""
        from ..player import playerManager
        return getattr(playerManager, "_osc_style_resolved",
                       None) == "mpvtk"

    def trickplay(self):
        """Decoded trickplay tile metadata for the current video, or None
        ({count, multiplier, width, height, file} — see TrickPlay)."""
        from ..player import playerManager
        return playerManager.trickplay_meta

    def hud_key_opts(self):
        """Keyboard policy for the idle HUD ({"grab", "key"}): by
        default only hud_wake_key is taken over during playback so
        mpv's own seek keys keep working."""
        return {"grab": bool(settings.hud_grab_keys),
                "key": settings.hud_wake_key or "ENTER"}

    def hud_menu_state(self):
        """osc_bridge's menu/track state blob for the HUD's pickers
        (audio/subtitles with selection, quality, …), or None."""
        from ..player import playerManager
        try:
            return playerManager.osc_bridge.build_state()
        except Exception:
            log.debug("hud_menu_state failed", exc_info=True)
            return None

    def hud_action(self, verb, arg=None):
        """Route a picker/skip action through the same dispatcher the
        lua OSC uses (osc_bridge.handle_action), so e.g. selecting a
        burn-in subtitle restarts the transcode exactly like the OSD
        menu would."""
        from ..player import playerManager
        args = [verb] if arg is None else [verb, str(arg)]
        playerManager.osc_bridge.handle_action(args)

    def get_speed(self):
        """Current playback speed (1.0 when unknown)."""
        from ..player import playerManager
        try:
            return float(playerManager._player.speed or 1.0)
        except Exception:
            return 1.0

    def set_speed(self, speed):
        self._act(lambda pm: setattr(pm._player, "speed", float(speed)))

    def get_aspect(self):
        """Current video-aspect-override (-1.0 = auto/unknown)."""
        from ..player import playerManager
        try:
            return float(playerManager._player.video_aspect_override or -1.0)
        except Exception:
            return -1.0

    def set_aspect(self, value):
        """``value`` is mpv's string form ("-1", "16:9", …) — the
        property parses ratio strings on both backends."""
        self._act(lambda pm: setattr(
            pm._player, "video_aspect_override", value))

    def toggle_stats(self):
        """Toggle mpv's stats overlay (the gear menu's Playback Data)."""
        self._act(lambda pm: pm._player.command(
            "script-binding", "stats/display-stats-toggle"))

    def set_paused(self, paused):
        """Explicit pause state (scrub-in-progress pauses; commit or
        cancel restores)."""
        self._act(lambda pm: pm.set_paused(bool(paused)))

    def toggle_mute(self):
        self._act(lambda pm: setattr(
            pm._player, "mute", not pm._player.mute))

    def toggle_fullscreen(self):
        """Toggle mpv fullscreen AND record the user's intent, exactly
        like the lua OSC's button (so auto-fullscreen doesn't
        re-fullscreen the next episode against their choice)."""
        def flip(pm):
            was = bool(pm._player.fullscreen)
            pm._player.fullscreen = not was
            pm.put_task(pm.set_fullscreen, not was, True)
        self._act(flip)

    # sub-margin-y saved while the HUD raises the subtitles clear of
    # its bottom bar (the lua OSC does the same while visible)
    _saved_sub_margin = None

    def hud_sub_margin(self, visible):
        """Raise bottom subtitles above the HUD's bar while it is
        summoned; restore on hide. Skipped for top/middle-positioned
        subtitles (sub-pos < 50), like the lua OSC."""
        from ..player import playerManager
        try:
            player = playerManager._player
            if visible:
                sub_pos = player.sub_pos
                if sub_pos is not None and sub_pos < 50:
                    return
                if self._saved_sub_margin is None:
                    self._saved_sub_margin = player.sub_margin_y
                player.sub_margin_y = 130
            elif self._saved_sub_margin is not None:
                player.sub_margin_y = self._saved_sub_margin
                self._saved_sub_margin = None
        except Exception:
            log.debug("hud_sub_margin failed", exc_info=True)

    def chapters(self):
        """mpv's chapter list as [{"title", "time"}], [] when none."""
        from ..player import playerManager
        try:
            chapters = playerManager._player.chapter_list or []
        except Exception:
            return []
        out = []
        for ch in chapters:
            try:
                out.append({"title": ch.get("title") or "",
                            "time": float(ch.get("time") or 0.0)})
            except Exception:
                continue
        return out

    def on_browse_leave(self):
        from ..player import playerManager
        # Restore video aspect handling / playback fullscreen, then hand the
        # OSC back (respecting the user's setting).
        playerManager.browse_yield()
        playerManager.enable_osc(settings.enable_osc)

    def play(self, item, server_uuid, offset_ticks=None,
             srcid=None, aid=None, sid=None):
        self.play_list([item.get("Id")], server_uuid, 0,
                       offset_ticks=offset_ticks, srcid=srcid, aid=aid, sid=sid)

    def play_list(self, item_ids, server_uuid, start_index, offset_ticks=None,
                  srcid=None, aid=None, sid=None):
        from ..event_handler import start_playback
        from ..sync.manager import syncManager
        client = clientManager.clients.get(server_uuid)
        if client is None:
            # Offline (server_uuid is the catalog's pseudo-server "offline",
            # or the real server is simply unreachable): play the local file.
            # start_playback tolerates client=None — offline_video_factory
            # resolves each item against the catalog instead. Mirrors
            # the play path.
            item_ids = list(item_ids)
            # Check the item that will actually start, not item_ids[0]:
            # starting a playlist partway through is the common case.
            first = (item_ids[start_index]
                     if 0 <= start_index < len(item_ids) else None)
            if not (first and syncManager.db
                    and syncManager.db.is_complete(first)):
                log.warning("mpvtk play: no connected client for %s and no "
                            "local copy of %s", server_uuid, first)
                return
        try:
            start_playback(
                client, list(item_ids), start_index=start_index,
                offset_ticks=offset_ticks, aid=aid, sid=sid, srcid=srcid,
                explicit_tracks=(aid is not None or sid is not None))
        except Exception:
            log.error("mpvtk browser failed to start playback", exc_info=True)

    # -- now-playing bar transport (failures are logged, not surfaced) ---

    @staticmethod
    def _act(fn):
        """Every transport action the browser performs goes through here.

        run_action, not a direct call: these run on the browser's loop
        thread, and the player's lock is held for the whole of a playback
        start. Calling through would freeze the window until the load
        finished or timed out — see PlayerManager.run_action.
        """
        from ..player import playerManager
        try:
            playerManager.run_action(fn)
        except Exception:
            log.error("mpvtk player action failed", exc_info=True)

    def raise_window(self):
        from ..player import playerManager
        playerManager.raise_window()

    def refresh_playstate(self):
        """Re-push the now-playing snapshot (the bar's 1s clock tick)."""
        from ..player import playerManager
        playerManager.push_playstate()

    def toggle_pause(self):
        self._act(lambda pm: pm.toggle_pause())

    def stop(self):
        # The now-playing bar's stop button must not take the window with it:
        # stop_and_close() drops force_window, which closed the library out
        # from under the bar that was just clicked.
        self._act(lambda pm: pm.stop_to_browser())

    def stop_for_close(self):
        """Stop playback on the way out of the window — plain stop(), NOT
        stop_to_browser(), which re-asserts the browse window we are in the
        middle of releasing."""
        self._act(lambda pm: pm.stop())

    def next(self):
        self._act(lambda pm: pm.play_next())

    def prev(self):
        self._act(lambda pm: pm.play_prev())

    @staticmethod
    def _ui_seek(pm):
        # HUD-originated seeks are exempt from seek-to-skip-intro for a
        # couple of seconds (scrubbing must not warp to the end of the
        # intro) — the same exemption the lua OSC requested by message.
        pm._last_ui_seek_time = time.time()

    def seek(self, secs):
        def do(pm):
            self._ui_seek(pm)
            pm.seek(float(secs), absolute=True)
        self._act(do)

    def seek_relative(self, secs):
        """Relative seek for the HUD's step buttons (±10s/±30s)."""
        def do(pm):
            self._ui_seek(pm)
            pm.seek(float(secs))
        self._act(do)

    def set_volume(self, pct, notify=True):
        self._act(lambda pm: pm.set_volume(float(pct), notify=notify))

    def set_repeat(self, mode):
        self._act(lambda pm: pm.set_repeat(mode))

    def toggle_favorite(self):
        self._act(lambda pm: pm.toggle_current_favorite())

    # -- tile actions (watched / favorite) --------------------------------

    def set_watched(self, server_uuid, item_id, watched):
        """Mark played/unplayed, queueing it when there's no server.

        Returns True if the change was recorded somewhere. Offline the mark
        goes into the sync catalog for later replay — returning silently
        left the UI showing an optimistic tick that reverted on the next
        reload and never reached the server."""
        if not item_id:
            return False
        client = clientManager.clients.get(server_uuid)
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
        from ..sync.db import STATUS_COMPLETE
        from ..sync.manager import syncManager

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
        client = clientManager.clients.get(server_uuid)
        if client is None or not item_id:
            return False
        try:
            client.jellyfin.favorite(item_id, bool(favorite))
            return True
        except Exception:
            log.error("mpvtk set_favorite failed", exc_info=True)
            return False

    def list_servers(self):
        """Saved servers with a connection badge, for the Settings panel —
        the whole credential list, not just the connected ones _collect_servers
        returns (an offline server must still be removable)."""
        out = []
        for cred in list(clientManager.credentials):
            uuid = cred.get("uuid")
            client = clientManager.clients.get(uuid)
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
            clientManager.remove_client(uuid)
            return True
        except Exception:
            log.error("mpvtk remove_server failed", exc_info=True)
            return False

    def known_servers(self):
        """Server addresses any local user has already used — so a new user
        doesn't have to retype the URL. Addresses only; the URL alone grants
        nothing without credentials."""
        from ..users import userManager
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
            return bool(clientManager.login_with_quick_connect(
                server, code_callback=code_callback,
                should_cancel=should_cancel))
        except Exception as e:
            log.error("mpvtk quick connect failed: %s", e)
            return False

    def add_server(self, server, username, password):
        try:
            return bool(clientManager.login(server, username, password))
        except Exception:
            log.error("mpvtk add_server failed", exc_info=True)
            return False

    def rebuild_source(self):
        from .repository import LibrarySource
        servers = _collect_servers()
        if not servers:
            return None
        return LibrarySource(servers, clientManager.device_id,
                             settings.player_name,
                             not settings.ignore_ssl_cert)

    def has_downloads(self):
        """Is there anything downloaded to browse?

        Cheap enough for a render path: a catalog count, no source built.
        The connecting screen gates its Work Offline button on this."""
        from ..sync.manager import syncManager
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
        from ..sync.manager import syncManager
        from .repository import OfflineLibrarySource
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

    # -- local users ------------------------------------------------------

    def list_users(self):
        """``[{id, name, locked, active}]`` for the chrome's user switcher."""
        from ..users import userManager
        try:
            active = userManager.active_id
            return [{"id": u["id"], "name": u.get("name", "?"),
                     "locked": bool(userManager.is_locked(u["id"])),
                     "require_startup": bool(u.get("require_startup")),
                     "active": u["id"] == active}
                    for u in userManager.public_users()]
        except Exception:
            log.error("mpvtk list_users failed", exc_info=True)
            return []

    def switch_user(self, user_id, pin=None):
        """Switch the active local user and rebuild the data source.

        Returns the new source; False if the user is PIN-locked and the PIN
        didn't match (the caller re-prompts); None if the switch worked but
        there is nothing to browse. Those last two are distinct — reporting
        an unreachable server as a bad PIN is what made a correct PIN look
        wrong. Runs on the browser's worker pool — clientManager.switch_user
        reconnects and can block."""
        from ..users import userManager
        try:
            if userManager.get(user_id) is None:
                return False
            if userManager.is_locked(user_id) and not userManager.verify_pin(
                    user_id, pin or ""):
                return False
            clientManager.switch_user(user_id)
        except Exception:
            log.error("mpvtk switch_user failed", exc_info=True)
            return False
        return self.rebuild_source() or self.offline_source()

    def add_user(self, name):
        """Raises on failure (a duplicate name, most often). Catching here
        made the field clear and nothing happen."""
        from ..users import userManager
        userManager.add_user(name)

    def rename_user(self, user_id, name):
        """Raises on failure — see add_user."""
        from ..users import userManager
        userManager.rename_user(user_id, name)

    def delete_user(self, user_id):
        """Returns (ok, error) — the active user and the last user can't go."""
        from ..users import userManager
        try:
            return userManager.delete_user(user_id)
        except Exception:
            log.error("mpvtk delete_user failed", exc_info=True)
            return False, None

    def set_user_pin(self, user_id, pin, require_startup=False):
        from ..users import userManager
        try:
            userManager.set_pin(user_id, pin or None,
                                require_startup=require_startup)
            return True
        except Exception:
            log.error("mpvtk set_user_pin failed", exc_info=True)
            return False

    # -- startup PIN lock -------------------------------------------------

    def needs_unlock(self):
        from ..users import userManager
        try:
            return bool(userManager.startup_needs_unlock())
        except Exception:
            return False

    def unlock_user(self, user_id, pin):
        """Verify a specific user's PIN (the PIN-setup dialog's current-PIN
        check), as opposed to unlock() which gates the active user."""
        from ..users import userManager
        try:
            return bool(userManager.verify_pin(user_id, pin))
        except Exception:
            return False

    def unlock(self, pin):
        from ..users import userManager
        try:
            return bool(userManager.verify_pin(userManager.active_id, pin))
        except Exception:
            log.error("mpvtk unlock failed", exc_info=True)
            return False

    def connect_and_rebuild(self):
        """Source to browse after a connect attempt: the live servers if any
        answered, else the download catalog. work_offline skips the attempt,
        so it always lands on the catalog."""
        if not settings.work_offline:
            try:
                clientManager.connect_all()
            except Exception:
                log.error("mpvtk connect failed", exc_info=True)
        return self.rebuild_source() or self.offline_source()

    def open_url(self, url):
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:
            log.error("could not open url %s", url, exc_info=True)

    def retry_connect(self):
        """Reconnect from the offline banner. Returns a live source if a
        server answered, else None — the caller stays offline. Explicitly
        going back online clears work_offline, so the *next* launch isn't
        silently offline again (mirrors the Tk browser's banner retry)."""
        try:
            clientManager.connect_all()
        except Exception:
            log.error("mpvtk retry connect failed", exc_info=True)
        source = self.rebuild_source()
        if source is not None and settings.work_offline:
            settings.work_offline = False
            settings.save()
        return source

    # -- play queue -------------------------------------------------------

    def get_queue(self):
        from ..player import playerManager
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
        from ..player import playerManager

        playerManager.queue_remove_many(list(playlist_item_ids))

    def queue_reorder(self, ordered_playlist_item_ids):
        """Reorder the playing queue. RAISES on failure — the queue view
        shows the new order optimistically and has to put it back."""
        from ..player import playerManager

        playerManager.queue_reorder(list(ordered_playlist_item_ids))

    def get_queue_ids(self):
        """Item ids of the playing queue, for "add queue to a playlist"."""
        from ..player import playerManager
        try:
            return list(playerManager.get_queue_ids())
        except Exception:
            log.error("mpvtk get_queue_ids failed", exc_info=True)
            return []

    def queue_items(self, server_uuid, item_ids):
        """Append items to the playing queue; if nothing plays, start them."""
        from ..player import playerManager
        item_ids = list(item_ids)
        if not item_ids:
            return
        # RAISES on failure. "Add to Queue" is a button press, so its
        # failure has to reach the user; this used to swallow AND the caller
        # wrapped it in _client_call -> _safe, so a rejected queue-add was
        # doubly invisible. Same reasoning as download_enqueue below.
        if not playerManager.has_video():
            self.play_list(item_ids, server_uuid, 0)
            return
        video = playerManager.get_video()
        if video is not None:
            video.parent.insert_items(item_ids, append=True)
            playerManager.upd_player_hide()

    # -- SyncPlay ---------------------------------------------------------

    def get_sync_groups(self, server_uuid=None):
        """Active SyncPlay groups, tagged with the server they live on.

        ``server_uuid=None`` asks every connected server. A group belongs to
        one server, and the dialog used to list only the selected one's — so
        with two servers signed in, half your groups were invisible and the
        only way to reach them was to switch servers first.
        """
        if server_uuid is not None:
            targets = [(server_uuid, clientManager.clients.get(server_uuid))]
        else:
            targets = list(clientManager.clients.items())
        names = {c.get("uuid"): (c.get("Name") or c.get("address"))
                 for c in list(clientManager.credentials)}
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
        """The joined group as ``{"group_id", "server_uuid"}``, or None.

        The dialog had no idea which group you were in: every group looked
        joinable and Leave was always offered, including when there was
        nothing to leave."""
        from ..player import playerManager
        try:
            sp = playerManager.syncplay
            if not sp.is_enabled():
                return None
            client = sp.client
            uuid = next((u for u, c in clientManager.clients.items()
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
            return {"group_id": sp.current_group, "server_uuid": uuid}
        except Exception:
            log.debug("sync_state failed", exc_info=True)
            return None

    def _sync(self, server_uuid, fn):
        client = clientManager.clients.get(server_uuid)
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
        self._sync(server_uuid, lambda jf: jf.new_sync_play())

    def sync_leave(self, server_uuid):
        self._sync(server_uuid, lambda jf: jf.leave_sync_play())

    def sync_active(self):
        """True when a SyncPlay group is currently joined."""
        from ..player import playerManager
        try:
            return bool(playerManager.syncplay.is_enabled())
        except Exception:
            return False

    # -- playlist editing -------------------------------------------------

    def _edit(self, server_uuid, fn):
        """Run a playlist/collection edit. RAISES on failure.

        It used to log and return, which quietly defeated every caller's
        error path: a failed delete still ran the caller's success handler
        and navigated away from a playlist that still existed. Callers that
        don't care use _client_call, whose _safe still swallows."""
        client = clientManager.clients.get(server_uuid)
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
        client = clientManager.clients.get(server_uuid)
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

    # -- offline downloads ------------------------------------------------

    def download_estimate(self, server_uuid, item_id, item_type):
        from ..sync.manager import syncManager
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
        from ..sync.manager import syncManager
        syncManager.enqueue(server_uuid, item_id, item_type,
                            include_watched=include_watched)

    def list_downloads(self):
        """The downloads manager's display tree. Reaching the sync db is this
        layer's job; the grouping is in ``downloads.group_downloads``."""
        from .downloads import group_downloads
        from ..sync.manager import syncManager
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
        from ..sync.manager import syncManager
        syncManager.delete(item_id=item_id, series_id=series_id,
                           season_id=season_id, playlist_id=playlist_id,
                           watched_only=watched_only)

    def check_updates(self):
        """One-shot update check at startup.

        Without it a GUI user only ever saw the update notice after starting
        playback, because that was the only thing driving the check."""
        from ..player import playerManager
        try:
            playerManager.update_check.check()
        except Exception:
            log.debug("startup update check failed", exc_info=True)

    def download_status(self):
        """Global download progress for the status bar:
        ``{"pending": n, "name": str, "percent": int|None}``, or None when
        nothing is outstanding."""
        from .downloads import progress_summary
        from ..sync.manager import syncManager
        from ..sync.db import STATUS_COMPLETE
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
        from ..sync.manager import syncManager
        db = getattr(syncManager, "db", None)
        if db is None:
            return (0, 0)
        try:
            from ..sync.db import STATUS_COMPLETE
            rows = db.list()
            pending = sum(1 for r in rows
                          if (r.get("status") or "") != STATUS_COMPLETE)
            return (pending, len(rows))
        except Exception:
            return (0, 0)

    # -- diagnostics ------------------------------------------------------

    def recent_logs(self):
        from ..log_utils import recent_log_lines
        return recent_log_lines()

    def config_dir(self):
        """The config directory, for messages and for the copy-to-file
        fallback."""
        from .. import conffile
        from ..constants import APP_NAME
        return os.path.dirname(conffile.get(APP_NAME, "conf.json"))

    def copy_text(self, text):
        """Copy to the system clipboard, falling back to a file.

        Returns ``(ok, method, path)``. mpv is offered first: it is
        in-process, so a box with none of wl-copy/xclip/xsel installed still
        works. See jellyfin_mpv_shim.clipboard."""
        from ..clipboard import copy_or_save
        player = None
        try:
            from ..player import playerManager
            player = playerManager._player
        except Exception:
            log.debug("no mpv handle for the clipboard", exc_info=True)
        return copy_or_save(
            text, os.path.join(self.config_dir(), "copied-logs.txt"),
            player=player)

    def open_config_folder(self):
        """Reveal the config directory. The tray menu used to be the only way
        to reach it, and the mpvtk browser has no tray."""
        import subprocess
        import sys

        from .. import conffile
        from ..constants import APP_NAME

        path = os.path.dirname(conffile.get(APP_NAME, "conf.json"))
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif sys.platform == "win32":
                os.startfile(path)  # noqa: S606 - documented Windows API
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            log.error("could not open config folder %s", path, exc_info=True)

    def downloaded_ids(self):
        """(item ids, series ids, season ids, playlist ids).

        Neither a playlist nor a season is ever itself a downloads row —
        playlists live in their own table, and a season is expanded into its
        episodes — so without the last two sets a fully downloaded playlist
        or season could never read as downloaded."""
        from ..sync.manager import syncManager
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
        from ..sync.manager import syncManager
        try:
            syncManager.on_change = callback
        except Exception:
            log.debug("could not subscribe to sync changes", exc_info=True)


class UserInterface:
    def __init__(self):
        self.open_player_menu = lambda: None
        self.stop_callback = None
        self.gui_ready = None
        self._app = None
        self._browser = None
        self._thread = None
        self._tray = None
        # True while we are deliberately tearing the render loop down (mpv
        # idle-quit / reconnect), so _run doesn't mistake it for a window
        # close and stop the whole app.
        self._detaching = False

    def start(self):
        # The tray is the only way to reach the app while the mpv window is
        # showing video (or is minimized), so it runs regardless of the
        # browser's state. It lives in its own process — pystray needs its
        # process's main thread, and pystray + libmpv in one process segfaults
        # with GNOME AppIndicator. See tray.py.
        from ..tray import TrayManager

        self._tray = TrayManager({
            "show": self.activate,
            "show_preferences": lambda: self._open_settings("servers"),
            "show_console": lambda: self._open_settings("logs"),
            "open_player_menu": lambda: self.open_player_menu(),
            "open_config": self._open_config_folder,
            "quit": self._quit,
        })
        self._tray.start()
        # The browser itself is created in login_servers, once the mpv handle
        # and saved credentials are available.

    # -- tray actions -----------------------------------------------------

    def activate(self):
        """Surface the UI: leave playback, show the browser, raise the window.

        Also what SingleInstance calls when the app is launched a second time
        (mpv_shim wires ``single.on_activate`` to this)."""
        from ..player import playerManager

        if self._browser is not None:
            # Re-gate behind the startup PIN before anything is revealed: the
            # unlock at launch covers that launch, not every later reopen.
            self._browser.maybe_relock()
            self._browser.enter_browse()
        try:
            playerManager.raise_window()
        except Exception:
            log.debug("could not raise the player window", exc_info=True)

    def _open_settings(self, tab):
        if self._browser is None:
            return
        self.activate()
        self._browser.open_settings(tab)

    def _display_content(self, client, item_id):
        """Route a remote's DisplayContent to the browser, resolving which
        connected server it came from."""
        if self._browser is None:
            return
        uuid = next((u for u, c in clientManager.clients.items()
                     if c is client), None)
        # display_item decides whether to take the window — it must not
        # interrupt playback, so waking the client is its call, not ours.
        self._browser.display_item(uuid, item_id)

    def _open_config_folder(self):
        _PlayerController().open_config_folder()

    def _quit(self):
        if self.stop_callback is not None:
            self.stop_callback()

    def on_window_closed(self):
        """The user closed the mpv window.

        With one shared window, closing it means "minimize to tray" — the app
        stays alive as a cast target. But that is only safe if there *is* a
        tray: without one the app would keep running with no way to reach or
        quit it, so we exit instead."""
        if not settings.close_to_tray:
            self._quit()
            return
        if self._tray is None or not self._tray.available:
            log.info("Window closed and no system tray is available; "
                     "exiting rather than becoming unreachable.")
            self._quit()
            return
        if self._browser is not None:
            # Gate now, while the window is going away, so the locked screen
            # is what's already there when it comes back.
            self._browser.maybe_relock()
            self._browser.minimize()
        # Closing the window means "stop playing" — for music especially,
        # which kept going with no window to control it from. Order matters:
        # minimize() cannot release force_window while something is playing
        # (set_browse_window's `not self._video` guard), so the window used to
        # stay on screen. Stopping *after* it re-enters minimize() through the
        # stopped playstate, which is where force_window finally drops.
        _PlayerController().stop_for_close()

    def login_servers(self):
        from ..player import playerManager, is_using_ext_mpv
        from ..mpvtk.app import MpvtkApp
        from ..mpvtk.rawimage import MemoryStore, cache_dir
        from .app import MpvtkBrowser
        from .repository import LibrarySource
        from .strips import StripStore
        from .thumbnails import ThumbnailStore

        clientManager.load_credentials()

        app = MpvtkApp.attach(playerManager.get_mpv(), ext=is_using_ext_mpv)
        self._app = app
        strips = (StripStore(mem_store=MemoryStore()) if app.in_process
                  else StripStore(cache_dir=cache_dir("mpvtk-browser-")))
        thumbs = ThumbnailStore(
            cache_dir("mpvtk-thumbs-"),
            verify_ssl=not settings.ignore_ssl_cert,
            max_mem_mb=settings.library_image_cache_mb,
        )
        # Open immediately on an empty source (spinner); populate on connect.
        source = LibrarySource([], clientManager.device_id,
                               settings.player_name,
                               not settings.ignore_ssl_cert)
        browser = MpvtkBrowser(app, source, strips=strips, thumbs=thumbs,
                               controller=_PlayerController())
        self._browser = browser
        playerManager.mpvtk_active = True
        playerManager.on_playstate = browser.on_playstate
        # Loading screen + failure/retry UI. Without these a failed start was
        # a blank window for the whole playback_timeout and then nothing.
        playerManager.on_load_start = browser.on_load_start
        playerManager.on_load_error = browser.on_load_error
        # Update notices surface in the browser banner (not the MPV OSD).
        playerManager.notify_update = browser.notify_update

        playerManager.on_window_closed = self.on_window_closed
        # A server that was down at startup must appear once it answers,
        # rather than staying invisible until a manual retry or restart.
        clientManager.on_server_connected = self._on_server_connected
        # Refresh download badges the moment the catalog changes, rather
        # than only when Settings -> Downloads is opened. The push hook has
        # always existed; the browser just never subscribed.
        _PlayerController().on_downloads_changed(browser.on_downloads_changed)
        # mpv is torn down and rebuilt across idle-quit and crash recovery;
        # the renderer is bound to a specific handle, so follow it.
        playerManager.on_mpv_gone = self.on_mpv_gone
        playerManager.on_mpv_terminated = self.on_mpv_terminated
        playerManager.on_mpv_recreated = self.on_mpv_recreated
        playerManager.on_hud_menu = self._browser.open_hud_menu
        # start_minimized: come up in the windowless state — running, castable,
        # reachable from the tray — instead of opening the library. Without a
        # tray there'd be no way back, so honour it only when one is up.
        if settings.start_minimized and self._tray is not None:
            self._tray.ready.wait(5)
        if settings.start_minimized and self._tray is not None \
                and self._tray.available:
            browser.minimize()
        else:
            if settings.start_minimized:
                log.info("start_minimized ignored: no system tray to "
                         "restore the window from.")
            browser.enter_browse()  # take the window + hide the OSC
        if browser.headless:
            # Cast-target UX: the backdrop wants the whole screen, and the
            # browse window deliberately is not fullscreen (browser_
            # fullscreen), so ask explicitly — as the old mirror did.
            browser.show_cast()
            try:
                playerManager.set_fullscreen(True)
            except Exception:
                log.debug("headless fullscreen failed", exc_info=True)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mpvtk-browser")
        self._thread.start()
        browser.start_background_work()
        # BACK/ESC — from the keyboard or a Jellyfin remote (menu_action maps
        # "back" to ESC when the in-window UI owns input).
        playerManager.on_nav_back = browser.on_back
        playerManager.on_nav_command = browser.on_nav_command
        # "Show me this" from a phone/web client. Always ours now — the
        # browser either opens the item's page or, in headless mode, paints
        # it on the cast screen.
        from ..event_handler import eventHandler

        eventHandler.display_content = self._display_content
        # A startup PIN gates connection: show the lock screen and let the
        # unlock drive the connect. Otherwise connect in the background.
        from ..users import userManager
        try:
            locked = userManager.startup_needs_unlock()
        except Exception:
            locked = False
        if locked:
            browser.show_locked()
        else:
            # On the connecting screen, not an empty home route: a home
            # route with no source renders as a bare spinner with nothing
            # explaining it and no way past a server that never answers.
            browser.show_connecting()
            threading.Thread(target=self._connect, daemon=True,
                             name="mpvtk-connect").start()

    # -- following mpv across teardown / re-create -------------------------

    def on_mpv_gone(self):
        """The mpv handle is no longer ours (idle-quit or a lost connection).

        Stop the render loop and detach. Deliberately does NOT free the
        composited tile bitmaps: on libmpv those are in-process buffers mpv
        reads BY ADDRESS every frame it composites, and mpv is still being
        terminated on another thread at this point. Freeing here released
        memory out from under a live compositor — a segfault on quit. That
        happens in on_mpv_terminated instead."""
        self._detaching = True
        app, self._app = self._app, None
        if app is not None:
            app.quit()
        # Wait for the render loop to actually stop before anything else
        # touches the caches it reads. quit() only enqueues.
        # Deliberately NOT cleared when the join times out: on_mpv_recreated
        # joins it again rather than starting a second loop alongside it.
        # build() is not reentrant (it writes _size, _live_offsets, the
        # poster caches, and starts pollers), so two of them is corruption.
        self._join_render_loop()
        if self._browser is not None:
            self._browser.app = None

    def on_mpv_terminated(self):
        """mpv is really dead — now the tile buffers can go.

        Holding them would both leak and defeat the memory saving that
        quitting mpv while minimized is for; freeing them any earlier
        crashes. See playerManager.on_mpv_terminated."""
        if self._browser is not None:
            try:
                self._browser.strips.clear()
            except Exception:
                log.debug("clearing the tile cache failed", exc_info=True)

    def on_mpv_recreated(self):
        """A fresh mpv handle exists — attach a new renderer to it.

        mpvtk binds its event callbacks and loads renderer.lua at attach time,
        so the app object is per-handle; the browser keeps all of its state
        (routes, data, caches) and simply gets pointed at the new one."""
        from ..player import playerManager, is_using_ext_mpv
        from ..mpvtk.app import MpvtkApp

        if self._browser is None:
            return
        try:
            app = MpvtkApp.attach(playerManager.get_mpv(), ext=is_using_ext_mpv)
        except Exception:
            log.error("could not re-attach the mpvtk UI to the new mpv",
                      exc_info=True)
            return
        # Belt and braces: on_mpv_gone joins the old loop, but its join is
        # bounded. Starting a second one alongside a survivor would have two
        # threads calling the non-reentrant build().
        if not self._join_render_loop():
            # The old loop is wedged. A second one would race it inside
            # build(), which is worse than not re-attaching.
            return
        self._app = app
        # set_app (not a bare assignment): the fresh app needs the
        # browser's nav/HUD callbacks re-wired or its events go nowhere.
        self._browser.set_app(app)
        self._detaching = False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mpvtk-browser")
        self._thread.start()
        # A fresh renderer starts active; re-assert the real state
        # (browse / HUD-idle for a video in flight / fully out of the way).
        self._browser.reassert_window_state()
        self._browser.invalidate()

    RENDER_LOOP_JOIN = 2.0

    def _join_render_loop(self):
        """Stop tracking the render loop once it has actually exited.

        Returns True if it is gone. A survivor is kept in ``_thread`` so the
        next attach joins it again instead of racing it."""
        thread = self._thread
        if thread is None or not thread.is_alive():
            self._thread = None
            return True
        thread.join(timeout=self.RENDER_LOOP_JOIN)
        if thread.is_alive():
            log.warning("mpvtk render loop did not stop within %.0fs; "
                        "not starting another alongside it",
                        self.RENDER_LOOP_JOIN)
            return False
        self._thread = None
        return True

    def _run(self):
        app = self._app
        try:
            app.run(self._browser.build)
        except Exception:
            log.error("mpvtk browser loop crashed", exc_info=True)
        finally:
            # A loop that ended because *we* detached (idle-quit, reconnect)
            # is expected — only a real window close should stop the app.
            if not self._detaching and self.stop_callback is not None:
                self.stop_callback()

    def _on_server_connected(self, *_a):
        """A server came up after startup — rebuild so it appears.

        keep_place: this fires from the websocket redial loop, the
        cast-recovery path and the periodic health check, so it lands at
        arbitrary moments mid-session. Resetting to Home threw the user out
        of whatever they were reading every time a flaky server bounced."""
        if self._browser is None:
            return
        try:
            source = _PlayerController().rebuild_source()
        except Exception:
            log.debug("rebuild after connect failed", exc_info=True)
            return
        if source is not None:
            self._browser.set_source(source, server_uuid=self._browser.server,
                                     keep_place=True)

    def _connect(self):
        from .repository import LibrarySource
        if not settings.work_offline:
            try:
                clientManager.connect_all()
            except Exception:
                log.error("mpvtk browser connect failed", exc_info=True)
        servers = _collect_servers()
        if self._browser is None:
            return
        if not servers:
            # No live server — browse the downloads instead of dead-ending on
            # the login screen. work_offline always arrives here (the connect
            # above was skipped), which is what makes the setting mean
            # something in this UI.
            offline = _PlayerController().offline_source()
            if offline is not None:
                self._browser.set_source(offline)
                return
            # Nothing downloaded either. If saved servers exist this is a
            # failed connect, so say so on the connecting screen and leave
            # the retry there; a first run with no accounts wants the login
            # form.
            if _saved_servers_exist():
                log.warning("mpvtk browser: no servers connected")
                self._browser.connect_failed()
                return
            log.warning("mpvtk browser: no servers configured; showing login")
            self._browser.show_login()
            return
        source = LibrarySource(servers, clientManager.device_id,
                               settings.player_name,
                               not settings.ignore_ssl_cert)
        self._browser.set_source(source)

    def stop(self):
        from ..player import playerManager
        if self._tray is not None:
            self._tray.stop()
        playerManager.mpvtk_active = False
        app, self._app = self._app, None
        if app is not None:
            app.quit()
            # quit() only enqueues; wait for the loop to stop pushing scenes
            # before anything frees what those scenes reference.
            self._join_render_loop()
        if self._browser is not None:
            # free_bitmaps=False: mpv may still be alive here (the caller is
            # on its way to terminating it), and on libmpv it composites the
            # tile buffers by address. They are released by
            # on_mpv_terminated, or reclaimed with the process.
            self._browser.shutdown(free_bitmaps=False)


user_interface = UserInterface()
