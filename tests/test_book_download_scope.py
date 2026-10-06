"""Which server's downloaded copy a book reader is handed. CX7.

`db.get` is a primary-key read, and **item ids collide across servers** --
Jellyfin derives one from the media's path
(docs/jellyfin-api-notes.md 13b). Every other content read went through
`_content_clause`; this one had no scope parameter at all, so the browser's
`book_download_state` could answer with a row server A wrote for an item
server B is showing. For a book that matters more than for anything else: the
app cannot render one, so the downloaded file is not an offline convenience,
it is the only way to read the thing.

The ~13 readers inside `sync/` are unscoped on purpose -- they already hold
the row's identity and are asking about the file on disk -- so the capability
is defaulted there and **required** at the browser's door, where a default
would let a new call site ask the permissive question by omission.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import json
import os
import shutil
import tempfile
import unittest

from jellyfin_mpv_shim.sync.db import (ANY_SERVER, COLUMNS, STATUS_COMPLETE,
                                       STATUS_PENDING, SyncDB)

A = "server-a"
B = "server-b"
#: One id, two servers -- which is the whole finding. There is only ever ONE
#: row for it, because `downloads.item_id` is the primary key: the collision
#: is not two rows to choose between, it is one row answering for a server it
#: does not belong to.
BOOK = "collides"


def _row(item_id, server_id, status=STATUS_COMPLETE, path="b/book.epub"):
    row = {c: None for c in COLUMNS}
    row.update({"item_id": item_id, "content_server_id": server_id,
                "status": status, "type": "Book", "name": item_id,
                "file_path": path,
                "item_json": json.dumps({"Id": item_id, "Type": "Book"})})
    return row


class TheCatalogReadIsScopableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = SyncDB(os.path.join(self.tmp, "catalog.db"))
        self.addCleanup(self.db.close)
        self.db.upsert(_row(BOOK, A))

    def test_another_servers_row_does_not_answer(self):
        self.assertIsNone(self.db.get(BOOK, server_id=B))

    def test_its_own_servers_row_does(self):
        """The control: a scope that answers nothing for every input would
        pass the test above."""
        self.assertEqual(self.db.get(BOOK, server_id=A)["item_id"], BOOK)

    def test_unscoped_is_the_default_because_sync_asks_that_way(self):
        """The ~13 callers inside `sync/` hold the row's identity already --
        the worker resolving what to download next is not asking a content
        question. Left required, every one of them would have had to invent a
        scope, and the honest answer for them is "this row"."""
        self.assertEqual(self.db.get(BOOK)["item_id"], BOOK)
        self.assertEqual(self.db.get(BOOK, server_id=ANY_SERVER)["item_id"],
                         BOOK)

    def test_a_login_that_resolves_to_nothing_answers_nothing(self):
        """`None` is not "any": it is `content_id_for` failing on a login that
        was named, and answering every server's rows to that is the wrong
        direction to be permissive in. Same rule as every other content
        read -- this goes through `_content_clause` rather than carrying its
        own copy of it."""
        self.assertIsNone(self.db.get(BOOK, server_id=None))

    def test_a_row_with_no_server_answers_only_the_unscoped_ask(self):
        """Step 5 took the clause's NULL branch out. A row whose manifest
        could not be read is not an answer to a *named* server -- for a book
        that is the difference between opening the right file and opening
        another server's -- and it stays reachable through every unscoped
        caller, with the offline library listing it under Orphaned Items."""
        self.db.upsert(_row("unhomed", None))
        self.assertIsNone(self.db.get("unhomed", server_id=B))
        self.assertEqual(self.db.get("unhomed")["item_id"], "unhomed",
                         "it is now invisible to the unscoped readers too")

    def test_but_a_falsy_scope_does_not_even_get_the_unhomed_row(self):
        """The two rules meet here, and this is the pair that tells them
        apart: an unhomed row answers a *named* server, and a falsy scope
        answers **nothing at all** -- `_content_clause` asks for no rows
        rather than for the NULL ones.

        Pinned here because a mutation round found that no behavioural test in
        the suite covers that branch: breaking it failed only the
        mutation-plan guard, which checks that a pattern still matches and not
        that anything observes it.
        """
        self.db.upsert(_row("unhomed", None))
        self.assertIsNone(self.db.get("unhomed", server_id=None))


class TheReaderIsHandedItsOwnServersFileTest(unittest.TestCase):
    """The same question through the gateway, which is where the finding was
    reported: a colliding id on server B opened server A's downloaded file."""

    def setUp(self):
        from unittest import mock

        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway
        import jellyfin_mpv_shim.sync.manager as mgr

        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = SyncDB(os.path.join(self.tmp, "catalog.db"))
        self.addCleanup(self.db.close)
        self.db.upsert(_row(BOOK, A))
        # A real file, because `book_download_state` stats it: a catalog that
        # says complete and a file that is gone is a state it reports as not
        # downloaded, and without the file every assertion here would pass for
        # that reason instead.
        os.makedirs(os.path.join(self.tmp, "b"), exist_ok=True)
        with open(os.path.join(self.tmp, "b", "book.epub"), "wb") as fh:
            fh.write(b"x")          # bytes: nothing reads this, it must exist

        db, root = self.db, self.tmp

        class FakeSync:
            pass

        FakeSync.db = db
        FakeSync.root = root
        #: login uuid -> ServerId, the translation the gateway does before it
        #: touches the catalog. Modelled rather than stubbed to identity: the
        #: browser speaks in logins and the catalog in ServerIds, and a fake
        #: that conflated them would make the conversion untested.
        FakeSync.content_id_for = staticmethod(
            lambda uuid: {"uuid-a": A, "uuid-b": B}.get(uuid))
        patcher = mock.patch.object(mgr, "syncManager", FakeSync())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctl = PlayerGateway()

    def test_the_owning_server_gets_the_path(self):
        status, path = self.ctl.book_download_state(BOOK, "uuid-a")
        self.assertEqual(status, STATUS_COMPLETE)
        self.assertTrue(path and os.path.exists(path))

    def test_another_server_is_told_nothing_is_downloaded(self):
        self.assertEqual(self.ctl.book_download_state(BOOK, "uuid-b"),
                         (None, None))

    def test_and_the_desktop_is_handed_no_file_for_it(self):
        """The one that matters: this method is what hands a path to the
        user's reader application."""
        ok, _method = self.ctl.open_downloaded_file(BOOK, "uuid-b")
        self.assertFalse(ok)

    def test_a_login_that_resolves_to_no_server_opens_nothing(self):
        """An unknown login resolves to None, which matches no rows -- not to
        "any", which would open whatever is on disk."""
        self.assertEqual(self.ctl.book_download_state(BOOK, "uuid-gone"),
                         (None, None))

    def test_a_pending_download_reports_its_status_without_a_path(self):
        """Unchanged behaviour, re-checked because the scope now sits between
        the two: status is still reported for the owning server so the button
        can say "downloading", and a path is only ever set with complete."""
        self.db.update(BOOK, status=STATUS_PENDING)
        self.assertEqual(self.ctl.book_download_state(BOOK, "uuid-a"),
                         (STATUS_PENDING, None))


