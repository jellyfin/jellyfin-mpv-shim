# Plex MPV Shim

This project aims to allow casting of content from a Plex server to MPV, with
minimal dependencies to prevent the project from becoming unmaintained.

Currently this software has been tested to work with direct play through
the Plex iOS app. I have fixed support for some features, such as subtitle
selection. I have also ported everything to python3. Most controls work, with
the notable exceptions being skipping between videos.

You'll need to install the following pip3 packages:
 - python-mpv
 - requests

This project is based on https://github.com/wnielson/omplex, which
is available under the terms of the MIT License.

