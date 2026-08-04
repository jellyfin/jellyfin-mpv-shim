"""Settings-schema helpers for the mpvtk browser's Settings view.

Classifies the
``conf.Settings`` annotations the Tk browser's form uses, and reads/writes
the in-process ``conf.settings`` singleton directly (no IPC — the mpvtk
browser runs in the player's process).
"""

import sys
import typing

from ..conf import Settings, settings
from ..i18n import _, _p

# Structured / non-scalar config that the flat form can't express, plus
# internal bookkeeping. Everything else is editable.
# client_uuid is the device identity the server keys sessions on; editing it
# free-text orphans every session and playstate the server has recorded.
# config_version is migration bookkeeping; editing it re-runs or skips
# one-time upgrades.
# window_width/height/maximized are remembered window *state*, rewritten on
# every exit — editing them in a form the app then overwrites is a setting
# that appears not to work. The preference that governs them,
# remember_window_size, stays visible.
_HIDDEN = {"language_config", "client_uuid", "config_version",
           "window_width", "window_height", "window_maximized"}

# Passthrough toggles, in the order they should appear, paired with the mpv
# codec name that decides whether the current audio mode offers them at all.
# The mode -> codec mapping itself lives in player_audio.py
# (AUDIO_PASSTHROUGH_CODECS) so there is one place that knows what a cable
# can carry.
AUDIO_PASSTHROUGH_KEYS = [
    ("ac3", "audio_passthrough_ac3"),
    ("dts", "audio_passthrough_dts"),
    ("eac3", "audio_passthrough_eac3"),
    ("dts-hd", "audio_passthrough_dts_hd"),
    ("truehd", "audio_passthrough_truehd"),
]

# Audio settings that only mean something in particular modes. Hidden
# elsewhere rather than shown as a control that quietly does nothing.
AUDIO_MODE_ONLY = {
    "audio_optical_encode_ac3": {"optical"},
}

# Exactly one of these is shown, because they are the same question asked of
# two different machines: "keep running once the window is gone". With a tray
# that is close-to-tray and the tray is the way back; without one the app is
# invisible and only `jellyfin-mpv-shim stop` ends it, which is a different
# enough deal to need its own opt-in rather than a toggle that silently
# changes meaning.
TRAY_DEPENDENT = ("close_to_tray", "allow_background")

# mpv honours --audio-exclusive on wasapi, coreaudio and sndio only; on ALSA
# and PulseAudio/PipeWire it is accepted and ignored. Hidden where it cannot
# work rather than offered as a checkbox that does nothing -- the same rule
# AUDIO_MODE_ONLY applies to the passthrough toggles.
#
# On the platform, not the running AO: mpv picks its AO per file (a
# passthrough track can send it down the list to one that can carry the
# format), so a control that appeared and vanished with the last file played
# would be worse than one that is simply absent on Linux.
EXCLUSIVE_PLATFORMS = ("win32", "darwin")

# Starting minimized asks the app to come up in the state whichever of the
# above is on screen permits, so offering it while that one is off is
# offering a setting that cannot do anything. Shown directly below it.
BACKGROUND_DEPENDENT = ("start_minimized",)

