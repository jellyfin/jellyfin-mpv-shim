import os
import sys
import unittest
from unittest.mock import patch

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jellyfin_mpv_shim.utils import configure_macos_vulkan


class MacOSVulkanTest(unittest.TestCase):
    def setUp(self):
        self._orig_vk = os.environ.get("VK_DRIVER_FILES")
        self._orig_icd = os.environ.get("VK_ICD_FILENAMES")
        if "VK_DRIVER_FILES" in os.environ:
            del os.environ["VK_DRIVER_FILES"]
        if "VK_ICD_FILENAMES" in os.environ:
            del os.environ["VK_ICD_FILENAMES"]

    def tearDown(self):
        if self._orig_vk is not None:
            os.environ["VK_DRIVER_FILES"] = self._orig_vk
        elif "VK_DRIVER_FILES" in os.environ:
            del os.environ["VK_DRIVER_FILES"]

        if self._orig_icd is not None:
            os.environ["VK_ICD_FILENAMES"] = self._orig_icd
        elif "VK_ICD_FILENAMES" in os.environ:
            del os.environ["VK_ICD_FILENAMES"]

    def test_non_darwin_does_nothing(self):
        with patch("sys.platform", "linux"):
            configure_macos_vulkan()
            self.assertNotIn("VK_DRIVER_FILES", os.environ)

    def test_already_set_does_not_overwrite(self):
        os.environ["VK_DRIVER_FILES"] = "/custom/path.json"
        with patch("sys.platform", "darwin"):
            configure_macos_vulkan()
            self.assertEqual(os.environ["VK_DRIVER_FILES"], "/custom/path.json")

    def test_discovers_homebrew_or_candidate(self):
        with patch("sys.platform", "darwin"), \
             patch("os.path.isfile", side_effect=lambda p: p == "/opt/homebrew/etc/vulkan/icd.d/MoltenVK_icd.json"):
            configure_macos_vulkan()
            self.assertEqual(os.environ.get("VK_DRIVER_FILES"), "/opt/homebrew/etc/vulkan/icd.d/MoltenVK_icd.json")

    def test_discovers_frozen_bundled_icd(self):
        with patch("sys.platform", "darwin"), \
             patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", "/App/Contents/MacOS/Jellyfin MPV Shim"), \
             patch("os.path.isfile", side_effect=lambda p: p == "/App/Contents/Resources/vulkan/icd.d/MoltenVK_icd.json"):
            configure_macos_vulkan()
            self.assertEqual(os.environ.get("VK_DRIVER_FILES"), "/App/Contents/Resources/vulkan/icd.d/MoltenVK_icd.json")


if __name__ == "__main__":
    unittest.main()
