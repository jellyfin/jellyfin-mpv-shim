"""State/action bridge between the player and the playback HUD.

Assembles the menu/track state blob (localized labels included) the
mpvtk playback HUD's pickers and gear menu render from (build_state),
and dispatches their selections (handle_action). Actions are routed
through the same playerManager code paths the OSD menu (menu.py) uses,
so e.g. selecting a burn-in subtitle stream restarts the transcode
exactly like the ``c`` menu would.

(Historically this drove the jellyfin-styled lua OSC over script
messages; that OSC was retired once the HUD reached parity, and the
verb vocabulary lives on here.)
"""

import logging

from .conf import settings
from .i18n import _
from .menu import COLOR_LIST, SIZE_LIST, TRANSCODE_LEVELS, lang_filter
from .utils import get_sub_display_title

log = logging.getLogger("osc_bridge")


class OscBridge:
    def __init__(self, player_manager):
        self.playerManager = player_manager
        # SyncPlay group discovery hits the server, so it only happens on
        # explicit request (the HUD fires syncplay-refresh when its
        # SyncPlay sheet opens); the result is cached for later builds.
        self._syncplay_groups = []

    # ------------------------------------------------------------- state

    def build_state(self):
        """The full menu/track state blob (tracks with selection,
        quality, sub style, profiles, SyncPlay, favorite, queue) the
        HUD renders its pickers and gear menu from. May raise —
        callers guard."""
        return self._build_state()

    def _build_state(self):
        pm = self.playerManager
        state = {
            "strings": self._strings(),
            "has_media": False,
        }

        video = pm.get_video()
        if video is None or video.media_source is None:
            return state

        state["has_media"] = True
        state["allow_screenshot"] = bool(settings.screenshot_menu)
        state["favorite"] = bool(
            ((getattr(video, "item", None) or {}).get("UserData") or {})
            .get("IsFavorite")
        )
        parent = getattr(video, "parent", None)
        state["queue"] = {
            "has_prev": bool(getattr(parent, "has_prev", False)),
            "has_next": bool(getattr(parent, "has_next", False)),
        }
        state["subtitles"] = self._subtitle_streams(video)
        state["audio"] = self._audio_streams(video)
        state["quality"] = self._quality(video)
        state["sub_style"] = self._sub_style()
        profiles = self._profiles()
        if profiles is not None:
            state["profiles"] = profiles
        syncplay = self._syncplay()
        if syncplay is not None:
            state["syncplay"] = syncplay
        return state

    @staticmethod
    def _strings():
        # Localized fixed strings used by the Lua side.
        return {
            "off": _("None"),
            "none": _("None"),
            "unknown": _("unknown"),
            "ends_at": _("Ends at {0}").format("%s"),
            "quality": _("Change Video Quality"),
            "speed": _("Playback Speed"),
            "aspect": _("Aspect Ratio"),
            "aspect_auto": _("Auto"),
            "profile": _("Change Video Playback Profile"),
            "stats": _("Playback Data"),
            "screenshot": _("Screenshot"),
            "unwatched": _("Quit and Mark Unwatched"),
            "sub_size": _("Subtitle Size"),
            "sub_position": _("Subtitle Position"),
            "sub_color": _("Subtitle Color"),
            "sp_disabled": _("None (Disabled)"),
            "sp_new": _("New Group"),
        }

    def _subtitle_streams(self, video):
        selected_sid = video.sid
        items = [{
            "id": -1,
            "label": _("None"),
            "selected": selected_sid is None or selected_sid == -1,
        }]
        for stream in (video.media_source.get("MediaStreams") or []):
            if stream.get("Type") != "Subtitle":
                continue
            sid = stream.get("Index")
            if (
                settings.lang_filter_sub
                and sid != selected_sid
                and stream.get("Language") not in lang_filter
            ):
                continue
            item = {
                "id": sid,
                "label": get_sub_display_title(stream),
                "selected": sid == selected_sid,
            }
            if sid in video.subtitle_enc:
                item["aside"] = _("Transcode")
            elif sid in video.subtitle_url:
                item["aside"] = _("External")
            items.append(item)
        return items

    def _audio_streams(self, video):
        selected_aid = video.aid
        items = []
        for stream in (video.media_source.get("MediaStreams") or []):
            if stream.get("Type") != "Audio":
                continue
            aid = stream.get("Index")
            if (
                settings.lang_filter_audio
                and aid != selected_aid
                and stream.get("Language") not in lang_filter
            ):
                continue
            items.append({
                "id": aid,
                "label": stream.get("DisplayTitle")
                or stream.get("Title")
                or _("unknown"),
                "selected": aid == selected_aid,
            })
        return items

    @staticmethod
    def _options(pairs, current):
        return [
            {"id": value, "label": label, "selected": value == current}
            for label, value in pairs
        ]

    def _quality(self, video):
        current = video.get_transcode_bitrate()
        pairs = [(_("No Transcode"), "none"), (_("Maximum"), "max")]
        pairs.extend(TRANSCODE_LEVELS)
        options = self._options(pairs, current)
        current_label = next(
            (o["label"] for o in options if o["selected"]), _("No Transcode")
        )
        return {"current": current_label, "options": options}

    def _sub_style(self):
        size = self._options(SIZE_LIST, settings.subtitle_size)
        color = self._options(COLOR_LIST, settings.subtitle_color)
        position = self._options(
            [(_("Bottom"), "bottom"), (_("Top"), "top"), (_("Middle"), "middle")],
            settings.subtitle_position,
        )

        def group(options, fallback):
            return {
                "current": next(
                    (o["label"] for o in options if o["selected"]), fallback
                ),
                "options": options,
            }

        return {
            "size": group(size, str(settings.subtitle_size)),
            "position": group(position, settings.subtitle_position),
            "color": group(color, settings.subtitle_color),
        }

    def _profiles(self):
        menu = self.playerManager.menu
        manager = menu.profile_manager if menu is not None else None
        if manager is None:
            return None
        pairs = [(_("None (Disabled)"), "none")]
        for profile_name, profile in manager.profiles.items():
            if (
                profile.get("subtype") is not None
                and settings.shader_pack_subtype not in profile["subtype"]
            ):
                continue
            pairs.append((profile["displayname"], profile_name))
        current = manager.current_profile or "none"
        options = self._options(pairs, current)
        current_label = next(
            (o["label"] for o in options if o["selected"]), _("None (Disabled)")
        )
        return {"current": current_label, "options": options}

    def _syncplay(self):
        syncplay = self.playerManager.syncplay
        if syncplay is None:
            return None
        enabled = syncplay.is_enabled()
        groups = []
        for group in self._syncplay_groups:
            groups.append({
                "id": group["GroupId"],
                "label": group["GroupName"],
                "selected": group["GroupId"] == syncplay.current_group,
            })
        return {
            "enabled": enabled,
            "current": _("SyncPlay Enabled") if enabled else _("None (Disabled)"),
            "groups": groups,
        }

    # ----------------------------------------------------------- actions

    def handle_action(self, args):
        """Dispatch a HUD action verb ([verb, arg?]).

        Anything that touches playback state is queued onto the action
        thread via put_task, mirroring how menu.py handlers behave. The
        HUD re-reads build_state() on its next repaint, so no push is
        needed once the change lands.
        """
        if not args:
            return
        verb, arg = args[0], (args[1] if len(args) > 1 else None)
        pm = self.playerManager
        try:
            if verb == "set-sub":
                pm.put_task(pm.set_streams, None, int(arg))
            elif verb == "set-audio":
                pm.put_task(pm.set_streams, int(arg), None)
            elif verb == "set-quality":
                pm.put_task(self._set_quality, arg)
            elif verb in ("set-sub-size", "set-sub-position", "set-sub-color"):
                pm.put_task(self._set_sub_style, verb, arg)
            elif verb == "set-profile":
                pm.put_task(self._set_profile, arg)
            elif verb == "syncplay-refresh":
                pm.put_task(self._refresh_syncplay)
            elif verb == "syncplay-join":
                pm.put_task(self._syncplay_join, arg)
            elif verb == "syncplay-new":
                pm.put_task(self._syncplay_new)
            elif verb == "syncplay-disable":
                pm.put_task(self._syncplay_disable)
            elif verb == "toggle-favorite":
                pm.put_task(self._toggle_favorite)
            elif verb == "next-item":
                pm.put_task(pm.play_next)
            elif verb == "prev-item":
                pm.put_task(pm.play_prev)
            elif verb == "skip-segment":
                pm.put_task(pm.skip_intro)
            elif verb == "set-fullscreen":
                # The HUD already toggled mpv's fullscreen locally; this
                # only records the user's intent so auto-fullscreen
                # (fullscreen_disable) doesn't re-fullscreen the next
                # episode. No timeline push is needed.
                pm.put_task(pm.set_fullscreen, arg == "yes", True)
                return
            elif verb == "screenshot":
                pm.put_task(pm.screenshot)
            elif verb == "unwatched-quit":
                pm.put_task(pm.unwatched_quit)
            else:
                log.warning("Unknown OSC action %r", verb)
                return
            pm.timeline_handle()
        except Exception:
            log.error("Error handling OSC action %r", args, exc_info=True)

    def _set_quality(self, arg):
        # Runs on the action thread (queued by handle_action).
        pm = self.playerManager
        video = pm.get_video()
        if video is None:
            return
        if arg == "none":
            video.set_trs_override(None, False)
        elif arg == "max":
            video.set_trs_override(None, True)
        else:
            video.set_trs_override(int(arg), True)
        pm.restart_playback()

    def _set_sub_style(self, verb, arg):
        pm = self.playerManager
        key = {
            "set-sub-size": "subtitle_size",
            "set-sub-position": "subtitle_position",
            "set-sub-color": "subtitle_color",
        }[verb]
        value = int(arg) if key == "subtitle_size" else arg
        setattr(settings, key, value)
        settings.save()
        # Size changes during a transcode need the server-rendered size
        # re-requested; everything else is applied locally (menu.py does
        # the same, but from the action thread we always queue it).
        pm.put_task(pm.update_subtitle_visuals)

    def _set_profile(self, arg):
        menu = self.playerManager.menu
        manager = menu.profile_manager if menu is not None else None
        if manager is None:
            return
        if arg == "none":
            manager.unload_profile()
            success = True
            profile_name = None
        else:
            profile_name = arg
            success = manager.load_profile(profile_name)
        if settings.shader_pack_remember and success:
            settings.shader_pack_profile = profile_name
            settings.save()

    def _toggle_favorite(self):
        # Runs on the action thread (network I/O).
        pm = self.playerManager
        video = pm.get_video()
        if video is None or getattr(video, "item", None) is None:
            return
        item_id = video.item.get("Id")
        if not item_id:
            return
        user_data = video.item.setdefault("UserData", {})
        target = not user_data.get("IsFavorite")
        try:
            video.client.jellyfin.favorite(item_id, target)
        except Exception:
            log.error("Could not toggle favorite.", exc_info=True)
            return
        user_data["IsFavorite"] = target

    # SyncPlay helpers all run on the action thread (network I/O).

    def _syncplay_client(self):
        client = self.playerManager.get_current_client()
        self.playerManager.syncplay.client = client
        return client

    def _refresh_syncplay(self):
        # The HUD's open sheet picks the refreshed groups up on its
        # next periodic rebuild.
        try:
            client = self._syncplay_client()
            self._syncplay_groups = client.jellyfin.get_sync_play() or []
        except Exception:
            log.error("Could not fetch SyncPlay groups.", exc_info=True)
            self._syncplay_groups = []

    def _syncplay_join(self, group_id):
        client = self._syncplay_client()
        client.jellyfin.join_sync_play(group_id)

    def _syncplay_new(self):
        from .clients import clientManager

        client = self._syncplay_client()
        client.jellyfin.new_sync_play_v2(
            _("{0}'s Group").format(
                clientManager.get_username_from_client(client)
            )
        )

    def _syncplay_disable(self):
        client = self._syncplay_client()
        client.jellyfin.leave_sync_play()
