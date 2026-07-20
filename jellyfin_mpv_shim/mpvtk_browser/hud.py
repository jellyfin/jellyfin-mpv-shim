"""Playback HUD — YouTube-on-TV style controls inside the mpv window.

Rendered by the browser while it is yielded to video playback, via the
renderer's attached-but-idle lifecycle (``mpvtk-hud``): playback runs
clean until an arrow key / ENTER / mouse motion summons the HUD, and
~4s without input hides it again (both renderer-side; see
renderer.lua). The browser owns the summoned flag (``_hud_shown``) and
calls :func:`build_hud` from ``build()``; playstate comes from the
same ``push_playstate`` snapshots that feed the audio now-playing bar,
kept fresh by the shared 1s ticker.

This IS the jellyfin-styled player UI (``osc_style: mpvtk``, the
default) — it replaced the retired trickplay-jf-osc.lua at feature
parity (MIGRATION.md Phase 9).
"""

import logging
import time

from ..i18n import _
from ..mpvtk.widgets import (
    Box,
    Button,
    Column,
    Dropdown,
    Gradient,
    Image,
    Menu,
    Row,
    Slider,
    Spacer,
    Stack,
    Text,
)
from . import theme

log = logging.getLogger("mpvtk_browser.hud")

# Scrim geometry: the renderer's gradient is solid-ish below the fade
# midpoint at ~h/2.2 from its bottom edge, so the scrim must be ~2.2x
# the bar's height for the title/slider to sit on dark instead of the
# ramp's transparent half. Capped by a window fraction so short
# windows keep most of the picture clean.
SCRIM_FRAC = 0.55
SCRIM_MAX = 380


def _episode_context(st):
    """``"Series   ·   S1E2"`` for an episode, ``""`` for anything else.

    The old lua OSC got this free from mpv's media-title, which the shim
    sets to ``Media.get_proper_title()`` ("Show - s1e02 - Name"). The mpvtk
    HUD reads the playstate instead, which carried only the item's own name
    — so an episode showed "Pilot" with no clue which show it belonged to.

    Either part alone is still worth showing: a season/episode number with
    no series, or a series whose numbering the server doesn't have.
    """
    if not st:
        return ""
    season, episode = st.get("season"), st.get("episode")
    se = ("S%sE%s" % (season, episode)
          if season is not None and episode is not None else "")
    return "   ·   ".join(p for p in (st.get("series_name"), se) if p)


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


# ------------------------------------------------- settings gear menu
# The lua OSC's jf_settings_sheet, rebuilt on the Menu widget. One
# level open at a time (b._hud_menu names it); submenus swap the item
# list in place, with a Back row for keyboard/remote users. Leaf
# actions route through hud_action verbs where osc_bridge has one;
# speed/aspect/stats set mpv properties via the controller, exactly
# like the lua sheet does locally.

_SPEEDS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
_ASPECTS = [
    (None, -1.0, "-1"),          # label filled with _("Auto")
    ("16:9", 16.0 / 9.0, "16:9"),
    ("4:3", 4.0 / 3.0, "4:3"),
    ("2.35:1", 2.35, "2.35:1"),
]


def _open_hud_menu(b, kind, anchor=None):
    """``anchor`` names the button node the menu hangs off (gear or the
    top bar's SyncPlay button); omitted on submenu/Back transitions so
    the menu stays where it opened."""
    if kind == "syncplay":
        # group discovery hits the server; request it once on open (the
        # result lands in a later build via osc_bridge's cache)
        _hud_action(b, "syncplay-refresh")
    if anchor is not None:
        b._hud_menu_anchor = anchor
    b._hud_menu = kind
    b.invalidate()


def _close_hud_menu(b):
    b._hud_menu = None
    b.invalidate()


def _ctl_get(b, name, default):
    fn = getattr(b.controller, name, None)
    if fn is None:
        return default
    try:
        value = fn()
        return default if value is None else value
    except Exception:
        return default


