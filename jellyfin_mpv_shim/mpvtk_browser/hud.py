"""Playback HUD — YouTube-on-TV style controls inside the mpv window.

Rendered by the browser while it is yielded to video playback, via the
renderer's attached-but-idle lifecycle (``mpvtk-hud``): playback runs
clean until an arrow key / ENTER / mouse motion summons the HUD, and
``hud_hide_secs`` without input hides it again, on the policy
``hud_autohide`` sets (both renderer-side; see
renderer.lua). ``hud_control.HudController`` owns the summoned flag and
calls :func:`build_hud` from ``build()``; playstate comes from the
same ``push_playstate`` snapshots that feed the audio now-playing bar,
kept fresh by the shared 1s ticker.

This IS the jellyfin-styled player UI (``osc_style: mpvtk``, the
default) — it replaced the retired trickplay-jf-osc.lua at feature
parity.
"""

import logging
import time
from typing import Optional

from ..conf import settings
from ..i18n import _
from ..mpvtk.widgets import (
    Box,
    Button,
    Column,
    Dialog,
    Dropdown,
    Element,
    Gradient,
    Menu,
    Row,
    Slider,
    Spacer,
    Stack,
    Text,
    VScroll,
)
from . import theme, window_chrome
from .components import media_info
from .window_chrome import WINDOW_CONTROL_W

log = logging.getLogger("mpvtk_browser.hud")

# Scrim geometry: a ramp from transparent at its top edge to alpha 215 at
# the window's bottom. What it has to do is put the bar's own text on
# something dark, so the number that matters is its height against the bar's
# (116px at the stock cover size): the taller it is, the denser the ramp is
# where the title and the scrubber sit.
#
# Capped by a window fraction as well, so short windows keep most of the
# picture clean; the cap is what binds at any normal size.
#
# 0.55/380 -> 0.42/300 after #620, where the shadow over the picture was the
# first thing anyone mentioned, then -> 200 on Izzie's UX pass. 200 is ~1.7x
# the bar rather than the ~2.6x it was, which puts the title at roughly
# alpha 100 instead of 140 -- deliberately lighter, and the reason the
# "none" mode's per-glyph shadow exists as the other end of the same dial.
SCRIM_FRAC = 0.42
SCRIM_MAX = 200
# Top scrim, same relation to the header's height.
TOP_SCRIM_FRAC = 0.20
TOP_SCRIM_MAX = 130
# (There was a "half" mode here -- the same ramp at half height. It was
# offered as the middle setting between the full ramp and no shading at all,
# and it stopped earning that place twice over. Once the default came down to
# 200 its half was 100, shorter than the bar itself, so the scrubber and the
# bar's top edge sat on bare picture; and at any height it left the seekbar's
# chapter markers to fend for themselves, which is the one thing on that bar
# that is thin, light and positional. "panel" is the middle setting now, and
# "none" is the far end.)
# "panel": a flat band exactly the height of the bar rather than a ramp --
# a hard edge, and no wash over the picture above it. Opacity, 255 opaque.
# Black rather than theme.SCRIM: the HUD is drawn over VIDEO and stays dark
# whatever the theme does (see mpvtk.theme), which is why the gradients
# above are a literal too.
PANEL_BG = "000000"
PANEL_ALPHA = 170

# Bottom inset of the Skip Intro/Credits button, measured to its BOTTOM
# edge so the two implementations line up whatever the label's measured
# height turns out to be. renderer.lua draws the same button while the
# HUD is idle (PHUD_SKIP_BOTTOM there) and hands over to this one mid-
# segment, so a mismatch shows as the button hopping on summon/hide.
# Enforced by tests/test_python_lua_constants.py.
_SKIP_BOTTOM = 106
# ...and so must its type size and padding, or the two copies differ in
# size and weight even when they share a corner. renderer.lua rebuilds
# the Button box from these plus layout.LINE_H.
_SKIP_SIZE = 18
_SKIP_PAD = 10
# ...and the horizontal inset, for the same reason. This one was a bare
# literal on both sides, so when the UI scale landed only the Python copy
# scaled (layout folds dx into x, which scale_scene converts) and the two
# buttons drifted apart horizontally by 24*(scale-1) px -- including the
# renderer-drawn hit rect, which is what you actually click.
_SKIP_RIGHT = 24
# ...and the colours, for the same handoff reason: a mismatch here is a
# flash of a different-coloured button on summon rather than a hop. Dark
# translucent grey with white text, so the button reads as part of the
# player's overlay furniture over any picture instead of a light chip
# punched out of the video. _SKIP_ALPHA is opacity (255 = opaque) and
# applies on hover too — renderer.lua reuses node.a whatever the hover
# fill is, so only the fill changes under the pointer.
_SKIP_BG = "202020"
_SKIP_BG_HOVER = "3a3a3a"
_SKIP_FG = "ffffff"
_SKIP_ALPHA = 180


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