class ThePagesAskAboutTheServerTheyAreOnTest(unittest.TestCase):
    """The nine call sites. Every one had a server in hand already -- the
    point of CX7 is that none of them was passing it.

    Driven through `navigate` rather than by constructing a Page, so the route
    the page reads is the one the shell really builds.
    """

    def _browser(self, current="uuid-a"):
        from tests._shell_harness import FakeController, FakeSource, _SyncPool
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

        src = FakeSource()
        src.items[BOOK] = {"Id": BOOK, "Name": "A Book", "Type": "Book"}
        ctl = FakeController()
        # Deliberately "downloaded": a page that gets (None, None) may stop
        # before it asks a second time, and what is under test is the asking.
        ctl.book_downloads = {BOOK: (STATUS_COMPLETE, "/tmp/nope.epub")}
        b = MpvtkBrowser(app=None, source=src, controller=ctl)
        b._pool = _SyncPool()
        b.server = current
        return b, ctl

    @staticmethod
    def _asked_servers(ctl):
        return [srv for _iid, srv in ctl.book_state_asked]

    def test_the_reader_asks_about_the_route_s_server(self):
        b, ctl = self._browser(current="uuid-a")
        b.navigate({"kind": "reader", "server": "uuid-b",
                    "item_id": BOOK, "title": "A Book"})
        self.assertIn("uuid-b", self._asked_servers(ctl),
                      "the page asked the catalog without naming a server")
        self.assertNotIn("uuid-a", self._asked_servers(ctl),
                         "it asked about the browser's current server rather "
                         "than the one this screen is showing")

    def test_the_comic_reader_does_too(self):
        b, ctl = self._browser(current="uuid-a")
        b.navigate({"kind": "comic", "server": "uuid-b",
                    "item_id": BOOK, "title": "A Book"})
        self.assertIn("uuid-b", self._asked_servers(ctl))
        self.assertNotIn("uuid-a", self._asked_servers(ctl))

    def test_and_the_refresh_hook_keeps_the_screen_s_server(self):
        """The hook fires from the shell when the catalog changes, which can
        be while the browser has moved on. The state being refreshed is the
        one this screen drew, so it asks with the route's server rather than
        whatever is current."""
        b, ctl = self._browser(current="uuid-a")
        b.navigate({"kind": "reader", "server": "uuid-b",
                    "item_id": BOOK, "title": "A Book"})
        del ctl.book_state_asked[:]
        page = b._page_for(b.route)
        page.refresh_download_state()
        self.assertEqual(self._asked_servers(ctl), ["uuid-b"])


