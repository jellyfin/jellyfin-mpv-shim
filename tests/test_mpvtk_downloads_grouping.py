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

import json  # noqa: E402

from jellyfin_mpv_shim.mpvtk_browser.downloads import (  # noqa: E402
    audiobook_group, group_downloads, progress_summary, qualified_title,
    row_size, season_title, status_text)
from jellyfin_mpv_shim.sync.db import (  # noqa: E402
    ORIGIN_AUTO_NEXT_UP, ORIGIN_AUTO_LOOKAHEAD, ORIGIN_USER)


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


class TestWatchedRollup(unittest.TestCase):
    """The catalog stores the server's UserData blob verbatim and nothing was
    reading Played out of it, so the panel could neither mark a watched item
    nor tell whether "Remove Watched" would delete anything."""

    @staticmethod
    def _row(item_id, played, **kw):
        import json as _json
        return row(item_id, userdata_json=_json.dumps({"Played": played}),
                   **kw)

    def test_an_item_carries_its_watched_flag(self):
        tree = group_downloads([self._row("m1", True)], [],
                               lambda pid: [], {})
        self.assertTrue(tree[0]["children"][0]["watched"])

    def test_unparsable_userdata_is_unwatched_rather_than_a_crash(self):
        tree = group_downloads([row("m1", userdata_json="{{{")], [],
                               lambda pid: [], {})
        self.assertFalse(tree[0]["children"][0]["watched"])

    def test_a_series_counts_its_watched_episodes(self):
        rows = [self._row("e1", True, series_id="s1", series_name="S",
                          season_id="a", parent_index=1, index_number=1),
                self._row("e2", False, series_id="s1", series_name="S",
                          season_id="a", parent_index=1, index_number=2)]
        tree = group_downloads(rows, [], lambda pid: [], {})
        self.assertEqual(tree[0]["watched_count"], 1)
        self.assertEqual(tree[0]["children"][0]["watched_count"], 1)

    def test_a_group_with_nothing_watched_reports_zero(self):
        tree = group_downloads([self._row("m1", False)], [],
                               lambda pid: [], {})
        self.assertEqual(tree[0]["watched_count"], 0)

    def test_every_group_carries_the_key(self):
        """The view gates a button on it, so it must never be missing."""
        rows = [self._row("e1", True, series_id="s1", series_name="S",
                          season_id="a", parent_index=1),
                self._row("m1", False)]
        pls = [{"playlist_id": "p1", "name": "Mix"}]
        tree = group_downloads(rows, pls,
                               lambda pid: [self._row("t1", True,
                                                      type="Movie")], {})
        for g in tree:
            self.assertIn("watched_count", g, g["kind"])


class TestStatusText(unittest.TestCase):
    """Raw catalog values were rendered verbatim and untranslated."""

    def test_a_download_in_flight_reports_a_percentage(self):
        self.assertEqual(
            status_text({"status": "downloading", "done": 42, "total": 100}),
            "Downloading 42%")

    def test_an_unprobed_size_drops_the_percentage_rather_than_showing_zero(self):
        self.assertEqual(
            status_text({"status": "downloading", "done": 10, "total": 0}),
            "Downloading")

    def test_queued_and_failed_are_words(self):
        self.assertEqual(status_text({"status": "pending"}), "Queued")
        self.assertEqual(status_text({"status": "error"}), "Failed")

    def test_complete_says_nothing_because_the_size_already_does(self):
        self.assertEqual(status_text({"status": "complete"}), "")

    def test_an_unknown_status_falls_through_rather_than_vanishing(self):
        self.assertEqual(status_text({"status": "weird"}), "weird")

    def test_the_entry_carries_the_raw_byte_pair(self):
        """`size` is whichever of the two is meaningful; the view needs both
        to compute a percentage."""
        tree = group_downloads(
            [row("m1", "M", downloaded_bytes=5, size_bytes=9)], [],
            lambda pid: [], {})
        entry = tree[0]["children"][0]
        self.assertEqual((entry["done"], entry["total"]), (5, 9))


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


