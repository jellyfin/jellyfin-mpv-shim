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

import logging

from ..i18n import _
from ..mpvtk.widgets import (
    Button,
    Column,
    Dropdown,
    Gradient,
    Image,
    Row,
    Slider,
    Spacer,
    Stack,
    Text,
)

log = logging.getLogger("mpvtk_browser.hud")

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


def _trickplay_frame(b, secs):
    """Scrub preview bitmap for ``secs``, via the TrickPlay worker's
    decoded raw-BGRA tile file (player.trickplay_meta). Returns a
    strips.bitmap entry {"src", "iw", "ih"} or None (no trickplay data
    for this video / frame not readable)."""
    get = getattr(b.controller, "trickplay", None)
    if get is None or b.strips is None:
        return None
    try:
        meta = get()
    except Exception:
        return None
    if not meta or not meta.get("count"):
        return None
    w, h = meta["width"], meta["height"]
    idx = max(0, min(meta["count"] - 1,
                     int(secs * 1000 / max(1, meta["multiplier"]))))
    key = ("trickplay", meta["file"], meta["count"],
           meta["multiplier"], idx)
    # one-slot cache: repaints at the same scrub index (1s ticker while
    # holding still) skip the file read + decode
    last = getattr(b, "_hud_frame", None)
    if last is not None and last[0] == key:
        return last[1]
    frame = w * h * 4
    try:
        with open(meta["file"], "rb") as fh:
            fh.seek(idx * frame)
            data = fh.read(frame)
    except OSError:
        return None
    if len(data) < frame:
        return None
    try:
        from PIL import Image as PILImage

        img = PILImage.frombytes("RGBA", (w, h), data, "raw", "BGRA")
    except Exception:
        log.debug("trickplay frame decode failed", exc_info=True)
        return None
    entry = b.strips.bitmap(key, img)
    b._hud_frame = (key, entry)
    return entry


def _hud_action(b, verb, arg=None):
    b._ctl(lambda c: c.hud_action(verb, arg))


def _option_picker(b, node_id, icon, tip, options, verb):
    """Icon-trigger dropdown over osc_bridge option dicts
    ([{id, label, selected}]); selecting routes through hud_action so
    the change lands exactly like the lua OSC's menus."""
    sel = next((i for i, o in enumerate(options) if o.get("selected")), 0)
    return Dropdown(
        node_id, [o.get("label") or "" for o in options], selected=sel,
        force=True, trigger_icon=icon, tip=tip,
        on_select=lambda i, v, opts=options: _hud_action(
            b, verb, opts[i]["id"]))


def _chapters(b):
    if b.controller is None or not hasattr(b.controller, "chapters"):
        return []
    try:
        return b.controller.chapters() or []
    except Exception:
        return []


def _chapter_jump(b, chapters, pos, direction):
    """Seek to the previous/next chapter start (the lua OSC's
    ch_prev/ch_next). Prev re-seeks the current chapter's start unless
    pressed within its first 2 seconds, like mpv's 'add chapter -1'."""
    if direction < 0:
        target = 0.0
        for ch in chapters:
            if ch["time"] < pos - 2.0:
                target = ch["time"]
        b._ctl(lambda c: c.seek(target))
        return
    for ch in chapters:
        if ch["time"] > pos + 0.5:
            b._ctl(lambda c, t=ch["time"]: c.seek(t))
            return


