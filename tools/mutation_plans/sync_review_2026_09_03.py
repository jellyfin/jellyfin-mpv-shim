"""Mutations for the sync/auth review round of 2026-09-03.

A snapshot, not a suite: it records what the round's repairs each claimed, so
that a later refactor which quietly makes one of them unkillable shows up as
a survivor instead of as nothing at all.

    xvfb-run -a python3 tools/mutate_round.py \\
        tools/mutation_plans/sync_review_2026_09_03.py

Run `--dry-run` first after any refactor of these files: a pattern that no
longer matches is reported rather than skipped, because a mutation that
changes nothing looks exactly like one that survived. Rewrite the pattern to
whatever now expresses the same broken behaviour, or drop the entry and say
in the commit why that claim no longer needs evidence.

All of them were killed when the round closed. The four at the top of the
list are the follow-up session's repairs, added the same day and killed too.
"""

SELECT = [
    '-k', 'test_sync_manager',
    '-k', 'test_auth_header_truth_table',
    '-k', 'test_no_second_owner',
    '-k', 'test_player_auth_scope',
    '-k', 'test_track_truth_table',
    '-k', 'test_osc_bridge',
]


MUTATIONS = [
    ('restore: sidecars only when there is an aside (the old missing branch)',
     'jellyfin_mpv_shim/sync/manager.py',
     '                    if aside is not None:\n                        os.replace(catalog_path + suffix, aside + suffix)\n                    else:\n                        os.remove(catalog_path + suffix)',
     '                    if aside is not None:\n                        os.replace(catalog_path + suffix, aside + suffix)'),
    # Three sites of one rule -- never leave a writable, empty, *readable*
    # catalog where the evidence used to be. Patterns carry the line above so
    # each names one place; a bare `self.db = SyncDB(..., read_only=True)`
    # matches all three.
    ('restore: a failed one opens a writable catalog',
     'jellyfin_mpv_shim/sync/manager.py',
     '            self.db = SyncDB(catalog_path, read_only=True)\n            return CATALOG_ABSENT if missing else CATALOG_BEHIND',
     '            self.db = SyncDB(catalog_path)\n            return CATALOG_ABSENT if missing else CATALOG_BEHIND'),
    ('open: a catalog that will not open falls back to a writable one',
     'jellyfin_mpv_shim/sync/manager.py',
     '                self.db = SyncDB(catalog_path, read_only=True)\n            if not os.path.exists(backup_path):',
     '                self.db = SyncDB(catalog_path)\n            if not os.path.exists(backup_path):'),
    ('restore: a restored catalog that will not open falls back to writable',
     'jellyfin_mpv_shim/sync/manager.py',
     '                self.db = SyncDB(catalog_path, read_only=True)\n            return CATALOG_BEHIND',
     '                self.db = SyncDB(catalog_path)\n            return CATALOG_BEHIND'),
    ('restore: no staging, copy straight onto the target',
     'jellyfin_mpv_shim/sync/manager.py',
     '            shutil.copyfile(backup_path, staged)',
     '            shutil.copyfile(backup_path, catalog_path); staged = catalog_path'),
    # --- the follow-up session's repairs (bdd30a3d, cb2b50c1, and this one)
    ('restore: the sidecar step swallows every failure again',
     'jellyfin_mpv_shim/sync/manager.py',
     '                    if exc.errno != errno.ENOENT:\n                        raise',
     '                    pass'),
    ('delete: a removal that removed nothing still reports success',
     'jellyfin_mpv_shim/sync/manager.py',
     '        if os.path.exists(item_dir):\n            log.warning("Could not remove the files for %s at %s.",\n                        row.get("item_id"), item_dir)\n            return False\n        return True',
     '        return True'),
    ('playlist: membership is written without checking for the row',
     'jellyfin_mpv_shim/sync/db.py',
     '                    "(playlist_id, item_id, sort_index, owned) "\n                    "SELECT ?,?,?,? WHERE EXISTS "\n                    "(SELECT 1 FROM downloads WHERE item_id=?)",\n                    [(playlist_id, iid, idx, 1 if owned else 0, iid)',
     '                    "(playlist_id, item_id, sort_index, owned) "\n                    "VALUES (?,?,?,?)",\n                    [(playlist_id, iid, idx, 1 if owned else 0)'),
    ('adopt: an adopted orphan keeps the playlist claim on it',
     'jellyfin_mpv_shim/sync/manager.py',
     '        self._claim_from_playlists(item_id)\n        log.warning("Re-adopted the download at %s',
     '        log.warning("Re-adopted the download at %s'),
    # --- the claim-release repair. One rule, three ways to break it: the row
    # creator stops releasing, the manifest gets a vote again, and the
    # suppression stops being about the request.
    ('enqueue: a row created over a standing claim keeps it',
     'jellyfin_mpv_shim/sync/manager.py',
     '        if self.db.get(item["Id"]) is None:',
     '        if False:'),
    ('adopt: the manifest\'s own type decides whether the claim is released',
     'jellyfin_mpv_shim/sync/manager.py',
     '        self._claim_from_playlists(item_id)\n        log.warning("Re-adopted',
     '        if item.get("Type") != "Playlist":\n            self._claim_from_playlists(item_id)\n        log.warning("Re-adopted'),
    ('enqueue: a playlist download disowns the members it holds',
     'jellyfin_mpv_shim/sync/manager.py',
     '        claims_its_members = item_type == "Playlist"',
     '        claims_its_members = False'),
    ('move: catalog/backup sort key the wrong way round',
     'jellyfin_mpv_shim/sync/manager.py',
     'names.sort(key=lambda n: (n == "catalog.db", n == self.CATALOG_BACKUP))',
     'names.sort(key=lambda n: (n == self.CATALOG_BACKUP, n == "catalog.db"))'),
    ('backup: an empty catalog may overwrite one with rows',
     'jellyfin_mpv_shim/sync/manager.py',
     '        if not self.db.list() and os.path.exists(backup_path):',
     '        if False:'),
    ('startup: a restored catalog drives the orphan sweep',
     'jellyfin_mpv_shim/sync/manager.py',
     '                self._reconcile_disk(sweep_orphans=catalog is CATALOG_TRUSTED)',
     '                self._reconcile_disk(sweep_orphans=True)'),
    ('startup: a restored catalog is not reconciled at all',
     'jellyfin_mpv_shim/sync/manager.py',
     '        if catalog is not CATALOG_ABSENT:',
     '        if catalog is CATALOG_TRUSTED:'),
    ('cancel: the single actor stops re-checking',
     'jellyfin_mpv_shim/sync/manager.py',
     '        with self._active_lock:\n            if item_id not in self._cancelled:\n                return False',
     '        with self._active_lock:\n            if False:\n                return False'),
    ('cancel: the finally stops honouring',
     'jellyfin_mpv_shim/sync/manager.py',
     '            if self._drop_cancelled(row):\n                log.info("Honouring the delete of %s that arrived while the "',
     '            if False and self._drop_cancelled(row):\n                log.info("Honouring the delete of %s that arrived while the "'),
    ('cancel: the entry check stops honouring',
     'jellyfin_mpv_shim/sync/manager.py',
     '        if self._drop_cancelled(row):\n            log.info("Download cancelled before it started: %s",',
     '        if False and self._drop_cancelled(row):\n            log.info("Download cancelled before it started: %s",'),
    ('cancel: the row delete leaves the critical section',
     'jellyfin_mpv_shim/sync/manager.py',
     '            self._cancelled.discard(item_id)\n            self.db.delete(item_id)\n        self._remove_files(row)',
     '            self._cancelled.discard(item_id)\n        self.db.delete(item_id)\n        self._remove_files(row)'),
    ('cancel: a declined reap keeps the flag it set',
     'jellyfin_mpv_shim/sync/manager.py',
     '                    self._uncancel(item_id)\n                    return False',
     '                    return False'),
    ('enqueue: uncancel every item, including the declined ones',
     'jellyfin_mpv_shim/sync/manager.py',
     '        for item in items:\n            iid = item.get("Id")\n            if self.db.is_complete(iid):',
     '        for item in items:\n            iid = item.get("Id")\n            self._uncancel(iid)\n            if self.db.is_complete(iid):'),
    ('relocate: commonpath raises across drives again',
     'jellyfin_mpv_shim/sync/manager.py',
     '                             == old_abs)\n            except ValueError:',
     '                             == old_abs)\n            except _NeverRaised:'),
    ('relocate: the destination is not resolved',
     'jellyfin_mpv_shim/sync/manager.py',
     '                                                 os.path.realpath(new_root)])',
     '                                                 new_root])'),
    ('relocate: the store itself is not resolved',
     'jellyfin_mpv_shim/sync/manager.py',
     '            old_abs = os.path.realpath(old_root)',
     '            old_abs = os.path.abspath(old_root)'),
    ('rollback: replaces a source recreated during the move',
     'jellyfin_mpv_shim/sync/manager.py',
     '                    if os.path.exists(src):',
     '                    if False:'),
    ('origin: raw ports, so an explicit :443 is a different origin',
     'jellyfin_mpv_shim/utils.py',
     '            parts.port or _DEFAULT_PORTS.get(parts.scheme))',
     '            parts.port)'),
    ('origin: media.py keeps its own copy of the comparison',
     'jellyfin_mpv_shim/media.py',
     '                if not same_origin(path, server):',
     '                if (urllib.parse.urlparse(path).scheme,\n                        urllib.parse.urlparse(path).hostname,\n                        urllib.parse.urlparse(path).port) != (\n                        urllib.parse.urlparse(server).scheme,\n                        urllib.parse.urlparse(server).hostname,\n                        urllib.parse.urlparse(server).port):'),
    ("auth: an unparseable server url answers 'nothing foreign'",
     'jellyfin_mpv_shim/media.py',
     '            return {"unknown"}',
     '            return set()'),
    ('subtitle: only the subtitle being taken decides the restart',
     'jellyfin_mpv_shim/media.py',
     '            if sid in self.subtitle_enc or was_burned_in:',
     '            if sid in self.subtitle_enc:'),
    ('subtitle: every subtitle change restarts the stream',
     'jellyfin_mpv_shim/media.py',
     '            if sid in self.subtitle_enc or was_burned_in:',
     '            if True:'),
]