class TestTheShapeTheViewExpects(unittest.TestCase):
    """The settings panel reads specific keys off this tree, and its own
    fixtures are hand-built — so a key added here can go missing there and
    quietly change what renders. Pin the contract in one place."""

    GROUP_KEYS = {"kind", "id", "title", "size", "count", "watched_count",
                  "children"}
    ITEM_KEYS = {"kind", "id", "title", "status", "size", "index", "done",
                 "total", "watched"}

    def _tree(self):
        import json as _json
        rows = [row("e1", "Ep1", series_id="s1", series_name="S",
                    season_id="a", parent_index=1, index_number=1,
                    userdata_json=_json.dumps({"Played": True})),
                row("m1", "A Movie")]
        pls = [{"playlist_id": "p1", "name": "Mix"}]
        return group_downloads(rows, pls,
                               lambda pid: [row("t1", "T", type="Movie")], {})

    def test_every_group_carries_the_keys_the_view_reads(self):
        for g in self._tree():
            with self.subTest(kind=g["kind"]):
                self.assertLessEqual(self.GROUP_KEYS, set(g))

    def test_every_item_carries_the_keys_the_view_reads(self):
        def items(node):
            for c in node.get("children") or []:
                if c.get("kind") == "item":
                    yield c
                else:
                    yield from items(c)

        seen = 0
        for g in self._tree():
            for it in items(g):
                seen += 1
                self.assertLessEqual(self.ITEM_KEYS, set(it))
        self.assertTrue(seen, "the fixture produced no item rows")


if __name__ == "__main__":
    unittest.main()


class AutoSubtreeTest(unittest.TestCase):
    """Automatic downloads get their own groups, ahead of the hand-picked
    ones: they are what changes without the user doing anything."""

    def _tree(self, rows):
        return group_downloads(rows, [], lambda pid: [], {})

    def test_each_source_is_its_own_group(self):
        tree = self._tree([
            row("a", origin=ORIGIN_AUTO_NEXT_UP, type="Episode"),
            row("b", origin=ORIGIN_AUTO_LOOKAHEAD, type="Episode"),
        ])
        self.assertEqual([g["title"] for g in tree],
                         ["Automatic: Next Up", "Automatic: Actively Watched"])

    def test_an_empty_source_shows_no_group(self):
        tree = self._tree([row("a", origin=ORIGIN_AUTO_NEXT_UP,
                               type="Episode")])
        self.assertEqual(len(tree), 1)

    def test_auto_rows_are_not_also_listed_under_their_series(self):
        """Otherwise the same episode appears twice and its size is counted
        twice in the totals."""
        tree = self._tree([
            row("a", origin=ORIGIN_AUTO_NEXT_UP, type="Episode",
                series_id="s1", series_name="Show"),
        ])
        self.assertEqual([g["kind"] for g in tree], ["auto"])

    def test_user_rows_still_group_by_series(self):
        tree = self._tree([
            row("u", origin=ORIGIN_USER, type="Episode",
                series_id="s1", series_name="Show"),
        ])
        self.assertEqual([g["kind"] for g in tree], ["series"])

    def test_the_group_has_no_server_side_id(self):
        """The renderer deletes an id-less group by listing its rows, which
        is right: no server object means "what auto-download fetched"."""
        tree = self._tree([row("a", origin=ORIGIN_AUTO_NEXT_UP,
                               type="Episode")])
        self.assertIsNone(tree[0]["id"])

    def test_an_unknown_auto_source_still_appears(self):
        """A catalog from an early build. It has to be reachable or it is
        disk used with no way to reclaim it from this screen."""
        tree = self._tree([row("a", origin="auto", type="Episode")])
        self.assertEqual([g["kind"] for g in tree], ["auto"])


