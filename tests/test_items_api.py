"""`items_api` — the query that goes to `/Items` instead of the old route.

`GET /Users/{userId}/Items` is `[Obsolete]` upstream and already lossy: its
body is `=> await GetItems(...)` with a literal `[]` for the language
arrays, so of 88 parameters it silently drops three. A filter can be
perfectly well formed, perfectly supported, and do nothing.

Which makes the failure mode of *this* module the same shape as the bug it
fixes: a keyword we forget to map is not an error, it is a filter that
stops being applied, and a library that matches everything looks exactly
like a library with no filter on it. So the mapping is checked against the
apiclient's own signature rather than against a copy of itself, and an
unknown keyword raises instead of being dropped.
"""

import inspect
import sys
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_apiclient_python.api import API                    # noqa: E402

from jellyfin_mpv_shim import items_api                          # noqa: E402


#: Arguments `get_user_items` takes that are not per-field query keys.
NOT_QUERY_KEYS = {"self", "ids", "params"}


class MappingCoverageTest(unittest.TestCase):
    def test_every_apiclient_keyword_is_mapped(self):
        """The drift guard.

        If the apiclient grows an argument and this module does not, a
        caller passing it gets a TypeError -- loud, and at the call. If it
        were merely ignored, the query would go out without the filter.
        """
        theirs = set(inspect.signature(API.get_user_items).parameters)
        theirs -= NOT_QUERY_KEYS
        missing = theirs - set(items_api._QUERY_KEYS)
        self.assertEqual(
            missing, set(),
            "get_user_items takes these and items_api does not map them: %s"
            % sorted(missing))

    def test_nothing_is_mapped_that_they_do_not_have(self):
        # The other direction is not a silent failure, but it does mean the
        # two have drifted and one of them is wrong about the API.
        theirs = set(inspect.signature(API.get_user_items).parameters)
        extra = set(items_api._QUERY_KEYS) - theirs
        self.assertEqual(extra, set(), "items_api maps keywords the "
                         "apiclient does not have: %s" % sorted(extra))

    def test_the_query_keys_match_theirs_exactly(self):
        """Names, not just coverage: `parent_id` must become `ParentId`.

        Read out of the apiclient's own source, because a hand-written copy
        of a mapping is exactly the artefact that drifts.
        """
        src = inspect.getsource(API.get_user_items)
        for kwarg, key in items_api._QUERY_KEYS.items():
            with self.subTest(kwarg):
                self.assertIn("'%s': %s," % (key, kwarg), src)


class BuildQueryTest(unittest.TestCase):
    def test_none_means_not_sent(self):
        q = items_api.build_query(parent_id=None, limit=10)
        self.assertNotIn("ParentId", q)
        self.assertEqual(q["Limit"], 10)

    def test_the_user_rides_as_a_query_parameter(self):
        # The whole point of the move: the user is no longer a path
        # segment. `{UserId}` is substituted by the http layer, in params
        # as well as in the URL, so no caller holds a user id.
        self.assertEqual(items_api.build_query()["UserId"], "{UserId}")

    def test_ids_are_joined(self):
        self.assertEqual(items_api.build_query(ids=["a", "b"])["Ids"], "a,b")

    def test_params_win(self):
        q = items_api.build_query(limit=10, params={"Limit": 5, "X": "y"})
        self.assertEqual((q["Limit"], q["X"]), (5, "y"))

    def test_an_unknown_keyword_raises(self):
        # Never silently dropped -- see the module docstring.
        with self.assertRaises(TypeError) as caught:
            items_api.build_query(audio_languages="eng")
        self.assertIn("audio_languages", str(caught.exception))

    def test_a_false_value_is_still_sent(self):
        # `is_favorite=False` and `recursive=False` are real filters; only
        # None means "leave it out". Getting this wrong turns "not
        # favourited" into "everything".
        q = items_api.build_query(is_favorite=False, recursive=False)
        self.assertIs(q["IsFavorite"], False)
        self.assertIs(q["Recursive"], False)


class CallSiteTest(unittest.TestCase):
    def test_nothing_calls_the_legacy_endpoint_any_more(self):
        """The migration, asserted rather than remembered."""
        import pathlib

        pkg = pathlib.Path(items_api.__file__).parent
        bad = []
        for path in sorted(pkg.rglob("*.py")):
            if path.name == "items_api.py":
                continue          # names it in prose, on purpose
            text = path.read_text(encoding="utf-8")
            for n, line in enumerate(text.splitlines(), 1):
                if "get_user_items" in line or ".user_items(" in line:
                    bad.append("%s:%d" % (path.relative_to(pkg), n))
        self.assertEqual(bad, [], "still on GET /Users/{id}/Items, which "
                         "drops audioLanguages, subtitleLanguages and "
                         "indexNumber: %s" % bad)


if __name__ == "__main__":
    unittest.main()