# Curated groups, mirroring the Tk browser's form. Anything not listed shows
# under "Advanced".
SECTIONS = [
    # enable_gui and headless are deliberately *not* here. Both are one-way
    # doors from the settings form's point of view: enable_gui doesn't
    # disable "the Jellyfin UI", it drops the whole app to CLI mode -- no
    # window, no tray, no settings -- and headless makes the cast screen the
    # only page, so with no system tray installed there is nothing left to
    # reach Settings from. Either way the way back is hand-editing conf.json,
    # which is not a thing to leave one click away in the main list. They stay
    # editable under Advanced, with notes saying what they cost. Someone who
    # wants mpv's own controls wants osc_style, which is in this section.
    (_("Interface"), ["player_name", "browser_fullscreen",
                      "display_mirror_summon",
                      "close_to_tray", "allow_background",
                      "start_minimized",
                      "remember_window_size", "window_controls",
                      "fullscreen", "osc_style",
                      "hud_grab_keys", "hud_wake_key",
                      "hud_scrim", "hud_autohide", "hud_hide_secs",
                      "mouse_chapter_nav", "raise_mpv",
                      "discord_presence",
                      "check_updates", "notify_updates"]),
    # The three startup-applied "look" settings, together: the theme sets the
    # palette and its own cover size, and these two can override the sizing.
    (_("Theme"), ["theme", "poster_scale", "ui_scale"]),
    (_("Playback"), ["auto_play", "always_transcode", "local_kbps",
                     "remote_kbps", "direct_paths", "remote_direct_paths",
                     "playback_timeout"]),
    # Passthrough keys are listed in full here; sections() drops the ones the
    # selected mode cannot carry.
    (_("Audio"), ["audio_device", "audio_exclusive",
                  "audio_mode", "audio_night_mode"]
                 + [k for _c, k in AUDIO_PASSTHROUGH_KEYS]
                 + ["audio_optical_encode_ac3"]),
    (_("Subtitles & Languages"), ["subtitle_size", "subtitle_color",
                                  "subtitle_position", "language_preference",
                                  "preferred_language", "remember_audio_track",
                                  "remember_subtitle_track", "lang_filter",
                                  "lang_filter_sub", "lang_filter_audio"]),
    (_("Transcoding"), ["allow_transcode_to_h265", "prefer_transcode_to_h265",
                        "transcode_hevc", "transcode_av1", "transcode_4k",
                        "transcode_hdr", "transcode_hi10p",
                        "transcode_dolby_vision", "force_video_codec",
                        "force_audio_codec"]),
    (_("Video Enhancement"), ["shader_pack_enable", "shader_pack_subtype",
                              "shader_pack_remember", "shader_pack_gpu_api"]),
    (_("Skip Intro / Credits"), ["segment_intro", "segment_outro",
                                 "segment_commercial", "segment_preview",
                                 "segment_recap", "skip_intro_on_seek"]),
    (_("Library Browser"), ["library_image_cache_mb", "scroll_wheel_pixels",
                            "scroll_mode", "paginated", "logo_legibility_live_tv",
                            "logo_legibility_library"]),
    (_("Downloads"), ["sync_path", "prefer_downloaded",
                      "auto_download_enable", "auto_download_next_up",
                      "auto_download_next_up_limit",
                      "auto_download_lookahead", "auto_download_max_gb",
                      "auto_download_delete_watched",
                      "auto_download_keep_days",
                      "auto_download_interval_mins"]),
]

# Free-text is wrong for these: an unlisted value silently breaks the feature.
ENUMS = {
    "subtitle_position": ["top", "bottom", "middle"],
    "mpv_log_level": ["fatal", "error", "warn", "info", "debug", "noise"],
    "shader_pack_subtype": ["lq", "hq"],
}

#: Shared by the five media-segment settings below.
_SEGMENT_ACTIONS = [
    (_("Never"), "off"),
    (_("Ask"), "ask"),
    (_("Always"), "always"),
]