def _menu_rows(b, st):
    """(label, icon, action) rows for the open settings-menu level.
    ``st`` is the osc_bridge state blob ({} when unavailable)."""
    kind = b._hud_menu
    rows = []

    def leaf(fn):
        def run():
            fn()
            _close_hud_menu(b)
        return run

    def option_rows(group, verb):
        for o in (group or {}).get("options") or []:
            rows.append((
                o.get("label") or "",
                "check" if o.get("selected") else None,
                leaf(lambda oid=o.get("id"): _hud_action(b, verb, oid)),
            ))

    def with_current(label, current):
        return "%s  ·  %s" % (label, current) if current else label

    sub_style = st.get("sub_style") or {}
    if kind == "root":
        quality = st.get("quality") or {}
        if quality.get("options"):
            rows.append((with_current(_("Change Video Quality"),
                                      quality.get("current")), None,
                         lambda: _open_hud_menu(b, "quality")))
        speed = float(_ctl_get(b, "get_speed", 1.0))
        rows.append((with_current(_("Playback Speed"), "%gx" % speed),
                     None, lambda: _open_hud_menu(b, "speed")))
        rows.append((_("Aspect Ratio"), None,
                     lambda: _open_hud_menu(b, "aspect")))
        profiles = st.get("profiles") or {}
        if profiles.get("options"):
            rows.append((with_current(
                _("Change Video Playback Profile"),
                profiles.get("current")), None,
                lambda: _open_hud_menu(b, "profiles")))
        for key, label in (("size", _("Subtitle Size")),
                           ("position", _("Subtitle Position")),
                           ("color", _("Subtitle Color"))):
            group = sub_style.get(key)
            if group:
                rows.append((with_current(label, group.get("current")),
                             None,
                             lambda k=key: _open_hud_menu(b, "sub_" + k)))
        syncplay = st.get("syncplay")
        if syncplay is not None:
            rows.append((with_current(_("SyncPlay"),
                                      syncplay.get("current")), None,
                         lambda: _open_hud_menu(b, "syncplay")))
        rows.append((_("Playback Data"), None, leaf(
            lambda: b._ctl(lambda c: c.toggle_stats()))))
        if st.get("allow_screenshot"):
            rows.append((_("Screenshot"), None, leaf(
                lambda: _hud_action(b, "screenshot"))))
        if st.get("has_media"):
            rows.append((_("Quit and Mark Unwatched"), None, leaf(
                lambda: _hud_action(b, "unwatched-quit"))))
        return rows

    if getattr(b, "_hud_menu_anchor", None) != "hud-syncplay":
        # opened from the gear: submenus can step back to its root.
        # The top bar's SyncPlay button opens its sheet standalone
        # (like the lua OSC's drop-down), so no Back there.
        rows.append((_("Back"), "arrow_back",
                     lambda: _open_hud_menu(b, "root")))
    if kind == "quality":
        option_rows(st.get("quality"), "set-quality")
    elif kind == "speed":
        cur = float(_ctl_get(b, "get_speed", 1.0))
        for s in _SPEEDS:
            rows.append(("%gx" % s,
                         "check" if abs(cur - s) < 0.005 else None,
                         leaf(lambda s=s: b._ctl(
                             lambda c: c.set_speed(s)))))
    elif kind == "aspect":
        cur = float(_ctl_get(b, "get_aspect", -1.0))
        for label, num, value in _ASPECTS:
            rows.append((label or _("Auto"),
                         "check" if abs(cur - num) < 0.01 else None,
                         leaf(lambda v=value: b._ctl(
                             lambda c: c.set_aspect(v)))))
    elif kind == "profiles":
        option_rows(st.get("profiles"), "set-profile")
    elif kind in ("sub_size", "sub_position", "sub_color"):
        option_rows(sub_style.get(kind[4:]),
                    "set-" + kind.replace("_", "-"))
    elif kind == "syncplay":
        sp = st.get("syncplay") or {}
        rows.append((_("None (Disabled)"),
                     "check" if not sp.get("enabled") else None,
                     leaf(lambda: _hud_action(b, "syncplay-disable"))))
        if not sp.get("enabled"):
            rows.append((_("New Group"), None,
                         leaf(lambda: _hud_action(b, "syncplay-new"))))
        for g in sp.get("groups") or []:
            rows.append((g.get("label") or "",
                         "check" if g.get("selected") else None,
                         leaf(lambda gid=g.get("id"): _hud_action(
                             b, "syncplay-join", gid))))
    return rows


def _settings_menu(b, menu_state, size):
    """The open gear menu as a Menu node anchored at the gear button
    (renderer clamps to the screen and flips above near the bottom)."""
    if not b._hud_menu:
        return None
    st = menu_state if menu_state and menu_state.get("has_media") else {}
    rows = _menu_rows(b, st)
    if not rows:
        return None
    w, h = size
    x, y = w - 300, h - 160
    anchor = getattr(b, "_hud_menu_anchor", None) or "hud-settings"
    if b.app is not None and hasattr(b.app, "node_rect"):
        rect = b.app.node_rect(anchor)
        if rect is not None:
            x = rect["x"]
            # drop below a top-bar anchor, rise above a bottom one
            # (the renderer flips/clamps if it doesn't fit anyway)
            y = (rect["y"] + rect["h"] + 4 if rect["y"] < h / 2
                 else rect["y"] - 4)
    return Menu(
        "hud-menu", [r[0] for r in rows], x=x, y=y,
        icons=[r[1] for r in rows],
        on_select=lambda i, v, rr=rows: rr[i][2](),
        on_dismiss=lambda: _close_hud_menu(b))


