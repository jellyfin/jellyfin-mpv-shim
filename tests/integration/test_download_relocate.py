"""Moving the download folder, end to end, on a real filesystem.

`tests/test_sync_manager.py` covers `relocate`'s rules with the catalog closed
and no worker running: it asks whether the method decides correctly. This asks
the question the user asks, which is different and is the one that has broken
twice -- **after the move, is the download folder still a download folder?**
Every case here therefore ends in `_assert_store_is_usable`: the catalog opens
where it now lives, every row is still in it, and the file each row names is on
disk under the new root. A move that answers `(True, "")` and leaves a store
that cannot describe itself has failed, and no assertion about the return value
can see that.

What is real here that is not real in the fast suite: the catalog is opened and
reopened through `_open_and_run` (the migration, the restore path, the disk
reconcile and the backup all run), the download worker is really started and
really stopped, and the bytes are really copied. `file_path` is relative to the
root **on purpose** -- that is what makes a move a move rather than a rename of
every row -- so a store that came out unusable is the symptom of the column
becoming absolute, which is the shape the reconcile would report as
"Downloaded file missing; re-queuing" one launch later.

Both platforms. Windows is not a spot check here: it is case-insensitive,
it has real drive letters, `os.rename` across them is the EXDEV path, and it is
the platform whose file manager hands out **quoted** paths to paste. Cases that
need a genuinely separate volume read `JMS_ALT_VOLUME` and skip without one;
the EXDEV code path itself is covered unconditionally by forcing `os.rename` to
refuse, so a box with one filesystem still tests the copy.
"""

import errno
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
import _harness as h  # noqa: E402

from jellyfin_mpv_shim.sync import manager as manager_module  # noqa: E402
from jellyfin_mpv_shim.sync.manager import WRITE_PROBE_NAME as PROBE_NAME  # noqa: E402,E501
from jellyfin_mpv_shim.sync.db import (  # noqa: E402
    SyncDB, STATUS_COMPLETE, ORIGIN_USER)

#: A directory on a *different* volume from the temp dir, for the legs that
#: want a real cross-device move rather than a forced one. `X:\jms-reloc` on
#: the Windows VM; anything on a second mount on Linux.
ALT_VOLUME = os.environ.get("JMS_ALT_VOLUME")

ITEMS = 3
MEDIA_BYTES = 48 * 1024


#: The Jellyfin ServerId these fixtures' rows belong to. Set deliberately:
#: a downloads row with no content server is an ORPHAN, and the orphan path
#: is a distinct contract (docs/offline-sync.md section 1).
#: A fixture that omits this silently tests the orphan path under another
#: name -- which is what every row in this file used to do.
CONTENT_SERVER = "srv-content"