# Enums whose stored value isn't presentable: [(label, value), ...].
LABELED_ENUMS = {
    "osc_style": [
        (_("Jellyfin UI"), "mpvtk"),
        (_("MPV UI with thumbnails"), "mpv"),
        (_("MPV built-in default"), "default"),
        (_("No player controls"), "none"),
    ],
    "ui_scale": [
        (_("Follow display"), None),
        (_("100% (no scaling)"), 1.0),
        (_("125%"), 1.25),
        (_("150%"), 1.5),
        (_("200%"), 2.0),
    ],
    # "theme" is deliberately NOT here: themes are JSON files and the user can
    # add their own, so the list is built at display time by
    # settings.general._dynamic_enum.
    # Order and values must match conf.SCROLL_MODES, which is what actually
    # decides behaviour; test_mpvtk_adopt pins them together.
    # Phrased as what the user sees, not as "client-side decorations":
    # "auto" is not a guess about the desktop, it is MPV reporting whether
    # anything decorated this window. See conf.window_controls.
    "window_controls": [
        (_("Only when the window has no title bar"), "auto"),
        (_("Always"), "always"),
        (_("Never"), "never"),
    ],
    "scroll_mode": [
        (_("Continuous"), "continuous"),
        (_("Aligned to rows"), "aligned"),
        (_("One row per notch"), "row"),
    ],
    # One list, five settings: the three things that can be done about a
    # media segment (jellyfin-web offers the same three).
    "segment_intro": _SEGMENT_ACTIONS,
    "segment_outro": _SEGMENT_ACTIONS,
    "segment_commercial": _SEGMENT_ACTIONS,
    "segment_preview": _SEGMENT_ACTIONS,
    "segment_recap": _SEGMENT_ACTIONS,
    "hud_scrim": [
        (_("Default"), "default"),
        (_("Panel behind the controls"), "panel"),
        (_("None (shadowed text)"), "none"),
    ],
    "hud_autohide": [
        (_("Hide unless hovered"), "hover"),
        (_("Always hide"), "always"),
        (_("Never hide while paused"), "paused"),
    ],
    # Every label points at the value it has always pointed at -- "Small" is
    # the base size, which reads oddly beside two smaller steps but is the
    # price of not silently re-pointing a string 86 locales have translated.
    "poster_scale": [
        (_("Theme default"), None),
        (_("Extra Compact"), 0.75),
        (_("Compact"), 0.85),
        (_("Small"), 1.0),
        (_("Medium"), 1.2),
        (_("Large"), 1.4),
        (_("Extra Large"), 1.7),
    ],
    "shader_pack_gpu_api": [
        (_("Automatic (recommended)"), "auto"),
        (_("Vulkan"), "vulkan"),
        (_("Direct3D 11 (Windows only)"), "d3d11"),
        (_("OpenGL (compatibility)"), "opengl"),
    ],
    "audio_mode": [
        (_("Default (auto)"), "auto"),
        (_("Force Stereo"), "stereo"),
        (_("Optical Surround"), "optical"),
        (_("HDMI Passthrough"), "hdmi"),
    ],
    "language_preference": [
        (_("Unset"), "unset"),
        (_("Dubbed (shows only)"), "dubbed_shows"),
        (_("Subbed (shows only)"), "subbed_shows"),
        (_("Dubbed (all)"), "dubbed_all"),
        (_("Subbed (all)"), "subbed_all"),
        (_("Custom (set in config)"), "custom"),
    ],
}

