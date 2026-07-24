# Configuration Reference

Every setting Jellyfin MPV Shim understands. Most of these are editable in the app under
**Settings**, which is usually easier than editing the file by hand — this document is the
complete reference, including the handful of options that have no UI.

See the [README](../README.md) for an introduction to the features these configure.

The configuration file is located in different places depending on your platform. You can also open the
configuration folder using the systray icon if you are using the shim version. When you launch the program
on Linux or macOS from the terminal, the location of the config file will be printed. The locations are:

- Windows - `%appdata%\jellyfin-mpv-shim\conf.json`
- Linux - `~/.config/jellyfin-mpv-shim/conf.json`
- Linux (Flatpak) - `~/.var/app/com.github.iwalton3.jellyfin-mpv-shim/config/jellyfin-mpv-shim/conf.json`
- macOS - `~/Library/Application Support/jellyfin-mpv-shim/conf.json`
- CygWin - `~/.config/jellyfin-mpv-shim/conf.json`

You can specify a custom configuration folder with the `--config` option.

## Transcoding

You can adjust the basic transcoder settings via the menu.

- `always_transcode` - This will tell the client to always transcode. Default: `false`
  - This may be useful if you are using limited hardware that cannot handle advanced codecs.
  - Please note that Jellyfin may still direct play files that meet the transcode profile
      requirements. There is nothing I can do on my end to disable this, but you can reduce
      the bandwidth setting to force a transcode.
- `transcode_hdr` - Force transcode HDR videos to SDR. Default: `false`
- `transcode_dolby_vision` - Force transcode Dolby Vision videos to SDR. Default: `false`
  - MPV plays Dolby Vision natively now, so this is off by default. Existing configs are
    migrated off it once, on the first launch after upgrading; re-enable it if your setup
    still needs the SDR transcode.
  - Dolby Vision tone mapping comes from `vo=gpu-next`, which is no longer experimental and is
    MPV's default video output as of **MPV 0.41**. On MPV 0.40 and older the default is still
    the older `vo=gpu`, which drops the Dolby Vision mapping and renders the base layer — fine
    for profile 8, wrong-looking for profile 5. The Flatpak and Windows builds ship a current
    MPV, so this only affects distro/pip installs using an older system `libmpv`.
  - If that is you, set `vo=gpu-next` in `mpv.conf` (or update MPV). Turning this transcode
    option back on is *not* a good substitute: Jellyfin does not tone map unless the server
    admin enabled it — it is off by default and in practice wants a GPU on the server — so
    without that you get a transcode that still looks wrong.
  - A `vo=gpu` line already in your `mpv.conf` overrides the new default, so remove it if you
    want the newer renderer. The shader packs do not set `vo`, so using them does not opt you out.
- `transcode_hi10p` - Force transcode 10 bit color videos to 8 bit color. Default: `false`
- `transcode_hevc` - Force transcode HEVC videos. Default: `false`
- `transcode_av1` - Force transcode AV1 videos. Default: `false`
- `transcode_4k` - Force transcode videos over 1080p. Default: `false`
- `remote_kbps` - Bandwidth to permit for remote streaming. Default: `10000`
- `local_kbps` - Bandwidth to permit for local streaming. Default: `2147483`
- `direct_paths` - Play media files directly from the SMB or NFS source. Default: `false`
  - `remote_direct_paths` - Apply this even when the server is detected as remote. Default: `false`
  - `path_substitutions` - Rewrite the path reported by Jellyfin before opening it directly. Default: `[]`
    - This is useful when Jellyfin runs in Docker and reports paths like `/media/shows/...` but your playback machine needs a Windows path such as `Z:\\media\\shows\\...` or `\\\\TRUENAS\\Media\\shows\\...`.
    - `~` and environment variables are expanded for substitution entries and resolved direct paths (for example `$HOME`, `${HOME}`, or `%USERPROFILE%`).
    - Format: `[["/media", "Z:\\media"], ["/mnt/media", "\\\\TRUENAS\\Media"]]`
    - Format (with expansion): `[["/media", "%USERPROFILE%\\media"], ["/mnt/media", "$HOME/media"], ["/srv/media", "~/media"]]`
  - Note that `Shared network folder` support was deprecated in Jellyfin 10.9, and is no longer exposed in the Jellyfin UI.
