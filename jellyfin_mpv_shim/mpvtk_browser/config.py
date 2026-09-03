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

# Curated groups, per settings tab. Anything not listed shows under
# "Advanced", which lives on the General tab.
#
# **Three tabs, not one.** These were all one "General" page, which had grown
# to eleven groups and about a hundred controls in a single scroll -- with a
# twenty-key "Interface" group at the top holding four unrelated topics
# (device identity, window behaviour, the playback HUD, and update
# notifications). Finding anything meant scrolling past everything.
#
# The split is by *what you are doing when you want it*, not by which module
# reads it: someone adjusting subtitles or the seek buttons is watching
# something, and someone adjusting scrolling or covers is browsing. General
# keeps what belongs to the installation rather than to either activity.
#
# TAB_SECTIONS is ordered, and so is each list; `sections(tab)` reads them
# straight through.
TAB_SECTIONS = {
    "general": [
        # enable_gui and headless are deliberately *not* here. Both are
        # one-way doors from the settings form's point of view: enable_gui
        # doesn't disable "the Jellyfin UI", it drops the whole app to CLI
        # mode -- no window, no tray, no settings -- and headless makes the
        # cast screen the only page, so with no system tray installed there
        # is nothing left to reach Settings from. Either way the way back is
        # hand-editing conf.json, which is not a thing to leave one click
        # away in the main list. They stay editable under Advanced, with
        # notes saying what they cost.
        (_("This Device"), ["player_name", "raise_mpv",
                            "discord_presence",
                            "check_updates", "notify_updates"]),
        # A controller drives the *library* as much as playback -- it is the
        # couch input for the whole app, not a player control -- so it sits
        # here rather than under Player Controls on the playback tab.
        #
        # `media_keys` deliberately stays uncurated: it is not what most
        # people are looking for, and on Linux desktops that route media
        # keys through MPRIS it is likely broken anyway. Promoting it would
        # be offering a switch we cannot say works.
        (_("Input"), ["input_gamepad", "gamepad_swap_confirm"]),
        # Everything about the window itself, in the order you meet it:
        # how it opens, whether it remembers, what closing it means.
        (_("Window"), ["fullscreen", "browser_fullscreen",
                       "remember_window_size", "window_controls",
                       "close_to_tray", "allow_background",
                       "start_minimized", "display_mirror_summon"]),
    ],
    "browse": [
        # The three startup-applied "look" settings, together: the theme
        # sets the palette and its own cover size, and these two can
        # override the sizing.
        (_("Theme"), ["theme", "poster_scale", "ui_scale",
                       "ui_text_scale", "ui_text_min"]),
        (_("Library Browser"), ["library_image_cache_mb",
                                "scroll_wheel_pixels",
                                "scroll_mode", "paginated",
                                "grid_fill",
                                "backdrop_full_width",
                                "detail_poster",
                                "detail_episode_image",
                                "logo_legibility_live_tv",
                                "logo_legibility_library",
                                # Last, and not between the two detail-page
                                # image switches or the two logo ones: both
                                # of those pairs read as a pair, and a
                                # setting about clocks wedged into either
                                # makes them look unrelated.
                                "clock_12h"]),
        # The epub reader. On this tab rather than Playback because a book
        # is not played -- and next to the browser's look because that is
        # what these are: how a page is set, in a window the same size.
        (_("Reading"), ["reader_font_size", "reader_theme",
                        "reader_justify", "comic_fit"]),
        # Downloading is acquiring library content for later, which is a
        # browsing activity rather than a watching one. NOT on the Downloads
        # tab, the obvious-looking home: that tab is the *manager* -- what is
        # on disk right now, per item, with delete and move buttons -- and it
        # is already crowded with media management. A settings form bolted
        # above it would be the smaller thing on a page about something else.
        (_("Downloads"), ["sync_path", "prefer_downloaded",
                          "auto_download_enable", "auto_download_next_up",
                          "auto_download_next_up_limit",
                          "auto_download_lookahead", "auto_download_max_gb",
                          "auto_download_delete_watched",
                          "auto_download_keep_days",
                          "auto_download_interval_mins"]),
        # Behind the disclosure, directly under the settings they qualify
        # (#661). Blank means "use the simple lookahead above", which is not
        # guessable from a label, so NOTES carries it.
        (_("Download Tuning"), ["auto_download_lookahead_min",
                                "auto_download_lookahead_max",
                                "auto_download_max_per_pass"]),
    ],
    "playback": [
        # The in-player UI leads: it is the thing you are looking at while
        # watching, and osc_style decides whether the rest of the group
        # applies at all.
        (_("Player Controls"), ["osc_style", "hud_grab_keys", "hud_wake_key",
                                "hud_scrim", "hud_autohide", "hud_hide_secs",
                                "mouse_chapter_nav", "mouse_click_pauses",
                                "trickplay_fast_mode"]),
        (_("Playback"), ["auto_play", "hwdec", "network_buffer",
                         "always_transcode",
                         "local_kbps", "remote_kbps", "direct_paths",
                         "remote_direct_paths", "playback_timeout"]),
        # Passthrough keys are listed in full here; sections() drops the
        # ones the selected mode cannot carry.
        (_("Audio"), ["audio_device", "audio_exclusive",
                      "audio_mode", "audio_night_mode"]
                     + [k for _c, k in AUDIO_PASSTHROUGH_KEYS]
                     + ["audio_optical_encode_ac3"]),
        (_("Subtitles & Languages"), ["subtitle_size", "subtitle_color",
                                      "subtitle_position",
                                      "language_preference",
                                      "preferred_language",
                                      "remember_audio_track",
                                      "remember_subtitle_track",
                                      "lang_filter", "lang_filter_sub",
                                      "lang_filter_audio"]),
        (_("Transcoding"), ["allow_transcode_to_h265",
                            "prefer_transcode_to_h265",
                            "transcode_hevc", "transcode_av1",
                            "transcode_4k", "transcode_hdr",
                            "transcode_hi10p", "transcode_dolby_vision",
                            "force_video_codec", "force_audio_codec"]),
        # Debanding and rendering quality lead, ahead of the shader pack:
        # they are the two answers that cost nothing to try and do not
        # spend the single shader-profile slot. Somebody arriving here
        # because anime looks blocky wants the first row, not a decision
        # about upscalers.
        (_("Video Enhancement"), ["deband", "render_quality",
                                  "tone_mapping",
                                  "shader_pack_enable",
                                  "shader_pack_subtype",
                                  "shader_pack_remember",
                                  "shader_pack_gpu_api",
                                  "deinterlace_auto",
                                  "motion_interpolation"]),
        (_("Skip Intro / Credits"), ["segment_intro", "segment_outro",
                                     "segment_commercial",
                                     "segment_preview", "segment_recap",
                                     "skip_intro_on_seek"]),
    ],
}