LABEL_OVERRIDES = {
    "sync_path": _("Download Folder"),
    "prefer_downloaded": _("Prefer Downloaded Copy"),
    "auto_download_enable": _("Automatically Download Upcoming Episodes"),
    "auto_download_next_up": _("Include Next Up"),
    "auto_download_next_up_limit": _("Next Up Entries to Consider"),
    "auto_download_lookahead": _("Episodes to Keep Ahead (0 = off)"),
    "auto_download_max_gb": _("Storage Limit for Automatic Downloads (GB)"),
    "auto_download_delete_watched": _("Delete Automatic Downloads Once Watched"),
    "auto_download_keep_days": _("Delete Unwatched After (days, 0 = never)"),
    "auto_download_interval_mins": _("Check Every (minutes)"),
    "close_to_tray": _("Close to Tray (keep running)"),
    "allow_background": _("Keep Running in Background"),
    "remember_window_size": _("Remember Window Size"),
    "window_controls": _("Window Buttons in the Top Bar"),
    "osc_style": _("Player Controls Style"),
    "discord_presence": _("Show What You're Watching in Discord"),
    "ui_scale": _("Interface Scale"),
    "theme": _("Theme"),
    "poster_scale": _("Cover Size"),
    "logo_legibility_live_tv": _("Make Live TV logos more legible"),
    "logo_legibility_library": _("Make library logos more legible"),
    "headless": _("Cast-target mode (no library browsing)"),
    "display_mirror_summon": _("Casting Opens the Library Browser"),
    "browser_fullscreen": _("Fullscreen Library Browser"),
    "hud_grab_keys": _("Always Bind Arrow Keys to Player Controls"),
    "hud_wake_key": _("Player Controls Activation Key"),
    "segment_intro": _("Skip Intros"),
    # The one segment label that collides with its own Skip BUTTON:
    # gettext keys on the English, the other four are pluralised ("Skip
    # Intros" vs the button's "Skip Intro"), and "Credits" is already
    # plural. The context goes on the LABEL rather than the button because
    # the button is the string people actually see, and a context discards
    # every existing translation of the string it is added to.
    "segment_outro": _p("setting", "Skip Credits"),
    "segment_commercial": _("Skip Commercials"),
    "segment_preview": _("Skip Previews"),
    "segment_recap": _("Skip Recaps"),
    "hud_scrim": _("Shading Behind the Player Controls"),
    "hud_autohide": _("When the Player Controls Hide"),
    "hud_hide_secs": _("Hide the Player Controls After (seconds)"),
    "mouse_chapter_nav": _("Mouse Back/Forward Buttons Skip Chapters"),
    "audio_mode": _("Audio Output Mode"),
    "audio_device": _("Audio Output Device"),
    "audio_exclusive": _("Take the Device Exclusively"),
    "audio_night_mode": _("Night Mode (Auto Volume Adj)"),
    "shader_pack_enable": _("Enable Video Playback Profiles"),
    "shader_pack_subtype": _("Profile Group"),
    "shader_pack_remember": _("Remember Last Used Profile"),
    "shader_pack_gpu_api": _("Graphics API for Shaders"),
    "audio_passthrough_ac3": _("Pass Through AC3"),
    "audio_passthrough_dts": _("Pass Through DTS"),
    "audio_passthrough_eac3": _("Pass Through E-AC3"),
    "audio_passthrough_dts_hd": _("Pass Through DTS-HD"),
    "audio_passthrough_truehd": _("Pass Through TrueHD"),
    "audio_optical_encode_ac3": _("Re-encode Others to AC3"),
}

# The classic MPV Shim behaviour is three ordinary settings and nothing said
# so, which is how people ended up reaching for enable_gui (which does
# something else entirely) to get it. Shown under whichever keep-running
# toggle this machine has -- they are the same question, so the recipe reads
# the same under either.
CAST_TARGET_NOTE = _(
    "With \"Start Minimized\" and \"Fullscreen\" this is the classic "
    "cast-target setup: the app waits out of the way, plays fullscreen when "
    "something casts to it, and goes back to waiting when the video ends or "
    "you press Q.")

