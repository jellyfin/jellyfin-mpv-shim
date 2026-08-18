import logging
import os
import uuid
import socket
import json
import os.path
import sys
import getpass
import threading
from typing import List, Optional
from .settings_base import SettingsBase, adv_bool, object_types
from .language_config import LanguageRule, parse_language_config

# Register the structured-type parser. Done here (rather than in
# settings_base.py) to keep settings_base free of dependencies that would
# otherwise cycle through conf.py.
object_types[Optional[List[LanguageRule]]] = parse_language_config

log = logging.getLogger("conf")
config_path = None

# Bump when a default changes in a way existing installs must pick up, and add
# the corresponding step to Settings._migrate().
#   1: transcode_dolby_vision defaults off (mpv plays Dolby Vision natively).
#   2: the four skip_intro/skip_credits booleans become one action per
#      media segment type (segment_intro and friends).
#   3: a kb_* binding cleared to JSON null had coerced to the STRING
#      "None" and been saved back verbatim; those become real nulls.
#   4: the seek_* settings leave conf.json -- the keys the user CHANGED
#      become real bindings in their input.conf (see input_conf.py).
CONFIG_VERSION = 4

# Media segment types the server publishes, and the setting that decides what
# each one does. Jellyfin's enum also has "Unknown", which has no meaning to
# act on. The values are SEGMENT_ACTIONS: leave it alone, offer a Skip
# button, or skip it silently -- the three jellyfin-web offers.
SEGMENT_SETTINGS = {
    "Intro": "segment_intro",
    "Outro": "segment_outro",
    "Commercial": "segment_commercial",
    "Preview": "segment_preview",
    "Recap": "segment_recap",
}
SEGMENT_ACTIONS = ("off", "ask", "always")


def segment_action(segment_type):
    """What to do about a segment of this type: "off", "ask" or "always".

    Unknown types -- a newer server, a hand-edited value -- are "off". A
    segment the shim does not understand must not be skipped out from under
    the viewer, and a Skip button for it would have nothing to say.
    """
    key = SEGMENT_SETTINGS.get(segment_type)
    value = getattr(settings, key, None) if key else None
    return value if value in SEGMENT_ACTIONS else "off"


def any_segment_wanted():
    """Whether any segment type is set to anything but "off" -- i.e.
    whether it is worth asking the server for segments at all."""
    return any(segment_action(t) != "off" for t in SEGMENT_SETTINGS)


def wanted_segment_types():
    """The segment types to request, newest-first order irrelevant."""
    return [t for t in SEGMENT_SETTINGS if segment_action(t) != "off"]

# Values `scroll_mode` accepts, most permissive first. The first entry is the
# default AND the fallback: anything unrecognised (a hand-edited typo, a value
# from a newer build) has to land on continuous scrolling rather than on a
# mitigation nobody asked for -- which is what every other enum setting here
# does, and what `snapped_scrolling` used to get for free by going through
# adv_bool. mpvtk_browser/config.py holds the same values with their labels;
# tests/test_mpvtk_adopt.py pins the two lists together.
SCROLL_MODES = ("continuous", "aligned", "row")

# Serializes writers of the config file. save() is reachable from the UI
# action loop and from background workers (e.g. the download-folder move);
# unsynchronized truncate+rewrite writers can interleave into invalid JSON,
# which load() swallows — silently resetting every setting on next launch.
_save_lock = threading.Lock()


def get_default_sdir():
    if sys.platform.startswith("win32"):
        if os.environ.get("USERPROFILE"):
            return os.path.join(os.environ["USERPROFILE"], "Desktop")
        else:
            username = getpass.getuser()
            return os.path.join(r"C:\Users", username, "Desktop")
    else:
        return None