#: Settings that do nothing until the app is started again.
#:
#: **"Requires restart" means literally nothing happened**, and the settings
#: form marks exactly these rows and no others. Everything else is assumed
#: to apply now or to the next thing you play, which is why no setting says
#: so in its own note any more -- that sentence used to be repeated across
#: nine of them, in three different phrasings, and was the only thing a
#: reader had to reconcile them by.
#:
#: **Deliberately conservative, and under-listing is the safe direction.**
#: A key missing from here costs a banner nobody sees; a key wrongly IN here
#: asks somebody to restart for a change that had already taken effect,
#: which teaches them to ignore the banner. So every entry below was read
#: from its call site rather than assumed, and the ones that looked like
#: candidates and are not (`window_controls`, `shader_pack_subtype`,
#: `log_decisions`, `mpv_idle_quit`, `playback_timeout`, `paginated`) are
#: absent because they are re-read as they are used.
#:
#: This is **not** the same question as "does it apply right now". A third
#: group -- `hwdec`, `deband`, `deinterlace_auto` and the rest of
#: `mpv_options.PRESET_SETTINGS` -- applies to the next thing you play,
#: which is neither live nor a restart, and a banner for those would be
#: asking for a restart that is not needed. docs/settings-curation.md
#: section 3 is the full table.
#:
#: `start_minimized` and the tray pair are absent for a related reason:
#: nothing about them is *pending*. They are settings whose whole subject is
#: the next launch, so they have already taken full effect.
RESTART_REQUIRED = frozenset({
    # The whole interface geometry is derived once, at startup.
    "ui_scale",
    # `theme` is deliberately NOT here, even though half of it waits for a
    # restart. Colours repaint the moment you pick one, so marking the row
    # "Requires restart" would say nothing happened when something visibly
    # did -- and that is the one claim this marker cannot afford to get
    # wrong. It is the only setting that splits, and its own note carries
    # the exception.
    #
    # Decides which OSC mpv is CONSTRUCTED with.
    "osc_style",
    # mpv reads it exactly once, in mp_input_load_config -- a runtime write
    # succeeds and reads back yes while the SDL thread is never started.
    "input_gamepad",
    # One-way doors: both change what the app is at startup rather than what
    # it is doing.
    "enable_gui", "headless",
    # player.py picks its backend at import time.
    "mpv_ext", "mpv_ext_path", "mpv_ext_ipc", "mpv_ext_start",
    "mpv_ext_no_ovr", "mpv_ext_start_retries", "mpv_ext_start_retry_delay_ms",
    # Read when the profile manager is built and when the pack is loaded.
    "shader_pack_enable", "shader_pack_custom",
    # `if settings.discord_presence:` at module scope in player.py -- an
    # import guard, so nothing short of a restart re-runs it.
    "discord_presence",
    # Handed to each JellyfinClient at construction; until then the servers
    # go on showing the old name.
    "player_name",
    # Read once at startup to configure logging.
    "mpv_log_level",
    # mpv construction options and key bindings made with it. A re-created
    # mpv (idle-quit, crash recovery) would pick these up too, but not
    # predictably, so a restart is the answer that is always true.
    #
    # `media_key_seek` is NOT here despite sitting beside `media_keys`: the
    # binding is unconditional and the setting is read inside the handler
    # (player.py `_on_media_prev`), so it applies at the next key press. It
    # was listed once, which is precisely the wrong-badge case this set's
    # docstring warns trains people to ignore the banner.
    "menu_mouse", "media_keys", "mouse_chapter_nav",
    # Snapshotted at menu.py module scope (`lang_filter = set(...)` at
    # import), and every consumer reads that copy rather than the setting.
    # Worse than a plain missing badge: the two booleans beside it ARE read
    # live, so the filter visibly starts working with the old language list
    # and reads as "ignored" rather than "pending".
    "lang_filter",
    # Baked into the ThumbnailStore's MemoryCache at construction
    # (mpvtk_browser/ui.py), once, when the browser starts. A setting whose
    # whole purpose is "this machine is short on RAM" that silently does
    # nothing until relaunch.
    "library_image_cache_mb",
    # Passed to both the API client and mpv at construction.
    "tls_client_cert", "tls_client_key", "tls_server_ca",
})

