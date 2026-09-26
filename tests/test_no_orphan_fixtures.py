"""A downloads-row fixture must say which server its rows belong to.

Runs `tools/audit_row_fixtures.py`. The reasoning is there; the short version
is that a row with no `content_server_id` is an **orphan**, orphans are a
separate contract (they never sync, in either direction), and a fixture that
omits the column moves every filing assertion built on it onto that contract
while the test name still claims otherwise.

This is not covered by `tests/test_no_fake_gaps.py`: that audit asks which
field a fake *omitted*, and the fixture that motivated this one omits nothing
-- it builds `{c: None for c in COLUMNS}` and populates six of them.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_row_fixtures import audit, _column_names_used  # noqa: E402


class RowFixturesNameTheirServerTest(unittest.TestCase):
    def test_every_downloads_row_fixture_is_homed(self):
        offenders, checked = audit(
            os.path.join(os.path.dirname(os.path.abspath(__file__))))
        self.assertTrue(
            checked, "the audit found no file writing a downloads row, which "
                     "means it stopped matching rather than that the tree is "
                     "clean")
        self.assertEqual(
            [], offenders,
            "these fixtures build downloads rows with no content server, so "
            "every row they write is an orphan and every filing assertion on "
            "them tests the orphan path. Home them, or add the file to "
            "ACCEPTED in tools/audit_row_fixtures.py with the reason:\n  "
            + "\n  ".join("%s: %s" % pair for pair in offenders))


if __name__ == "__main__":
    unittest.main()


class OnlySettingTheColumnSatisfiesTheAuditTest(unittest.TestCase):
    """The audit's own rule, which nothing in the tree happens to exercise.

    It asks whether a module that writes downloads rows *sets*
    `content_server_id`. Every subscript used to count, so a file that built
    ten orphan rows and then read the column once -- in an assertion, or as
    `db.list(server_id=row["content_server_id"])` -- satisfied it. A read is
    exactly what turns up in a file about this column, so the hole was
    widest on the files the audit was written for.
    """

    def _names(self, source):
        import ast

        # Module scope, not the class body: a function imported into a class
        # body becomes a method, so the audit's own helper would be called
        # with `self` as its tree.
        return _column_names_used(ast.parse(source))

    def test_reading_the_column_does_not_count(self):
        self.assertNotIn("content_server_id", self._names(
            'assert row["content_server_id"] is None\n'))

    def test_passing_it_along_does_not_count_either(self):
        self.assertNotIn("content_server_id", self._names(
            'ids = db.list(server_id=row["content_server_id"])\n'))

    def test_a_dict_literal_counts(self):
        self.assertIn("content_server_id", self._names(
            'row = {"item_id": "x", "content_server_id": SERVER}\n'))

    def test_assigning_the_subscript_counts(self):
        self.assertIn("content_server_id", self._names(
            'row["content_server_id"] = SERVER\n'))

    def test_a_keyword_argument_counts(self):
        """`add_row(..., content_server_id=...)` ends in `row.update(kw)`,
        which is why keywords are in here at all."""
        self.assertIn("content_server_id", self._names(
            'add_row(m, "x", content_server_id=SERVER)\n'))

    def test_a_mention_in_a_comment_or_docstring_does_not_count(self):
        self.assertNotIn("content_server_id", self._names(
            '"""content_server_id is set elsewhere."""\n'
            '# content_server_id\n'))