class Settings(SettingsBase):
    # Schema revision of the config on disk, used to apply one-time migrations
    # when a default changes in a way that should reach existing installs.
    # A config written before this key existed loads as 0. See _migrate().
    config_version: int = 0
    player_name: str = socket.gethostname()
    client_uuid: str = str(uuid.uuid4())
    media_ended_cmd: Optional[str] = None
    pre_media_cmd: Optional[str] = None
    stop_cmd: Optional[str] = None
    auto_play: bool = True
    # Persisted playback volume (0-100), kept separately for music and video —
    # the loudness gap between the two means users want different levels.
    # Applied when a track starts and reconciled back on the timeline tick.
    music_volume: int = 100
    video_volume: int = 100
    idle_cmd: Optional[str] = None
    idle_ended_cmd: Optional[str] = None
    play_cmd: Optional[str] = None
    idle_cmd_delay: int = 60
    # Quit mpv after this many idle seconds to free the window / GPU / memory;
    # it is re-created on the next play (or when the library is reopened from
    # the tray). On by default: with the browser and the player sharing one
    # process, this is what makes a minimized app cost almost nothing.
    mpv_idle_quit: bool = True
    mpv_idle_quit_secs: int = 300
    direct_paths: bool = False
    remote_direct_paths: bool = False
    path_substitutions: list = []
    #: Hardware video decoding. "no" (mpv's own default and ours),
    #: "over-1080p" (software below, hardware above), "auto" (mpv's
    #: whitelisted direct modes), or "auto-copy" (the same, copied back to
    #: system RAM).
    #:
    #: **Off by default, and that follows mpv rather than the other
    #: clients.** mpv's manual on turning it on: "acknowledge that this may
    #: cause problems", and its maintainers decline to default it on
    #: (mpv#12948) because particular vendor/GPU combinations are badly
    #: broken -- AMD vaapi on Linux causing GPU resets, vp9 on Intel Macs
    #: hanging mpv before the window even opens. Jellyfin Media Player did
    #: default it on and it worked for most people; this is about the tail
    #: it did not work for, who are disproportionately the people on
    #: hardware that needed it.
    #:
    #: "over-1080p" is the option that exists because *we* can do what mpv
    #: cannot: the source resolution is in the DTO before playback starts,
    #: so decoding can be software where software is fine and hardware only
    #: where it is not. Most hardware of the last decade decodes 1080p
    #: without help, and often looks better doing it.
    #: Draw the item's own poster inset into a detail page's header (#7).
    #: A film's or series' key art, beside the backdrop it is normally
    #: shown against everywhere else.
    detail_poster: bool = True
    #: The same slot on an *episode*, where the artwork is a still.
    #:
    #: Split from the poster deliberately, because the two objections are
    #: unrelated and only one is about taste [iw]: a still is a frame of an
    #: episode you have **not watched**, on the page you opened to decide
    #: whether to watch it. Somebody avoiding spoilers wants this off and
    #: the poster left alone.
    detail_episode_image: bool = True
    hwdec: str = "no"
    #: mpv's ``--deinterlace=auto``: deinterlace only what the file says is
    #: interlaced, rather than everything or nothing.
    #:
    #: **Off by default, which is also mpv's default**, and the reason is
    #: that the flag is not reliable. Plenty of interlaced DVD and broadcast
    #: rips are not marked, and plenty of progressive files carry the flag
    #: from whatever produced them -- so auto is right much more often than
    #: it is wrong, but the case where it is wrong (deinterlacing
    #: progressive video) softens a picture that was fine. The per-session
    #: toggle in the playback HUD's gear menu is the answer for the other
    #: half, the file that IS interlaced and does not say so -- it lasts
    #: until the library comes back or the window is closed.
    #:
    #: ``auto`` is mpv 0.38+. An older build rejects the value, and the
    #: setting then behaves as off -- see PlayerManager._apply_deinterlace,
    #: which will not substitute ``yes``: forcing deinterlacing on
    #: everything is not a degraded version of this, it is a different and
    #: worse setting.
    deinterlace_auto: bool = False
    #: mpv frame interpolation -- ``video-sync=display-resample`` plus
    #: ``interpolation`` and a ``tscale`` filter. See
    #: mpv_options.INTERPOLATION_PRESETS for what each value writes.
    #:
    #: **This is frame BLENDING, not motion compensation.** It resamples
    #: along the time axis so a 24fps film on a 60Hz screen stops
    #: repeating frames unevenly; it does not synthesise motion the way
    #: SVP does. That was looked at and rejected [iw]: it is paid on Linux
    #: now, and its dependencies are heavy enough that shipping them is
    #: not on the table.
    #:
    #: Off by default, and the reason is not that it is expensive. Measured
    #: on a 9950X3D + RTX 4090 [iw], it dropped frames badly -- on a
    #: MULTI-MONITOR X11 desktop whose screens run at different refresh
    #: rates (144Hz beside two at 60). That is not a hardware limit, it is
    #: mpv reading the wrong clock: "on multi-monitor systems, there is a
    #: chance that the detected value is from the wrong monitor", and
    #: "setting an incorrect value (even if slightly incorrect) can ruin
    #: video playback" (mpv's manual, --display-fps-override). All three
    #: modes follow that clock, so all three inherit the problem, and it is
    #: a common enough desktop for the default to have to assume it.
    #:
    #: The first thing to try is mpv's own
    #: ``display-fps-override=<the refresh of the screen you watch on>`` in
    #: the user's mpv.conf -- but it did NOT fix the machine above [iw], so
    #: the mismatched-desktop case is not fully understood and this is not
    #: a workaround to promise anyone. Deliberately not a setting of ours
    #: either way: we would be guessing at which monitor, which is the
    #: thing that is already wrong.
    #:
    #: It also costs GPU on every frame, mpv reverts to audio timing on its
    #: own for low-framerate or VFR content, and ``display-resample``
    #: adjusts audio speed to track the display -- which is exactly what
    #: somebody bitstreaming to a receiver does not want.
    motion_interpolation: str = "off"
    always_transcode: bool = False
    transcode_hi10p: bool = False
    transcode_hdr: bool = False
    transcode_hevc: bool = False
    transcode_av1: bool = False
    transcode_4k: bool = False
    # Off by default: mpv handles Dolby Vision natively now, so force-
    # transcoding it to SDR costs server CPU and loses the HDR presentation
    # for no benefit. CONFIG_VERSION 1 migrates existing configs off it.
    transcode_dolby_vision: bool = False
    allow_transcode_to_h265: bool = False
    prefer_transcode_to_h265: bool = False
    remote_kbps: int = 10000
    local_kbps: int = 2147483
    subtitle_size: int = 100
    subtitle_color: str = "#FFFFFFFF"
    subtitle_position: str = "bottom"
    # Off by default since the browser and the player share one window: a
    # cast target that only ever showed video could reasonably grab the
    # screen, but an app you are actively browsing should not go fullscreen
    # out from under you when you press play.
    fullscreen: bool = False
    enable_gui: bool = True
    # Run the in-window library browser fullscreen. Off by default: browsing is
    # a desktop activity, and `fullscreen` (which still applies to playback)
    # would otherwise make the browser take over the screen at startup.
    browser_fullscreen: bool = False
    start_minimized: bool = False
    # Window size for the player/browser window. mpv's own default is a fixed
    # 960x540 regardless of how big the display is, which is small for a
    # browsable UI. These are rewritten on exit when remember_window_size is
    # on, so they double as "the size you left it at".
    window_width: int = 1280
    window_height: int = 720
    window_maximized: bool = False
    # Client-side decorations: draw a drag handle and minimize/maximize/close
    # buttons into the browser's own top bar, for windows the desktop gives no
    # title bar of its own.
    #
    # "auto" asks mpv rather than sniffing the environment, because mpv's
    # `border` property already IS the answer: on a Wayland compositor with no
    # zxdg_decoration_manager_v1 (GNOME/mutter, which supports client-side
    # decorations only) mpv writes border=false back into the option itself,
    # and where the protocol does exist it writes whichever mode the
    # compositor actually granted. So "no server-side title bar" is one
    # property read, correct on every platform and both backends -- and it
    # also covers someone who simply set --border=no, who wants these controls
    # for exactly the same reason. See window_controls_wanted().
    #
    # "always" / "never" override it.
    window_controls: str = "auto"
    # Persist the window size across launches. Off means window_width/height
    # are a fixed preference the app always opens at, which is what you want
    # if you deliberately pinned a size.
    remember_window_size: bool = True
    # When True, closing the library-browser window hides it to the system tray
    # (keeping the app alive as a cast target) rather than exiting. Defaults to
    # the historical behaviour; the user is prompted once on first close.
    close_to_tray: bool = True
    # The same thing for machines with no system tray, where close_to_tray is
    # ignored because the app would have no way back on screen. Opting in says
    # "an invisible background process is what I want" -- it is off by default
    # because the only ways out are `jellyfin-mpv-shim stop` and killing it.
    allow_background: bool = False
    # RAM for DECODED artwork, which is the expensive form: a 4K backdrop is
    # 33 MB decoded against ~400 KB on the wire.
    #
    # Deliberately modest, because this cache sits behind another one. What
    # decoded images are *for* is compositing tile strips, and the strips are
    # themselves cached -- so a decoded poster is only wanted while a row is
    # being built, and scrolling back over a row that is still cached never
    # asks for one. That makes this a working set, not a library: a screenful
    # of posters is ~7 MB, and the big single items are backdrops.
    #
    # It is not a stand-in for the artwork cache on disk either. That one
    # holds the server's compressed bytes and the OS page-caches them for
    # free; what it cannot do is skip the decode, which is what this is.
    library_image_cache_mb: int = 96
    # Pixels a single wheel notch scrolls in the library browser. This is the
    # step the settings page has always used; applying it everywhere lets the
    # scrollbar glide continuously while the content snaps to the nearest row
    # (or home-screen section), so a trackpad/trackball no longer overshoots
    # whole rows. On an equal-row grid the step is rounded so a whole number of
    # notches spans one row (consistent, not a 2-3-2-3 cadence). Raise it to
    # scroll faster, lower it for finer control.
    scroll_wheel_pixels: int = 80
    # How much of a row the wheel is allowed to land in the middle of. Three
    # states, in order of how much they quantize:
    #
    #   continuous  scroll by pixels, draw wherever that lands. The renderer
    #               still aligns to rows on its own for a gesture asking for
    #               frames faster than it can draw them (see state.rcost in
    #               renderer.lua) -- that is a mitigation, not a mode.
    #   aligned     always draw aligned to the nearest row or home-screen
    #               section. The wheel still moves by pixels and the
    #               scrollbar still glides; only the content steps. Makes the
    #               above mitigation permanent, for a display the measurement
    #               reads as keeping up when the user can see that it is not.
    #   row         one wheel notch moves exactly one row or section, and the
    #               scrollbar steps with it. An accessibility escape hatch,
    #               and the oldest behaviour.
    #
    # Was two booleans, snapped_scrolling and force_scroll_snapping, which
    # named the same word twice for different things and could be combined
    # into a state that meant nothing (`row` already draws aligned, so
    # forcing alignment on top of it did nothing at all).
    #
    # Deliberately NOT migrated, though snapped_scrolling shipped in
    # pre-releases and people did set it. They set it because continuous
    # scrolling had been taken away, so it is a record of the best thing
    # available at the time rather than a preference for stepping -- and
    # carrying it forward would keep exactly the people who worked around
    # that on the workaround, with the fix sitting one dropdown away and no
    # reason to look for it. Both old keys load as "ignored" (which is
    # logged) and everyone starts on continuous.
    scroll_mode: str = "continuous"
    # Accessibility: page the library and music tile grids instead of scrolling.
    # Each page is one screenful (no scrolling within it) with a bottom bar to
    # move between pages; adjacent pages are prefetched so paging is instant.
    # Global — applies to every tile grid at once.
    paginated: bool = False
    # Artwork on a transparent background gets a light plate — the page it was
    # drawn for — plus, where its outermost ink is itself white, a drop shadow
    # to hold its shape against that plate. Off, it gets the theme's own card
    # grey and no shadow, which is what jellyfin-web does.
    #
    # Two settings rather than one, because transparent artwork arrives in two
    # conventions that want opposite treatment (see live_tv.is_channel_artwork).
    # A broadcaster's channel logo is usually DARK ink drawn for a white page,
    # and on this UI's near-black surfaces it is invisible without the plate.
    # A film's or series' Logo artwork is white by convention and reads on a
    # dark surface perfectly well — plating that is how you end up needing a
    # shadow to rescue white ink from a white plate, which is what #637 was
    # about. Hence the split defaults. Both are reachable either way: neither
    # is a bug, and libraries differ.
    logo_legibility_live_tv: bool = True
    logo_legibility_library: bool = False
    sync_path: Optional[str] = None
    work_offline: bool = False
    prefer_downloaded: bool = True
    # Auto-download: keep upcoming episodes on disk without being asked.
    # Off by default — it is the only feature that writes to the user's disk
    # unattended, so it is opt-in rather than something to discover after the
    # fact. It runs on a schedule and only while nothing is playing, so it
    # never competes with streaming for bandwidth.
    auto_download_enable: bool = False
    # Sources. next_up follows the server's Next Up across every series;
    # lookahead follows the series you are actually working through, queueing
    # this many episodes past the last one you watched (0 disables it).
    auto_download_next_up: bool = True
    # Next Up is as long as your started-series count -- 50+ on a real
    # library, far more than anyone wants fetched unattended. The server
    # returns it most-recent first, so a small limit is the shows you are
    # actually working through.
    auto_download_next_up_limit: int = 10
    auto_download_lookahead: int = 2
    #: Hysteresis for the lookahead (#661), and the per-pass cap that was
    #: hardcoded. **All three default to None, meaning "behave exactly as
    #: before"** -- a flat window of auto_download_lookahead and a 20-item
    #: pass. That is deliberate: the failure mode this feature is about is
    #: unwanted disk activity, so an install that never opens the setting
    #: must not change behaviour at all.
    #:
    #: With min and max set, a series is left alone while it already has at
    #: least `min` upcoming episodes held or queued, and is topped up to
    #: `max` when it drops below. Fewer, larger batches -- which is the
    #: point: the reporter's HDDs were spinning up for one episode at a
    #: time.
    auto_download_lookahead_min: Optional[int] = None
    auto_download_lookahead_max: Optional[int] = None
    auto_download_max_per_pass: Optional[int] = None
    # Budget for auto-downloads only (see SyncDB.auto_size). Downloads the
    # user asked for are never counted against it and never reaped.
    auto_download_max_gb: int = 20
    # Retention. Both can be on; delete_watched keeps the footprint near the
    # lookahead window, keep_days reclaims a show that was abandoned midway.
    # keep_days = 0 means never expire on age alone.
    auto_download_delete_watched: bool = True
    auto_download_keep_days: int = 30
    auto_download_interval_mins: int = 60
    # Which servers the scheduler may pull from, as a comma-separated list of
    # server uuids. Empty means NONE, not all: a logged-in server may be a
    # friend's, and pointing unattended downloads at someone else's hardware
    # is a rude thing to do by default. Switching auto-download on seeds this
    # with the server you were looking at when you did it, which is the one
    # you meant; every other server — and every other local user, whose
    # servers have different uuids — stays off until ticked in the Servers
    # tab.
    auto_download_servers: Optional[str] = None
    media_key_seek: bool = False
    # The mouse's back/forward buttons jump a chapter during playback.
    # Off by default: they are easy to hit by accident on some mice,
    # and skipping a chapter of a film is not as forgiving as the Back
    # press they perform in the library. Read when mpv is created.
    mouse_chapter_nav: bool = False
    mpv_ext: bool = sys.platform.startswith("darwin")
    mpv_ext_path: Optional[str] = None
    mpv_ext_ipc: Optional[str] = None
    mpv_ext_start: bool = True
    mpv_ext_start_retries: int = 10
    mpv_ext_start_retry_delay_ms: int = 3000
    mpv_ext_no_ovr: bool = False
    use_web_seek: bool = False
    # Locked-down cast-target mode: the cast screen is the only page, and
    # the library cannot be reached from the machine itself. Replaces
    # display_mirroring, which was a *second* UI rather than a page and so
    # could not coexist with the browser. NOT a security boundary — anyone
    # who can attach input can usually also edit this file, and the tray
    # still reaches Settings. It stops a plugged-in mouse from playing the
    # library, which is what it is for.
    headless: bool = False
    # Browsing on a phone mirrors onto this client: a DisplayContent event
    # navigates the library browser to whatever the remote is looking at.
    # That stays on. What is off by default is letting it *summon* the
    # window: with the browser closed to the tray, idly scrolling a phone
    # would otherwise pop the app open on the TV. The page is still
    # navigated, so it is waiting when the browser is next opened.
    display_mirror_summon: bool = False
    log_decisions: bool = False
    mpv_log_level: str = "info"
    idle_when_paused: bool = False
    stop_idle: bool = False
    # Optional, because the documentation has always said "you can also set
    # them to `null` to disable the shortcut" and `str` made that a lie: a
    # JSON null coerced to the STRING "None", which _bind_key's None guard
    # ("an unset keybind is a supported configuration") could never match.
    # It looked like it worked only because no keyboard produces a key named
    # None, so mpv bound something unreachable instead of binding nothing.
    # #16 needs the difference to be real: "the user cleared this" is how a
    # one-time migration to input.conf tells someone who parked our
    # interception on purpose from someone who never touched it.
    kb_stop: Optional[str] = "q"
    kb_prev: Optional[str] = "<"
    kb_next: Optional[str] = ">"
    kb_watched: Optional[str] = "w"
    kb_unwatched: Optional[str] = "u"
    kb_menu: Optional[str] = "c"
    kb_menu_esc: Optional[str] = "esc"
    kb_menu_ok: Optional[str] = "enter"
    kb_menu_left: Optional[str] = "left"
    kb_menu_right: Optional[str] = "right"
    kb_menu_up: Optional[str] = "up"
    kb_menu_down: Optional[str] = "down"
    kb_pause: Optional[str] = "space"
    kb_fullscreen: Optional[str] = "f"
    kb_kill_shader: Optional[str] = "k"
    # seek_up / seek_down / seek_right / seek_left / seek_v_exact /
    # seek_h_exact were removed in config version 4. #16 gave the arrow
    # keys back to mpv, so a seek distance lives in the user's input.conf
    # now; the settings could not work after that (a changed distance made
    # the shim CLAIM the key, and the claim seeks by mpv's own amount) and
    # were never in the settings UI or the README to begin with. The
    # migration carries an old value across by reading the raw config --
    # see input_conf.LEGACY_SEEK_DEFAULTS.
    shader_pack_enable: bool = True
    shader_pack_custom: bool = False
    shader_pack_remember: bool = True
    shader_pack_profile: Optional[str] = None
    shader_pack_subtype: str = "lq"
    shader_pack_gpu_api: str = "auto"
    svp_enable: bool = False
    svp_url: str = "http://127.0.0.1:9901/"
    svp_socket: Optional[str] = None
    sanitize_output: bool = True
    write_logs: bool = False
    playback_timeout: int = 30
    #: Seconds a photo is held before the queue moves to the next item --
    #: mpv's --image-display-duration, set per start. It cannot simply be
    #: left to mpv.conf: the in-window browser parks the same option at
    #: "inf" while it owns the window. mpv's own default of 1s is a
    #: slideshow you cannot read; 5 is roughly what the other clients use.
    photo_display_secs: int = 5
    sync_max_delay_speed: int = 50
    sync_max_delay_skip: int = 300
    sync_method_thresh: int = 2000
    sync_speed_time: int = 1000
    sync_speed_attempts: int = 3
    sync_attempts: int = 5
    sync_osd_message: bool = True
    screenshot_menu: bool = True
    check_updates: bool = True
    notify_updates: bool = True
    lang: Optional[str] = None
    discord_presence: bool = False
    ignore_ssl_cert: bool = False
    menu_mouse: bool = True
    media_keys: bool = True
    input_gamepad: bool = False
    #: Which face button confirms. mpv reports the face buttons by POSITION
    #: -- ACTION_DOWN is the bottom one whatever is printed on it -- and the
    #: two common layouts disagree about where A lives: Xbox-style pads put
    #: it at the bottom, Nintendo-style ones (Switch Pro, most 8BitDo) put it
    #: on the right. There is no way to tell them apart from the button
    #: events, so this is the user saying which pad is in their hands. See
    #: gamepad.py.
    gamepad_swap_confirm: bool = False
    connect_retry_mins: int = 0
    transcode_warning: bool = True
    lang_filter: str = "und,eng,jpn,mis,mul,zxx"
    lang_filter_sub: bool = False
    lang_filter_audio: bool = False
    remember_audio_track: bool = True
    remember_subtitle_track: bool = True
    # Audio output mode. "auto" touches nothing at all -- no audio-channels,
    # no audio-spdif, no filters -- so mpv (and the user's own mpv.conf)
    # decide. The other modes are described in player.py:apply_audio_settings.
    # Replaced the old audio_output key, which nothing ever read.
    audio_mode: str = "auto"
    # Which output device mpv opens, as an mpv device name
    # ("alsa/iec958:CARD=X,DEV=0", "wasapi/{guid}", ...). None means "don't
    # touch it", so mpv's own default and anything in the user's mpv.conf are
    # left alone -- the same contract audio_mode's "auto" has.
    #
    # This exists because passthrough usually cannot go through a sound
    # server. The IEC61937 non-audio flag lives in the S/PDIF channel status,
    # which is set through the ALSA device name's AES parameters, and a
    # PipeWire/PulseAudio device has nowhere to put them: the stream arrives
    # as ordinary PCM and a receiver plays it as static. Reaching the device
    # directly is the only way, and picking it needs a control.
    audio_device: Optional[str] = None
    # Take the device exclusively, so nothing else can mix into it (and
    # nothing resamples or attenuates a bitstream on the way out). mpv only
    # honours this on wasapi, coreaudio and sndio -- not ALSA -- so the
    # settings form hides it where it would do nothing.
    audio_exclusive: bool = False
    # Which codecs to pass through, for the modes that pass anything through.
    # Default on: a mode is opt-in already, and picking "HDMI Passthrough"
    # only to find nothing passes through would be surprising. Which of these
    # are *offered* depends on the mode (S/PDIF has the bandwidth for AC3 and
    # DTS core only) -- see AUDIO_PASSTHROUGH_CODECS.
    audio_passthrough_ac3: bool = True
    audio_passthrough_dts: bool = True
    audio_passthrough_eac3: bool = True
    audio_passthrough_dts_hd: bool = True
    audio_passthrough_truehd: bool = True
    # Optical only. Anything the cable cannot pass through is encoded to AC3,
    # which is the only way surround crosses S/PDIF at all. Off means those
    # tracks go out as stereo PCM instead -- worse, but the encoder adds
    # latency on some receivers and that is a real reason to refuse it.
    audio_optical_encode_ac3: bool = True
    # "Night mode": compress the dynamic range so explosions and dialogue end
    # up at similar volumes. Independent of audio_mode, but it forces PCM --
    # a filter cannot sit downstream of a compressed passthrough stream.
    audio_night_mode: bool = False
    language_preference: str = "custom"
    preferred_language: str = "eng"
    force_set_played: bool = False
    screenshot_dir: Optional[str] = get_default_sdir()
    raise_mpv: bool = True
    force_video_codec: Optional[str] = None
    force_audio_codec: Optional[str] = None
    health_check_interval: Optional[int] = 300
    # One per media segment type the server knows (SEGMENT_SETTINGS below),
    # each "off" / "ask" / "always". Replaces the four booleans that covered
    # only intros and credits; migrated from them at CONFIG_VERSION 2.
    segment_intro: str = "ask"
    segment_outro: str = "ask"
    segment_commercial: str = "off"
    segment_preview: str = "off"
    segment_recap: str = "off"
    # Seeking forward during an intro/credits window skips the whole
    # segment. Applies to keyboard/remote seeks; seeks made from the
    # jellyfin OSC's seekbar never trigger it (it has its own button).
    skip_intro_on_seek: bool = False
    thumbnail_enable: bool = True
    thumbnail_osc_builtin: bool = True
    # In-player UI: "mpvtk" (the in-window playback HUD rendered by the
    # library browser — jellyfin-web styled, remote navigable; needs
    # enable_gui, falls back to "mpv" otherwise), "mpv" (stock
    # mpv OSC patched with trickplay previews), or "default" (whatever
    # OSC is built into the mpv binary / the user's own scripts).
    # "jellyfin" is a legacy alias for "mpvtk" (the lua OSC it used to
    # name was retired once the HUD reached parity).
    osc_style: str = "mpvtk"
    # Playback HUD looks and lifecycle (osc_style "mpvtk"). The scrim
    # is what makes the controls legible over any frame; "none" pays
    # for that with a drop shadow on the text instead.
    hud_scrim: str = "default"
    # When the controls hide: "hover" (they stay while the pointer is
    # on them, paused or not), "always" (the timer, regardless), or
    # "paused" (the timer, but never while paused).
    hud_autohide: str = "hover"
    # Seconds of no input before they hide. 0 means "as soon as the
    # pointer is not on them", and forces the hover mode.
    hud_hide_secs: float = 4.0
    # Scale factor for the whole in-player UI (tiles, text, chrome).
    # null follows the display: mpv's display-hidpi-scale, which is 1.0
    # on X11 and the compositor's factor on Wayland/macOS. Set a number
    # (1.5, 2.0) to force it — useful on a 1x display to see what a HiDPI
    # user gets. Read once at startup; changing it needs a restart,
    # because rescaling live means dropping every cached bitmap and that
    # is only safe on the libmpv path once mpv is gone.
    ui_scale: Optional[float] = None
    #: Multiplier on the UI's base text size, for readability. Separate
    #: from ui_scale, which scales the whole interface (boxes, artwork,
    #: spacing) -- this moves only the type, so a user who wants bigger
    #: words without bigger posters has a control that says so.
    ui_text_scale: float = 1.0
    #: Nothing in the interface renders smaller than this, whatever the
    #: scale works out to. 0 = no floor. For low vision: a multiplier
    #: keeps the smallest text the smallest text, so a floor is the
    #: control that actually raises the hardest thing on screen to read.
    ui_text_min: int = 0
    # Library-browser theme (see mpvtk_browser/themes.py). "default" is the
    # stock look; poster_scale overrides the theme's own cover size.
    theme: str = "default"
    #: Cover Size: a multiplier on the tile ARTWORK, every shape of it
    #: (poster, square, landscape/thumbnail, banner). Deliberately not a
    #: text control -- captions and badges keep their size, because
    #: ui_text_scale/ui_text_min are what move type and a second unlabelled
    #: one is how "bigger covers" quietly became "bigger everything".
    poster_scale: Optional[float] = None
    # The in-window epub reader (mpvtk_browser/pages/reader.py). These are
    # settings rather than per-book state because they are a property of
    # the reader's eyes and the room, not of the book: somebody who needs
    # 27px needs it for every book, and having set it once should never be
    # asked again. The reader's own A-/A+ and page-colour buttons write
    # these, so the control on screen and the row in Settings are the same
    # value seen twice.
    #
    # Type size is in logical pixels before UI scaling, like every other
    # size in the browser. The reader steps it through a list of sensible
    # sizes; a number typed here is used as-is, clamped to something that
    # can hold a line of text.
    reader_font_size: int = 21
    # Page colour: "dark", "sepia" or "light". Dark by default because the
    # app around it is dark and a white page in a dark room at midnight is
    # the reason readers grew this setting -- not because it is better for
    # reading, which the other two are there for.
    reader_theme: str = "dark"
    # Justified text is what a printed book does, and what every epub is
    # typeset expecting. Off gives a ragged right edge, which some people
    # find easier and which avoids the rivers a narrow window can open up.
    reader_justify: bool = True
    # The comic reader's reading mode: "width" (the page as wide as the
    # window, scrolled down) or "page" (a whole page at once). Sticky
    # because it is a preference about how somebody reads comics, not
    # about one comic -- a reader who wants whole pages wants them for
    # every book, and picking it again per volume is the annoyance.
    comic_fit: str = "width"
    # While a video plays with the HUD hidden, grab UP/DOWN/LEFT/RIGHT
    # (and ENTER) to summon/drive the HUD. Off by default: mpv's own
    # seek keys keep working and only hud_wake_key is taken over.
    #: Where a grid's leftover width goes. A row of fixed-size tiles almost
    #: never divides the available width exactly, and the remainder used to
    #: land entirely on the right -- at some window sizes that is most of a
    #: tile's width of empty background down one side.
    #:
    #: "justify" (the default) widens the gaps so the row runs margin to
    #: margin; "center" splits the remainder between the two margins and
    #: leaves the tiles evenly spaced; "off" is the old behaviour.
    #:
    #: justify rather than center because of WHICH number moves. Centring
    #: makes both margins follow the window, and the page margin is the edge
    #: every heading, button and paragraph on the screen lines up against --
    #: a margin that changes as you drag is more noticeable than tiles that
    #: sit a few pixels further apart.
    grid_fill: str = "justify"
    #: Detail, series and programme headers span the whole viewport, with
    #: no padding above or beside them -- jellyfin-web's full-bleed
    #: backdrop.
    #:
    #: **On by default -- and it shipped off.** Both of the things that
    #: decided that turned out to be bugs elsewhere rather than costs of
    #: this mode, and both are fixed:
    #:
    #: * "backdrop artwork is low-resolution and this stretches it
    #:   furthest" was the header asking the thumbnail store to decode into
    #:   a box shaped like the BANNER. That store contains where the
    #:   compositor covers, so a 1920x1080 backdrop was contained down to
    #:   796px wide and then blown back up over the header -- not a
    #:   property of the artwork at all (TileRenderer.backdrop_node);
    #: * "a full-width header stops 10px short of the window" was the
    #:   scrollbar gutter being reserved by this side unconditionally and
    #:   by the layout only when the content overflowed. The layout now
    #:   reserves it always and renderer.lua paints the track whenever it
    #:   exists, so the strip beside the banner is a scrollbar rather than
    #:   a void (mpvtk.layout).
    #:
    #: It costs no vertical space either way: the header keeps the height
    #: the padded version had (see TileRenderer.banner_box).
    backdrop_full_width: bool = True
    #: Left-click on the video toggles pause (#669).
    #:
    #: On -- the default, and what this client has always done -- the
    #: hidden HUD takes ``mbtn_left`` and pauses, like the lua OSC's
    #: click-anywhere. Off gives mpv's own modality back: nothing binds the
    #: left button, so the VO drags the window with it and RIGHT click is
    #: what pauses. Measured, both ways: double-click still fullscreens in
    #: *either* mode, because mpv delivers mbtn_left, mbtn_left_dbl,
    #: mbtn_left for a double -- so the two pause toggles cancel and the
    #: default binding still fires.
    mouse_click_pauses: bool = True
    hud_grab_keys: bool = False
    # The key that summons the HUD for keyboard driving while it is
    # hidden (mpv key name syntax). ENTER also toggles pause on wake.
    hud_wake_key: str = "ENTER"
    thumbnail_preferred_size: int = 320
    tls_client_cert: Optional[str] = None
    tls_client_key: Optional[str] = None
    tls_server_ca: Optional[str] = None
    language_config: Optional[List[LanguageRule]] = None

    def _migrate(self, data=None):
        """Apply one-time upgrades for configs written by older versions.

        Every key is written to disk on save (see SettingsBase.dict), so a
        changed default alone never reaches an existing install -- the old
        value is already recorded. Migrations here bring those installs
        forward. Returns whether anything changed (the caller saves).

        Only migrate a default whose old value is actively harmful; a setting
        the user may have deliberately chosen cannot be distinguished from an
        inherited default, so each step here silently overrides a real choice.
        A step that RENAMES a setting is exempt from that caution -- carrying
        a real choice across is the whole point -- and needs ``data``, the
        raw config as read from disk, because the old key is no longer in the
        schema and so never reaches an attribute.
        """
        changed = False
        data = data or {}
        if self.config_version < 3:
            # The kb_* settings were `str` while the documentation said you
            # could set them to null to disable a shortcut -- so a JSON null
            # coerced to the STRING "None", and save() wrote that back
            # verbatim. A user who cleared a binding years ago has "None" on
            # disk *now*, and merely retyping the fields to Optional[str]
            # does not reach them: it loads as a non-empty string, so
            # `settings.kb_stop is None` is still False.
            #
            # This is a RENAME-shaped step, not a changed default: it is
            # carrying a real choice across, and the choice is unreadable
            # without it. #16's migration of these bindings to input.conf
            # turns on exactly this distinction -- someone who parked our
            # interception on purpose must not have the key re-bound for
            # them.
            for key in [k for k in self.__fields__ if k.startswith("kb_")]:
                if getattr(self, key, None) == "None":
                    log.info("Config migration: %s = null (was the string "
                             "\"None\", which is what a cleared binding "
                             "used to be written as).", key)
                    setattr(self, key, None)
                    changed = True
        if self.config_version < 2:
            # The four booleans become one action per segment type. Read from
            # the raw config rather than from self: the old keys are gone from
            # the schema, so they never reach an attribute.
            #
            # adv_bool, not Python truthiness. The keys being read were
            # declared `bool`, which in this schema means adv_bool -- and
            # adv_bool says the STRINGS "false"/"no"/"0"/"off" are False
            # while bool() says they are all True. A hand-edited or
            # templated conf.json that quotes its booleans would otherwise
            # migrate "skip_intro_always": "false" to `always`, i.e. start
            # silently skipping intros for someone who had asked to be
            # prompted -- and the source keys are dropped on the next save,
            # so the evidence goes with them.
            for prefix, key in (("skip_intro", "segment_intro"),
                                ("skip_credits", "segment_outro")):
                if adv_bool(data.get(prefix + "_always")):
                    action = "always"
                elif prefix + "_enable" in data:
                    action = "ask" if adv_bool(data[prefix + "_enable"]) \
                        else "off"
                else:
                    continue
                if getattr(self, key) != action:
                    log.info("Config migration: %s = %s (from %s_*)",
                             key, action, prefix)
                    setattr(self, key, action)
        if self.config_version < 1:
            # mpv plays Dolby Vision natively now, so the old default of
            # force-transcoding it to SDR just burns server CPU and throws
            # away the HDR presentation.
            if self.transcode_dolby_vision:
                log.info(
                    "Config migration: disabling transcode_dolby_vision "
                    "(mpv now supports Dolby Vision natively)."
                )
                self.transcode_dolby_vision = False
        if self.config_version < 4:
            # #16: the keys the user CHANGED become real mpv bindings, and
            # the settings are cleared so nothing binds them twice. The
            # choice moves out of our config and into theirs, where they
            # can edit it like any other mpv binding.
            from . import conffile, input_conf
            from .constants import APP_NAME

            try:
                if input_conf.migrate(
                        self, conffile.get(APP_NAME, "input.conf"),
                        raw=data):
                    changed = True
            except Exception:
                log.warning("Could not migrate key bindings to input.conf",
                            exc_info=True)
        if self.config_version != CONFIG_VERSION:
            self.config_version = CONFIG_VERSION
            changed = True
        return changed

    def __get_file(self, path: str, mode: str = "r", create: bool = True):
        created = False

        if not os.path.exists(path):
            try:
                _fh = open(path, mode)
            except IOError as e:
                if e.errno == 2 and create:
                    fh = open(path, "w")
                    json.dump(self.dict(), fh, indent=4, sort_keys=True)
                    fh.close()
                    created = True
                else:
                    raise e
            except Exception:
                log.error("Error opening settings from path: %s" % path)
                return None

        # This should work now
        return open(path, mode), created

    def load(self, path: str, create: bool = True):
        global config_path  # Don't want in model.
        fh, created = self.__get_file(path, "r", create)
        config_path = path
        if created:
            # A config written from the current defaults is already current;
            # stamp it so _migrate() never re-runs against a fresh install.
            self.config_version = CONFIG_VERSION
            self.save()
        if not created:
            try:
                data = json.load(fh)
                safe_data = self.parse_obj(data)

                # Copy and count items
                input_params = 0
                for key in safe_data.__fields_set__:
                    setattr(self, key, getattr(safe_data, key))
                    # ...and the RECORD of which keys came from the file,
                    # not only their values. parse_obj builds it on the
                    # throwaway object and this loop used to leave it there,
                    # so the global `settings` reported that the user had
                    # touched nothing, ever. Nothing consulted it until #16,
                    # which asks exactly that question to tell "this is our
                    # default, take it back" from "this is the user's
                    # choice, honour it".
                    self.__fields_set__.add(key)
                    input_params += 1

                # Print warnings
                for key, value in data.items():
                    if key not in safe_data.__fields_set__:
                        log.warning("Config item {0} was ignored.".format(key))
                    elif value != getattr(safe_data, key):
                        log.warning(
                            "Config item {0} was was coerced from {1} to {2}.".format(
                                key, repr(value), repr(getattr(safe_data, key))
                            )
                        )

                log.info("Loaded settings from json: %s" % path)
                migrated = self._migrate(data)
                if migrated or input_params < len(self.__fields__):
                    log.info("Saving back due to schema change.")
                    self.save()
            except Exception as e:
                log.error("Error loading settings from json: %s" % e)
                fh.close()
                return False

        fh.close()
        return True

    def save(self):
        if config_path is None:
            raise FileNotFoundError("Config path not set.")

        # Write-temp-then-rename under a lock: concurrent savers can't
        # interleave, and a crash mid-write leaves the old file intact
        # instead of a truncated one.
        with _save_lock:
            tmp = config_path + ".tmp"
            try:
                with open(tmp, "w") as fh:
                    json.dump(self.dict(), fh, indent=4, sort_keys=True)
                    fh.flush()
                os.replace(tmp, config_path)
            except Exception as e:
                log.error("Error saving settings to json: %s" % e)
                return False

        return True


settings = Settings()
