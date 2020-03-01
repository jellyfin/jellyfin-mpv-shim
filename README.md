# Jellyfin MPV Shim

Jellyfin MPV Shim is a simple and lightweight Jellyfin client, with support for Windows
and Linux. Think of it as an open source Chromecast for Jellyfin. You can cast almost
anything from Jellyfin and it will Direct Play. Subtitles are fully supported, and
there are tools to manage them like no other Jellyfin client.

**Note: Jellyfin MPV Shim has no navigation UI. You must cast your media from another application
such as the web or mobile application.**

## Getting Started

If you are on Windows, simply [download the binary](https://github.com/iwalton3/jellyfin-mpv-shim/releases).
If you are using Linux or OSX, please see the [Linux Installation](https://github.com/iwalton3/jellyfin-mpv-shim/blob/master/README.md#linux-installation) or [OSX Installation](https://github.com/iwalton3/jellyfin-mpv-shim/blob/master/README.md#osx-installation)
sections below.

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

## Advanced Features

### Menu

To open the menu, press **c** on your computer. Depending on what app you are
using to control Jellyfin, you may also be able to open the menu using that app.
The web application currently doesn't have the required buttons to do so.

The menu enables you to:
 - Adjust video transcoding quality.
 - Change the default transcoder settings.
 - Change subtitles or audio, while knowing the track names.
 - Change subtitles or audio for an entire series at once.
 - Mark the media as unwatched and quit.

On your computer, use the arrow keys, enter, and escape to navigate. On your phone, use
the arrow buttons, ok, back, and home to navigate.

Please also note that the on-screen controller for MPV (if available) cannot change the
audio and subtitle track configurations for transcoded media. It also cannot load external
subtitles. You must either use the menu or the application you casted from.

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

The configuration file is located in different places depending on your platform. You can open the
configuration folder using the systray icon. When you launch the program on Linux or OSX from the terminal,
the location of the config file will be printed. The locations are:
 - Windows - `%appdata%\jellyfin-mpv-shim\conf.json`
 - Linux - `~/.config/jellyfin-mpv-shim/conf.json`
 - Mac OSX - `Library/Application Support/jellyfin-mpv-shim/conf.json`
 - CygWin - `~/.config/jellyfin-mpv-shim/conf.json`

### Transcoding

You can adjust the basic transcoder settings via the menu.

- `always_transcode` - This will tell the client to always transcode. Default: `false`
    - This may be useful if you are using limited hardware that cannot handle advanced codecs.
- `transcode_h265` - Transcode HEVC videos. Default: `false`
- `transcode_hi10p` - Transcode 10 bit color videos. Default: `false`
- `remote_kbps` - Bandwidth to permit for remote streaming. Default: `10000`
- `local_kbps` - Bandwidth to permit for local streaming. Default: `2147483`
- `direct_paths` - Play media files directly from the SMB or NFS source. Default: `false`
    - `remote_direct_paths` - Apply this even when the server is detected as remote. Default: `false`

### Shell Command Triggers

You can execute shell commands on media state using the config file:

 - `media_ended_cmd` - When all media has played.
 - `pre_media_cmd` - Before the player displays. (Will wait for finish.)
 - `stop_cmd` - After stopping the player.
 - `idle_cmd` - After no activity for `idle_cmd_delay` seconds.

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
for media playback on OSX.

- `mpv_ext` - Enable usage of the external player by default. Default: `false`
    - The external player may still be used by default if `libmpv1` is not available.
- `mpv_ext_path` - The path to the `mpv` binary to use. By default it uses the one in the PATH. Default: `null`
    - If you are using Windows, make sure to use two backslashes. Example: `C:\\path\\to\\mpv.exe`
- `mpv_ext_ipc` - The path to the socket to control MPV. Default: `null`
    - If unset, the socket is a randomly selected temp file.
    - On Windows, this is just a name for the socket, not a path like on Linux.
- `mpv_ext_start` - Start a managed copy of MPV with the client. Default: `true`
    - If not specified, the user must start MPV prior to launching the client.
    - MPV must be launched with `--input-ipc-server=[value of mpv_ext_ipc]`.

### Other Configuration Options

 - `player_name` - The name of the player that appears in the cast menu. Initially set from your hostname.
 - `client_uuid` - The identifier for the client. Set to a random value on first run.
 - `audio_output` - Currently has no effect. Default: `hdmi`
 - `fullscreen` - Fullscreen the player when starting playback. Default: `true`
 - `enable_gui` - Enable the system tray icon and GUI features. Default: `true`
 - `media_key_seek` - Use the media next/prev keys to seek instead of skip episodes. Default: `false`
 - `enable_osc` - Enable the MPV on-screen controller. Default: `true`
    - It may be useful to disable this if you are using an external player that already provides a user interface.
 - `use_web_seek` - Use the seek times set in Jellyfin web for arrow key seek. Default: `false`
 - `display_mirroring` - Enable webview-based display mirroring (content preview). Default: `false`
 - `log_decisions` - Log the full media decisions and playback URLs. Default: `false`
 - `mpv_log_level` - Log level to use for mpv. Default: `info`
    - Options: fatal, error, warn, info, v, debug, trace

### MPV Configuration

You can configure mpv directly using the `mpv.conf` file. (It is in the same folder as `conf.json`.)
This may be useful for customizing video upscaling, keyboard shortcuts, or controlling the application
via the mpv IPC server.

### Authorization

The `cred.json` file contains the authorization information. If you are having problems with the client,
such as the Now Playing not appearing or want to delete a server, you can delete this file and add the
servers again.

## Development

Make sure you pull the submodules using `git submodule init && git submodule update`.

If you'd like to run the application without installing it, run `./run.py`.
The project is written entierly in Python 3. There are no closed-source
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

## Linux Installation

If you are on Linux, you can install via pip. You'll need [libmpv1](https://github.com/Kagami/mpv.js/blob/master/README.md#get-libmpv) or `mpv` installed.
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

## OSX Installation
Currently on OSX only the external MPV backend seems to be working. I cannot test on OSX, so please report any issues you find.

To install the CLI version:

1. Install brew. ([Instructions](https://brew.sh/))
2. Install python3 and mpv. `brew install python mpv`
3. Install jellyfin-mpv-shim. `pip3 install --upgrade jellyfin-mpv-shim`
4. Run `jellyfin-mpv-shim`.

If you'd like to install the GUI version, you need a working copy of tkinter.

1. Install pyenv. ([Instructions](https://medium.com/python-every-day/python-development-on-macos-with-pyenv-2509c694a808))
2. Install TK and mpv. `brew install tcl-tk mpv`
3. Install python3 with TK support. `FLAGS="-I$(brew --prefix tcl-tk)/include" pyenv install 3.8.1`
4. Set this python3 as the default. `pyenv global 3.8.1`
5. Install jellyfin-mpv-shim and pystray. `pip3 install --upgrade jellyfin-mpv-shim[gui]`
6. Run `jellyfin-mpv-shim`.

Display mirroring is not tested on OSX, but may be installable with 'pip3 install --upgrade jellyfin-mpv-shim[mirror]`.

## Building on Windows

There is a prebuilt version for Windows in the releases section. When
following these directions, please take care to ensure both the python
and libmpv libraries are either 64 or 32 bit. (Don't mismatch them.)

1. Install Git for Windows. Open Git Bash and run `git clone https://github.com/iwalton3/jellyfin-mpv-shim; cd jellyfin-mpv-shim; git submodule update --init`.
2. Install [Python3](https://www.python.org/downloads/) with PATH enabled. Install [7zip](https://ninite.com/7zip/).
3. After installing python3, open `cmd` as admin and run `pip install --upgrade pyinstaller python-mpv jellyfin-apiclient-python pywin32 pystray Jinja2 pywebview python-mpv-jsonipc`.
4. Download [libmpv](https://sourceforge.net/projects/mpv-player-windows/files/libmpv/).
5. Extract the `mpv-1.dll` from the file and move it to the `jellyfin-mpv-shim` folder.
6. Open a regular `cmd` prompt. Navigate to the `jellyfin-mpv-shim` folder.
7. Download [WebBrowserInterop.x64.dll](https://github.com/r0x0r/pywebview/blob/master/webview/lib/WebBrowserInterop.x64.dll?raw=true) and [Winforms Webview](https://www.nuget.org/api/v2/package/Microsoft.Toolkit.Forms.UI.Controls.WebView/6.0.0).
8. Rename the `*.nupkg` to a `*.zip` file and extract `lib\net462\Microsoft.Toolkit.Forms.UI.Controls.WebView.dll` to the project root.
7. Run `build-win.bat`.