class RelocateTestCase(h.TmpDirTest):
    """A real store, and the one assertion every case ends in."""

    def make_store(self, root=None, items=ITEMS):
        """A SyncManager on a real catalog with real media under `root`.

        Goes through `_open_and_run` rather than assigning `db` directly, so
        the worker is running and the catalog has been through the same open
        the app performs -- which is what `relocate` has to stop and redo.
        """
        root = root or os.path.join(self.tmp, "store")
        m = manager_module.SyncManager()
        m.root = root
        m.get_client = lambda uuid: None
        # Offline: nothing here downloads, and a worker that reached the
        # network would make the test's timing the server's.
        m._playback_source = lambda *a, **k: None
        m._download_artwork = lambda *a, **k: None
        m._download_subs = lambda *a, **k: None
        m._download_trickplay = lambda *a, **k: None
        m._download_segments = lambda *a, **k: None
        m._download_series_art = lambda *a, **k: None
        m._download_season_art = lambda *a, **k: None
        m._open_and_run()
        self.addCleanup(m.stop)
        for i in range(items):
            self._add_download(m, i)
        return m

    def _add_download(self, m, i):
        item_id = "%032x" % i
        item_dir = os.path.join(m.root, "srv", item_id)
        os.makedirs(item_dir, exist_ok=True)
        media = os.path.join(item_dir, "media.mkv")
        with open(media, "wb") as fh:
            fh.write(bytes((i + 1,)) * MEDIA_BYTES)
        with open(os.path.join(item_dir, "item.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"Id": item_id, "Type": "Movie",
                       "Name": "Film %d" % i}, fh)
        m.db.upsert({
            "item_id": item_id, "server_uuid": "uuid",
            "content_server_id": CONTENT_SERVER,
            "type": "Movie", "name": "Film %d" % i,
            # Relative to the root. The move relies on it.
            "file_path": os.path.relpath(media, m.root),
            "status": STATUS_COMPLETE, "size_bytes": MEDIA_BYTES,
            "downloaded_bytes": MEDIA_BYTES, "origin": ORIGIN_USER,
        })

    def assert_store_is_usable(self, m, root, items=ITEMS):
        """The store at `root` can still describe and produce its downloads.

        Read through a **fresh** `SyncDB` as well as through the manager's own
        handle: the manager's could be an open connection to a file that is no
        longer where it says it is, which is precisely the state a half-done
        move leaves and the state that reads as fine from inside the process.
        """
        self.assertEqual(os.path.normcase(os.path.realpath(m.root)),
                         os.path.normcase(os.path.realpath(root)),
                         "the manager is not pointed at the folder it "
                         "reported moving to")
        catalog = os.path.join(root, "catalog.db")
        self.assertTrue(os.path.exists(catalog),
                        "no catalog at %s: the store cannot say what the "
                        "files under it are" % root)
        fresh = SyncDB(catalog, read_only=True)
        try:
            self.assertTrue(fresh.healthy(), "the catalog at %s does not read"
                            % root)
            rows = fresh.list()
        finally:
            fresh.close()
        self.assertEqual(len(rows), items,
                         "%d rows survived the move, expected %d"
                         % (len(rows), items))
        for row in rows:
            self.assertTrue(row["file_path"],
                            "row %s lost its file path" % row["item_id"])
            self.assertFalse(os.path.isabs(row["file_path"]),
                             "row %s holds an absolute path, so the next move "
                             "loses it" % row["item_id"])
            full = os.path.join(root, row["file_path"])
            self.assertTrue(os.path.exists(full),
                            "row %s names %s, which is not there"
                            % (row["item_id"], full))
            self.assertEqual(os.path.getsize(full), MEDIA_BYTES,
                             "row %s was truncated by the move"
                             % row["item_id"])

    def assert_nothing_moved(self, m, root, items=ITEMS):
        """A refusal is only correct if it left the store alone."""
        self.assert_store_is_usable(m, root, items)

    def in_the_temp_dir(self):
        """Run this test with the temp dir as cwd.

        For the cases whose path is deliberately malformed. A Windows path or
        one with leading whitespace is *relative* to `abspath` until it is
        cleaned up, so the moment normalization regresses the store lands
        under the current directory -- which is the repo, and which is where
        the mutation run that proved these tests can fail put it. The suite
        already learned this once (`tests/test_sync_manager.py`'s cross-drive
        case says so); a test that can dirty the tree it is testing is a bad
        trade for one line.
        """
        here = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, here)

    def alt_dir(self, name):
        """A directory on another volume, or skip."""
        if not ALT_VOLUME:
            self.skipTest("set JMS_ALT_VOLUME to a path on a second volume")
        path = os.path.join(ALT_VOLUME, "jms-e2e-%d-%s" % (os.getpid(), name))
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path