def _secondary_available(st, subs):
    """Whether the subtitle picker should offer 'Secondary…': a primary track
    is active AND there is a second, mpv-renderable track to pick. A secondary
    with no primary is just a differently-placed primary, so it's suppressed."""
    sub2 = st.get("secondary_subtitles") or []
    primary_on = any(s.get("id", -1) != -1 and s.get("selected") for s in subs)
    return len(sub2) > 1 and primary_on


def _subtitle_picker(b, st, subs):
    """The primary subtitle dropdown, with a trailing 'Secondary…' entry that
    opens the secondary-track submenu (see _menu_rows' 'secondary_sub'). Custom
    rather than _option_picker because that last row opens a menu instead of
    setting a track."""
    labels = [o.get("label") or "" for o in subs]
    ids = [o.get("id") for o in subs]
    sel = next((i for i, o in enumerate(subs) if o.get("selected")), 0)
    secondary = _secondary_available(st, subs)
    if secondary:
        # Show the current secondary alongside the entry, like the gear menu's
        # with_current does for its submenus.
        cur = next((o.get("label") for o in st.get("secondary_subtitles") or []
                    if o.get("selected") and o.get("id", -1) != -1), None)
        labels.append("%s  ·  %s" % (_("Secondary…"), cur) if cur
                      else _("Secondary…"))

    def on_select(i, v):
        if secondary and i == len(labels) - 1:
            _open_hud_menu(b, "secondary_sub", anchor="hud-sub")
        else:
            _hud_action(b, "set-sub", ids[i])

    return Dropdown("hud-sub", labels, selected=sel, force=True,
                    trigger_icon="closed_caption", tip=_("Subtitle Track"),
                    on_select=on_select)


def _chapters(b):
    if b.controller is None or not hasattr(b.controller, "chapters"):
        return []
    try:
        return b.controller.chapters() or []
    except Exception:
        return []


def _chapter_jump(b, direction):
    """Seek to the previous/next chapter start (the lua OSC's
    ch_prev/ch_next).

    The rule -- prev re-seeks the current chapter's start unless pressed
    within its first 2 seconds, like mpv's 'add chapter -1' -- lives in
    player.chapter_target, because the mouse's back/forward buttons ask the
    same question (mouse_chapter_nav) and two copies of it would drift.
    Going through the player also puts the jump through SyncPlay, which
    working the target out here and seeking to it did not.
    """
    b._ctl(lambda c: c.chapter_seek(direction))


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
        out.append(_subtitle_picker(b, st, subs))
    quality = st.get("quality") or {}
    if quality.get("options") and tiers["quality"]:
        out.append(_option_picker(b, "hud-quality", "hd",
                                  _("Video Quality"), quality["options"],
                                  "set-quality"))
    return out


# ------------------------------------------------- settings gear menu
# The lua OSC's jf_settings_sheet, rebuilt on the Menu widget. One
# level open at a time (b.hud.menu names it); submenus swap the item
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
    if kind == "profiles":
        # Same shape, same reason: which library an item is in takes a
        # request, and the state blob is built on the render path. Asked
        # for once on open; the "This Library" row appears in a later
        # build, and is cached for the rest of the series.
        _hud_action(b, "profiles-scopes")
    if anchor is not None:
        b.hud.menu_anchor = anchor
    b.hud.menu = kind
    b.invalidate()


def _close_hud_menu(b):
    b.hud.menu = None
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


#: Width at which the bar grows its own Video Quality button. Read by both
#: build_hud's tiers and _menu_rows, because "is that button on screen?" is
#: exactly the question the gear's Quality row has to answer.
QUALITY_BTN_W = 560