def _pickers(b, menu_state, pos, chapters, tiers):
    """Right-aligned controls: chapters, audio/subtitle tracks, quality
    — each only when there is a real choice to make (and the viewport
    has room for it)."""
    out = []
    if chapters and tiers["chapters"]:
        cur = 0
        for i, ch in enumerate(chapters):
            if ch["time"] <= pos:
                cur = i
        labels = [
            "%s  %s" % (_clock(ch["time"]),
                        ch["title"] or _("Chapter %d") % (i + 1))
            for i, ch in enumerate(chapters)
        ]
        out.append(Dropdown(
            "hud-chapters", labels, selected=cur, force=True,
            trigger_icon="bookmark", tip=_("Chapters"),
            on_select=lambda i, v, chs=chapters: b._ctl(
                lambda c: c.seek(chs[i]["time"]))))
    st = menu_state if menu_state and menu_state.get("has_media") else None
    if st is None:
        return out
    audio = st.get("audio") or []
    if len(audio) > 1:
        out.append(_option_picker(b, "hud-audio", "audiotrack",
                                  _("Audio Track"), audio, "set-audio"))
    subs = st.get("subtitles") or []
    if len(subs) > 1:  # more than just "None"
        out.append(_option_picker(b, "hud-sub", "closed_caption",
                                  _("Subtitle Track"), subs, "set-sub"))
    quality = st.get("quality") or {}
    if quality.get("options") and tiers["quality"]:
        out.append(_option_picker(b, "hud-quality", "hd",
                                  _("Video Quality"), quality["options"],
                                  "set-quality"))
    return out


def _skip_float(b, size):
    """Floating Skip Intro / Skip Credits button above the bar's right
    edge (jellyfin-web's placement), when the player says a skippable
    segment is live (playstate skip_label)."""
    label = (b._hud_state or {}).get("skip_label")
    if not label:
        return None
    rect = None
    if b.app is not None and hasattr(b.app, "node_rect"):
        rect = b.app.node_rect("hud-seek")
    if rect is None:
        return None
    return Button(
        label, id="hud-skip", size=18, bg="eeeeee", fg="111111",
        hover={"fill": "ffffff"},
        on_click=lambda: _hud_action(b, "skip-segment"),
        anchor="ne", dx=-24, dy=rect["y"] - 56)