class MovesThatMustWorkTest(RelocateTestCase):

    def test_a_destination_that_does_not_exist_yet(self):
        m = self.make_store()
        dest = os.path.join(self.tmp, "elsewhere")
        ok, message = m.relocate(dest)
        self.assertTrue(ok, message)
        self.assert_store_is_usable(m, dest)

    def test_a_destination_that_already_exists_and_is_empty(self):
        """The folder a person makes in their file manager *before* coming
        here to point at it, which is the ordinary way to do this. It looks
        identical to the not-yet-created case from the outside and is a
        different branch inside: `makedirs(exist_ok=True)` is a no-op, the
        emptiness check has a real `listdir` to run, and the writability of
        the folder is somebody else's decision rather than ours."""
        m = self.make_store()
        dest = os.path.join(self.tmp, "made-in-advance")
        os.makedirs(dest)
        ok, message = m.relocate(dest)
        self.assertTrue(ok, message)
        self.assert_store_is_usable(m, dest)

    def test_a_destination_whose_parents_do_not_exist(self):
        m = self.make_store()
        dest = os.path.join(self.tmp, "media", "jellyfin", "downloads")
        ok, message = m.relocate(dest)
        self.assertTrue(ok, message)
        self.assert_store_is_usable(m, dest)

    def test_a_destination_written_with_a_trailing_separator(self):
        """Typed by anyone who tab-completes, and on Windows by anyone who
        copies a folder path out of the address bar."""
        m = self.make_store()
        dest = os.path.join(self.tmp, "trailing")
        ok, message = m.relocate(dest + os.sep)
        self.assertTrue(ok, message)
        self.assert_store_is_usable(m, dest)

    def test_a_destination_pasted_with_quotes_around_it(self):
        """Windows Explorer's **Copy as path** produces `"C:\\...\\x"`, quotes
        included, and that is how a Windows user gets a path into a text
        field. A double quote cannot appear in an NTFS name, so the quoted
        spelling names nothing that can ever exist -- the refusal was
        `Can't create that folder`, which is a sentence about permissions for
        a path that was only mis-typed."""
        self.in_the_temp_dir()
        m = self.make_store()
        dest = os.path.join(self.tmp, "pasted")
        ok, message = m.relocate('"%s"' % dest)
        self.assertTrue(ok, message)
        self.assert_store_is_usable(m, dest)

    def test_a_destination_pasted_with_whitespace_around_it(self):
        self.in_the_temp_dir()
        m = self.make_store()
        dest = os.path.join(self.tmp, "spaced")
        ok, message = m.relocate("  %s\t" % dest)
        self.assertTrue(ok, message)
        self.assert_store_is_usable(m, dest)

    def test_a_move_to_another_volume_copies_the_bytes(self):
        """The EXDEV path for real: `os.rename` cannot cross a mount, so every
        entry is copied and then removed. This is the move the progress bar
        and the out-of-space message exist for, and on Windows it is the
        ordinary case -- downloads go on the big drive, not on C:."""
        m = self.make_store()
        dest = self.alt_dir("crossvolume")
        seen = []
        ok, message = m.relocate(dest,
                                 progress=lambda c, t: seen.append((c, t)))
        self.assertTrue(ok, message)
        self.assert_store_is_usable(m, dest)
        self.assertTrue(seen, "a cross-volume copy reported no progress, so "
                              "the UI shows a frozen window for its duration")
        self.assertEqual([c for c, _t in seen],
                         sorted(c for c, _t in seen),
                         "progress went backwards")

    def test_a_move_to_another_volume_that_already_exists_and_is_empty(self):
        m = self.make_store()
        dest = self.alt_dir("crossvolume-empty")
        os.makedirs(dest)
        ok, message = m.relocate(dest)
        self.assertTrue(ok, message)
        self.assert_store_is_usable(m, dest)

    def test_the_copy_path_runs_even_on_a_one_volume_machine(self):
        """The same code as the case above, reached by making `os.rename`
        refuse. Unconditional, so the copy/remove path is covered on a box
        with nowhere else to put anything -- which is most CI."""
        m = self.make_store()
        dest = os.path.join(self.tmp, "forced-exdev")
        real_rename = os.rename

        def refuse(src, dst, *a, **k):
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        os.rename = refuse
        try:
            ok, message = m.relocate(dest)
        finally:
            os.rename = real_rename
        self.assertTrue(ok, message)
        self.assert_store_is_usable(m, dest)

    def test_a_copy_that_completes_short_does_not_take_the_original(self):
        """A move may carry the user's unrelated files -- that is expected --
        but **a failed copy must not lose anything**. [iw]:
        docs/offline-sync-goals.md G5.

        The copy loop raises on a real write error (ENOSPC), and that path is
        covered: the sources are only dropped after every entry is across.
        What was not covered is a copy that *finishes without raising* and is
        nonetheless short -- a truncating filesystem, a network mount that
        buffers and loses, an interrupted chunk that does not surface. Nothing
        compared the destination with the source before `_discard` removed the
        original, so a silent short copy was an unrecoverable loss of both the
        download and whatever else lived beside it.
        """
        m = self.make_store()
        mine = os.path.join(m.root, "tax-return.pdf")
        with open(mine, "wb") as fh:
            fh.write(b"x" * 4096)
        dest = os.path.join(self.tmp, "short-copy")

        real_copy = manager_module.SyncManager._copy_tree

        def truncating(self_, src, dst, state, progress):
            real_copy(self_, src, dst, state, progress)
            # Whatever landed, make one file short without erroring.
            if os.path.isfile(dst) and os.path.getsize(dst) > 1:
                with open(dst, "r+b") as fh:
                    fh.truncate(os.path.getsize(dst) - 1)

        def refuse(src, dst, *a, **k):
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        real_rename = os.rename
        os.rename = refuse
        manager_module.SyncManager._copy_tree = truncating
        try:
            ok, message = m.relocate(dest)
        finally:
            os.rename = real_rename
            manager_module.SyncManager._copy_tree = real_copy

        self.assertFalse(
            ok, "a short copy reported success, so the originals were dropped")
        self.assertTrue(
            os.path.exists(mine),
            "the user's own file was removed after a copy that did not "
            "produce the same bytes")
        self.assertEqual(4096, os.path.getsize(mine),
                         "the surviving original is not intact")
        self.assertTrue(os.path.isdir(m.root),
                        "the store was left pointing at nothing")

    def test_moving_twice_in_a_row(self):
        """The second move starts from a store this code produced rather than
        one the test built, which is the only way to find out that the first
        move left it in a shape the second can read."""
        m = self.make_store()
        first = os.path.join(self.tmp, "first")
        ok, message = m.relocate(first)
        self.assertTrue(ok, message)
        second = os.path.join(self.tmp, "second")
        ok, message = m.relocate(second)
        self.assertTrue(ok, message)
        self.assert_store_is_usable(m, second)
        self.assertFalse(os.path.exists(os.path.join(first, "catalog.db")),
                         "the first destination still holds a catalog, so two "
                         "folders now claim to be the download folder")

    def _default_root(self):
        """Where `start()` puts the store when the setting is empty.

        Resolved through the same two calls the app makes rather than spelled
        out here, because the point of these two tests is that `relocate` and
        `start` agree about it. The integration harness primes `--config` with
        a throwaway directory at import, so this is inside the temp config and
        not the developer's own.
        """
        from jellyfin_mpv_shim.conffile import confdir
        from jellyfin_mpv_shim.constants import APP_NAME
        return os.path.join(confdir(APP_NAME), "offline")

    def test_clearing_the_setting_moves_it_back_to_the_default(self):
        """Emptying the field is how a person asks for the default back, and
        it has to land on the path the *next launch* will look in.

        `start()` reads `normalize_root(settings.sync_path) or <confdir>/offline`
        and `relocate` computes the same fallback for a falsy argument. If those
        two ever disagree the downloads end up in a folder nothing opens, which
        is indistinguishable from having lost them -- and the existing coverage
        was `normalize_root("") is None`, which is the first half of that
        sentence and not the second.
        """
        default = self._default_root()
        # Both tests here move into the SAME folder -- it is the one path in
        # this module the test cannot choose -- so each leaves it empty again.
        # Without this they pass or fail on alphabetical order.
        self.addCleanup(shutil.rmtree, default, ignore_errors=True)
        self.assertFalse(os.path.exists(os.path.join(default, "catalog.db")),
                         "the default folder already holds a store, so this "
                         "would be testing the refusal instead")
        m = self.make_store(root=os.path.join(self.tmp, "somewhere-else"))
        ok, message = m.relocate("")
        self.assertTrue(ok, message)
        self.assert_store_is_usable(m, default)

    def test_and_then_asking_again_says_it_is_already_there(self):
        """The follow-on, because "already there" is reported as success with
        a message and a dead Move button once read as a working one."""
        self.addCleanup(shutil.rmtree, self._default_root(), ignore_errors=True)
        m = self.make_store(root=os.path.join(self.tmp, "somewhere-else-2"))
        ok, _message = m.relocate("")
        self.assertTrue(ok)
        ok, message = m.relocate(None)
        self.assertTrue(ok)
        self.assertTrue(message, "a move that did not happen reported no "
                                 "message, so the caller claims one did")
        self.assert_store_is_usable(m, self._default_root())


