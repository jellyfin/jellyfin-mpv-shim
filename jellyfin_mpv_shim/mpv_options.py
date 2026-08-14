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

import logging
import platform
import sys
from collections import OrderedDict

from . import conffile
from .conf import settings
from .constants import APP_NAME, DESKTOP_ID, USER_APP_NAME
from .utils import get_resource

log = logging.getLogger("mpv_options")

#: Styles that must not leave mpv's built-in OSC on: two that replace it
#: with something of ours, and one that replaces it with nothing.
_REPLACES_OSC = ("mpv", "mpvtk", "none")


#: Config value -> the mpv ``hwdec`` value, where it is a constant.
#: "over-1080p" is absent because it is not one: see :func:`hwdec_for`.
_HWDEC_STATIC = {"no": "no", "auto": "auto", "auto-copy": "auto-copy"}

#: Above this source height, "over-1080p" turns hardware decoding on.
#: Strictly greater, so a 1920x1080 file decodes in software and a 4K one
#: does not -- which is the line the setting is named after.
HWDEC_THRESHOLD_H = 1080


#: hwdec values that are a *policy* rather than a requirement: "use
#: hardware decoding if you can, whatever that turns out to be here". A
#: shader pack naming one of these is expressing an opinion about the
#: machine, which is not its to have.
#:
#: Every other value is a requirement of the profile and survives -- a
#: named decoder (``d3d11va``, which the shipped rtx-vsr needs for its
#: Direct3D filter) **and ``no``**, which is a profile saying its shaders
#: need software frames. Nothing in the current pack sets ``no``; it is
#: listed as a requirement rather than a policy because that is what it
#: would mean if one did [iw].
NAIVE_HWDEC = frozenset({
    "yes", "auto", "auto-safe", "auto-unsafe",
    "auto-copy", "auto-copy-safe", "auto-copy-unsafe",
})


def hwdec_pinned_by_config():
    """The ``hwdec`` the user's own ``mpv.conf`` sets, or None.

    **A pin, not a default: where this answers, nothing else writes hwdec
    at all** -- not the setting, not the copy upgrade, not a shader
    profile. Somebody who has written the option into mpv.conf has said
    something more specific than any of them, and silently overriding it
    was the complaint that the whole of this feature is downstream of.

    Deliberately a plain scan of the top level of the file rather than an
    mpv-accurate parse: profile sections (``[name]``) are conditional and
    reading them as unconditional would pin on a value that may never
    apply. A `hwdec` reachable only inside a profile is therefore *not*
    treated as a pin, which is the safe direction -- the setting keeps
    working and mpv's own precedence still applies it where it fires.
    """
    from . import conffile
    from .constants import APP_NAME

    try:
        path = conffile.get(APP_NAME, "mpv.conf", True)
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line.startswith("["):
                    break            # a profile section; see the docstring
                if not line:
                    continue
                key, sep, value = line.partition("=")
                if sep and key.strip().lstrip("-") == "hwdec":
                    return value.strip().strip("\"'") or None
    except Exception:
        log.debug("could not read mpv.conf for an hwdec pin", exc_info=True)
    return None


#: Direct mode -> the copy-back variant of the same thing. Used when
#: something in the pipeline needs frames in system RAM; see hwdec_for.
_COPY_OF = {"auto": "auto-copy"}


def hwdec_for(height=None, needs_copy=False):
    """mpv's ``hwdec`` for the configured mode and this file's height.

    ``needs_copy`` says something downstream needs frames in system RAM --
    a video filter, which the direct modes cannot feed. It **upgrades**, it
    never enables: off stays off, because the user turning hardware
    decoding off is not a preference about *which* hardware decoding. A
    mode that is already copy-back is unchanged.

    That asymmetry is the whole design. The shader pack asks for
    ``hwdec: auto-copy`` in every profile, which conflates two things --
    "turn hardware decoding on" (not the pack's call, and the reason the
    setting defaults off is a long tail of broken drivers) and "if it is
    on, I need the copy kind" (entirely the pack's call, since it knows
    what it is going to do with the frames). Only the second survives.

    ``height`` is the source's video height, or None when nothing is loaded
    (mpv's construction, and any item whose height we could not read). None
    resolves the threshold mode to "no": starting a file with hardware
    decoding already on and turning it off is the wrong way round -- the
    failure modes this setting is cautious about happen at *decoder init*.

    ``--disable-hwdec`` overrides everything and is checked here, so there
    is one place that can be wrong. It is a per-run override rather than a
    config write (like --ui-scale, unlike --reset-shaders): it exists for
    the case where hardware decoding stops the window opening at all, and
    once it has opened the setting is reachable in the ordinary way.
    """
    from .args import get_args

    try:
        if getattr(get_args(), "disable_hwdec", False):
            return "no"
    except Exception:
        # Argument parsing is not available in every embedding of this
        # module (tests import it bare). A missing override is "no
        # override", never a crash on the playback path.
        log.debug("could not read the hwdec override", exc_info=True)
    pinned = hwdec_pinned_by_config()
    if pinned is not None:
        # Not "use their value" -- *do not write the option at all*, so
        # mpv's own config precedence stands and nothing here has to model
        # it. Callers treat None as "leave it alone".
        return None

    def resolved(value):
        return _COPY_OF.get(value, value) if needs_copy else value

    mode = (settings.hwdec or "no").strip().lower()
    if mode in _HWDEC_STATIC:
        return resolved(_HWDEC_STATIC[mode])
    if mode == "over-1080p":
        try:
            on = int(height or 0) > HWDEC_THRESHOLD_H
        except (TypeError, ValueError):
            on = False
        return resolved("auto") if on else "no"
    # A value hand-edited into the JSON. Software decoding is the answer
    # that cannot make things worse, and handing an unknown string to mpv
    # would fail the option at construction and take the player with it.
    log.warning("Unknown hwdec setting %r; using software decoding.", mode)
    return "no"


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