def _menu_rows(b, st, w=None):
    """(label, icon, action) rows for the open settings-menu level.
    ``st`` is the osc_bridge state blob ({} when unavailable).

    ``w`` is the window width, for the one row whose presence depends on
    whether the bar already has a button for it. Without it (a caller that
    does not know) the row is kept: an unreachable setting is worse than a
    duplicated one."""
    kind = b.hud.menu
    rows = []
    # Declared up front because the name is reused by two loops with
    # different element types: the sub-style pairs are always labelled,
    # _ASPECTS carries a None for the entry labelled _("Auto") at use.
    label: Optional[str]

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
        # Only when the bar's own Video Quality button is NOT on screen.
        # It drops out below QUALITY_BTN_W, and there the gear is the only
        # way to reach the setting; above it, this row is a second door to
        # a sheet whose button is a few pixels away.
        if quality.get("options") and (w is None or w < QUALITY_BTN_W):
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
        # No SyncPlay row: the bar carries its own SyncPlay button under
        # exactly the same condition this row had (a state blob with
        # media), so it was never the only way in — and unlike Quality it
        # has no width tier to drop out at.
        rows.append((_("Night Mode (Auto Volume Adj)"),
                     "check" if settings.audio_night_mode else None,
                     leaf(lambda: b._ctl(lambda c: c.toggle_night_mode()))))
        # Ours, not mpv's. mpv's stats.lua overlay is still one keypress
        # away on `i` (player._stats_key) and is a different question --
        # what the decoder is doing, not what the server is sending -- so
        # the two are not rivals for this row. This is the one a viewer
        # wants when the fan spins up.
        rows.append((_("Playback Info"), None, leaf(
            lambda: _open_info(b))))
        if st.get("allow_screenshot"):
            rows.append((_("Screenshot"), None, leaf(
                lambda: _hud_action(b, "screenshot"))))
        if st.get("has_media"):
            rows.append((_("Quit and Mark Unwatched"), None, leaf(
                lambda: _hud_action(b, "unwatched-quit"))))
        return rows

    if kind.startswith("profiles:"):
        # Back goes to the scope step, not to the gear root: this list was
        # reached through it, and returning past it would strand the user
        # one level further out than they came from.
        rows.append((_("Back"), "arrow_back",
                     lambda: _open_hud_menu(b, "profiles")))
    elif b.hud.menu_anchor not in ("hud-syncplay", "hud-sub"):
        # opened from the gear: submenus can step back to its root. The top
        # bar's SyncPlay button and the subtitle dropdown's Secondary… entry
        # open their sheets standalone (like the lua OSC's drop-downs), so no
        # Back there — they weren't reached through the gear root.
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
        # The scope step, which is also the report: each row carries the
        # profile that scope holds and the winning one is marked, so a film
        # being sharpened differently from the rest of its library has a
        # visible cause. Scopes that do not apply are absent -- a film has
        # no series row.
        scopes = (st.get("profiles") or {}).get("scopes") or []
        if not scopes:
            # No scope information (an older state blob, or the library is
            # still being resolved): the profile list itself, which is what
            # this menu was before scopes and still does the useful thing.
            option_rows(st.get("profiles"), "set-profile")
        for scope in scopes:
            rows.append((
                with_current(scope.get("label") or "", scope.get("value")),
                "check" if scope.get("in_effect") else None,
                lambda sid=scope.get("id"): _open_hud_menu(
                    b, "profiles:" + str(sid))))
    elif kind.startswith("profiles:"):
        wanted = kind.split(":", 1)[1]
        for scope in (st.get("profiles") or {}).get("scopes") or []:
            if scope.get("id") == wanted:
                option_rows(scope, "set-profile")
                break
    elif kind in ("sub_size", "sub_position", "sub_color"):
        option_rows(sub_style.get(kind[4:]),
                    "set-" + kind.replace("_", "-"))
    elif kind == "secondary_sub":
        option_rows({"options": st.get("secondary_subtitles")},
                    "set-secondary-sub")
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
    if not b.hud.menu:
        return None
    st = menu_state if menu_state and menu_state.get("has_media") else {}
    w, h = size
    rows = _menu_rows(b, st, w)
    if not rows:
        return None
    x, y = w - 300, h - 160
    anchor = b.hud.menu_anchor or "hud-settings"
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


#: The info panel's width, and the height its scroll gives up at. Wide
#: enough for "The video resolution is not supported." on one line, since
#: those sentences are the point of the panel; tall enough for a typical
#: file's whole summary without scrolling, and no taller, because it is
#: floating over the frame somebody is watching.
INFO_W = 520
INFO_MAX_H = 420


