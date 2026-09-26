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
from ..constants import APP_NAME, OFFLINE_SERVER_UUID
from ..i18n import _
from ..utils import get_profile
from .auto import AutoDownloader
from .db import (ANY_SERVER, NO_ACTOR, STORE_DIR, SyncDB, STATUS_PENDING,
                 STATUS_DOWNLOADING, STATUS_COMPLETE, STATUS_ERROR,
                 ORIGIN_USER, is_auto, item_dir, legacy_playlist_art_dir,
                 playlist_art_dir, season_art_dir, series_art_dir)

log = logging.getLogger("sync.manager")

# -- Reading `ServerId` off a raw DTO -----------------------------------------
#
# Several places here take the content server straight from the item the server
# sent, rather than from `content_id_for(login)`: it is the right key for a
# question *about that DTO*, and the wrong one is what CR8 removed. This note
# is the one place that says what a **falsy** answer means, because it used to
# mean two opposite things sixteen lines apart -- "match no rows" at
# `is_complete`, and "clear the tombstone on every server" at the two
# `_clear_discard` calls beside it. Both shipped in `d00daafc`, unremarked.
#
# **They agree now, and the agreement is the point:** a falsy `ServerId` means
# *no effect anywhere*. Nothing matches (`SyncDB._content_clause`, whose NULL
# branch step 5 removed) and nothing is cleared (`SyncDB.clear_discarded`, which
# has no broad row left to clear since the tombstones became one scoped table).
#
# It is also **expected never to happen**: `_add_row` refuses a DTO that names
# no server, loudly, measured against a real server across every downloadable
# type including under a restricted `Fields` list. The sites below cite this
# note rather than restating it; if a third meaning is ever wanted, it goes here
# first.

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
#: It also paces the first real sweep, now that a pass with no client to ask
#: leaves the trigger up rather than consuming it (`_sweep_if_due`; why that
#: is not "a sweep that found nothing": docs/offline-sync.md section 3).
USERDATA_SWEEP_SETTLE = 60

#: How long a reap may be held waiting for a sweep that has not landed.
#: The guarantee is "succeeded, with a bound", and this is the bound. A constant
#: rather than a setting: it is the width of a failure window, not a
#: preference. One auto interval is too coarse to bound anything (it is an
#: hour by default) and USERDATA_SWEEP_FLOOR is five minutes, which a server
#: that is merely slow to come back would exceed for reasons that are not a
#: failure. docs/offline-sync.md section 4.
REAP_SWEEP_HOLD = 900

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


def normalize_root(path):
    """Clean up a hand-entered download folder, or None for "the default".

    The field is typed into, and on Windows it is typed into by pasting:
    Explorer's Copy as path and the address bar's context menu both hand over
    a **quoted** path, and every measured refusal of one was
    `Can't create that folder` -- a message about permissions for a path that
    was only ever mis-spelled. Stripping is not cosmetic there, because a
    double quote is not a legal filename character on NTFS, so the quoted
    spelling can never name anything.

    Applied by `start()` as well as `relocate()`: `sync_path` is a JSON key a
    person can edit by hand, and a root the app declines to normalize is a
    root it silently treats as a different folder from the one the settings
    field will show.
    """
    if not path:
        return None
    text = str(path).strip()
    for quote in ('"', "'"):
        if len(text) >= 2 and text[0] == quote and text[-1] == quote:
            text = text[1:-1].strip()
            break
    return text or None


#: What `_destination_is_writable` writes to prove it can. Named once
#: because the emptiness check has to recognise it: the probe tolerates its
#: own removal failing, and a leftover then refuses the user's chosen folder
#: as "not empty" over a dotfile only this app writes.
WRITE_PROBE_NAME = ".jellyfin-mpv-shim-write-test"


def same_directory(a, b):
    """Do these two paths name the same directory?

    `==` is the wrong test on Windows, where the filesystem is
    case-insensitive: a user who retypes their own download folder with the
    drive letter in the other case fell past the equality check and into the
    *containment* one, which answered -- correctly, and uselessly -- that the
    folder is inside itself. `normcase` is the per-platform answer to
    "would the filesystem call these the same name", and `realpath` is what
    makes a junction or a symlink to the store resolve to the store.
    """
    if not a or not b:
        return False
    try:
        return (os.path.normcase(os.path.realpath(a))
                == os.path.normcase(os.path.realpath(b)))
    except OSError:
        return False


class _Stopped(Exception):
    """Raised inside the worker when the app is shutting down mid-download."""


class DownloadCollision(Exception):
    """Every item asked for is already held under a different server.

    Item ids are not unique across servers (docs/jellyfin-api-notes.md 13b)
    and `downloads.item_id` is the catalog-wide primary key, so the second
    server's copy cannot be held alongside the first. Raised rather than
    returning zero because the gateway turns an exception into the failure
    line the user sees, and a silent no-op is how "Download" came to look
    like it had worked.
    """


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


def _requested_user_id(server_uuid):
    """The Jellyfin ``UserId`` behind a saved login, or ``NO_ACTOR``.

    Only the person: the server half of `requested_by` is taken from the item
    being enqueued, which names it, so this cannot disagree with the row about
    which server it belongs to.

    Never raises. A login that will not resolve is recorded as `NO_ACTOR`
    rather than refused, because the row is about to describe a file on disk.
    """
    try:
        actor = _actor_for(server_uuid)
    except Exception:
        log.debug("could not resolve who is asking for a download",
                  exc_info=True)
        return NO_ACTOR
    return (actor[1] if actor and actor[1] else NO_ACTOR)


def _actor_for(server_uuid):
    """Who a saved login belongs to, for `SyncDB`'s userdata migration.

    A module function rather than a bound method so the catalog can be
    handed a resolver without being handed the manager. Imported per call:
    `users` reaches back into this package, and a module-scope import here
    is a cycle.

    **Loads the registry itself rather than trusting a caller to have done
    it.** `userManager.users` is empty until `load()` runs, whose only caller
    is `clientManager.load_credentials` from `login_servers()` -- which
    `mpv_shim.main` reaches *after* `syncManager.start()` has already opened
    and migrated the catalog. So this answered `None` for every login on a
    real launch, and a migration that drops what it cannot attribute deleted
    every queued offline playstate entry on upgrade. `load()` is idempotent
    and reads one file, so asking here costs a flag check and makes the
    guarantee independent of anyone's call order.
    """
    from ..users import userManager
    try:
        userManager.load()
        return userManager.actor_for(server_uuid)
    except Exception:
        log.debug("could not resolve the actor for %s", server_uuid,
                  exc_info=True)
        return None


def _registry_unreadable():
    """Did `users.json` exist and fail to parse, with nothing to restore?

    **One predicate, asked in two places**, because the two answers must never
    disagree: `start` refuses to open the catalog on it, and `_actor_resolver`
    withholds the resolver on it. Two spellings of "is the registry usable"
    is how a subsystem ends up half-running.

    Note what it is *not*: being offline. A launch with no network reads the
    same registry as any other. R12 was about corruption -- [iw], on being
    shown that not loading also confiscates offline playback: *"that was
    directed at 'the critical config files for corrupted not it is offline'."*

    `load()` is idempotent and, since the registry gained a backup, answers
    True only when the primary **and** the copy are both unusable.
    """
    from ..users import userManager
    userManager.load()
    return bool(userManager.load_failed)


def _names_an_account(asked_by):
    """Does this ``(server, user)`` pair name a person?

    `NO_ACTOR` and the empty string both mean "could not say who", which is a
    different thing from a person: `db.upsert` substitutes the column default
    for a writer that omits the pair, and `_backfill_requested_by` fills the
    rest in with `NO_ACTOR` where the enqueuing login no longer resolves.
    """
    return bool(asked_by[1]) and asked_by[1] != NO_ACTOR


def _actor_resolver():
    """The resolver to hand `SyncDB`, or **None when the registry is unreadable**.

    `SyncDB`'s migrations already draw the right distinction -- *"cannot ask, so
    has not been told nobody"* -- but they draw it on the resolver being
    **absent**, and a resolver that answers `None` for everything looks exactly
    like one that has been told nobody. A resolver that *raises* is no better:
    the migration catches that and drops the entry too.

    **Kept as the second line, not the first.** Since R12, `start` refuses to
    open the catalog at all when the registry is unreadable, so in a running
    app this branch is unreachable -- and that is the point rather than a
    redundancy to tidy away: what made the `''` sentinel reachable was a
    catalog that opened and migrated with nobody to ask. Tests drive `SyncDB`
    directly, and they are the remaining caller of the withheld path.
    """
    if _registry_unreadable():
        log.warning("The saved logins could not be read, so the catalog "
                    "migration is not attributing anything this launch.")
        return None
    return _actor_for


