"""mpvtk_browser: the Jellyfin library browser rendered with the mpvtk
toolkit, inside the player's own mpv window.

This package is the application (routing, views, data layer, strip
compositing); ``jellyfin_mpv_shim.mpvtk`` is the reusable, app-agnostic
toolkit it builds on (see ``mpvtk/README.md`` for rationale and
``mpvtk/GUIDE.md`` for the developer reference). It replaced an earlier
Tkinter browser package.

The data layer (``repository``, ``thumbnails``) is UI-agnostic and lives
here as the single source of truth; the mpv-window UI shares the player
process, so — unlike the Tk browser — there is no separate
``multiprocessing`` child.
"""
