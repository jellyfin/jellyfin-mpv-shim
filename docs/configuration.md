# Configuration Reference

Every setting Jellyfin MPV Shim understands. **Most are easier to change in the app under
Settings**, which has a search box; this page is the complete reference, including the
options with no UI. See the [README](../README.md) for an introduction to the features.

It is laid out the way the Settings screen is: [General](#general), [Browse](#browse) and
[Playback](#playback), then [settings that are only in the config file](#config-file-only),
then the files you can drop into the config folder ([themes](#writing-a-theme),
[`mpv.conf` and `input.conf`](#mpv-configuration)).

## The config file

`conf.json` lives in a per-platform folder. The systray menu can open it, and the path is
printed at startup when launched from a terminal on Linux or macOS.

| Platform | Location |
|---|---|
| Windows | `%appdata%\jellyfin-mpv-shim\conf.json` |
| Linux | `~/.config/jellyfin-mpv-shim/conf.json` |
| Linux (Flatpak) | `~/.var/app/com.github.iwalton3.jellyfin-mpv-shim/config/jellyfin-mpv-shim/conf.json` |
| macOS | `~/Library/Application Support/jellyfin-mpv-shim/conf.json` |

`--config DIR` uses another folder (and runs a separate instance — see the README's
[Tips](../README.md#run-multiple-instances)). The file is read as UTF-8, with or without a
byte-order mark. In JSON, a Windows path needs doubled backslashes: `"C:\\path\\to\\mpv.exe"`.

**When a setting takes effect:** immediately, or for the next thing you play, unless its
entry says *Requires restart* — Settings marks those rows the same way, and offers to restart
for you when one changes.

---

## General

### This Device

- `lang` - Interface language as a locale code (`de`, `pt_BR`, `zh_Hans`). Default: `null`
  (follow the system). Requires restart.
  - The picker shows how much of the app each language covers; the rest stays in English.
    Translations are contributed on [Weblate](https://translate.jellyfin.org/projects/jellyfin/jellyfin-mpv-shim/).
  - "The system" means `LANGUAGE`, then `LC_ALL`, `LC_MESSAGES` and `LANG`; on Windows, the
    user's display language.
- `player_name` - The name this player shows in other clients' cast menus. Default: your hostname. Requires restart.
- `raise_mpv` - Windows only. Bring the player window to the front when playback starts.
  Default: `true`
- `discord_presence` - Share what you are watching via Discord Rich Presence. Default: `false`
  - Needs the optional `pypresence` package (`pip install 'jellyfin-mpv-shim[discord]'`).
    Requires restart.
- `mpris` - Linux: let media keys, headset buttons, the lock screen and desktop media widgets
  control playback, and show what is playing there. Default: `true`
  - Needs PyGObject. Requires restart. Next/previous follow the queue (or seek, with
    `media_key_seek`).
  - The bus name is `com.github.iwalton3.jellyfin-mpv-shim`; with playerctl, give it in full:
    `playerctl -p com.github.iwalton3.jellyfin-mpv-shim play-pause`.
- `check_updates` - Check GitHub for a new release, at most once a day. Default: `true`
- `notify_updates` - Show a notice when an update is available. One of `default`, `enabled`,
  `disabled`. Default: `default`
  - `default` is off in the Flatpak (your software centre announces updates there) and on
    everywhere else.

### Input

- `input_gamepad` - Drive the library and playback with a game controller. Default: `false`
  (on by default on SteamOS). Requires restart.
  - D-pad and left stick move; right stick seeks; bottom face button selects, right goes
    back, left pauses; shoulders page through lists; Start opens the context menu (the
    player's settings menu during playback).
  - **The controller is not focus-aware** — it drives the app even when another window is in
    front.
  - Needs an mpv built with `-Dsdl2-gamepad=enabled`. The Windows builds, the Flatpak and
    Debian's mpv have it; most others do not, and the setting is then ignored (the log says so).
    One controller at a time.
  - Every button is an ordinary mpv key and can be rebound in [`input.conf`](#mpv-configuration);
    a line there wins over the built-in binding. Y/triangle (`GAMEPAD_ACTION_UP`), Select/View
    (`GAMEPAD_BACK`) and both triggers are left free for you:

    ```
    GAMEPAD_ACTION_UP       cycle fullscreen
    GAMEPAD_LEFT_TRIGGER    add speed -0.1
    ```
- `gamepad_swap_confirm` - Confirm on the **right** face button and back on the bottom one,
  as on Nintendo-style pads. Default: `false`
- `browse_block_keys` - Ignore mpv's own single-key shortcuts while the library is on screen.
  Default: `true`
  - Otherwise keys like `1`-`8` (picture adjustments), `d` (deinterlace) and `s` (screenshot)
    act invisibly and carry over into the next video. Arrows, paging, the wheel and modified
    keys are unaffected, and `9`/`0`/`m` still set the volume while music plays.
  - Turn it off to use your own `input.conf` bindings in the library.

### Window

- `fullscreen` - Go fullscreen when playback starts. Default: `false`
- `browser_fullscreen` - Run the library fullscreen too. Default: `false`
  - Pressing `f` remembers your choice in whichever of these two applies at the time.
- `remember_window_size` - Reopen at the size the window was last closed at. Default: `true`
- `window_controls` - Draw move, minimize, maximize and close controls in the app's own top bar,
  plus a resize grip, for windows the desktop gives no title bar. One of `auto`, `always`,
  `never`. Default: `auto`
  - `auto` shows them when mpv reports the window undecorated — GNOME on Wayland, or
    `border=no` in your `mpv.conf` — and not on desktops that draw a real title bar.
  - Dragging the bar to move the window needs **mpv 0.39** or newer; the buttons work on older
    versions.
- `window_controls_fullscreen` - Keep those controls on screen in fullscreen. Default: `false`
- `hide_title_bar` - Ask the desktop for no title bar, so the app's own top bar replaces it.
  Default: `false`. Requires restart.
- `close_to_tray` - Closing the window keeps the app running in the system tray as a cast
  target, instead of quitting. Default: `true`
  - Ignored when there is no tray, unless `allow_background` or `headless` is set.
- `allow_background` - Allow running with no window *and* no tray icon, so `close_to_tray`
  and `start_minimized` work on desktops without a tray. Default: `false`
  - The only ways to stop it then are `jellyfin-mpv-shim stop` or killing the process.
- `start_minimized` - Start in the tray instead of opening the library. Default: `false`
  - Needs a tray or `allow_background`. The `--minimized` flag starts one launch minimized and
    works even without a tray. Launching the app again shows the window.
- `display_mirror_summon` - Let casting from another device open the window, or bring it to
  the front. Default: `false`
  - Off, casting still navigates an already-open library, without stealing focus.

---

## Browse

### Theme

- `theme` - Look of the library browser. Default: `default`
  - Built in: `default`; `nebula` (deep violet, rounded, larger covers); `superdark`
    (near-black, no colour); `elegance` (inspired by the ElegantFin web theme); and
    `jf-blueradiance`, `jf-wmc`, `jf-purplehaze`, `jf-light`, `jf-appletv`, translations of
    jellyfin-web's own themes. The last two are light themes.
  - Colours apply immediately; a theme's cover and heading *sizes* need a restart.
  - The playback controls stay dark under every theme, as jellyfin-web's do.
  - You can write your own — see [Writing a theme](#writing-a-theme).
- `poster_scale` - Cover size, overriding the theme's. Default: `null` (the theme's own)
  - Any number works; Settings offers `0.75` to `1.7`. Scales artwork only, not text. Also on
    each library's **View** menu.
- `ui_scale` - Scale of the whole interface. Default: `null` (follow the display's HiDPI
  factor). Requires restart.
  - Set e.g. `1.5` or `2.0` to make it readable on a TV across the room. `--scale FACTOR`
    overrides it for one run.
  - Downloaded (offline) artwork was fetched at 1x and is upscaled.
- `ui_text_scale` - Multiply the size of all text, leaving artwork and spacing alone.
  Default: `1.0`
  - For when the words are too small rather than everything. If you want more than `1.5`,
    `ui_scale` is usually the better answer.
- `ui_text_min` - No text renders smaller than this many pixels. `0` disables it. Default: `0`
  - The low-vision control: it raises the smallest labels without enlarging headings.

### Library Browser

- `library_image_cache_mb` - Memory budget for decoded artwork. Default: `96`. Requires restart.
  - Raise it if you browse very large libraries and have RAM to spare.
  - Downloaded artwork is also cached on disk (`$XDG_CACHE_HOME/jellyfin-mpv-shim/artwork`,
    `~/Library/Caches` on macOS, `%LOCALAPPDATA%` on Windows) — up to 1 GiB or 5% of free
    space, whichever is less, pruned after 30 days unused. Deleting it is always safe.
- `scroll_wheel_pixels` - How far one wheel notch scrolls. Default: `80`
- `scroll_mode` - One of `continuous`, `aligned`, `row`. Default: `continuous`
  - `continuous` scrolls smoothly, lining rows up only when drawing falls behind.
  - `aligned` never draws a row part-scrolled — try it if scrolling stutters (very large or
    remote displays, external mpv).
  - `row` moves exactly one row per notch.
- `paginated` - Page through tile grids (First / Previous / Next / Last) instead of scrolling.
  Default: `false`
- `remember_filters` - Reopen each library with the filters it had last time. Default: `true`
  - Stored on your server, so they follow your account to other devices running this app.
- `grid_fill` - Where the leftover width of a tile grid goes. One of `justify` (widen the
  gaps), `center` (split it between the margins), `off` (all on the right). Default: `justify`
- `backdrop_full_width` - Run a detail page's backdrop edge to edge, as the web client does.
  Default: `true`
- `detail_poster` - Show the poster inset in a film's or series' header. Default: `true`
- `detail_episode_image` - Show the episode still in an episode's header. Default: `true`
  - Separate so you can avoid spoilers without losing posters.
- `logo_legibility_live_tv` - Put a light plate behind transparent channel logos so dark ones
  stay visible. Default: `true`
- `logo_legibility_library` - The same for libraries drawing **Logo** artwork. Default: `false`
  - Also on the library's **View** menu.
- `clock_12h` - Show times as `8:30 PM` instead of `20:30`. Default: `false`

### Reading

These control the built-in [book readers](../README.md#books-comics-and-audiobooks).

- `reader_font_size` - Text size in the epub reader, in pixels. Default: `21`
  - The reader's `A-` / `A+` buttons change this.
- `reader_theme` - Page colour: `dark`, `sepia` or `light`. Default: `dark`
- `reader_justify` - Justify text, as a printed book does. Default: `true`
- `comic_fit` - How a comic page opens: `width` (fit width, scroll down) or `page` (whole
  page). Default: `page`
  - The comic reader's **Fit Width** / **Fit Page** buttons change this.

### Downloads

Downloads are managed from **Downloads** in the sidebar; these settings control where they go
and what is fetched automatically.

- `sync_path` - Where downloads are stored. Default: `null` (a `downloads` folder in the
  config folder)
  - **Change it in Settings, not by hand** — Settings moves the existing files; editing the
    path leaves them behind. The new folder must be empty, and the app owns it afterwards.
- `prefer_downloaded` - Play the downloaded copy when there is one. Default: `true`
- `work_offline` - Browse downloaded media only and don't contact the server. Default: `false`

**Automatic downloads** keep upcoming episodes on disk. They run on a schedule, only while
nothing is playing, and are off by default since they write to your disk unattended.

- `auto_download_enable` - Turn automatic downloads on. Default: `false`
- `auto_download_next_up` - Download from your Next Up list. Default: `true`
- `auto_download_next_up_limit` - How many Next Up entries to consider (most recent first).
  Default: `10`
- `auto_download_lookahead` - Episodes to keep ahead of where you are in each series. `0`
  disables it. Default: `2`
  - Counted from the next episode to watch, so a series you stop watching does not keep
    downloading.
- `auto_download_max_gb` - Storage budget for automatic downloads, in GB. Default: `20`
  - Downloads you started yourself are never counted or deleted automatically.
- `auto_download_delete_watched` - Delete automatic downloads once watched. Default: `true`
- `auto_download_keep_watched_hours` - ...but keep them this many hours first. `0` deletes on
  the next check. Default: `24`
  - When the budget is full, watched downloads are removed early to make room.
- `auto_download_keep_days` - Delete unwatched automatic downloads after this many days. `0`
  keeps them. Default: `30`
- `auto_download_interval_mins` - How often to check, in minutes. Default: `60`

Under **Show advanced settings**:

- `auto_download_lookahead_min` - Leave a series alone while it has at least this many
  upcoming episodes held or queued. Default: `null`
- `auto_download_lookahead_max` - ...and top it up to this many when it drops below.
  Default: `null`
  - **Set both or neither** (otherwise `auto_download_lookahead` applies). This downloads in
    fewer, larger batches, so a spun-down disk wakes less often.
- `auto_download_max_per_pass` - How many items one check may queue. Default: `null` (20)

---

## Playback

### Player Controls

- `osc_style` - Which on-screen controls to use during playback. Default: `mpvtk`. Requires restart.
  - `mpvtk` - The app's own controls, styled after jellyfin-web: seek bar with chapters and
    previews, track and quality pickers, a settings (gear) menu, Skip Intro/Credits. Works
    with keyboard, mouse and remotes. Needs `enable_gui`.
  - `mpv` - mpv's own controls, with seek previews.
  - `custom` - Your own OSC script (e.g. uosc). Put it in `scripts/` in the config folder
    (fonts in `fonts/`), and add `osc=no` to `mpv.conf`. Seek previews still work.
  - `none` - No on-screen controls; keyboard shortcuts and the menu key still work.
  - **With `mpv` or `custom`**, change audio and subtitle tracks with the menu key (`c`) or
    from the library, not the OSC's own picker: that picker cannot see Jellyfin's external or
    burned-in subtitles, and its choice is not remembered. Its playlist and next/previous
    buttons do nothing, because the queue is kept by the app.
- `thumbnail_enable` - Show [seek previews](#seek-previews-trickplay) while scrubbing.
  Default: `true`. Requires restart.
- `hud_grab_keys` - Let the arrow keys and ENTER always drive the on-screen controls while
  they are up. Default: `false`
  - Off, the arrows keep seeking and only `hud_wake_key` takes keyboard control.
- `hud_wake_key` - The key that brings up the controls for keyboard use (it also toggles
  pause). Default: `ENTER`
- `hud_scrim` - Shading behind the controls: `default` (gradient), `panel` (flat band behind
  each bar) or `none` (outlined text instead). Default: `default`
- `hud_autohide` - When the controls hide: `hover` (unless the pointer is on them), `always`
  (even while paused) or `paused` (never while paused). Default: `hover`
- `hud_hide_secs` - Seconds without input before the controls hide. Default: `4.0`
- `hud_auto_scale` - On a narrow window, shrink the controls (down to 24×24 px targets) before
  dropping any. Default: `true`
- `mouse_chapter_nav` - The mouse's back/forward buttons skip chapters during playback.
  Default: `false`. Requires restart.
- `mouse_click_pauses` - Left click on the video pauses. Default: `true`
  - Off restores mpv's behaviour: drag the video to move the window, right click to pause.
    Double click is fullscreen either way.
- `trickplay_fast_mode` - Load every seek preview up front. Default: `false`
  - Previews never wait, at the cost of hundreds of MB of memory on a long video. See
    [Seek previews](#seek-previews-trickplay).

### Playback

- `auto_play` - Play the next item in the queue automatically. Default: `true`
- `hwdec` - Hardware video decoding: `no`, `over-1080p`, `auto` or `auto-copy`. Default: `no`
  - `over-1080p` uses hardware only above 1080p — the cautious way to turn it on.
  - `auto-copy` is slower than `auto` but avoids driver interop problems, and is the one that
    works with video filters such as SVP.
  - If turning it on breaks video, start once with `--disable-hwdec` and change it back.
  - **A top-level `hwdec` in your `mpv.conf` wins**: the app then sets none, and Settings
    shows "Pinned by config". A shader profile that needs a specific decoder (`rtx-vsr`
    needs `d3d11va`) sets it while loaded.
- `network_buffer` - How far ahead to read: `default` (mpv's, one second), `large` (20 s,
  400 MiB) or `huge` (60 s, 1 GiB). Default: `default`
  - Raise it if playback stops for buffering over a slow or distant connection.
- `always_transcode` - Always ask the server to transcode. Default: `false`
  - The server may still direct play files that already fit the profile; lower the bandwidth
    limit to force it.
- `local_kbps` - Bandwidth limit on your local network. Default: `2147483`
- `remote_kbps` - Bandwidth limit for remote streaming. Default: `10000`
- `direct_paths` - Open files straight from their SMB/NFS share instead of streaming.
  Default: `false`
- `remote_direct_paths` - Use direct paths even when the server is remote. Default: `false`
- `playback_timeout` - Seconds to wait for a file to start before giving up. Default: `30`
  - Hitting this usually means a missing or corrupt file on the server.

### Audio

By default nothing about audio is changed and mpv (and your `mpv.conf`) decides. These are for
sending audio to a receiver.

- `audio_device` - Which output device to use, as an mpv device name. Default: unset (mpv
  decides)
  - Settings lists the devices on this machine. **This is usually what passthrough needs** —
    see [below](#passthrough-troubleshooting).
- `audio_exclusive` - Take the device exclusively. Default: `false`
  - Windows (WASAPI), macOS and sndio only; on Linux choose the hardware device with `audio_device`.
- `audio_mode` - Default: `auto`
  - `auto` - Change nothing.
  - `stereo` - Downmix to stereo, normalized.
  - `optical` - S/PDIF: passes through AC3 and DTS, and encodes anything else to AC3 so you
    keep surround.
  - `hdmi` - Passes through every format your receiver accepts; anything else is sent as
    multichannel PCM.
- `audio_passthrough_ac3`, `audio_passthrough_dts`, `audio_passthrough_eac3`,
  `audio_passthrough_dts_hd`, `audio_passthrough_truehd` - Which formats your receiver
  accepts. All default to `true`; Settings shows only those the selected mode can carry.
- `audio_optical_encode_ac3` - In `optical` mode, encode other formats to AC3. Default: `true`
  - Off, they are sent as stereo PCM. Some receivers add latency with the encoder.
- `audio_night_mode` - Even out loud effects and quiet dialogue. Default: `false`
  - Also in the player's settings menu. Disables passthrough while on.

Don't combine `audio-spdif` and `af=lavcac3enc` in your own `mpv.conf` (an old tip): mpv
silently drops the encoder. `audio_mode: optical` does this correctly, per track.

#### Passthrough troubleshooting

mpv can report a passthrough stream (`spdif-ac3` in the log) that never reaches the receiver:
the bitstream travels as ordinary PCM, so a sound server that resamples it, changes its volume
or routes it to the wrong device turns it into static or silence.

The reliable setup is to **bypass the sound server**: set `audio_device` to the S/PDIF or HDMI
device itself (`alsa/iec958:CARD=…`), not a `pulse/…` or `pipewire` entry, and on Windows or
macOS also enable `audio_exclusive`. On Linux the sound server usually holds that device open,
so release it first:

```
pactl set-card-profile <card-name> off       # card names from `pactl list cards`
```

Signs worth knowing on PipeWire/PulseAudio:

- `pactl list cards` listing only `iec958-stereo`, with no `iec958-ac3-surround-51`, means the
  sound server will never pass through on that card — going direct is the only route.
- `Unknown PCM default:AES0=6,...` in the log means mpv could not open the device and fell back
  to `default`. `cat /proc/asound/card*/pcm0p/sub0/status` shows who has it open.
- With plain speakers attached, loud static is the *correct* result for a bitstream.

### Subtitles & Languages

- `subtitle_size` - Subtitle size, in percent. Default: `100`
- `subtitle_color` - Subtitle colour, as hex. Default: `#FFFFFFFF`
- `subtitle_position` - `top`, `bottom` or `middle`. Default: `bottom`
  - These three do not apply to every subtitle format, nor to subtitles burned in by a
    transcode. For full styling, use `mpv.conf`.
- `language_preference` - Automatic track selection preset: `unset`, `dubbed_shows`,
  `subbed_shows`, `dubbed_all`, `subbed_all` or `custom`. Default: `custom`
  - **Subbed** prefers Japanese audio with full `preferred_language` subtitles, then any
    `preferred_language` subtitles, then `preferred_language` audio.
  - **Dubbed** prefers `preferred_language` audio with signs/songs subtitles, then
    `preferred_language` audio alone, then the subbed rules.
  - The *shows* variants leave films to the server's choice.
  - **The subbed presets assume the original audio is Japanese** (the anime case). For
    anything else, choose `custom` and write [`language_config`](#language-config-power-user)
    rules.
  - Unset writes no rules; Custom leaves your own rules alone.
- `preferred_language` - The language the presets are built around, as your files tag it
  (usually ISO 639-2/B, like `eng`). Default: `eng`
- `remember_audio_track` - Reuse your audio choice for later episodes of a show. Default: `true`
- `remember_subtitle_track` - Reuse your subtitle choice for later episodes. Default: `true`
- `lang_filter` - Languages the audio and subtitle **menus** list. Default:
  `und,eng,jpn,mis,mul,zxx`. Requires restart.
  - **Does nothing until `lang_filter_sub` or `lang_filter_audio` is on.** It only shortens
    the menus; it does not choose tracks. Remove `und` to hide untagged tracks.
- `lang_filter_sub` - Apply `lang_filter` to the subtitle menu. Default: `false`
- `lang_filter_audio` - Apply `lang_filter` to the audio menu. Default: `false`

### Transcoding

- `allow_transcode_to_h265` - Allow the server to transcode *to* HEVC. Default: `false`
  - This also breaks forced transcoding of HDR and Dolby Vision
    ([jellyfin#9313](https://github.com/jellyfin/jellyfin/issues/9313)).
- `prefer_transcode_to_h265` - Ask for HEVC as the transcode target. Default: `false`
- `transcode_hevc` - Always transcode HEVC video. Default: `false`
- `transcode_av1` - Always transcode AV1 video. Default: `false`
- `transcode_4k` - Always transcode video above 1080p. Default: `false`
- `transcode_hdr` - Always transcode HDR to SDR. Default: `false`
- `transcode_hi10p` - Always transcode 10-bit video to 8-bit. Default: `false`
- `transcode_dolby_vision` - Always transcode Dolby Vision to SDR. Default: `false`
  - mpv plays Dolby Vision itself with `vo=gpu-next`, which is mpv's default from **0.41**
    (the Windows and Flatpak builds ship it). With an older system mpv, set `vo=gpu-next` in
    `mpv.conf` rather than enabling this: the server only tone maps if its admin set that up.
- `extract_image_subtitles` - While transcoding, deliver image subtitles (PGS, and VobSub on
  Jellyfin 12+) as a separate track instead of burning them in. Default: `true`
  - The server extracts every subtitle stream in the file to do this. External `.sup`/`.mks`
    files are unaffected.
- `force_video_codec` - Request this video codec. Default: `null`
  - Combine with `always_transcode` to force a transcode into it. Overrides the `transcode_*`
    codec options above.
- `force_audio_codec` - Request this audio codec. Default: `null`

### Video Enhancement

- `deband` - Smooth the stepping in gradients (skies, fades, dark scenes). One of `off`,
  `light`, `standard`, `strong`. Default: `off`
  - Labelled by content: animation benefits most; detailed live action is mostly untouched,
    and `strong` can soften fine low-contrast texture. Each step costs some GPU.
  - What each writes (mpv's defaults: 1 iteration, threshold 48, range 16, grain 32):

    | | iterations | threshold | range | grain |
    |---|---|---|---|---|
    | `light` | 1 | 32 | 16 | 16 |
    | `standard` | 2 | 48 | 14 | 24 |
    | `strong` | 4 | 64 | 12 | 32 |

    Range falls as iterations rise because mpv's radius grows with each iteration.
  - The parameters are not exposed individually: leave this `off` and set `deband*` in your
    `mpv.conf` for a specific combination. `off` writes nothing, so your values stand — true of
    every setting in this group. When not `off`, this overrides the shader pack's debanding.
- `render_quality` - `default` or `high`, mpv's `high-quality` preset (better upscaling,
  some GPU cost, no shader files). Default: `default`
  - Worth trying before the shader pack. Its HDR options need mpv 0.37+ and only have an
    effect on `vo=gpu-next`.
- `tone_mapping` - How HDR is fitted to an SDR display: `auto`, `bt.2390`, `bt.2446a`,
  `spline`, `hable`, `reinhard` or `clip`. Default: `auto`
  - These are mpv's curves. `auto` leaves mpv's choice alone; try `clip` if HDR looks grey and
    flat. Has no effect when HDR is passed through to an HDR display.
- `shader_pack_enable` - Offer the [shader pack](#shader-packs)'s video profiles. Default: `true`. Requires restart.
- `shader_pack_subtype` - Profile group: `lq` or `hq` (for a capable graphics card).
  Default: `lq`
- `shader_pack_remember` - Remember the last profile you chose. Default: `true`
- `shader_pack_gpu_api` - Graphics API while a profile is loaded: `auto`, `vulkan`, `d3d11`
  or `opengl`. Default: `auto`
  - Change it only if video breaks when a profile loads. `opengl` is the most compatible but
    can cost HDR output; on Windows `d3d11` and `vulkan` keep HDR.
- `deinterlace_auto` - Deinterlace video flagged as interlaced. Default: `false`
  - The flag is unreliable both ways. For an interlaced file that is not flagged, use
    **Deinterlace** in the player's settings menu, which lasts until you return to the library.
  - Needs mpv 0.38+; on older builds it behaves as off.
- `motion_interpolation` - Blend frames to reduce judder (24 fps on a 60 Hz screen):
  `off`, `smooth`, `blend` or `hq`. Default: `off`
  - `smooth` blends only at frame transitions and stays sharp; `blend` is smoothest but softer
    on pans; `hq` is smoother again and costs real GPU. This is frame blending, not SVP-style
    motion synthesis.
  - Switches mpv to `video-sync=display-resample`, which adjusts audio speed slightly — not
    what you want when bitstreaming to a receiver.
  - **Dropping frames on capable hardware?** Suspect mixed refresh-rate monitors: mpv may read
    the wrong one. Try `display-fps-override=<your screen's rate>` in `mpv.conf`. Dropped
    frames are shown in the player's **Playback Info**.

### Skip Intro / Credits

Uses the server's media segments. Each setting is `off`, `ask` (show a **Skip** button; a
seek prompt with other OSC styles) or `always` (skip silently).

- `segment_intro` - Intros. Default: `ask`
- `segment_outro` - Credits. Default: `ask`
- `segment_commercial` - Commercials. Default: `off`
- `segment_preview` - Previews. Default: `off`
- `segment_recap` - Recaps. Default: `off`
- `skip_intro_on_seek` - Seeking forward with the keyboard or a remote during an intro or
  credits skips the whole segment. Default: `false`

---

## Config-file only

Settings with no dedicated control. Most also appear under **General → Show advanced
settings**.

### Startup and background

- `enable_gui` - The window, tray and library. Default: `true`. Requires restart.
  - **Off puts the app in command-line mode with no window and no Settings** — the only way
    back is editing `conf.json`. It is not how to get mpv's own controls; that is
    `osc_style: mpv`.
- `headless` - Cast-target mode: show "Ready to cast" instead of the library, and make the
  library unreachable from this machine. Default: `false`. Requires restart.
  - Not a security boundary — see [Cast-target mode](../README.md#cast-target-mode-headless).
    Without a tray icon the way back is editing `conf.json`.
  - For an ordinary "wait in the background for casts" setup you don't need this: use
    `close_to_tray` (or `allow_background`), `start_minimized` and `fullscreen`.
- `connect_retry_mins` - At launch, keep retrying the server this many minutes before showing
  the login screen. Default: `0`
- `health_check_interval` - Seconds between server connection checks; `null` disables them.
  Default: `300`
- `mpv_idle_quit` - Close mpv when idle to free memory and the GPU; it restarts on the next
  play or when the library is reopened. Never while the library is on screen, and never an mpv
  you started yourself (`mpv_ext` with `mpv_ext_start: false`). Default: `true`
- `mpv_idle_quit_secs` - Idle seconds before that happens. Default: `300`

### Server connection

- `ignore_ssl_cert` - Don't verify the server's TLS certificate. Default: `false`
  - Consider a free Let's Encrypt certificate instead.
- `tls_client_cert` - Client certificate for a server behind mutual TLS. Default: `null`.
  Requires restart.
- `tls_client_key` - Its private key. Default: `null`. Requires restart.
- `tls_server_ca` - CA bundle to verify the server with. Default: `null`. Requires restart.
- `path_substitutions` - With `direct_paths`, rewrite the server's file paths into ones this
  machine can open. Default: `[]`
  - For a server in Docker reporting `/media/...` when you need `Z:\media\...`:
    `[["/media", "Z:\\media"], ["/mnt/media", "\\\\TRUENAS\\Media"]]`
  - `~` and environment variables (`$HOME`, `%USERPROFILE%`) are expanded.

### Playback behaviour

- `media_keys` - Bind mpv to the keyboard's media keys. Default: `true`. Requires restart.
- `media_key_seek` - Media next/previous seek instead of changing episode. Default: `false`
- `use_web_seek` - Use the seek distances set in Jellyfin web for the arrow keys.
  Default: `false`
- `force_set_played` - Mark an item played when mpv reaches the end, for files whose broken
  timestamps stop that happening normally. Default: `false`
- `photo_display_secs` - Slideshow speed for photos, in seconds. Default: `5`
  - Setting `image-display-duration` in `mpv.conf` has no effect; use this.
- `transcode_warning` - Warn the first time something transcodes (classic OSC styles only).
  Default: `true`
- `screenshot_menu` - Offer screenshots in the menu. Default: `true`
- `screenshot_dir` - Where screenshots go. Default: the desktop on Windows, the current
  folder elsewhere.
- `menu_mouse` - Mouse support in the legacy menu. Default: `true`. Requires restart.
- `thumbnail_preferred_size` - Preferred seek-preview width, in pixels. Default: `320`
- `thumbnail_scale` - Enlarge seek previews as they are drawn. Default: `null` (follow the
  interface or display scale). Needs mpv 0.38+.

### Keyboard shortcuts

Set any of these to `null` to disable it, though some features may then be unreachable.

- `kb_stop` - Stop playback. Default: `q`
- `kb_prev` - Previous item. Default: `<`
- `kb_next` - Next item. Default: `>`
- `kb_watched` - Mark watched and skip. Default: `w`
- `kb_unwatched` - Mark unwatched and stop. Default: `u`
- `kb_menu` - Open the player's settings menu (the legacy menu outside the in-window
  controls). Default: `c`
- `kb_menu_esc` - Back out of a menu or page; exits fullscreen when there is nothing to back
  out of. Default: `esc`
- `kb_nav_back` - "Back" only — never exits fullscreen, and does nothing at the library's
  top level. Default: `null` (unbound)
- `kb_menu_ok` - OK in the **legacy OSD menu only**; the library and player controls use
  `ui_select_key`. Default: `enter`
- `kb_menu_left` - Left in the legacy menu; seeks otherwise. Default: `left`
- `kb_menu_right` - Right in the legacy menu; seeks otherwise. Default: `right`
- `kb_menu_up` - Up in the legacy menu; seeks otherwise. Default: `up`
- `kb_menu_down` - Down in the legacy menu; seeks otherwise. Default: `down`
- `kb_pause` - Pause. Default: `space`
- `kb_fullscreen` - Toggle fullscreen. Default: `f`
- `kb_kill_shader` - Unload the shader profile. Default: `k`
- `ui_select_key` - The key that activates whatever is focused in the library or player
  controls. Default: `ENTER`
  - A gamepad's Confirm and a remote's Select send this key too, so they move with it.
  - It replaces ENTER rather than adding to it. `hud_wake_key` keeps its own key.

Seek distances are mpv bindings in `input.conf`; the old `seek_*` settings were moved there for
you — except with `mpv_ext` and `mpv_ext_no_ovr` both on, where your own mpv config is used and the
lines are printed to the log for you to copy instead. See [mpv configuration](#mpv-configuration).

### Shell command triggers

Run a command when playback changes state.

- `play_cmd` - After playback starts.
- `pre_media_cmd` - Before the player appears (waits for it to finish).
- `stop_cmd` - Once when playback ends, for any reason. Does not run if nothing was playing.
- `stop_cmd_on_replace` - Run `stop_cmd` for the old item when something new is started over
  it, rather than when the new one ends. Default: `false`
- `media_ended_cmd` - When the whole queue has played (in addition to `stop_cmd`).
- `idle_cmd` - After `idle_cmd_delay` seconds with nothing happening.
- `idle_ended_cmd` - When the player stops being idle.
- `idle_cmd_delay` - Seconds before `idle_cmd` runs. Default: `60`
- `idle_when_paused` - Count paused as idle. Default: `false`
- `stop_idle` - Stop playback when idle (needs `idle_when_paused`). Default: `false`

### Shader packs

The shader pack bundles mpv settings and shaders (FSRCNNX, Anime4K, …) into profiles you pick
from the player's settings menu. Choosing one overrides shaders set in your `mpv.conf`. Details
of the profiles: [default-shader-pack](https://github.com/iwalton3/default-shader-pack).

- `shader_pack_custom` - Use your own copy of the pack. Default: `false`. Requires restart.
  - On first enable, the built-in pack is copied to `shader_pack/` in the config folder (if it
    does not exist yet), and that copy is used from then on.
- `shader_pack_profile` - The profile to load. Default: `null`
  - Updated when you pick one, with `shader_pack_remember`. If a profile breaks video, start
    with `--reset-shaders` (clears this and `shader_pack_gpu_api`) or press `k`.

### Seek previews (trickplay)

Previews come from the server's trickplay images, falling back to chapter images, and work with
every `osc_style` — including your own OSC if it supports
[thumbfast](https://github.com/po5/thumbfast).

They are fetched a window at a time around where you are seeking, since a whole film's worth
can be hundreds of MB uncompressed; scrubbing far outside the window shows an empty frame
briefly. `trickplay_fast_mode` loads them all instead.

### SVP integration

Set `svp_enable`, then in SVP enable *Settings → Control options → External control via HTTP*.

- `svp_enable` - Enable SVP integration. Default: `false`
- `svp_url` - SVP's web API. Default: `http://127.0.0.1:9901/`
- `svp_socket` - mpv socket for SVP. Default: `mpvpipe` on Windows, `/tmp/mpvsocket`
  elsewhere.

On Windows the bundled mpv cannot run SVP. Install an mpv build with
[VapourSynth](https://github.com/shinchiro/mpv-winbuild-cmake/wiki/Setup-vapoursynth-for-mpv),
then set `mpv_ext` and `mpv_ext_path` to use it.

### External mpv

Drive a separate mpv process instead of the bundled library — for a distribution that ships
mpv only as a program, or an mpv-based GUI player (SMPlayer behaves oddly). This is the only
working backend on macOS, and is rarely practical inside the Flatpak.

- `mpv_ext` - Use an external mpv. Default: `false` (it is used anyway if `libmpv` is missing). Requires restart.
- `mpv_ext_path` - The `mpv` executable. Default: `null` (the one on `PATH`). Requires restart.
- `mpv_ext_ipc` - The control socket. Default: `null` (a random temporary file).
  Requires restart.
  - On Windows this is a pipe name, not a path.
- `mpv_ext_start` - Start mpv with the app. Default: `true`. Requires restart.
  - If off, start mpv yourself first with `--input-ipc-server=<mpv_ext_ipc>`.
- `mpv_ext_start_retries` - Attempts to start mpv. Default: `10`. Requires restart.
- `mpv_ext_start_retry_delay_ms` - Delay between attempts. Default: `3000`. Requires restart.
- `mpv_ext_no_ovr` - Use your own mpv configuration instead of the app's. Default: `false`. Requires restart.
  - Scripts that keep mpv open, among others, may break the app. The app's `input.conf` is not
    read in this mode.

### SyncPlay tuning

You are unlikely to need these.

- `sync_max_delay_speed` - Drift (ms) before adjusting speed to catch up. Default: `50`
- `sync_max_delay_skip` - Drift (ms) before seeking to catch up. Default: `300`
- `sync_method_thresh` - Drift (ms) above which seeking is used instead of speed. Default: `2000`
- `sync_speed_time` - How long (ms) a speed adjustment lasts. Default: `1000`
- `sync_speed_attempts` - Speed adjustments before giving up on them. Default: `3`
- `sync_attempts` - Sync attempts before SyncPlay is disabled. Default: `5`
- `sync_osd_message` - Show SyncPlay status messages on screen. Default: `true`

### Debugging

You may be asked to set these when reporting an issue. Logs are also under **Settings → Logs**.

- `write_logs` - Write logs to the config folder. Default: `false`
- `log_decisions` - Log the full media decisions and playback URLs. Default: `false`
- `mpv_log_level` - mpv's log level: `fatal`, `error`, `warn`, `info`, `v`, `debug`, `trace`,
  or `noise` (`debug` without the app filtering its own per-frame chatter). Default: `info`. Requires restart.
- `sanitize_output` - Keep server tokens out of logs. Default: `true`

### Interface font (environment variable)

`JELLYFIN_MPV_SHIM_UI_FONT` is an environment variable, not a setting. Set it to a font file
and it is tried first for all text the app draws. The automatic choice checks that each
character can be drawn, so this is mostly an appearance preference. Emoji keep working, but the
font is used for every script it covers.

Its main use is a **Traditional Chinese** library on Windows, which otherwise gets Japanese
letterforms because a title's language cannot be detected:

```
JELLYFIN_MPV_SHIM_UI_FONT=C:\Windows\Fonts\msjh.ttc
```

---

## Writing a theme

Themes are JSON files. Built-ins ship in `jellyfin_mpv_shim/themes/`; a file in `themes/` in
your config folder adds one, or replaces the built-in of the same name. **The file name is the
theme id** you put in `theme`. New files appear after a restart.

A theme states only what it changes; everything else comes from the default theme:

```json
{
    "name": "Crimson",
    "palette": { "ACCENT": "cc2222", "ACCENT_HOVER": "e04444" }
}
```

Colours are `"rrggbb"`, `#` optional. Mistakes are logged (Settings → Logs) and skipped, and
the rest of the theme applies.

| Key | Meaning |
| --- | --- |
| `name` | Label in the dropdown. Defaults to the file name. |
| `palette` | Colour table; see below. |
| `browse_bg` | mpv's `background-color` behind the browser, `"#rrggbb"`. |
| `glow` | Accent halo behind bold titles and around the selected card. |
| `rounded` | Rounded cards instead of square. |
| `accent_buttons` | Accent-bordered top bar and settings tabs. |
| `badge_shadow` | Tile badges as a white mark with a drop shadow, instead of a filled pill. |
| `arrow_mode` | `header` (a pair of arrows in the section heading) or `overlay` (round buttons over the artwork). |
| `arrow_bg`, `arrow_alpha` | Fill and opacity (0–255) of the `overlay` buttons. |
| `hud_accent` | Accent over *video* (the seek bar). `null` follows `ACCENT`; set it if your accent is hard to see on a moving picture. |
| `window_gradient` | Vertical background ramp, e.g. `[[0.0, "0f3562"], [0.5, "1162a4"], [1.0, "03215f"]]`, or `null`. |
| `topbar_gradient` | The same, horizontally across the top bar. |
| `poster_scale` | Cover-size multiplier (the `poster_scale` setting overrides it). |
| `heading_size` | Section-title font size. |
| `tile_landscape` | `[width, height]` of landscape tiles. |
| `tile_title_size`, `tile_sub_size` | Tile caption font sizes; `null` for stock. |

Palette colours: `WINDOW_BG`, `CARD_BG`, `PANEL_BG`, `PLACEHOLDER_BG`, `BUTTON_BG`,
`BUTTON_ACTIVE`, `ENTRY_BG`, `BORDER`, `TEXT_FG`, `SUBTLE_FG`, `ACCENT`, `ACCENT_HOVER`,
`ACCENT_SOFT`, `ACCENT_FG`, `FAV_RED`, `OK_GREEN`, `WARN_AMBER`, `PROGRESS_TRACK`,
`WATCHED_GREEN`.

`ACCENT` is the one to change: it is the only accent in the UI (buttons, selection, focus,
progress), so it retints everything coherently. `ACCENT_HOVER` is it lightened, `ACCENT_SOFT`
darkened, and `ACCENT_FG` is the text drawn on it.

**Use as few gradient stops as describe the shape** — each stop is drawn as a separate ramp, so
extra ones show as bands rather than adding smoothness.

## Language Config (Power User)

`language_config` is a list of rules for choosing audio and subtitle tracks automatically. The
[`language_preference`](#subtitles--languages) presets write it for you; choose `custom` to
write your own.

Rules are tried in order, and the first one whose constraints can all be met sets the tracks.
If none matches, the server's defaults apply. A rule sets only what it names —
`{"alang": "jpn"}` picks Japanese audio and leaves subtitles alone — and a match overrides the
track chosen by a casting client.

Constraints (the rule fails if any cannot be met):

- `type` - `"movie"` or `"series"` (episodes).
- `alang` / `slang` - comma-separated audio / subtitle language priority, mpv-style (`"jpn,eng"`).
- `amatch` / `smatch` - regex an audio / subtitle track title must match.
- `subtype` - `"signs"` (the title mentions signs, songs, op/ed or lyrics, or the track is
  forced) or `"full"` (anything else, including plain "English").

Biases, which narrow the choice without failing the rule:

- `aprefer` / `sprefer` - regex preference over track titles, e.g.
  `"aprefer": "^(?!.*commentary)"` to avoid commentary.

Among several subtitles in the chosen language, full dialogue beats signs/songs even without
`subtype`.

Japanese audio with full English subtitles for series, films left alone:

```json
"language_config": [
    {"type": "series", "alang": "jpn", "slang": "eng", "subtype": "full"},
    {"type": "series", "alang": "jpn", "slang": "eng"},
    {"alang": "eng"}
]
```

English audio with signs/songs subtitles, falling back to subbed:

```json
"language_config": [
    {"alang": "eng", "slang": "eng", "subtype": "signs"},
    {"alang": "eng"},
    {"alang": "jpn", "slang": "eng", "subtype": "full"},
    {"alang": "jpn", "slang": "eng"}
]
```

## MPV Configuration

`mpv.conf` and `input.conf` in the config folder (beside `conf.json`) are read like mpv's own,
for anything mpv can do: scaling, subtitle styling, key bindings, an IPC server. Edits take
effect after a restart.

Seek distances live in `input.conf`:

```
up    seek 30
down  seek -30
right seek 10 exact
left  seek -10 exact
```

## Authorization

Logins are kept in the config folder. If one misbehaves — remote control stops appearing,
say — remove that server in **Settings → Servers** and sign in again.

To start over completely, quit the app and delete the whole config folder. That also deletes
your settings and, unless `sync_path` points elsewhere, your downloads and any watched state
not yet sent to the server.

## Removed settings

These are ignored if still in your `conf.json`:

| Old setting | Now |
|---|---|
| `enable_osc` | `osc_style: none` (not migrated) |
| `display_mirroring` | Mirroring is always on; see `display_mirror_summon` |
| `snapped_scrolling`, `force_scroll_snapping` | `scroll_mode: row` (not migrated) |
| `thumbnail_osc_builtin` | `osc_style: custom` (not migrated) |
| `skip_intro_*`, `skip_credits_*` | `segment_intro`, `segment_outro` (migrated) |
| `seek_up`, `seek_down`, `seek_left`, `seek_right`, `seek_*_exact` | `input.conf` (migrated) |
