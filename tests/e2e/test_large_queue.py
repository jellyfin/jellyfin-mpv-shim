"""A queue big enough to overflow the request line.

Reported 2026-07-14: "one of my playlists does not show titles, artists, or
runtimes in the queue. The other does... `HTTPException: (414, HTTPError('414
Client Error: Request-URI Too Large'))` ... turns out the playlist is too big
to request metadata for all at once."

`get_items_by_ids` batches at `CHUNK = 100` because of it. Nothing executed
that: the limit belongs to Kestrel and to whatever reverse proxy is in front
of it, not to the client, so a fake accepts a URL of any length and the
batching is decoration as far as the suite is concerned.

Measured against this server, which is where the numbers below come from:
100 ids fine, 200 ids fine, **400 ids `414 URI Too Long`**. So the control
matters — `test_a_single_request_of_this_size_would_fail` is what stops every
other test here passing just as well with the batching deleted.
"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

#: Comfortably past the measured 414 threshold, and past three chunks.
BIG = 400


@_e2e.require_server
class LargeQueueMetadataTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from jellyfin_mpv_shim.mpvtk_browser.repository import LibrarySource
        cls.CHUNK = 100     # mirrors get_items_by_ids; asserted below
        cls.session = _e2e.Session()
        cls.source = cls.session.library_source()
        libs = cls.source.get_libraries(_e2e.SOURCE_UUID)
        music = [lib for lib in libs if lib["Name"] == "Bulk Music"]
        if not music:
            cls.source.stop()
            cls.session.stop()
            raise unittest.SkipTest(
                "no Bulk Music library — this test is about a queue too big "
                "for one request and there is nothing big enough")
        cls.songs = cls.session.find_all(
            parent_id=music[0]["Id"], item_type="Audio", Limit=BIG + 50)
        cls.ids = [s["Id"] for s in cls.songs]
        if len(cls.ids) < BIG:
            cls.source.stop()
            cls.session.stop()
            raise unittest.SkipTest(
                "only %d tracks; need %d to overflow the request line"
                % (len(cls.ids), BIG))
        cls.api = cls.source._conn(_e2e.SOURCE_UUID).api

    @classmethod
    def tearDownClass(cls):
        try:
            cls.source.stop()
        finally:
            cls.session.stop()

    def test_a_single_request_of_this_size_would_fail(self):
        """The control. Without it every test below passes with the batching
        removed, and this file would be testing nothing at all."""
        # The apiclient logs the whole rejected URL, which for 400 GUIDs is
        # 30KB of console noise on a test that is *supposed* to fail.
        #
        # `Jellyfin.`, not `JELLYFIN.`: http.py is the one module in the
        # apiclient that spells its logger root in mixed case, and logger
        # names are case-sensitive -- so the shouted spelling silenced a
        # logger that does not exist and the dump came out anyway.
        http_log = logging.getLogger("Jellyfin.jellyfin_apiclient_python.http")
        previous = http_log.level
        http_log.setLevel(logging.CRITICAL)
        self.addCleanup(http_log.setLevel, previous)
        with self.assertRaises(Exception) as caught:
            self.api.get_items(self.ids[:BIG], fields="")
        self.assertIn(
            "414", str(caught.exception),
            "a single request for %d ids did not fail with 414, so the "
            "server's request-line limit is higher than it was when the "
            "batching was written and these tests no longer prove it is "
            "needed: %s" % (BIG, caught.exception))

    def test_every_row_gets_its_metadata(self):
        """The reported symptom: no titles, artists or runtimes in the queue."""
        got = self.source.get_items_by_ids(_e2e.SOURCE_UUID, self.ids[:BIG])
        self.assertEqual(
            len(got), BIG,
            "asked for %d rows and got %d — a failed batch leaves its rows "
            "without metadata" % (BIG, len(got)))
        nameless = [i for i, item in enumerate(got) if not item.get("Name")]
        self.assertEqual(nameless, [],
                         "%d rows came back without a Name" % len(nameless))
        # The queue table's columns, which are what the reporter could not
        # see. Runtime is the one that needs a real DTO rather than the id.
        runtimeless = [i for i, item in enumerate(got)
                       if not item.get("RunTimeTicks")]
        self.assertEqual(
            runtimeless, [],
            "%d rows have no RunTimeTicks, so the queue would show a blank "
            "duration column" % len(runtimeless))

    def test_order_is_the_order_asked_for(self):
        """`Ids=` does not preserve order server-side, so the queue would be
        shuffled against the playlist it came from."""
        want = self.ids[:BIG]
        got = [item.get("Id") for item
               in self.source.get_items_by_ids(_e2e.SOURCE_UUID, want)]
        self.assertEqual(got, want)

    def test_the_chunk_boundary(self):
        """One under, exactly on, one over. Off-by-one here drops a row or
        sends an empty final batch."""
        for size in (self.CHUNK - 1, self.CHUNK, self.CHUNK + 1,
                     self.CHUNK * 2, self.CHUNK * 2 + 1):
            with self.subTest(size=size):
                want = self.ids[:size]
                got = self.source.get_items_by_ids(_e2e.SOURCE_UUID, want)
                self.assertEqual(
                    [i.get("Id") for i in got], want,
                    "%d ids did not round-trip intact" % size)

    def test_a_repeated_id_gets_a_row_each_time(self):
        """A queue can hold the same track twice, and both rows have to draw.

        The batch is de-duplicated before it is sent — asking for the same
        GUID twice in one `Ids=` wastes the very budget this is rationing —
        so the mapping back has to expand it again.
        """
        want = [self.ids[0], self.ids[1], self.ids[0], self.ids[0]]
        got = self.source.get_items_by_ids(_e2e.SOURCE_UUID, want)
        self.assertEqual([i.get("Id") for i in got], want)

    def test_an_unknown_id_is_dropped_rather_than_faked(self):
        """A stale playlist entry must not become a blank row with no id."""
        want = [self.ids[0], "0" * 32, self.ids[1]]
        got = self.source.get_items_by_ids(_e2e.SOURCE_UUID, want)
        self.assertEqual([i.get("Id") for i in got],
                         [self.ids[0], self.ids[1]])


if __name__ == "__main__":
    unittest.main()