class GuardsThatMustRefuseTest(RelocateTestCase):
    """Every one of these has to leave the store exactly where it was. The
    message matters as much as the refusal: this is a text field with no
    folder picker behind it, so the message is the only thing that tells the
    user what to type instead."""

    def test_a_folder_with_the_users_own_files_in_it(self):
        m = self.make_store()
        dest = os.path.join(self.tmp, "holidays")
        os.makedirs(dest)
        with open(os.path.join(dest, "2019 Italy.jpg"), "wb") as fh:
            fh.write(b"x")
        ok, message = m.relocate(dest)
        self.assertFalse(ok)
        self.assertIn("empty", message.lower())
        self.assertTrue(os.path.exists(os.path.join(dest, "2019 Italy.jpg")),
                        "the refusal still touched the user's files")
        self.assert_nothing_moved(m, os.path.join(self.tmp, "store"))

    def test_our_own_leftover_write_probe_is_not_the_users_clutter(self):
        """The one non-empty folder this app is responsible for.

        `_destination_is_writable` writes a probe and removes it, and
        tolerates the removal failing -- a Windows scanner holding the handle
        for a moment is enough. If that move is then refused for any other
        reason, the file stays, and the user's next attempt at the folder
        THEY chose is refused as "not empty" over litter only this app
        writes, with no way to see it: it is a dotfile, and the message
        blames them for it.
        """
        m = self.make_store()
        dest = os.path.join(self.tmp, "mine")
        os.makedirs(dest)
        with open(os.path.join(dest, PROBE_NAME), "wb") as fh:
            fh.write(b"")
        ok, message = m.relocate(dest)
        self.assertTrue(ok, "refused the user's own folder over our own "
                            "leftover probe: %s" % message)
        self.assert_store_is_usable(m, dest)

    def test_but_a_dotfile_we_did_not_write_still_counts(self):
        """The negative control. "Ignore hidden files" would be a different
        and much worse rule: a `.stfolder`, a `.nomedia` or a Syncthing
        marker means somebody else is managing that folder, which is exactly
        when the store must not take it over."""
        m = self.make_store()
        dest = os.path.join(self.tmp, "syncthing")
        os.makedirs(dest)
        with open(os.path.join(dest, ".stfolder"), "wb") as fh:
            fh.write(b"")
        ok, message = m.relocate(dest)
        self.assertFalse(ok)
        self.assertIn("empty", message.lower())
        self.assert_nothing_moved(m, os.path.join(self.tmp, "store"))

    def test_a_folder_that_already_holds_downloads_says_so_differently(self):
        """The one non-empty folder somebody picks deliberately: their own
        store, from another install or another user. "Choose an empty folder"
        reads as a refusal to find downloads that are plainly there."""
        m = self.make_store()
        dest = os.path.join(self.tmp, "other-install")
        os.makedirs(dest)
        other = SyncDB(os.path.join(dest, "catalog.db"))
        other.close()
        ok, message = m.relocate(dest)
        self.assertFalse(ok)
        self.assertIn("already contains downloads", message.lower())
        self.assert_nothing_moved(m, os.path.join(self.tmp, "store"))

    def test_a_folder_inside_the_current_download_folder(self):
        """`_copy_tree` would walk into the destination it is creating."""
        m = self.make_store()
        ok, message = m.relocate(os.path.join(m.root, "inside"))
        self.assertFalse(ok)
        self.assertIn("inside", message.lower())
        self.assert_nothing_moved(m, os.path.join(self.tmp, "store"))

    def test_the_folder_it_is_already_in(self):
        """Succeeds -- there is nothing to do -- but it must *say* that. An
        empty message makes the caller print "Download folder moved", which is
        how a Move button that does nothing reads as one that works."""
        m = self.make_store()
        root = m.root
        ok, message = m.relocate(root)
        self.assertTrue(ok)
        self.assertTrue(message, "a move that did nothing claimed to have "
                                 "moved something")
        self.assert_nothing_moved(m, root)

    def test_the_folder_it_is_already_in_spelled_differently(self):
        """Windows is case-insensitive, so `C:\\Downloads` and `c:\\downloads`
        are one folder -- and the equality check compared strings. The fall
        through was into the *containment* test, which answered, correctly and
        uselessly, that the folder is inside itself: a user who retyped their
        own path with the drive letter in the other case was told to choose a
        folder outside the one they were already in.

        On a case-sensitive filesystem the two names really are two folders,
        so this asserts the property both platforms share -- the answer is
        never "it is inside itself" -- and the identity only where the
        filesystem agrees they are the same."""
        m = self.make_store()
        root = m.root
        ok, message = m.relocate(root.upper())
        self.assertNotIn("inside", (message or "").lower(),
                         "told the user their own download folder is inside "
                         "itself")
        if os.path.normcase(root) == os.path.normcase(root.upper()):
            self.assertTrue(ok, message)
            self.assertTrue(message, "the no-op move said nothing")
        self.assert_nothing_moved(m, root)

    def test_the_folder_it_is_already_in_reached_through_a_link(self):
        m = self.make_store()
        link = os.path.join(self.tmp, "link-to-store")
        try:
            os.symlink(m.root, link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("no symlinks here")
        ok, message = m.relocate(link)
        self.assertTrue(ok, message)
        self.assertTrue(message, "a move through a link to the same folder "
                                 "claimed to have moved something")
        self.assert_nothing_moved(m, os.path.join(self.tmp, "store"))

    @unittest.skipIf(os.name == "nt",
                     "chmod does not describe an NTFS ACL; the probe itself "
                     "is asserted directly below on both platforms")
    def test_a_folder_this_process_may_not_write_to(self):
        """Empty, listable, and not ours: another account's folder, a
        read-only mount, a share exported ro. It passes every other check, and
        without the pre-flight probe it gets as far as **stopping the download
        worker and closing the catalog** before the move fails.

        The message alone cannot see that -- the errno branch on the failure
        path says "write" too -- so what is asserted is the generation
        counter: `_open_and_run` bumps it, so an unmoved counter is the
        evidence that the store was never taken down and put back for a move
        that was never going to happen."""
        m = self.make_store()
        dest = os.path.join(self.tmp, "not-mine")
        os.makedirs(dest)
        os.chmod(dest, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, dest, 0o700)
        before = m._generation
        ok, message = m.relocate(dest)
        self.assertFalse(ok, "accepted a folder it cannot write to")
        self.assertIn("write", message.lower())
        self.assertEqual(m._generation, before,
                         "the store was stopped and reopened for a move that "
                         "was refused; the refusal came too late")
        self.assert_nothing_moved(m, os.path.join(self.tmp, "store"))

    def test_the_write_probe_is_a_write_and_tidies_up(self):
        """`os.access(W_OK)` was the obvious way to ask and is the wrong one:
        on Windows it reports mode bits that do not describe an ACL at all, so
        a folder denied to this account answers True. The probe writes.

        Asserted directly because the ACL half cannot be set up portably, and
        because the tidy-up is not incidental -- a probe file left behind is a
        file in a folder the emptiness check has just certified as empty."""
        dest = os.path.join(self.tmp, "probe-target")
        os.makedirs(dest)
        self.assertTrue(
            manager_module.SyncManager._destination_is_writable(dest))
        self.assertEqual(os.listdir(dest), [],
                         "the probe left its test file behind")
        self.assertFalse(
            manager_module.SyncManager._destination_is_writable(
                os.path.join(dest, "no", "such", "folder")),
            "the probe said yes about a folder that is not there")

    def test_while_a_download_is_running(self):
        """Moving the tree out from under an open `.part` handle is the
        corruption this refuses. On Windows it is not even possible -- the
        file is locked -- so the refusal is what stops it being reported as
        "moving failed" there and as a corrupt download everywhere else."""
        m = self.make_store()
        with m._active_lock:
            m._claim_active(1, "%032x" % 0)
        try:
            ok, message = m.relocate(os.path.join(self.tmp, "nope"))
        finally:
            with m._active_lock:
                m._release_active(1)
        self.assertFalse(ok)
        self.assertIn("download", message.lower())
        self.assert_nothing_moved(m, os.path.join(self.tmp, "store"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nope",
                                                     "catalog.db")))

    def test_when_the_worker_will_not_stop(self):
        """The claim is sampled before `stop()`, and the chunk loop only
        notices between chunks -- a stalled socket parks it there for the read
        timeout. A move that went ahead anyway would leave the abandoned
        worker appending to a `.part` while a second worker started on the
        same rows at the new root."""
        m = self.make_store()
        real_stop = m.stop

        def will_not_stop():
            real_stop()
            return False

        m.stop = will_not_stop
        try:
            ok, message = m.relocate(os.path.join(self.tmp, "nope"))
        finally:
            m.stop = real_stop
        self.assertFalse(ok)
        self.assert_nothing_moved(m, os.path.join(self.tmp, "store"))


