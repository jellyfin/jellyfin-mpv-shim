"""Offline download / sync support.

The catalog (:class:`.db.SyncDB`) and download manager (:mod:`.manager`) run in
the main process; the library browser opens the same SQLite file read-only for
offline browsing and download indicators.
"""