class QualifiedTitleTest(unittest.TestCase):
    """The automatic groups are flat and mix shows, so a bare episode name
    does not say what it belongs to."""

    def test_series_and_numbering_are_included(self):
        self.assertEqual(
            qualified_title(row("a", name="Chapter Four", type="Episode",
                                series_name="Show", parent_index=1,
                                index_number=4)),
            "Show - S01E04 - Chapter Four")

    def test_a_movie_is_left_alone(self):
        self.assertEqual(
            qualified_title(row("m", name="Arrival", type="Movie")), "Arrival")

    def test_missing_numbering_is_dropped_not_rendered(self):
        """"S01ENone" is worse than no numbering at all."""
        out = qualified_title(row("a", name="Special", type="Episode",
                                  series_name="Show"))
        self.assertEqual(out, "Show - Special")

    def test_an_episode_with_no_series_still_reads(self):
        self.assertEqual(
            qualified_title(row("a", name="Pilot", type="Episode")), "Pilot")

    def test_the_auto_group_uses_it(self):
        tree = group_downloads(
            [row("a", name="Chapter Four", origin=ORIGIN_AUTO_NEXT_UP,
                 type="Episode", series_name="Show", parent_index=1,
                 index_number=4)], [], lambda pid: [], {})
        self.assertEqual(tree[0]["children"][0]["title"],
                         "Show - S01E04 - Chapter Four")

    def test_series_groups_keep_the_bare_name(self):
        """Under a series/season heading the tree already supplies context;
        repeating it would be noise."""
        tree = group_downloads(
            [row("u", name="Chapter Four", origin=ORIGIN_USER, type="Episode",
                 series_id="s1", series_name="Show", parent_index=1,
                 index_number=4)], [], lambda pid: [], {})
        self.assertEqual(
            tree[0]["children"][0]["children"][0]["title"], "Chapter Four")


class EntryNumberingTest(unittest.TestCase):
    """The view numbers item rows ("4. Chapter Four"). That reads correctly
    under a season heading and not at all in a flat group whose titles
    already spell out S01E04 -- and across mixed shows, a bare "4." means
    nothing at all."""

    def _entry_for(self, origin):
        tree = group_downloads(
            [row("a", name="Chapter Four", origin=origin, type="Episode",
                 series_id="s1", series_name="Show", parent_index=1,
                 index_number=4)], [], lambda pid: [], {})
        group = tree[0]
        # series groups nest a season; auto groups are flat
        child = group["children"][0]
        return child["children"][0] if child.get("kind") == "season" else child

    def test_auto_entries_opt_out_of_numbering(self):
        self.assertTrue(self._entry_for(ORIGIN_AUTO_NEXT_UP)["qualified"])

    def test_series_entries_keep_numbering(self):
        entry = self._entry_for(ORIGIN_USER)
        self.assertFalse(entry["qualified"])
        self.assertIsNotNone(entry["index"],
                             "the season view still numbers its episodes")


