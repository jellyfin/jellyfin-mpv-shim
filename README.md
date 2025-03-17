# Jellyfin MPV Shim

[![Current Release](https://img.shields.io/github/release/jellyfin/jellyfin-mpv-shim.svg)](https://github.com/jellyfin/jellyfin-mpv-shim/releases)
[![PyPI](https://img.shields.io/pypi/v/jellyfin-mpv-shim)](https://pypi.org/project/jellyfin-mpv-shim/)
[![Translation Status](https://translate.jellyfin.org/widgets/jellyfin/-/jellyfin-mpv-shim/svg-badge.svg)](https://translate.jellyfin.org/projects/jellyfin/jellyfin-mpv-shim/)
[![Code Stype](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

Jellyfin MPV Shim is a cross-platform cast client for Jellyfin.
It has support for all your advanced media files without transcoding, as well as tons of
features which set it apart from other multimedia clients:

- Direct play most media using MPV.
- Watch videos with friends using SyncPlay.
- Offers a shim mode which runs in the background.
- The Jellyfin mobile apps can fully control the client.
- Reconfigure subtitles for an entire season at once.
- Supports all of the [MPV keyboard shortcuts](https://github.com/jellyfin/jellyfin-mpv-shim#keyboard-shortcuts).
- Enhance your video with [Shader Packs](https://github.com/jellyfin/jellyfin-mpv-shim#shader-packs) and [SVP Integration](https://github.com/jellyfin/jellyfin-mpv-shim#svp-integration).
- Optionally share your media activity with friends using Discord Rich Presence.
- Most features, as well as MPV itself, [can be extensively configured](https://github.com/jellyfin/jellyfin-mpv-shim#configuration).
- You can configure the player to use an [external MPV player](https://github.com/jellyfin/jellyfin-mpv-shim#external-mpv) of your choice.
- Enable a chromecast-like experience with [Display Mirroring](https://github.com/jellyfin/jellyfin-mpv-shim#display-mirroring).
- You can [trigger commands to run](https://github.com/jellyfin/jellyfin-mpv-shim#shell-command-triggers) when certain events happen.

To learn more, keep reading. This README explains everything, including [configuration](https://github.com/jellyfin/jellyfin-mpv-shim#configuration), [tips & tricks](https://github.com/jellyfin/jellyfin-mpv-shim#tips-and-tricks), and [development information](https://github.com/jellyfin/jellyfin-mpv-shim#development).

## Getting Started

If you are on Windows, simply [download the binary](https://github.com/jellyfin/jellyfin-mpv-shim/releases).
If you are using Linux, you can [install via flathub](https://flathub.org/apps/details/com.github.iwalton3.jellyfin-mpv-shim) or [install via pip](https://github.com/jellyfin/jellyfin-mpv-shim#linux-installation<>). If you are on macOS, see the [macOS Installation](https://github.com/jellyfin/jellyfin-mpv-shim#osx-installation)
section below.

To use the client, simply launch it and log into your Jellyfin server. You’ll need to enter the
URL to your server, for example `http://server_ip:8096` or `https://secure_domain`. Make sure to
include the subdirectory and port number if applicable. You can then cast your media
from another Jellyfin application.

The application runs with a notification icon by default. You can use this to edit the server settings,
view the application log, open the config folder, and open the application menu. Unlike Plex MPV Shim,
authorization tokens for your server are stored on your device, but you are able to cast to the player
regardless of location.

Note: Due to the huge number of questions and issues that have been submitted about URLs, I now tolerate
bare IP addresses and not specifying the port by default. If you want to connect to port 80 instead of
8096, you must add the `:80` to the URL because `:8096` is now the default.

## Limitations

- Music playback and Live TV are not supported.
- The client can’t be shared seamlessly between multiple users on the same server. ([Link to issue.](https://features.jellyfin.org/posts/319/mark-device-as-shared))

### Known Issues

Please also note that the on-screen controller for MPV (if available) cannot change the
audio and subtitle track configurations for transcoded media. It also cannot load external
subtitles. You must either [use the menu](https://github.com/jellyfin/jellyfin-mpv-shim#menu) or the application you casted from.

Please note the following issues with controlling SyncPlay:

- If you attempt to join a SyncPlay group when casting to MPV Shim, it will play the media but it will not activate SyncPlay.
  - You can, however, proceed to activate SyncPlay [using the menu within MPV](https://github.com/jellyfin/jellyfin-mpv-shim#menu).
- If you would like to create a group or join a group for currently playing media, [use menu within MPV](https://github.com/jellyfin/jellyfin-mpv-shim#menu).
- SyncPlay as of 10.7.0 is new and kind of fragile. You may need to rejoin or even restart the client. Please report any issues you find.

Music playback sort-of works, but repeat, shuffle, and gapless playback have not been implemented and
would require major changes to the application to properly support, as it was built for video.

The shader packs feature is sensitive to graphics hardware. It may simply just not work on your computer.
You may be able to use the log files to get some more diagnostic information. If you're really unlucky,
you'll have to disable the feature by pressing `k` to restore basic functionality.
If you find the solution for your case, *please* send me any information you can provide, as every test case helps.

## Advanced Features

### Menu

To open the menu, press **c** on your computer or use the navigation controls
in the mobile/web app.

The menu enables you to:

- Adjust video transcoding quality.
- Change the default transcoder settings.
- Change subtitles or audio, while knowing the track names.
- Change subtitles or audio for an entire series at once.
- Mark the media as unwatched and quit.
- Enable and disable SyncPlay.
- Configure shader packs and SVP profiles.
- Take screenshots.

On your computer, use the mouse or arrow keys, enter, and escape to navigate.
On your phone, use the arrow buttons, ok, back, and home to navigate.

### Shader Packs

Shader packs are a recent feature addition that allows you to easily use advanced video
shaders and video quality settings. These usually require a lot of configuration to use,
but MPV Shim's default shader pack comes with [FSRCNNX](https://github.com/igv/FSRCNN-TensorFlow)
and [Anime4K](https://github.com/bloc97/Anime4K) preconfigured. Try experimenting with video
profiles! It may greatly improve your experience.

Shader Packs are ready to use as of the most recent MPV Shim version. To use, simply
navigate to the **Video Playback Profiles** option and select a profile.

For details on the shader settings, please see [default-shader-pack](https://github.com/iwalton3/default-shader-pack).
If you would like to customize the shader pack, there are details in the configuration section.

### SVP Integration

SVP integration allows you to easily configure SVP support, change profiles, and enable/disable
SVP without having to exit the player. It is not enabled by default, please see the configuration
instructions for instructions on how to enable it.

### Display Mirroring

This feature allows media previews to show on your display before you cast the media,
similar to Chromecast. It is not enabled by default. To enable it, do one of the following:

- Using the systray icon, click "Application Menu". Go to preferences and enable display mirroring.
  - Use the arrow keys, escape, and enter to navigate the menu.
- Cast media to the player and press `c`. Go to preferences and enable display mirroring.
- In the config file (see below), change `display_mirroring` to `true`.

Then restart the application for the change to take effect. To quit the application on Windows with
display mirroring enabled, press Alt+F4.

### Keyboard Shortcuts

This program supports most of the [keyboard shortcuts from MPV](https://mpv.io/manual/stable/#interactive-control). The custom keyboard shortcuts are:

- < > to skip episodes
- q to close player
- w to mark watched and skip
- u to mark unwatched and quit
- c to open the menu
- k disable shader packs

Here are the notable MPV keyboard shortcuts:

- space - Pause/Play
- left/right - Seek by 5 seconds
- up/down - Seek by 1 minute
- s - Take a screenshot
- S - Take a screenshot without subtitles
- f - Toggle fullscreen
- ,/. - Seek by individual frames
- \[/\] - Change video speed by 10%
- {/} - Change video speed by 50%
- backspace - Reset speed
- m - Mute
- d - Enable/disable deinterlace
- Ctrl+Shift+Left/Right - Adjust subtitle delay.

## Configuration

The configuration file is located in different places depending on your platform. You can also open the
configuration folder using the systray icon if you are using the shim version. When you launch the program
on Linux or macOS from the terminal, the location of the config file will be printed. The locations are:

- Windows - `%appdata%\jellyfin-mpv-shim\conf.json`
- Linux - `~/.config/jellyfin-mpv-shim/conf.json`
- Linux (Flatpak) - `~/.var/app/com.github.iwalton3.jellyfin-mpv-shim/config/jellyfin-mpv-shim/conf.json`
- macOS - `~/Library/Application Support/jellyfin-mpv-shim/conf.json`
- CygWin - `~/.config/jellyfin-mpv-shim/conf.json`

You can specify a custom configuration folder with the `--config` option.

### Transcoding

You can adjust the basic transcoder settings via the menu.

- `always_transcode` - This will tell the client to always transcode. Default: `false`
  - This may be useful if you are using limited hardware that cannot handle advanced codecs.
  - Please note that Jellyfin may still direct play files that meet the transcode profile
      requirements. There is nothing I can do on my end to disable this, but you can reduce
      the bandwidth setting to force a transcode.
- `transcode_hdr` - Force transcode HDR videos to SDR. Default: `false`
- `transcode_dolby_vision` - Force transcode Dolby Vision videos to SDR. Default: `true`
  - If your computer can handle it, you can get tone mapping to work for this using `vo=gpu-next`.
  - Note that `vo=gpu-next` is considered experimental by MPV at this time.
- `transcode_hi10p` - Force transcode 10 bit color videos to 8 bit color. Default: `false`
- `transcode_hevc` - Force transcode HEVC videos. Default: `false`
- `transcode_av1` - Force transcode AV1 videos. Default: `false`
- `transcode_4k` - Force transcode videos over 1080p. Default: `false`
- `remote_kbps` - Bandwidth to permit for remote streaming. Default: `10000`
- `local_kbps` - Bandwidth to permit for local streaming. Default: `2147483`
- `direct_paths` - Play media files directly from the SMB or NFS source. Default: `false`
  - `remote_direct_paths` - Apply this even when the server is detected as remote. Default: `false`
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

### Features

You can use the config file to enable and disable features.

- `fullscreen` - Fullscreen the player when starting playback. Default: `true`
- `enable_gui` - Enable the system tray icon and GUI features. Default: `true`
- `enable_osc` - Enable the MPV on-screen controller. Default: `true`
  - It may be useful to disable this if you are using an external player that already provides a user interface.
- `media_key_seek` - Use the media next/prev keys to seek instead of skip episodes. Default: `false`
- `use_web_seek` - Use the seek times set in Jellyfin web for arrow key seek. Default: `false`
- `display_mirroring` - Enable webview-based display mirroring (content preview). Default: `false`
- `screenshot_menu` - Allow taking screenshots from menu. Default: `true`
- `check_updates` - Check for updates via GitHub. Default: `true`
  - This requests the GitHub releases page and checks for a new version.
  - Update checks are performed when playing media, once per day.
- `notify_updates` - Display update notification when playing media. Default: `true`
  - Notification will only display once until the application is restarted.
- `discord_presence` - Enable Discord rich presence support. Default: `false`
- `menu_mouse` - Enable mouse support in the menu. Default: `true`
  - This requires MPV to be compiled with lua support.

### Shell Command Triggers

You can execute shell commands on media state using the config file:

- `media_ended_cmd` - When all media has played.
- `pre_media_cmd` - Before the player displays. (Will wait for finish.)
- `stop_cmd` - After stopping the player.
- `idle_cmd` - After no activity for `idle_cmd_delay` seconds.
- `idle_when_paused` - Consider the player idle when paused. Default: `false`
- `stop_idle` - Stop the player when idle. (Requires `idle_when_paused`.) Default: `false`
- `play_cmd` - After playback starts.
- `idle_ended_cmd` - After player stops being idle.

### Subtitle Visual Settings

These settings may not works for some subtitle codecs or if subtitles are being burned in
during a transcode. You can configure custom styled subtitle settings through the MPV config file.

- `subtitle_size` - The size of the subtitles, in percent. Default: `100`
- `subtitle_color` - The color of the subtitles, in hex. Default: `#FFFFFFFF`
- `subtitle_position` - The position (top, bottom, middle). Default: `bottom`

### External MPV

The client now supports using an external copy of MPV, including one that is running prior to starting
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
- `mpv_ext_no_ovr` - Disable built-in mpv configuration files and use user defaults.
  - Please note that some scripts and settings, such as ones to keep MPV open, may break
      functionality in MPV Shim.

### Keyboard Shortcuts

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

### Shader Packs

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
- `shader_pack_subtype` - The profile group to use. The default pack contains `lq` and `hq` groups. Use `hq` if you have a fancy graphics card.

### Trickplay Thumbnails

MPV will automatically display thumbnail previews. By default it uses the Trickplay images and falls back to chapter images. Please note that this feature will download and
uncompress all of the chapter images before it becomes available for a video. For a 4 hour movie this
causes disk usage of about 250 MB, but for the average TV episode it is around 40 MB. It also requires
overriding the default MPV OSC, which may conflict with some custom user script. Trickplay is compatible
with any OSC that uses [thumbfast](https://github.com/po5/thumbfast), as I have added a [compatibility layer](https://github.com/jellyfin/jellyfin-mpv-shim/blob/master/jellyfin_mpv_shim/thumbfast.lua).

- `thumbnail_enable` - Enable thumbnail feature. (Default: `true`)
- `thumbnail_osc_builtin` - Disable this setting if you want to use your own custom osc but leave trickplay enabled. (Default: `true`)
- `thumbnail_preferred_size` - The ideal size for thumbnails. (Default: `320`)

### SVP Integration

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

### SyncPlay

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

### Debugging

These settings assist with debugging. You will often be asked to configure them when reporting an issue.

- `log_decisions` - Log the full media decisions and playback URLs. Default: `false`
- `mpv_log_level` - Log level to use for mpv. Default: `info`
  - Options: fatal, error, warn, info, v, debug, trace
- `sanitize_output` - Prevent the writing of server auth tokens to logs. Default: `true`
- `write_logs` - Write logs to the config directory for debugging. Default: `false`

### Other Configuration Options

Other miscellaneous configuration options. You probably won't have to change these.

- `player_name` - The name of the player that appears in the cast menu. Initially set from your hostname.
- `client_uuid` - The identifier for the client. Set to a random value on first run.
- `audio_output` - Currently has no effect. Default: `hdmi`
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
- `screenshot_dir` - Sets where screenshots go.
  - Default is the desktop on Windows and unset (current directory) on other platforms.
- `force_set_played` - This forcibly sets items as played when MPV playback finished.
  - If you have files with malformed timestamps that don't get marked as played, enable this.
- `raise_mpv` - Windows only. Disable this if you are fine with MPV sometimes appearing behind other windows when playing.
- `health_check_interval` - The number of seconds between each client health check. Null disables it. Default: `300`

### Skip Intro Support

It works the same ways as it did on MPV Shim for Plex. Now uses the MediaSegments API!

- `skip_intro_always` - Always skip intros, without asking. Default: `false`
- `skip_intro_enable` - Prompt to skip intro via seeking. Default: `true`
- `skip_credits_always` - Always skip credits, without asking. Default: `false`
- `skip_credits_enable` - Prompt to skip credits via seeking. Default: `true`

### MPV Configuration

You can configure mpv directly using the `mpv.conf` and `input.conf` files. (It is in the same folder as `conf.json`.)
This may be useful for customizing video upscaling, keyboard shortcuts, or controlling the application
via the mpv IPC server.

### Authorization

The `cred.json` file contains the authorization information. If you are having problems with the client,
such as the Now Playing not appearing or want to delete a server, you can delete this file and add the
servers again.

## Tips and Tricks

Various tips have been found that allow the media player to support special
functionality, albeit with more configuration required.

### Open on Specific Monitor (#19)

Please note: Edits to the `mpv.conf` will not take effect until you restart the application. You can open the config directory by using the menu option in the system tray icon.

**Option 1**: Select fullscreen output screen through MPV.
Determine which screen you would like MPV to show up on.

- If you are on Windows, right click the desktop and select "Display Settings". Take the monitor number and subtract one.
- If you are on Linux, run `xrandr`. The screen number is the number you want. If there is only one proceed to **Option 2**.

Add the following to your `mpv.conf` in the [config directory](https://github.com/jellyfin/jellyfin-mpv-shim#mpv-configuration), replacing `0` with the number from the previous step:

```
fs=yes
fs-screen=0
```

**Option 2**: (Linux Only) If option 1 does not work, both of your monitors are likely configured as a single "screen".

Run `xrandr`. It should look something like this:

```
Screen 0: minimum 8 x 8, current 3520 x 1080, maximum 16384 x 16384
VGA-0 connected 1920x1080+0+0 (normal left inverted right x axis y axis) 521mm x 293mm
   1920x1080     60.00*+
   1680x1050     59.95
   1440x900      59.89
   1280x1024     75.02    60.02
   1280x960      60.00
   1280x800      59.81
   1280x720      60.00
   1152x864      75.00
   1024x768      75.03    70.07    60.00
   800x600       75.00    72.19    60.32    56.25
   640x480       75.00    59.94
LVDS-0 connected 1600x900+1920+180 (normal left inverted right x axis y axis) 309mm x 174mm
   1600x900      59.98*+
```

If you want MPV to open on VGA-0 for instance, add the following to your `mpv.conf` in the [config directory](https://github.com/jellyfin/jellyfin-mpv-shim#mpv-configuration):

```
fs=yes
geometry=1920x1080+0+0
```

**Option 3**: (Linux Only) If your window manager supports it, you can tell the window manager to always open on a specific screen.

- For OpenBox: https://forums.bunsenlabs.org/viewtopic.php?id=1199
- For i3: https://unix.stackexchange.com/questions/96798/i3wm-start-applications-on-specific-workspaces-when-i3-starts/363848#363848

### Control Volume with Mouse Wheel (#48)

Add the following to `input.conf`:

```
WHEEL_UP add volume 5
WHEEL_DOWN add volume -5
```

### MPRIS Plugin (#54)

Set `mpv_ext` to `true` in the config. Add `script=/path/to/mpris.so` to `mpv.conf`.

### Run Multiple Instances (#45)

You can pass `--config /path/to/folder` to run another copy of the player. Please
note that running multiple copies of the desktop client is currently not supported.

### Audio Passthrough

You can edit `mpv.conf` to support audio passthrough. A [user on Reddit](https://reddit.com/r/jellyfin/comments/fru6xo/new_cross_platform_desktop_client_jellyfin_mpv/fns7vyp) had luck with this config:

```
audio-spdif=ac3,dts,eac3 # (to use the passthrough to receiver over hdmi)
audio-channels=2 # (not sure this is necessary, but i keep it in because it works)
af=scaletempo,lavcac3enc=yes:640:3 # (for aac 5.1 tracks to the receiver)
```

### MPV Crashes with "The sub-scale option must be a floating point number or a ratio"

Run the jellyfin-mpv-shim program with LC_NUMERIC=C.

### Use with gnome-mpv/celluloid (#61)

You can use `gnome-mpv` with MPV Shim, but you must launch `gnome-mpv` separately before MPV Shim. (`gnome-mpv` doesn't support the MPV command options directly.)

Configure MPV Shim with the following options (leave the other ones):

```json
{
    "mpv_ext": true,
    "mpv_ext_ipc": "/tmp/gmpv-socket",
    "mpv_ext_path": null,
    "mpv_ext_start": false,
    "enable_osc": false
}
```

Then within `gnome-mpv`, click the application icon (top left) > Preferences. Configure the following Extra MPV Options:

```
--idle --input-ipc-server=/tmp/gmpv-socket
```

### Heavy Memory Usage

A problem has been identified where MPV can use a ton of RAM after media has been played,
and this RAM is not always freed when the player goes into idle mode. Some users have
found that using external MPV lessens the memory leak. To enable external MPV on Windows:

- [Download a copy of MPV](https://sourceforge.net/projects/mpv-player-windows/files/64bit/)
- Unzip it with 7zip.
- Configure `mpv_ext` to `true`. (See the config section.)
- Configure `mpv_ext_path` to `C:\\replace\\with\\path\\to\\mpv.exe`. (Note usage of two `\\`.)
- Run the program and wait. (You'll probably have to use it for a while.)
- Let me know if the high memory usage is with `mpv.exe` or the shim itself.

On Linux, the process is similar, except that you don't need to set the `mpv_ext_path` variable.
On macOS, external MPV is already the default and is the only supported player mode.

In the long term, I may look into a method of terminating MPV when not in use. This will require
a lot of changes to the software.

### Player Sizing (#91)

MPV by default may force the window size to match the video aspect ratio, instead of allowing
resizing and centering the video accordingly. Add the following to `mpv.conf` to enable resizing
of the window freely, if desired:

```
no-keepaspect-window
```

## Development

If you'd like to run the application without installing it, run `./run.py`.
The project is written entirely in Python 3. There are no closed-source
components in this project. It is fully hackable.

The project is dependent on `python-mpv`, `python-mpv-jsonipc`, and `jellyfin-apiclient-python`. If you are
using Windows and would like mpv to be maximize properly, `pywin32` is also needed. The GUI
component uses `pystray` and `tkinter`, but there is a fallback cli mode. The mirroring dependencies
are `Jinja2` and `pywebview`, along with platform-specific dependencies. (See the installation and building
guides for details on platform-specific dependencies for display mirroring.)

This project is based Plex MPV Shim, which is based on https://github.com/wnielson/omplex, which
is available under the terms of the MIT License. The project was ported to python3, modified to
use mpv as the player, and updated to allow all features of the remote control api for video playback.

The Jellyfin API client comes from [Jellyfin for Kodi](https://github.com/jellyfin/jellyfin-kodi/tree/master/jellyfin_kodi).
The API client was originally forked for this project and is now a [separate package](https://github.com/iwalton3/jellyfin-apiclient-python).

The css file for desktop mirroring is from [jellyfin-chromecast](https://github.com/jellyfin/jellyfin-chromecast/tree/5194d2b9f0120e0eb8c7a81fe546cb9e92fcca2b) and is subject to GPL v2.0.

The shaders included in the shader pack are also available under verious open source licenses,
[which you can read about here](https://github.com/iwalton3/default-shader-pack/blob/master/LICENSE.md).

### Local Dev Installation

If you are on Windows there are additional dependencies. Please see the Windows Build Instructions.

1. Install the dependencies: `pip3 install --upgrade python-mpv jellyfin-apiclient-python pystray Jinja2 pywebview python-mpv-jsonipc pypresence`.
    - If you run `./gen_pkg.sh --install`, it will also fetch these for you.
    - Note: Recent distributions make pip unusable by default. Consider using conda or add a virtualenv to your user's path.
2. Clone this repository: `git clone https://github.com/jellyfin/jellyfin-mpv-shim`
    - You can also download a zip build.
3. `cd` to the repository: `cd jellyfin-mpv-shim`
4. Run prepare script: `./gen_pkg.sh`
    - To do this manually, download the web client, shader pack, and build the language files.
5. Ensure you have a copy of `libmpv` or `mpv` available.
6. Install any platform-specific dependencies from the respective install tutorials.
7. You should now be able to run the program with `./run.py`. Installation is possible with `sudo pip3 install .`.
    - You can also install the package with `./gen_pkg.sh --install`.

### Translation

This project uses gettext for translation. The current template language file is `base.pot` in `jellyfin_mpv_shim/messages/`.

To regenerate `base.pot` and update an existing translation with new strings:

```bash
./regen_pot.sh
```

To compile all `*.po` files to `*.mo`:

```bash
./gen_pkg.sh --skip-build
```

## Linux Installation

You can [install the software from flathub](https://flathub.org/apps/details/com.github.iwalton3.jellyfin-mpv-shim). The pip installation is less integrated but takes up less space if you're not already using flatpak.

If you are on Linux, you can install via pip. You'll need [libmpv](https://github.com/Kagami/mpv.js#get-libmpv) or `mpv` installed.

```bash
sudo pip3 install --upgrade jellyfin-mpv-shim
```

If you would like the GUI and systray features, also install `pystray` and `tkinter`:

```bash
sudo pip3 install pystray
sudo apt install python3-tk
```

If you would like display mirroring support, install the mirroring dependencies:

```bash
sudo apt install python3-jinja2 python3-webview
# -- OR --
sudo pip3 install jellyfin-mpv-shim[mirror]
sudo apt install gir1.2-webkit2-4.0
```

Discord rich presence support:

```bash
sudo pip3 install jellyfin-mpv-shim[discord]
```

You can build mpv from source to get better codec support. Execute the following:

```bash
sudo pip3 install --upgrade python-mpv
sudo apt install autoconf automake libtool libharfbuzz-dev libfreetype6-dev libfontconfig1-dev libx11-dev libxrandr-dev libvdpau-dev libva-dev mesa-common-dev libegl1-mesa-dev yasm libasound2-dev libpulse-dev libuchardet-dev zlib1g-dev libfribidi-dev git libgnutls28-dev libgl1-mesa-dev libsdl2-dev cmake wget python g++ libluajit-5.1-dev
git clone https://github.com/mpv-player/mpv-build.git
cd mpv-build
echo --enable-libmpv-shared > mpv_options
./rebuild -j4
sudo ./install
sudo ldconfig
```

## <h2 id="osx-installation">macOS Installation</h2>
Currently on macOS only the external MPV backend seems to be working. I cannot test on macOS, so please report any issues you find.

To install the CLI version:

1. Install brew. ([Instructions](https://brew.sh/))
2. Install python3 and mpv. `brew install python mpv`
3. Install pipx. `brew install pipx`
4. Set path `pipx ensurepath`
5. Install jellyfin-mpv-shim. `pipx install jellyfin-mpv-shim`
6. Run `jellyfin-mpv-shim`.

If you'd like to install the GUI version, you need a working copy of tkinter.

1. Install TK and mpv. `brew install python-tk mpv`
2. Install python3. `brew install python`
3. Install pipx. `brew install pipx`
4. Set path `pipx ensurepath`
5. Install jellyfin-mpv-shim and pystray. `pipx install 'jellyfin-mpv-shim[gui]'`
6. Run `jellyfin-mpv-shim`.

Display mirroring is not tested on macOS, but may be installable with 'pipx install 'jellyfin-mpv-shim[mirror]'`.

## Building on Windows

There is a prebuilt version for Windows in the releases section. When
following these directions, please take care to ensure both the python
and libmpv libraries are either 64 or 32 bit. (Don't mismatch them.)

If you'd like to build the installer, please install [Inno Setup](https://jrsoftware.org/isinfo.php) to build
the installer. If you'd like to build a 32 bit version, download the 32 bit version of mpv.dll and
copy it into a new folder called mpv32. You may also need to edit the batch file for 32 bit builds to point to the right python executable.

1. Install Git for Windows. Open Git Bash and run `git clone https://github.com/jellyfin/jellyfin-mpv-shim; cd jellyfin-mpv-shim`.
    - You can update the project later with `git pull`.
2. Install [Python3](https://www.python.org/downloads/) with PATH enabled. Install [7zip](https://ninite.com/7zip/).
3. After installing python3, open `cmd` as admin and run `pip install --upgrade .[all] pythonnet pywebview pywin32`.
4. Download [libmpv](https://sourceforge.net/projects/mpv-player-windows/files/libmpv/).
5. Extract the `mpv-2.dll` from the file and move it to the `jellyfin-mpv-shim` folder.
6. Open a regular `cmd` prompt. Navigate to the `jellyfin-mpv-shim` folder.
7. Run `./gen_pkg.sh --skip-build` using the Git for Windows console.
    - This builds the translation files and downloads the shader packs.
8. Run `build-win.bat`.