#: Which tab "Advanced" (everything uncurated) is appended to. General, so
#: the other two stay the size the split made them -- and so there is one
#: place to look for a key you cannot find.
ADVANCED_TAB = "general"

#: Group titles that sit behind the "Show advanced settings" disclosure,
#: on whichever tab they appear.
#:
#: It used to be the literal title "Advanced", which made the disclosure a
#: property of a group's *name* -- so a tab could have at most one, it had
#: to be called that, and a handful of tuning keys could not be tucked away
#: beside the settings they qualify without being moved to another tab
#: entirely. #661's three fields are exactly that case.
ADVANCED_GROUPS = frozenset({
    _("Advanced"),
    _("Download Tuning"),
})

#: The tabs the schema-driven config form is drawn on, in order. The other
#: four Settings tabs are not this form: three are their own screens and
#: the home-screen one lives on the server. `sections()` and `search()`
#: both walk these.
FORM_TABS = ("general", "browse", "playback")

#: Flattened, in tab order. Anything that wants "every curated key" reads
#: this rather than knowing about the tabs.
SECTIONS = [group for tab in FORM_TABS for group in TAB_SECTIONS[tab]]

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
    "hwdec": [
        (_("Off (software decoding)"), "no"),
        (_("Only above 1080p"), "over-1080p"),
        (_("On"), "auto"),
        (_("Copy (advanced)"), "auto-copy"),
    ],
    # Not quality tiers -- three different trades. See
    # mpv_options.INTERPOLATION_PRESETS for what each writes and why the
    # last one is labelled as the expensive one rather than the best one.
    "motion_interpolation": [
        (_("Off"), "off"),
        (_("Smooth Motion"), "smooth"),
        (_("Blend Frames"), "blend"),
        (_("Smooth (high quality)"), "hq"),
    ],
    # Named for the content, not the strength: the numbers behind these
    # (mpv_options.DEBAND_PRESETS) mean nothing to anyone who has not read
    # mpv's manual, whereas "my anime looks blocky" is exactly why somebody
    # is on this row. "Off" is shared with motion_interpolation's, which is
    # the same sense and so the same catalogue entry on purpose.
    #
    # None of these may be spelled "Light" or "Standard" alone: gettext keys
    # on the English, `_("Light")` is already the reader's light THEME, and
    # collapsing the two would make them one entry no language could tell
    # apart. See docs/i18n.md section 6.
    "deband": [
        (_("Off"), "off"),
        (_("Light (live action)"), "light"),
        (_("Standard (animation)"), "standard"),
        (_("Strong"), "strong"),
    ],
    # MPV's own vocabulary rather than invented names, because these are
    # different curves and not a strength ladder -- there is no honest way
    # to order them. "Automatic" alone is already the SVP profile and the
    # automatic download group, so this one says what it is automatic about.
    "tone_mapping": [
        (_("Automatic (MPV decides)"), "auto"),
        (_("BT.2390 (reference)"), "bt.2390"),
        (_("BT.2446a"), "bt.2446a"),
        (_("Spline"), "spline"),
        (_("Hable"), "hable"),
        (_("Reinhard"), "reinhard"),
        (_("Clip (no tone mapping)"), "clip"),
    ],
    # "MPV default" rather than "Default", which is already a stream flag
    # and a scrim style.
    "render_quality": [
        (_("MPV default"), "default"),
        (_("High quality"), "high"),
    ],
    # Not "Default"/"Large" either -- "Large" is already a cover size.
    "network_buffer": [
        (_("MPV default (1 second)"), "default"),
        (_("Large (20 seconds)"), "large"),
        (_("Very large (60 seconds)"), "huge"),
    ],
    "osc_style": [
        (_("Jellyfin UI"), "mpvtk"),
        (_("MPV UI with thumbnails"), "mpv"),
        (_("MPV built-in default"), "default"),
        (_("Custom OSC"), "custom"),
        (_("No player controls"), "none"),
    ],
    # Down as well as up. Smaller is useful on a small screen, and it is
    # also how the scale gets tested in the direction where nothing
    # overflows -- text that shrinks cannot break a layout, so it isolates
    # "does the scale reach this widget" from "does this widget have room".
    #
    # Stops at 150%, and the reason is CONTENT rather than overflow.
    # [iw]: "most of the tiles are already using an ellipsis at that
    # point", and "after 150% most of the UI scaling breaks down and needs
    # to get bigger, but text scaling is for text only by definition".
    # So the honest ceiling is where captions stop saying anything useful;
    # past it the right control is `ui_scale`, which moves the artwork and
    # the spacing with the words.
    #
    # An earlier version of this comment claimed 1.75 overflowed Live TV's
    # tab strip. That measurement was taken while the multiplier was being
    # applied twice by every composite widget -- "150%" was really 225% on
    # buttons -- so it described a bug, not the layout. With that fixed
    # nothing overflows until 3.0. The cap stays for the reason above.
    "ui_text_scale": [
        (_("75%"), 0.75),
        (_("85%"), 0.85),
        (_("90%"), 0.9),
        (_("100% (no scaling)"), 1.0),
        (_("110%"), 1.1),
        (_("125%"), 1.25),
        (_("150%"), 1.5),
    ],
    "ui_text_min": [
        (_("No minimum"), 0),
        (_("14 px"), 14),
        (_("16 px"), 16),
        (_("18 px"), 18),
        (_("20 px"), 20),
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
    "scroll_mode": [
        (_("Continuous"), "continuous"),
        (_("Aligned to rows"), "aligned"),
        (_("One row per notch"), "row"),
    ],
    # Phrased as what the user sees, not as "client-side decorations":
    # "auto" is not a guess about the desktop, it is MPV reporting whether
    # anything decorated this window. See conf.window_controls.
    "window_controls": [
        (_("Only when the window has no title bar"), "auto"),
        (_("Always"), "always"),
        (_("Never"), "never"),
    ],
    # One list, five settings: the three things that can be done about a
    # media segment (jellyfin-web offers the same three).
    "segment_intro": _SEGMENT_ACTIONS,
    "segment_outro": _SEGMENT_ACTIONS,
    "segment_commercial": _SEGMENT_ACTIONS,
    "segment_preview": _SEGMENT_ACTIONS,
    "segment_recap": _SEGMENT_ACTIONS,
    "reader_theme": [
        (_("Dark"), "dark"),
        (_("Sepia"), "sepia"),
        (_("Light"), "light"),
    ],
    # The same two the comic reader's own bar offers, and the same value:
    # picking one there writes this.
    "comic_fit": [
        (_("Fit Width"), "width"),
        (_("Fit Page"), "page"),
    ],
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
    "grid_fill": [
        (_("Widen the gaps"), "justify"),
        (_("Centre the tiles"), "center"),
        (_("Leave it on the right"), "off"),
    ],
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
    "input_gamepad": _("Game Controller"),
    "gamepad_swap_confirm": _("Swap Confirm and Back Buttons"),
    "sync_path": _("Download Folder"),
    "prefer_downloaded": _("Prefer Downloaded Copy"),
    "mouse_click_pauses": _("Left Click Pauses Playback"),
    "detail_poster": _("Show Posters on Detail Pages"),
    "detail_episode_image": _("Show Episode Thumbnails on Detail Pages"),
    "hwdec": _("Hardware Decoding"),
    "deinterlace_auto": _("Deinterlace Automatically"),
    "motion_interpolation": _("Motion Interpolation"),
    "deband": _("Debanding"),
    "tone_mapping": _("HDR Tone Mapping"),
    "render_quality": _("Rendering Quality"),
    "network_buffer": _("Network Buffer"),
    "auto_download_enable": _("Automatically Download Upcoming Episodes"),
    "auto_download_next_up": _("Include Next Up"),
    "auto_download_next_up_limit": _("Next Up Entries to Consider"),
    "auto_download_lookahead": _("Episodes to Keep Ahead (0 = off)"),
    "auto_download_max_gb": _("Storage Limit for Automatic Downloads (GB)"),
    "auto_download_delete_watched": _("Delete Automatic Downloads Once Watched"),
    "auto_download_keep_days": _("Delete Unwatched After (days, 0 = never)"),
    "auto_download_interval_mins": _("Check Every (minutes)"),
    "auto_download_lookahead_min": _("Top Up When Fewer Than (episodes)"),
    "auto_download_lookahead_max": _("Top Up To (episodes)"),
    "auto_download_max_per_pass": _("Maximum Downloads per Check"),
    "close_to_tray": _("Close to Tray (keep running)"),
    "allow_background": _("Keep Running in Background"),
    "remember_window_size": _("Remember Window Size"),
    "window_controls": _("Window Buttons in the Top Bar"),
    "osc_style": _("Player Controls Style"),
    "trickplay_fast_mode": _("Load All Seek Previews at Once"),
    "discord_presence": _("Show What You're Watching in Discord"),
    "ui_scale": _("Interface Scale"),
    "ui_text_scale": _("Text size"),
    "ui_text_min": _("Minimum Text Size"),
    "theme": _("Theme"),
    "poster_scale": _("Cover Size"),
    "backdrop_full_width": _("Full-Width Backdrops"),
    "clock_12h": _("12-Hour Clock"),
    "grid_fill": _("Grid Spacing"),
    "logo_legibility_live_tv": _("Make Live TV logos more legible"),
    "logo_legibility_library": _("Make library logos more legible"),
    # Named for what they do to the page, not for the module they belong
    # to: under a "Reading" heading, "Reader Font Size" says "reader"
    # twice and "Font" about something that is not a font.
    "reader_font_size": _("Type Size"),
    "reader_theme": _("Page Colour"),
    "reader_justify": _("Justify Text"),
    "comic_fit": _("Comic Reading Mode"),
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
    # Both caveats are ones a user would otherwise report as bugs: input
    # arriving while another window is focused, and the setting appearing to
    # do nothing at all on an mpv that cannot do it.
    "input_gamepad": _("Move with the d-pad "
                       "or left stick, seek with the right stick, shoulder "
                       "buttons to page, Start for the menu. A controller "
                       "is not focus-aware, so it will reach this app even "
                       "when another window is in front. Needs an mpv built "
                       "with SDL2 gamepad support; if yours lacks it this "
                       "setting is ignored and the log says so. Any button "
                       "can be reassigned in your input.conf."),
    # The buttons are reported by POSITION, so this is not something the
    # shim can detect -- see gamepad.py. Worth spelling the layouts out:
    # somebody with a Switch-style pad is looking for "my A button goes
    # back", not for the word "swap".
    "gamepad_swap_confirm": _("Turn this on for a controller whose A button "
                              "is on the right rather than at the bottom "
                              "(Switch Pro, most 8BitDo pads)."),
    # A blank numeric field meaning "use the setting above" is not
    # guessable from a label, and these three are the ones where leaving
    # them alone is the right answer for almost everybody.
    "auto_download_lookahead_min": _("Leave these blank unless downloads are "
                                     "waking your disks too often. Set both "
                                     "of the first two together: episodes "
                                     "are then fetched in one batch when a "
                                     "series runs low, instead of one at a "
                                     "time."),
    # Both reasons someone turns this off, because they are unrelated and
    # only one of them is about taste.
    # Only the second one carries the spoiler argument, which is why they
    # are two settings and not one.
    "detail_episode_image": _("An episode's thumbnail is a frame of an "
                              "episode you may not have watched yet."),
    # What turning it OFF buys, since that is the non-obvious half: the
    # left button is what the VO drags the window with, so pausing with it
    # and dragging with it are mutually exclusive.
    "mouse_click_pauses": _("Off gives MPV's own mouse behaviour instead: "
                            "drag the video to move the window, and right "
                            "click to pause. Double click is full screen "
                            "either way."),
    # The reason this is off by default, in the place someone deciding
    # whether to change it is looking. mpv's own manual says to
    # "acknowledge that this may cause problems"; the tail it breaks for
    # is disproportionately the hardware that needed it.
    "deinterlace_auto": _(
        "Deinterlace video the file says is interlaced. Off by default, as in "
        "MPV itself: the flag is not reliable in either direction, and "
        "deinterlacing progressive video softens a picture that was fine. To "
        "force it on for something that is interlaced without saying so, use "
        "Deinterlace in the player's settings menu, which lasts until you "
        "return to the library. Needs MPV 0.38 or newer."),
    # Three things a user would otherwise report as a bug: that it is off
    # by default, that it is not free on the hardware this app often runs
    # on, and that leaving it off is how you keep your own mpv.conf values.
    "deband": _(
        "Smooths the blocky steps that appear in gradients — skies, dark "
        "scenes, fades. Animation and anime benefit most, because flat "
        "gradients are most of the picture; live action is largely "
        "unaffected either way, "
        "though a strong setting can soften genuinely fine detail. Costs GPU "
        "work whatever the content, which is worth knowing on a small or "
        "older machine. Leave this Off if you set the deband options in "
        "mpv.conf yourself — Off writes nothing at all, so your values are "
        "left alone."),
    "tone_mapping": _(
        "How HDR video is fitted to an SDR display. This does nothing when "
        "HDR is being passed through to an HDR display, because no tone "
        "mapping is happening to change. Leave it on Automatic unless HDR "
        "films look wrong to you; BT.2390 is the reference curve, and Clip "
        "is what to try if highlights look grey rather than bright."),
    "render_quality": _(
        "High quality applies the same options as MPV's own high-quality "
        "preset: better upscaling and HDR handling, at some GPU cost. It "
        "needs no shader files and does not use up your shader profile, so "
        "it is the thing to try before the shader pack below."),
    "network_buffer": _(
        "How far ahead of playback to read. MPV's default is one second, "
        "which is short for a server reached over the internet or a slow "
        "connection — raise this if playback keeps stopping for buffering. "
        "Larger buffers use more memory and make the first few seconds of a "
        "file slower to start."),
    "motion_interpolation": _(
        "Frame blending (blends frames together, not the same as "
        "SVP/DLSS/framegen). Reduces juddering caused by mismatched frame "
        "rate between content and display. May cause dropped frames if your "
        "displays have mismatched frame rates."),
    "hwdec": _("Off by default because some graphics drivers handle it "
               "badly. \"Only above 1080p\" is the cautious way to turn "
               "it on: most hardware decodes 1080p in software without "
               "help. \"Copy (advanced)\" is slower, but it is the mode "
               "that works with video filters, so it is the one to pick if "
               "you run SVP or another VapourSynth filter. If video stops "
               "working, start with --disable-hwdec and change this back."),
    "close_to_tray": CAST_TARGET_NOTE,
    "allow_background": CAST_TARGET_NOTE,
    # Advanced-only (see SECTIONS), and the note is why: it reads like "turn
    # off the Jellyfin UI", and what it actually does is leave a Windows user
    # with a process they can neither see nor quit.
    "enable_gui": _(
        "Off means command-line mode: no window, no system tray and no "
        "settings screen, so the only way back is to edit conf.json by hand. "
        "This is not how you get MPV's own on-screen controls; use Player "
        "Controls Style under Interface for that."),
    # Advanced-only for the same reason as enable_gui: with no tray icon
    # installed, the cast screen is the only page and Settings is gone.
    "headless": _(
        "Show only what is cast to this machine. The library, including this "
        "settings screen, cannot be reached from here, and without a system "
        "tray icon the only way back is to edit conf.json. This is for a "
        "shared TV nobody should be able to browse from; for an ordinary cast "
        "target, use the Interface settings."),
    # Says what it is for, not what it is called. "Client-side decorations"
    # is the right term and means nothing to the person whose window has no
    # close button; the note has to be recognisable from the symptom.
    "window_controls": _(
        "Some desktops, GNOME on Wayland in particular, draw no title bar on "
        "the player window, leaving no way to move or close it. This puts "
        "minimize, maximize and close in the top bar instead, and lets you "
        "drag the window by it. On \"Only when the window has no title bar\", "
        "MPV is asked whether this window got one, so desktops that draw a "
        "title bar are left alone."),
    "trickplay_fast_mode": _("Seek previews are normally fetched a few "
                             "minutes at a time around where you are "
                             "seeking, so scrubbing somewhere new waits "
                             "briefly. Turn this on to load them all up "
                             "front instead: nothing ever waits, but a long "
                             "film can cost several hundred MB of memory. "
                             "Turning it on applies to what you are watching "
                             "the next time you seek somewhere new; turning "
                             "it off applies to the next video."),
    "osc_style": _("MPV keybinds are used by "
                   "default. Press ENTER to drive the player controls by "
                   "keyboard. \"No player controls\" leaves playback bare; "
                   "the library, the keyboard shortcuts and the menu key "
                   "still work. Choose \"Custom OSC\" when you have "
                   "installed your own OSC script: it turns MPV's own off "
                   "and gives the library a solid background, so the "
                   "script's idle screen cannot show through it."),
    "scroll_wheel_pixels": _(
        "Pixels one wheel notch scrolls. Raise it to scroll faster, lower it "
        "for finer control. On a grid the value is rounded so that a whole "
        "number of notches spans one row, in every scroll mode, so a trackpad "
        "or trackball never leaves you a sliver of a row off."),
    # Names no mechanism and no measurement: the reason to pick one of these
    # is what you can see happening, so the note is written as symptoms.
    # "Continuous" says what it does rather than promising smoothness --
    # "Smooth scrolling" would be read as *animated* scrolling, and someone
    # who turned it on and got no animation would report it broken.
    "scroll_mode": _(
        "\"Continuous\" scrolls by pixels and lands wherever the wheel puts "
        "it, lining rows up by itself if the display cannot keep up. Pick "
        "\"Aligned to rows\" if scrolling still stutters, as it can on a very "
        "large, slow or remote display, or with an external MPV. Pick \"One "
        "row per notch\" to move a whole row, or one home-screen section, at a"
        " time."),
    # Deliberately says nothing about the pypresence dependency: the Windows
    # build bundles it, so naming a package most users will never have to
    # think about only invites questions. The dynamic note in
    # settings/general.py raises it, and only for someone it is actually
    # broken for.
    "discord_presence": _("Discord Rich Presence."),
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
        "Channel logos are ink on a transparent background, drawn for the "
        "white page other clients put them on, so on a dark one the black ones"
        " vanish. On, each logo gets the light plate it was made for, and the "
        "few with a white outline get a drop shadow so they keep an edge "
        "against it. Off, they get the theme's card colour and no shadow, as "
        "Jellyfin Web does."),
    "logo_legibility_library": _(
        "The same treatment for a library set to draw Logo artwork. Off by "
        "default: a film or series logo is white by convention and already "
        "reads on a dark background, and it is the plate that makes it need a "
        "shadow. Turn it on if yours are dark."),
    "theme": _("Palette, glow, cover style and default cover size. Colours "
               "change immediately; cover and heading sizes take effect "
               "after a restart."),
    "grid_fill": _("Where the width a whole number of covers does not use "
                   "ends up. Widening the gaps keeps the page margins the "
                   "same on every screen size; centring keeps the covers "
                   "evenly spaced and moves both margins instead."),
    "backdrop_full_width": _("Run the backdrop on a detail page to the "
                             "edges of the window, as the web client does. "
                             "It shows more of the artwork without taking "
                             "any more vertical space."),
    "poster_scale": _("Overrides the theme's cover size. Also on the View "
                      "menu of any library."),
    # "am"/"pm" and "24 hour" are what somebody types; none of them is in
    # the label, and the note is the searchable half (docs/settings-
    # curation.md section 2.5).
    "clock_12h": _("Show times of day as \"8:30 PM\" rather than "
                   "\"20:30\" -- the Live TV guide and its air times, and "
                   "the \"Ends at\" labels on a detail page and the "
                   "player controls."),
    "hud_scrim": _("The controls have to stay legible over any frame. "
                   "\"None\" gives the text a drop shadow instead of "
                   "shading the picture behind it."),
    "hud_autohide": _("\"Hide unless hovered\" keeps them up only while "
                      "the pointer is on them, paused or not."),
    "hud_hide_secs": _("0 hides them as soon as the pointer is not on "
                       "them, and forces \"Hide unless hovered\"."),
    "mouse_chapter_nav": _(
        "During playback only; in the library those buttons stay Back and "
        "Forward. Off by default because they are easy to hit by accident on "
        "some mice."),
    # xgettext: no-python-format
    # "100% on" reads as the conversion "% o" (space flag, octal), so xgettext
    # marks this python-format and msgfmt --check then rejects any translation
    # that does not carry the fake directive through -- which zh_Hans already
    # tripped over. This string is a NOTES entry rendered as-is and is never
    # %-formatted, so the flag is wrong rather than unmet. The override changes
    # no msgid, so nothing already translated is discarded.
    "ui_scale": _("\"Follow display\" uses the scale your desktop reports, "
                  "which is 100% on X11."),
    "ui_text_scale": _(
        "Scales the text only. Interface Scale above resizes everything, "
        "artwork and spacing and controls included, so use this one when the "
        "words are too small rather than the whole interface. It stops at "
        "150%: past that most tile captions are ellipsized, and what needs to "
        "be bigger is the whole interface, which is Interface Scale."),
    "ui_text_min": _("Nothing renders smaller than this, whatever Text Size "
                     "works out to. Raises the smallest labels without "
                     "enlarging headings, which a percentage cannot do."),
    "audio_mode": _("\"Default\" changes nothing and lets MPV (and your own "
                    "mpv.conf) decide. Pick a mode only if you are sending "
                    "audio to a receiver."),
    "audio_optical_encode_ac3": _(
        "Audio your receiver cannot be sent directly is encoded to AC3, the "
        "only way surround fits down an optical cable. Turn this off if the "
        "encoder causes audio delay; those tracks become stereo instead."),
    "shader_pack_subtype": _("\"hq\" offers heavier profiles. Pick it if you "
                             "have a fast graphics card."),
    "shader_pack_gpu_api": _("Leave on Automatic unless video breaks when a "
                             "profile is loaded. This only applies while a "
                             "profile is active, and OpenGL can cost you HDR "
                             "output."),
    "reader_font_size": _("In pixels, before interface scaling. The A- and "
                          "A+ buttons in the reader change this too."),
    "comic_fit": _("How a comic page is fitted to the window when you open "
                   "it. The reader's own buttons change this too."),
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


