"""Assembling the option set mpv is constructed with.

This was the first 170 lines of ``PlayerManager._init_mpv``. It is a pure
function of ``settings`` plus three facts the player knows (which backend is
in use, whether the browser wants a window, whether trickplay started), and
splitting it out means the answers can be checked without constructing an
mpv -- which, on the libmpv backend, means opening a real window.

Nothing here touches mpv, and nothing here imports ``player``. The
directory-creating half of the original block (``conffile.get_dir`` /
``conffile.get``) deliberately stayed behind: ``confdir`` is a path lookup
and safe to call, but creating the config tree is a side effect and belongs
with the code that is actually starting a player.
"""

import platform
import sys
from collections import OrderedDict

from . import conffile
from .conf import settings
from .constants import APP_NAME, DESKTOP_ID, USER_APP_NAME
from .utils import get_resource

#: Styles that must not leave mpv's built-in OSC on: two that replace it
#: with something of ours, and one that replaces it with nothing.
_REPLACES_OSC = ("mpv", "mpvtk", "none")


def resolve_osc_style():
    """Which in-player UI to load, after the aliases and fallbacks.

    ``settings.osc_style`` is not the answer on its own: it may hold a legacy
    alias, and two settings can force a fallback. The result is stored on the
    player as ``_osc_style_resolved`` because the c-menu routing, enable_osc
    and the skip-button path all key off the resolved value rather than the
    configured one.
    """
    # Which in-player UI to load: the in-window mpvtk playback HUD
    # ("mpvtk"; no lua script — the browser renders it, see
    # mpvtk_browser/hud.py), the stock mpv OSC patched with
    # trickplay previews ("mpv"), whatever the mpv binary ships and
    # the user's own scripts ("default"), or nothing at all ("none").
    # "jellyfin" is a legacy alias for the HUD — the jellyfin-styled
    # lua OSC it used to name was retired once the HUD reached parity.
    #
    # "none" is where the old enable_osc setting went. That was a
    # separate switch that only ever reached mpv's OWN controls, so
    # turning it off under the default style did nothing at all and
    # then silently took the controls away if you later switched to
    # the mpv OSC (#615). One question, one answer.
    osc_style = settings.osc_style
    if osc_style == "jellyfin":
        osc_style = "mpvtk"
    if osc_style == "mpvtk" and not settings.enable_gui:
        # The playback HUD is rendered by the library browser; with the
        # GUI disabled there is nothing to render it, so the patched
        # stock OSC is the closest thing.
        osc_style = "mpv"
    if osc_style == "mpvtk" and not settings.thumbnail_osc_builtin:
        # Legacy opt-out: thumbnail_osc_builtin=False used to mean
        # "don't replace my OSC" (e.g. users running uosc).
        osc_style = "default"
    return osc_style


def mpv_scripts(osc_style, trickplay):
    """Lua scripts to load, in load order.

    ``trickplay`` is whether the TrickPlay worker actually started -- not
    whether it was asked to. thumbfast is the script side of that feature, so
    a worker that failed to come up must not advertise it to mpv.
    """
    scripts = []
    if settings.menu_mouse:
        scripts.append(get_resource("mouse.lua"))
    if trickplay:
        # Loaded regardless of OSC style: both shim OSCs consume
        # it, and thumbfast-aware user OSCs (e.g. uosc) benefit
        # under "default" too.
        scripts.append(get_resource("thumbfast.lua"))
    if osc_style == "mpv":
        scripts.append(get_resource("trickplay-osc.lua"))
    return scripts


def mpv_binary_location():
    """Path to the mpv binary for the external-mpv backend, or None to let
    the library find one.

    Only the frozen macOS build ships its own; everywhere else an unset
    setting means "whatever is on PATH".
    """
    mpv_location = settings.mpv_ext_path
    if (
        mpv_location is None
        and platform.system() == "Darwin"
        and getattr(sys, "frozen", False)
    ):
        mpv_location = get_resource("mpv")
    return mpv_location