def _toggle_tc(b):
    b._hud_tc_remaining = not getattr(b, "_hud_tc_remaining", False)
    b.invalidate()


def _toggle_hud_favorite(b):
    st = b._hud_state or {}
    st["favorite"] = not st.get("favorite")   # optimistic, like the np bar
    _hud_action(b, "toggle-favorite")
    b.invalidate()


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
        "favorite": w >= 560,
        "ch_btns": w >= 700,     # chapter prev/next buttons
        "chapters": w >= 700,    # chapter list dropdown
        "volbar": w >= 760,      # volume slider (mute button always)
        "ends_at": w >= 1000,    # wall-clock end time
    }

    def tbtn(icon, node_id, cb, autofocus=False, icon_size=30, tip=None,
             repeat=False, fg="eeeeee"):
        return Button("", id=node_id, icon=icon, flat=True, fg=fg,
                      icon_size=sz(icon_size), autofocus=autofocus,
                      tip=tip, repeat=repeat, on_click=cb)

    # Scrub semantics: 'change' only moves the preview + clock; the seek
    # happens once on 'commit' (drag release / adjust-mode exit), so
    # scrubbing never spams seeks at a transcode. ESC/focus-away cancels.
    # The bar wakes focused AND active on a key/remote summon
    # (autofocus slider → renderer enters adjust mode): LEFT/RIGHT
    # scrub immediately, ENTER commits, UP/DOWN step off the bar.
    seek = Slider(
        "hud-seek", value=pos, min=0, max=max(1.0, dur),
        force=True, flex=1, h=26, autofocus=True, always_adjust=True,
        marks=([ch["time"] / dur for ch in chapters if 0 < ch["time"] < dur]
               if dur > 0 else None),
        ranges=([(max(0.0, a / dur), min(1.0, e / dur))
                 for a, e in (st.get("ranges") or []) if e > a]
                if dur > 0 else None),
        on_change=b._hud_scrub_change,
        on_commit=b._hud_scrub_commit,
        on_cancel=b._hud_scrub_cancel,
        on_hover=b._hud_hover_move,
        on_hover_end=b._hud_hover_end)

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
        icon_size=36))
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
    # (no stop button: the top bar's back arrow yields to the library)
    shown_pos = pos if scrub is None else scrub
    if tiers["clock"]:
        # click toggles total <-> negative-remaining (the lua tc_right)
        if b._hud_tc_remaining and dur > 0:
            end_part = "-" + _clock(max(0.0, dur - shown_pos))
        else:
            end_part = _clock(dur)
        controls.append(Box(
            [Text("%s / %s" % (_clock(shown_pos), end_part),
                  size=sz(17),
                  color="ffffff" if scrub is not None else "dddddd")],
            id="hud-clock", pad=4, align="center", direction="row",
            on_click=lambda: _toggle_tc(b)))
    if tiers["ends_at"] and dur > 0:
        speed = max(0.01, float(_ctl_get(b, "get_speed", 1.0)))
        ends = time.strftime(
            "%H:%M",
            time.localtime(time.time() + max(0.0, dur - pos) / speed))
        controls.append(Text(_("Ends at {0}").format(ends),
                             size=sz(16), color="aaaaaa"))
    controls.append(Spacer())

    right = []
    if tiers["favorite"]:
        fav = bool(st.get("favorite"))
        right.append(tbtn(
            "favorite" if fav else "favorite_border", "hud-fav",
            lambda: _toggle_hud_favorite(b),
            tip=_("Favorite"), fg=theme.FAV_RED if fav else "eeeeee"))
    right.extend(_pickers(b, menu_state, pos, chapters, tiers))
    muted = bool(st.get("muted"))
    vol = st.get("volume", 100) or 0
    right.append(tbtn(
        "volume_off" if muted else
        ("volume_up" if vol >= 50 else "volume_down"),
        "hud-mute", lambda: b._ctl(lambda c: c.toggle_mute()),
        tip=_("Mute")))
    if tiers["volbar"]:
        right.append(Slider(
            "hud-vol", value=0 if muted else vol, min=0, max=100,
            w=sz(110), force=True,
            on_change=lambda v: b._ctl(lambda c: c.set_volume(v))))
    right.append(tbtn(
        "settings", "hud-settings",
        lambda: _open_hud_menu(b, "root", anchor="hud-settings"),
        tip=_("Settings")))
    right.append(tbtn(
        "fullscreen_exit" if st.get("fullscreen") else "fullscreen",
        "hud-fs", lambda: b._ctl(lambda c: c.toggle_fullscreen()),
        tip=_("Fullscreen")))

    transport = Row(controls + right, gap=sz(6), align="center")

    bar = Column(
        [
            # the Slider has a fixed default width, so stretch can't
            # touch it directly: an unsized Row wrapper stretches to the
            # column width and flex=1 spreads the slider inside it
            Row([seek], align="center"),
            transport,
        ],
        gap=sz(6), pad=(sz(24), sz(14)), w=w, anchor="s",
        align="stretch")

    # Top header, like the lua OSC's: back (yield to the library),
    # title, SyncPlay drop-down — over its own top-down scrim.
    heading = Text(st.get("title") or "", size=sz(20), bold=True, flex=1)
    context = _episode_context(st)
    if context:
        # Series and SxEy go on their own line above the episode title,
        # not joined into one string. The detail banner learned this the
        # hard way ("Clannad · S1E1 · On the Hillside Pa"), and the top bar
        # is tighter still — a back button one side, SyncPlay the other.
        heading = Column(
            [Text(context, size=sz(15), color="bbbbbb"),
             Text(st.get("title") or "", size=sz(20), bold=True)],
            gap=sz(1), flex=1, align="stretch")
    top_items = [
        tbtn("arrow_back", "hud-back",
             lambda: b._ctl(lambda c: c.stop()), tip=_("Back")),
        heading,
    ]
    st_menu = (menu_state
               if menu_state and menu_state.get("has_media") else {})
    syncplay = st_menu.get("syncplay")
    if syncplay is not None:
        top_items.append(tbtn(
            "groups", "hud-syncplay",
            lambda: _open_hud_menu(b, "syncplay",
                                   anchor="hud-syncplay"),
            tip=_("SyncPlay"),
            fg=theme.ACCENT if syncplay.get("enabled") else "eeeeee"))
    top = Row(top_items, gap=sz(10), pad=(sz(24), sz(10)), w=w,
              anchor="n", align="center")

    children = [
        Gradient(color="000000", top=0, bottom=215, w=w,
                 h=min(int(h * SCRIM_FRAC), SCRIM_MAX), anchor="sw"),
        # top scrim: dense at the top, ~2.2x the header height so the
        # title sits on the solid half (same math as the bottom scrim)
        Gradient(color="000000", top=170, bottom=0, w=w,
                 h=min(int(h * 0.25), 160), anchor="nw"),
        bar,
        top,
    ]

    skip = _skip_float(b, size)
    if skip is not None:
        children.append(skip)

    preview_at = scrub if scrub is not None else b._hud_hover
    preview = _preview_float(b, preview_at, dur, size, chapters)
    if preview is not None:
        children.append(preview)

    menu = _settings_menu(b, menu_state, size)
    if menu is not None:
        children.append(menu)

    return Stack(children, w=w, h=h)