class APendingReadOpensOnItsOwnServerTest(unittest.TestCase):
    """`_pending_reads` is keyed by item id and the id is not enough.

    Press Read on a book that is not downloaded and the entry waits for the
    worker; `flush_pending_reads` then asks the catalog and opens the file. The
    server is part of the value because the key cannot carry it -- and without
    it the flush asks unscoped and can open another server's copy.
    """

    def _actions(self):
        from unittest import mock

        from jellyfin_mpv_shim.mpvtk_browser.item_actions import ItemActions
        from tests._shell_harness import FakeController

        ctl = FakeController()
        ctl.book_downloads = {BOOK: (STATUS_COMPLETE, "/tmp/nope.epub")}
        services = mock.Mock()
        services.controller = ctl
        actions = ItemActions(services=services, run=None, dialogs=None,
                              on_launch=lambda *a, **k: None)
        return actions, ctl

    def test_the_flush_asks_and_opens_on_the_pressing_server(self):
        actions, ctl = self._actions()
        actions._pending_reads[BOOK] = ("A Book", "uuid-b")
        actions.flush_pending_reads()
        self.assertEqual(ctl.book_state_asked, [(BOOK, "uuid-b")])
        self.assertEqual(ctl.opened_on, ["uuid-b"],
                         "the file was opened without naming a server")

    def test_pressing_read_records_the_server_it_was_pressed_on(self):
        """The write, not the read. Setting `_pending_reads` by hand (as the
        tests above do) exercises the flush and leaves the *recording*
        untested -- and a mutation that stored `None` for the server passed
        every one of them.
        """
        import types

        actions, ctl = self._actions()
        # Not downloaded, so the press enqueues and registers a pending read.
        ctl.book_downloads = {}
        actions.run = types.SimpleNamespace(
            epoch=0,
            run=lambda work, done, ep, **kw: done(work()))
        # Two neighbours of the branch under test, both modelled rather than
        # left to the Mock: `offline` answers truthy by default, which sends
        # the press down the "nothing to fetch from" branch and records no
        # pending read at all -- so this test would have passed against any
        # implementation.
        actions.services.offline = False
        actions.can_download = lambda server: True
        actions.read_book({"Id": BOOK, "Name": "A Book", "Type": "Book"},
                          server="uuid-b")
        self.assertEqual(actions._pending_reads.get(BOOK), ("A Book", "uuid-b"))

    def test_pressing_read_on_a_downloaded_book_opens_it_on_that_server(self):
        """The other half of the press: already on disk, so there is no
        pending read at all and the file is opened straight away. A separate
        branch from the flush's open, and it had no check -- the mutation that
        dropped its server passed everything else here.
        """
        import types

        actions, ctl = self._actions()
        actions.run = types.SimpleNamespace(
            epoch=0,
            run=lambda work, done, ep, **kw: done(work()))
        actions.services.offline = False
        actions.can_download = lambda server: True
        actions.read_book({"Id": BOOK, "Name": "A Book", "Type": "Book"},
                          server="uuid-b")
        self.assertEqual(actions._pending_reads, {},
                         "a book already on disk should wait for nothing")
        self.assertEqual(ctl.opened_on, ["uuid-b"])

    def test_the_entry_is_still_consumed_once(self):
        """The check-then-pop that stops two downloads finishing together
        launching the reader twice -- re-checked because the value's shape
        changed under it."""
        actions, ctl = self._actions()
        actions._pending_reads[BOOK] = ("A Book", "uuid-b")
        actions.flush_pending_reads()
        actions.flush_pending_reads()
        self.assertEqual(ctl.opened, [BOOK])


if __name__ == "__main__":
    unittest.main()