def sections(tab=None):
    """``[(title, [key, ...]), ...]`` — curated groups, then Advanced with
    everything else that's editable.

    ``tab`` limits the result to one settings tab (see TAB_SECTIONS);
    ``None`` returns every group, which is what anything asking "is this key
    reachable at all" wants.

    **Advanced is computed against every curated key, not the tab's.**
    Otherwise each tab would list the other two tabs' settings as
    uncurated — every key would appear three times, and the split would have
    made the page longer rather than shorter.
    """
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
    wanted = (None if tab is None
              else {t for t, _k in TAB_SECTIONS.get(tab, [])})
    for title, keys in SECTIONS:
        present = [k for k in keys if k in schema and k not in hidden]
        # Every group contributes to `curated` — see the docstring — but
        # only this tab's groups are drawn.
        curated.update(present)
        if present and (wanted is None or title in wanted):
            out.append((title, present))
    if tab is not None and tab != ADVANCED_TAB:
        return out
    advanced = sorted(k for k in schema if k not in curated)
    if advanced:
        out.append((_("Advanced"), advanced))
    return out


#: Words a user types that appear nowhere in a setting's label, key or note.
#:
#: Search-only, and that is the point: these do not belong in the prose. A
#: note exists to explain a setting to somebody already looking at it, and
#: padding it with synonyms to feed the search would make it worse at its
#: real job. Everything here was a measured miss -- the query returned
#: nothing while the setting sat two tabs away.
#:
#: Matching is substring and directional (see `search`), so the LONGER form
#: is what belongs here: "certificate" finds a label saying "Cert", but
#: "Cert" alone is never found by "certificate".
SEARCH_ALIASES = {
    "local_kbps": "bitrate bandwidth quality",
    "remote_kbps": "bitrate bandwidth quality",
    "ignore_ssl_cert": "certificate https",
    "tls_client_cert": "certificate https",
    "tls_client_key": "certificate https",
    "tls_server_ca": "certificate https",
    "render_quality": "upscale upscaler sharpness",
    # Whichever of the pair this machine shows, "tray" has to find it --
    # and on a machine with NO tray it is `allow_background` that is
    # offered, whose label and note never say the word. That is the machine
    # where somebody types it.
    "allow_background": "tray systray notification area",
    "close_to_tray": "systray notification area",
    "start_minimized": "minimise",
    "ui_scale": "hidpi dpi",
    "audio_mode": "surround 5.1 7.1",
    "motion_interpolation": "stutter",
    # "am" and "24" are what somebody types and neither is in the label or
    # the note; "pm", "clock" and "time" would be redundant with them.
    "clock_12h": "am 24 format",
}


