"""Tkinter-based local library browser for Jellyfin MPV Shim.

This package runs inside its own process (spawned by ``gui_mgr``) and talks to
the main process over multiprocessing queues. It is browse-only: it builds its
own read-only :class:`~jellyfin_apiclient_python.JellyfinClient` connections for
listing media and fetching artwork, and hands actual playback requests back to
the main process where the real player and clients live.

The data layer (:mod:`.repository`) is deliberately an abstraction so that an
offline / synced-content source can be slotted in later without the views
knowing the difference.
"""
