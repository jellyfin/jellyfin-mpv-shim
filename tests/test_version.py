"""PEP 440 ordering for the update check.

The bug being pinned here: the checker compared version *strings*, so anyone
on a pre-release or a local build was told the newest stable tag was an
upgrade, every day, forever.
"""

import unittest

from jellyfin_mpv_shim.version import is_newer, parse


class ParseTest(unittest.TestCase):
    def test_leading_v_is_accepted(self):
        self.assertEqual(parse("v2.10.0"), parse("2.10.0"))

    def test_trailing_zeros_do_not_change_the_version(self):
        self.assertEqual(parse("2.10"), parse("2.10.0"))
        self.assertEqual(parse("2.10.0.0"), parse("2.10.0"))

    def test_zero_release_is_not_stripped_away(self):
        self.assertIsNotNone(parse("0"))
        self.assertTrue(is_newer("1", "0"))

    def test_junk_is_unparseable(self):
        for bad in ("", "latest", "main", "2.10.0-", None, "v"):
            self.assertIsNone(parse(bad), bad)

    def test_a_pre_release_without_a_number_is_number_zero(self):
        # PEP 440 normalizes "rc" to "rc0" rather than rejecting it.
        self.assertEqual(parse("2.10.0-rc"), parse("2.10.0rc0"))

    def test_pre_release_spellings_are_equivalent(self):
        # PEP 440 folds c/pre/preview into rc, which is what makes the
        # project's own "v3.0.0pre8" tags orderable.
        for spelling in ("3.0.0rc8", "3.0.0pre8", "3.0.0preview8", "3.0.0c8",
                         "3.0.0-rc.8", "3.0.0_pre_8"):
            self.assertEqual(parse(spelling), parse("3.0.0rc8"), spelling)


class OrderingTest(unittest.TestCase):
    def assertOrdered(self, versions):
        for lower, higher in zip(versions, versions[1:]):
            self.assertTrue(is_newer(higher, lower),
                            "%s should be newer than %s" % (higher, lower))
            self.assertFalse(is_newer(lower, higher),
                             "%s should not be newer than %s" % (lower, higher))

    def test_release_ordering(self):
        self.assertOrdered(["2.9.0", "2.10.0", "2.10.1", "3.0.0"])

    def test_numeric_not_lexicographic(self):
        # Every one of these compares the wrong way round as strings, which is
        # the whole point: segments are integers, not text.
        self.assertTrue(is_newer("2.10.0", "2.9.0"))
        self.assertTrue(is_newer("1.10.1", "1.1.1"))
        self.assertFalse(is_newer("1.1.1", "1.10.1"))
        self.assertTrue(is_newer("1.0.10", "1.0.9"))

    def test_pre_releases_precede_their_own_release(self):
        self.assertOrdered(["3.0.0a1", "3.0.0b1", "3.0.0rc1", "3.0.0rc2",
                            "3.0.0", "3.0.0.post1"])

    def test_dev_precedes_everything_at_that_version(self):
        self.assertOrdered(["3.0.0.dev1", "3.0.0a1", "3.0.0"])

    def test_epoch_outranks_the_release(self):
        self.assertTrue(is_newer("1!1.0", "99.0.0"))

    def test_equal_versions_are_not_upgrades(self):
        self.assertFalse(is_newer("2.10.0", "2.10.0"))
        self.assertFalse(is_newer("v2.10.0", "2.10.0"))
        self.assertFalse(is_newer("2.10", "2.10.0"))

    def test_the_regression(self):
        # Someone testing a pre-release must not be offered the older stable
        # release as an "update" -- the string comparison did exactly that.
        self.assertFalse(is_newer("2.10.0", "3.0.0pre8"))
        self.assertTrue(is_newer("3.0.0", "3.0.0pre8"))

    def test_unparseable_falls_back_to_inequality(self):
        # Better to over-notify than to silently stop notifying if the tag
        # format ever changes shape.
        self.assertTrue(is_newer("weird-tag", "2.10.0"))
        self.assertFalse(is_newer("weird-tag", "weird-tag"))


if __name__ == "__main__":
    unittest.main()