# Must match renderer.lua's SLIDER_PAD (track inset inside the slider node —
# the thumb travels between the insets). A click is mapped back to a seek
# time through this, so drift puts the seek off where the user clicked.
# Enforced by tests/test_python_lua_constants.py.
_SLIDER_PAD = 8


def _preview_float(b, secs, dur, size, chapters):
    """Seek-preview bubble floated above the slider at ``secs``: the
    trickplay thumbnail (when the video has tiles) over the chapter
    name and timestamp — the lua OSC's hover bubble. Shown while
    scrubbing or while the pointer rests on the bar. Geometry comes
    from the previous scene's laid-out slider rect — one frame stale,
    which is fine: the bar doesn't move while the HUD is up."""
    if secs is None or dur <= 0:
        return None
    rect = None
    if b.app is not None and hasattr(b.app, "node_rect"):
        rect = b.app.node_rect("hud-seek")
    if rect is None:
        return None
    w, h = size
    entry = _trickplay_frame(b, secs)
    rows = []
    if entry is not None:
        rows.append(Image(entry["src"], entry["iw"], entry["ih"]))
    chapter = None
    for ch in chapters:
        if ch["time"] <= secs and ch.get("title"):
            chapter = ch["title"]
    if chapter:
        rows.append(Text(chapter, size=14, color="dddddd",
                         align="center"))
    rows.append(Text(_clock(secs), size=14, bold=True, align="center"))
    bw = max(entry["iw"] if entry is not None else 0, 120) + 16
    frac = max(0.0, min(1.0, secs / dur))
    track_x = rect["x"] + _SLIDER_PAD
    track_w = rect["w"] - 2 * _SLIDER_PAD
    px = track_x + frac * track_w - bw / 2
    px = max(8, min(w - bw - 8, px))
    # anchor sw + negative dy pins the bubble's BOTTOM just above the
    # slider whatever its content height turns out to be
    return Box(
        [Column(rows, gap=4, align="center")],
        id="hud-preview", bg="282828", radius=6, pad=8,
        anchor="sw", dx=px, dy=rect["y"] - 10 - h)