class AFailedMoveLeavesAStoreBehindTest(RelocateTestCase):
    """The half that matters more than any refusal: when the move itself
    fails, what the user is told is "they were left in place", and this is
    what has to make that true."""

    def _fail_partway(self, m, dest, exc):
        """Let the first entry across and fail on the next one."""
        real_copy = m._copy_tree
        real_rename = os.rename
        state = {"n": 0}

        def counted(*a, **k):
            state["n"] += 1
            if state["n"] > 1:
                raise exc
            return real_copy(*a, **k)

        def refuse(src, dst, *a, **k):
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        m._copy_tree = counted
        os.rename = refuse
        try:
            return m.relocate(dest)
        finally:
            os.rename = real_rename
            m._copy_tree = real_copy

    def test_a_full_disk_says_so_and_changes_nothing(self):
        m = self.make_store()
        root = m.root
        ok, message = self._fail_partway(
            m, os.path.join(self.tmp, "full"),
            OSError(errno.ENOSPC, "No space left on device"))
        self.assertFalse(ok)
        self.assertIn("space", message.lower())
        self.assert_nothing_moved(m, root)

    def test_a_permission_failure_says_so_and_changes_nothing(self):
        """The backstop behind the pre-flight probe: a denial that appears
        part way into the tree, or after the probe passed. "Moving the
        downloads failed" sent people looking for a bug in this app."""
        m = self.make_store()
        root = m.root
        ok, message = self._fail_partway(
            m, os.path.join(self.tmp, "denied"),
            OSError(errno.EACCES, "Permission denied"))
        self.assertFalse(ok)
        self.assertIn("write", message.lower())
        self.assert_nothing_moved(m, root)

    def test_the_downloads_are_still_playable_after_a_failed_move(self):
        """`assert_nothing_moved` is the whole point of the three tests above,
        and this states why: the failure path reopens the catalog at the old
        root, and the old root has to still be a store. A move that took the
        catalog across first and then failed reopened here with no catalog at
        all, and the startup orphan sweep finished the job -- while the user
        was being told nothing had been touched."""
        m = self.make_store()
        root = m.root
        self._fail_partway(m, os.path.join(self.tmp, "doomed"),
                           OSError(errno.ENOSPC, "No space left on device"))
        self.assert_store_is_usable(m, root)
        # And the abandoned destination holds no half-copy that a retry would
        # skip over as "already there".
        doomed = os.path.join(self.tmp, "doomed")
        leftovers = os.listdir(doomed) if os.path.isdir(doomed) else []
        self.assertEqual(leftovers, [],
                         "a failed move left %s behind at the destination"
                         % leftovers)

    def test_a_retry_after_the_failure_succeeds(self):
        """What the user does next. It is also the check that the rollback
        left no duplicate: a retry that hit "already there, skip it" would
        finish a partial tree and report success over a store missing half
        its media -- which `assert_store_is_usable` is what catches."""
        m = self.make_store()
        dest = os.path.join(self.tmp, "retry")
        self._fail_partway(m, dest,
                           OSError(errno.ENOSPC, "No space left on device"))
        ok, message = m.relocate(dest)
        self.assertTrue(ok, message)
        self.assert_store_is_usable(m, dest)


