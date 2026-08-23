"""Assembling the option set mpv is constructed with.

A pure function of ``settings`` plus three facts the player knows (which
backend is in use, whether the browser wants a window, whether trickplay
started), so the answers can be checked without constructing an mpv -- which,
on the libmpv backend, means opening a real window.

Nothing here touches mpv, and nothing here imports ``player``. The
directory-creating calls (``conffile.get_dir`` / ``conffile.get``)
deliberately live in ``_init_mpv`` instead: ``confdir`` is a path lookup and
safe to call, but creating the config tree is a side effect and belongs with
the code that is actually starting a player.

Before editing this file, read ``docs/mpv-backends.md``.
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
#: A build without lua does not ignore ``--osc``, it refuses to start -- on
#: both backends, and differently on each. That made the lua fallback itself
#: unreachable, since `lua_works` needs a live mpv to probe and the app died
#: constructing one. Both reports, and why none of them has to be parsed:
#: docs/mpv-backends.md section 2.
OSC_OPTION = "osc"


#: The option mpv only registers when it was built with SDL2 gamepad support.
#:
#: `sdl2-gamepad` is `value: 'disabled'` in mpv's own `meson.options` -- not
#: `auto` -- so having SDL2 installed is not enough and most builds simply do
#: not have this. Debian's does; shinchiro's Windows builds pass
#: `-Dsdl2-gamepad=enabled` explicitly, so the mpv-2.dll CI ships does too.
#:
#: Like `OSC_OPTION`, an mpv without it refuses to start rather than ignoring
#: the flag, so `_construct_mpv` drops it and remembers (docs/mpv-backends.md
#: sections 2 and 4). Only ever present when the user asked for it.
#:
#: It must be set at construction. mpv reads `use_gamepad` exactly once, in
#: `mp_input_load_config` (input/input.c), but the option group carries
#: UPDATE_INPUT -- so a runtime write *succeeds and reads back yes* while the
#: SDL thread is never started. Measured; the setting would look applied and
#: do nothing.
GAMEPAD_OPTION = "input_gamepad"


#: What each ``motion_interpolation`` value writes, as mpv property names.
#:
#: All three ON values set ``video-sync`` as well, and that is not
#: incidental: ``--interpolation`` is **silently disabled** without a
#: display- sync mode, so a setting writing only ``interpolation`` would do
#: nothing and report success. Hence one table rather than two independent
#: options, and hence `tests/test_picture_processing.py` asserting that the
#: pair travels together.
#:
#: The three filters are three different trades rather than three quality
#: tiers; what each one does is in docs/configuration.md under
#: ``motion_interpolation``.
INTERPOLATION_PRESETS = {
    "off": {},
    "smooth": {"video-sync": "display-resample",
               "interpolation": True, "tscale": "oversample"},
    "blend": {"video-sync": "display-resample",
              "interpolation": True, "tscale": "linear"},
    "hq": {"video-sync": "display-resample",
           "interpolation": True, "tscale": "mitchell"},
}


#: Debanding, which is what the ``deband`` setting picks between.
#:
#: **Not offered as the four raw knobs on purpose.** Anyone who wants a
#: specific combination writes it in their own ``mpv.conf`` and leaves this
#: on "off", which -- see :func:`preset_props` -- writes nothing at all and
#: therefore leaves their values standing. The presets are for everyone
#: else, who wants the banding gone and has no way to tell 32 from 48.
#:
#: ``threshold`` (how flat a region must be before it is touched),
#: ``iterations`` (how many passes) and ``grain`` (the noise added
#: afterwards to mask what debanding could not fix) rise together: those are
#: the strength axes, and a strong threshold with no grain looks worse than
#: either alone.
#:
#: **``range`` moves the other way, and that is mpv's instruction rather
#: than a preference.** It is the filter's initial radius, and mpv's manual
#: says the radius "increases linearly for each iteration" and then, in as
#: many words: "If you increase the --deband-iterations, you should probably
#: decrease this to compensate." An earlier version of this table raised all
#: four together because a monotone ladder looked tidier -- which is exactly
#: the kind of reasoning that has no source behind it. `light` therefore
#: sits at mpv's own default radius, since it also runs mpv's own single
#: iteration.
#:
#: mpv's own defaults are threshold 48, range 16, grain 32, iterations 1
#: (measured on 0.41, and pinned by tests/test_picture_processing.py), so
#: "standard" is roughly mpv's strength with a second pass and "light" sits
#: deliberately below it -- live action fails the flatness test almost
#: everywhere, so the risk there is a threshold high enough to smear real
#: low-contrast texture rather than debanding being wrong in principle.
#:
#: **This is not the shader pack's debanding.** ``pack.json`` lists
#: ``deband-default`` under ``default-setting-groups``, which
#: ``video_profile.load_profile`` applies -- so debanding today arrives
#: bundled with picking an upscaler and leaves again when it is unloaded.
#: The pack's ``deband-grain: 0`` is only correct because
#: ``static-grain-default`` re-adds noise through shaders; copying that
#: number here would remove the masking without replacing it.
DEBAND_PRESETS = {
    "off": {},
    "light": {"deband": True, "deband-iterations": 1,
              "deband-threshold": 32, "deband-range": 16,
              "deband-grain": 16},
    "standard": {"deband": True, "deband-iterations": 2,
                 "deband-threshold": 48, "deband-range": 14,
                 "deband-grain": 24},
    "strong": {"deband": True, "deband-iterations": 4,
               "deband-threshold": 64, "deband-range": 12,
               "deband-grain": 32},
}


#: HDR-to-SDR tone mapping curve. mpv's own vocabulary rather than invented
#: preset names, because unlike debanding these are not a strength ladder --
#: they are different curves with different opinions about what to do with
#: the highlights, and mpv documents each one.
#:
#: **Only does anything when the output is SDR.** Where the display takes
#: HDR and ``player_window`` has hinted the colorspace through, mpv is not
#: tone mapping at all and every value here is equally inert. That is the
#: note the setting carries in the UI, since a control that silently does
#: nothing on the machines that most want HDR handled would otherwise read
#: as broken.
TONE_MAPPING_PRESETS = {
    "auto": {},
    "bt.2390": {"tone-mapping": "bt.2390"},
    "bt.2446a": {"tone-mapping": "bt.2446a"},
    "spline": {"tone-mapping": "spline"},
    "hable": {"tone-mapping": "hable"},
    "reinhard": {"tone-mapping": "reinhard"},
    "clip": {"tone-mapping": "clip"},
}


#: What mpv's own ``high-quality`` profile sets, written as properties.
#:
#: Written out rather than applied as ``profile=high-quality``, and the
#: reason is that a profile **cannot be taken back**: mpv has no way to read
#: one back, which is why the shader pack lists ``profile`` in
#: ``setting-revert-ignore`` and never reverts it. A setting that can only
#: be turned on is not a setting. These four are ordinary properties with
#: readable values, so the same snapshot-and-restore every other entry here
#: uses works on them.
#:
#: The contents are mpv's, not ours, and they have changed across versions
#: (``gpu-hq`` is now an alias for this). ``tests/test_picture_processing``
#: asks the installed mpv what the profile contains and fails if this table
#: has drifted from it, so the copy stays honest rather than quietly
#: becoming a different preset than the one it is named after.
RENDER_QUALITY_PRESETS = {
    "default": {},
    "high": {"scale": "ewa_lanczossharp", "scale-antiring": 0.6,
             "hdr-peak-percentile": 99.995, "hdr-contrast-recovery": 0.30},
}


#: How much the demuxer reads ahead. For a client whose every file arrives
#: over a network this is the option users reach for first, and mpv's
#: default readahead is **one second** -- generous on bytes (150 MiB) and
#: very short on time, which is the wrong shape for a remote server on a
#: slow or jittery link.
#:
#: Bytes are spelled as integers rather than as "400MiB": the string form is
#: mpv's own command-line parsing, and a property write wants the number.
#:
#: Unlike the picture settings, these are read when the **demuxer starts**,
#: so a change lands on the next thing played rather than on what is already
#: open. That is the same granularity ``_play_media`` applies everything else
#: at, so nothing special is needed -- but it is why "restore" here is not
#: visible until the next file either.
BUFFER_PRESETS = {
    "default": {},
    "large": {"demuxer-max-bytes": 400 * 1024 * 1024,
              "demuxer-max-back-bytes": 100 * 1024 * 1024,
              "demuxer-readahead-secs": 20},
    "huge": {"demuxer-max-bytes": 1024 * 1024 * 1024,
             "demuxer-max-back-bytes": 200 * 1024 * 1024,
             "demuxer-readahead-secs": 60},
}


#: setting key -> (preset table, the value meaning "leave mpv alone").
#:
#: One registry rather than five bespoke apply methods. The first entry was
#: the only one for a long time and the other four are the same shape, which
#: is precisely the argument for generalising: the second implementation of
#: a discipline is where it gets subtly dropped, and what would have been
#: dropped here is the snapshot -- the half that makes "off" mean "give the
#: user their own value back" instead of "write our idea of off over it".
#:
#: ``PlayerManager._apply_render_presets`` walks this, so adding an option
#: group is a table entry and a config key, not a new method.
PRESET_SETTINGS = OrderedDict((
    ("motion_interpolation", (INTERPOLATION_PRESETS, "off")),
    ("deband", (DEBAND_PRESETS, "off")),
    ("tone_mapping", (TONE_MAPPING_PRESETS, "auto")),
    ("render_quality", (RENDER_QUALITY_PRESETS, "default")),
    ("network_buffer", (BUFFER_PRESETS, "default")),
))


def preset_keys(key):
    """Every mpv property any preset of ``key`` writes.

    What "off" has to put back is this whole set, not the ones the CURRENT
    preset happens to name -- somebody who used interpolation's `hq` and
    then switched to off must get their `tscale` back too.
    """
    presets, _fallback = PRESET_SETTINGS[key]
    return tuple(sorted({prop for props in presets.values() for prop in props}))


def preset_props(key):
    """``{mpv property: value}`` for ``key``'s configured preset, or ``{}``.

    ``{}`` for the off value is deliberate and is NOT the same as writing
    mpv's defaults back. Every property in these tables is one somebody may
    reasonably have set in their own ``mpv.conf``, and all five settings
    default to off -- so an off that wrote its idea of "not doing this"
    would reach out on the first item and undo their config, with no setting
    here to put it back. Turning the feature off is the player's job, and it
    restores what was there before it first wrote
    (``PlayerManager._apply_render_preset``, docs/mpv-backends.md §6).

    This is also what makes "leave it off and write your own" a supported
    way to use these settings rather than an accident.

    An unrecognised value reads as off. It is a plain string in a JSON file
    somebody can type into, and the alternative to a default is a KeyError
    out of the middle of starting playback.
    """
    presets, fallback = PRESET_SETTINGS[key]
    return dict(presets.get(getattr(settings, key, fallback),
                            presets[fallback]))


#: Kept as names of their own because they are what the interpolation
#: docstrings, the tests and docs/mpv-backends.md §6 already cite.
INTERPOLATION_KEYS = preset_keys("motion_interpolation")


def interpolation_props():
    """``{mpv property: value}`` for the configured preset, or ``{}``.
    See :func:`preset_props`."""
    return preset_props("motion_interpolation")


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
    # force-window is only live from mpv 0.41 (docs/mpv-backends.md §3):
    # an older build stores a runtime change and never acts on it while
    # idle, so set_browse_window raised no window at all, the app came up
    # invisible, and the tray's Show Library Browser had nothing to show.
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

    # Game controllers. Only added when asked for, so the default path never
    # carries a build-gated option and can never pay for one.
    if settings.input_gamepad:
        mpv_options[GAMEPAD_OPTION] = True

    return mpv_options
