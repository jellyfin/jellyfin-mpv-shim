# Plex MPV Shim

This project allows casting of content from a Plex server to MPV, with
minimal dependencies to prevent the project from becoming unmaintained.

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

You can install this project with the following commands:
```bash
git clone https://github.com/iwalton3/plex-mpv-shim
cd plex-mpv-shim
sudo pip3 install --upgrade .
```

After installing the project, you can run it with `plex-mpv-shim`.
If you'd like to run it without installing it, run `./run.py`.

Keyboard Shortcuts:
 - Standard MPV shortcuts.
 - < > to skip episodes
 - q to close player
 - w to mark watched and skip
 - u to mark unwatched and quit

This project is based on https://github.com/wnielson/omplex, which
is available under the terms of the MIT License. The project was ported
to python3, modified to use mpv as the player, and updated to allow all
features of the remote control api for video playback.