def build_mpv_options(osc_style, scripts, ext_mpv, browser_wants_window):
    """The full option set to construct mpv with.

    ``osc_style`` comes from :func:`resolve_osc_style` and ``scripts`` from
    :func:`mpv_scripts` -- both are passed in rather than recomputed, because
    the player has to interleave the TrickPlay worker's startup between them.

    ``ext_mpv`` is the backend in use (``player.is_using_ext_mpv``).
    ``browser_wants_window`` is whether the in-window UI should be on screen
    as soon as mpv comes up; see the force_window comment below for why the
    player, not this function, decides it.
    """
    mpv_options = OrderedDict()
    if ext_mpv:
        mpv_options.update(
            {
                "start_mpv": settings.mpv_ext_start,
                "ipc_socket": settings.mpv_ext_ipc,
                "mpv_location": mpv_binary_location(),
                "player-operation-mode": "cplayer",
                "start_retries": settings.mpv_ext_start_retries,
                "start_retry_delay_ms": settings.mpv_ext_start_retry_delay_ms,
            }
        )

    if osc_style in _REPLACES_OSC:
        # "mpv" loads the patched stock OSC as a script; "mpvtk" has the
        # in-window playback HUD replace any OSC. Either way mpv's built-in
        # one must be off.
        mpv_options["osc"] = False

    if scripts:
        if settings.mpv_ext:
            mpv_options["script"] = scripts
        else:
            mpv_options["scripts"] = (
                ";" if sys.platform.startswith("win32") else ":"
            ).join(scripts)

    if not (settings.mpv_ext and settings.mpv_ext_no_ovr):
        mpv_options["config"] = True
        mpv_options["config_dir"] = conffile.confdir(APP_NAME)

    if settings.tls_client_cert and settings.tls_client_key:
        mpv_options["tls_cert_file"] = settings.tls_client_cert
        mpv_options["tls_key_file"] = settings.tls_client_key

        if settings.tls_server_ca:
            mpv_options["tls_ca_file"] = settings.tls_server_ca

    # Audio-only files (music) are controlled from the browser's now-playing
    # bar, not an mpv window: don't decode embedded cover art into a video
    # track (which would otherwise pop a window showing the album art).
    # Only affects audio-only files — video and music videos are untouched.
    mpv_options["audio_display"] = "no"

    # The output device and exclusive mode are deliberately NOT set here, even
    # though they are plain options. Both are applied live by
    # apply_audio_settings, which runs from _init_mpv before anything can
    # play, and which snapshots what mpv had first so that going back to
    # "Default" can put it back.
    #
    # Setting them here as well broke exactly that. mpv would be *constructed*
    # with the chosen device, so the snapshot taken a moment later recorded
    # our own value as the original -- and after a restart, choosing Default
    # restored the device it was trying to leave. One place applies them, and
    # it is the one that can see what came before.

    # Window title. mpv's default is "No file - mpv", which names the
    # wrong application and reports "No file" for what is actually the
    # library browser. Property expansion is mpv's, evaluated live, so
    # the title follows playback without us pushing updates.
    mpv_options["title"] = "${?media-title:${media-title} - }%s" % USER_APP_NAME

    # Window size. mpv defaults to a fixed 960x540 whatever the display
    # size, which is cramped for a browsable UI. Restored from the last
    # session when remember_window_size is on (see _save_window_geometry).
    width = max(320, int(settings.window_width or 1280))
    height = max(240, int(settings.window_height or 720))
    mpv_options["geometry"] = "%dx%d" % (width, height)
    if settings.window_maximized:
        mpv_options["window_maximized"] = True
    # geometry is documented as an INITIAL size, but X11 re-applies it on
    # every VO reconfig (rc = geo.win whenever geometry.wh_valid), so a
    # window the user resized snapped back to the stored size on the next
    # file. _sync_window_geometry keeps the armed value equal to the live
    # size so that re-apply is a no-op; it must never be *cleared* at
    # runtime — see the comment there. auto-window-resize is the other
    # half: without it mpv fills the gap by resizing to each video's
    # native size, which is what geometry had been masking. Both are
    # needed — per mpv's own docs, auto-window-resize "does not have any
    # impact on the --geometry option".
    mpv_options["auto_window_resize"] = False

    # The in-window UI has to ask for its window on the command line.
    #
    # mpv before 0.41 accepts a runtime force-window change and stores it,
    # but never acts on it while idle: the VO is created only if the
    # option was set at startup, and once created it can no longer be
    # released. Measured on 0.40.0 vs 0.41.0 -- with --idle and no file,
    # setting force-window over IPC leaves `vo-configured` false on 0.40
    # and flips it true on 0.41. It is a version difference, not a backend
    # one; the libmpv path only looked fine here because the installed
    # libmpv was newer than the mpv binary. So on 0.40 set_browse_window
    # raised no window at all, and with the browser being the window's
    # entire content the app came up invisible and the tray's Show
    # Library Browser had nothing to show.
    #
    # First launch takes the window unless start_minimized asked for the
    # windowless state. A re-open (crash recovery, idle-quit) takes it
    # only if the browser was on screen: the play path doesn't need this,
    # because loading a file brings the VO up on its own. That distinction
    # needs the live player state, which is why the caller decides it.
    #
    # Only force_window is passed here, not the browse background --
    # background=color needs mpv 0.38, and an unknown option makes mpv
    # exit at startup rather than raise something recoverable.
    # set_browse_window applies the background a moment later.
    if osc_style == "mpvtk" and browser_wants_window:
        mpv_options["force_window"] = True

    # Desktop-icon hints. mpv has no "set the window icon" option; on
    # Linux the icon is resolved by matching the window's class against
    # an installed .desktop file, so naming ourselves after ours is the
    # whole mechanism. Only meaningful once the .desktop is installed
    # (packaged/Flatpak, not run-from-source), and some window managers
    # still prefer mpv's built-in _NET_WM_ICON — overriding that needs
    # Xlib, which is not worth a dependency.
    #
    # Platform-gated: --x11-name only exists in builds with X11 support,
    # so setting it on a Windows or macOS mpv fails at startup. Those
    # platforms take their icon from the exe/bundle anyway.
    if sys.platform not in ("win32", "darwin"):
        mpv_options["x11_name"] = DESKTOP_ID
        mpv_options["wayland_app_id"] = DESKTOP_ID

    return mpv_options