def _mpv_rows(stats):
    """The live-counter block, from mpv rather than from the DTO.

    Only what a *viewer* asks: is my GPU being used, why is it stuttering,
    why did it stall. Each row is omitted when mpv had nothing to say --
    which is a real state rather than an error (no rendered-fps estimate
    before the first frame, none of the video counters during audio), and
    is why they are individually guarded rather than shown as zeroes.
    """
    rows = []
    hwdec = stats.get("hwdec")
    if hwdec:
        # mpv says "no" for software decoding, which reads as a broken
        # value rather than an answer.
        rows.append((_("Hardware acceleration"),
                     _("No") if hwdec == "no" else str(hwdec)))
    if stats.get("vo"):
        rows.append((_("Video output"), str(stats["vo"])))
    fps = stats.get("fps")
    if fps:
        rows.append((_("Framerate"), "%.2f" % float(fps)))
    drops_vo, drops_dec = stats.get("drops_vo"), stats.get("drops_dec")
    if drops_vo is not None or drops_dec is not None:
        # Both, and labelled, because they mean opposite things: the
        # decoder dropping frames is a machine that cannot keep up, the VO
        # dropping them is usually a display-sync problem. One combined
        # number sends people to the wrong place.
        rows.append((_("Dropped frames"),
                     _("%(vo)d output, %(dec)d decoder")
                     % {"vo": int(drops_vo or 0), "dec": int(drops_dec or 0)}))
    if stats.get("avsync") is not None:
        rows.append((_("A/V sync"), "%+.3f s" % float(stats["avsync"])))
    if stats.get("buffered") is not None:
        # The answer to "why does it keep stalling" -- and the one number
        # here that a Jellyfin user can act on, by turning the stream down.
        rows.append((_("Buffered"), _("%.1f s") % float(stats["buffered"])))
    speed = stats.get("cache_speed")
    if speed:
        rows.append((_("Download speed"),
                     _("%.1f Mbps") % (float(speed) * 8 / 1000000.0)))
    return rows


def _info_rows(info, stats=None):
    """``[(heading, [(label, value), ...]), ...]`` for the panel.

    jellyfin-web's playerstats categories, minus the two that need the
    server polled (transcode completion and encoder fps) — see the gateway's
    ``playback_info`` — plus one they do not have: what mpv is doing with
    the stream once it arrives.
    """
    source = info.get("source") or {}
    method = info.get("play_method")
    playback = []
    if info.get("item_type"):
        playback.append((_("Media type"), info["item_type"]))
    label = media_info.play_method_label(method)
    if label:
        # How it got here, which web cannot say and we can: a file we opened
        # ourselves is a different thing from the same bytes over HTTP, and
        # on a downloaded copy it is the whole answer.
        if method == media_info.DIRECT_PLAY:
            label += "  (%s)" % (_("downloaded copy") if info.get("offline")
                                 else _("local file") if info.get("direct_path")
                                 else _("stream from server"))
        playback.append((_("Play method"), label))
    reasons = media_info.transcode_reasons(info.get("transcode_reasons"))

    out = [(_("Playback"), playback)] if playback else []
    if reasons:
        # Numbered rather than bulleted: the label column is what every
        # other block here uses, and an empty one leaves the sentences
        # hanging in the middle of the panel.
        out.append((_("Reasons"),
                    [("%d." % (i + 1), r) for i, r in enumerate(reasons)]))
    player_rows = _mpv_rows(stats or {})
    if player_rows:
        out.append((_("Player"), player_rows))
    file_rows = media_info.source_attributes(source)
    if file_rows:
        out.append((_("File"), file_rows))
    for stream in media_info.visible_streams(source):
        rows = media_info.stream_attributes(stream, source)
        if rows:
            out.append((media_info.stream_heading(stream), rows))
    return out


