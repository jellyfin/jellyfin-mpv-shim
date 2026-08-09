"""Item queries go to `/Items`, and it matters which one.

`GET /Users/{userId}/Items` is `[Obsolete("Kept for backwards
compatibility")]`; its body is `=> await GetItems(...)` with a literal `[]`
for the language arrays, so of 88 parameters it drops three -- and does it
silently. Upstream plans to remove the legacy endpoints [iw], which is the
other half of the reason to be off them.

Two things are asserted, and the first is the one that can regress without
anyone noticing:

**A parameter the legacy route drops takes effect.** This is the only test
in the project that can tell the two endpoints apart, because from the
client's side a dropped filter and a library where everything matches look
identical. Measured on the v12 QA server: `AudioLanguages=eng` is 1108 of
1131 through `/Items` and 1131 through the legacy route.

It is skipped where the server has no such parameter -- `audioLanguages`
landed on master 2026-05-10 and is in no 10.11 release -- and the skip is
decided by **asking the server**, via `/Items/Filters2`, which is the same
gate jellyfin-web uses to decide whether to draw the control. So this test
needs no version knowledge and neither will the filter UI.

**Everything else is unchanged.** The legacy action delegates to the very
handler `/Items` is, so a migrated query must return the same items. Pinned
across several shapes because "it still works" is easy to believe from one.
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

from jellyfin_mpv_shim import items_api  # noqa: E402


@_e2e.require_server
class ItemsEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = _e2e.Session()
        cls.source = cls.session.library_source()
        cls.api = cls.source._conn(_e2e.SOURCE_UUID).api
        views = cls.api.get_views()["Items"]
        cls.movies = next(v["Id"] for v in views if v["Name"] == "Movies")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.source.stop()
        finally:
            cls.session.stop()

    def _filters2(self):
        return self.api.items("/Filters2", params={
            "userId": "{UserId}", "parentId": self.movies,
            "includeItemTypes": "Movie"}) or {}

    def test_a_parameter_the_legacy_route_drops_takes_effect(self):
        offered = [o.get("Value") for o in self._filters2().get(
            "AudioLanguages") or []]
        if not offered:
            self.skipTest("this server has no audio-language filter "
                          "(pre-12.0); nothing to tell apart")
        base = dict(recursive=True, include_item_types="Movie", limit=0,
                    parent_id=self.movies)
        total = items_api.get_items(self.api, **base)["TotalRecordCount"]
        real = items_api.get_items(
            self.api, params={"AudioLanguages": offered[0]},
            **base)["TotalRecordCount"]
        nonsense = items_api.get_items(
            self.api, params={"AudioLanguages": "zzz"},
            **base)["TotalRecordCount"]

        self.assertEqual(nonsense, 0,
                         "a nonsense language matched %d of %d items, so "
                         "the parameter is being dropped -- this query is "
                         "on the legacy endpoint again" % (nonsense, total))
        self.assertGreater(real, 0, "the server offered %r as an audio "
                           "language and then matched nothing with it"
                           % offered[0])
        self.assertLessEqual(real, total)

    def test_a_migrated_query_returns_what_the_old_one_did(self):
        cases = {
            "grid": dict(parent_id=self.movies, recursive=True,
                         include_item_types="Movie", limit=20,
                         fields="PrimaryImageAspectRatio,UserData",
                         sort_by="SortName", sort_order="Ascending"),
            "paged": dict(parent_id=self.movies, recursive=True,
                          include_item_types="Movie", start_index=17,
                          limit=5, sort_by="SortName"),
            "search": dict(search_term="the", recursive=True, limit=10,
                           include_item_types="Movie,Series"),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                old = self.api.get_user_items(**kw)
                new = items_api.get_items(self.api, **kw)
                self.assertEqual(old["TotalRecordCount"],
                                 new["TotalRecordCount"])
                self.assertEqual([i["Id"] for i in old["Items"]],
                                 [i["Id"] for i in new["Items"]])
                # The DTO shape too: a `Fields` that stopped being honoured
                # would leave the browser drawing placeholders.
                if old["Items"]:
                    self.assertEqual(sorted(old["Items"][0]),
                                     sorted(new["Items"][0]))

    def test_the_user_context_survives_the_move(self):
        """UserData is the thing a path-segment user id was buying.

        It now rides as a query parameter, substituted by the http layer.
        Get that wrong and every item comes back with no played state --
        which looks like a fresh account, not like a bug.
        """
        res = items_api.get_items(
            self.api, parent_id=self.movies, recursive=True,
            include_item_types="Movie", limit=5, fields="UserData")
        items = res.get("Items") or []
        self.assertTrue(items, "no movies to check")
        for item in items:
            self.assertIn("UserData", item,
                          "the query lost its user context")


if __name__ == "__main__":
    unittest.main()
