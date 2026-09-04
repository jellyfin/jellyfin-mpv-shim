"""Offline download manager (main process).

Owns the catalog DB and a single background download worker. The browser drives
it over IPC (estimate / enqueue / delete) and receives change + progress pushes.
Downloads pull the original file via /Items/{id}/Download.
"""

import errno
import glob
import json
import logging
import math
import os
import sqlite3
import shutil
import threading
import time

import requests

from .. import items_api
from ..utils import same_origin
from ..books import AUDIOBOOK_TYPE, BOOK_TYPE, book_format, is_book
from ..conf import settings
from ..conffile import confdir
from ..constants import APP_NAME
from ..i18n import _
from ..utils import get_profile
from .auto import AutoDownloader
from .db import (SyncDB, STATUS_PENDING, STATUS_DOWNLOADING, STATUS_COMPLETE,
                 STATUS_ERROR, ORIGIN_USER, is_auto)

log = logging.getLogger("sync.manager")

#: Item types this manager knows how to fetch. Books and audiobooks are in
#: for opposite reasons: an AudioBook is an ordinary audio file and needs
#: nothing special, and a Book has no media source at all -- its bytes come
#: from the same /Items/{id}/Download endpoint everything else uses, which
#: is the *only* endpoint that serves it (see ``books.py``).
DOWNLOADABLE = frozenset({"Movie", "Episode", "Video", "Audio",
                          BOOK_TYPE, AUDIOBOOK_TYPE})

#: Containers expanded by listing their children. Only books libraries
#: produce these -- everything else has a typed container (Series, Season,
#: Playlist) with an endpoint of its own.
FOLDER_ITEM_TYPES = frozenset({"Folder", "CollectionFolder", "UserView"})

#: Directory names inside ``<root>/<server_id>/`` that are shared caches
#: rather than per-item download directories. They are keyed by series /
#: season / playlist id under here and are referenced by rows in states the
#: orphan sweep does not walk, so the sweep must not treat them as items.
#:
#: ``playlist`` was missing for the life of the feature, so every start
#: deleted the playlist poster cache that ``_download_playlist_art`` writes
#: (and that ``repository._art_path_uncached`` reads) -- and nothing refetches
#: it short of downloading the playlist again.
RESERVED_STORE_DIRS = frozenset({"series", "season", "playlist"})

#: Characters a Jellyfin item id is made of. Ids are GUIDs, normally
#: dash-stripped hex; the dashed spelling is accepted because both reach a
#: client depending on the endpoint.
_ITEM_ID_CHARS = frozenset("0123456789abcdefABCDEF-")


def _looks_like_item_id(name):
    """Is ``name`` shaped like the item id this app names a directory after?

    The orphan sweep's positive test. Deliberately narrow, and deliberately
    not a `try: uuid.UUID(name)`: what matters is not that a name is a valid
    GUID but that it is one *we* could have written, and a false negative
    costs a stale directory while a false positive deletes somebody's files.
    """
    if not (32 <= len(name) <= 36):
        return False
    return all(c in _ITEM_ID_CHARS for c in name) and any(
        c in "0123456789abcdefABCDEF" for c in name)


#: What `SyncManager._open_catalog` found, and therefore what this launch is
#: allowed to conclude from the disk. The distinction that matters is BEHIND
#: vs TRUSTED: a restored catalog is older than the tree, so anything
#: downloaded since the snapshot has files and no row -- the exact shape the
#: orphan sweep deletes.
CATALOG_TRUSTED = "trusted"   # opened and read cleanly; the sweep may run
CATALOG_BEHIND = "behind"     # restored, or unreadable: reconcile, never sweep
CATALOG_ABSENT = "absent"     # nothing to reconcile against; touch no files

CHUNK = 1 << 20            # 1 MiB
PROGRESS_STEP = 4 << 20    # push progress every ~4 MiB
PLAYSTATE_INTERVAL = 30    # replay offline playstate at least this often (s)

#: Minimum gap between two catalog sweeps, whatever asked for them.
#:
#: **There is deliberately no interval to go with it.** The sweep is not a
#: poll: `apply_userdata_event` applies changes as the server pushes them,
#: so a timer would be asking, over and over, about a period during which
#: anything that happened has already been applied. What a sweep covers is
#: a *stretch with nobody listening*, and that stretch has edges the app
#: can see -- it starts when a server drops and ends when one connects. So
#: sweeps are triggered by those edges (`_note_connected_servers`, plus
#: the first pass after start), never by elapsed time.
#:
#: This floor exists for the one thing that has no edge: a server that
#: flaps, reconnecting every few seconds, would otherwise sweep on each
#: one. A pending sweep is **deferred** by the floor rather than dropped
#: (see `_run`), so no trigger is ever lost -- only delayed.
USERDATA_SWEEP_FLOOR = 300

#: How long after the catalog opens the first sweep waits.
#:
#: Startup is the one moment every part of this app wants the network at
#: once -- logging in, the home screen's rows, artwork for every one of
#: them -- and the sweep is the only one of them nobody is waiting for. A
#: minute is long enough for the first screen to have settled and short
#: enough that "watched on the flight out" is right by the time anyone
#: scrolls to it.
#:
#: It also fixes something the floor used to do by accident. The worker
#: starts in `mpv_shim.main` *before* `login_servers()`, so its first pass
#: ran with no clients registered at all: it swept nothing, and stamped
#: `_last_userdata` on the way past. The server then appeared a second
#: later, re-armed the sweep -- and the floor held it off for five minutes,
#: which is precisely the stretch the sweep exists to cover. So a pass with
#: no client to ask now leaves the trigger up and costs nothing (see
#: `_sweep_if_due`), and this settle is what paces the first real one.
USERDATA_SWEEP_SETTLE = 60

#: Ids per request. They travel in the query string, which servers and
#: proxies cap (the apiclient's own note on get_items says so), and a
#: catalog of a few hundred downloads would otherwise be one 414.
USERDATA_BATCH = 60

#: Seconds between one sweep request and the next. The sweep is background
#: work with nobody waiting on it, and a catalog of a few hundred items is
#: several requests: sending them back to back is a burst at a server that
#: may also be streaming to this client. Spread them instead -- 500 items
#: is 9 requests over ~24s rather than 9 at once. Waited on `_wake`, which
#: `stop()` sets, so shutdown does not sit out the delay.
USERDATA_BATCH_PAUSE = 3

#: A UserDataChanged carrying more entries than this is applied by marking
#: the sweep due rather than one row at a time. The ordinary message is two
#: entries (the item and its parent); something that moved hundreds at once
#: is a bulk mark or a plugin, and walking it on the websocket thread would
#: hold up every other event behind it.
USERDATA_EVENT_MAX = 200
STOP_JOIN_TIMEOUT = 10     # how long stop() waits for the worker to unwind (s)


class _Stopped(Exception):
    """Raised inside the worker when the app is shutting down mid-download."""


class _Cancelled(Exception):
    """Raised inside the worker when the active download is being deleted."""


class ExpandFailed(Exception):
    """The server could not be asked what is inside a container.

    Deliberately distinct from an empty answer. "This playlist holds nothing
    downloadable" is a fact about the playlist and a reason to drop its
    record; "the request failed" is a fact about the network and must never
    be read that way. Both were the bare value ``[]`` until this existed,
    which is how a 500 on the ordinary top-up gesture deleted a downloaded
    playlist and its ownership rows while the dialog reported success.

    Public because ``AutoDownloader.fill`` catches it by name: one unlistable
    item has to be skipped rather than end the pass.
    """


#: Re-exported under its old private name so the call site below and
#: tests/test_sync_auth_headers.py keep reading as they did. The player needs
#: the same test, and a second implementation of "is this our server" is
#: exactly the kind of duplicate this codebase gets wrong once and then twice.
_same_origin = same_origin


def _disposition_ext(headers):
    """Extension from a response's ``Content-Disposition`` filename, or None.

    Jellyfin sends both spellings -- ``filename="A Book.epub"`` and the
    RFC 5987 ``filename*=UTF-8\'\'A%20Book.epub`` -- and the plain one is
    read here because the *extension* is all that is wanted and it is ASCII
    in every format that exists. Parsed with the stdlib's own message
    machinery rather than by splitting on semicolons: a filename may contain
    one, quoted.

    Returns a bare lowercase extension (``"epub"``), never a leading dot,
    and never a path: a header is server-controlled input, and a filename
    like ``"../../x.epub"`` must not be able to steer where anything is
    written. Only the last suffix survives, which cannot contain a
    separator.
    """
    from email.message import Message

    raw = (headers or {}).get("Content-Disposition") or ""
    if not raw:
        return None
    msg = Message()
    msg["Content-Disposition"] = raw
    name = msg.get_filename() or ""
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    # Belt and braces on top of splitext: an extension is alphanumeric in
    # every format the resolver accepts, so anything else is not one.
    return ext if ext and ext.isalnum() else None


def _sub_format(codec):
    """Map a subtitle codec to the format extension the server should serve."""
    c = (codec or "").lower()
    if c in ("ass", "ssa"):
        return "ass"
    if c in ("vtt", "webvtt"):
        return "vtt"
    if c in ("sub", "subviewer", "microdvd"):
        return "sub"
    return "srt"  # subrip and unknowns -> srt