class TestBookSections(unittest.TestCase):
    """Books and audiobooks are their own two sections.

    Both used to fall into the flat "Movies & Videos" bucket, which is a
    problem of kind rather than of tidiness: a books library is hundreds of
    tiny rows, and once they are mixed in there is no way to find the film
    you were looking for, or to reclaim the space either one is using.
    """

    def test_a_book_does_not_land_with_the_films(self):
        tree = group_downloads(
            [row("m", "A Film", type="Movie"),
             row("b", "A Novel", type="Book")],
            [], lambda pid: [], {})
        self.assertEqual(kinds(tree), ["books", "movies"])
        self.assertEqual([c["title"] for c in tree[0]["children"]],
                         ["A Novel"])
        self.assertEqual([c["title"] for c in tree[1]["children"]],
                         ["A Film"])

    def test_audiobook_chapters_nest_under_their_book(self):
        rows = [
            row("c2", "Chapter 02", type="AudioBook", index_number=2,
                size_bytes=2, item_json=json.dumps({"Album": "The Account"})),
            row("c1", "Chapter 01", type="AudioBook", index_number=1,
                size_bytes=3, item_json=json.dumps({"Album": "The Account"})),
        ]
        tree = group_downloads(rows, [], lambda pid: [], {})
        self.assertEqual(kinds(tree), ["audiobooks"])
        section = tree[0]
        self.assertEqual(section["count"], 2)
        self.assertEqual(section["size"], 5)
        self.assertEqual([b["title"] for b in section["children"]],
                         ["The Account"])
        # In listening order, not the order the catalog happened to hold
        # them: a rip is downloaded in whatever order the worker got to it.
        self.assertEqual([e["title"] for e in section["children"][0]["children"]],
                         ["Chapter 01", "Chapter 02"])

    def test_two_books_are_two_subgroups(self):
        rows = [
            row("a", "Chapter 01", type="AudioBook",
                item_json=json.dumps({"Album": "Book A"})),
            row("b", "Chapter 01", type="AudioBook",
                item_json=json.dumps({"Album": "Book B"})),
        ]
        tree = group_downloads(rows, [], lambda pid: [], {})
        self.assertEqual([b["title"] for b in tree[0]["children"]],
                         ["Book A", "Book B"])

    def test_a_single_file_audiobook_is_grouped_under_its_own_name(self):
        # An .m4b is one item and one book. Album is often absent on one,
        # and grouping every untagged single-file audiobook together under
        # a shared "Ungrouped" heading would be worse than useless.
        tree = group_downloads(
            [row("x", "The Lantern Keeper", type="AudioBook")],
            [], lambda pid: [], {})
        self.assertEqual([b["title"] for b in tree[0]["children"]],
                         ["The Lantern Keeper"])

    def test_books_lead_nothing_and_audiobooks_lead_books(self):
        # Audiobooks first: hours of audio against a few hundred kilobytes,
        # so it is the section anyone reclaiming space came for.
        tree = group_downloads(
            [row("b", "A Novel", type="Book"),
             row("a", "Ch 1", type="AudioBook")],
            [], lambda pid: [], {})
        self.assertEqual(kinds(tree), ["audiobooks", "books"])

    def test_an_automatic_book_still_belongs_to_the_scheduler(self):
        # The auto groups are lifted out FIRST, and must stay that way: what
        # the reaper may delete is the question that section answers, and a
        # book filed by type would be invisible to it.
        tree = group_downloads(
            [row("b", "A Novel", type="Book", origin=ORIGIN_AUTO_NEXT_UP)],
            [], lambda pid: [], {})
        self.assertEqual(kinds(tree), ["auto"])

    def test_watched_counts_reach_the_section(self):
        rows = [
            row("a", "Ch 1", type="AudioBook",
                item_json=json.dumps({"Album": "B"}),
                userdata_json=json.dumps({"Played": True})),
            row("b", "Ch 2", type="AudioBook",
                item_json=json.dumps({"Album": "B"})),
        ]
        tree = group_downloads(rows, [], lambda pid: [], {})
        self.assertEqual(tree[0]["watched_count"], 1)
        self.assertEqual(tree[0]["children"][0]["watched_count"], 1)


class TestAudiobookGroup(unittest.TestCase):

    def test_album_is_what_joins_a_rip(self):
        self.assertEqual(
            audiobook_group(row("x", "Ch 1",
                                item_json=json.dumps({"Album": "The Book"}))),
            "The Book")

    def test_an_unreadable_item_blob_falls_back_to_the_name(self):
        self.assertEqual(audiobook_group(row("x", "Ch 1", item_json="{oops")),
                         "Ch 1")

    def test_a_row_with_nothing_at_all_still_gets_a_heading(self):
        # It has to appear somewhere or it is disk used with no way to
        # reclaim it from this screen -- the same rule AUTO_OTHER_TITLE is
        # there for.
        self.assertTrue(audiobook_group({"item_id": "x"}))


if __name__ == "__main__":
    unittest.main()