def _info_dialog(b, size):
    """The playback-info panel, or None when it is closed.

    A ``Dialog`` rather than a floating Box for two reasons that are one
    reason: it gets ESC and click-outside dismissal for free, and the
    renderer's auto-hide treats an open modal as a busy HUD
    (``phud_busy``) — so the panel cannot be read for four seconds and then
    yanked away with the bar it is attached to.
    """
    if not b.hud.info:
        return None
    info = _ctl_get(b, "playback_info", None)
    if not info:
        return None
    # Sized against the window, not at a constant. The HUD is drawn at every
    # width from a phone-shaped window upward -- the bar itself scales to
    # 72% and sheds controls -- and a 520-wide panel in a 480-wide window is
    # a dialog with its own edges off both sides of the screen.
    win_w, win_h = size
    w = max(280, min(INFO_W, win_w - 40))
    # The floor is deliberately below anything readable: at that point
    # the window is too short for the panel AND its own heading, and a
    # scroll of two rows inside the window beats a panel whose Close
    # button is off the bottom of the screen.
    body_h = max(60, min(INFO_MAX_H, win_h - 220))
    blocks = [Text(_("Playback Info"), size=22, bold=True)]
    if info.get("title"):
        blocks.append(Text(info["title"], size=16, color=theme.SUBTLE_FG,
                           wrap=True, w=w - 48))
    body = []
    stats = _ctl_get(b, "player_stats", {}) or {}
    for heading, rows in _info_rows(info, stats):
        body.append(Text(heading, size=17, bold=True, color=theme.ACCENT))
        for label, value in rows:
            # An explicit value width rather than flex, for the reason
            # DialogsMixin.MINFO_VALUE_W spells out: a wrap=True Text with
            # no `w` measures one line tall, so inside a Row it clips and
            # ellipsizes -- which on a path throws away the filename and on
            # a transcode reason throws away the end of the sentence.
            label_w = min(160, w // 3)
            body.append(Row([
                Text(label, size=15, color=theme.SUBTLE_FG, w=label_w),
                Text(value, size=15, wrap=True,
                     w=max(80, w - 48 - label_w - 8)),
            ], gap=8, align="start"))
    if not body:
        body.append(Text(_("Nothing is playing."), size=15,
                         color=theme.SUBTLE_FG))
    blocks.append(VScroll(Column(body, gap=6, align="stretch"),
                          id="hud-info-scroll", h=body_h))
    blocks.append(Row([Spacer(flex=1),
                       Button(_("Close"), id="hud-info-close",
                              on_click=lambda: _close_info(b))], gap=10))
    return Dialog("hud-info",
                  Column(blocks, pad=24, gap=14, bg=theme.PANEL_BG,
                         radius=12, border=theme.BORDER, w=w,
                         align="stretch"),
                  on_dismiss=lambda: _close_info(b))


def _open_info(b):
    b.hud.menu = None          # it was opened from the gear menu
    b.hud.info = True
    b.invalidate()


def _close_info(b):
    b.hud.info = False
    b.invalidate()


def _toggle_tc(b):
    b.hud.tc_remaining = not b.hud.tc_remaining
    b.invalidate()


def _toggle_hud_mute(b):
    """Flip the icon on the click, not on the round trip.

    The player now observes ``mute`` and pushes a snapshot (see
    _on_volume_change), so this only covers the trip out to the action
    thread and back — but that trip runs behind the player's lock, which a
    playback start holds for its whole duration. Same optimism as the
    favourite button below, and self-correcting: the next snapshot is the
    truth whatever we guessed."""
    st = b.hud.state or {}
    st["muted"] = not st.get("muted")
    b._ctl(lambda c: c.toggle_mute())
    b.invalidate()


def _toggle_hud_favorite(b):
    st = b.hud.state or {}
    st["favorite"] = not st.get("favorite")   # optimistic, like the np bar
    _hud_action(b, "toggle-favorite")
    b.invalidate()


def _skip_float(b, size):
    """Floating Skip Intro / Skip Credits button above the bar's right
    edge (jellyfin-web's placement), when the player says a skippable
    segment is live (playstate skip_label).

    Positioned by a constant inset from the bottom rather than off the
    laid-out slider rect, for two reasons: the rect is a frame stale, so
    keying off it left the button out of the HUD's very first scene
    (it showed up a tick late, or not at all until something else
    invalidated); and renderer.lua draws the standalone version of this
    button while the HUD is idle, so the two have to land in the same
    place or the handoff between them reads as a jump."""
    label = (b.hud.state or {}).get("skip_label")
    if not label:
        return None
    return Button(
        label, id="hud-skip", size=_SKIP_SIZE, pad=_SKIP_PAD,
        bg=_SKIP_BG, alpha=_SKIP_ALPHA, fg=_SKIP_FG,
        hover={"fill": _SKIP_BG_HOVER},
        on_click=lambda: _hud_action(b, "skip-segment"),
        anchor="se", dx=-_SKIP_RIGHT, dy=-_SKIP_BOTTOM)


def _panel():
    """Box styling for the two bars: a flat translucent band under the
    "panel" scrim, and an invisible one otherwise.

    Invisible rather than absent because the renderer needs the bars to
    EXIST as scene nodes: it holds the auto-hide off while the pointer is
    over them (phud_busy), and layout only emits a node for a container that
    has a fill, a border or a click. Alpha 0 costs one ASS event that draws
    nothing, and the node is not a hit target -- node_at ignores a rect with
    no click, tip or hover of its own.
    """
    return {"bg": PANEL_BG,
            "alpha": PANEL_ALPHA if settings.hud_scrim == "panel" else 0}


def _scrim(h, w):
    """The wash behind the controls, per ``hud_scrim``.

    It is not decoration: white-on-white is what the controls hit without
    it, over a frame nobody chose. So "none" is not simply the absence of
    the others -- it moves the job onto the glyphs, which is the ``shadow``
    flag the renderer draws them with (see gateway.hud_key_opts).
    """
    style = settings.hud_scrim
    if style in ("none", "panel"):
        return []          # panel paints as the bars' own background
    return [
        Gradient(color="000000", top=0, bottom=215, w=w,
                 h=int(min(h * SCRIM_FRAC, SCRIM_MAX)), anchor="sw"),
        # top scrim: dense at the top, same relation to the header's height
        # as the bottom one has to the bar's.
        Gradient(color="000000", top=170, bottom=0, w=w,
                 h=int(min(h * TOP_SCRIM_FRAC, TOP_SCRIM_MAX)),
                 anchor="nw"),
    ]


def build_hud(b, size):
    """The summoned HUD scene. ``b`` is the Browser (playstate snapshot,
    scrub state, controller plumbing); returns the full-window tree."""
    w, h = size
    st = b.hud.state or {}
    pos = st.get("position", 0) or 0
    dur = st.get("duration", 0) or 0
    pp = "play_arrow" if st.get("paused") else "pause"
    scrub = b.hud.scrub
    chapters = _chapters(b)

    # Responsive shrink, mirroring the lua OSC's jellyfin layout:
    # everything scales down to 72% as the window narrows, and the
    # less essential controls drop out at breakpoints (in the spirit
    # of jellyfin-web's).
    scale = min(1.0, max(0.72, w / 900.0))

    def sz(v):
        return int(v * scale + 0.5)

    # A still has no timeline and no sound. mpv reports a duration for one
    # -- --image-display-duration, i.e. when the NEXT photo arrives -- and
    # dressing that up as playback is worse than saying nothing: ±10s either
    # does nothing or skips the picture, and a clock counting 0:00 / 0:05
    # across a photograph reads as a video about to end. Volume is simply
    # not a question a picture answers.
    #
    # What survives is what an album needs: pause (stop it moving on), and
    # prev/next (move through it).
    photo = bool(st.get("is_photo"))
    tiers = {
        "seek_btns": w >= 500 and not photo,   # ±10s/±30s step buttons
        "clock": w >= 500 and not photo,
        "quality": w >= QUALITY_BTN_W,
        "favorite": w >= 560,
        "ch_btns": w >= 700,     # chapter prev/next buttons
        "chapters": w >= 700,    # chapter list dropdown
        "volume": not photo,     # mute button
        "volbar": w >= 760 and not photo,      # volume slider
        "ends_at": w >= 1000 and not photo,    # wall-clock end time
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
        on_video=True,   # drawn over the picture; see widgets.Slider
        force=True, flex=1, h=26, autofocus=True, always_adjust=True,
        marks=([ch["time"] / dur for ch in chapters if 0 < ch["time"] < dur]
               if dur > 0 else None),
        ranges=([(max(0.0, a / dur), min(1.0, e / dur))
                 for a, e in (st.get("ranges") or []) if e > a]
                if dur > 0 else None),
        on_change=b.hud.scrub_change,
        on_commit=b.hud.scrub_commit,
        on_cancel=b.hud.scrub_cancel,
        # The renderer floats the trickplay/chapter bubble itself -- but
        # only where there is a timeline to describe. `max` is floored at
        # 1.0 above so the renderer's frac has a divisor, which also
        # defeats its own `max > 0` guard; a live channel reports no
        # duration, and without this the bubble tracked the pointer along
        # the bar reading 0:00 the whole way. Guarded like `marks` and
        # `ranges` beside it. (The deleted _preview_float opened with the
        # same test.)
        preview=dur > 0)

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
            lambda: _chapter_jump(b, -1),
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
            lambda: _chapter_jump(b, 1),
            tip=_("Next Chapter")))
    controls.append(tbtn(
        "skip_next", "hud-next",
        lambda: b._ctl(lambda c: c.next()), tip=_("Next")))
    # (no stop button: the top bar's back arrow yields to the library)
    shown_pos = pos if scrub is None else scrub
    if tiers["clock"]:
        # click toggles total <-> negative-remaining (the lua tc_right)
        if b.hud.tc_remaining and dur > 0:
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
    if tiers["volume"]:
        right.append(tbtn(
            "volume_off" if muted else
            ("volume_up" if vol >= 50 else "volume_down"),
            "hud-mute", lambda: _toggle_hud_mute(b),
            tip=_("Mute")))
    if tiers["volbar"]:
        right.append(Slider(
            "hud-vol", value=0 if muted else vol, min=0, max=100,
            on_video=True,   # drawn over the picture; see widgets.Slider
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

    # A photo has a duration -- mpv's --image-display-duration, i.e. when the
    # next one arrives -- but scrubbing inside it means nothing, and a
    # progress bar crawling across a picture reads as a video about to end.
    # Prev/next and pause stay: those are how you move through an album and
    # how you stop it moving on its own.
    bar_rows = ([] if st.get("is_photo") else [
        # the Slider has a fixed default width, so stretch can't
        # touch it directly: an unsized Row wrapper stretches to the
        # column width and flex=1 spreads the slider inside it
        Row([seek], align="center")]) + [transport]
    # id: renderer.lua's phud_busy holds the auto-hide off while the pointer
    # is over the controls, and these two rects are what "over the controls"
    # means (hover mode). Also where the "panel" scrim paints.
    bar = Column(bar_rows, gap=sz(6), pad=(sz(24), sz(14)), w=w, anchor="s",
                 align="stretch", id="hud-bar", **_panel())

    # Top header, like the lua OSC's: back (yield to the library),
    # title, SyncPlay drop-down — over its own top-down scrim.
    # Element, not Text: an episode gets a two-line Column instead.
    heading: Element = Text(st.get("title") or "", size=sz(20), bold=True,
                            flex=1)
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
    # The same three window buttons the library's top bar grows when the
    # desktop draws no title bar. Here for the reason they exist there: a
    # windowed video on such a desktop is otherwise a window with no way to
    # close, minimize or move it. ESC does get you back to the library, but
    # "press an undocumented key first" is not what a close button is.
    #
    # Smaller than the HUD's own buttons (sz(20) against sz(30)): window
    # furniture sits below the content controls everywhere, and at transport
    # size these would read as three more playback actions.
    #
    # Nothing extra is needed for fullscreen -- window_controls_wanted()
    # already answers no there, which is right for both bars.
    top_items += window_chrome.window_controls(
        b, prefix="hud-win", icon_size=sz(20), w=sz(WINDOW_CONTROL_W),
        fg="eeeeee", gap=sz(4))
    top = Row(top_items, gap=sz(10), pad=(sz(24), sz(10)), w=w,
              anchor="n", align="center", id="hud-topbar",
              # Drag the video window by its header, as the library's bar
              # does. `_panel()` may leave this bar with no fill at all
              # (hud_scrim "none"), which is exactly when the marker has to
              # be what conjures the hit rect -- see layout._arrange_box.
              window_drag=b.window_controls, **_panel())

    children = _scrim(h, w) + [bar, top]

    skip = _skip_float(b, size)
    if skip is not None:
        children.append(skip)

    # The scrub preview bubble is NOT here. The renderer draws it, from the
    # trickplay tiles and mpv's chapter list, without asking (#618/#612) —
    # see renderer.lua's `pv` slider flag.

    menu = _settings_menu(b, menu_state, size)
    if menu is not None:
        children.append(menu)

    info = _info_dialog(b, size)
    if info is not None:
        children.append(info)

    # The same corner the library grows, for the same reason the buttons
    # above are here: a windowed video on a desktop that draws no frame is
    # otherwise a window that cannot be resized either. Last, so it is over
    # the transport bar it shares a corner with.
    grip = window_chrome.resize_grip(b, w, h)
    if grip is not None:
        children.append(grip)

    return Stack(children, w=w, h=h)
