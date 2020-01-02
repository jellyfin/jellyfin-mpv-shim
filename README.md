# Plex MPV Shim

Plex MPV Shim is a simple and lightweight Plex client. It supports Windows and
Linux and is easy to install. The project offers an experience similar to
Chromecast, but with direct play and subtitle support. The application requires
another app to play content and does not save login information, making it
ideal for applications where security is a concern. This project is
significantly smaller and less complicated than Plex Media Player, and is
written entirely in open-source Python.

The project supports the following:
 - Direct play of HEVC mkv files with subtitles.
 - Switching of subtitles and audio tracks.
 - Casting videos from the iOS mobile app and web app.
 - Seeking within a video using the seek bar and buttons.
 - Play, pause, and stop.
 - Using the built-in MPV controls. (OSD and keyboard shortcuts.)
 - Configuration of mpv via mpv.conf.
 - Connecting to shared servers.
 - Installing the package system-wide.
 - Skipping between videos.
 - Autoplaying the next video. (Can be disabled.)
 - Extra keyboard shortcuts: < > skip, u unwatched/stop, w watched/next
 - Playing multiple videos in a queue.
 - The app doesn't require or save any Plex passwords or tokens.
 - Executing commands before playing, after media end, and when stopped.
 - Configurable transcoding support. (Please see the section below.)
 - The application shows up in Plex dashboard and usage tracking.

You'll need [libmpv1](https://github.com/Kagami/mpv.js/blob/master/README.md#get-libmpv). To install `plex-mpv-shim`, run:
```bash
sudo pip3 install --upgrade plex-mpv-shim
```

The current Debian package for `libmpv1` doesn't support the on-screen controller. If you'd like this, or need codecs that aren't packaged with Debian, you need to build mpv from source. Execute the following:
```bash
sudo apt install autoconf automake libtool libharfbuzz-dev libfreetype6-dev libfontconfig1-dev libx11-dev libxrandr-dev libvdpau-dev libva-dev mesa-common-dev libegl1-mesa-dev yasm libasound2-dev libpulse-dev libuchardet-dev zlib1g-dev libfribidi-dev git libgnutls28-dev libgl1-mesa-dev libsdl2-dev cmake wget python g++ libluajit-5.1-dev
git clone https://github.com/mpv-player/mpv-build.git
cd mpv-build
echo --enable-libmpv-shared > mpv_options
./rebuild -j4
sudo ./install
sudo ldconfig
```

After installing the project, you can run it with `plex-mpv-shim`.
If you'd like to run it without installing it, run `./run.py`.

Keyboard Shortcuts:
 - Standard MPV shortcuts.
 - < > to skip episodes
 - q to close player
 - w to mark watched and skip
 - u to mark unwatched and quit

You can execute shell commands on media state using the config file:
 - `media_ended_cmd` - When all media has played.
 - `pre_media_cmd` - Before the player displays. (Will wait for finish.)
 - `stop_cmd` - After stopping the player.
 - `idle_cmd` - After no activity for `idle_cmd_delay` seconds.

This project is based on https://github.com/wnielson/omplex, which
is available under the terms of the MIT License. The project was ported
to python3, modified to use mpv as the player, and updated to allow all
features of the remote control api for video playback.

## Transcoding Support

Plex-MPV-Shim 1.2 introduces revamped transcoding support. It will automatically ask the server to see if transcoding is suggested, which enables Plex-MPV-Shim to play more of your library on the go. You can configure this or switch to the old local transcode decision system.

- `always_transcode`: This will tell the client to always transcode, without asking. Default: `false`
    - This may be useful if you are using limited hardware that cannot handle advanced codecs.
    - You may have some luck changing `client_profile` in the configuration to a more restrictive one.
- `auto_transcode`: This will ask the server to determine if transcoding is suggested. Default: `true`
    - `transcode_kbps`: Transcode bandwidth to request. Default: `2000`
    - `transcode_res`: Transcode resolution to request. Default: `720p`
- `remote_transcode`: This will check for transcoding using locally available metadata for remote servers only. Default: `true`
    - This will not take effect if `auto_transcode` is enabled.
    - Configuration options from `auto_transcode` are also used.
    - `remote_kbps_thresh`: The threshold to force transcoding. If this is lower than the configured server bandwidth, playback may fail.
- `adaptive_transcode`: Tell the server to adjust the quality while streaming. Default: `false`

Caveats:
 - Controlling Plex-MPV-Shim from the Plex web application only works on a LAN where a Plex Server resides. It does NOT have to be the one you are streaming from. An empty server will work.
 - The only way to configure transcode quality is the config file. There is no native way to configure transcode quality from the Plex remote control interface. I may implement an on-screen menu to adjust this and other settings.

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

## Usage

To use `plex-mpv-shim` you merely need to start the application.

Your firewall will need to allow inbound TCP 3000 (for this application's web server) and inbound UDP 32410, 32412, 32413, 32414 ([Plex's GDM ports](https://support.plex.tv/articles/201543147-what-network-ports-do-i-need-to-allow-through-my-firewall/)).