# Explanatory line rendered under a setting, for the ones whose default
# isn't self-explanatory from the label alone.
NOTES = {
    "close_to_tray": CAST_TARGET_NOTE,
    "allow_background": CAST_TARGET_NOTE,
    # Advanced-only (see SECTIONS), and the note is why: it reads like "turn
    # off the Jellyfin UI", and what it actually does is leave a Windows user
    # with a process they can neither see nor quit.
    "enable_gui": _("Off means command-line mode: no window, no system tray "
                    "and no settings screen, so the only way back is editing "
                    "conf.json by hand. It is not how you get MPV's own "
                    "on-screen controls — \"Player Controls Style\" under "
                    "Interface does that."),
    # Advanced-only for the same reason as enable_gui: with no tray icon
    # installed, the cast screen is the only page and Settings is gone.
    "headless": _("Show only what is cast to this machine — the library, "
                  "including this settings screen, can't be reached from "
                  "here. Without a system tray icon the only way back is "
                  "editing conf.json. For the classic cast-target setup you "
                  "want the Interface settings instead; this is for a shared "
                  "TV nobody should be able to browse from."),
    # Says what it is for, not what it is called. "Client-side decorations"
    # is the right term and means nothing to the person whose window has no
    # close button; the note has to be recognisable from the symptom.
    "window_controls": _("Some desktops \u2014 GNOME on Wayland in particular \u2014 "
                         "draw no title bar on the player window, leaving no "
                         "way to move or close it. This puts minimize, "
                         "maximize and close in the top bar instead, and "
                         "lets you drag the window by it. Left on "
                         "\"Only when the window has no title bar\", MPV is "
                         "asked whether this window got one, so desktops "
                         "that do decorate it are left alone."),
    "osc_style": _("Requires restart to change. MPV keybinds are used by "
                   "default. Press ENTER to drive the player controls by "
                   "keyboard. \"No player controls\" leaves playback bare; "
                   "the library, the keyboard shortcuts and the menu key "
                   "still work."),
    "scroll_wheel_pixels": _("Pixels one wheel notch scrolls. On a grid this "
                             "is rounded so a whole number of notches spans "
                             "one row, whichever scroll mode you are in — a "
                             "trackpad or trackball never leaves you a sliver "
                             "of a row off. Raise it to scroll faster, lower "
                             "it for finer control."),
    # Names no mechanism and no measurement: the reason to pick one of these
    # is what you can see happening, so the note is written as symptoms.
    # "Continuous" says what it does rather than promising smoothness --
    # "Smooth scrolling" would be read as *animated* scrolling, and someone
    # who turned it on and got no animation would report it broken.
    "scroll_mode": _("\"Continuous\" scrolls by pixels and lands wherever the "
                     "wheel puts it; it lines rows up by itself if the "
                     "display cannot keep up. Pick \"Aligned to rows\" if "
                     "scrolling still stutters — on a very large display, a "
                     "slow or remote one, or with an external MPV. Pick "
                     "\"One row per notch\" to move a whole row (or one "
                     "home-screen section) at a time."),
    # Deliberately says nothing about the pypresence dependency: the Windows
    # build bundles it, so naming a package most users will never have to
    # think about only invites questions. The dynamic note in
    # settings/general.py raises it, and only for someone it is actually
    # broken for.
    "discord_presence": _("Discord Rich Presence. Takes effect after a "
                          "restart."),
    "audio_device": _("Leave this to Default unless setting up passthrough. "
                      "Note some audio servers like Pipewire don't like "
                      "passthrough and will need to be disabled for a card "
                      "before it'll let *any* audio through in passthrough "
                      "mode. In my tests I got silence otherwise, but results "
                      "may vary."),
    "audio_exclusive": _("Stop anything else using the device while playing. "
                         "Needed for passthrough on some systems, and it "
                         "means other applications will be silent."),
    "paginated": _("Page the library and music tile grids instead of "
                   "scrolling: each page is one screenful with First / "
                   "Previous / Next / Last controls and a page number you can "
                   "type into. Easier than precise scrolling on a trackpad."),
    "logo_legibility_live_tv": _(
        "Channel logos come as ink on a transparent background, drawn for the "
        "white page other clients put them on — so on a dark one the black "
        "ones vanish. On, they are backed with the light plate they were made "
        "for, and the few whose own outline is white get a drop shadow so "
        "they still have an edge against it. Off, they get the theme's card "
        "colour and no shadows, as Jellyfin Web does."),
    "logo_legibility_library": _(
        "The same treatment for a library set to draw Logo artwork. Off by "
        "default, because a film's or series' logo is white by convention and "
        "already reads on a dark background — the plate is what makes it need "
        "a shadow. Turn it on if yours are dark."),
    "theme": _("Palette, glow, cover style and default cover size. Colours "
               "change immediately; cover and heading sizes take effect "
               "after a restart."),
    "poster_scale": _("Overrides the theme's cover size. Applies "
                      "immediately, and is also on the View menu of any "
                      "library."),
    "hud_scrim": _("The controls have to stay legible over any frame. "
                   "\"None\" gives the text a drop shadow instead of "
                   "shading the picture behind it."),
    "hud_autohide": _("\"Hide unless hovered\" keeps them up only while "
                      "the pointer is on them, paused or not."),
    "hud_hide_secs": _("0 hides them as soon as the pointer is not on "
                       "them, and forces \"Hide unless hovered\"."),
    "mouse_chapter_nav": _("During playback only — in the library those "
                           "buttons stay Back and Forward. Off by default "
                           "because they are easy to hit by accident on some "
                           "mice. Takes effect after a restart."),
    # xgettext: no-python-format
    # "100% on" reads as the conversion "% o" (space flag, octal), so xgettext
    # marks this python-format and msgfmt --check then rejects any translation
    # that does not carry the fake directive through -- which zh_Hans already
    # tripped over. This string is a NOTES entry rendered as-is and is never
    # %-formatted, so the flag is wrong rather than unmet. The override changes
    # no msgid, so nothing already translated is discarded.
    "ui_scale": _("Takes effect after a restart. \"Follow display\" uses the "
                  "scale your desktop reports, which is 100% on X11."),
    "audio_mode": _("\"Default\" changes nothing and lets MPV (and your own "
                    "mpv.conf) decide. Pick a mode only if you are sending "
                    "audio to a receiver."),
    "audio_optical_encode_ac3": _("Audio your receiver can't be sent directly "
                                  "is encoded to AC3, which is the only way "
                                  "surround fits down an optical cable. Turn "
                                  "this off if the encoder causes audio delay "
                                  "— those tracks become stereo instead."),
    "shader_pack_subtype": _("\"hq\" offers heavier profiles. Pick it if you "
                             "have a fast graphics card."),
    "shader_pack_gpu_api": _("Leave on Automatic unless video breaks when a "
                             "profile is loaded. This only applies while a "
                             "profile is active, and OpenGL can cost you HDR "
                             "output."),
    "audio_night_mode": _("Evens out loud effects and quiet dialogue. This "
                          "turns passthrough off while it is enabled, because "
                          "the volume has to be adjusted before your receiver "
                          "gets the audio."),
}

