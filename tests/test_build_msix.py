"""The MSIX manifest is what Partner Center and makeappx both validate, and
CI only finds a bad one at the end of a 20-minute Windows build."""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import unittest
import xml.etree.ElementTree as ET

from tools import build_msix

NS = "{http://schemas.microsoft.com/appx/manifest/foundation/windows10}"


class Version(unittest.TestCase):
    def test_a_release_gets_a_zero_revision(self):
        self.assertEqual(build_msix.msix_version("3.2.0"), "3.2.0.0")

    def test_a_pre_release_keeps_only_its_numbers(self):
        self.assertEqual(build_msix.msix_version("3.3.0pre1"), "3.3.0.0")
        self.assertEqual(build_msix.msix_version("3.3.0rc2"), "3.3.0.0")

    def test_the_shipped_version_is_usable(self):
        build_msix.msix_version(build_msix.client_version())


class Manifest(unittest.TestCase):
    def _identity(self, **kw):
        root = ET.fromstring(build_msix.render_manifest("3.2.0", "arm64", **kw))
        return root.find(NS + "Identity").attrib

    def test_identity_carries_version_and_architecture(self):
        ident = self._identity(name="Real.Name", publisher="CN=ABC")
        self.assertEqual(ident, {"Name": "Real.Name", "Publisher": "CN=ABC",
                                 "Version": "3.2.0.0",
                                 "ProcessorArchitecture": "arm64"})

    def test_an_unset_repo_variable_falls_back_to_the_placeholder(self):
        """CI passes ${{ vars.X }}, which is an empty string when unset."""
        ident = self._identity(name="", publisher="")
        self.assertEqual(ident["Name"], build_msix.PLACEHOLDER_NAME)
        self.assertEqual(ident["Publisher"], build_msix.PLACEHOLDER_PUBLISHER)

    def test_a_publisher_with_quotes_still_parses(self):
        """Partner Center publishers are distinguished names, which can
        carry quoted RDNs."""
        ident = self._identity(publisher='CN="Walton, Izzie", O=X')
        self.assertEqual(ident["Publisher"], 'CN="Walton, Izzie", O=X')


if __name__ == "__main__":
    unittest.main()
