"""The playback HUD's data and its option writes.

Split out of the single 1,154-line ``PlayerGateway``; see
``gateway/__init__.py`` for why the facade is composed rather than nested.
"""

import logging

from ...conf import settings
from .base import GatewayCore

log = logging.getLogger("mpvtk_browser.gateway.hud")


class HudMixin(GatewayCore):
    def use_hud(self):
        """Whether video playback uses the in-window playback HUD.
        Reads the player's RESOLVED style (settings may hold the legacy
        "jellyfin" alias, and fallbacks may have applied)."""
        from ...player import playerManager
        return getattr(playerManager, "_osc_style_resolved",
                       None) == "mpvtk"

    def hud_key_opts(self):
        """Everything the renderer owns about the HUD, sent with the engage.

        Keyboard policy ("grab"/"key"): by default only hud_wake_key is taken
        over during playback so mpv's own seek keys keep working.

        Auto-hide policy ("hide"/"mode") and the no-scrim text halo
        ("shadow") ride the same message, because the renderer owns the
        summon/hide lifecycle and draws the glyphs -- and because engage
        re-sends them, which is what makes a settings change stick without a
        restart.
        """
        return {"grab": bool(settings.hud_grab_keys),
                "key": settings.hud_wake_key or "ENTER",
                "hide": max(0.0, float(settings.hud_hide_secs or 0)),
                "mode": settings.hud_autohide or "hover",
                "shadow": settings.hud_scrim == "none",
                # Whether the hidden HUD takes the left button at all. It
                # rides this message rather than being read at startup for
                # the same reason the rest do: engage() re-sends them, which
                # is what makes the setting apply without a restart.
                "click": bool(settings.mouse_click_pauses),
                # Whether a pause the RENDERER performs has to go through
                # Python. Normally it does not -- `cycle pause` locally is
                # what makes click-to-pause feel immediate -- but in a
                # SyncPlay group a local pause is not a pause, it is a
                # desync the group then has to correct. Same shape as
                # `click` above, and re-sent by engage(), which is what
                # makes joining a group take effect without a restart.
                "syncplay": self._syncplay_active()}

    @staticmethod
    def _syncplay_active():
        from ...player import playerManager

        try:
            return bool(playerManager.syncplay.is_enabled())
        except Exception:
            return False

    def hud_menu_state(self):
        """osc_bridge's menu/track state blob for the HUD's pickers
        (audio/subtitles with selection, quality, …), or None."""
        from ...player import playerManager
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
        from ...player import playerManager
        args = [verb] if arg is None else [verb, str(arg)]
        playerManager.osc_bridge.handle_action(args)

    def get_speed(self):
        """Current playback speed (1.0 when unknown)."""
        from ...player import playerManager
        try:
            return float(playerManager._player.speed or 1.0)
        except Exception:
            return 1.0

    def set_speed(self, speed):
        self._act(lambda pm: setattr(pm._player, "speed", float(speed)))

    def get_aspect(self):
        """Current video-aspect-override (-1.0 = auto/unknown)."""
        from ...player import playerManager
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
        """Toggle mpv's stats overlay (the gear menu's Playback Data).
        Goes through the player's tracked toggle so the overlay is cleared
        when the library returns (see on_browse_enter -> clear_stats)."""
        self._act(lambda pm: pm.toggle_stats())

    def playback_info(self):
        """What the playback-info panel shows, or None with nothing playing.

        **Not on the playstate snapshot**, unlike everything else the HUD
        draws: that blob is pushed on every position tick and this carries a
        whole MediaSource. It is read once when the panel opens instead,
        which is also when it is true -- none of it can change without
        playback restarting.

        Every field is already decided and sitting on the Video. We do not
        ask the server what it is doing with our session the way
        jellyfin-web has to (``/Sessions`` -> ``TranscodingInfo``, polled):
        the decision was *ours*, taken in
        ``media.Video._get_url_from_source``, so this is an attribute read
        rather than a request per second. The cost is that live transcode
        progress and encoder fps are not available, which is the trade
        recorded in docs/UI_FIXES_4.md §10.
        """
        from ...player import playerManager
        try:
            video = playerManager.get_video()
        except Exception:
            log.debug("could not read the playing video", exc_info=True)
            return None
        if video is None:
            return None
        item = getattr(video, "item", None) or {}
        return {
            "title": item.get("Name") or "",
            "item_type": item.get("Type") or "",
            "media_type": item.get("MediaType") or "",
            # None until a url has been asked for, which is every state the
            # panel can be opened in -- but the panel must not assume it.
            "play_method": getattr(video, "play_method", None),
            "transcode_reasons": list(
                getattr(video, "transcode_reasons", None) or ()),
            "direct_path": bool(getattr(video, "direct_path", False)),
            "offline": getattr(video, "client", None) is None,
            # The source actually chosen, not MediaSources[0]: an item with
            # several versions would otherwise describe the wrong file.
            "source": getattr(video, "media_source", None) or {},
            "aid": getattr(video, "aid", None),
            "sid": getattr(video, "sid", None),
        }

    #: mpv properties the playback-info panel shows, as
    #: ``(property, key)``. Deliberately short: this is the half of mpv's
    #: own stats.lua overlay that answers a question a *viewer* has --
    #: why is it stuttering, why is it buffering, is my GPU being used --
    #: rather than the half that answers one a developer has.
    #:
    #: **The overlay itself is not a substitute, which is the point.** It is
    #: ASS OSD and our HUD is overlay bitmaps, and bitmaps composite above
    #: all script ASS (mpvtk GUIDE 6) -- so mpv's stats have always been
    #: drawn *behind* the very controls you are reading them from. Moving
    #: the useful rows into a panel we draw is the only way they are
    #: legible while the OSC is up. `i` still opens mpv's, for the rest.
    _MPV_STATS = (
        ("hwdec-current", "hwdec"),
        ("current-vo", "vo"),
        ("estimated-vf-fps", "fps"),
        ("frame-drop-count", "drops_vo"),
        ("decoder-frame-drop-count", "drops_dec"),
        ("avsync", "avsync"),
        ("demuxer-cache-duration", "buffered"),
        ("cache-speed", "cache_speed"),
    )

    def player_stats(self):
        """Live mpv counters for the playback-info panel, ``{}`` when idle.

        Read per build rather than pushed on the playstate snapshot: the
        panel is only open for seconds at a time, and these would otherwise
        ride every 1s tick for the whole of every playback. While it *is*
        open the HUD's own ticker rebuilds about once a second, which is
        also the rate at which these are worth re-reading.

        Every property is fetched independently and a failure is dropped
        rather than raised: they are not all present on every mpv, on every
        backend, or at every moment (there is no ``estimated-vf-fps``
        before the first frame, and none of the video ones during audio),
        and a panel that shows nothing because one counter was missing is
        worse than one that shows seven rows.
        """
        from ...player import playerManager
        player = getattr(playerManager, "_player", None)
        if player is None or not getattr(playerManager, "_mpv_alive", True):
            return {}
        out = {}
        for prop, key in self._MPV_STATS:
            try:
                # Both backends turn an unknown attribute into a property
                # read, which is also why a typo here would be silent -- it
                # raises, gets dropped below, and the row simply never
                # appears. tests/test_mpv_stat_properties.py checks every
                # name against `mpv --list-properties` for that reason.
                value = getattr(player, prop.replace("-", "_"))
            except Exception:
                continue
            if value is not None:
                out[key] = value
        return out

    def toggle_night_mode(self):
        """Night mode on/off from the playback HUD's gear menu. Applies to
        what is playing right now — no reload."""
        self._act(lambda pm: pm.set_night_mode(not settings.audio_night_mode))

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

    def chapter_seek(self, direction):
        """Previous (-1) / next (+1) chapter, by the player's rule.

        The HUD used to work its own target out from the chapter list it had
        already fetched and seek there. That was a second definition of what
        "previous chapter" means, and it went round SyncPlay -- see
        PlayerManager.chapter_seek, which is now the only one.
        """
        self._act(lambda pm: pm.chapter_seek(direction))

    def chapters(self):
        """mpv's chapter list as [{"title", "time"}], [] when none."""
        from ...player import playerManager
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
