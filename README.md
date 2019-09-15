# Plex MPV Shim

This project allows casting of content from a Plex server to MPV, with
minimal dependencies to prevent the project from becoming unmaintained.
This project is 1/17th the size of Plex Media Player and is all python.

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
 - Configurable transcoding support based on remote server and bitrate.

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
 - media\_ended\_cmd - When all media has played.
 - pre\_media\_cmd - Before the player displays. (Will wait for finish.)
 - stop\_cmd - After stopping the player.
 - idle\_cmd - After no activity for idle\_cmd\_delay seconds.

This project is based on https://github.com/wnielson/omplex, which
is available under the terms of the MIT License. The project was ported
to python3, modified to use mpv as the player, and updated to allow all
features of the remote control api for video playback.

UPDATE: It looks like we have a reversal on the Plex Media Player situation.
That being said, this project has proven to be interesting as a hackable
Plex client, so I’ll probably still continue to add features.
