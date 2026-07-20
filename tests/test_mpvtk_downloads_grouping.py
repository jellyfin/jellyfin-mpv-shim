"""The downloads manager's display tree.

This shaping used to live inside ui.py's list_downloads, wrapped around live
sync-db calls, so none of it could be tested without a syncManager — and
several of its rules are ones the project has already been bitten by:
ownership rows outliving their playlist (downloads that were invisible *and*
undeletable), music playlists unfolding hundreds of tracks, and sizes read
from a key that does not exist.
"""

import sys
import unittest

sys.argv = ["test"]      # the app parses argv on first config-dir resolution

from jellyfin_mpv_shim.mpvtk_browser.downloads import (  # noqa: E402
    group_downloads, progress_summary, row_size, season_title)


def row(item_id, name="Item", **kw):
    r = {"item_id": item_id, "name": name}
    r.update(kw)
    return r


def kinds(tree):
    return [g["kind"] for g in tree]


def by_id(tree, gid):
    return next(g for g in tree if g["id"] == gid)


class TestRowSize(unittest.TestCase):
    def test_disk_bytes_win_over_the_expected_size(self):
        self.assertEqual(row_size({"downloaded_bytes": 5, "size_bytes": 9}), 5)

    def test_expected_size_is_the_fallback_before_probing(self):
        self.assertEqual(row_size({"size_bytes": 9}), 9)

    def test_a_row_with_neither_is_zero_not_a_crash(self):
        self.assertEqual(row_size({}), 0)


class TestGrouping(unittest.TestCase):
    def test_loose_items_land_in_one_movies_group(self):
        tree = group_downloads(
            [row("a", "Zeta", size_bytes=2), row("b", "Alpha", size_bytes=3)],
            [], lambda pid: [], {})
        self.assertEqual(kinds(tree), ["movies"])
        self.assertEqual([c["title"] for c in tree[0]["children"]],
                         ["Alpha", "Zeta"], "not sorted by title")
        self.assertEqual(tree[0]["size"], 5)
        self.assertEqual(tree[0]["count"], 2)

    def test_episodes_nest_under_series_and_season(self):
        rows = [
            row("e2", "Ep2", series_id="s1", series_name="Show",
                season_id="sea1", parent_index=1, index_number=2),
            row("e1", "Ep1", series_id="s1", series_name="Show",
                season_id="sea1", parent_index=1, index_number=1),
        ]
        tree = group_downloads(rows, [], lambda pid: [], {})
        self.assertEqual(kinds(tree), ["series"])
        show = tree[0]
        self.assertEqual(show["count"], 2)
        season = show["children"][0]
        self.assertEqual(season["title"], "Season 1")
        self.assertEqual([e["title"] for e in season["children"]],
                         ["Ep1", "Ep2"], "episodes not in index order")

    def test_an_episode_with_no_index_sorts_last(self):
        rows = [
            row("x", "Bonus", series_id="s1", series_name="S", season_id="a",
                parent_index=1),
            row("y", "Ep1", series_id="s1", series_name="S", season_id="a",
                parent_index=1, index_number=1),
        ]
        tree = group_downloads(rows, [], lambda pid: [], {})
        season = tree[0]["children"][0]
        self.assertEqual([e["title"] for e in season["children"]],
                         ["Ep1", "Bonus"])

    def test_playlists_come_first(self):
        pls = [{"playlist_id": "p1", "name": "Mix"}]
        tree = group_downloads([row("m", "Movie")], pls,
                               lambda pid: [row("t", "Track", type="Audio")],
                               {})
        self.assertEqual(kinds(tree), ["playlist", "movies"])

    def test_a_music_playlist_stays_collapsed(self):
        """Hundreds of tracks nobody wants enumerated."""
        pls = [{"playlist_id": "p1", "name": "Mix"}]
        items = [row("t%d" % i, "T%d" % i, type="Audio") for i in range(300)]
        tree = group_downloads([], pls, lambda pid: items, {})
        self.assertEqual(tree[0]["children"], [])
        self.assertEqual(tree[0]["count"], 300, "the count is still shown")

    def test_a_video_playlist_expands(self):
        pls = [{"playlist_id": "p1", "name": "Films"}]
        items = [row("v1", "A", type="Movie"), row("v2", "B", type="Episode")]
        tree = group_downloads([], pls, lambda pid: items, {})
        self.assertEqual([c["title"] for c in tree[0]["children"]], ["A", "B"])

    def test_one_song_in_a_video_playlist_keeps_it_collapsed(self):
        """Whitelist, not an audio blacklist — a mixed playlist must not
        unfold, and neither must one with an unrecognized type."""
        pls = [{"playlist_id": "p1", "name": "Mixed"}]
        for bad in ({"type": "Audio"}, {"type": None}, {}):
            with self.subTest(bad=bad):
                items = [row("v1", "A", type="Movie"), row("v2", "B", **bad)]
                tree = group_downloads([], pls, lambda pid: items, {})
                self.assertEqual(tree[0]["children"], [])

    def test_a_playlists_items_are_not_also_listed_below(self):
        rows = [row("v1", "A", type="Movie")]
        pls = [{"playlist_id": "p1", "name": "Films"}]
        tree = group_downloads(rows, pls, lambda pid: rows, {"v1": "p1"})
        self.assertEqual(kinds(tree), ["playlist"],
                         "the item was counted twice")

    def test_an_orphaned_ownership_row_still_shows_its_item(self):
        """An ownership row can outlive its playlist. Skipping those rows
        unconditionally made the download invisible AND undeletable — disk
        used with no way to reclaim it."""
        rows = [row("v1", "A", type="Movie")]
        tree = group_downloads(rows, [], lambda pid: [], {"v1": "gone"})
        self.assertEqual(kinds(tree), ["movies"])
        self.assertEqual(tree[0]["children"][0]["id"], "v1")

    def test_an_empty_catalog_is_an_empty_tree(self):
        self.assertEqual(group_downloads([], [], lambda pid: [], {}), [])


class TestSeasonTitle(unittest.TestCase):
    def test_the_stored_name_wins(self):
        self.assertEqual(
            season_title({"item_json": '{"SeasonName": "Book One"}',
                          "parent_index": 3}), "Book One")

    def test_season_zero_is_specials(self):
        self.assertEqual(season_title({"parent_index": 0}), "Specials")

    def test_no_index_is_episodes(self):
        self.assertEqual(season_title({}), "Episodes")

    def test_unparsable_json_falls_back_rather_than_raising(self):
        self.assertEqual(season_title({"item_json": "{{{", "parent_index": 2}),
                         "Season 2")


class TestProgressSummary(unittest.TestCase):
    def test_nothing_pending_is_none(self):
        self.assertIsNone(progress_summary([]))

    def test_the_row_with_bytes_on_disk_is_the_active_one(self):
        rows = [row("a", "Queued"),
                row("b", "Downloading", downloaded_bytes=50, size_bytes=200)]
        self.assertEqual(progress_summary(rows),
                         {"pending": 2, "name": "Downloading", "percent": 25})

    def test_an_unprobed_size_gives_no_percentage_rather_than_zero(self):
        got = progress_summary([row("a", "Queued", downloaded_bytes=10)])
        self.assertIsNone(got["percent"])

    def test_it_falls_back_to_the_first_row_when_none_have_started(self):
        got = progress_summary([row("a", "First"), row("b", "Second")])
        self.assertEqual(got["name"], "First")


if __name__ == "__main__":
    unittest.main()