def search_haystack(key, title="", include_aliases=True,
                    include_note=True):
    """Everything a settings search should match ``key`` on.

    The **note** is in here deliberately, and it is what makes the feature
    worth having: a label is two or three words chosen before anyone knew
    what people would call the thing. "banding", "buffer", "stutter",
    "washed out", "controller" and "tray" are all in notes and none is in a
    label. The cost is that a common word can pull in a setting whose note
    merely mentions it -- which is the right way round for a search box,
    since the alternative is a query that finds nothing and a user who
    concludes the setting does not exist.

    The raw key is included because the docs, the issue tracker and
    `conf.json` all name settings that way, so somebody arriving from any
    of them types `auto_download_lookahead` rather than its label.

    ``include_aliases`` and ``include_note`` exist for the test that checks
    a note-dependent case really does depend on the note: it has to be able
    to ask what the haystack looks like with each part taken away. Without
    that, a case could start matching via an alias or an enum label and go
    on claiming to prove the notes are searched.
    """
    parts = [label_for(key), key, key.replace("_", " "), title]
    note = NOTES.get(key) if include_note else None
    if note:
        parts.append(note)
    if include_aliases:
        alias = SEARCH_ALIASES.get(key)
        if alias:
            parts.append(alias)
    for label, _value in LABELED_ENUMS.get(key) or ():
        parts.append(label)
    parts.extend(ENUMS.get(key) or ())
    return " ".join(str(p) for p in parts).lower()


def search(query, tabs=FORM_TABS):
    """``[(tab, title, [key, ...]), ...]`` for settings matching ``query``.

    Every whitespace-separated word must match somewhere in the setting's
    haystack (AND, not OR): with a corpus this small and notes this long,
    OR returns most of the form for any two common words, which is the same
    as returning nothing.

    Built on :func:`sections`, not on the schema, so a control the form is
    currently **hiding** cannot be found -- the passthrough toggles the
    selected audio mode cannot carry, `close_to_tray` on a machine with no
    tray. Finding a setting that the form then refuses to draw would be a
    search result that leads nowhere.

    Advanced groups are searched and returned like any other. The
    disclosure exists so the tab is not a hundred controls long; somebody
    who has typed a query has already narrowed it, and hiding half the
    answers behind a checkbox they cannot see from here would make the
    search quietly incomplete.
    """
    words = [w for w in (query or "").lower().split() if w]
    if not words:
        return []
    out = []
    for tab in tabs:
        for title, keys in sections(tab):
            hits = [k for k in keys
                    if all(w in search_haystack(k, title) for w in words)]
            if hits:
                out.append((tab, title, hits))
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