class SyncManager:
    def __init__(self):
        self.db = None
        self.root = None
        self.get_client = lambda server_uuid: None
        self.on_change = lambda: None
        self.on_progress = lambda item_id, name, downloaded, total: None
        # Built in start(); None until then so the worker loop (which tests
        # drive directly, without start()) stays safe.
        self.auto = None

        #: series/item id -> the CollectionFolder it lives in, for
        #: _library_id_for. Positive answers only; see there.
        self._library_ids = {}

        self._worker = None
        self._wake = threading.Event()
        self._stop = False
        # Bumped for every worker started. stop() joins with a timeout and
        # gives up if the worker is still busy, but leaves it running; the
        # next _open_and_run then sets _stop back to False, which re-armed
        # that abandoned thread's loop. Two workers against one catalog
        # interleave appends into the same .part file. A worker also checks
        # that it is still the current generation, so an abandoned one
        # exits no matter what _stop is set to afterwards.
        self._generation = 0
        # Coordinates the worker with deletes of the item it is actively
        # downloading: the worker owns cleanup so files/rows can't be yanked
        # out from under an in-flight write.
        self._active_lock = threading.Lock()
        self._active_item = None
        self._cancelled = set()
        # Set while relocate() is moving the store (worker stopped, catalog
        # closed). enqueue/delete short-circuit so nothing writes to a catalog
        # that is mid-move.
        self._relocating = False
        self._last_playstate = 0.0
        #: When the last catalog sweep ran, for USERDATA_SWEEP_FLOOR. Unlike
        #: _last_playstate this does not schedule anything: nothing is due
        #: because time passed, only because something asked.
        self._last_userdata = 0.0
        #: Set when something has happened that the websocket could not have
        #: told us about. True at construction because starting up is one of
        #: those things -- the app was not listening a moment ago.
        self._sweep_due = True
        #: When the catalog was opened, for USERDATA_SWEEP_SETTLE. Zero
        #: until then, which reads as "no settle to serve" -- a manager
        #: driven directly (every test) is not a client starting up.
        self._started_at = 0.0
        #: Server uuids that had a client last time the worker looked. The
        #: transition *into* this set is the reconnect signal; see
        #: _note_connected_servers for why it is watched here rather than
        #: subscribed to.
        self._connected_servers = set()
        #: All connected clients, for the above. Set in start(); the default
        #: keeps a directly-driven manager (every test) safe.
        self.get_clients = lambda: {}
        # item_id -> (last downloaded size, consecutive no-progress short reads).
        # A short read normally leaves the row pending to resume; but a server
        # that cleanly truncates at the same offset every time would resume
        # from the same size forever. In-memory (a restart is a fair fresh
        # attempt), escalated to STATUS_ERROR after a few stalls.
        self._short_read_stalls = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self, get_client, get_clients=None, is_busy=None):
        """get_clients (() -> {uuid: client}) and is_busy (() -> bool) power
        auto-download; both optional so existing callers and the tests keep
        working, in which case auto-download simply finds no servers."""
        self.get_client = get_client
        if get_clients is not None:
            self.get_clients = get_clients
        self.auto = AutoDownloader(self, get_clients=get_clients,
                                   is_busy=is_busy,
                                   should_stop=lambda: self._stop)
        self.root = settings.sync_path or os.path.join(confdir(APP_NAME), "offline")
        self._open_and_run()

    def _open_and_run(self):
        """Open the catalog at self.root and (re)start the download worker.

        Shared by start() and relocate() so re-pointing at a new folder goes
        through exactly the same recover/reconcile path as a fresh launch.
        """
        os.makedirs(self.root, exist_ok=True)
        # Stamped before anything slow: the settle is measured from the app
        # opening its catalog, not from the end of a disk reconcile.
        self._started_at = time.monotonic()
        catalog_path = os.path.join(self.root, "catalog.db")
        catalog = self._open_catalog(catalog_path)
        # Recover rows interrupted mid-download on a previous run.
        for row in self.db.list(status=STATUS_DOWNLOADING):
            self.db.update(row["item_id"], status=STATUS_PENDING)
        # Reconcile the catalog with what is actually on disk (best-effort) --
        # but NEVER against a catalog that did not exist a moment ago.
        #
        # An empty catalog says "nothing on disk is known", and the sweep
        # believes it: every media directory becomes an orphan and is deleted.
        # That is not hypothetical. A relocation that moved `catalog.db` across
        # and then failed on the media reopened here, at the old root, with the
        # catalog gone -- and the sweep finished the job the failed move had
        # started, while the user was being told their downloads were left in
        # place. A first run has no catalog and no media either, so skipping
        # costs nothing; anything else is media we cannot prove is orphaned.
        if catalog is not CATALOG_ABSENT:
            try:
                # The rows a restore is missing come back on the next
                # sweep-eligible launch; the files must survive until then.
                self._reconcile_disk(sweep_orphans=catalog is CATALOG_TRUSTED)
            except Exception:
                log.debug("Startup disk reconcile failed.", exc_info=True)
        elif any(os.path.isdir(os.path.join(self.root, n))
                 for n in (os.listdir(self.root) if os.path.isdir(self.root)
                           else [])):
            log.warning("Opened a new catalog at %s next to existing media; "
                        "skipping the orphan sweep so nothing is removed on "
                        "the strength of an empty catalog.", self.root)
        # Last, so the snapshot is of a catalog that opened, migrated and
        # reconciled cleanly -- backing up before that would happily preserve
        # a catalog we are about to find unreadable.
        self._backup_catalog(catalog_path)
        self._stop = False
        self._generation += 1
        self._worker = threading.Thread(target=self._run,
                                        args=(self._generation,), daemon=True)
        self._worker.start()

    #: Kept beside the catalog. See `_open_catalog`.
    CATALOG_BACKUP = "catalog.db.bak"

    @staticmethod
    def _catalog_reads(catalog_path):
        """Whether the file already at `catalog_path` is a catalog that reads.

        Asked **read-only, and before anything opens it writable**, because a
        writable open is not a read: `SyncDB.__init__` runs
        `executescript(_SCHEMA)`. On a zero-byte file -- a truncated write, a
        copy that never finished, a restore killed between create and populate
        -- that *creates* the tables, and `healthy()` then says yes of a
        catalog with nothing in it. The store ends up described by nothing,
        with no later launch finding anything wrong: the exact end state the
        rest of this path exists to prevent.

        `SyncDB(read_only=True).healthy()` rather than a query of its own, so
        "can the rows be read" keeps one implementation. Read-only opening
        never creates and never migrates, so asking is free of side effects.
        """
        try:
            probe = SyncDB(catalog_path, read_only=True)
        except sqlite3.Error:
            return False
        try:
            return probe.healthy()
        finally:
            probe.close()

    @staticmethod
    def _open_writable(catalog_path):
        """Open the catalog for use, or None if it will not open.

        Separate from `_catalog_reads` because they can disagree: the probe
        reads `downloads`, while the constructor's schema and migration touch
        every table. Left to propagate, a failure here escapes `_open_catalog`
        entirely -- so the caller never gets a verdict, and the backup sitting
        beside the damaged file is never restored.
        """
        try:
            return SyncDB(catalog_path)
        except sqlite3.Error:
            log.warning("The download catalog at %s could not be opened.",
                        catalog_path, exc_info=True)
            return None

    def _open_catalog(self, catalog_path):
        """Open the catalog, restoring the backup if it is unreadable or gone.

        Returns what the caller may do with the disk -- `CATALOG_TRUSTED`,
        `CATALOG_BEHIND` or `CATALOG_ABSENT`. That is a return value rather
        than something the caller works out for itself because it used to
        ask `os.path.exists(catalog_path)` *before* this ran, which stopped
        being the same question the moment a missing catalog could be
        restored: a successful restore was reported as a first run, logged
        as "opened a new catalog", and skipped the reconcile entirely.

        A corrupt catalog is not merely a lost index. The download tree is
        `<root>/<server_id>/<item_id>/media.<ext>` -- ids, not names -- so
        without the catalog the UI cannot list what is there, cannot play it
        and cannot delete it. The files stop being an offline library and
        become unlabelled dead weight that only a file manager can clear.
        That is the loss this guards, and it is why the corrupt file is set
        aside rather than overwritten: it is still the better copy of
        anything the backup predates, and recovering it by hand
        (`.recover` in the sqlite shell) is possible right up until we
        delete it.

        Locking is not the failure mode being covered. Single-instance
        election means one writer, and the browser's handle is read-only.
        This is for the file itself: a power cut mid-write, a bad sector, a
        network or removable filesystem that lied about a flush.
        """
        backup_path = os.path.join(os.path.dirname(catalog_path),
                                   self.CATALOG_BACKUP)
        # A catalog that is GONE is the same emergency as one that cannot be
        # read: `SyncDB` would create an empty one, `healthy()` would say yes,
        # and the store would be left described by nothing. It is reachable --
        # a killed process partway through `_move_tree`, a restore that
        # failed, a filesystem hiccup, a user tidying up.
        missing = not os.path.exists(catalog_path)
        if missing and not os.path.exists(backup_path):
            self.db = SyncDB(catalog_path)      # a genuine first run
            return CATALOG_ABSENT
        if missing:
            log.warning("The download catalog at %s is missing; restoring the "
                        "backup.", catalog_path)
        else:
            self.db = (self._open_writable(catalog_path)
                       if self._catalog_reads(catalog_path) else None)
            if self.db is not None and self.db.healthy():
                return CATALOG_TRUSTED
            if self.db is None:
                # Nothing opened, so there is no handle to answer reads with
                # and none to close below. Read-only because it is the one
                # open that cannot create or migrate: whatever is at that
                # path stays exactly as damaged as it was, and `healthy()`
                # keeps answering false, which is what holds the sweep off
                # the disk.
                self.db = SyncDB(catalog_path, read_only=True)
            if not os.path.exists(backup_path):
                log.error("The download catalog at %s cannot be read and there "
                          "is no backup to restore. Downloads are left "
                          "untouched.", catalog_path)
                return CATALOG_BEHIND
            log.warning("The download catalog at %s cannot be read; restoring "
                        "the backup.", catalog_path)
            try:
                self.db.close()
            except Exception:
                log.debug("Closing the unreadable catalog failed.",
                          exc_info=True)
        ok, aside = self._restore_from_backup(catalog_path, backup_path)
        if not ok:
            # **Never a writable empty catalog where the evidence used to be.**
            # An empty catalog is *readable*, so the next launch sees nothing
            # wrong with it, never retries the restore, and the store stays
            # undescribed for good. Opening read-only cannot create one: with
            # no file it holds no connection at all, every read answers empty
            # and every write is a no-op, so this launch runs describing
            # nothing rather than recording that nothing is there.
            self.db = SyncDB(catalog_path, read_only=True)
            return CATALOG_ABSENT if missing else CATALOG_BEHIND
        self.db = self._open_writable(catalog_path)
        if self.db is None or not self.db.healthy():
            log.error("The restored catalog is unreadable too.")
            if self.db is None:
                self.db = SyncDB(catalog_path, read_only=True)
            return CATALOG_BEHIND
        log.warning("Restored the download catalog from %s.%s Downloads "
                    "finished since the backup was taken are still on disk "
                    "and will be re-listed on the next start.", backup_path,
                    (" The unreadable file is kept at %s." % aside)
                    if aside else "")
        return CATALOG_BEHIND

    def _restore_from_backup(self, catalog_path, backup_path):
        """Put the backup where the catalog belongs. Returns (ok, aside).

        One path for both emergencies -- a catalog that cannot be read, and
        one that is not there -- because they differ in exactly one step
        (there is no bad file to set aside when it is missing) and need every
        other step alike. Written as two branches, the missing one got the
        copy and neither the staging nor the sidecars: a full disk made the
        loss permanent, and a stale `-wal` replayed the very pages the
        restore was recovering from.

        The order is load-bearing:

        1. Stage the copy, so a failure here has touched nothing.
        2. Move any existing catalog aside. It is still the better copy of
           anything the backup predates, and `.recover` in the sqlite shell
           can read it right up until we delete it.
        3. Move the `-wal`/`-shm` off the live name -- **whether or not there
           was a catalog to set aside**, because a catalog that has gone
           missing can still have its WAL sitting beside it, and that is
           exactly the state a crash between steps 2 and 4 leaves behind.
           Moved rather than deleted when there is an aside to keep them
           with: a WAL can hold pages newer than the file it belongs to, so
           it is part of what a hand recovery has to work from. Failing to
           shift one aborts the restore -- this is the one step here whose
           success step 4 assumes, rather than merely prefers.
        4. Promote the staged copy.

        A failure anywhere leaves the catalog **absent**, which is the state
        the caller must not paper over: see `_open_catalog`, which opens
        read-only rather than letting sqlite create the empty, readable,
        writable catalog that no later launch would find anything wrong with.
        Putting the aside back here as well was tried and dropped -- it made
        no outcome differ, and a second mechanism for one rule is how the two
        drift apart.
        """
        staged = catalog_path + ".restoring"
        aside = None
        try:
            shutil.copyfile(backup_path, staged)
            if os.path.exists(catalog_path):
                aside = "%s.corrupt-%d" % (catalog_path, int(time.time()))
                os.replace(catalog_path, aside)
            for suffix in ("-wal", "-shm"):
                try:
                    if aside is not None:
                        os.replace(catalog_path + suffix, aside + suffix)
                    else:
                        os.remove(catalog_path + suffix)
                except OSError as exc:
                    # ENOENT is the ordinary case -- usually there is no
                    # sidecar at all -- and the only failure that still
                    # leaves nothing beside the name. Every other one means
                    # a sidecar is *still there*, and the promote below
                    # happens on the strength of this having worked: sqlite
                    # applies a WAL to whatever takes that name next,
                    # checking only that the WAL is internally consistent
                    # and never that it belongs to the file it is being
                    # replayed into. Swallowed, that silently discarded four
                    # rows in five and read back integrity-clean.
                    if exc.errno != errno.ENOENT:
                        raise
            os.replace(staged, catalog_path)
        except OSError:
            log.error("Could not restore the catalog backup; the catalog is "
                      "left absent so the next start tries again.",
                      exc_info=True)
            try:
                os.remove(staged)
            except OSError:
                pass
            return False, aside
        return True, aside

    def _backup_catalog(self, catalog_path):
        """Snapshot the catalog, unless doing so would destroy a better one.

        **An empty catalog never replaces a backup that has rows in it.**
        Without that, the backup deletes itself on exactly the launch it
        exists for: any path that ends with an empty catalog open -- a
        restore whose copy failed on a full disk, or the missing-catalog case
        above failing too -- reached here, `healthy()` said yes of a catalog
        with nothing in it, and the one artifact that could still describe
        the store was overwritten by it.

        The cost is a stale backup after somebody deletes every download: it
        keeps rows for files that are gone, and restoring it would re-queue
        them. That is a bounded annoyance, and it clears on the next launch
        or relocate that opens a catalog with rows in it -- this has one call
        site, `_open_and_run`, so a download alone does not do it. Losing the
        only index of a full download folder does not clear at all.
        """
        if self.db is None or not self.db.healthy():
            return
        backup_path = os.path.join(os.path.dirname(catalog_path),
                                   self.CATALOG_BACKUP)
        if not self.db.list() and os.path.exists(backup_path):
            log.warning("Not backing up an empty catalog over %s.",
                        backup_path)
            return
        self.db.backup(backup_path)

    def relocate(self, new_path, progress=None):
        """Move the download tree to new_path and re-point the manager at it.

        progress, if given, is called as progress(copied_bytes, total_bytes)
        during the move (throttled) so a slow cross-drive copy can show a bar
        instead of freezing the UI. Run this off any UI/event-loop thread.

        Returns (ok, message): message is a user-facing string to surface when
        ok is False (or empty on success). Refuses while a download is actively
        transferring, so nothing is moved out from under an open write. On any
        move failure the downloads are left untouched at the old location and
        the manager resumes there.
        """
        old_root = self.root
        if new_path:
            new_root = os.path.abspath(os.path.expanduser(new_path))
        else:
            new_root = os.path.join(confdir(APP_NAME), "offline")
        if old_root and os.path.abspath(old_root) == new_root:
            return True, ""
        with self._active_lock:
            if self._active_item is not None:
                return False, _("Can't change the download folder while a "
                                "download is in progress. Wait for it to finish, "
                                "then try again.")
        # Containment, not just equality. An empty folder *inside* the current
        # download folder passes both the equality check and the non-empty
        # check, and then `_copy_tree` walks into the destination it is
        # creating: ~1000 directories deep until RecursionError, undone by
        # `_undo_move` but reported as the generic "moving failed".
        if old_root:
            # realpath on both, because the refusal is about where the bytes
            # actually land: a symlink under the new path that resolves back
            # inside the store passes the textual test and then walks into the
            # destination it is creating, exactly as an untested subdirectory
            # did.
            old_abs = os.path.realpath(old_root)
            try:
                contained = (os.path.commonpath([old_abs,
                                                 os.path.realpath(new_root)])
                             == old_abs)
            except ValueError:
                # Two paths on different Windows drives have no common
                # prefix, and `commonpath` says so by raising. That is the
                # answer "not contained", not an error: letting it propagate
                # made `relocate` raise instead of returning (ok, message) --
                # and a cross-drive move is the entire reason the EXDEV copy
                # path, its byte progress and its ENOSPC message exist.
                contained = False
            if contained:
                return False, _("That folder is inside the current download "
                                "folder. Choose one outside it.")
        # **Anything at all, not just a rival catalog.** The store owns its
        # root: `_move_tree` moves every entry out of it, and the orphan
        # sweep deletes item-shaped directories inside it. Sharing the folder
        # with the user's own files makes both of those act on data this app
        # never wrote, and the failure surfaces launches later with no
        # gesture to connect it to. An empty folder is the only one where
        # "the store owns this" is true when we say it.
        try:
            existing = os.listdir(new_root) if os.path.isdir(new_root) else []
        except OSError:
            return False, _("Can't read that folder. Check the path and its "
                            "permissions.")
        if existing:
            if os.path.exists(os.path.join(new_root, "catalog.db")):
                # Named apart because it is the one non-empty folder a user
                # picks on purpose, and "choose an empty folder" reads as a
                # refusal to find their own downloads.
                return False, _("That folder already contains downloads. "
                                "Choose an empty folder.")
            return False, _("That folder isn't empty. Choose an empty folder — "
                            "the download folder is managed by this app and "
                            "anything else in it can be moved or removed.")
        try:
            os.makedirs(new_root, exist_ok=True)
        except OSError:
            return False, _("Can't create that folder. Check the path and its "
                            "permissions.")
        # Stop the worker and close the catalog so nothing is open mid-move.
        # _relocating keeps enqueue/delete off the (closed) catalog until we
        # reopen at the destination.
        self._relocating = True
        if not self.stop():
            # The worker is still alive and still holds an open .part handle.
            # The _active_item check above is not enough on its own: it is
            # sampled before stop(), and the chunk loop only notices _stop
            # between chunks -- a stalled connection parks it in a socket read
            # for up to the 60s read timeout. Moving the tree out from under
            # that handle means the abandoned worker keeps appending while
            # _open_and_run starts a SECOND worker on the same rows at the new
            # root: two writers interleaving into one .part, which is the
            # corruption _generation was introduced to prevent and does not
            # cover mid-_stream.
            #
            # Reopen where the files still are and refuse the move.
            self.root = old_root
            try:
                self._open_and_run()
            finally:
                self._relocating = False
            return False, _("A download is still finishing. Wait for it to "
                            "stop, then try again.")
        try:
            self._move_tree(old_root, new_root, progress)
        except Exception as exc:
            log.error("Failed to move download folder from %r to %r",
                      old_root, new_root, exc_info=True)
            self.root = old_root
            try:
                self._open_and_run()  # resume where the downloads still are
            finally:
                # Cleared only once the catalog is open again -- see below.
                self._relocating = False
            # Named separately because it is the one the user can act on, and
            # the generic wording sent people looking for a bug instead of at
            # their free space.
            if getattr(exc, "errno", None) == errno.ENOSPC:
                return False, _("There isn't enough space on that drive to "
                                "move the downloads. Free some space and try "
                                "again — nothing was moved.")
            return False, _("Moving the downloads failed. They were left in "
                            "place; the download folder was not changed.")
        self.root = new_root
        try:
            self._open_and_run()
        finally:
            # NOT in a `finally` around the move: this used to be cleared
            # before the reopen, leaving a window where self.db was the
            # *closed* old handle and the enqueue/delete guards were open.
            # An enqueue landing there did its network work, wrote to a
            # closed connection (a silent no-op), and still told the user N
            # items were queued.
            self._relocating = False
        return True, ""

    def _move_tree(self, old_root, new_root, progress=None):
        """Move every entry from old_root into new_root (created by the caller).

        Same-filesystem entries are renamed (instant); entries on a different
        drive are copied with byte progress and then removed. Skips any name
        that already exists in the destination rather than clobber it.
        """
        if not os.path.isdir(old_root):
            if progress:
                progress(0, 0)
            return
        names = [n for n in os.listdir(old_root)
                 if not os.path.exists(os.path.join(new_root, n))]
        # catalog.db LAST. While it is still at the old root, a move that dies
        # partway can reopen the real catalog there -- which is what keeps the
        # startup sweep in `_open_and_run` from mistaking surviving media for
        # orphans. Moving it first is what turned "the copy failed" into "the
        # downloads are gone".
        # ...and the backup immediately before it, so a move killed outright
        # (where `_undo_move` never runs) leaves the old root still able to
        # describe itself: media, then the backup, then the catalog. The two
        # keys were the wrong way round and put the BACKUP last -- so a kill
        # in that window left the backup alone at the old root and the
        # catalog at the new one, which is one of the states `_open_catalog`
        # has to recover from rather than a state to arrange.
        names.sort(key=lambda n: (n == "catalog.db", n == self.CATALOG_BACKUP))
        sizes = {n: self._tree_size(os.path.join(old_root, n)) for n in names}
        # [copied so far, total, bytes at last emit] — mutated as we go.
        state = [0, sum(sizes.values()), 0]
        if progress:
            progress(0, state[1])
        # What has been done so far, as (src, dest, renamed), so a failure can
        # put it all back. The caller tells the user "nothing was moved" and
        # this is what has to make that true: sources copied across are removed
        # only once EVERY entry is over (deleting each as it finished meant a
        # later failure had already destroyed the earlier originals), and
        # anything already across is undone rather than left where it fell.
        #
        # Undoing is not a second authority over the user's data: a copied
        # entry's original is still at `old_root`, so only OUR copy is
        # removed; and `names` never included anything that was in `new_root`
        # before we started, so nothing there is ever a candidate. A renamed
        # entry is renamed straight back **only while its old name is still
        # free** -- see `_undo_move`, which refuses rather than replace.
        done = []
        for name in names:
            src = os.path.join(old_root, name)
            dest = os.path.join(new_root, name)
            try:
                try:
                    os.rename(src, dest)  # instant on the same filesystem
                    renamed = True
                except OSError:
                    # Different drive (EXDEV): copy across; the original stays
                    # put until the whole move has succeeded.
                    self._copy_tree(src, dest, state, progress)
                    renamed = False
            except BaseException:
                # This entry's own half-written destination first -- otherwise
                # a retry after freeing space hits the "already there, skip it"
                # filter above and silently finishes a partial tree.
                self._discard(dest)
                self._undo_move(done)
                raise
            done.append((src, dest, renamed))
            if renamed:
                state[0] += sizes[name]
                self._emit_progress(state, progress, force=True)
        # Everything is across. Only now is dropping the originals safe.
        for src, _dest, renamed in done:
            if not renamed:
                self._discard(src)
        # Drop the now-empty old folder (best-effort; harmless if it lingers).
        try:
            os.rmdir(old_root)
        except OSError:
            pass
        if progress:
            progress(state[1], state[1])

    @staticmethod
    def _discard(path):
        """Remove a file or directory we created, best effort."""
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass

    def _undo_move(self, done):
        """Put back everything a failed move had already got across.

        Without this a cross-drive move that died partway left a full second
        copy of every entry it had finished -- at `new_root`, with the
        original still at `old_root` -- and a retry skipped them ("already
        there") so the duplicate was never reclaimed. On a mixed tree it was
        worse: a renamed entry was simply gone from the old root, and
        reopening there re-queued it for download while the file sat at the
        new one.

        Best effort throughout: this runs while something has already failed
        (usually a full disk), so it must not raise over the top of the error
        the caller is about to report.
        """
        for src, dest, renamed in reversed(done):
            try:
                if renamed:
                    if os.path.exists(src):
                        # **Never over the top of something that is there
                        # now.** The rollback is only safe while `src` is
                        # still the slot we vacated; anything else writing
                        # into the store during a move breaks that, and
                        # `os.replace` destroys what it wrote without a
                        # sound. Leaving the entry at `dest` and saying so
                        # loses nothing -- the caller reports the move failed
                        # either way, and both copies still exist.
                        log.warning("Not undoing the move of %s: something "
                                    "was created there while the move ran. "
                                    "The moved copy is at %s.", src, dest)
                        continue
                    os.replace(dest, src)
                else:
                    self._discard(dest)     # the original never left old_root
            except OSError:
                log.warning("Could not undo the move of %s; it is at %s.",
                            src, dest, exc_info=True)

    @staticmethod
    def _tree_size(path):
        if os.path.isfile(path):
            try:
                return os.path.getsize(path)
            except OSError:
                return 0
        total = 0
        for dirpath, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        return total

    def _copy_tree(self, src, dst, state, progress):
        """Recursively copy src->dst, chunking files so `state`/progress advance
        smoothly on a large media file."""
        if os.path.isdir(src):
            os.makedirs(dst, exist_ok=True)
            for name in os.listdir(src):
                self._copy_tree(os.path.join(src, name),
                                os.path.join(dst, name), state, progress)
            return
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            while True:
                chunk = fin.read(CHUNK)
                if not chunk:
                    break
                fout.write(chunk)
                state[0] += len(chunk)
                self._emit_progress(state, progress)
        shutil.copystat(src, dst)

    @staticmethod
    def _emit_progress(state, progress, force=False):
        copied, total, last = state
        if not progress:
            return
        if force or copied - last >= PROGRESS_STEP:
            state[2] = copied
            progress(min(copied, total), total)

    def stop(self):
        """Stop the download worker and close the catalog.

        Returns True if the worker actually unwound. False means it is still
        running and still owns an open ``.part`` handle -- callers that are
        about to touch the store's files (relocate) MUST NOT proceed on a
        False. Shutdown paths can ignore it: the thread is a daemon and the
        process is going away regardless.
        """
        self._stop = True
        self._wake.set()
        # Join the worker so it isn't killed mid-write, then close the catalog.
        # The chunk loop polls self._stop every chunk, but a chunk can take up
        # to the 60s read timeout to arrive, so this join genuinely can expire.
        joined = True
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=STOP_JOIN_TIMEOUT)
            if worker.is_alive():
                joined = False
                log.warning("Download worker did not stop within %ds.",
                            STOP_JOIN_TIMEOUT)
        if self.db is not None:
            try:
                self.db.close()
            except Exception:
                log.debug("Closing catalog on stop failed.", exc_info=True)
        return joined

    # -- queries (also used by the browser via IPC) ------------------------

    def downloaded_item_ids(self):
        return self.db.downloaded_item_ids() if self.db else set()

    def downloaded_series_ids(self):
        return self.db.downloaded_series_ids() if self.db else set()

    def downloaded_season_ids(self):
        return self.db.downloaded_season_ids() if self.db else set()

    def state(self):
        """Snapshot the browser caches for indicators + the status bar."""
        if not self.db:
            return {"items": [], "series": [], "total_bytes": 0, "active": 0,
                    "downloading": None}
        rows = self.db.list()
        items = [r["item_id"] for r in rows if r["status"] == STATUS_COMPLETE]
        series = sorted({r["series_id"] for r in rows
                         if r["status"] == STATUS_COMPLETE and r["series_id"]})
        total = sum(r["downloaded_bytes"] or 0 for r in rows)
        active = sum(1 for r in rows
                     if r["status"] in (STATUS_PENDING, STATUS_DOWNLOADING))
        downloading = next((r["name"] for r in rows
                            if r["status"] == STATUS_DOWNLOADING), None)
        return {"items": items, "series": series, "total_bytes": total,
                "active": active, "downloading": downloading}

    # -- estimate / enqueue / delete --------------------------------------

    def estimate(self, server_uuid, item_id, item_type):
        client = self.get_client(server_uuid)
        if not client:
            return {"count": 0, "total_bytes": 0, "watched_count": 0}
        items = self._expand(client.jellyfin, item_id, item_type)
        total = sum(self._source_size(i) for i in items)
        watched = sum(1 for i in items if (i.get("UserData") or {}).get("Played"))
        already = sum(1 for i in items if self.db.is_complete(i.get("Id")))
        # Flag a music (audio-only) collection so the dialog can default to
        # including "watched" (played) items — you don't skip played songs.
        audio_only = bool(items) and all(
            i.get("Type") in ("Audio", AUDIOBOOK_TYPE) for i in items)
        # Books have no size on the wire under any Fields value, so the
        # estimate for one is honestly unknown rather than zero. Counted, not
        # flagged: a folder can hold both, and "3 of 8 unknown" is the true
        # statement -- a bare flag would make a folder with one book in it
        # report the whole thing as unmeasurable.
        unsized = sum(1 for i in items if not self._source_size(i))
        return {"count": len(items), "total_bytes": total,
                "watched_count": watched, "already_count": already,
                "unsized_count": unsized,
                "audio_only": audio_only}

    def enqueue(self, server_uuid, item_id, item_type, include_watched=False,
                origin=ORIGIN_USER):
        if self._relocating:
            return 0  # catalog is mid-move; caller can retry after
        client = self.get_client(server_uuid)
        if not client:
            return 0
        server_id = client.config.data.get("auth.server-id")
        items = self._expand(client.jellyfin, item_id, item_type)
        # For a playlist, capture which items already existed before this
        # download so ownership (what a later "delete playlist" may remove) goes
        # only to items this playlist actually pulls down — see _record_playlist.
        pre_existing = ({i.get("Id") for i in items if self.db.get(i.get("Id"))}
                        if item_type == "Playlist" else set())
        added = 0
        members = []  # item ids that will be present offline, in playlist order

        def keep(iid):
            """Record that this enqueue leaves `iid` present offline.

            The `_uncancel` belongs to this decision and not to the top of the
            loop. Withdrawn unconditionally, it also withdrew the delete of an
            item this enqueue goes on to *decline* -- a watched episode, with
            `include_watched` off -- so deleting a stalled download and then
            pressing Download on its series resumed and completed the very
            item the enqueue had refused to queue.
            """
            self._uncancel(iid)
            members.append(iid)

        for item in items:
            iid = item.get("Id")
            if self.db.is_complete(iid):
                keep(iid)  # already downloaded → still a member
                # A user asking for something the scheduler already fetched
                # takes ownership of it, so the reaper stops considering it.
                # Never the reverse: an auto pass must not downgrade a
                # download the user asked for.
                if origin == ORIGIN_USER:
                    row = self.db.get(iid)
                    if row and is_auto(row["origin"]):
                        self.db.set_origin(iid, ORIGIN_USER)
                    self._clear_discard(iid)
                    self._claim_from_playlists(iid, item_type)
                continue
            if not include_watched and (item.get("UserData") or {}).get("Played"):
                continue
            if origin == ORIGIN_USER:
                # Asking for it by hand overrides a previous auto discard,
                # which is the only signal that outranks the reaper.
                self._clear_discard(iid)
                self._claim_from_playlists(iid, item_type)
            keep(iid)
            self._add_row(server_uuid, server_id, item, origin=origin)
            added += 1
        if item_type == "Playlist":
            self._record_playlist(server_uuid, server_id, client.jellyfin,
                                  item_id, members, pre_existing)
        if added:
            log.info("Queued %d item(s) for offline download.", added)
            self._notify_change()
            self._wake.set()
        return added

    def _is_cancelled(self, item_id):
        """Whether a delete is pending for `item_id`.

        Lock-free on purpose. `set.__contains__` is atomic, and every caller
        either already holds `_active_lock` -- which is a plain `Lock`, so
        taking it again would deadlock -- or is a signal that only has to be
        noticed eventually, on the next chunk.
        """
        return item_id in self._cancelled

    def _drop_cancelled(self, row):
        """Act on a pending delete for `row`. Returns whether one was owed.

        **The only place a cancellation is acted on.** "The delete wins" used
        to be written out at three sites in `_download` with three different
        combinations of check, discard, remove-files, delete-row and notify,
        and they drifted exactly as you would expect: the `except _Cancelled`
        handler never re-checked, so a download the user had asked for again
        in the meantime was deleted anyway.

        The sample and the row delete are **one** critical section. Sampling
        under the lock and then acting outside it left a window in which an
        `enqueue` could withdraw the cancel, write a fresh row, and have this
        delete that row -- the failure `_uncancel` exists to prevent, one
        window along. The files are removed afterwards, outside the lock:
        losing them costs a re-download, and a re-enqueued item rebuilds its
        directory anyway, whereas losing the row is what strands the bytes.
        """
        item_id = row["item_id"]
        with self._active_lock:
            if item_id not in self._cancelled:
                return False
            self._cancelled.discard(item_id)
            self.db.delete(item_id)
        self._remove_files(row)
        self._short_read_stalls.pop(item_id, None)
        self._notify_change()
        return True

    def _uncancel(self, item_id):
        """Withdraw a pending cancellation for an item that is wanted again.

        `_cancelled` is a transient signal to the worker, not a record of
        policy, and it outlives the delete that raised it: the worker only
        honours it between chunks, and a chunk can take up to the 60s read
        timeout. Deleting a stalled download and then changing your mind
        inside that window used to enqueue the item, report it queued, and
        have the worker's unwind delete the row underneath -- so the download
        silently did not happen and pressing Download a second time worked.

        Not gated on origin: this says the item is wanted, which is as true
        of a scheduled fetch as of one asked for by hand.
        """
        with self._active_lock:
            self._cancelled.discard(item_id)

    def _claim_from_playlists(self, item_id, item_type):
        """A user download outranks a playlist's claim on the same item.

        There are two things that will delete a download the user did not ask
        to delete: the reaper, and deleting a playlist that *owns* the item.
        `enqueue` already answers the first -- `set_origin` promotes the row
        so the reaper stops considering it -- and answered only the first, so
        pressing Download on an episode a downloaded playlist had pulled in
        left it owned, and deleting that playlist deleted it anyway.

        Not for `item_type == "Playlist"`: that call *is* the playlist
        download, and `_record_playlist` recomputes ownership from
        `pre_existing` a few lines below. Disowning here would race it.

        Best-effort, like `_clear_discard` beside it: a missing table or a
        closed catalog must not fail a download the user asked for.
        """
        if item_type == "Playlist":
            return
        try:
            self.db.disown_playlist_items(item_id)
        except Exception:
            log.debug("Could not claim %s from its playlist", item_id,
                      exc_info=True)

    def _clear_discard(self, item_id):
        """Best-effort: a missing tombstone table or a closed catalog must
        not fail a download the user asked for."""
        try:
            self.db.clear_discarded(item_id)
        except Exception:
            log.debug("Could not clear the discard for %s", item_id,
                      exc_info=True)

    def _record_playlist(self, server_uuid, server_id, api, playlist_id,
                         member_ids, pre_existing):
        """Persist a downloaded playlist and its membership. An item is `owned`
        by this playlist if this download is what pulls it in (it wasn't already
        in the catalog), or it was already owned by this playlist on a prior
        download. Items that pre-existed from another route stay unowned so a
        later playlist delete leaves them (and their original grouping) intact."""
        if not member_ids:
            # Nothing supported/available offline — drop any stale record so an
            # emptied playlist doesn't linger in the offline UI.
            self.db.delete_playlist(playlist_id)
            return
        try:
            name = (api.get_item(playlist_id) or {}).get("Name") or "Playlist"
        except Exception:
            log.debug("Failed to fetch playlist name for %s", playlist_id,
                      exc_info=True)
            name = "Playlist"
        already_owned = self.db.playlist_owned_ids(playlist_id)
        # A playlist may list the same item twice; membership is keyed by
        # item_id, so keep the first position and drop later duplicates.
        entries, seen = [], set()
        for iid in member_ids:
            if iid in seen:
                continue
            seen.add(iid)
            owned = iid in already_owned or iid not in pre_existing
            entries.append((iid, len(entries), owned))
        self.db.upsert_playlist(playlist_id, server_id, server_uuid, name)
        self.db.replace_playlist_items(playlist_id, entries)
        try:
            self._download_playlist_art(
                self.get_client(server_uuid), server_id, playlist_id)
        except Exception:
            log.debug("Could not cache playlist art for %s", playlist_id,
                      exc_info=True)

    def _cancel_if_active(self, item_id):
        """If the worker is downloading `item_id`, flag it for cancellation and
        let the worker do the file/row cleanup. Returns True if it was active."""
        with self._active_lock:
            if self._active_item == item_id:
                self._cancelled.add(item_id)
                return True
        return False

    def delete_item(self, item_id, only_if_auto=False):
        """Remove one download. Returns whether anything was removed.

        ``only_if_auto`` is the reaper's: delete the row only while it is
        still an auto-download. The reaper decides from a snapshot taken
        before a long run of network calls, and a user pressing Download in
        that window promotes the row to user-owned -- which is exactly the
        promise `enqueue` makes when it does so. Without this the episode was
        deleted out from under them.
        """
        # Drop any short-read stall bookkeeping so it can't linger for a
        # deleted item (the worker's finally only clears _cancelled).
        self._short_read_stalls.pop(item_id, None)
        if self._cancel_if_active(item_id):
            # Not for the reaper: cancelling an in-flight download is a
            # deletion too, and `only_if_auto` exists so it cannot touch a row
            # the user has claimed. Unreachable today -- the reaper only walks
            # COMPLETE and ERROR rows, never the active one -- but the guard
            # should not have a hole in it that a future caller can find.
            if only_if_auto:
                row = self.db.get(item_id)
                if row is not None and not is_auto(row.get("origin")):
                    # `_cancel_if_active` has already flagged it. Withdraw, or
                    # the worker honours a delete we just declined -- which is
                    # the one thing `only_if_auto` exists to prevent.
                    self._uncancel(item_id)
                    return False
            self._notify_change()
            return True
        if only_if_auto:
            # Claimed atomically, then the files. Row first is deliberate
            # here: a failed unlink leaves orphaned files that the next
            # reconcile sweeps, whereas files-first with a failed row delete
            # leaves a COMPLETE row pointing at nothing, which the same sweep
            # answers by downloading it all over again.
            row = self.db.delete_if_auto(item_id)
            if row is None:
                log.info("Not reaping %s: it is no longer an auto-download.",
                         item_id)
                return False
            self._remove_files(row)
            self._notify_change()
            return True
        row = self.db.get(item_id)
        if not row:
            return False
        if not self._remove_files(row):
            # The row is what stops this becoming a resurrection. A directory
            # with no row is an orphan, and `_adopt_orphan` rebuilds it as
            # `user` -- it has no evidence the download was ever scheduled --
            # so the item the user just deleted comes back, in the one state
            # `delete_if_auto` will never remove. Left in the catalog the two
            # still agree, and a retry once the file is free does the job.
            return False
        self.db.delete(item_id)
        self._notify_change()
        return True

    def delete(self, item_id=None, series_id=None, season_id=None,
               watched_only=False, watched_all=False, playlist_id=None,
               only_if_auto=False):
        """Flexible delete: a single item, a season, a whole series, a
        playlist's downloads, and/or only watched items within that scope.

        An unscoped call deletes NOTHING. A caller that simply forgot to pass
        its scope used to wipe the entire catalog, and the only thing standing
        between that and the user was a confirm dialog naming the group they
        thought they were deleting.

        ``watched_all`` is the library-wide watched sweep, and it **implies**
        ``watched_only``. It used to be only half of that -- a scope that
        unlocked the whole catalog, with the filter left to a second argument
        -- so ``watched_all=True`` alone deleted everything, watched or not,
        under the one name in this signature that reads like a filter. There
        is now no combination of these arguments that deletes an unwatched
        download outside a named series, season or playlist."""
        if self._relocating:
            return  # catalog is mid-move; caller can retry after
        if watched_all:
            watched_only = True
        if item_id:
            # The only branch with a meaningful return -- the reaper reads it
            # to know whether its count and its tombstone are earned.
            return self.delete_item(item_id, only_if_auto=only_if_auto)
        if not (series_id or season_id or playlist_id or watched_all):
            log.error("sync delete called with no scope; refusing to delete "
                      "the whole catalog")
            return
        if playlist_id:
            self._delete_playlist(playlist_id, watched_only=watched_only)
            return
        rows = self.db.list(series_id=series_id) if series_id else self.db.list()
        removed = 0
        for row in rows:
            if season_id and row.get("season_id") != season_id:
                continue
            if watched_only:
                try:
                    userdata = json.loads(row.get("userdata_json") or "{}")
                except ValueError:
                    userdata = {}
                if not userdata.get("Played"):
                    continue
            if self._cancel_if_active(row["item_id"]):
                removed += 1
                continue
            if not self._remove_files(row):
                continue        # see delete_item: dropping the row here
                                # resurrects it on the next launch
            self.db.delete(row["item_id"])
            removed += 1
        if removed:
            self._notify_change()

    def _delete_playlist(self, playlist_id, watched_only=False):
        """Delete a downloaded playlist. Only the items this playlist *owns*
        (pulled down itself) are removed from disk; items that were already
        downloaded another way stay put. The playlist record is then dropped."""
        owned = self.db.playlist_owned_ids(playlist_id)
        for item_id in owned:
            if watched_only:
                row = self.db.get(item_id)
                try:
                    played = bool(json.loads(
                        (row or {}).get("userdata_json") or "{}").get("Played"))
                except ValueError:
                    played = False
                if not played:
                    continue
            self.delete_item(item_id)  # removes files + row, cleans membership
        if not watched_only:
            self.db.delete_playlist(playlist_id)
        self._notify_change()

    # -- expansion / helpers ----------------------------------------------

    def _expand(self, api, item_id, item_type):
        try:
            if item_type == "Series":
                res = api.get_episodes(item_id, fields="MediaSources")
                return (res or {}).get("Items", [])
            if item_type == "Season":
                season = api.get_item(item_id) or {}
                series_id = season.get("SeriesId")
                if not series_id:
                    return []
                res = api.get_episodes(series_id, season_id=item_id,
                                       fields="MediaSources")
                return (res or {}).get("Items", [])
            if item_type == "Playlist":
                res = api.get_playlist_items(item_id, fields="MediaSources")
                items = (res or {}).get("Items", [])
                # Playlists can mix in other entries; only download the types
                # the browser surfaces (mirrors PLAYLIST_SUPPORTED_TYPES).
                # Audio is included so music playlists download as one unit.
                return [i for i in items if i.get("Type") in DOWNLOADABLE]
            if item_type in FOLDER_ITEM_TYPES:
                # A books library is a folder tree, and a multi-file
                # audiobook is a *folder* -- nothing else joins its chapters
                # (SeriesName is null on audiobooks and Album is tag-derived,
                # so an untagged rip has no metadata linking its files at
                # all). So the folder is the download unit, and it is the
                # only container that has to be expanded by listing.
                #
                # Not recursive: "download this folder" means this folder,
                # and an author directory holding forty books should not
                # quietly become forty downloads. Path is asked for because
                # it is the only statement of a Book's format (books.py).
                res = items_api.get_items(api, parent_id=item_id,
                                         fields="MediaSources,Path",
                                         sort_by="SortName", limit=500)
                items = (res or {}).get("Items", [])
                return [i for i in items if i.get("Type") in DOWNLOADABLE]
            item = api.get_item(item_id, fields="MediaSources,Path")
            return [item] if item else []
        except Exception as exc:
            # Raised, not swallowed into []. Two documented contracts above
            # this depend on it: `gateway.download_enqueue` ("Raises on
            # failure... swallowed, a rejected enqueue looked exactly like a
            # queued one") and `gateway.download_estimate` (a zero estimate
            # made failure indistinguishable from "already fully downloaded"
            # and hid the retry control). Both were defeated here.
            log.error("Failed to expand %s (%s)", item_id, item_type, exc_info=True)
            raise ExpandFailed(
                "could not list %s (%s)" % (item_id, item_type)) from exc

    @staticmethod
    def _source_size(item):
        sources = item.get("MediaSources") or []
        return (sources[0].get("Size") or 0) if sources else 0

    @staticmethod
    def _ext_for(item):
        """Filename extension to store this item's media under.

        Everything with a media source states its container, and that is the
        answer. A `Book` has no media source and no `Container` field at all
        (measured: `Fields=Size`, `Fields=MediaSources` and `Fields=Container`
        all come back empty on one), so its format is read from `Path` --
        which is what jellyfin-web does too, and is the only place it is
        stated. For a book the extension is not cosmetic: it is what tells
        the desktop which application opens the file.
        """
        source = (item.get("MediaSources") or [{}])[0]
        container = (source.get("Container") or "").split(",")[0]
        if container:
            return container
        if is_book(item):
            # "bin" rather than "mkv" when even Path says nothing: an
            # unopenable file named honestly beats one claiming to be a
            # video. _download corrects it from Content-Disposition.
            return book_format(item) or "bin"
        return "mkv"

    def _library_id_for(self, server_uuid, item):
        """The CollectionFolder ``item`` lives in, or None. Best effort.

        Recorded at download time so the shader-profile library scope can be
        answered for downloaded media **with the server away**, and so the
        play path never has to make this call -- it runs there under the
        player lock, which is the wrong place for a request that the
        apiclient will retry for two and a half minutes against an
        unresponsive server.

        Keyed on the series where there is one: every episode of a show is
        in the same library, so a season costs one request rather than one
        per file. Failure is not cached here (unlike the player-side cache):
        this runs once per item on a path that is already doing network I/O,
        and a download queued during a blip should get its library on the
        next one rather than never.
        """
        lookup = (item or {}).get("SeriesId") or (item or {}).get("Id")
        if not lookup:
            return None
        if lookup in self._library_ids:
            return self._library_ids[lookup]
        found = None
        try:
            client = self.get_client(server_uuid)
            if client is not None:
                for ancestor in client.jellyfin.get_ancestors(lookup) or []:
                    if ancestor.get("Type") == "CollectionFolder":
                        found = ancestor.get("Id")
                        break
        except Exception:
            log.debug("could not resolve the library for %s", lookup,
                      exc_info=True)
            return None
        self._library_ids[lookup] = found
        return found

    def _add_row(self, server_uuid, server_id, item, origin=ORIGIN_USER):
        source = (item.get("MediaSources") or [{}])[0]
        ext = self._ext_for(item)
        self.db.upsert({
            "item_id": item["Id"],
            "server_id": server_id,
            "server_uuid": server_uuid,
            "type": item.get("Type"),
            "name": item.get("Name"),
            "series_id": item.get("SeriesId"),
            "series_name": item.get("SeriesName"),
            "season_id": item.get("SeasonId"),
            "parent_index": item.get("ParentIndexNumber"),
            "index_number": item.get("IndexNumber"),
            "media_source_id": source.get("Id"),
            "file_path": None,
            "ext": ext,
            "size_bytes": source.get("Size") or 0,
            "downloaded_bytes": 0,
            "status": STATUS_PENDING,
            "runtime_ticks": item.get("RunTimeTicks"),
            "library_id": self._library_id_for(server_uuid, item),
            "item_json": json.dumps(item),
            "source_json": json.dumps(source),
            "userdata_json": json.dumps(item.get("UserData") or {}),
            "added_at": int(time.time()),
            "origin": origin,
            "completed_at": None,
        })

    def _item_dir(self, row):
        return os.path.join(self.root, row.get("server_id") or "server",
                            row["item_id"])

    def _remove_files(self, row):
        """Remove a download's directory. Returns whether it is gone.

        `rmtree(ignore_errors=True)` cannot raise, and "cannot raise" is not
        "succeeded" -- a locked file leaves the directory standing and says
        nothing. That is the ordinary Windows case, not a crash: the media
        open in a player, a scanner or an indexer holding it. So the
        observable is the only honest answer, and callers that drop the row
        on the strength of this one hand the next launch an item directory
        with no row, which is the orphan shape.
        """
        item_dir = self._item_dir(row)
        try:
            shutil.rmtree(item_dir, ignore_errors=True)
        except Exception:
            log.debug("Failed to remove files for %s", row.get("item_id"),
                      exc_info=True)
        if os.path.exists(item_dir):
            log.warning("Could not remove the files for %s at %s.",
                        row.get("item_id"), item_dir)
            return False
        return True

    def _reconcile_disk(self, sweep_orphans=True):
        """Best-effort startup sweep to keep the catalog and the file store in
        agreement (S12):

        * a row marked COMPLETE whose media file has vanished is re-queued
          (PENDING) so it downloads again;
        * an on-disk per-item directory with no catalog row is removed.

        The second half **identifies what it deletes rather than inferring
        it**, and every one of the four tests below is load-bearing:

        * the catalog has to be readable (`db.healthy`) -- an unreadable one
          answers `[]` to every query, which reads as "none of this is known";
        * the server directory has to be one the catalog *names*, so a
          directory this app never wrote is not a candidate at all;
        * the child has to be shaped like a Jellyfin item id, which is what
          this app names an item directory;
        * and the child must not be a live row.

        Inferring it -- "in the store, not in the catalog, therefore ours and
        orphaned" -- is how a download folder pointed at an existing media
        library (`~/Videos/Holidays/2019 Italy`) had the user's own
        directories deleted on the second launch, the first being covered by
        the empty-catalog guard in `_open_and_run` and no launch after it.
        """
        if not self.db.healthy():
            # Refusing the requeue half too: a `[]` from an unreadable catalog
            # is not "no rows to check" either, and the write it would skip is
            # the harmless half anyway.
            log.error("Skipping the disk reconcile: the catalog is unreadable.")
            return
        rows = self.db.list()
        known = {}  # server_dir -> set(item_id)
        server_uuids = {}   # server_dir -> server_uuid, for _adopt_orphan
        for row in rows:
            server_dir = row.get("server_id") or "server"
            known.setdefault(server_dir, set()).add(row["item_id"])
            if row.get("server_uuid"):
                server_uuids.setdefault(server_dir, row["server_uuid"])
            if row["status"] != STATUS_COMPLETE:
                continue
            file_path = row.get("file_path")
            full = os.path.join(self.root, file_path) if file_path else None
            if not full or not os.path.exists(full):
                log.warning("Downloaded file missing for %s; re-queuing.",
                            row.get("name") or row["item_id"])
                self.db.update(row["item_id"], status=STATUS_PENDING,
                               downloaded_bytes=0, file_path=None)

        if not sweep_orphans:
            return
        # Only the server directories the catalog names -- never everything
        # in the root. A store with no rows sweeps nothing, which is correct:
        # there is no such thing as an orphan we can prove.
        for server_dir, item_ids in known.items():
            base = os.path.join(self.root, server_dir)
            if not os.path.isdir(base):
                continue
            try:
                children = os.listdir(base)
            except OSError:
                continue
            for child in children:
                if child in RESERVED_STORE_DIRS or child in item_ids:
                    continue
                child_path = os.path.join(base, child)
                if not os.path.isdir(child_path):
                    continue
                if not _looks_like_item_id(child):
                    # Not a name this app writes. Leaving it costs a stale
                    # directory; deleting it is unrecoverable and, on a store
                    # sharing a folder with anything else, not even ours.
                    log.warning("Leaving %s alone: it is inside the download "
                                "store but is not named like a download.",
                                child_path)
                    continue
                if self._adopt_orphan(server_dir, child, child_path,
                                      server_uuids):
                    continue
                log.warning("Removing orphaned download dir: %s", child_path)
                shutil.rmtree(child_path, ignore_errors=True)

    def _adopt_orphan(self, server_dir, item_id, item_dir, server_uuids):
        """Rebuild the catalog row for a complete download that has none.

        `_download` writes `item.json` and `source.json` beside the media
        precisely so a download describes itself, which makes "the catalog
        forgot this" recoverable rather than terminal. Adopting is what stops
        a catalog restored from the backup (`_open_catalog`) from turning into
        a *delayed* wipe: everything downloaded after the snapshot has files
        and no row, which is the orphan shape, and the launch after the
        restore would have swept exactly those.

        Both halves are required -- the manifest *and* the media. A directory
        with a manifest and no media is an interrupted download or the residue
        of a delete whose unlink failed, and reclaiming that space is the
        sweep's actual job. Only a whole, playable copy is worth arguing over,
        and where it is arguable the recoverable error is the right one: an
        item that reappears can be deleted again, media deleted on the
        strength of a missing row cannot be got back.

        Returns whether the row was written (i.e. do not delete this).
        """
        manifest = os.path.join(item_dir, "item.json")
        media = sorted(glob.glob(os.path.join(glob.escape(item_dir), "media.*")))
        media = [m for m in media if not m.endswith(".part")]
        if not os.path.exists(manifest) or not media:
            return False
        try:
            # Explicit encoding: `_download` writes these with json.dump's
            # default ensure_ascii, so today they are ASCII either way -- but
            # this reads a file some other build may have written, and a bare
            # open() here would decode it with the locale codec (cp1252 on
            # Windows) and fail the adopt, which answers "leave it alone".
            with open(manifest, encoding="utf-8") as fh:
                item = json.load(fh)
            source = {}
            source_path = os.path.join(item_dir, "source.json")
            if os.path.exists(source_path):
                with open(source_path, encoding="utf-8") as fh:
                    source = json.load(fh)
            media_path = media[0]
            size = os.path.getsize(media_path)
            self.db.upsert({
                "item_id": item_id,
                "server_id": None if server_dir == "server" else server_dir,
                # Recovered from a surviving row for the same server: the id
                # is in the path, the uuid is only ever in the catalog. None
                # is survivable (the copy still plays offline; only its
                # watched-state sync waits for a re-download) and is better
                # than guessing.
                "server_uuid": server_uuids.get(server_dir),
                "type": item.get("Type"),
                "name": item.get("Name"),
                "series_id": item.get("SeriesId"),
                "series_name": item.get("SeriesName"),
                "season_id": item.get("SeasonId"),
                "parent_index": item.get("ParentIndexNumber"),
                "index_number": item.get("IndexNumber"),
                "media_source_id": source.get("Id"),
                "file_path": os.path.relpath(media_path, self.root),
                "ext": os.path.splitext(media_path)[1].lstrip("."),
                "size_bytes": size,
                "downloaded_bytes": size,
                "status": STATUS_COMPLETE,
                "runtime_ticks": item.get("RunTimeTicks"),
                "library_id": None,
                "item_json": json.dumps(item),
                "source_json": json.dumps(source),
                "userdata_json": json.dumps(item.get("UserData") or {}),
                "added_at": int(time.time()),
                # Never auto: the reaper deletes auto rows, and a row this
                # method invented has no evidence it was ever a scheduled
                # download. Guessing wrong in that direction deletes it.
                "origin": ORIGIN_USER,
                "completed_at": int(os.path.getmtime(media_path)),
            })
        except Exception:
            log.warning("Could not re-adopt the download at %s; leaving it in "
                        "place.", item_dir, exc_info=True)
            # Deliberately True: we could not describe it, so we certainly
            # cannot justify deleting it.
            return True
        log.warning("Re-adopted the download at %s (%s) — it had no catalog "
                    "row.", item_dir, item.get("Name") or item_id)
        return True

    def _notify_change(self):
        try:
            self.on_change()
        except Exception:
            log.debug("sync on_change callback failed", exc_info=True)

    # -- worker ------------------------------------------------------------

    def _run(self, gen=None):
        def stopping():
            """Shutting down, or superseded by a newer worker."""
            return self._stop or (gen is not None
                                  and gen != self._generation)

        error_streak = 0
        while not stopping():
            # Consume the wake signal up front. It used to be cleared only in
            # the idle branch, which is unreachable while a pending row
            # exists — so _download's no-client wait() returned instantly and
            # one queued download against an unreachable server busy-spun
            # this loop at full speed.
            self._wake.clear()
            try:
                # Replay offline playstate on its own cadence — not only when the
                # queue is idle — so one pending download for an unreachable
                # server can't starve watched-state sync for a reachable one.
                now = time.monotonic()
                if now - self._last_playstate >= PLAYSTATE_INTERVAL:
                    self._last_playstate = now
                    self._sync_playstate()
                self._note_connected_servers()
                self._sweep_if_due(now)
                row = self._next_runnable()
                # Only between downloads: a pass here would otherwise
                # enqueue work while the user's own download is streaming,
                # and tick() is a no-op unless the interval has elapsed.
                # Gated on *runnable* work, not on the queue being empty: a
                # pending row for a server we cannot reach is not a download
                # in progress, and treating it as one used to mean one dead
                # server switched auto-download's reaper off for the life of
                # the process — retention and the cap silently stopped being
                # enforced, with the queue's own log line the only clue.
                if self.auto is not None and row is None:
                    self.auto.tick()
                    row = self._next_runnable()
                if row is None:
                    self._wake.wait(5)
                    continue
                self._download(row, stopping=stopping)
                error_streak = 0
            except Exception:
                # The worker must survive anything (disk full, DB errors —
                # note the error path's own db.update can raise again on a
                # full disk); back off so a persistent failure can't spin.
                error_streak += 1
                log.exception("Download worker iteration failed.")
                self._wake.wait(min(60, 5 * error_streak))

    def _next_runnable(self):
        """The first pending row we can actually start now, or None.

        Rows whose server does not resolve are *skipped*, not waited on. The
        queue is drained in enqueue order (see db.list) and the worker used to
        take the head unconditionally, so a single row for a server that is
        gone — logged out, removed, a laptop whose second server is only on
        the home LAN — parked itself at the front and every later download
        queued behind it forever. Nothing retires such a row: it is left
        pending on purpose so it resumes when the server comes back, and
        removing a server does not purge its catalog rows, so "permanently
        unresolvable" is a steady state rather than a blip.

        _sync_playstate already iterates past unresolvable clients for exactly
        this reason; this is the same rule for the download queue.
        """
        blocked = 0
        for row in self.db.list(status=STATUS_PENDING):
            if self.get_client(row["server_uuid"]) is not None:
                if blocked:
                    log.debug("Skipped %d pending download(s) whose server is "
                              "unreachable.", blocked)
                return row
            blocked += 1
        return None

    def _sync_playstate(self):
        """Replay offline playstate once a server is reachable — advancing only:
        mark watched if the server hasn't, and push a later resume position."""
        pending = self.db.list_playstate()
        if not pending:
            return
        done = []
        for entry in pending:
            client = self.get_client(entry.get("server_uuid"))
            if client is None:
                continue  # still offline for this server
            try:
                server_ud = client.jellyfin.get_userdata_for_item(
                    entry["item_id"]) or {}
                update = {}
                if entry.get("played") and not server_ud.get("Played"):
                    update["Played"] = True
                local_pos = entry.get("position_ticks") or 0
                if local_pos > (server_ud.get("PlaybackPositionTicks") or 0):
                    update["PlaybackPositionTicks"] = local_pos
                if update:
                    client.jellyfin.update_userdata_for_item(entry["item_id"],
                                                             update)
                # The values as they were READ, not just the id: the row is
                # updated in place by upsert_playstate, so acknowledging by id
                # would retire progress written while we were on the network.
                done.append((entry["id"], entry.get("position_ticks"),
                             entry.get("played")))
            except Exception:
                log.debug("Failed to replay playstate %s", entry.get("id"),
                          exc_info=True)
        if done:
            self.db.clear_playstate(done)
            log.info("Synced %d offline playstate change(s) to the server.",
                     len(done))

    def _sweep_if_due(self, now):
        """Run a pending catalog sweep, unless one ran too recently.

        The floor **defers, it does not drop** -- the flag is set because
        something happened the websocket could not report, and that does not
        stop being true because a sweep ran three minutes ago. So a flapping
        server costs one sweep per floor rather than one per flap.

        Two other things hold a due sweep back and **neither consumes it**: the
        settle (the first screen gets the network to itself) and having nobody
        to ask (a pass with no clients is not a sweep that found nothing, it is
        a sweep that did not happen).

        Returns whether it swept, which is what the tests read.
        See docs/offline-sync.md section 3.
        """
        if not self._sweep_due:
            return False
        if now - self._started_at < USERDATA_SWEEP_SETTLE:
            return False
        if (self._last_userdata
                and now - self._last_userdata < USERDATA_SWEEP_FLOOR):
            return False
        try:
            if not self.get_clients():
                return False    # nobody to ask; the trigger stays up
        except Exception:
            log.debug("could not read the connected server list",
                      exc_info=True)
            return False
        self._sweep_due = False
        self._last_userdata = now
        self._refresh_userdata()
        return True

    def _note_connected_servers(self):
        """Watch for a server appearing, and mark a sweep due when one does.

        This is the whole schedule. A sweep covers a stretch during which
        nothing was listening, and a server *becoming reachable* is the end of
        exactly such a stretch -- so it is the trigger, in place of the
        interval this used to have.

        **Watched here rather than subscribed to.** `clientManager`'s
        `on_server_connected` is a single slot the browser already assigns, and
        it is a notification fired from five call sites -- a sixth reconnect
        path would leave a gap invisible until somebody's catalog is stale. The
        registry is the state itself, so a set comparison cannot miss a
        transition however the server came back.

        Disappearances are recorded but trigger nothing.
        See docs/offline-sync.md section 3.
        """
        try:
            connected = set(self.get_clients() or {})
        except Exception:
            log.debug("could not read the connected server list",
                      exc_info=True)
            return
        if connected - self._connected_servers:
            log.debug("Server(s) %s reachable again; catalog sweep due.",
                      ", ".join(sorted(connected - self._connected_servers)))
            self._sweep_due = True
        self._connected_servers = connected

    def request_userdata_refresh(self):
        """Ask for a catalog sweep — the home screen is loading.

        The one trigger that is not an edge the app can see, and the only
        thing left covering the gap measured in
        `tests/e2e/test_offline_sync.py`: another client can play something
        to the end and never report its stop, and the server announces that
        to nobody. No reconnect happens, so nothing else here would ever
        notice. Home is where it would show, and a person opening Home is
        the closest thing to a signal that exists.

        Not floored here -- `_run` defers rather than drops, so bouncing in
        and out of Home cannot turn this into a poll and cannot lose a
        request either.

        Cheap and non-blocking: this only marks the sweep due and wakes the
        worker, so the requests happen on the sync thread rather than on
        whatever loaded the page.
        """
        self._sweep_due = True
        self._wake.set()

    def mirror_playstate(self, item_id, position_ticks=None, played=None):
        """Record what *this* app just played, for an item we hold a copy of.

        The catalog is what offline browsing reads, and until this existed
        it was written only when the file being played was the downloaded
        one. Streaming an episode you also have downloaded therefore left
        the catalog at position 0 -- so the copy on disk, the one you keep
        precisely because you are about to lose the network, was the one
        thing that did not know you had watched it.

        Unconditional on purpose: no check of whether the server is
        reachable, and no check of whether the item is downloaded. The
        server half is somebody else's job (the timeline reports to it, and
        `_sync_playstate` replays what it missed); the downloaded half is
        answered by ``db.update_userdata``, which returns False for an item
        it holds no row for. That makes this safe to call for every item
        played, which is the property that keeps it from being forgotten at
        a call site again.

        Advance-only, like every other writer of this column. Never raises.
        """
        if not item_id or (played is None and position_ticks is None):
            return False
        db = self.db
        if db is None:
            return False
        try:
            return db.update_userdata(item_id, played=played,
                                      position_ticks=position_ticks)
        except Exception:
            log.debug("Could not mirror playstate for %s", item_id,
                      exc_info=True)
            return False

    def mirror_watched(self, item_id, played):
        """Record a *deliberate* watched mark in the catalog, immediately.

        The counterpart to :meth:`mirror_playstate` and deliberately not the
        same rule. That one is playback, where advance-only is right. This is a
        person choosing Mark played or Mark unplayed -- **the only signal in
        the app authoritative in both directions** -- so it writes verbatim
        through ``db.set_watched``. Before this, every writer was advance-only
        and an item un-watched here stayed watched on disk forever.

        Unconditional at the call sites, like ``mirror_playstate``:
        ``db.watched_targets`` answers with nothing for an item we hold no copy
        of, which is what keeps the check from being forgotten again.

        Fans out over a series or season id. Never raises; returns how many
        rows moved. See docs/offline-sync.md section 1.
        """
        db = self.db
        if db is None:
            return 0
        try:
            targets = db.watched_targets(item_id)
        except Exception:
            log.debug("Could not resolve downloads for %s", item_id,
                      exc_info=True)
            return 0
        moved = 0
        for target_id, _server in targets:
            try:
                if db.set_watched(target_id, played):
                    moved += 1
            except Exception:
                log.debug("Could not mirror the watched mark for %s",
                          target_id, exc_info=True)
        if moved:
            log.debug("Mirrored a watched mark onto %d downloaded item(s).",
                      moved)
            self._notify_change()
        return moved

    def apply_userdata_event(self, arguments):
        """Apply a ``UserDataChanged`` push to the catalog. No requests.

        This is how watched state normally arrives, and it is free: the server
        sends the changed values themselves. Payload is
        ``{UserId, ServerId, UserDataList: [UserItemDataDto...]}``.

        **Not every save produces one.** The server drops ``PlaybackProgress``
        saves before it ever builds this message, so a client streaming
        elsewhere announces its *start* and its *stop* and nothing in between --
        which is why this does not replace the sweep.

        Ids not in the catalog cost one indexed SELECT and are dropped, which
        is most of them. Runs on the websocket thread, so a long list is handed
        to the sweep instead of walked here.
        See docs/offline-sync.md section 2.
        """
        entries = (arguments or {}).get("UserDataList") or []
        if not entries:
            return
        if len(entries) > USERDATA_EVENT_MAX:
            log.debug("UserDataChanged carried %d entries; sweeping instead.",
                      len(entries))
            self.request_userdata_refresh()
            return
        db = self.db
        if db is None:
            return              # catalog not open (or already closed)
        updated = 0
        for entry in entries:
            item_id = entry.get("ItemId")
            if not item_id:
                continue
            try:
                # `or None` on played, matching the sweep: db.update_userdata
                # is advance-only, and False there would mean "leave it
                # alone" anyway. An un-watch elsewhere does not retreat the
                # local copy -- see _refresh_userdata's note on that rule,
                # which this deliberately does not change.
                if db.update_userdata(
                        item_id,
                        played=entry.get("Played") or None,
                        position_ticks=entry.get("PlaybackPositionTicks")):
                    updated += 1
            except Exception:
                log.debug("Could not apply pushed userdata for %s", item_id,
                          exc_info=True)
        if updated:
            log.debug("Applied pushed watched state for %d downloaded "
                      "item(s).", updated)
            self._notify_change()

    def _refresh_userdata(self):
        """Pull the server's watched state for what we hold, and store it.

        The other direction from :meth:`_sync_playstate`, and **the fallback
        rather than the mechanism** -- `apply_userdata_event` applies most of
        this for free. What this covers is the stretch where nothing was
        listening, after which there is nothing to replay and only asking will
        do.

        Batched one request per ``USERDATA_BATCH`` ids per server and spaced by
        ``USERDATA_BATCH_PAUSE``; nothing is waiting on it.

        **Advance-only**, via ``db.update_userdata``: an item un-watched on
        another device stays watched here. That is the inherited rule rather
        than a decision taken here, and it is the one thing about this worth
        revisiting. See docs/offline-sync.md section 1.
        """
        try:
            rows = self.db.list(status=STATUS_COMPLETE)
        except Exception:
            log.debug("Could not list the catalog for a userdata refresh",
                      exc_info=True)
            return
        by_server = {}
        for row in rows:
            if row.get("item_id"):
                by_server.setdefault(row.get("server_uuid"), []).append(
                    row["item_id"])
        updated = 0
        sent = 0
        for server_uuid, ids in by_server.items():
            client = self.get_client(server_uuid)
            if client is None:
                continue        # still offline for this server
            for start in range(0, len(ids), USERDATA_BATCH):
                if self._stop:
                    return      # shutdown: the catalog closes behind us
                if sent:
                    # Between requests, never before the first: a sweep of
                    # one batch must not pay for spacing it does not need.
                    # Counted across servers too -- two servers' worth of
                    # batches back to back is the same burst from this
                    # machine's uplink even though neither server sees it.
                    self._wake.wait(USERDATA_BATCH_PAUSE)
                    if self._stop:
                        return
                sent += 1
                batch = ids[start:start + USERDATA_BATCH]
                try:
                    # `fields=""`, not the apiclient's default: that default
                    # is info(), 29 fields including MediaSources, and this
                    # wants exactly one field group -- UserData, which comes
                    # back whatever Fields says. Measured against 12.0 for
                    # 60 ids: 73 ms and 191 KB with the default, 13 ms and
                    # 60 KB without it, for byte-identical UserData.
                    result = client.jellyfin.get_items(batch, fields="") or {}
                except Exception:
                    log.debug("Userdata refresh failed for %s", server_uuid,
                              exc_info=True)
                    break
                for item in result.get("Items") or []:
                    data = item.get("UserData") or {}
                    if not item.get("Id") or not data:
                        continue
                    try:
                        if self.db.update_userdata(
                                item["Id"],
                                played=data.get("Played") or None,
                                position_ticks=data.get(
                                    "PlaybackPositionTicks")):
                            updated += 1
                    except Exception:
                        log.debug("Could not store userdata for %s",
                                  item.get("Id"), exc_info=True)
        if updated:
            log.info("Refreshed watched state for %d downloaded item(s).",
                     updated)
            self._notify_change()

    def _record_permanent_failure(self, row):
        """Remember that an auto-download failed in a way that will not fix
        itself, so the scheduler stops fetching it once an hour forever.

        STATUS_ERROR alone cannot carry this. The planner's "already known"
        check is db.get, and the reaper deletes exactly these rows to reclaim
        their .part bytes — one call *before* fill() runs, in the same pass.
        So the row that was supposed to be the memory of the attempt is gone
        by the time anything consults it, and the item is still unwatched,
        still Next Up, still the lookahead anchor: re-enqueued immediately,
        re-downloaded, re-failed, every pass, for as long as the app runs.

        Only the two branches that have *judged* the failure permanent call
        this — a 4xx, and a server that keeps truncating at the same offset.
        The catch-all Exception branch deliberately does not: disk full, a
        permissions problem or a bug in us are not the item's fault, they end
        as soon as the environment is fixed, and blacklisting every episode
        that met a full disk would quietly gut auto-download with nothing to
        show for it.

        Auto rows only. The tombstone table records auto decisions, and a
        user download's failure is theirs to look at and retry.
        """
        if not is_auto(row["origin"]):
            return
        try:
            self.db.mark_discarded(row["item_id"])
        except Exception:
            log.debug("Could not record the failure of %s", row["item_id"],
                      exc_info=True)

    def _download(self, row, stopping=None):
        item_id = row["item_id"]
        client = self.get_client(row["server_uuid"])
        if client is None:
            # Now only reachable if the server went away between _run picking
            # this row and getting here — _next_runnable does the skipping.
            # No wait: returning drops straight back into the loop, which
            # skips this row and idles on the queue as a whole.
            log.warning("No client for download %s; leaving pending.", item_id)
            return
        # Deletion requested before we got here. Ahead of `_active_item`, so
        # this invocation never clears ownership it did not take.
        if self._drop_cancelled(row):
            log.info("Download cancelled before it started: %s",
                     row.get("name") or item_id)
            return
        with self._active_lock:
            self._active_item = item_id
        try:
            # A delete may have raced in just before we marked the item active
            # (it would have taken the direct path and removed the row). If the
            # row is gone, don't resurrect it.
            if not self.db.get(item_id):
                self._remove_files(row)
                return
            # Inside the try, so a catalog error here still leaves through the
            # `finally` rather than stranding `_active_item` set.
            self.db.update(item_id, status=STATUS_DOWNLOADING)
            self._notify_change()
            log.info("Downloading %s…", row.get("name") or item_id)
            item = json.loads(row["item_json"] or "{}")
            source = json.loads(row["source_json"] or "{}")
            book = is_book(item)
            if not book:
                # Prefer the PlaybackInfo MediaSource: it has DeliveryMethod /
                # DeliveryUrl and full stream details the plain item manifest
                # omits. A Book is not IHasMediaSources, so PlaybackInfo has
                # nothing to say about one -- asking would spend a round trip
                # to be told so, and log a server-side error on the way.
                pb_source = self._playback_source(client, item_id, row)
                if pb_source:
                    source = pb_source
            item_dir = self._item_dir(row)
            os.makedirs(item_dir, exist_ok=True)
            # Explicit encoding on the writes as well as the reads: the
            # output is ASCII only while nobody passes `ensure_ascii=False`,
            # and cp1252 is the default on the Windows leg.
            with open(os.path.join(item_dir, "item.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(item, fh)
            with open(os.path.join(item_dir, "source.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(source, fh)
            self._download_artwork(client, item, item_dir)
            if not book:
                # Subtitles, trickplay tiles and media segments are all
                # properties of a media source. A book has none, so each of
                # these would be a request that can only come back empty.
                self._download_subs(client, item_id, source, item_dir)
                self._download_trickplay(client, item_id, source, item_dir)
                self._download_segments(client, source, item_dir)
            if item.get("Type") == "Episode" and item.get("SeriesId"):
                self._download_series_art(client, row.get("server_id"),
                                          item["SeriesId"])
                if item.get("SeasonId"):
                    self._download_season_art(client, row.get("server_id"),
                                              item["SeasonId"])

            media_path = os.path.join(item_dir, "media." + (row["ext"] or "mkv"))
            tmp = media_path + ".part"
            url = client.jellyfin.download_url(item_id, include_apikey=False)
            expected = row.get("size_bytes") or 0
            served = {}
            size, total = self._stream(url, media_path, item_id, row.get("name"),
                                       expected, stopping=stopping,
                                       headers=self._headers_for(client, url),
                                       on_headers=served.update)
            ext = row["ext"]
            if book:
                # The response says what the file actually is, and it is the
                # only statement of it we did not have to infer. `Path` is
                # normally right and normally present, but it is metadata and
                # this is the file: a server that serves a converted or
                # renamed copy would otherwise hand the desktop an epub called
                # .pdf, which every reader refuses with a corruption error.
                # Only the *name* changes -- the bytes already on disk are
                # promoted as they are.
                served_ext = _disposition_ext(served)
                if served_ext and served_ext != ext:
                    log.info("Book %s is served as .%s (metadata said %r).",
                             row.get("name") or item_id, served_ext, ext)
                    ext = served_ext
                    media_path = os.path.join(item_dir, "media." + ext)

            # Never record a short/truncated response as complete: keep the
            # .part and leave the row pending so a later pass resumes it. Don't
            # clobber the known size_bytes with the short length. But if the
            # response keeps ending short at the same offset (no forward
            # progress), give up rather than retry forever.
            if total and size < total:
                last_size, stalls = self._short_read_stalls.get(item_id, (-1, 0))
                stalls = stalls + 1 if size <= last_size else 0
                if stalls >= 3:
                    log.error("Download of %s repeatedly ended short at %d of "
                              "%d bytes; marking failed.",
                              row.get("name") or item_id, size, total)
                    self._short_read_stalls.pop(item_id, None)
                    self.db.update(item_id, status=STATUS_ERROR,
                                   downloaded_bytes=size)
                    self._record_permanent_failure(row)
                else:
                    log.error("Download of %s ended short (%d of %d bytes); "
                              "leaving pending to resume.",
                              row.get("name") or item_id, size, total)
                    self._short_read_stalls[item_id] = (size, stalls)
                    self.db.update(item_id, status=STATUS_PENDING,
                                   downloaded_bytes=size)
                self._notify_change()
                return
            self._short_read_stalls.pop(item_id, None)

            # Commit point: promote the .part and mark complete atomically with a
            # final cancellation check under the active lock, so a delete that
            # lands after the last chunk (S4) is honoured instead of being lost
            # to a COMPLETE row. Clearing _active_item here means any delete that
            # arrives after the commit takes the direct path against the now
            # fully-downloaded item rather than the deferred-cancel path.
            rel = os.path.relpath(media_path, self.root)
            with self._active_lock:
                if self._is_cancelled(item_id):
                    raise _Cancelled()
                os.replace(tmp, media_path)
                self.db.update(item_id, status=STATUS_COMPLETE, file_path=rel,
                               downloaded_bytes=size,
                               size_bytes=size or expected,
                               ext=ext,
                               media_source_id=source.get("Id") or row.get("media_source_id"),
                               source_json=json.dumps(source),
                               # Completion time, not enqueue time: the reaper
                               # evicts oldest-first, and a row that sat in the
                               # queue for hours should age from when it landed
                               # on disk.
                               completed_at=int(time.time()))
                self._active_item = None
            log.info("Downloaded %s (%.1f MiB).", row.get("name") or item_id,
                     size / (1 << 20))
        except _Cancelled:
            # Only a log. The `finally` honours it -- and re-checks, so a
            # delete withdrawn between the raise and here is not acted on.
            log.info("Download cancelled (deleted): %s",
                     row.get("name") or item_id)
        except _Stopped:
            # App is quitting mid-download: leave it pending so it resumes next
            # launch (the .part file is kept), rather than poisoning it to error.
            log.info("Download interrupted by shutdown: %s", item_id)
            self.db.update(item_id, status=STATUS_PENDING)
        except requests.HTTPError as exc:
            # An HTTP status the server returned. 5xx/429 are transient (server
            # busy) — keep the row PENDING to resume from the .part. 4xx means
            # the item is gone or forbidden — permanent, mark ERROR.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and (status >= 500 or status == 429):
                log.warning("Download of %s got HTTP %s; will resume.",
                            row.get("name") or item_id, status)
                self.db.update(item_id, status=STATUS_PENDING)
                self._notify_change()
                raise
            log.error("Download of %s failed with HTTP %s.",
                      row.get("name") or item_id, status)
            self.db.update(item_id, status=STATUS_ERROR)
            self._record_permanent_failure(row)
        except requests.RequestException as exc:
            # Transient: a dropped connection or read timeout. Keep the row
            # PENDING so the .part resumes (resume offset is read from the file
            # on disk), and re-raise so _run's error backoff throttles the
            # retry instead of hot-looping.
            log.warning("Download of %s interrupted (%s); will resume.",
                        row.get("name") or item_id, exc)
            self.db.update(item_id, status=STATUS_PENDING)
            self._notify_change()
            raise
        except Exception:
            log.error("Download failed for %s", item_id, exc_info=True)
            self.db.update(item_id, status=STATUS_ERROR)
        finally:
            with self._active_lock:
                self._active_item = None
            # Here rather than in each handler because "the delete wins" is a
            # property of leaving this method at all, including by paths not
            # yet written. Every handler above ends by writing this row back --
            # PENDING on shutdown and on a transient network failure, ERROR on
            # the rest -- and each of them used to run over the top of a delete
            # the user had already been told had succeeded, resurrecting the
            # item on the next launch. The chunk loop only honours a cancel
            # between chunks, and `stopping()` is tested first, so quitting the
            # app during the delete of an in-flight download took that path
            # every time.
            if self._drop_cancelled(row):
                log.info("Honouring the delete of %s that arrived while the "
                         "download was unwinding.", row.get("name") or item_id)
        self._notify_change()

    def _headers_for(self, client, url):
        """Credentials for ``url``, as a headers dict, or ``{}``.

        The single way this module authenticates an outbound request. It is
        one function rather than a line at each call site because the call
        sites are the failure mode: the subtitle sidecar was converted to
        the header and the other six requests in here were not, so an
        offline download went on quietly fetching its media, artwork and
        trickplay tiles with ``?ApiKey=`` in the url. Six of those seven
        swallow their own exceptions, so against a proxy that requires the
        header they do not fail loudly -- the download completes and the
        artwork is simply absent. ``test_sync_auth_headers`` now enumerates
        the call sites so a new one cannot repeat it.

        Same-origin gated like everything else. Every url here is built by
        the apiclient from ``auth.server``, so the test is true by
        construction today; it is applied anyway because "true by
        construction today" is exactly what stops being true when someone
        threads a server-supplied path through one of these.
        """
        try:
            server = client.config.data.get("auth.server", "") or ""
            if not _same_origin(url, server):
                return {}
            return {"Authorization": client.http._get_authenication_header()}
        except Exception:
            # {} is this function's documented answer, and the whole body is
            # inside the guard so a half-built client cannot take a download
            # down with an AttributeError instead. The request that follows
            # then fails as an honest 401 rather than a traceback on a
            # background worker.
            log.debug("could not build an auth header", exc_info=True)
            return {}

    def _stream(self, url, dest, item_id, name, expected,
                stopping=None, headers=None, on_headers=None):
        """Download `url` to `dest`.part, resuming a partial file where possible.

        Returns ``(downloaded, total)``. The caller promotes the .part to `dest`
        (see _download's commit point) — this only fills the .part so a final
        cancellation check can still discard it. `total` is the best-known full
        size (size_bytes or Content-Length) for the short-read guard, or 0.
        """
        tmp = dest + ".part"
        resume = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        # A prior run may have died between the stream finishing and the
        # promotion, leaving a full-size .part. Re-requesting with
        # Range: bytes=<full>- makes the server answer 416; instead, promote
        # what's already on disk (S6).
        if expected and resume >= expected:
            return expected, expected
        try:
            return self._stream_request(url, tmp, item_id, name, expected,
                                        resume, stopping, headers, on_headers)
        except requests.HTTPError as exc:
            resp = getattr(exc, "response", None)
            if resp is None or resp.status_code != 416:
                raise
            # Range not satisfiable. If the .part already matches the expected
            # size it really is complete; otherwise it's stale/over-long — drop
            # it and restart the download from the beginning (S6).
            if expected and resume == expected:
                return expected, expected
            log.info("Resume offset rejected (416); restarting %s from scratch.",
                     name or item_id)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return self._stream_request(url, tmp, item_id, name, expected,
                                        0, stopping, headers, on_headers)

    def _stream_request(self, url, tmp, item_id, name, expected,
                        resume, stopping=None, headers=None, on_headers=None):
        verify = not settings.ignore_ssl_cert
        # Range and Authorization both, not one or the other: this used to
        # build the dict from scratch here, which is why the resume header
        # arrived and the credentials did not.
        headers = dict(headers or {})
        if resume:
            headers["Range"] = "bytes=%d-" % resume
        with requests.get(url, stream=True, headers=headers, verify=verify,
                          timeout=(10, 60)) as resp:
            if resume and resp.status_code == 200:
                resume = 0  # server ignored Range; restart cleanly
            resp.raise_for_status()
            if on_headers is not None:
                on_headers(resp.headers)
            total = expected or (int(resp.headers.get("Content-Length", 0)) + resume)
            downloaded = resume
            last_push = downloaded
            mode = "ab" if resume else "wb"
            with open(tmp, mode) as fh:
                stopping = stopping or (lambda: self._stop)
                for chunk in resp.iter_content(CHUNK):
                    if stopping():
                        raise _Stopped()
                    if self._is_cancelled(item_id):
                        raise _Cancelled()
                    if not chunk:
                        continue
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if downloaded - last_push >= PROGRESS_STEP:
                        self.db.update(item_id, downloaded_bytes=downloaded)
                        try:
                            self.on_progress(item_id, name, downloaded, total)
                        except Exception:
                            pass
                        last_push = downloaded
        return downloaded, total

    def _download_trickplay(self, client, item_id, source, item_dir):
        """Download trickplay (scrubbing preview) tiles for offline use."""
        api = client.jellyfin
        try:
            full = api.get_item(item_id, fields="Trickplay") or {}
        except Exception:
            return
        manifest = (full.get("Trickplay") or {}).get(source.get("Id")) or {}
        widths = []
        for key in manifest.keys():
            try:
                widths.append(int(key))
            except ValueError:
                pass
        if not widths:
            return
        prefer = settings.thumbnail_preferred_size or 320
        width = min(widths, key=lambda w: abs(w - prefer))
        data = manifest[str(width)]
        try:
            tiles = math.ceil(
                data["ThumbnailCount"] / data["TileWidth"] / data["TileHeight"])
        except Exception:
            return

        verify = not settings.ignore_ssl_cert
        tp_dir = os.path.join(item_dir, "trickplay", str(width))
        os.makedirs(tp_dir, exist_ok=True)
        for i in range(tiles):
            url = api.trickplay_tile_url(item_id, width, i, source.get("Id"),
                                         include_apikey=False)
            try:
                resp = requests.get(url, timeout=(10, 30), verify=verify,
                                    headers=self._headers_for(client, url))
                resp.raise_for_status()
                with open(os.path.join(tp_dir, "%d.jpg" % i), "wb") as fh:
                    fh.write(resp.content)
            except Exception:
                log.debug("Trickplay tile %d failed for %s", i, item_id,
                          exc_info=True)
                return
        with open(os.path.join(item_dir, "trickplay.json"), "w") as fh:
            json.dump({"width": width, "data": data}, fh)
        log.debug("Downloaded %d trickplay tiles for %s.", tiles, item_id)

    def _download_segments(self, client, source, item_dir):
        """Cache the item's media segments (intro, outro, …) for offline use.

        Best-effort, like the trickplay tiles beside it: segments come from a
        plugin, so most items legitimately have none and a server without the
        plugin answers nothing at all. A failure here must not fail the
        download.

        **Every type, not the ones the settings currently want.** What is on
        disk outlives the setting that was set when it was written -- turning
        Recap on months later must not require re-downloading -- so the
        filtering happens at playback, where conf.segment_action already does
        it for the online path.
        """
        try:
            data = client.jellyfin.get_media_segments(source.get("Id"))
        except Exception:
            log.debug("No media segments for %s", source.get("Id"),
                      exc_info=True)
            return
        items = (data or {}).get("Items") or []
        if not items:
            return
        try:
            with open(os.path.join(item_dir, "segments.json"), "w") as fh:
                json.dump(items, fh)
        except OSError:
            log.debug("Could not write segments.json", exc_info=True)
            return
        log.debug("Downloaded %d media segments for %s.", len(items),
                  source.get("Id"))

    def _download_series_art(self, client, server_id, series_id):
        """Cache series poster/backdrop so offline series tiles + the series page
        have artwork (episodes only carry their own images)."""
        series_dir = os.path.join(self.root, server_id or "server", "series",
                                  series_id)
        poster = os.path.join(series_dir, "poster.jpg")
        backdrop = os.path.join(series_dir, "backdrop.jpg")
        if os.path.exists(poster) and os.path.exists(backdrop):
            return
        api = client.jellyfin
        verify = not settings.ignore_ssl_cert
        os.makedirs(series_dir, exist_ok=True)
        jobs = []
        if not os.path.exists(poster):
            jobs.append((poster, api.artwork(series_id, "Primary", 600,
                                             include_apikey=False)))
        if not os.path.exists(backdrop):
            jobs.append((backdrop, api.artwork(series_id, "Backdrop", 1280,
                                               include_apikey=False)))
        for path, url in jobs:
            try:
                resp = requests.get(url, timeout=(10, 30), verify=verify,
                                    headers=self._headers_for(client, url))
                resp.raise_for_status()
                with open(path, "wb") as fh:
                    fh.write(resp.content)
            except Exception:
                log.debug("Series art failed: %s", url, exc_info=True)

    def _download_playlist_art(self, client, server_id, playlist_id):
        """Cache the playlist's own poster so its offline tile has artwork.

        A playlist carries its own image; the tile used to borrow a member's
        poster, which meant a playlist whose first member had no art on disk
        showed a bare glyph."""
        pl_dir = os.path.join(self.root, server_id or "server", "playlist",
                              playlist_id)
        poster = os.path.join(pl_dir, "poster.jpg")
        if os.path.exists(poster):
            return
        os.makedirs(pl_dir, exist_ok=True)
        url = client.jellyfin.artwork(playlist_id, "Primary", 600,
                                      include_apikey=False)
        try:
            resp = requests.get(url, timeout=(10, 30),
                                verify=not settings.ignore_ssl_cert,
                                headers=self._headers_for(client, url))
            resp.raise_for_status()
            with open(poster, "wb") as fh:
                fh.write(resp.content)
        except Exception:
            # Playlists without an image are normal — the tile falls back to
            # its glyph, same as online.
            log.debug("Playlist art failed: %s", url, exc_info=True)

    def _playback_source(self, client, item_id, row):
        """Resolve the full MediaSource via PlaybackInfo (metadata only)."""
        try:
            info = client.jellyfin.get_play_info(
                item_id, get_profile(is_remote=False), is_playback=False,
                media_source_id=row.get("media_source_id"))
        except Exception:
            log.debug("PlaybackInfo failed for %s; using item manifest.",
                      item_id, exc_info=True)
            return None
        sources = (info or {}).get("MediaSources") or []
        if not sources:
            return None
        msid = row.get("media_source_id")
        return next((s for s in sources if s.get("Id") == msid), sources[0])

    def _download_season_art(self, client, server_id, season_id):
        """Cache season poster so offline season tiles have artwork."""
        season_dir = os.path.join(self.root, server_id or "server", "season",
                                  season_id)
        poster = os.path.join(season_dir, "poster.jpg")
        if os.path.exists(poster):
            return
        os.makedirs(season_dir, exist_ok=True)
        verify = not settings.ignore_ssl_cert
        url = client.jellyfin.artwork(season_id, "Primary", 600,
                                      include_apikey=False)
        try:
            resp = requests.get(url, timeout=(10, 30), verify=verify,
                                headers=self._headers_for(client, url))
            resp.raise_for_status()
            with open(poster, "wb") as fh:
                fh.write(resp.content)
        except Exception:
            log.debug("Season art failed for %s", season_id, exc_info=True)

    def _download_artwork(self, client, item, item_dir):
        api = client.jellyfin
        tags = item.get("ImageTags") or {}
        jobs = []
        if "Primary" in tags:
            jobs.append(("poster.jpg", api.artwork(item["Id"], "Primary", 600,
                                                   include_apikey=False)))
        if item.get("BackdropImageTags"):
            jobs.append(("backdrop.jpg", api.artwork(item["Id"], "Backdrop", 1280,
                                                     include_apikey=False)))
        if "Thumb" in tags:
            jobs.append(("thumb.jpg", api.artwork(item["Id"], "Thumb", 600,
                                                  include_apikey=False)))
        verify = not settings.ignore_ssl_cert
        for name, url in jobs:
            try:
                resp = requests.get(url, timeout=(10, 30), verify=verify,
                                    headers=self._headers_for(client, url))
                resp.raise_for_status()
                with open(os.path.join(item_dir, name), "wb") as fh:
                    fh.write(resp.content)
            except Exception:
                log.debug("Artwork %s failed for %s", name, item.get("Id"),
                          exc_info=True)

    def _download_subs(self, client, item_id, source, item_dir):
        """Fetch every external subtitle as a sidecar (subs/<index>.<fmt>).

        The cached source (from get_item) usually has no DeliveryUrl, so we build
        the subtitle stream URL ourselves. Embedded subtitles ride along inside
        the downloaded original file and need no sidecar.
        """
        server = client.config.data.get("auth.server", "").rstrip("/")
        verify = not settings.ignore_ssl_cert
        media_source_id = source.get("Id") or item_id
        subs_dir = os.path.join(item_dir, "subs")
        for stream in source.get("MediaStreams") or []:
            if stream.get("Type") != "Subtitle" or not stream.get("IsExternal"):
                continue
            index = stream.get("Index")
            if index is None:
                continue
            fmt = _sub_format(stream.get("Codec"))
            delivery = stream.get("DeliveryUrl")
            external = bool(stream.get("IsExternalUrl"))
            if delivery:
                url = delivery if external else (server + delivery)
            else:
                url = client.jellyfin.subtitle_url(
                    item_id, media_source_id, index, fmt,
                    include_apikey=False)
            # We issue this request ourselves, so the token goes in a header
            # rather than the query string -- no token in logs, in ps output
            # or in any proxy in the path.
            #
            # But only to our own server. IsExternalUrl does NOT mean "third
            # party": it means the stream's Path was already an absolute
            # http(s) URI, so the server handed that over instead of
            # proxying it (StreamInfo.cs:1264-1274). That host is often the
            # same one -- a plugin, a co-located file server -- and
            # sometimes not, and the DTO does not say which. So the test is
            # the origin, not the flag: same host as the server we are
            # logged in to, send the header; anything else, send nothing.
            #
            # The old code attached api_key to these unconditionally, which
            # handed our access token to whatever host the path named.
            try:
                os.makedirs(subs_dir, exist_ok=True)
                resp = requests.get(url, timeout=(10, 30), verify=verify,
                                    headers=self._headers_for(client, url))
                resp.raise_for_status()
                with open(os.path.join(subs_dir, "%s.%s" % (index, fmt)), "wb") as fh:
                    fh.write(resp.content)
                log.debug("Downloaded subtitle stream %s (%s).", index, fmt)
            except Exception:
                log.debug("Subtitle download failed for stream %s",
                          index, exc_info=True)


syncManager = SyncManager()