- `allow_transcode_to_h265` - Allow the server to transcode media *to* `hevc`. Default: `false`
  - If you enable this, it'll allow remuxing to HEVC but it'll also break force transcoding of Dolby Vision and HDR content if those settings are used. (See [this bug](https://github.com/jellyfin/jellyfin/issues/9313).)
- `prefer_transcode_to_h265` - Requests the server to transcode media *to* `hevc` as the default. Default: `false`
- `transcode_warning` - Display a warning the first time media transcodes in a session. Default: `true`
- `force_video_codec` - Force a specified video codec to be played. Default: `null`
  - This can be used in tandem with `always_transcode` to force the client to transcode into
      the specified format.
  - This may have the same limitations as `always_transcode`.
  - This will override `transcode_to_h265`, `transcode_h265` and `transcode_hi10p`.
- `force_audio_codec` - Force a specified audio codec to be played. Default: `null`
  - This can be used in tandeom with `always_transcode` to force the client to transcode into
      the specified format.
  - This may have the same limitations as `always_transcode`.

## Audio Output

By default the shim changes nothing about audio and lets MPV (and anything in
your own `mpv.conf`) decide. The settings below are for sending audio to an
external receiver.

- `audio_mode` - How audio is sent to your speakers or receiver. Default: `auto`
  - `auto` - Change nothing. MPV's defaults and your `mpv.conf` apply.
  - `stereo` - Force stereo and normalize the downmix.
  - `optical` - S/PDIF (optical or coaxial). Passes through AC3 and DTS, which
      is all the cable has bandwidth for, and encodes anything else to AC3 so
      you still get surround instead of a stereo downmix.
  - `hdmi` - Passes through every compressed format your receiver accepts.
      Never re-encodes: HDMI carries multichannel PCM natively, so anything
      that isn't passed through is sent uncompressed.
- `audio_passthrough_ac3`, `audio_passthrough_dts`, `audio_passthrough_eac3`,
  `audio_passthrough_dts_hd`, `audio_passthrough_truehd` - Which formats to
  pass through. All default to `true`; untick anything your receiver can't
  decode. Only the ones the selected mode can carry are shown in Settings —
  `optical` offers AC3 and DTS only.
- `audio_optical_encode_ac3` - In `optical` mode, encode audio that can't be
  passed through to AC3. Default: `true`
  - This is the only way surround fits down an optical cable, so leave it on
    unless the encoder causes trouble — it adds latency on some receivers.
  - With it off, those tracks are sent as stereo PCM. S/PDIF can't carry
    multichannel PCM either, so there is no third option. Formats your
    receiver *can* accept directly are unaffected.
- `audio_night_mode` - "Night Mode (Auto Volume Adj)". Evens out loud effects
  and quiet dialogue. Default: `false`
  - Also on the player's settings menu, and applies to what is already playing.
  - Turns passthrough off while enabled. The volume has to be adjusted before
    your receiver gets the audio, which means the audio has to be decoded
    first. In `optical` mode you keep surround (it is re-encoded to AC3); in
    `hdmi` mode you get multichannel PCM.

## Features

You can use the config file to enable and disable features.

- `auto_play` - Automatically play the next item in the queue. Default: `true`
- `fullscreen` - Fullscreen the player when starting playback. Default: `false`
  - The library browser and the player share one window, so playback no longer takes over the screen unless you ask it to.
- `enable_gui` - Enable the system tray icon and GUI features. Default: `true`
- `browser_fullscreen` - Run the in-window library browser fullscreen. Default: `false`
  - Browsing is a desktop activity, so it opens windowed even when `fullscreen` is set. `fullscreen` still applies when playback starts.
  - Toggling fullscreen in the player (`f`, or the on-screen control) is remembered: it writes `browser_fullscreen` while browsing and `fullscreen` while something is playing.
- `close_to_tray` - When enabled, closing the player window minimizes the app to the system tray, keeping it running as a cast target; when disabled, closing exits. Ignored (treated as `false`) when no system tray is available, unless `allow_background` or `headless` is set. Default: `true`
  - In `headless` (cast-target) mode there is no library to come back to and being reachable over the network is the point, so it keeps running with or without a tray. Set this to `false` if you want closing the window to quit.
- `allow_background` - Permit running with no window and no tray icon: the app stays alive as a cast target but is invisible on the desktop. This is what makes `close_to_tray` and `start_minimized` work on machines with no system tray. Off by default because the only ways out are `jellyfin-mpv-shim stop` and killing the process. Default: `false`
  - In the settings form this replaces the "Close to Tray" checkbox when no tray is running, since on those machines it is the same question with different consequences.
- `start_minimized` - Start minimized to the tray instead of opening the library. Ignored when no tray is available unless `allow_background` is set — but passing `--minimized` on the command line is honoured regardless, since that is a decision made for that one launch. Either way, running `jellyfin-mpv-shim` again shows the window. Default: `false`
  - The settings form only offers this once the toggle above it (`close_to_tray`, or `allow_background` where there is no tray) is enabled, since it is asking the app to start in the state that toggle permits. Turning that toggle back off also turns this one off, so it can't keep acting from a checkbox that is no longer on screen; the form says so when it happens.
- `remember_window_size` - Persist the window size across launches. Default: `true`
  - Off means the size is a fixed preference the app always opens at, which is what you want if you deliberately pinned one.
- `display_mirror_summon` - Let casting *open* the window when it is closed to the tray. Default: `false`
  - Mirroring itself is always on; this only controls whether idly browsing on a phone can pop the window open.
- `library_image_cache_mb` - Disk budget for cached library artwork. Default: `256`
- `scroll_wheel_pixels` - Pixels a single wheel notch scrolls in the library browser. Default: `80`
  - The scrollbar glides continuously while the content snaps to the nearest row (or home-screen section), so a trackpad or trackball no longer overshoots whole rows. On an equal-row grid the step is rounded so a whole number of notches spans one row. Raise it to scroll faster, lower it for finer control.
- `snapped_scrolling` - Make each wheel notch jump exactly one row (or one home-screen section) instead of gliding. Default: `false`
  - An accessibility escape hatch that restores the older stepped scrolling.
- `paginated` - Page the library and music tile grids instead of scrolling them. Default: `false`
  - Each page is one screenful (no scrolling within a page), with a bottom bar for First / Previous / Next / Last and a page-number box you can type into. Adjacent pages are prefetched so paging is instant. Global — applies to every tile grid. The songs list and genre grids keep scrolling.
- `enable_osc` - Enable the MPV on-screen controller. Default: `true`
  - It may be useful to disable this if you are using an external player that already provides a user interface.
- `ui_scale` - Scale factor for the in-player UI (tiles, text, chrome). Default: `null`
  - `null` follows the display: mpv's `display-hidpi-scale`, which is `1.0` on
    X11 and the compositor's factor on Wayland/macOS.
  - Set a number (`1.5`, `2.0`) to force it. Handy on a 1x display to see what
    a HiDPI user gets, or to make the UI readable on a TV across the room.
  - Read once at startup; changing it requires a restart.
  - `--scale FACTOR` overrides this for a single run without touching
    the config, e.g. `jellyfin-mpv-shim --scale 1.5`.
  - Artwork is re-fetched from the server at the larger size, so scaling up
    stays sharp. Art from **offline sync** is the exception: it was downloaded
    at 1x and will be upscaled.
- `theme` - Visual theme for the library browser. Default: `default`
  - `default` - The stock look, unchanged from earlier versions.
  - `nebula` - A deep-violet, glowing theme with rounded, cover-cropped cards
    and larger covers.
  - Sets the palette, glow, cover style and default cover size; the Cover Size
    and Interface Scale settings can still override the sizing.
  - Read once at startup; changing it requires a restart.
- `poster_scale` - Overrides the active theme's default cover size. Default: `null`
  - `null` keeps the theme's own size; a number (e.g. `1.0`, `1.4`, `1.7`)
    scales the cover tiles.
  - Read once at startup; changing it requires a restart.
- `osc_style` - Which on-screen controller to use. Default: `mpvtk`
  - `mpvtk` - A player UI styled after jellyfin-web, rendered by the library
    browser inside the player window: top bar (back, title, SyncPlay),
    seek bar with chapter marks, buffered ranges and hover previews,
    transport with seek/chapter steps, track/quality pickers, a settings
    menu (speed, aspect, shader profiles, subtitle style, SyncPlay,
    stats, screenshot), favorites, volume, and Skip Intro/Credits.
    Playback runs clean; mouse motion (or the `hud_wake_key`) summons the
    controls, and a few seconds without input hides them again. Fully
    navigable with a keyboard or a Jellyfin remote. Needs `enable_gui`
    (falls back to `mpv` otherwise). `jellyfin` is accepted as a legacy
    alias.
  - `mpv` - The stock mpv controls, patched with trickplay preview support.
  - `default` - Whatever OSC is built into your mpv (or your own OSC scripts).
    Thumbnail data is still published for thumbfast-aware OSCs like uosc.
- `hud_grab_keys` - Always take over the arrow keys and ENTER for the
  on-screen controls while a video plays. Default: `false` — mpv's own seek
  keys keep working, and only `hud_wake_key` is taken over. With the default,
  controls raised by mouse motion are driven by the pointer alone and the
  arrows still seek; pressing `hud_wake_key` then takes keyboard control of
  the controls already on screen, which reverts as soon as they hide.
  (Jellyfin remotes always drive the controls either way.)
- `hud_wake_key` - The key that summons the on-screen controls for keyboard
  driving while they are hidden, and that takes keyboard control of controls
  already showing (mpv key name syntax). ENTER also toggles pause/play when
  it wakes them. Default: `ENTER`
- `media_key_seek` - Use the media next/prev keys to seek instead of skip episodes. Default: `false`
- `use_web_seek` - Use the seek times set in Jellyfin web for arrow key seek. Default: `false`
- `headless` - Cast-target mode: show the "Ready to cast" screen instead of the library, and make the library unreachable from this machine. Default: `false`
  - Not a security boundary — see [Cast-target mode](../README.md#cast-target-mode-headless).
  - (Replaces `display_mirroring`. Mirroring itself is now always on and needs no setting; a stale `display_mirroring` entry in your config is ignored.)
- `screenshot_menu` - Allow taking screenshots from menu. Default: `true`
- `check_updates` - Check for updates via GitHub. Default: `true`
  - This requests the GitHub releases page and checks for a new version.
  - Update checks are performed when playing media, once per day.
- `notify_updates` - Display update notification when playing media. Default: `true`
  - Notification will only display once until the application is restarted.
- `discord_presence` - Enable Discord rich presence support. Default: `false`
- `menu_mouse` - Enable mouse support in the menu. Default: `true`
  - This requires MPV to be compiled with lua support.

## Downloads and Offline Sync

You can download media to watch without a server connection. Downloads are managed from the
library browser (**Downloads** in the sidebar); these settings control where they go and whether
episodes are fetched for you automatically.

- `sync_path` - Where downloaded media is stored. Default: `null` (a `downloads` folder in the
  config directory)
  - Change this from *Settings → Downloads*, not by hand: moving the store copies the files and
    updates the catalog. Editing the path directly leaves the existing downloads behind.
- `prefer_downloaded` - Play the downloaded copy when one exists, instead of streaming. Default: `true`
- `work_offline` - Browse only downloaded media and don't contact the server. Default: `false`
  - Applied live when toggled, so you don't need to restart.

Automatic downloads keep upcoming episodes on disk without being asked. This is the only feature
that writes to your disk unattended, so it is off by default. It runs on a schedule and only while
nothing is playing.

- `auto_download_enable` - Turn automatic downloads on. Default: `false`
- `auto_download_next_up` - Follow the server's Next Up across every series. Default: `true`
- `auto_download_next_up_limit` - How many Next Up entries to consider. Default: `10`
  - Next Up is as long as your started-series count, which is often 50+ on a real library. The
    server returns it most-recent-first, so a small limit is the shows you are actually watching.
- `auto_download_lookahead` - Episodes to keep ahead of the last one you watched, for the series
  you are working through. `0` disables it. Default: `2`
- `auto_download_max_gb` - Storage budget for automatic downloads. Default: `20`
  - Only applies to automatic downloads. Ones you asked for are never counted against it and are
    never deleted automatically.
- `auto_download_delete_watched` - Delete automatic downloads once watched. Default: `true`
- `auto_download_keep_days` - Delete unwatched automatic downloads after this many days. `0` means
  never expire on age alone. Default: `30`
- `auto_download_interval_mins` - How often to check. Default: `60`

## Client Certificates

For servers behind mutual-TLS. All three are paths, and unset by default.

- `tls_client_cert` - Client certificate to present to the server. Default: `null`
- `tls_client_key` - The matching private key. Default: `null`
- `tls_server_ca` - CA bundle used to verify the server. Default: `null`

## Shell Command Triggers

You can execute shell commands on media state using the config file:

- `media_ended_cmd` - When all media has played.
- `pre_media_cmd` - Before the player displays. (Will wait for finish.)
- `stop_cmd` - After stopping the player.
- `idle_cmd` - After no activity for `idle_cmd_delay` seconds.
- `idle_cmd_delay` - Seconds of inactivity before `idle_cmd` fires. Default: `300`
- `idle_when_paused` - Consider the player idle when paused. Default: `false`
- `stop_idle` - Stop the player when idle. (Requires `idle_when_paused`.) Default: `false`
- `mpv_idle_quit` - Quit MPV when idle to free the window, GPU context, and memory; it is re-created automatically on the next playback request, or when the library is reopened from the tray. It never fires while the library browser is on screen. Not applied to an externally-managed MPV you started yourself (`mpv_ext` with `mpv_ext_start: false`). Default: `true`
- `mpv_idle_quit_secs` - Seconds of inactivity before `mpv_idle_quit` takes effect. Default: `300`
- `play_cmd` - After playback starts.
- `idle_ended_cmd` - After player stops being idle.

## Subtitle Visual Settings

These settings may not works for some subtitle codecs or if subtitles are being burned in
during a transcode. You can configure custom styled subtitle settings through the MPV config file.

- `subtitle_size` - The size of the subtitles, in percent. Default: `100`
- `subtitle_color` - The color of the subtitles, in hex. Default: `#FFFFFFFF`
- `subtitle_position` - The position (top, bottom, middle). Default: `bottom`

## External MPV

The client supports using an external copy of MPV, including one that is running prior to starting
the client. This may be useful if your distribution only provides MPV as a binary executable (instead
of as a shared library), or to connect to MPV-based GUI players. Please note that SMPlayer exhibits
strange behaviour when controlled in this manner. External MPV is currently the only working backend
for media playback on macOS. Additionally, due to Flatpak sandbox restrictions, external mpv is not
practical to use in most cases for the Flatpak version.

- `mpv_ext` - Enable usage of the external player by default. Default: `false`
  - The external player may still be used by default if `libmpv` is not available.
- `mpv_ext_path` - The path to the `mpv` binary to use. By default it uses the one in the PATH. Default: `null`
  - If you are using Windows, make sure to use two backslashes. Example: `C:\\path\\to\\mpv.exe`
- `mpv_ext_ipc` - The path to the socket to control MPV. Default: `null`
  - If unset, the socket is a randomly selected temp file.
  - On Windows, this is just a name for the socket, not a path like on Linux.
- `mpv_ext_start` - Start a managed copy of MPV with the client. Default: `true`
  - If not specified, the user must start MPV prior to launching the client.
  - MPV must be launched with `--input-ipc-server=[value of mpv_ext_ipc]`.
- `mpv_ext_start_retries` - The number of times to retry starting MPV if it fails to start. Default: `10`
- `mpv_ext_start_retry_delay_ms` - The delay in milliseconds between retries. Default: `3000`
- `mpv_ext_no_ovr` - Disable built-in mpv configuration files and use user defaults.
  - Please note that some scripts and settings, such as ones to keep MPV open, may break
      functionality in MPV Shim.

## Keyboard Shortcuts

You can reconfigure the custom keyboard shortcuts. You can also set them to `null` to disable the shortcut. Please note that disabling keyboard shortcuts may make some features unusable. Additionally, if you remap `q`, using the default shortcut will crash the player.

- `kb_stop` - Stop playback and close MPV. (Default: `q`)
- `kb_prev` - Go to the previous video. (Default: `<`)
- `kb_next` - Go to the next video. (Default: `>`)
- `kb_watched` - Mark the video as watched and skip. (Default: `w`)
- `kb_unwatched` - Mark the video as unwatched and quit. (Default: `u`)
- `kb_menu` - Open the configuration menu. (Default: `c`)
- `kb_menu_esc` - Leave the menu. Exits fullscreen otherwise. (Default: `esc`)
- `kb_menu_ok` - "ok" for menu. (Default: `enter`)
- `kb_menu_left` - "left" for menu. Seeks otherwise. (Default: `left`)
- `kb_menu_right` - "right" for menu. Seeks otherwise. (Default: `right`)
- `kb_menu_up` - "up" for menu. Seeks otherwise. (Default: `up`)
- `kb_menu_down` - "down" for menu. Seeks otherwise. (Default: `down`)
- `kb_pause` - Pause. Also "ok" for menu. (Default: `space`)
- `kb_fullscreen` - Toggle fullscreen. (Default: `f`)
- `kb_debug` - Trigger `pdb` debugger. (Default: `~`)
- `kb_kill_shader` - Disable shader packs. (Default: `k`)
- `seek_up` - Time to seek for "up" key. (Default: `60`)
- `seek_down` - Time to seek for "down" key. (Default: `-60`)
- `seek_right` - Time to seek for "right" key. (Default: `5`)
- `seek_left` - Time to seek for "left" key. (Default: `-5`)
- `media_keys` - Enable binding of MPV to media keys. Default: `true`
- `seek_v_exact` - Use exact seek for up/down keys. Default: `false`
- `seek_h_exact` - Use exact seek for left/right keys. Default: `false`

## Shader Packs

Shader packs allow you to import MPV config and shader presets into MPV Shim and easily switch
between them at runtime through the built-in menu. This enables easy usage and switching of
advanced MPV video playback options, such as video upscaling, while being easy to use.

If you select one of the presets from the shader pack, it will override some MPV configurations
and any shaders manually specified in `mpv.conf`. If you would like to customize the shader pack,
use `shader_pack_custom`.

- `shader_pack_enable` - Enable shader pack. (Default: `true`)
- `shader_pack_custom` - Enable to use a custom shader pack. (Default: `false`)
  - If you enable this, it will copy the default shader pack to the `shader_pack` config folder.
  - This initial copy will only happen if the `shader_pack` folder didn't exist.
  - This shader pack will then be used instead of the built-in one from then on.
- `shader_pack_remember` - Automatically remember the last used shader profile. (Default: `true`)
- `shader_pack_profile` - The default profile to use. (Default: `null`)
  - If you use `shader_pack_remember`, this will be updated when you set a profile through the UI.
  - It is reapplied while the player is being built, so a profile that breaks video breaks every launch. Launching with `--reset-shaders` clears this and `shader_pack_gpu_api` before that happens, then starts normally; pressing `k` does the same for this key only, and needs a window you can see.
- `shader_pack_subtype` - The profile group to use. The default pack contains `lq` and `hq` groups. Use `hq` if you have a fancy graphics card.
- `shader_pack_gpu_api` - Graphics API to force while a profile is loaded: `auto`, `vulkan`, `d3d11` or `opengl`. (Default: `auto`)
  - `auto` leaves MPV's own choice (and anything in your `mpv.conf`) alone. The shader pack's legacy `opengl` request is ignored, because the shaders do not need it and OpenGL can cost you HDR output. The pack's `fbo-format` request is ignored with it — that format name only exists on the OpenGL backend, and MPV's own default asks for the same 16-bit float format on every backend. A profile that names some *other* API is honored, since a profile built around a Direct3D 11 filter cannot run anywhere else.
  - Set this only if video breaks when you load a profile. `opengl` is the most compatible; on Windows, `d3d11` (the MPV default) and `vulkan` are the ones that handle HDR.

## Trickplay Thumbnails

MPV will automatically display thumbnail previews. By default it uses the Trickplay images and falls back to chapter images. Please note that this feature will download and
uncompress all of the chapter images before it becomes available for a video. For a 4 hour movie this
causes disk usage of about 250 MB, but for the average TV episode it is around 40 MB. It also requires
overriding the default MPV OSC, which may conflict with some custom user script. Trickplay is compatible
with any OSC that uses [thumbfast](https://github.com/po5/thumbfast), as I have added a [compatibility layer](https://github.com/jellyfin/jellyfin-mpv-shim/blob/master/jellyfin_mpv_shim/thumbfast.lua).

- `thumbnail_enable` - Enable thumbnail feature. (Default: `true`)
- `thumbnail_osc_builtin` - Legacy alias: disabling this behaves like `osc_style: default` (use your own OSC but leave trickplay enabled). Prefer `osc_style`. (Default: `true`)
- `thumbnail_preferred_size` - The ideal size for thumbnails. (Default: `320`)

## SVP Integration

To enable SVP integration, set `svp_enable` to `true` and enable "External control via HTTP" within SVP
under Settings > Control options. Adjust the `svp_url` and `svp_socket` settings if needed.

- `svp_enable` - Enable SVP integration. (Default: `false`)
- `svp_url` - URL for SVP web API. (Default: `http://127.0.0.1:9901/`)
- `svp_socket` - Custom MPV socket to use for SVP.
  - Default on Windows: `mpvpipe`
  - Default on other platforms: `/tmp/mpvsocket`

Currently on Windows the built-in MPV does not work with SVP. You must download MPV yourself.

- Download the latest MPV build [from here](https://sourceforge.net/projects/mpv-player-windows/files/64bit/).
- Follow the [vapoursynth instructions](https://github.com/shinchiro/mpv-winbuild-cmake/wiki/Setup-vapoursynth-for-mpv).
  - Make sure to use the latest Python, not Python 3.7.
- In the config file, set `mpv_ext` to `true` and `mpv_ext_path` to the path to `mpv.exe`.
  - Make sure to use two backslashes per each backslash in the path.

## SyncPlay

You probably don't need to change these, but they are defined here in case you
need to.

- `sync_max_delay_speed` - Delay in ms before changing video speed to sync playback. Default: `50`
- `sync_max_delay_skip` - Delay in ms before skipping through the video to sync playback. Default: `300`
- `sync_method_thresh` - Delay in ms before switching sync method. Default: `2000`
- `sync_speed_time` - Duration in ms to change playback speed. Default: `1000`
- `sync_speed_attempts` - Number of attempts before speed changes are disabled. Default: `3`
- `sync_attempts` - Number of attempts before disabling sync play. Default: `5`
- `sync_revert_seek` - Attempt to revert seek via MPV OSC. Default: `true`
  - This could break if you use revert-seek markers or scripts that use it.
- `sync_osd_message` - Write syncplay status messages to OSD. Default: `true`

## Debugging

These settings assist with debugging. You will often be asked to configure them when reporting an issue.

- `log_decisions` - Log the full media decisions and playback URLs. Default: `false`
- `mpv_log_level` - Log level to use for mpv. Default: `info`
  - Options: fatal, error, warn, info, v, debug, trace
- `sanitize_output` - Prevent the writing of server auth tokens to logs. Default: `true`
- `write_logs` - Write logs to the config directory for debugging. Default: `false`

## Other Configuration Options

Other miscellaneous configuration options. You probably won't have to change these.

- `player_name` - The name of the player that appears in the cast menu. Initially set from your hostname.
- `client_uuid` - The identifier for the client. Set to a random value on first run.
- `playback_timeout` - Timeout to wait for MPV to start loading video in seconds. Default: `30`
  - If you're hitting this, it means files on your server probably got corrupted or deleted.
  - It could also happen if you try to play an unsupported video format. These are rare.
- `lang` - Allows overriding system locale. (Enter a language code.) Default: `null`
  - MPV Shim should use your OS language by default.
- `ignore_ssl_cert` - Ignore SSL certificates. Default: `false`
  - Please consider getting a certificate from Let's Encrypt instead of using this.
- `connect_retry_mins` - Number of minutes to retry connecting before showing login window. Default: `0`
  - This only applies for when you first launch the program.
- `lang_filter` - Limit track selection to desired languages. Default: `und,eng,jpn,mis,mul,zxx`
  - Note that you need to turn on the options below for this to actually do something.
  - If you remove `und` from the list, it will ignore untagged items.
  - Languages are typically in [ISO 639-2/B](https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes),
      but if you have strange files this may not be the case.
- `lang_filter_sub` - Apply the language filter to subtitle selection. Default: `False`
- `lang_filter_audio` - Apply the language filter to audio selection. Default: `False`
- `remember_audio_track` - Reuse your audio track choice for later episodes of the same show. Default: `true`
- `remember_subtitle_track` - Reuse your subtitle track choice for later episodes. Default: `true`
- `language_preference` - Track-selection preset, set from *Settings → Subtitles & Languages*. Default: `custom`
  - One of `unset`, `dubbed_shows`, `subbed_shows`, `dubbed_all`, `subbed_all`, `custom`.
  - Anything other than `custom` generates `language_config` rules for you; `custom` leaves whatever you wrote there alone. See [Language Config](#language-config-power-user).
- `preferred_language` - The language the presets above are built around. Default: `eng`
- `screenshot_dir` - Sets where screenshots go.
  - Default is the desktop on Windows and unset (current directory) on other platforms.
- `force_set_played` - This forcibly sets items as played when MPV playback finished.
  - If you have files with malformed timestamps that don't get marked as played, enable this.
- `raise_mpv` - Windows only. Disable this if you are fine with MPV sometimes appearing behind other windows when playing.
- `health_check_interval` - The number of seconds between each client health check. Null disables it. Default: `300`

## Skip Intro Support

Intro and credits detection uses Jellyfin's MediaSegments API.

- `skip_intro_always` - Always skip intros, without asking. Default: `false`
- `skip_intro_enable` - Offer to skip intros. With the Jellyfin player UI
  (`osc_style: mpvtk`) this shows a floating "Skip Intro" button during the
  intro (even while the controls are hidden); with other UIs it shows the
  classic seek-to-skip prompt. Default: `true`
- `skip_credits_always` - Always skip credits, without asking. Default: `false`
- `skip_credits_enable` - Offer to skip credits (same behavior as
  `skip_intro_enable`). Default: `true`
- `skip_intro_on_seek` - Seeking forward during an intro/credits window skips
  the whole segment. Applies to keyboard and remote seeks only; scrubbing or
  seeking from the Jellyfin player UI never triggers it (use its Skip button).
  Default: `true`

## Language Config (Power User)

`language_config` is an opt-in list of preference rules for picking audio and subtitle tracks automatically.
Most users should leave it unset and stick with the per-show preferences in the application menu — this is
for people who tire of repeating the same selection on every video and know exactly what they want.

Each rule is a JSON object. Rules are evaluated in order; the first rule whose constraints can all be
satisfied sets the audio and subtitle tracks. If no rule matches, the Jellyfin server defaults apply
(same as if `language_config` were unset). When a rule matches, it overrides any track that was selected
from the casting client — open the in-player menu to override at runtime.

A rule sets only what it specifies: `{"alang": "jpn"}` selects Japanese audio and leaves the subtitle
track to the server default.

Constraints (rule fails to match if any cannot be satisfied):

- `type` - `"movie"` or `"series"` (matches `Episode` items).
- `alang` - mpv-style comma-separated audio language priority list (e.g. `"jpn,eng"`).
- `slang` - same for subtitles.
- `amatch` - regex that must match an audio track's title.
- `smatch` - same for subtitles.
- `subtype` - `"signs"` or `"full"`. Note the asymmetry:
  - `"signs"` requires **positive identification**: the subtitle title must contain `sign`, `song`, `op/ed`,
    or `lyric`, **or** the track must be marked forced. A plain "English" track will not qualify.
  - `"full"` is the **negation**: any subtitle that is not positively identified as signs/songs and is not
    marked forced. Untitled or generically-titled tracks (like "English") count as full.

Biases (narrow the candidate set without rejecting the rule):

- `aprefer` - regex bias over audio track titles, applied after `alang` selects a language.
- `sprefer` - same for subtitles. Useful for avoiding commentary tracks: `"aprefer": "^(?!.*commentary)"`.

When multiple subtitles in the matching language are available, the same dialogue-vs-signs scoring used by
the menu's "subbed" / "dubbed" options breaks the tie — full-dialogue tracks beat signs/songs tracks even
without a `subtype` constraint. So `{"slang": "eng"}` on a release with both `English Dialogue` and
`Signs/Songs` will pick the dialogue track.

For anime with full English subtitles and Japanese audio, while leaving movies untouched:

```json
"language_config": [
    {"type": "series", "alang": "jpn", "slang": "eng", "subtype": "full"},
    {"type": "series", "alang": "jpn", "slang": "eng"},
    {"alang": "eng"}
]
```

The `type: "series"` constraint is what keeps a movie that happens to ship a Japanese dub from being
auto-selected. If you'd rather have Ghibli-style anime films also match, drop the `type` constraint from
the first two rules — at the cost of occasionally picking a Japanese dub on a Western film.

For English audio with signs/songs subtitles, falling back to subbed when no dub exists:

```json
"language_config": [
    {"alang": "eng", "slang": "eng", "subtype": "signs"},
    {"alang": "eng"},
    {"alang": "jpn", "slang": "eng", "subtype": "full"},
    {"alang": "jpn", "slang": "eng"}
]
```

For a movies-only rule that defers to the menu for series:

```json
"language_config": [
    {"type": "movie", "alang": "eng,jpn", "slang": "eng"}
]
```

Anything more specific than this is probably better handled by a custom mpv lua script.

## MPV Configuration

You can configure mpv directly using the `mpv.conf` and `input.conf` files. (It is in the same folder as `conf.json`.)
This may be useful for customizing video upscaling, keyboard shortcuts, or controlling the application
via the mpv IPC server.

## Authorization

The `users.json` file contains your local users and, within each, the server authorization information
(migrated once from the older `cred.json`, which is left in place but no longer updated). If you are
having problems with the client, such as the Now Playing not appearing or want to start over, you can
delete `users.json` (and `cred.json`) and add the servers again.

