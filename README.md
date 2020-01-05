# Plex MPV Shim

Plex MPV Shim is a simple and lightweight Plex client, with support for Windows
and Linux. Think of it as an open source Chromecast for Plex. You can cast almost
anything from Plex and it will Direct Play. Subtitles are fully supported, and
there are tools to manage them like no other Plex client.

## Getting Started

If you are on Windows, simply [download the binary](https://github.com/iwalton3/plex-mpv-shim/releases).
If you are using Linux, please see the [Linux Installation](https://github.com/iwalton3/plex-mpv-shim/blob/master/README.md#linux-installation) section below.

To use the client, simply launch it and cast your media from another Plex application.
The mobile and web applications are supported. You do not have to log in to the client
or set it up in any other way.

If you are using the web application to cast, please note that the client must be running
on a network where there is a Plex server present. It does not have to be the plex server
that the media you are casting resides on. An empty Plex server will work.

## Advanced Features

### Menu

To open the menu, press **c** on your computer or **home** within the Plex mobile apps.

The menu enabled you to:
 - Adjust video transcoding quality.
 - Change the default transcoder settings.
 - Change subtitles or audio, while knowing the track names.
 - Change subtitles or audio for an entire series at once.
 - Mark the media as unwatched and quit.

On your computer, use the arrow keys, enter, and escape to navigate. On your phone, use
the arrow buttons, ok, back, and home to navigate. (The option for remote controls is
shown next to the name of the client when you select it from the cast menu.)

Please also note that the on-screen controller for MPV (if available) cannot change the
audio and subtitle track configurations for transcoded media. It also cannot load external
subtitles. You must either use the menu or the application you casted from.

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

The configuration file is located in different places depending on your platform. When you
launch the program, the location of the config file will be printed. The locations are:
 - Windows - `%appdata%\plex-mpv-shim\conf.json`
 - Linux - `~/.config/plex-mpv-shim/conf.json`
 - Mac OSX - `Library/Application Support/plex-mpv-shim/conf.json`
 - CygWin - `~/.config/plex-mpv-shim/conf.json`

### Transcoding

You can adjust the basic transcoder settings via the menu.

- `always_transcode` - This will tell the client to always transcode, without asking. Default: `false`
    - This may be useful if you are using limited hardware that cannot handle advanced codecs.
    - You may have some luck changing `client_profile` in the configuration to a more restrictive one.
- `auto_transcode` - This will ask the server to determine if transcoding is suggested. Default: `true`
    - `transcode_kbps` - Transcode bandwidth to request. Default: `2000`
- `remote_transcode` - This will check for transcoding using locally available metadata for remote servers only. Default: `true`
    - This will not take effect if `auto_transcode` is enabled.
    - This is a legacy feature. It is not regularly tested.
    - Configuration options from `auto_transcode` are also used.
    - `remote_kbps_thresh` - The threshold to force transcoding. If this is lower than the configured server bandwidth, playback may fail.
- `adaptive_transcode` - Tell the server to adjust the quality while streaming. Default: `false`

### Shell Command Triggers

You can execute shell commands on media state using the config file:

 - `media_ended_cmd` - When all media has played.
 - `pre_media_cmd` - Before the player displays. (Will wait for finish.)
 - `stop_cmd` - After stopping the player.
 - `idle_cmd` - After no activity for `idle_cmd_delay` seconds.

### Other Configuration Options

 - `player_name` - The name of the player that appears in the cast menu. Initially set from your hostname.
 - `http_port` - The TCP port to listen on for Plex to control the player. Default: `3000`
 - `enable_play_queue` - Enable play queue support. Default: `true`
    - If you disable this, the application will queue media based on the series.
    - This is a legacy feature. It is not regularly tested.
 - `client_uuid` - The identifier for the client. Set to a random value on first run.
 - `audio_output` - If set to `hdmi` it disables volume adjustment. Default: `hdmi`
 - `audio_ac3passthrough` - Does not work. Currently only changes transcoder settings. Default: `false`
 - `audio_dtspassthrough` - Does not work. Currently only changes transcoder settings. Default: `false`
 - `allow_http` - Allow insecure Plex server connections. Default: `false`
    - This may be useful if you are using a Plex server offline or not signed in.
 - `client_profile` - The client profile for transcoding. Default: `Plex Home Theater`
    - It may be useful to change this on limited hardware.
    - If you change this, it should be changed to a profile that supports `hls` streaming.

### MPV Configuration

You can configure mpv directly using the `mpv.conf` file. (It is in the same folder as `conf.json`.)
This may be useful for customizing video upscaling, keyboard shortcuts, or controlling the application
via the mpv IPC server.

## Development

If you'd like to run the application without installing it, run `./run.py`.
The project is written entierly in Python 3. There are no closed-source
components in this project. It is fully hackable.

The project is dependent on `python-mpv` and `requests`. There are no other
external dependencies.

If you are using a local firewall, you'll want to allow inbound connections on
TCP 3000 and UDP 32410, 32412, 32413, and 32414. The TCP port is for the web
server the client uses to recieve commands. The UDP ports are for the [GDM
discovery protocol](https://support.plex.tv/articles/201543147-what-network-ports-do-i-need-to-allow-through-my-firewall/).

This project is based on https://github.com/wnielson/omplex, which
is available under the terms of the MIT License. The project was ported
to python3, modified to use mpv as the player, and updated to allow all
features of the remote control api for video playback.

## Linux Installation

If you are on Linux, you can install via pip. You'll need [libmpv1](https://github.com/Kagami/mpv.js/blob/master/README.md#get-libmpv).
```bash
sudo pip3 install --upgrade plex-mpv-shim
```

The current Debian package for `libmpv1` doesn't support the on-screen controller. If you'd like this, or need codecs that aren't packaged with Debian, you need to build mpv from source. Execute the following:
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

## Building on Windows

There is a prebuilt version for Windows in the releases section. When
following these directions, please take care to ensure both the python
and libmpv libraries are either 64 or 32 bit. (Don't mismatch them.)

1. Install [Python3](https://www.python.org/downloads/) with PATH enabled. Install [7zip](https://ninite.com/7zip/).
2. After installing python3, open `cmd` as admin and run `pip install pyinstaller python-mpv requests`.
3. Download [libmpv](https://sourceforge.net/projects/mpv-player-windows/files/libmpv/).
4. Extract the `mpv-1.dll` from the file and move it to the `plex-mpv-shim` folder.
5. Open a regular `cmd` prompt. Navigate to the `plex-mpv-shim` folder.
6. Run `pyinstaller -cF --add-binary "mpv-1.dll;." --icon media.ico run.py`.