def build_hud(b, size):
    """The summoned HUD scene. ``b`` is the Browser (playstate snapshot,
    scrub state, controller plumbing); returns the full-window tree."""
    w, h = size
    st = b._hud_state or {}
    pos = st.get("position", 0) or 0
    dur = st.get("duration", 0) or 0
    pp = "play_arrow" if st.get("paused") else "pause"
    scrub = b._hud_scrub
    chapters = _chapters(b)

    # Responsive shrink, mirroring the lua OSC's jellyfin layout:
    # everything scales down to 72% as the window narrows, and the
    # less essential controls drop out at breakpoints (in the spirit
    # of jellyfin-web's).
    scale = min(1.0, max(0.72, w / 900.0))

    def sz(v):
        return int(v * scale + 0.5)

    tiers = {
        "seek_btns": w >= 500,   # ±10s/±30s step buttons
        "clock": w >= 500,
        "quality": w >= 560,
        "ch_btns": w >= 700,     # chapter prev/next buttons
        "chapters": w >= 700,    # chapter list dropdown
    }

    def tbtn(icon, node_id, cb, autofocus=False, icon_size=30, tip=None,
             repeat=False):
        return Button("", id=node_id, icon=icon, flat=True,
                      icon_size=sz(icon_size), autofocus=autofocus,
                      tip=tip, repeat=repeat, on_click=cb)

    # Scrub semantics: 'change' only moves the preview + clock; the seek
    # happens once on 'commit' (drag release / adjust-mode exit), so
    # scrubbing never spams seeks at a transcode. ESC/focus-away cancels.
    seek = Slider(
        "hud-seek", value=pos, min=0, max=max(1.0, dur),
        force=True, flex=1, h=26,
        marks=([ch["time"] / dur for ch in chapters if 0 < ch["time"] < dur]
               if dur > 0 else None),
        on_change=b._hud_scrub_change,
        on_commit=b._hud_scrub_commit,
        on_cancel=b._hud_scrub_cancel)

    menu_state = None
    if b.controller is not None and hasattr(b.controller, "hud_menu_state"):
        try:
            menu_state = b.controller.hud_menu_state()
        except Exception:
            menu_state = None

    controls = [
        tbtn("skip_previous", "hud-prev",
             lambda: b._ctl(lambda c: c.prev()), tip=_("Previous")),
    ]
    if chapters and tiers["ch_btns"]:
        controls.append(tbtn(
            "undo", "hud-ch-prev",
            lambda: _chapter_jump(b, chapters, pos, -1),
            tip=_("Previous Chapter")))
    if tiers["seek_btns"]:
        controls.append(tbtn(
            "replay_10", "hud-seek-back",
            lambda: b._ctl(lambda c: c.seek_relative(-10)),
            tip=_("Back 10 Seconds"), repeat=True))
    controls.append(tbtn(
        pp, "hud-pp", lambda: b._ctl(lambda c: c.toggle_pause()),
        autofocus=True, icon_size=36))
    if tiers["seek_btns"]:
        controls.append(tbtn(
            "forward_30", "hud-seek-fwd",
            lambda: b._ctl(lambda c: c.seek_relative(30)),
            tip=_("Forward 30 Seconds"), repeat=True))
    if chapters and tiers["ch_btns"]:
        controls.append(tbtn(
            "redo", "hud-ch-next",
            lambda: _chapter_jump(b, chapters, pos, 1),
            tip=_("Next Chapter")))
    controls.append(tbtn(
        "skip_next", "hud-next",
        lambda: b._ctl(lambda c: c.next()), tip=_("Next")))
    controls.append(tbtn(
        "stop", "hud-stop",
        lambda: b._ctl(lambda c: c.stop()), tip=_("Stop")))
    shown_pos = pos if scrub is None else scrub
    if tiers["clock"]:
        controls.append(Text(
            "%s / %s" % (_clock(shown_pos), _clock(dur)), size=sz(15),
            color="ffffff" if scrub is not None else "dddddd"))
    controls.append(Spacer())

    transport = Row(
        controls + _pickers(b, menu_state, pos, chapters, tiers),
        gap=sz(6), align="center")

    bar = Column(
        [
            Text(st.get("title") or "", size=sz(17), bold=True),
            # the Slider has a fixed default width, so stretch can't
            # touch it directly: an unsized Row wrapper stretches to the
            # column width and flex=1 spreads the slider inside it
            Row([seek], align="center"),
            transport,
        ],
        gap=sz(6), pad=(sz(24), sz(14)), w=w, anchor="s",
        align="stretch")

    children = [
        Gradient(color="000000", top=0, bottom=215, w=w,
                 h=min(int(h * SCRIM_FRAC), SCRIM_MAX), anchor="sw"),
        bar,
    ]

    skip = _skip_float(b, size)
    if skip is not None:
        children.append(skip)

    preview = _preview_float(b, scrub, dur, size)
    if preview is not None:
        children.append(preview)

    return Stack(children, w=w, h=h)


# Keep in sync with renderer.lua's SLIDER_PAD (track inset inside the
# slider node — the thumb travels between the insets).
_SLIDER_PAD = 8


def _preview_float(b, scrub, dur, size):
    """Trickplay thumbnail floated above the slider's scrub position
    (or None when not scrubbing / no data). Geometry comes from the
    previous scene's laid-out slider rect — one frame stale, which is
    fine: the bar doesn't move while the HUD is up."""
    if scrub is None or dur <= 0:
        return None
    entry = _trickplay_frame(b, scrub)
    if entry is None:
        return None
    rect = None
    if b.app is not None and hasattr(b.app, "node_rect"):
        rect = b.app.node_rect("hud-seek")
    if rect is None:
        return None
    w, _h = size
    iw, ih = entry["iw"], entry["ih"]
    frac = max(0.0, min(1.0, scrub / dur))
    track_x = rect["x"] + _SLIDER_PAD
    track_w = rect["w"] - 2 * _SLIDER_PAD
    px = track_x + frac * track_w - iw / 2
    px = max(8, min(w - iw - 8, px))
    py = rect["y"] - ih - 10
    return Image(entry["src"], iw, ih, id="hud-preview",
                 anchor="nw", dx=px, dy=py)
