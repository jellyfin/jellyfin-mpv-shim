"""Auto-download's two server calls, against a real server.

`tests/test_auto_download.py` is thorough about the *policy* — the cap, the
reaper's ordering, the discard tombstones, what a pass does on the fifth run.
Every one of those tests answers `get_next` and `get_episodes` from a fake
that returns exactly what the test told it to, so all of them would stay green
if this shim's beliefs about those two endpoints were wrong.

The beliefs are load-bearing. The lookahead window is anchored on *the next
episode to watch*, asked of the server, because anchoring it on the furthest
episode already downloaded made the window a ratchet: each pass started where
the last one finished fetching, so an unwatched series came down whole and the
only thing that ever stopped it was the size cap. Anchoring on NextUp is what
makes the window settle — and it rests on:

* **NextUp for a series with nothing watched names the first episode.** If it
  answered empty instead, `_watch_position` would return None and
  `_lookahead` would skip the series *silently and permanently* — so a season
  downloaded before a flight and not yet started would never extend. That is
  a worse failure than the ratchet it replaced, and nothing in the fast suite
  could see it.
* **NextUp advances with what has been watched**, including watching on
  another device, which is the entire reason the anchor is asked of the server
  rather than derived from the catalog.
* **NextUp for a finished series answers empty**, which is what stops the
  lookahead fetching past the end of a show forever.
* **`get_episodes(start_item_id=X)` is inclusive of X** and carries
  `MediaSources`, because the window includes the anchor and the cap is spent
  against real sizes rather than the unknown-size fallback.

Read-only apart from playstate, which every test here restores.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


@_e2e.require_server
class NextUpAnchorTest(unittest.TestCase):
    """The endpoint `AutoDownloader._watch_position` asks."""

    #: Any series with several episodes in one season. Named rather than
    #: discovered so a failure names the fixture it used.
    SERIES = "Absolute Numbering Show"

    @classmethod
    def setUpClass(cls):
        cls.session = _e2e.Session("qa-user")
        cls.series = cls.session.find(cls.SERIES, library="Shows",
                                      item_type="Series")
        cls.episodes = cls.session.episodes(cls.SERIES)
        if len(cls.episodes) < 4:
            cls.session.stop()
            raise unittest.SkipTest(
                "%r has %d episodes; the window tests need at least 4"
                % (cls.SERIES, len(cls.episodes)))

    @classmethod
    def tearDownClass(cls):
        cls.session.stop()

    def setUp(self):
        self.ids = [e["Id"] for e in self.episodes]
        # Playstate is the one thing here that another test can see.
        self.addCleanup(self.session.reset_played, *self.ids)
        self.session.reset_played(*self.ids)

    # -- the anchor --------------------------------------------------------

    def _anchor(self):
        """Exactly what `_watch_position` does."""
        result = self.session.api.get_next(series_id=self.series["Id"],
                                           limit=1) or {}
        items = result.get("Items") or []
        return items[0] if items else None

    def test_an_unstarted_series_anchors_at_the_first_episode(self):
        """The belief the whole fix rests on. Empty here would make the
        lookahead skip a held-but-unstarted series forever, with no log line
        and no way to tell from inside the app."""
        anchor = self._anchor()
        self.assertIsNotNone(
            anchor, "NextUp is empty for a series with nothing watched — "
                    "_lookahead would silently never extend it")
        self.assertEqual(anchor["Id"], self.ids[0])

    def test_the_anchor_advances_with_what_has_been_watched(self):
        """Asked of the server, not derived from the catalog, precisely so
        that watching on another device moves it."""
        self.session.api.item_played(self.ids[0], True)
        self.assertEqual(self._anchor()["Id"], self.ids[1])
        self.session.api.item_played(self.ids[1], True)
        self.assertEqual(self._anchor()["Id"], self.ids[2])

    def test_a_finished_series_has_no_anchor(self):
        """What stops the lookahead asking past the end of a show forever.
        `_lookahead` skips a series with no anchor rather than guessing."""
        for item_id in self.ids:
            self.session.api.item_played(item_id, True)
        self.assertIsNone(self._anchor())

    def test_marking_unwatched_puts_the_anchor_back(self):
        """The fixture cleans up after itself, and the assertion is free."""
        self.session.api.item_played(self.ids[0], True)
        self.session.reset_played(*self.ids)
        self.assertEqual(self._anchor()["Id"], self.ids[0])

    # -- the window --------------------------------------------------------

    def test_the_window_is_inclusive_of_the_anchor(self):
        """`_lookahead` takes `[:count]` from this, so an exclusive
        StartItemId would shift the whole window one episode late and leave
        the episode the user is about to watch undownloaded."""
        from jellyfin_mpv_shim.sync.auto import _FIELDS

        start = self.ids[1]
        result = self.session.api.get_episodes(
            self.series["Id"], start_item_id=start, limit=3,
            fields=_FIELDS) or {}
        items = result.get("Items") or []
        self.assertEqual([i["Id"] for i in items], self.ids[1:4])

    def test_the_window_carries_the_sizes_the_cap_is_spent_against(self):
        """Without MediaSources every candidate is charged _UNKNOWN_SIZE, so
        the storage cap would be enforced against a guess for 100% of them."""
        from jellyfin_mpv_shim.sync.auto import AutoDownloader, _FIELDS, _UNKNOWN_SIZE

        result = self.session.api.get_episodes(
            self.series["Id"], start_item_id=self.ids[0], limit=2,
            fields=_FIELDS) or {}
        items = result.get("Items") or []
        self.assertTrue(items)
        for item in items:
            with self.subTest(item["Name"]):
                self.assertTrue(item.get("MediaSources"),
                                "no MediaSources — see _FIELDS")
                self.assertNotEqual(AutoDownloader._size_of(item),
                                    _UNKNOWN_SIZE,
                                    "size fell back to the unknown-size guess")

    def test_the_planner_walks_a_real_series_the_way_it_claims_to(self):
        """The property end to end, against the server: with the user parked
        on episode 1 and two episodes held, a pass wants exactly the window —
        and wanting the *same* window after the same pass is what makes it
        settle rather than ratchet."""
        from jellyfin_mpv_shim.sync.auto import _FIELDS

        wanted = []
        for _pass in range(3):
            anchor = self._anchor()
            result = self.session.api.get_episodes(
                self.series["Id"], start_item_id=anchor["Id"], limit=2,
                fields=_FIELDS) or {}
            wanted.append([i["Id"] for i in (result.get("Items") or [])])
        self.assertEqual(wanted[0], self.ids[0:2])
        self.assertEqual(wanted[1], wanted[0], "the window moved on its own")
        self.assertEqual(wanted[2], wanted[0], "the window moved on its own")

        # ...and it advances by exactly one when one episode is watched.
        self.session.api.item_played(self.ids[0], True)
        anchor = self._anchor()
        result = self.session.api.get_episodes(
            self.series["Id"], start_item_id=anchor["Id"], limit=2,
            fields=_FIELDS) or {}
        self.assertEqual([i["Id"] for i in (result.get("Items") or [])],
                         self.ids[1:3])


if __name__ == "__main__":
    unittest.main()
