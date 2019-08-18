# Plex MPV Shim

This project aims to allow casting of content from a Plex server to MPV, with
minimal dependencies to prevent the project from becoming unmaintained.

Currently this software has been tested to work with direct play through
the Plex iOS app. I have fixed support for some features, such as subtitle
selection. I have also ported everything to python3. Most controls work, with
the notable exceptions being skipping between videos.

You can install this project with the following commands:
```bash
git clone https://github.com/iwalton3/plex-mpv-shim
cd plex-mpv-shim
sudo pip3 install --upgrade .
```

After installing the project, you can run it with `plex-mpv-shim`.
If you'd like to run it without installing it, run `./run.py`.

This project is based on https://github.com/wnielson/omplex, which
is available under the terms of the MIT License.