class ThePathTheSettingRemembersTest(RelocateTestCase):
    """`settings.sync_path` is a JSON key a person can edit by hand, and
    `start()` reads it raw. A spelling the settings field would clean up but
    the launch path would not means the app opens a *different folder* from
    the one the UI says it is using."""

    def test_start_and_relocate_agree_about_a_pasted_path(self):
        raw = '  "%s"  ' % os.path.join(self.tmp, "typed-by-hand")
        self.assertEqual(manager_module.normalize_root(raw),
                         os.path.join(self.tmp, "typed-by-hand"))

    def test_normalize_leaves_an_ordinary_path_alone(self):
        for path in (os.path.join(self.tmp, "plain"),
                     r"C:\Users\someone\Downloads",
                     "/home/someone/Downloads"):
            self.assertEqual(manager_module.normalize_root(path), path)

    def test_an_empty_or_blank_setting_means_the_default(self):
        for blank in (None, "", "   ", '""'):
            self.assertIsNone(manager_module.normalize_root(blank))

    def test_a_quote_inside_a_path_is_not_stripped(self):
        """Only a *matched surrounding pair* is a paste artifact. A quote in
        the middle is part of the name on every filesystem that allows one."""
        self.assertEqual(manager_module.normalize_root('/home/o"brien/dl'),
                         '/home/o"brien/dl')


if __name__ == "__main__":
    unittest.main(verbosity=2)
