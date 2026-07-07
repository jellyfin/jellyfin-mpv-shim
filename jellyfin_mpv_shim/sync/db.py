"""SQLite catalog of offline downloads.

Single writer (the main process via :class:`SyncDB`), many readers (the browser
opens the same file read-only). WAL mode lets a reader and the writer coexist
across processes. Read-only handles tolerate a missing file (empty catalog).
"""

import json
import logging
import os
import pathlib
import sqlite3
import threading
import time

log = logging.getLogger("sync.db")

# Columns of the `downloads` row, in order. Kept as a list so upsert/read share
# one source of truth.
COLUMNS = [
    "item_id", "server_id", "server_uuid", "type", "name",
    "series_id", "series_name", "season_id", "parent_index", "index_number",
    "media_source_id", "file_path", "ext", "size_bytes", "downloaded_bytes",
    "status", "runtime_ticks", "item_json", "source_json", "userdata_json",
    "added_at",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    item_id TEXT PRIMARY KEY,
    server_id TEXT,
    server_uuid TEXT,
    type TEXT,
    name TEXT,
    series_id TEXT,
    series_name TEXT,
    season_id TEXT,
    parent_index INTEGER,
    index_number INTEGER,
    media_source_id TEXT,
    file_path TEXT,
    ext TEXT,
    size_bytes INTEGER DEFAULT 0,
    downloaded_bytes INTEGER DEFAULT 0,
    status TEXT,
    runtime_ticks INTEGER,
    item_json TEXT,
    source_json TEXT,
    userdata_json TEXT,
    added_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_downloads_series ON downloads(series_id);
CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
CREATE TABLE IF NOT EXISTS pending_playstate (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_uuid TEXT,
    item_id TEXT,
    position_ticks INTEGER,
    played INTEGER,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS playlists (
    playlist_id TEXT PRIMARY KEY,
    server_id TEXT,
    server_uuid TEXT,
    name TEXT,
    added_at INTEGER
);
-- Membership of a downloaded playlist. `owned` marks the items this playlist
-- download is responsible for pulling down: deleting the playlist removes only
-- those, so an item that was already downloaded another way (owned=0) keeps its
-- original grouping and survives.
CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_id TEXT,
    item_id TEXT,
    sort_index INTEGER,
    owned INTEGER DEFAULT 0,
    PRIMARY KEY (playlist_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_playlist_items_item ON playlist_items(item_id);
"""

STATUS_PENDING = "pending"
STATUS_DOWNLOADING = "downloading"
STATUS_COMPLETE = "complete"
STATUS_ERROR = "error"


class SyncDB:
    def __init__(self, db_path, read_only=False):
        self.path = db_path
        self.read_only = read_only
        self._lock = threading.Lock()
        self._conn = None

        if read_only:
            if not os.path.exists(db_path):
                return  # empty catalog; all reads return nothing
            uri = pathlib.Path(db_path).as_uri() + "?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.executescript(_SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()

        if self._conn is not None:
            self._conn.row_factory = sqlite3.Row

    def close(self):
        with self._lock:
            if self._conn is None:
                return
            if not self.read_only:
                # Fold the WAL back into the main db file on a clean shutdown so
                # a stale -wal/-shm pair can't linger for the next launch.
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    log.debug("WAL checkpoint on close failed", exc_info=True)
            self._conn.close()
            self._conn = None

    # -- writes (main process) --------------------------------------------

    def upsert(self, row: dict):
        values = [row.get(col) for col in COLUMNS]
        placeholders = ",".join("?" for _ in COLUMNS)
        cols = ",".join(COLUMNS)
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO downloads (%s) VALUES (%s)" % (cols, placeholders),
                    values)
                self._conn.commit()
            except sqlite3.Error:
                # Don't leave a half-open transaction holding a write lock on
                # the shared connection; roll back before propagating.
                self._conn.rollback()
                raise

    def update(self, item_id, **fields):
        if not fields:
            return
        assignments = ",".join("%s=?" % k for k in fields)
        params = list(fields.values()) + [item_id]
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    "UPDATE downloads SET %s WHERE item_id=?" % assignments, params)
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def delete(self, item_id):
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute("DELETE FROM downloads WHERE item_id=?", (item_id,))
                # Drop the item from any playlist it belonged to so a deleted
                # file can't leave a dangling membership row behind.
                self._conn.execute("DELETE FROM playlist_items WHERE item_id=?",
                                   (item_id,))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    # -- playlists ---------------------------------------------------------

    def upsert_playlist(self, playlist_id, server_id, server_uuid, name):
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO playlists "
                    "(playlist_id, server_id, server_uuid, name, added_at) "
                    "VALUES (?,?,?,?,?)",
                    (playlist_id, server_id, server_uuid, name, int(time.time())))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def replace_playlist_items(self, playlist_id, entries):
        """Set a playlist's membership to ``entries`` (list of
        ``(item_id, sort_index, owned)``), replacing any prior membership so a
        re-download reflects the current order and removals."""
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    "DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
                self._conn.executemany(
                    "INSERT INTO playlist_items "
                    "(playlist_id, item_id, sort_index, owned) VALUES (?,?,?,?)",
                    [(playlist_id, iid, idx, 1 if owned else 0)
                     for iid, idx, owned in entries])
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def delete_playlist(self, playlist_id):
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute("DELETE FROM playlists WHERE playlist_id=?",
                                   (playlist_id,))
                self._conn.execute(
                    "DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def playlist_owned_ids(self, playlist_id):
        """Item ids this playlist download is responsible for (owned=1)."""
        return {r["item_id"] for r in self._query(
            "SELECT item_id FROM playlist_items WHERE playlist_id=? AND owned=1",
            (playlist_id,))}

    def list_playlists(self):
        """Playlists that still have at least one completely-downloaded item,
        each as a dict with ``playlist_id``/``name``/``server_id``/``server_uuid``."""
        return self._query(
            "SELECT p.playlist_id, p.name, p.server_id, p.server_uuid "
            "FROM playlists p "
            "WHERE EXISTS (SELECT 1 FROM playlist_items pi "
            "              JOIN downloads d ON d.item_id = pi.item_id "
            "              WHERE pi.playlist_id = p.playlist_id AND d.status=?) "
            "ORDER BY p.name", (STATUS_COMPLETE,))

    def playlist_item_rows(self, playlist_id):
        """A playlist's completely-downloaded items as full download rows, in
        playlist order."""
        return self._query(
            "SELECT d.* FROM playlist_items pi "
            "JOIN downloads d ON d.item_id = pi.item_id "
            "WHERE pi.playlist_id=? AND d.status=? "
            "ORDER BY pi.sort_index", (playlist_id, STATUS_COMPLETE))

    def playlist_ownership(self):
        """Map of item_id -> playlist_id for owned items (for grouping the
        Downloads screen). Only one owner per item."""
        return {r["item_id"]: r["playlist_id"] for r in self._query(
            "SELECT item_id, playlist_id FROM playlist_items WHERE owned=1")}

    def upsert_playstate(self, server_uuid, item_id, position_ticks=None,
                         played=None):
        """One pending row per item; position advances (max), played sticks True."""
        with self._lock:
            if self._conn is None:
                return
            try:
                existing = self._conn.execute(
                    "SELECT id, position_ticks, played FROM pending_playstate "
                    "WHERE server_uuid=? AND item_id=?",
                    (server_uuid, item_id)).fetchone()
                if existing:
                    new_pos = existing["position_ticks"]
                    if position_ticks is not None:
                        new_pos = max(new_pos or 0, position_ticks)
                    new_played = existing["played"]
                    if played:
                        new_played = 1
                    self._conn.execute(
                        "UPDATE pending_playstate SET position_ticks=?, played=? "
                        "WHERE id=?", (new_pos, new_played, existing["id"]))
                else:
                    self._conn.execute(
                        "INSERT INTO pending_playstate "
                        "(server_uuid, item_id, position_ticks, played, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (server_uuid, item_id, position_ticks,
                         1 if played else None, int(time.time())))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def update_userdata(self, item_id, played=None, position_ticks=None):
        """Merge offline playback progress into the download row's stored
        userdata_json. This is what "delete watched" reads, so keeping it in
        sync with local playback is what makes watched-based delete correct
        without a server round-trip. Advancing only: played sticks True, the
        position only moves forward — except that a finish clears the resume
        point (server-matching semantics)."""
        with self._lock:
            if self._conn is None:
                return
            row = self._conn.execute(
                "SELECT userdata_json, runtime_ticks FROM downloads "
                "WHERE item_id=?",
                (item_id,)).fetchone()
            if row is None:
                return
            try:
                userdata = json.loads(row["userdata_json"] or "{}")
            except ValueError:
                userdata = {}
            changed = False
            if played:
                if not userdata.get("Played"):
                    userdata["Played"] = True
                    changed = True
                # Mirror the server: completing (or marking) an item watched
                # clears its resume point, so the browser doesn't offer
                # "Resume from <the very end>" of a finished item.
                if userdata.get("PlaybackPositionTicks"):
                    userdata["PlaybackPositionTicks"] = 0
                    changed = True
            elif position_ticks is not None:
                # A near-end position on an already-Played item is the trailing
                # stop report of the finish that just cleared the resume point
                # (close-after-finish re-reports ~the full duration); storing it
                # would resurrect "Resume from <the very end>". The margin
                # mirrors player._finished_at_eof.
                runtime = row["runtime_ticks"] or 0
                near_end = runtime and (
                    position_ticks >= runtime * 0.95
                    or runtime - position_ticks <= 10 * 10_000_000
                )
                if userdata.get("Played") and near_end:
                    pass
                elif position_ticks > (
                        userdata.get("PlaybackPositionTicks") or 0):
                    userdata["PlaybackPositionTicks"] = position_ticks
                    changed = True
            if changed:
                # PlayedPercentage is derived; a stale server-seeded value
                # must not shadow the fresh position (the browser recomputes
                # it from position/runtime when rendering).
                userdata.pop("PlayedPercentage", None)
                try:
                    self._conn.execute(
                        "UPDATE downloads SET userdata_json=? WHERE item_id=?",
                        (json.dumps(userdata), item_id))
                    self._conn.commit()
                except sqlite3.Error:
                    self._conn.rollback()
                    raise

    def clear_playstate(self, ids):
        if not ids:
            return
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.executemany(
                    "DELETE FROM pending_playstate WHERE id=?", [(i,) for i in ids])
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    # -- reads (either process) -------------------------------------------

    def _query(self, sql, params=()):
        if self._conn is None:
            return []
        # Reads share the one connection with the writer thread; take the lock
        # so a read can't interleave with an in-flight write/commit.
        with self._lock:
            try:
                return [dict(r) for r in self._conn.execute(sql, params).fetchall()]
            except sqlite3.Error:
                # A locked/corrupt catalog must not masquerade as an empty one
                # (that reads as "nothing downloaded" and can trigger silent
                # re-downloads) — surface it loudly, but still return [] so
                # callers don't crash.
                log.warning("Catalog query failed: %s", sql, exc_info=True)
                return []

    def get(self, item_id):
        rows = self._query("SELECT * FROM downloads WHERE item_id=?", (item_id,))
        return rows[0] if rows else None

    def list(self, status=None, series_id=None):
        sql = "SELECT * FROM downloads"
        clauses, params = [], []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if series_id is not None:
            clauses.append("series_id=?")
            params.append(series_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if status == STATUS_PENDING:
            # The pending queue must be drained in enqueue order, not catalog
            # order: catalog order (by series/index) can float an item whose
            # client isn't resolvable yet to the front and wedge the whole
            # queue behind it. added_at is the enqueue timestamp; rowid breaks
            # ties (and covers rows written before added_at was populated).
            sql += " ORDER BY added_at, rowid"
        else:
            sql += " ORDER BY series_name, parent_index, index_number, name"
        return self._query(sql, tuple(params))

    def downloaded_item_ids(self):
        return {r["item_id"] for r in
                self._query("SELECT item_id FROM downloads WHERE status=?",
                            (STATUS_COMPLETE,))}

    def downloaded_series_ids(self):
        return {r["series_id"] for r in
                self._query("SELECT DISTINCT series_id FROM downloads "
                            "WHERE status=? AND series_id IS NOT NULL",
                            (STATUS_COMPLETE,))}

    def total_size(self):
        rows = self._query("SELECT COALESCE(SUM(downloaded_bytes),0) AS s FROM downloads")
        return rows[0]["s"] if rows else 0

    def is_complete(self, item_id):
        row = self.get(item_id)
        return bool(row and row["status"] == STATUS_COMPLETE)

    def list_playstate(self):
        return self._query("SELECT * FROM pending_playstate ORDER BY created_at")
