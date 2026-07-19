"""Playback HUD — YouTube-on-TV style controls inside the mpv window.

Rendered by the browser while it is yielded to video playback, via the
renderer's attached-but-idle lifecycle (``mpvtk-hud``): playback runs
clean until an arrow key / ENTER / mouse motion summons the HUD, and
~4s without input hides it again (both renderer-side; see
renderer.lua). The browser owns the summoned flag (``_hud_shown``) and
calls :func:`build_hud` from ``build()``; playstate comes from the
same ``push_playstate`` snapshots that feed the audio now-playing bar,
kept fresh by the shared 1s ticker.

Replaces trickplay-jf-osc.lua when ``osc_style`` is ``"mpvtk"`` (the
lua OSC stays selectable until this is field-proven — MIGRATION.md
Phase 9).
"""

from ..i18n import _
from ..mpvtk.widgets import (
    Button,
    Column,
    Gradient,
    Row,
    Slider,
    Spacer,
    Stack,
    Text,
)

# Scrim geometry: the renderer's gradient is solid-ish below the fade
# midpoint at ~h/2.2 from its bottom edge, so the scrim must be ~2.2x
# the bar's height for the title/slider to sit on dark instead of the
# ramp's transparent half. Capped by a window fraction so short
# windows keep most of the picture clean.
SCRIM_FRAC = 0.55
SCRIM_MAX = 380


def _clock(secs):
    secs = int(secs or 0)
    if secs >= 3600:
        return "%d:%02d:%02d" % (
            secs // 3600, (secs % 3600) // 60, secs % 60)
    return "%d:%02d" % (secs // 60, secs % 60)


def build_hud(b, size):
    """The summoned HUD scene. ``b`` is the Browser (playstate snapshot,
    controller plumbing); returns the full-window element tree."""
    w, h = size
    st = b._hud_state or {}
    pos = st.get("position", 0) or 0
    dur = st.get("duration", 0) or 0
    pp = "play_arrow" if st.get("paused") else "pause"

    def tbtn(icon, node_id, cb, autofocus=False, icon_size=30, tip=None):
        return Button("", id=node_id, icon=icon, flat=True,
                      icon_size=icon_size, autofocus=autofocus,
                      tip=tip, on_click=cb)

    seek = Slider(
        "hud-seek", value=pos, min=0, max=max(1.0, dur),
        force=True, flex=1, h=26,
        on_change=lambda v: b._ctl(lambda c: c.seek(v)))

    transport = Row(
        [
            tbtn("skip_previous", "hud-prev",
                 lambda: b._ctl(lambda c: c.prev()), tip=_("Previous")),
            tbtn(pp, "hud-pp",
                 lambda: b._ctl(lambda c: c.toggle_pause()),
                 autofocus=True, icon_size=36),
            tbtn("skip_next", "hud-next",
                 lambda: b._ctl(lambda c: c.next()), tip=_("Next")),
            tbtn("stop", "hud-stop",
                 lambda: b._ctl(lambda c: c.stop()), tip=_("Stop")),
            Text("%s / %s" % (_clock(pos), _clock(dur)), size=15,
                 color="dddddd"),
            Spacer(),
            # 9.3: audio/subtitle pickers, chapters, quality (osc_bridge)
        ],
        gap=6, align="center")

    bar = Column(
        [
            Text(st.get("title") or "", size=17, bold=True),
            seek,
            transport,
        ],
        gap=6, pad=(24, 14), w=w, anchor="s")

    return Stack(
        [
            Gradient(color="000000", top=0, bottom=215, w=w,
                     h=min(int(h * SCRIM_FRAC), SCRIM_MAX), anchor="sw"),
            bar,
        ],
        w=w, h=h)