_ACRONYMS = {"gui": "GUI", "ssl": "SSL", "tls": "TLS", "osc": "OSC",
             "mpv": "MPV", "hdr": "HDR", "av1": "AV1", "h265": "H265",
             "hevc": "HEVC", "kbps": "kbps", "url": "URL", "ipc": "IPC",
             "uuid": "UUID", "svp": "SVP", "id": "ID", "4k": "4K",
             "hi10p": "Hi10P", "ui": "UI"}


def label_for(key):
    if key in LABEL_OVERRIDES:
        return LABEL_OVERRIDES[key]
    return " ".join(_ACRONYMS.get(w, w.capitalize()) for w in key.split("_"))


def visible_passthrough_keys():
    """The passthrough toggles the current audio mode can actually use.

    S/PDIF has the bandwidth for AC3 and DTS core only, so offering TrueHD
    beside them would be offering a setting that silently does nothing. In
    "Default" and "Force Stereo" nothing is passed through at all.
    """
    # player_audio, not player: this is a constant, and reaching it through
    # player would drag libmpv into the browser's settings screen.
    from ..player_audio import AUDIO_PASSTHROUGH_CODECS

    usable = AUDIO_PASSTHROUGH_CODECS.get(settings.audio_mode or "auto", ())
    return [key for codec, key in AUDIO_PASSTHROUGH_KEYS if codec in usable]


def tray_available():
    """True if a system tray icon is actually up right now.

    Not "is pystray importable": the tray can fail to appear for reasons the
    parent process only learns from the child (missing typelib, no
    StatusNotifier host), and the form has to reflect what the user has, not
    what the install implies.
    """
    try:
        from .ui import user_interface

        tray = getattr(user_interface, "_tray", None)
        return tray is not None and tray.available
    except Exception:
        return False