class SyncManager:
    def __init__(self):
        self.db = None
        self.root = None
        #: Set when `start` declined to open the catalog, so the rest of the
        #: app can say why instead of showing an empty download list as
        #: though nothing had ever been downloaded. None when the subsystem
        #: is running normally, which is every ordinary launch. See R12 and
        #: `_registry_unreadable`.
        self.unavailable = None
        self.get_client = lambda server_uuid: None
        self.on_change = lambda: None
        self.on_progress = lambda item_id, name, downloaded, total: None
        # Built in start(); None until then so the worker loop (which tests
        # drive directly, without start()) stays safe.
        self.auto = None

        #: (Jellyfin ServerId, series/item id) -> the CollectionFolder it
        #: lives in, for _library_id_for. Positive answers only; see there.
        #: The server is in the key because item ids are only unique within
        #: one -- Jellyfin derives them from the media's path.
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
        #: worker generation -> the item that worker is downloading. A dict
        #: rather than one slot because two workers can be live at once; see
        #: `_claim_active`.
        self._active = {}
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
        #: Accounts whose downloaded rows a sweep has fully refreshed this
        #: session -- every batch covering them returned. Read only by
        #: `_sweep_owed`, and the reason it is "answered" rather than "swept":
        #: `_refresh_userdata` clears `_sweep_due` before it asks anything and
        #: swallows each server's failures, so "a sweep happened" is true of a
        #: pass that refreshed nothing.
        #:
        #: **The account, as ``(ServerId, UserId)``, not the server** (D1).
        #: The sweep asks a server as one signed-in login and files the
        #: answers under that login's account, so the account is what an
        #: answer belongs to -- and a profile switch then stops the previous
        #: person's answer counting *by construction*. That is what let
        #: `request_profile_sweep`, and the epoch race between its clear and
        #: a pass in flight, be deleted instead of repaired.
        self._answered_accounts = set()
        #: Monotonic deadline for the hold above, set the first time a reap
        #: is held and cleared when it releases. None while nothing is held.
        self._reap_hold_until = None
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
        working, in which case auto-download simply finds no servers.

        **Refuses outright when the saved logins cannot be read.** R12: *"If
        users.json is broken, don't load the sync subsystem."* Not a
        precaution -- it is what makes a whole class of defect unreachable.
        Everything this subsystem does is filed under a person: a catalog
        opened with nobody to ask still runs its migrations, still stamps the
        rows it cannot attribute with an empty actor, and still runs the
        sweep that deletes them for not matching a real server id. Both
        review rounds found that pair independently. Not opening removes the
        state they need rather than guarding the two sites that read it.

        The cost is this launch's downloads: `offline_video_factory` answers
        None with no catalog, so playback falls back to streaming, and the
        download list is empty. That is deliberate and it is bounded -- the
        registry now keeps a backup, so reaching here means the primary and
        the copy are both unusable, and the bytes of both are on disk to be
        looked at.
        """
        if _registry_unreadable():
            self.unavailable = "registry"
            log.error("Not starting the download subsystem: the saved logins "
                      "could not be read. Downloads are unavailable this "
                      "launch, and nothing in the catalog will be touched.")
            return
        self.unavailable = None
        self.get_client = get_client
        if get_clients is not None:
            self.get_clients = get_clients
        self.auto = AutoDownloader(self, get_clients=get_clients,
                                   is_busy=is_busy,
                                   should_stop=lambda: self._stop)
        self.root = (normalize_root(settings.sync_path)
                     or os.path.join(confdir(APP_NAME), "offline"))
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
        #
        # **Except one that a worker is streaming right now.** On an ordinary
        # launch there is no such worker and this requeues everything, which
        # is the point. Reached from `relocate`'s refusal path there is: a
        # worker that outlived the `stop()` asking it to quit still has that
        # row's `.part` open, and requeuing it hands the same row to the
        # replacement this is about to start -- two writers interleaving into
        # one file, which is the corruption `_generation` was introduced to
        # prevent and, as `relocate`'s own comment admits, does not cover
        # mid-`_stream`. The survivor writes the row back itself on its way
        # out, so nothing is stranded by waiting for it.
        in_flight = self._active_ids()
        for row in self.db.list(status=STATUS_DOWNLOADING):
            if row["item_id"] in in_flight:
                log.info("Leaving %s downloading: a worker still has it.",
                         row["item_id"])
                continue
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
                # Before the reconcile, because a homed row is one the sweep
                # and the download door can both reason about -- and after
                # the store's own pass, which has already tried the column.
                self.home_orphans_from_manifests()
                # After the homing, not before: a row rescued from its
                # manifest is syncable again, and pruning first would throw
                # away the entries that just became deliverable.
                self.db.drop_unsyncable_playstate()
            except Exception:
                log.debug("Homing orphans from manifests failed.",
                          exc_info=True)
            try:
                # The rows a restore is missing come back on the next
                # sweep-eligible launch; the files must survive until then.
                self._reconcile_disk(sweep_orphans=catalog is CATALOG_TRUSTED)
            except Exception:
                log.debug("Startup disk reconcile failed.", exc_info=True)
            try:
                # After the reconcile, because it reads the catalog's playlist
                # rows and the homing above is what gives them their servers.
                self._rehome_playlist_art()
            except Exception:
                log.debug("Re-homing the playlist art failed.", exc_info=True)
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

        Read-only, and asked **before** anything opens it writable: a writable
        open is not a read, since `SyncDB.__init__` runs the schema, so on a
        zero-byte file it *creates* the tables and `healthy()` then says yes of
        an empty catalog. Reuses `SyncDB(read_only=True).healthy()` so "can the
        rows be read" keeps one implementation. Why a store described by
        nothing is unrecoverable: docs/offline-sync.md section 5.
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

        Separate from `_catalog_reads` because they can disagree -- the probe
        reads `downloads`, the constructor's schema touches every table -- and
        because a failure here must reach the caller as a verdict rather than
        an exception, or the backup beside the damaged file is never restored.
        """
        try:
            return SyncDB(catalog_path, actor_for=_actor_resolver())
        except sqlite3.Error:
            log.warning("The download catalog at %s could not be opened.",
                        catalog_path, exc_info=True)
            return None

    def _open_catalog(self, catalog_path):
        """Open the catalog, restoring the backup if it is unreadable or gone.

        Returns what the caller may do with the disk -- `CATALOG_TRUSTED`,
        `CATALOG_BEHIND` or `CATALOG_ABSENT` -- rather than leaving it to be
        inferred from whether the file exists, which stops being the same
        question the moment a missing catalog can be restored. What is lost
        with the catalog, and the four mechanisms that keep it:
        docs/offline-sync.md section 5.

        Locking is not the failure mode being covered. Single-instance
        election means one writer, and the browser's handle is read-only.
        This is for the file itself: a power cut mid-write, a bad sector, a
        network or removable filesystem that lied about a flush.
        """
        backup_path = os.path.join(os.path.dirname(catalog_path),
                                   self.CATALOG_BACKUP)
        # A catalog that is GONE is the same emergency as one that cannot be
        # read, and reachable: a killed `_move_tree`, a failed restore, a user
        # tidying up.
        missing = not os.path.exists(catalog_path)
        if missing and not os.path.exists(backup_path):
            self.db = SyncDB(catalog_path, actor_for=_actor_resolver())  # first run
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
                # Read-only is the one open that cannot create or migrate, so
                # the damaged file stays as it is and `healthy()` keeps
                # answering false -- which is what holds the sweep off the disk.
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
            # **Never a writable empty catalog where the evidence used to be**
            # -- an empty catalog is *readable*, so no later launch would retry
            # the restore (docs/offline-sync.md section 5). Read-only cannot
            # create one: with no file it holds no connection at all.
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

        One path for both emergencies -- unreadable and missing -- since they
        differ only in whether there is a bad file to set aside; kept as one
        function because two branches drifted (docs/offline-sync.md section 5).

        The order is load-bearing:

        1. Stage the copy, so a failure here has touched nothing.
        2. Move any existing catalog aside; `.recover` in the sqlite shell can
           still read it until we delete it.
        3. Move the `-wal`/`-shm` off the live name **whether or not there was
           a catalog to set aside** -- a missing catalog can still have its WAL
           beside it, and a WAL may hold pages newer than the file it belongs
           to. **Failing to shift one aborts the restore**: it is the one step
           here whose success step 4 assumes rather than prefers.
        4. Promote the staged copy.

        A failure anywhere leaves the catalog **absent**, which `_open_catalog`
        must not paper over. Restoring the aside on failure was tried and
        dropped: no outcome differed, and a second mechanism for one rule is
        how the two drift apart.
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
                    # sqlite replays a WAL into whatever takes that name
                    # next, checking only that the WAL is internally
                    # consistent -- never that it belongs to that file. So any
                    # error but ENOENT leaves a sidecar standing and must
                    # abort: swallowed, it discarded four rows in five and read
                    # back integrity-clean.
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

        **An empty catalog never replaces a backup that has rows in it**, or
        the backup deletes itself on exactly the launch it exists for
        (docs/offline-sync.md section 5).

        The cost is a stale backup after somebody deletes every download --
        bounded, and cleared by the next launch or relocate that opens a
        catalog with rows. Losing the only index of a full download folder does
        not clear at all.
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
        if self.unavailable:
            # `self.root` is None here, and `same_directory(None, ...)` is not
            # the error anyone would want to read. Refusing is also correct on
            # its own terms: moving a store whose catalog was never opened
            # would leave the rows describing it untouched.
            return False, _("Downloads are unavailable: the saved logins "
                            "could not be read.")
        old_root = self.root
        new_path = normalize_root(new_path)
        if new_path:
            new_root = os.path.abspath(os.path.expanduser(new_path))
        else:
            new_root = os.path.join(confdir(APP_NAME), "offline")
        if same_directory(old_root, new_root):
            # Truthfully, rather than as a successful move. The caller shows
            # `message or "Download folder moved"`, so an empty string here
            # claimed a move that never happened -- which is exactly how a
            # dead Move button read as a working one.
            return True, _("The downloads are already in that folder.")
        if self._active_ids():
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
            # Our own leftover probe does not count. It is there because a
            # previous attempt got exactly this far and could not clean up
            # after itself, and refusing over it strands the user on the
            # folder they picked, with nothing visible in it to explain why.
            # Only this exact name, so somebody else's marker file (a
            # `.stfolder`, a `.nomedia`) still means "not ours".
            existing = [n for n in os.listdir(new_root)
                        if n != WRITE_PROBE_NAME] \
                if os.path.isdir(new_root) else []
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
        # **An existing folder answers `makedirs(exist_ok=True)` without ever
        # being written to**, so a destination the user can list and not write
        # -- another account's folder, a read-only share, a drive mounted ro --
        # got all the way past every check here, stopped the download worker,
        # closed the catalog, and only then failed, as the generic "moving
        # failed". Writing one file is the only question that was actually
        # being asked, and asking it costs nothing while everything is still
        # running.
        if not self._destination_is_writable(new_root):
            return False, _("This app isn't allowed to write to that folder. "
                            "Pick one you own, or change its permissions, "
                            "and try again.")
        # Stop the worker and close the catalog so nothing is open mid-move.
        # _relocating keeps enqueue/delete off the (closed) catalog until we
        # reopen at the destination.
        self._relocating = True
        if not self.stop():
            # The worker is still alive and still holds an open .part handle.
            # The `_active_ids` check above is not enough on its own: it is
            # sampled before stop(), and the chunk loop only notices _stop
            # between chunks -- a stalled connection parks it in a socket read
            # for up to the 60s read timeout. Moving the tree out from under
            # that handle means the abandoned worker keeps appending while
            # _open_and_run starts a SECOND worker on the same rows at the new
            # root: two writers interleaving into one .part, which is the
            # corruption _generation was introduced to prevent and does not
            # cover mid-_stream.
            #
            # The reopen below is what would have handed that row over --
            # `start()` requeues every DOWNLOADING row, which is right on an
            # ordinary launch and wrong here. It now leaves the one the
            # survivor still holds alone, and `_release_active` keeps that
            # survivor from freeing the replacement's claim on its way out.
            # Both are keyed on the generation, so an ordinary launch (where
            # nothing is claimed) behaves exactly as before.
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
            # The backstop behind the pre-flight probe above, which writes one
            # file at the top of the destination and cannot speak for what is
            # under it: a per-entry denial (an antivirus holding a media file
            # open, a permission that differs inside the tree) and anything
            # that revoked access between the probe and the move both land
            # here, and "moving failed" sends people looking for a bug in this
            # app instead of at the folder they chose.
            if getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM):
                return False, _("This app wasn't allowed to write to that "
                                "folder. The downloads were left in place; "
                                "check the folder's permissions and try "
                                "again.")
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

    @staticmethod
    def _destination_is_writable(new_root):
        """Can this process actually create a file in `new_root`?

        Written and removed rather than asked about: `os.access(W_OK)` reports
        the mode bits, which on Windows do not describe an ACL at all (a
        folder denied to this account answers True) and on Linux do not
        describe a read-only mount, an immutable flag or a full quota either.
        The destination is empty by the time this runs, so the probe file is
        the only thing in it and removing it puts the folder back exactly as
        it was found.
        """
        probe = os.path.join(new_root, WRITE_PROBE_NAME)
        try:
            with open(probe, "wb") as fh:
                fh.write(b"")
        except OSError:
            return False
        try:
            os.remove(probe)
        except OSError:
            # Written but not removable is still writable, and the move is
            # what this was asking about. The file is inside a folder the
            # store is about to own, so nothing else trips over it.
            log.debug("Could not remove the write probe at %s", probe,
                      exc_info=True)
        return True

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
        # Everything is across -- **and verified** before anything is dropped.
        # The copy loop raises on a real write error, so ENOSPC is already
        # handled; what this catches is a copy that returns *without* raising
        # and is short anyway (a truncating or buffering filesystem, a chunk
        # that never lands). Nothing compared the two sides, so that case
        # removed the original and there was no other copy of it -- and a move
        # legitimately carries the user's own files alongside ours, so the
        # thing lost need not be a download at all. Sizes rather than hashes:
        # this runs over whole media trees and the failure being guarded is
        # truncation, not corruption.
        for src, dest, renamed in done:
            if renamed:
                continue
            want, got = self._tree_size(src), self._tree_size(dest)
            if want != got:
                log.error("Copy of %s is %d byte(s) where the original is %d; "
                          "keeping the original and undoing the move.",
                          src, got, want)
                self._undo_move(done)
                raise OSError(
                    errno.EIO,
                    "the copy of %s did not come out the same size as the "
                    "original" % os.path.basename(src))
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

    @staticmethod
    def content_id_for(server_uuid):
        """The catalog's content key for a saved login: a ServerId,
        ``ANY_SERVER``, or ``None``.

        **The one place a login uuid becomes a server id.** Every caller in
        the app holds the first (it is what the browser browses with, and
        what a live client is registered under) and the catalog is scoped by
        the second, so translating once here keeps the rule from being
        re-applied, differently, at each site -- which is exactly how this
        file ended up with two scoping keys in the first place.

        **Three answers, where there used to be two.** The third is the fix:

        * a ``ServerId`` -- the ordinary case, scope to that server;
        * ``ANY_SERVER`` when **no login was named at all** (a falsy uuid) or
          when the uuid is the downloads browser's pseudo-server. Both are
          structurally unscoped: there is no second server to confuse a row
          with, and the catalog is the only source of items there is;
        * ``None`` when a login *was* named and did not resolve. That used to
          be the same falsy answer as the two above, so a read asked about a
          login we do not have answered for **every** server. It now matches
          nothing.

          **A resolution result, not proof of absence.** It is whatever
          `server_id_for` returns, which is also `None` for a credential that
          is present but carries no ``Id``, and its scan is deliberately
          unlocked so it can miss an entry. "The registry has no such login"
          was the old wording and claimed more than the call can establish --
          which does not weaken the rule, because none of those situations is
          evidence that a particular server owns the content.

        The distinction is not theoretical: `_content_clause` serves both a
        read, where permissive is right, and a narrowing, where permissive is
        maximally wrong. Ratified 2026-09-13, with all fourteen call sites
        classified first.
        """
        if server_uuid is ANY_SERVER:
            # A caller with no server to name says so here, and it travels
            # the same road as every other scope rather than round it.
            return ANY_SERVER
        if not server_uuid or server_uuid == OFFLINE_SERVER_UUID:
            return ANY_SERVER
        from ..users import userManager
        try:
            return userManager.server_id_for(server_uuid)
        except Exception:
            # Cannot ask, which is not the same as "no such login" -- the
            # registry is what failed, not the lookup. Unscoped keeps a
            # broken read showing the user their downloads.
            log.debug("could not resolve the content id for %s", server_uuid,
                      exc_info=True)
            return ANY_SERVER

    def is_complete(self, item_id, server_uuid):
        """Do we hold a finished copy, as far as this saved login is
        concerned? Translates the login to its server, like the id sets
        below -- callers outside the sync package hold a uuid, and this is
        the boundary where that becomes the catalog's content key.

        **No default.** The parameter used to default to None, which read as
        "unscoped" -- so a caller that simply forgot the argument got every
        server's answer and looked correct. The two callers that genuinely
        have no server to name now say `ANY_SERVER` where the uuid goes."""
        if not self.db:
            return False
        return self.db.is_complete(
            item_id, server_id=self.content_id_for(server_uuid))

    @staticmethod
    def actor_of(*, acting_login=None, server_id=None, user_id=None):
        """Who a piece of watched state belongs to, as (ServerId, UserId).

        **The one resolver.** Three ways in, tried in that order, because
        each call site holds a different thing and resolving them
        separately is how the catalog ended up with two scoping keys:

        1. an explicit pair -- the websocket already announces one;
        2. ``acting_login``, a saved login, which a live client gives us;
        3. the server alone, which is the offline case: no client to ask, so
           the active local profile's credential for that server names the
           person at the keyboard.

        **``acting_login`` is the login DOING the thing, never one read off
        a catalog row.** A row's login names whoever downloaded the copy,
        which on a shared machine is a different person from the one at the
        keyboard -- and because it is tried before the server, it won
        wherever the downloader's credential still existed. Keyword-only
        and named for the rule so a site passing the wrong thing has to say
        so out loud.

        A login belonging to a *different* server than ``server_id`` is
        still answered **as itself** -- see the comment in the code, which
        is the C2 repair. This paragraph used to say the opposite, four
        lines above the code contradicting it: that it was "passed over
        rather than answered with". That described the pre-C2 behaviour,
        which resolved the mismatch away into a pair the store then
        accepted, and it survived the change that removed it.

        Never None. When nobody can be named it answers ``NO_ACTOR``, which
        is a real key rather than a NULL, so the progress is still recorded
        locally and simply never queued for a server -- there is no account
        to send it as. [iw]'s ruling; docs/offline-sync.md section 1.
        """
        from ..users import userManager
        try:
            if server_id and user_id:
                return (server_id, user_id)
            if acting_login:
                actor = userManager.actor_for(acting_login)
                if actor:
                    # **As itself, even when it is on another server.** This
                    # used to fall through to the profile's account on
                    # `server_id`, producing a pair that *matches* the row --
                    # so the store's check accepted it and the mark landed on
                    # a film that account has never opened. A check cannot
                    # catch a mismatch resolved away before it runs, so the
                    # acting identity has to arrive intact and be refused
                    # there. docs/offline-sync.md section 1.
                    return actor
                if not server_id:
                    server_id = userManager.server_id_for(acting_login)
            if server_id:
                found = userManager.actor_on(server_id)
                if found:
                    return (server_id, found)
        except Exception:
            log.debug("could not resolve the acting user", exc_info=True)
        return (server_id or NO_ACTOR, NO_ACTOR)

    def downloaded_item_ids(self, server_uuid):
        if not self.db:
            return set()
        return self.db.downloaded_item_ids(
            server_id=self.content_id_for(server_uuid))

    def downloaded_series_ids(self, server_uuid):
        if not self.db:
            return set()
        return self.db.downloaded_series_ids(
            server_id=self.content_id_for(server_uuid))

    def downloaded_season_ids(self, server_uuid):
        if not self.db:
            return set()
        return self.db.downloaded_season_ids(
            server_id=self.content_id_for(server_uuid))

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
        # The item's own server, like every other question asked about an
        # item we are holding a DTO for. Asked through the credential, an
        # estimate disagreed with the enqueue that followed it whenever the
        # two derivations did -- CR8.
        already = sum(1 for i in items
                      if self.db.is_complete(i.get("Id"),
                                             server_id=i.get("ServerId")))
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
        # Scoping is by Jellyfin server, so a second account on the same box
        # is not refused a film that box is already holding.
        #
        # **The login's** content id -- what this sign-in is browsing. The
        # rule that has not changed: nothing here may take one from
        # `client.config.data`. The one that used to be read from there was
        # always None, so every playlist row ever written carried a NULL
        # server and the badge scope those rows feed narrowed nothing.
        # docs/do-not-fix.md 1 holds the mechanism (CR12).
        #
        # This used to be the *only* content id here, and that was the defect:
        # a question about a row or a DTO takes the server off the DTO (see
        # the note at the top of this file), and the playlist's scope below is
        # one of those.
        content_id = self.content_id_for(server_uuid)
        items = self._expand(client.jellyfin, item_id, item_type)
        # For a playlist, capture which items already existed before this
        # download so ownership (what a later "delete playlist" may remove) goes
        # only to items this playlist actually pulls down — see _record_playlist.
        pre_existing = ({i.get("Id") for i in items if self.db.get(i.get("Id"))}
                        if item_type == "Playlist" else set())
        # **A playlist is (id, content server), and the server is its items'.**
        # Not `content_id`: that is the login's, and where the registry is
        # present but unreadable `content_id_for` answers `ANY_SERVER` -- which
        # a read takes as "every server" while all three playlist writers fold
        # it to the NULL row, so the read and the writes stopped being about
        # one row. Ownership then came back from another server's claims and
        # was written onto the unscoped playlist.
        #
        # Taken from the members because that is the value they carry
        # themselves (`_add_row`), so a playlist and its membership cannot
        # disagree about whose they are. No members, or none naming a server:
        # `None`, the representable "could not tell" row -- an unknown scope
        # is that row and never every server. Ruled 2026-09-19.
        playlist_scope = None
        if item_type == "Playlist":
            playlist_scope = next(
                (i.get("ServerId") for i in items if i.get("ServerId")), None)
            if playlist_scope is None and content_id is not ANY_SERVER:
                # **No member to take it from**, which is the emptied
                # playlist -- and that is the path `_record_playlist` deletes
                # the row on, so it has to name the row the last download
                # wrote. The login's server is the only thing left that does.
                # `ANY_SERVER` is excluded rather than passed through: "every
                # server" is the reading this whole change removes, and a
                # delete is the last place to reintroduce it.
                playlist_scope = content_id
        # Computed once, here, because this is the only place that knows it:
        # a playlist download is what `_record_playlist` recomputes ownership
        # for, so releasing a claim it already holds over a row it already has
        # would disown the copy it pulled in itself.
        claims_its_members = item_type == "Playlist"
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

        refused = 0
        for item in items:
            iid = item.get("Id")
            verdict = self.claim_identity(iid, item, may_reap=False)
            if verdict == "refused":
                # Another server already holds this id, and holds it under
                # a name: not an orphan, so `claim_identity` had no evidence
                # to weigh and would not guess. It is the *same* id because
                # Jellyfin derives one from the media's path with no server
                # component in it, and `item_id` is the catalog-wide primary
                # key -- so `_add_row`'s INSERT OR REPLACE would take the
                # other server's row, and its files, which `_item_dir` puts
                # in the same place, would be orphaned with nothing pointing
                # at them. docs/jellyfin-api-notes.md 13b.
                #
                # An ORPHAN row does not arrive here: the claim re-homes it
                # when the byte counts agree and reaps it when they do not,
                # because the user asking for a download outranks a copy the
                # catalog cannot account for. docs/offline-sync.md section 3b.
                log.warning("Not downloading %s from %s: the same id is "
                            "already held from %s, and the catalog can only "
                            "keep one copy of it.",
                            item.get("Name") or iid,
                            server_uuid, self.db.owner_of(iid))
                refused += 1
                continue
            if not include_watched and (item.get("UserData") or {}).get("Played"):
                # Before the reap and after the refusal, which is the whole
                # of the ordering: this is the one filter that says whether
                # the request wants the item at all, and it reads the
                # server's DTO rather than the catalog, so it is free.
                continue
            if verdict == "stale":
                # **Only now.** The claim found an orphan whose bytes do not
                # match and deliberately did not act: this door can delete
                # media, and what justifies that is *"the user already
                # asked for a download"*, and an item the two filters above
                # decline was never asked for. Run destructively at the top,
                # a watched orphan inside a series download was reaped and
                # then skipped -- the user lost an episode they already had,
                # and the log said "Re-downloading" for a download that
                # never happened.
                if self.claim_identity(iid, item) == "busy":
                    keep(iid)   # already coming down; nothing to queue
                    continue
            # The item's own server. `content_id` is what *this login* is
            # on, which is the right key for browsing and the wrong one for
            # a question about the DTO in hand: where the two disagree, the
            # read said "we do not hold it" about a copy we hold and the
            # door said "refused" about a row that is ours. One derivation,
            # and it is the one `_add_row` writes -- CR8.
            # A falsy `ServerId` matches no rows; see "Reading `ServerId`
            # off a raw DTO" at the top of this file.
            if self.db.is_complete(iid, server_id=item.get("ServerId")):
                # **After the reap, not before it.** The premise this comment
                # used to give is gone: a complete orphan no longer answers
                # every scope (step 5), so it answers False here and is
                # re-fetched -- which homes the row to the server that asked,
                # at the cost of downloading it again. The ordering still
                # stands on its own: a row the reap is about to remove must
                # not be reported as held, or the claim door is switched off
                # for the case it was written for.
                keep(iid)  # already downloaded → still a member
                # A user asking for something the scheduler already fetched
                # takes ownership of it, so the reaper stops considering it.
                # Never the reverse: an auto pass must not downgrade a
                # download the user asked for.
                if origin == ORIGIN_USER:
                    row = self.db.get(iid)
                    if row and is_auto(row["origin"]):
                        self.db.set_origin(iid, ORIGIN_USER)
                    # Falsy: nothing to clear. See the note at the top.
                    self._clear_discard(iid, item.get("ServerId"))
                    if not claims_its_members:
                        self._claim_from_playlists(iid)
                continue
            if origin == ORIGIN_USER:
                # Asking for it by hand overrides a previous auto discard,
                # which is the only signal that outranks the reaper. Scoped
                # by the item's server, matching the tombstone the scheduler
                # wrote from `row["content_server_id"]`; falsy clears nothing,
                # and there is nothing broad left for it to clear. See the note
                # at the top of this file.
                self._clear_discard(iid, item.get("ServerId"))
                if not claims_its_members:
                    self._claim_from_playlists(iid)
            keep(iid)
            if self._add_row(server_uuid, item, origin=origin):
                added += 1
        if item_type == "Playlist":
            self._record_playlist(server_uuid, playlist_scope,
                                  client.jellyfin, item_id, members,
                                  pre_existing)
        if added:
            log.info("Queued %d item(s) for offline download.", added)
            self._notify_change()
            self._wake.set()
        if refused and not members:
            # Only when the collision cost the whole request. A season with
            # one shared episode still fetches the other nine, and saying so
            # in the log beats refusing all ten.
            #
            # `members`, not `added`: an item already on disk, or already
            # coming down, is kept without being queued, so `added == 0` is
            # also what a request nine tenths of which was *already satisfied*
            # looks like -- and this raise is the user being told their
            # download failed (`gateway.download_enqueue` does not catch it).
            # CR7.
            raise DownloadCollision(item_id)
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

        **The only place a cancellation is acted on**, so "the delete wins" has
        one spelling rather than one per call site in `_download`.

        The sample and the row delete are **one** critical section: sampling
        under the lock and acting outside it leaves a window for `enqueue` to
        withdraw the cancel, write a fresh row, and have this delete that row
        -- the failure `_uncancel` exists to prevent, one window along. The
        files go afterwards, outside the lock: losing them costs a re-download,
        whereas losing the row is what strands the bytes.
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

    def _claim_from_playlists(self, item_id):
        """Release a playlist's claim on an item that must not be deleted.

        Two deleters can take a download the user did not ask to delete -- the
        reaper and a playlist that *owns* the item -- and `ORIGIN_USER`
        answers only the first, so both claims must be released together.
        Callers and the ownership rules: docs/offline-sync.md section 5.

        **This states one proposition and takes no exception.** Whether a
        request claims its own members is a fact about the request, held by
        `enqueue` (`claims_its_members`); deciding it here from an item type
        meant `_adopt_orphan` reading it out of a manifest some other build
        may have written.

        Best-effort, like `_clear_discard` beside it: a missing table or a
        closed catalog must not fail a download the user asked for.
        """
        try:
            self.db.disown_playlist_items(item_id)
        except Exception:
            log.debug("Could not claim %s from its playlist", item_id,
                      exc_info=True)

    def _clear_discard(self, item_id, server_id):
        """Best-effort: a missing tombstone table or a closed catalog must
        not fail a download the user asked for.

        ``server_id`` is required rather than defaulted, mirroring the
        store's own signature: a default None here is the broad clear
        arrived at by omission, which is exactly what that keyword-only
        argument exists to prevent.
        """
        try:
            self.db.clear_discarded(item_id, server_id=server_id)
        except Exception:
            log.debug("Could not clear the discard for %s", item_id,
                      exc_info=True)

    def _record_playlist(self, server_uuid, content_server_id, api, playlist_id,
                         member_ids, pre_existing):
        """Persist a downloaded playlist and its membership. An item is `owned`
        by this playlist if this download is what pulls it in (it wasn't already
        in the catalog), or it was already owned by this playlist on a prior
        download. Items that pre-existed from another route stay unowned so a
        later playlist delete leaves them (and their original grouping) intact."""
        already_owned = self.db.playlist_owned_ids(
            playlist_id, server_id=content_server_id)
        # A playlist may list the same item twice; membership is keyed by
        # item_id, so keep the first position and drop later duplicates.
        entries, seen = [], set()
        for iid in member_ids:
            if iid in seen:
                continue
            seen.add(iid)
            owned = iid in already_owned or iid not in pre_existing
            entries.append((iid, len(entries), owned))
        # Membership first, and it is what answers "is any of this offline":
        # `entries` is what we would *like* to record, and the write filters
        # it again against the catalog. Nothing supported, nothing left after
        # the filter, or a delete that landed in between all arrive here as
        # zero, and the record goes with them -- so an emptied playlist does
        # not linger in the offline UI and there is no name to fetch or art to
        # cache for it.
        if not self.db.replace_playlist_items(
                playlist_id, entries, server_id=content_server_id):
            self.db.delete_playlist(playlist_id, server_id=content_server_id)
            return
        try:
            name = (api.get_item(playlist_id) or {}).get("Name") or "Playlist"
        except Exception:
            log.debug("Failed to fetch playlist name for %s", playlist_id,
                      exc_info=True)
            name = "Playlist"
        self.db.upsert_playlist(playlist_id, content_server_id, server_uuid, name)
        try:
            # The **content** server, which goes below `playlist/` rather than
            # above it -- see `db.playlist_art_dir`, which both this and the
            # offline reader build the path with. Posters written before that
            # are moved into place by `_rehome_playlist_art` at startup.
            self._download_playlist_art(
                self.get_client(server_uuid), content_server_id, playlist_id)
        except Exception:
            log.debug("Could not cache playlist art for %s", playlist_id,
                      exc_info=True)

    def _cancel_if_active(self, item_id):
        """If the worker is downloading `item_id`, flag it for cancellation and
        let the worker do the file/row cleanup. Returns True if it was active."""
        with self._active_lock:
            # Read directly rather than through `_active_ids`, which takes
            # this same plain Lock.
            if item_id in self._active.values():
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
               only_if_auto=False, playlist_server_id=None):
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
        download outside a named series, season or playlist.

        ``playlist_server_id`` says which server's playlist, since two can
        hold one with the same id; see `_delete_playlist`."""
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
            self._delete_playlist(playlist_id, watched_only=watched_only,
                                  server_id=playlist_server_id)
            return
        rows = self.db.list(series_id=series_id) if series_id else self.db.list()
        removed = 0
        for row in rows:
            if season_id and row.get("season_id") != season_id:
                continue
            if watched_only:
                # ANY actor, not the asking one. One file on disk, so one
                # decision -- requiring every account to have watched it
                # would mean a shared machine never reclaims anything.
                # [iw]'s ruling; docs/offline-sync.md section 1.
                if not self.db.played_by_anyone(row["item_id"]):
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

    def _delete_playlist(self, playlist_id, watched_only=False,
                         server_id=None):
        """Delete one server's downloaded playlist. Only the items this
        playlist *owns* (pulled down itself) are removed from disk; items that
        were already downloaded another way stay put. The playlist record is
        then dropped.

        ``server_id`` says *whose* playlist, because two servers can hold one
        with the same id (a playlist id is a hash of its name). It is
        `None`-defaulted rather than required for one reason only: `None` is
        also the representable "could not tell" scope, so it names a real row
        rather than standing for "any". A caller that has a server and omits it
        deletes the unscoped row and leaves the one it meant -- which is a
        wrong answer rather than a broad one, and the Downloads tree carries
        the server precisely so no caller has to omit it.
        """
        owned = self.db.playlist_owned_ids(playlist_id, server_id=server_id)
        for item_id in owned:
            if watched_only and not self.db.played_by_anyone(item_id):
                continue
            self.delete_item(item_id)  # removes files + row, cleans membership
        if not watched_only:
            self.db.delete_playlist(playlist_id, server_id=server_id)
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

        **The catalog is the last resort, and it is what makes not caching
        failure safe.** `upsert` is INSERT OR REPLACE over the whole row, so
        re-queuing an item writes whatever this answers -- and without the
        fallback a re-queue during a blip would overwrite a library resolved
        back when the server was up. Asking what we already recorded is one
        indexed point query and cannot regress the answer.
        """
        item = item or {}
        lookup = item.get("SeriesId") or item.get("Id")
        if not lookup:
            return None
        # Scoped to the *server*, and **the item's server, never the asker's**.
        # A CollectionFolder id is server-wide, so two accounts on one box are
        # one answer -- but the scope has to come off the DTO, because the
        # library belongs to the item's server and not to whoever asked.
        #
        # There used to be a fallback here to `content_id_for(server_uuid)`,
        # for a DTO that names no server. It was dead -- `_add_row` is the one
        # production caller and refuses such a DTO first (`ecfd316c`) -- and it
        # was not harmless: reached through an unscoped login it answers
        # `ANY_SERVER`, and that value then becomes part of `_library_ids`'
        # **cache key**, which is one entry shared by every server. Ruled
        # 2026-09-19, after two reviewers disagreed about it.
        server_id = item.get("ServerId")
        if not server_id:
            # No scope is available and inventing one is what this removes.
            return None
        key = (server_id, lookup)
        if key in self._library_ids:
            found = self._library_ids[key]
        else:
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
                return self.db.library_id(lookup,
                                          server_id=server_id)
            # Positive answers only, which is what the promise above costs:
            # `client is None` and an ancestor list with no CollectionFolder in
            # it are blips wearing the shape of an answer, and caching either
            # one recorded NULL for every later episode of the series.
            if found:
                self._library_ids[key] = found
        return found or self.db.library_id(lookup,
                                            server_id=server_id)

    def home_orphans_from_manifests(self):
        """Give rows their content server from the manifest beside the media.

        The **second** source, after `SyncDB._backfill_content_server_id`,
        which reads the `item_json` column and skips a row whose column is
        NULL or will not parse. The same DTO is on disk next to the file --
        `_download` writes it so a download describes itself, and
        `_adopt_orphan` proves it carries `ServerId` because that is where
        adoption gets it from.

        It lives here rather than in `SyncDB` for a structural reason, not a
        stylistic one: the store is constructed with a `db_path` and no store
        root, so it cannot reach a manifest at all.

        A row this cannot home stays an orphan, which is a **recognised
        state and not an error**: it is an item downloaded locally whose
        server was later removed from the client. It plays, it records
        playstate locally, and it syncs in neither direction ([iw], 11c).
        Never deletes; returns how many were homed.
        """
        if not self.db or not self.root:
            return 0
        homed = 0
        try:
            rows = self.db.list()
        except Exception:
            log.debug("could not list the catalog to home orphans",
                      exc_info=True)
            return 0
        for row in rows:
            if row.get("content_server_id"):
                continue
            manifest = os.path.join(self._item_dir(row), "item.json")
            try:
                with open(manifest, encoding="utf-8") as fh:
                    server_id = (json.load(fh) or {}).get("ServerId")
            except (OSError, ValueError):
                continue        # locked out, deliberately, not deleted
            if not server_id:
                continue
            if self.db.home_content_server(row["item_id"], server_id):
                homed += 1
        if homed:
            log.info("Catalog: homed %d row(s) from the file beside the "
                     "media.", homed)
        return homed

    @staticmethod
    def _declared_bytes(source):
        """A positive byte count from a MediaSource, or None.

        None is *not* zero and not "no evidence is fine": a source with no
        usable `Size` -- which is every `Book`, since `Book : BaseItem` is
        not `IHasMediaSources` (docs/readers.md 27) -- yields nothing to
        compare, and C4 says an unanswerable comparison takes the safe route.
        """
        try:
            size = int((source or {}).get("Size") or 0)
        except (TypeError, ValueError):
            return None
        return size if size > 0 else None

    def _held_bytes(self, row):
        """What we actually hold, in bytes, or None.

        The file on disk rather than the row's `size_bytes`: that column is
        the size the server *declared* at enqueue and can be stale or zero,
        while the bytes cannot lie about themselves.
        """
        path = row.get("file_path")
        if not path:
            return None
        try:
            size = os.path.getsize(os.path.join(self.root, path))
        except OSError:
            return None
        return size if size > 0 else None

    def claim_identity(self, item_id, item, may_reap=True):
        """Who a downloaded copy belongs to. **The one door**, for both
        entrances, and it may reap.

        **It derives the content key itself, from the item.** It used to take
        one, and its two entrances derived it differently: `enqueue` from the
        saved credential's `Id`, `_adopt_orphan` from the DTO's `ServerId`.
        Where those disagree -- a credential whose `Id` was never written, a
        server whose ServerId was regenerated, a login resolved through a
        different local profile -- `owner == content_id` is false for every
        row we hold, so this answers "refused" for all of them, `enqueue`
        raises `DownloadCollision`, and the item can never be re-downloaded.
        The item is the right source of the two: it is the same value
        `_add_row` writes into `content_server_id`, so the question this
        asks and the answer that gets stored cannot drift apart.

        A DTO that names no server answers "refused" against any homed row,
        which is correct -- nothing can show that copy is this request's --
        and `_add_row` is where that condition is explained.

        `enqueue` and `_adopt_orphan` both write rows keyed on an item id
        that is not unique across servers, and only one of them used to
        check. Answering here rather than at each keeps the rule single, and
        it is a rule with teeth: it deletes.

        Returns one of:

        - ``free`` -- we hold no row; go ahead.
        - ``ours`` -- the row is already this server's.
        - ``refused`` -- the row names a *different* server. Ids collide, so
          this is somebody else's film and neither entrance may take it.
        - ``rehomed`` -- the row was an orphan and the evidence says it is
          the same content, so it becomes this server's where it stands. No
          re-download: the bytes are already right.
        - ``reaped`` -- the row was an orphan and the evidence does not
          agree, or there is no evidence. Row, files and local watched state
          are gone and the caller may download afresh. [iw]: *"the user
          already asked for a download, an orphan shouldn't stop it."*
        - ``stale`` -- an orphan whose evidence does not agree, asked with
          ``may_reap=False``. Nothing has been deleted; ask again when the
          deletion is actually justified. `enqueue` uses this to keep the
          *refusal* -- which has to happen before `_add_row`'s INSERT OR
          REPLACE can take another server's row -- ahead of the filters that
          decide whether it wants the item, while the *reap* stays behind
          them. What justifies the reap is "the user already asked for
          a download"; an item the request then declines was never asked for.
        - ``busy`` -- it would have reaped, but a worker is writing into that
          directory now. Left alone; the caller queues nothing, because the
          copy being asked for is already on its way. This path used to
          rmtree a directory mid-write, and the worker then carried on into
          it and updated a row that no longer existed.

        **The evidence is bytes, not metadata.** An id collision already
        proves the .NET type and the path -- that is the entire derivation
        -- so `Type` adds nothing, and `Name` is editable while two encodes
        of one film routinely share a runtime. What separates "the file was
        upgraded in place" from "a different film at the same path on
        another install" is its length. Absent on either side, the answer is
        `reaped`, because reading missing evidence as agreement is precisely
        the silent failure docs/jellyfin-api-notes.md 13b describes.
        """
        content_id = item.get("ServerId")
        row = self.db.get(item_id)
        if row is None:
            return "free"
        owner = row.get("content_server_id")
        if owner:
            return "ours" if owner == content_id else "refused"
        want = self._declared_bytes((item.get("MediaSources") or [{}])[0])
        have = self._held_bytes(row)
        if want is not None and have is not None and want == have:
            if not self._home_row(item_id, content_id):
                # The store refused, so the row is still an orphan. Saying
                # "rehomed" here made `enqueue` skip the download on an
                # unscoped `is_complete`, and the user's explicit Download
                # did nothing at all. "free" is the honest answer: nothing
                # owns this id, carry on and write the row.
                log.warning("Could not home %s to %s; treating it as "
                            "unclaimed.", item_id, content_id)
                return "free"
            return "rehomed"
        if not may_reap:
            # The caller has not established that it wants the item yet, so
            # nothing may be deleted on its behalf. It asks again once it
            # has. See `enqueue`, which is the only caller that splits them.
            return "stale"
        if item_id in self._active_ids():
            # A worker is writing into this very directory. Deleting it now
            # races the write, and the worker would then update a row that
            # is gone. Left alone, and the caller queues nothing: the copy
            # being asked for is already on its way.
            #
            # Read-only on purpose -- **not** `_cancel_if_active`, which is
            # what the other deleters use. Cancelling here would abandon the
            # very download the user just asked for and then decline to
            # re-queue it, which is the same "asked for a download, got
            # nothing" outcome by a different route.
            log.info("Not reaping %s: it is downloading right now.", item_id)
            return "busy"
        log.info("Re-downloading %s: the copy on disk cannot be shown to be "
                 "the same content (%s on disk, server says %s).",
                 row.get("name") or item_id, have, want)
        self._remove_files(row)
        self.db.delete(item_id)
        return "reaped"

    def _home_row(self, item_id, content_id):
        """Give an orphan row its server, in the catalog **and beside the
        media**.

        Both, because they answer the question at different times: the row
        answers now, and the manifest answers after a catalog loss, when
        `_adopt_orphan` rebuilds from the file. Homing only the row leaves a
        restore to orphan it again, silently.

        Returns whether the row was homed. The store can refuse -- an empty
        content id is not a server -- and the caller has to know, because a
        claim reported as `rehomed` on a row that is still an orphan sends
        `enqueue` down the "already held" path for a copy nobody owns.
        """
        if not self.db.home_content_server(item_id, content_id):
            return False
        row = self.db.get(item_id) or {}
        manifest = os.path.join(self._item_dir(row), "item.json")
        try:
            with open(manifest, encoding="utf-8") as fh:
                data = json.load(fh)
            data["ServerId"] = content_id
            with open(manifest, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except (OSError, ValueError):
            # A manifest we cannot rewrite is survivable -- the row is homed
            # and only a catalog loss would expose it -- and refusing here
            # would undo a claim the caller has already been told about.
            log.debug("could not record the server in the manifest for %s",
                      item_id, exc_info=True)
        return True

    def _add_row(self, server_uuid, item, origin=ORIGIN_USER):
        """Write the catalog row for an item about to be downloaded.

        **Takes no path key, and there is no longer a column to take one.**
        This used to write `downloads.server_id` from `client.config.data`,
        where the value was always absent; CX8 dropped the column, and the rule
        that outlives it is that nothing here may read a server identity out of
        `config.data` at all. docs/do-not-fix.md 1 holds the mechanism (CR12).

        **Refuses a DTO that does not name its server**, and returns whether
        it wrote. A row with no `content_server_id` is a legacy state -- the
        migration fills it from the manifest, and one that still has none is
        one whose manifest could not be read -- and writing a new one by
        hand makes the legacy case unreachable by repair and permanent by
        construction: it answers for every server that asks, forever.

        Measured against the 12.0 QA server on 2026-09-18, which is the
        oracle for what a real DTO carries: Movie, Episode, Audio, Book,
        AudioBook, Photo, MusicVideo and Video all name their server, and so
        do items fetched with a restricted `Fields` list. So this is expected
        never to fire, which is why it is loud rather than silent.
        """
        # The site that makes the note at the top of this file true: every
        # other reader of a raw `ServerId` is downstream of this refusal.
        if not item.get("ServerId"):
            log.error("Refusing to queue %s: the item does not name its "
                      "server, so nothing could say which server's copy it "
                      "is. This should not happen against a real server.",
                      item.get("Id"))
            return False
        if self.db.get(item["Id"]) is None:
            # **A row this writes starts unclaimed**, whoever asked for it. A
            # standing `owned=1` over an item the catalog does not have is a
            # claim on whatever writes that row next, and this is that writer
            # for every origin -- the release above only covers the ones the
            # user asked for, so a scheduled download inherited the claim.
            # Guarded on the row's absence rather than on the caller: an
            # enqueue that re-queues a playlist's own in-progress member must
            # not disown it (see `claims_its_members`).
            self._claim_from_playlists(item["Id"])
        source = (item.get("MediaSources") or [{}])[0]
        ext = self._ext_for(item)
        self.db.upsert({
            "item_id": item["Id"],
            # The CONTENT key, from the item itself rather than from the
            # client's config -- this is the same value `_adopt_orphan`
            # recovers from the manifest. Guaranteed present by the refusal
            # above, so no `or None` fallback: an empty string used to reach
            # here and it is the worst of both, matching no server *and*
            # failing the `IS NULL` branch every content read leans on, so
            # the row was invisible everywhere rather than answering for
            # everyone.
            "content_server_id": item["ServerId"],
            # The LOGIN that asked. A delivery address rather than an
            # identity -- see `requested_*` below, which is the identity.
            "server_uuid": server_uuid,
            # **Who asked, as an account**, recorded now rather than derived
            # later. Deriving works today (measured 14/14 on 2026-09-19) and
            # stops the first time a server connection is deleted and added
            # back, which mints a new login uuid for the same person.
            #
            # Two uses of one record, stated together because this is where it
            # has drifted twice: it is a record of *who asked*, which the
            # sweep's scoping relies on, and resolving a live *client* by it is
            # the defect F48 names. R19; docs/rulings-log.md.
            #
            # The server half comes off the DTO, not the credential: the
            # refusal above guarantees it, and a login we cannot place must
            # still produce a row for a file that is about to exist on disk.
            "requested_server_id": item["ServerId"],
            "requested_user_id": _requested_user_id(server_uuid),
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
            "added_at": int(time.time()),
            "origin": origin,
            "completed_at": None,
        })
        return True

    def _item_dir(self, row):
        return item_dir(self.root, row["item_id"])

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

    def _rehome_playlist_art(self):
        """Move each cached playlist poster under its content server.

        R23: *"Moving is fine, we should just make it transactional so it
        doesn't strand files."* So:

        * `os.replace` per file, which is atomic within a filesystem, rather
          than a copy-then-delete that can be interrupted holding neither end;
        * **unconditional, on every open** -- the shape `_migrate`'s `origin`
          backfill uses and for its reason. An interrupted pass leaves each
          poster at one path or the other, and the next open finishes the job.
          There is no marker to get wrong, and nothing to re-run by hand.
        * per playlist, so one unmovable directory costs its own poster and not
          the rest.

        The one file this deletes is a duplicate: if the destination already
        holds a poster of that name, the *newer* write is the one at the
        destination, and the old copy has to go or the pass never converges.

        Orphaned directories -- a playlist deleted while its poster stayed --
        are **not** touched, because this walks catalog rows. That is
        unchanged: nothing has ever removed them, which is why `playlist` is in
        `RESERVED_STORE_DIRS` (the orphan sweep used to delete the whole cache).
        """
        try:
            rows = self.db.list_playlists(ANY_SERVER)
        except Exception:
            log.debug("Could not list playlists to re-home their art",
                      exc_info=True)
            return
        moved = 0
        for row in rows:
            playlist_id = row.get("playlist_id")
            if not playlist_id:
                continue
            old = legacy_playlist_art_dir(self.root, playlist_id)
            new = playlist_art_dir(self.root, row.get("server_id"),
                                   playlist_id)
            if old == new or not os.path.isdir(old):
                continue
            try:
                os.makedirs(new, exist_ok=True)
                for name in os.listdir(old):
                    src, dst = (os.path.join(old, name),
                                os.path.join(new, name))
                    if os.path.exists(dst):
                        os.remove(src)
                    else:
                        os.replace(src, dst)
                os.rmdir(old)
                moved += 1
            except OSError:
                log.debug("Could not re-home the art for playlist %s",
                          playlist_id, exc_info=True)
        if moved:
            log.info("Moved the cached art of %d playlist(s) under its "
                     "server.", moved)

    def _reconcile_disk(self, sweep_orphans=True):
        """Best-effort startup sweep to keep the catalog and the file store in
        agreement (S12):

        * a row marked COMPLETE whose media file has vanished is re-queued
          (PENDING) so it downloads again;
        * an on-disk per-item directory with no catalog row is removed.

        The second half **identifies what it deletes rather than inferring
        it**, and all four tests are load-bearing -- the catalog reads
        (`db.healthy`), there is at least one row (so an empty catalog sweeps
        nothing), the child is shaped like an item id, and it is not a live
        row. Each exists because inferring instead deleted something:
        docs/offline-sync.md section 5.

        The second of those used to be "the directory is one a row *names*",
        through `downloads.server_id`. That column is gone (CX8) and was NULL
        on every row ever written, so what it actually asserted was
        `known` being non-empty -- which is the form it takes now.
        """
        if not self.db.healthy():
            # Refusing the requeue half too: a `[]` from an unreadable catalog
            # is not "no rows to check" either, and the write it would skip is
            # the harmless half anyway.
            log.error("Skipping the disk reconcile: the catalog is unreadable.")
            return
        rows = self.db.list()
        known = set()           # every item id the catalog holds
        server_uuid = None      # any login we know, for _adopt_orphan
        for row in rows:
            known.add(row["item_id"])
            if server_uuid is None and row.get("server_uuid"):
                server_uuid = row["server_uuid"]
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
        # **Only the one store directory, and only when the catalog holds a
        # row.** A store with no rows sweeps nothing, which is correct: there
        # is no such thing as an orphan we can prove. Never the root either --
        # `<root>` is a folder the user chose and may share with their own
        # files, and only `<root>/server/` is ours.
        if not known:
            return
        base = os.path.join(self.root, STORE_DIR)
        if not os.path.isdir(base):
            return
        try:
            children = os.listdir(base)
        except OSError:
            return
        for child in children:
            if child in RESERVED_STORE_DIRS or child in known:
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
            if self._adopt_orphan(child, child_path, server_uuid):
                continue
            log.warning("Removing orphaned download dir: %s", child_path)
            shutil.rmtree(child_path, ignore_errors=True)

    def _adopt_orphan(self, item_id, item_dir, server_uuid):
        """Rebuild the catalog row for a complete download that has none.

        `_download` writes `item.json` and `source.json` beside the media so a
        download describes itself; adopting is what stops a restored catalog
        being a *delayed* wipe (docs/offline-sync.md section 5).

        **Both halves are required -- the manifest and the media.** A manifest
        with no media is an interrupted download or a delete whose unlink
        failed, and reclaiming that is the sweep's job. Where it is arguable,
        take the recoverable error: an item that reappears can be deleted
        again, media deleted on the strength of a missing row cannot.

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
            # **The same door `enqueue` uses.** This upserted unconditionally,
            # and `INSERT OR REPLACE` on a catalog-wide primary key means a
            # directory found here could take a row that belongs to another
            # server -- leaving that server's media in a place nothing points
            # at. **Today's sweep cannot reach that**: `known` holds every id
            # the catalog has, so a child it names is never a candidate. The
            # check stays because what makes this door's "yes" safe is the
            # claim and not its caller -- a second caller with a set built
            # some other way is the shape this repository has hit before, and
            # the cost here is media deleted for a row that exists.
            verdict = self.claim_identity(item_id, item)
            if verdict in ("ours", "refused", "busy"):
                # A row already answers for this id. Do not replace it, and
                # do NOT report it unadopted either: the caller deletes what
                # it cannot adopt, and deleting media on the strength of a
                # claim we just declined is the irrecoverable direction. It
                # stays on disk, unreferenced, and says so in the log.
                log.warning("Not adopting %s: the catalog already holds "
                            "that id for %s. Its files are left in place.",
                            item_id, self.db.owner_of(item_id))
                return True
            self.db.upsert({
                "item_id": item_id,
                # From the manifest, which is why an adopted row can be
                # content-scoped even when the uuid below comes back None:
                # the login is only ever in the catalog, but the server is
                # in the file beside the media.
                "content_server_id": item.get("ServerId") or None,
                # Recovered from a surviving row, because the login is only
                # ever in the catalog while the item id is in the path. None
                # is survivable (the copy still plays offline; only its
                # watched-state sync waits for a re-download) and is better
                # than guessing.
                "server_uuid": server_uuid,
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
        # Both claims, not just the reaper's. The `ORIGIN_USER` above exists
        # to stop the reaper deleting a download this method invented; a
        # playlist that still owns the item deletes it just as unconditionally,
        # and `_delete_playlist` does not look at origin at all. Releasing one
        # and not the other protects the file from whichever deleter happens
        # not to run first.
        self._claim_from_playlists(item_id)
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
                row = self._auto_after_sweep(now, row, stopping=stopping)
                if row is None:
                    self._wake.wait(5)
                    continue
                self._download(row, stopping=stopping, gen=gen)
                error_streak = 0
            except Exception:
                # The worker must survive anything (disk full, DB errors —
                # note the error path's own db.update can raise again on a
                # full disk); back off so a persistent failure can't spin.
                error_streak += 1
                log.exception("Download worker iteration failed.")
                self._wake.wait(min(60, 5 * error_streak))

    def _auto_after_sweep(self, now, row, stopping=None):
        """Run one auto-download pass, behind a sweep that landed.
        docs/offline-sync.md section 4.

        The reaper deletes on watched state, so it must not decide from a
        snapshot taken before the network came back. It used to ask the
        server itself, once per row; that was replaced with an ordering
        -- *reap after the sweep* -- and this is the ordering.

        Both a request and a hold are needed, and neither alone works. A
        request alone: the pass fires before the first sweep of the session,
        because `last_run` starts at zero (so a pass is due at launch) while
        `USERDATA_SWEEP_SETTLE` holds the first sweep back a minute. A hold
        alone: nothing ever sets `_sweep_due`, so hours into a session with
        no trigger the flag is down, nothing holds, and the reap runs stale.

        Only between downloads: a pass here would otherwise enqueue work
        while the user's own download is streaming, and tick() is a no-op
        unless the interval has elapsed. Gated on *runnable* work, not on
        the queue being empty: a pending row for a server we cannot reach is
        not a download in progress, and treating it as one used to mean one
        dead server switched auto-download's reaper off for the life of the
        process -- retention and the cap silently stopped being enforced,
        with the queue's own log line the only clue.

        Returns the row to start next, which a pass may have queued.
        """
        if self.auto is None or row is not None:
            return row
        try:
            # Eligibility *after* `_next_runnable`, deliberately: asking
            # first would fire a network sweep ahead of a queued user
            # download, on behalf of a pass that then cannot run anyway.
            # due() covers playback too, so a busy machine never gets here.
            if not self.auto.due():
                return row
        except Exception:
            log.debug("could not tell whether an auto pass is due",
                      exc_info=True)
            return row
        self._sweep_due = True
        self._sweep_if_due(now)
        if self._sweep_owed(now):
            return row
        # `stopping`, not the constructor's flag: a worker stop() gave up
        # on has had `_stop` cleared under it by the next start(), so its
        # generation is the only thing that still says it was replaced.
        self.auto.tick(should_stop=stopping)
        return self._next_runnable()

    def _sweep_owed(self, now):
        """Is a reap waiting on a sweep that has not landed yet?
        docs/offline-sync.md section 4.

        Owed means: some server owning a row this pass could delete has not
        been fully refreshed, **and** somebody is signed in who could still
        answer for it. Offline requires no sweep, and the watched grace
        period carries that case instead (`auto_download_keep_watched_hours`,
        whose default was moved off zero so that there is a grace to carry it
        with).

        **A client-list failure reads as offline, not as unknown.** We
        cannot tell which it is, and holding on what we cannot tell is the
        starvation direction -- the same failure `_next_runnable` records
        having fixed once already.

        **Answered is per session, not per pass**, and that is a reading of
        the guarantee rather than a detail: what the ordering closes is a
        reaper deciding from a *download-time snapshot*, and once a sweep has
        written into the catalog for a server the websocket keeps it live, so
        a second reading before each pass adds delay rather than safety. It
        would also make the hold routine instead of exceptional -- every pass
        whose hour fell inside `USERDATA_SWEEP_FLOOR` would wait the floor
        out.

        **What is owed is an account's answer, not a server's** (D1). The
        sweep asks one server as whichever login is signed in for it and
        files what comes back under that login's account, so that account is
        what "answered" can honestly record -- and a profile switch then
        invalidates the record by changing the answer, with nothing to clear.
        Both sides derive the account with the same `actor_of` call, so they
        agree by construction.

        A server nobody is signed in for is **not** owed a sweep, as before:
        offline requires none. Unchanged too, and worth saying because the
        narrowed pull makes it easier to misread: a row whose downloading
        account is not the one signed in is not represented here at all,
        because no connected account can answer for it. That was already true
        of every row on a server nobody is signed in for.

        Bounded: at `REAP_SWEEP_HOLD` past the first hold the pass runs
        regardless, so a server that answers the connection but never the
        request cannot switch retention off for the life of the process.
        """
        try:
            wanted = {row["content_server_id"]
                      for row in self.db.list_auto(status=STATUS_COMPLETE)
                      if row["content_server_id"]}
        except Exception:
            log.debug("could not list the reaper's candidates", exc_info=True)
            return False
        if not wanted:
            self._reap_hold_until = None
            return False
        try:
            routes = self._connected_routes()
        except Exception:
            # A client-list failure reads as offline, per the paragraph
            # above: nothing is owed, so nothing is held.
            log.debug("could not read the connected server list",
                      exc_info=True)
            self._reap_hold_until = None
            return False
        owed = {self.actor_of(acting_login=routes[content_id][0])
                for content_id in wanted if content_id in routes}
        unanswered = owed - self._answered_accounts
        if not unanswered:
            self._reap_hold_until = None
            return False
        named = ", ".join(sorted("%s/%s" % a for a in unanswered))
        if self._reap_hold_until is None:
            self._reap_hold_until = now + REAP_SWEEP_HOLD
            log.debug("Holding the auto-download pass for a sweep as %s.",
                      named)
            return True
        if now >= self._reap_hold_until:
            self._reap_hold_until = None
            log.info("Auto-download: reaping without a fresh sweep as %s; "
                     "held for %ds.", named, REAP_SWEEP_HOLD)
            return False
        return True

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
        # **Once per pass, not once per row.** Each rebuild calls
        # `content_id_for` per live client, which scans every local profile's
        # whole credential list -- so a few hundred rows queued against an
        # unreachable server, the case this method exists for, was thousands
        # of list scans every five seconds for the life of the process.
        # `routes_for`'s docstring already said callers that need several in
        # a pass read the index once.
        #
        # `{}` on failure, which is what `routes_for` answers with too: no
        # route found, and the row falls through to its own login.
        try:
            routes = self._connected_routes()
        except Exception:
            log.debug("could not read the connected server list",
                      exc_info=True)
            routes = {}
        blocked = 0
        for row in self.db.list(status=STATUS_PENDING):
            if self._client_for_row(row, routes=routes) is not None:
                if blocked:
                    log.debug("Skipped %d pending download(s) whose server is "
                              "unreachable.", blocked)
                return row
            blocked += 1
        return None

    def _client_for_row(self, row, routes=None):
        """A live client that may fetch one download row's file, or None.

        Three questions in order, and the order is the point (F48):

        1. **the account that asked for it** -- `requested_*`, written at
           enqueue since R19. This is what closes the case the entry names: a
           server answering at two addresses is two logins and one account,
           `clients._connect_all` registers the client under whichever
           address answered first, and a row carrying the other uuid sat
           pending forever with a perfectly good route open beside it. Asking
           by account also means a download is never fetched as somebody
           else, which resolving by server alone would allow.
        2. **any login for the row's own server**, for a row that names no
           account: rows enqueued before R19, and enqueues that could not
           resolve a login. Whoever is signed in there is the only candidate
           available, and it is also what the third question answered for
           these rows before.
        3. **the login on the row**, last, because an orphan row (no
           `content_server_id`, and its DTO never named one) has no other
           handle at all. Exactly the old behaviour, kept so that no row
           becomes unstartable.

        None means "not now, not never": the row stays pending and
        `_next_runnable` skips past it. [iw] on the case:
        *"let the download fall back to a queue."*

        ``routes`` is `_connected_routes` already built, for a caller asking
        about several rows in one pass. Omitted, this builds its own -- which
        is what every caller outside the queue loop does, and what keeps the
        three questions above readable from here.
        """
        actor = (row.get("requested_server_id"), row.get("requested_user_id"))
        client = self._client_for_actor(*actor)
        if client is not None:
            return client
        try:
            live = bool(self.get_clients())
        except Exception:
            log.debug("could not read the connected server list", exc_info=True)
            live = False
        if live and actor[1] and actor[1] != NO_ACTOR:
            # The row knows whose it is and that person is not signed in.
            # Falling through would fetch their file as whoever else is on
            # that server, under their token and their permissions.
            #
            # **Only when there IS a client list to have looked in.** An empty
            # one is "cannot tell", not "that person is absent" -- the same
            # rule this subsystem applies everywhere else -- and `get_clients`
            # is an *optional* argument to `start`, so a manager can be wired
            # with `get_client` alone. Refusing on that made every attributed
            # row unstartable and the queue silently stopped; found by the e2e
            # legs, which are wired exactly that way. Nothing is fetched as
            # somebody else either way: with no client list the only route
            # left below is the row's own login.
            return None
        content_id = row.get("content_server_id")
        if routes is None:
            route = self.routes_for(content_id)
        else:
            # The same two refusals `routes_for` makes, against an index that
            # is already built: a falsy server is not a question, and the
            # index never files a route under `ANY_SERVER`, so a lookup of it
            # answers None on its own.
            route = routes.get(content_id) if content_id else None
        if route is not None:
            return route[1]
        return self.get_client(row.get("server_uuid"))

    def _client_for_actor(self, server_id, user_id):
        """A live client that can speak for this person, or None.

        Matched on the actor rather than on the login that queued the entry.
        A person can hold several logins for one server -- a LAN address and
        a remote one are two -- and the one they were signed in as offline
        is often not the one that comes back first. Keyed on the uuid, such
        an entry stayed pending with a perfectly good route sitting open
        next to it.

        Only the connected clients are considered, so this answers None
        while that person is not signed in anywhere, which is the state the
        queue exists for.
        """
        if not user_id or user_id == NO_ACTOR:
            return None
        try:
            from ..users import userManager
            for uuid, client in (self.get_clients() or {}).items():
                if userManager.actor_for(uuid) == (server_id, user_id):
                    return client
        except Exception:
            log.debug("could not find a route for %s on %s", user_id,
                      server_id, exc_info=True)
        return None

    def routes_for(self, content_server_id):
        """The live login that can reach one server, as ``(uuid, client)``.

        **The one index between a ServerId and a way to talk to it**, and the
        question it answers is "which door is open", never "who is this". The
        account question is :meth:`_client_for_actor`, which is a different
        one and deliberately not merged with this: it must not answer with a
        client belonging to somebody else, and this one may.

        Callers that need several in a pass read :meth:`_connected_routes`
        once instead -- the index is cheap but it is not free, and rebuilding
        it per row was the shape this replaced.

        **No guard against `ANY_SERVER` here, deliberately.** The sentinel is
        truthy, so one looks as though it belongs -- but the index refuses to
        file a route under it (see `_connected_routes`), so asking for it
        already answers None. A second check here could not change an answer
        and would suggest to the next reader that it can.
        """
        if not content_server_id:
            return None
        try:
            return self._connected_routes().get(content_server_id)
        except Exception:
            log.debug("could not read the connected server list",
                      exc_info=True)
            return None

    def _connected_routes(self):
        """content server -> (login uuid, client) for everything connected.

        The inverse of :meth:`content_id_for`, built once per pass because
        several logins can answer for one server -- two addresses, or two
        people -- and a caller wants exactly one of them: the one that is
        signed in, whose account a sweep's answers will be filed under.

        First match wins and the order is the registry's, which is the order
        the chains connected in. Only one client per server can be live at
        all (`clients._connect_all` groups the credentials by ``Id`` into one
        fallback chain), so "first" and "only" are the same thing here --
        stated rather than relied on silently, because the day that changes
        this becomes a choice.
        """
        routes = {}
        for uuid, client in (self.get_clients() or {}).items():
            content_id = self.content_id_for(uuid)
            # `is not ANY_SERVER` and not just truthiness: the sentinel is
            # truthy, so a client whose login will not resolve used to be filed
            # under "the server called ANY_SERVER" and then answered as *a*
            # route. A route is a real ServerId or it is not a route.
            if (content_id and content_id is not ANY_SERVER
                    and content_id not in routes):
                routes[content_id] = (uuid, client)
        return routes

    def _sync_playstate(self):
        """Replay offline playstate once a server is reachable — advancing only:
        mark watched if the server hasn't, and push a later resume position."""
        pending = self.db.list_playstate()
        if not pending:
            return
        done = []
        routes = {}
        for entry in pending:
            actor = (entry.get("server_id"), entry.get("user_id"))
            if actor not in routes:
                routes[actor] = self._client_for_actor(*actor)
            client = routes[actor]
            if client is None:
                continue  # nobody signed in who can speak for this person
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

        This is the whole schedule: a server *becoming reachable* ends exactly
        the stretch a sweep covers, so it is the trigger rather than an
        interval. **Watched here rather than subscribed to** -- the registry is
        the state itself, so a set comparison cannot miss a transition however
        the server came back, where `on_server_connected` is a single slot and
        a notification. Disappearances are recorded but trigger nothing.
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

        The one trigger that is not an edge the app can see, covering the gap
        measured in `tests/e2e/test_offline_sync.py`: another client can finish
        an item and never report its stop, and the server announces that to
        nobody. Home is where it would show.

        Not floored here -- `_run` defers rather than drops, so bouncing in and
        out of Home neither becomes a poll nor loses a request
        (docs/offline-sync.md section 3). Cheap and non-blocking: this only
        marks the sweep due and wakes the worker.
        """
        self._sweep_due = True
        self._wake.set()

    # A profile switch has no trigger of its own (D1, R16 in the narrow
    # form). `request_profile_sweep` was one, and all three things it did are
    # either unnecessary or the descope:
    #
    # - clearing the answered set: unnecessary. It is keyed on the account
    #   now, so the new profile's accounts were never in it.
    # - firing a sweep: `stop_all_clients` + `connect_all` empties the
    #   connected set and refills it with this profile's uuids, and
    #   `_note_connected_servers` reads that as servers becoming reachable.
    #   Its docstring's reason for not trusting that -- two profiles sharing
    #   a credential uuid -- rested on `force_unique`, which nothing passed and
    #   which has since been deleted: `_finalize_login` now always mints a
    #   uuid4.
    # - clearing the floor and resetting the settle: dropped on purpose. With
    #   no deferred-sweep obligation a switch is a reconnect like any other,
    #   so it waits out the floor and the settle like any other. The cost is
    #   that a switch inside `USERDATA_SWEEP_FLOOR` of the last sweep sees
    #   this account's state up to five minutes late; what it buys is one
    #   schedule instead of two, and the reap is still held correctly for
    #   the new account by the point above.
    #
    # Do not add a switch trigger back without a ruling: the deferred model
    # it belonged to is the thing R16 removed.

    def mirror_playstate(self, item_id, position_ticks=None, played=None,
                         server_uuid=None):
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

        ``server_uuid`` is the login that is *playing*, which is who the
        progress belongs to. Without it the actor is resolved from the row's
        own server and the active local profile, which is right for the
        common single-profile case and is the best available offline.
        """
        if not item_id or (played is None and position_ticks is None):
            return False
        db = self.db
        if db is None:
            return False
        try:
            row = db.get(item_id)
            actor = self.actor_of(
                acting_login=server_uuid,
                server_id=(row["content_server_id"] if row else None))
            return db.update_userdata(item_id, actor=actor, played=played,
                                      position_ticks=position_ticks)
        except Exception:
            log.debug("Could not mirror playstate for %s", item_id,
                      exc_info=True)
            return False

    def mirror_watched(self, item_id, played, server_uuid=None):
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
            targets = db.watched_targets(
                item_id, server_id=self.content_id_for(server_uuid))
        except Exception:
            log.debug("Could not resolve downloads for %s", item_id,
                      exc_info=True)
            return 0
        moved = 0
        for target_id, target_server in targets:
            # Resolved per target, from the login doing the *marking* and
            # the row's own content server: a mark is an act by a person, so
            # it belongs to whoever made it and not to whoever happened to
            # download the file -- and it is filed under the server that
            # holds the row, which is where `set_watched` will look for it.
            actor = self.actor_of(acting_login=server_uuid,
                                  server_id=target_server)
            try:
                if db.set_watched(target_id, played, actor=actor):
                    moved += 1
            except Exception:
                log.debug("Could not mirror the watched mark for %s",
                          target_id, exc_info=True)
        if moved:
            log.debug("Mirrored a watched mark onto %d downloaded item(s).",
                      moved)
            self._notify_change()
        return moved

    def apply_userdata_event(self, arguments, server_uuid=None):
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
        arguments = arguments or {}
        # The payload names the actor and this used to throw the server half
        # away, so a push about one account moved every account's copy.
        #
        # **The server comes from the connection first.** `ServerId` in the
        # body is a claim the message makes about itself; which socket it
        # arrived on is a fact, and it cannot be absent. With the field
        # missing, `actor_of` answered `(NO_ACTOR, NO_ACTOR)` and every entry
        # was filed with nobody named, for rows whose server is perfectly
        # well known. Taking it from the socket removes the case instead of
        # guarding it.
        # `or` would be wrong now: `content_id_for` answers `ANY_SERVER`
        # when no login was named, and that is truthy. Spelled out, because
        # this is the one site of the fourteen whose question is not "which
        # rows do we hold" but "who is this event about" -- a scope is not an
        # answer to it, so both non-answers fall through to the body's claim
        # exactly as they did before.
        from_socket = self.content_id_for(server_uuid)
        if from_socket is ANY_SERVER or from_socket is None:
            from_socket = arguments.get("ServerId")
        actor = self.actor_of(server_id=from_socket,
                              user_id=arguments.get("UserId"))
        entries = arguments.get("UserDataList") or []
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
                # `or None` on played: db.update_userdata is advance-only
                # unless asked otherwise, and False there means "leave it
                # alone". So an un-watch announced over the socket does not
                # retreat the local copy.
                #
                # **The sweep does retreat and this does not**
                # (`allow_retreat`; docs/offline-sync.md section 1), and the
                # asymmetry is deliberate rather than pending. [iw]: a sweep runs at launch anyway, and somebody
                # managing watch state for their downloads is almost
                # certainly doing it on this client, where `record_watched`
                # writes both ways immediately. docs/do-not-fix.md F47.
                if db.update_userdata(
                        item_id, actor=actor,
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

        **Grouped by the row's content server, asked as whichever account is
        connected for it** (C5). Grouping by the row's saved login instead
        made two things wrong at once: one box reached through two addresses
        is two logins and one server, so half its rows were never refreshed
        while the other half were; and two people on one box are two logins
        and one server, so the sweep asked as whoever downloaded the copy
        rather than as whoever is signed in. Both observed on the QA server
        -- one ServerId behind two addresses and two accounts.

        **Lazy, per connected account** (docs/offline-sync.md section 3): an
        account that is not
        connected is not asked and not waited for. Its rows keep the state
        they had until it signs in, which is what makes a profile switch need
        no schedule of its own -- see the note where
        ``request_profile_sweep`` used to be.

        **Scoped to the union R21 names** (R16 in the narrow form): for each
        server, this asks about *the rows the signed-in account downloaded*,
        and about **all** of them when that account is one the machine has
        auto-download turned on for. The second half is ongoing interest --
        the profile fetches from that server unattended, so its whole
        catalog there is live state -- and the first is R16's own scoping,
        durable since R19 gave a row the account that asked for it.

        **A row that names no account is swept by whoever is signed in**, and
        that is the third clause rather than an exception. R19 writes the pair
        at enqueue and the backfill fills old rows, so an unattributed row is
        one whose enqueuing login could not be resolved at all -- it is in
        nobody's scope, and scoping it to nobody means its state can never
        refresh again for anyone. The same shape as `_client_for_row`'s second
        question, for the same reason. Found by the e2e leg: every fixture
        there wrote rows with no account, and the whole pull went silent.

        Four cases, which is how R21 was checked before it was adopted: a
        profile that downloaded by hand with auto-download off is still swept
        (the downloader half); a profile with neither is not swept, which is
        the saving; another person's watches still reach their server through
        their own queue drain, because **push stays universal**; and a
        profile with auto-download on for a server it has not downloaded from
        yet is swept, which is the interest the descoped opt-in was for.

        What this stops paying for: on a shared machine, sweeping the whole
        catalog as whoever happens to be signed in -- which cost a request
        per batch *and* wrote a second account's userdata row for every item
        somebody else had downloaded.

        **The pull may retreat**, which the push may not: the
        server clearing a watched flag is applied here unless the queue
        still owes that actor the mark. ``db.update_userdata`` holds the
        rule and the transaction; see it for why. docs/offline-sync.md
        section 1.
        """
        try:
            rows = self.db.list(status=STATUS_COMPLETE)
        except Exception:
            log.debug("Could not list the catalog for a userdata refresh",
                      exc_info=True)
            return
        by_server = {}
        for row in rows:
            if not row.get("item_id"):
                continue
            if not row.get("content_server_id"):
                # An orphan: the row's server is unknown, so there is no
                # account to ask as and nowhere to file the answer if there
                # were. It plays and records locally and syncs in neither
                # direction ([iw], 11c) -- asking anyway would spend a
                # request to write into the machine-wide bucket under a
                # person the server named and this row cannot.
                continue
            by_server.setdefault(row["content_server_id"], []).append(
                (row["item_id"], (row.get("requested_server_id"),
                                  row.get("requested_user_id"))))
        routes = self._connected_routes()
        try:
            from ..users import userManager
            wide = userManager.auto_download_accounts()
        except Exception:
            # Cannot say is not everybody: an unreadable allow-list narrows
            # the sweep to what each account downloaded rather than widening
            # it to every row.
            log.debug("could not read the auto-download allow-list",
                      exc_info=True)
            wide = set()
        answered = set()
        updated = 0
        sent = 0
        for content_id, held in by_server.items():
            route = routes.get(content_id)
            if route is None:
                continue        # still offline for this server
            server_uuid, client = route
            # The sweep asks one server as one login, so everything it
            # brings back belongs to that login's account -- which is what
            # makes this the one userdata writer that needs no guesswork.
            actor = self.actor_of(acting_login=server_uuid)
            # Answered even when the narrowing leaves nothing to ask: the key
            # is the account and this one owes nothing here, so holding a reap
            # for it would wait out REAP_SWEEP_HOLD every session on a shared
            # machine.
            answered.add(actor)
            ids = [item_id for item_id, asked_by in held
                   if actor in wide or asked_by == actor
                   or not _names_an_account(asked_by)]
            if not ids:
                continue
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
                    # Half a server is not a server: `break` leaves the rest
                    # of its ids unasked, so it must not count as answered.
                    # The reap-after-sweep hold is what reads this
                    # (docs/offline-sync.md section 4).
                    answered.discard(actor)
                    break
                for item in result.get("Items") or []:
                    data = item.get("UserData") or {}
                    if not item.get("Id") or not data:
                        continue
                    try:
                        if self.db.update_userdata(
                                item["Id"], actor=actor,
                                played=data.get("Played"),
                                position_ticks=data.get(
                                    "PlaybackPositionTicks"),
                                allow_retreat=True):
                            updated += 1
                    except Exception:
                        log.debug("Could not store userdata for %s",
                                  item.get("Id"), exc_info=True)
        # Rebound rather than `|=`: nothing clears this set any more (D1),
        # but a read-modify-store from the worker while another thread reads
        # it is still worth avoiding, and one STORE_ATTR cannot be seen
        # half-applied. The race this replaced -- `request_profile_sweep`'s
        # fresh empty set thrown away between the read and the store -- is
        # gone with the call.
        self._answered_accounts = self._answered_accounts | answered
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
            self.db.mark_discarded(row["item_id"],
                                   server_id=row.get("content_server_id"))
        except Exception:
            log.debug("Could not record the failure of %s", row["item_id"],
                      exc_info=True)

    # -- the in-progress claim ---------------------------------------------
    #
    # Three questions are asked of it and they are not the same question:
    # "is anything live" (relocate), "is this item live" (_cancel_if_active)
    # and "which items are live" (the requeue on reopen). They go through
    # `_active_ids` and `_cancel_if_active` rather than reading the state,
    # because there can be more than one live worker and every reader that
    # assumed otherwise was wrong in a different way.
    #
    # Locking is deliberately split: `_active_ids` and `_cancel_if_active`
    # take `_active_lock` themselves, while `_claim_active` and
    # `_release_active` require it, because both of those sit inside larger
    # critical sections (the commit holds the lock across the rename, the
    # row update and the release). `_active_lock` is a plain Lock, so
    # calling a self-locking one from inside a held section deadlocks.

    def _claim_active(self, gen, item_id):
        """Record that worker `gen` is downloading `item_id`.

        Keyed by generation, not a single slot. `relocate`'s refusal path
        starts a replacement while the worker that outlived `stop()` is
        still parked in a socket read, so two workers are live at once and a
        slot can only name one -- the replacement overwrote the survivor's
        claim on the way in, and after that a delete for the survivor's row
        was answered "not downloading" while it was.

        The generation is the key rather than the item id because both
        workers can be on the *same* row: it is still PENDING, the stale one
        not having written anything back.

        **Caller must hold `_active_lock`.**
        """
        self._active[gen] = item_id

    def _release_active(self, gen):
        """Give up worker `gen`'s claim, and only that one.

        Two places let go -- the commit, and the `finally` that covers every
        other way out -- and they must agree, so they ask here. Popping by
        generation is what makes "if it is still ours" structural rather
        than a comparison somebody has to keep right: a superseded worker
        cannot reach the replacement's entry, and the replacement finishing
        first cannot free the survivor's.

        **Caller must hold `_active_lock`.**
        """
        self._active.pop(gen, None)

    def _active_ids(self):
        """The items every live worker is holding. Empty when the store is
        idle, which is what `relocate` asks. Takes `_active_lock`."""
        with self._active_lock:
            return set(self._active.values())

    def _download(self, row, stopping=None, gen=None):
        item_id = row["item_id"]
        client = self._client_for_row(row)
        if client is None:
            # Now only reachable if the server went away between _run picking
            # this row and getting here — _next_runnable does the skipping.
            # No wait: returning drops straight back into the loop, which
            # skips this row and idles on the queue as a whole.
            log.warning("No client for download %s; leaving pending.", item_id)
            return
        # Deletion requested before we got here. Ahead of the claim, so this
        # invocation never has one to release.
        if self._drop_cancelled(row):
            log.info("Download cancelled before it started: %s",
                     row.get("name") or item_id)
            return
        with self._active_lock:
            self._claim_active(gen, item_id)
        try:
            # A delete may have raced in just before we marked the item active
            # (it would have taken the direct path and removed the row). If the
            # row is gone, don't resurrect it.
            if not self.db.get(item_id):
                self._remove_files(row)
                return
            # Inside the try, so a catalog error here still leaves through the
            # `finally` rather than stranding the claim.
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
                self._download_series_art(client, item["SeriesId"])
                if item.get("SeasonId"):
                    self._download_season_art(client, item["SeasonId"])

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
            # to a COMPLETE row. Releasing the claim here means any delete that
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
                self._release_active(gen)
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
                self._release_active(gen)
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
        with open(os.path.join(item_dir, "trickplay.json"), "w",
                  encoding="utf-8") as fh:
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
            with open(os.path.join(item_dir, "segments.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(items, fh)
        except OSError:
            log.debug("Could not write segments.json", exc_info=True)
            return
        log.debug("Downloaded %d media segments for %s.", len(items),
                  source.get("Id"))

    def _download_series_art(self, client, series_id):
        """Cache series poster/backdrop so offline series tiles + the series page
        have artwork (episodes only carry their own images).

        **Not scoped by content server, and that is correct** -- unlike
        `playlist_art_dir`, which needs a scope because a playlist id hashes its
        *name*. A series id is derived from the media path, so two servers
        handing out the same one are describing the same folder, and one cached
        poster for it is right rather than a collision. **R26**; do not add a
        scope here on the strength of the playlist one, because they are keyed
        on different things -- that one hashes a playlist's *name*.

        The `server_id` parameter that used to sit here was never that scope: it
        was `downloads.server_id`, NULL on every row, so this path has always
        been the literal below.
        """
        series_dir = series_art_dir(self.root, series_id)
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

    def _download_playlist_art(self, client, content_server_id, playlist_id):
        """Cache the playlist's own poster so its offline tile has artwork.

        A playlist carries its own image; the tile used to borrow a member's
        poster, which meant a playlist whose first member had no art on disk
        showed a bare glyph.

        ``content_server_id`` scopes the directory, through the one helper the
        offline reader uses too (`db.playlist_art_dir`): two servers can hold
        a playlist with the same id, and one poster was overwriting the other.
        """
        pl_dir = playlist_art_dir(self.root, content_server_id, playlist_id)
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

    def _download_season_art(self, client, season_id):
        """Cache season poster so offline season tiles have artwork. Not content
        scoped either; see `_download_series_art`."""
        season_dir = season_art_dir(self.root, season_id)
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
