"""tools/collect_licenses.py: what a package ships must carry the licence of what it bundles."""

if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contextlib
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import collect_licenses as CL  # noqa: E402


def dist(site, name, version, license_line="License-Expression: MIT", files=("LICENSE",)):
    d = os.path.join(site, "%s-%s.dist-info" % (name, version))
    os.makedirs(d)
    with open(os.path.join(d, "METADATA"), "w", encoding="utf-8") as fh:
        fh.write("Metadata-Version: 2.4\nName: %s\nVersion: %s\n%s\n" % (name, version, license_line))
    for f in files:
        with open(os.path.join(d, f), "w", encoding="utf-8") as fh:
            fh.write("licence text of %s\n" % name)


class CollectLicensesTest(unittest.TestCase):
    def run_it(self, site, out, *extra):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = CL.main(["collect_licenses.py", site, out] + list(extra))
        return rc, err.getvalue()

    def test_licence_files_are_copied_per_package_and_a_table_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            site, out, table = (os.path.join(tmp, n) for n in ("site", "out", "table.md"))
            os.makedirs(site)
            dist(site, "alpha", "1.0", files=("LICENSE", "NOTICE"))
            dist(site, "beta", "2.0", "License: Apache-2.0")
            rc, err = self.run_it(site, out, "--table", table)
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(sorted(os.listdir(os.path.join(out, "alpha"))), ["LICENSE", "NOTICE"])
            self.assertTrue(os.path.exists(os.path.join(out, "beta", "LICENSE")))
            with open(table, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("| alpha | 1.0 | MIT | LICENSE, NOTICE |", text)
            self.assertIn("| beta | 2.0 | Apache-2.0 | LICENSE |", text)

    def test_a_package_without_a_licence_file_fails_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            site, out = os.path.join(tmp, "site"), os.path.join(tmp, "out")
            os.makedirs(site)
            dist(site, "nolicence", "1.0", files=())
            rc, err = self.run_it(site, out)
            self.assertEqual(rc, 1)
            self.assertIn("nolicence 1.0: no licence file", err)

    def test_a_package_without_licence_metadata_fails_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            site, out = os.path.join(tmp, "site"), os.path.join(tmp, "out")
            os.makedirs(site)
            dist(site, "anon", "1.0", license_line="Summary: x")
            rc, err = self.run_it(site, out)
            self.assertEqual(rc, 1)
            self.assertIn("anon 1.0: no licence metadata", err)

    def test_the_classifier_is_used_when_there_is_no_expression(self):
        with tempfile.TemporaryDirectory() as tmp:
            site, out = os.path.join(tmp, "site"), os.path.join(tmp, "out")
            os.makedirs(site)
            dist(site, "classy", "1.0", "Classifier: License :: OSI Approved :: BSD License")
            self.assertEqual(self.run_it(site, out)[0], 0)

    def test_missing_arguments_print_usage_and_change_nothing(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(CL.main(["collect_licenses.py"]), 64)
        self.assertIn("SITE_PACKAGES", out.getvalue())


if __name__ == "__main__":
    unittest.main()