def sections():
    """``[(title, [key, ...]), ...]`` — curated groups first, then Advanced
    with everything else that's editable."""
    schema = settings_schema()
    mode = settings.audio_mode or "auto"
    # Seeded, not built up: an audio toggle hidden because the mode can't use
    # it must not reappear under "Advanced" as an uncurated key.
    curated = ({k for _c, k in AUDIO_PASSTHROUGH_KEYS} | set(AUDIO_MODE_ONLY)
               | set(TRAY_DEPENDENT) | set(BACKGROUND_DEPENDENT)
               | {"audio_exclusive"})
    out = []
    try:
        shown = set(visible_passthrough_keys())
    except Exception:
        # Importing player pulls in mpv; never let that break the whole form.
        shown = set()
    shown |= {k for k, modes in AUDIO_MODE_ONLY.items() if mode in modes}
    if sys.platform in EXCLUSIVE_PLATFORMS:
        shown.add("audio_exclusive")
    keep_running = "close_to_tray" if tray_available() else "allow_background"
    shown.add(keep_running)
    if getattr(settings, keep_running, False):
        shown.update(BACKGROUND_DEPENDENT)
    hidden = curated - shown
    for title, keys in SECTIONS:
        present = [k for k in keys if k in schema and k not in hidden]
        curated.update(present)
        if present:
            out.append((title, present))
    advanced = sorted(k for k in schema if k not in curated)
    if advanced:
        out.append((_("Advanced"), advanced))
    return out


def _classify(ann):
    if ann is bool:
        return "bool"
    if ann is int:
        return "int"
    if ann is float:
        return "float"
    if ann is str:
        return "str"
    if typing.get_origin(ann) is typing.Union:
        non_none = [a for a in typing.get_args(ann) if a is not type(None)]
        if len(non_none) == 1:
            return _classify(non_none[0])
    return "skip"  # lists / structured configs — not editable in the flat form


def is_nullable(key):
    """True when the key's annotation is Optional[...].

    settings_schema() collapses Optional[float] to "float" (the form only
    needs the editor kind), but a nullable key can legitimately be set back
    to None -- ui_scale's "Follow display" is exactly that -- and coerce()
    would otherwise raise on it.
    """
    ann = Settings.__annotations__.get(key)
    return (typing.get_origin(ann) is typing.Union
            and type(None) in typing.get_args(ann))


def settings_schema():
    """``{key: "bool"|"int"|"float"|"str"}`` for the editable settings."""
    out = {}
    for key, ann in Settings.__annotations__.items():
        if key.startswith("_") or key in _HIDDEN:
            continue
        kind = _classify(ann)
        if kind != "skip":
            out[key] = kind
    return out


def get_settings():
    return settings.dict()


def coerce(kind, value):
    if kind == "bool":
        return bool(value)
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    return str(value)


def materialize_language_preset():
    """The language dropdown writes language_config rules (README-style): a
    preset generates rules, Unset clears them, Custom leaves them alone.

    Without it, choosing "Dubbed (shows only)" persists a
    string that nothing reads and track selection never changes."""
    from ..language_config import preset_rules, parse_language_config

    pref = settings.language_preference
    if pref == "custom":
        return
    if pref == "unset":
        settings.language_config = None
        return
    settings.language_config = parse_language_config(
        preset_rules(pref, settings.preferred_language))


def set_setting(key, value):
    """Coerce ``value`` to the key's declared type, apply, and persist.
    Returns True on success, False if the value was invalid for the type.

    ``sync_path`` is *not* handled here — moving the download store is a long
    filesystem operation, see relocate_downloads()."""
    schema = settings_schema()
    kind = schema.get(key)
    if kind is None:
        return False
    try:
        if value is None and is_nullable(key):
            setattr(settings, key, None)
        else:
            setattr(settings, key, coerce(kind, value))
    except (ValueError, TypeError):
        return False
    if key in ("language_preference", "preferred_language"):
        try:
            materialize_language_preset()
        except Exception:  # a bad preset must not block the save
            pass
    settings.save()
    return True


def relocate_downloads(new_path, progress=None):
    """Move the download store to ``new_path`` and persist the *resolved*
    path. Returns (ok, message). Blocking — call from a worker."""
    from ..sync.manager import syncManager

    ok, message = syncManager.relocate(new_path or None, progress=progress)
    if ok:
        settings.sync_path = syncManager.root if new_path else None
        settings.save()
    return ok, message
