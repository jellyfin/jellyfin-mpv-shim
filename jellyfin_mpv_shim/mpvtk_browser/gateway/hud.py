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
                "shadow": settings.hud_scrim == "none"}

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