#: The option mpv only registers when it was built with lua.
#:
#: A build without lua does not ignore ``--osc``, it refuses to start.
#: Measured against a `-Dlua=disabled` mpv 0.41, on both backends: libmpv
#: raises ``AttributeError('mpv option does not exist', ...)`` from the
#: constructor, and the external binary prints "Error parsing option osc
#: (option not found)" and exits, which reaches the shim as
#: ``MPVError("MPV process retry limit reached.")`` after burning every
#: start retry on it.
#:
#: That made the lua fallback unreachable: `lua_works` needs a live mpv to
#: probe, and the app died constructing one.
OSC_OPTION = "osc"


#: What each ``motion_interpolation`` value writes, as mpv property names.
#:
#: All three ON values set ``video-sync`` as well, and that is not
#: incidental: mpv's own manual says ``--interpolation`` "requires setting
#: the --video-sync option to one of the display- modes, or it will be
#: **silently disabled**". A setting that writes only ``interpolation`` is
#: therefore a setting that does nothing and reports success, which is why
#: the pair is a table here rather than two independent options -- and why
#: `tests/test_motion_interpolation.py` asserts they travel together.
#:
#: The filters are mpv's, and they are three different trades rather than
#: three quality tiers:
#:
#: * ``oversample`` is mpv's own default and barely blends at all -- it
#:   holds each frame and crossfades only across the transition, which is
#:   MPC's "smooth motion". Judder goes, sharpness stays.
#: * ``linear`` is a true cross-fade between the two nearest frames. The
#:   smoothest motion of the three and visibly softer on a pan, which some
#:   people want and some cannot stand.
#: * ``mitchell`` is a wider kernel over more frames: smoother still, and
#:   the one that costs enough GPU to matter. On hardware that cannot keep
#:   up it drops frames, which looks like the judder it was turned on to
#:   fix -- so it is offered last and labelled as the expensive one.
INTERPOLATION_PRESETS = {
    "off": {},
    "smooth": {"video-sync": "display-resample",
               "interpolation": True, "tscale": "oversample"},
    "blend": {"video-sync": "display-resample",
              "interpolation": True, "tscale": "linear"},
    "hq": {"video-sync": "display-resample",
           "interpolation": True, "tscale": "mitchell"},
}


#: Every property any preset writes. What "off" has to put back is this
#: whole set, not the ones the CURRENT preset happens to name -- somebody
#: who used `hq` and then switched to off must get their `tscale` back too.
INTERPOLATION_KEYS = tuple(sorted(
    {key for props in INTERPOLATION_PRESETS.values() for key in props}))


def interpolation_props():
    """``{mpv property: value}`` for the configured preset, or ``{}``.

    ``{}`` for "off" is deliberate and is NOT the same as writing the
    defaults back: ``video-sync`` is a timing mode somebody may reasonably
    have chosen in their own ``mpv.conf``, and an "off" that wrote
    ``audio`` over it would be this setting overriding a more specific
    statement -- the mistake ``hwdec_pinned_by_config`` exists to avoid.
    Turning the feature off is the player's job, and it restores what was
    there before it first wrote (PlayerManager._apply_interpolation).

    An unrecognised value reads as off. It is a plain string in a JSON file
    somebody can type into, and the alternative to a default is a KeyError
    out of the middle of starting playback.
    """
    return dict(INTERPOLATION_PRESETS.get(
        settings.motion_interpolation, INTERPOLATION_PRESETS["off"]))


def deinterlace_value(override=None):
    """mpv's ``deinterlace`` for the configured mode, or for ``override``.

    ``override`` is the playback HUD's per-session toggle: ``True`` forces
    it on for a file whose interlacing is not flagged, ``None`` means
    nobody has said anything and the setting decides.
    """
    if override is not None:
        return "yes" if override else "no"
    return "auto" if settings.deinterlace_auto else "no"


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

    # Hardware decoding, at construction. The threshold mode resolves to
    # "no" here (nothing is loaded, so there is no height) and is raised
    # per file in PlayerManager._play_media -- which is also where the
    # static modes are re-applied, so that changing this setting takes
    # effect on the next item rather than the next launch.
    hwdec = hwdec_for()
    if hwdec is not None:
        mpv_options["hwdec"] = hwdec

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
