"""A Windows drive-letter path is a file path, not a URL.

``urlparse(r"D:\\Movies\\x.mkv").scheme`` is ``"d"``, so a direct path the
server reports (or a ``path_substitutions`` target like ``Z:\\media``) went
down the "remote direct path" branch: handed to mpv unchecked, with no
``os.path.isfile`` test and so no fallback to streaming when the drive or
share is not mounted.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import unittest
from types import SimpleNamespace as NS
from unittest import mock

sys.argv = [sys.argv[0]]

SERVER = "https://jellyfin.example.invalid"


def _video(path):
    from jellyfin_mpv_shim.media import Video

    source = {"Id": "src", "Path": path, "Protocol": "File",
              "SupportsDirectPlay": True, "SupportsDirectStream": True}
    cl = NS(config=NS(data={"auth.server": SERVER, "auth.token": "T"}))
    v = Video.__new__(Video)
    v.item_id = "item1"
    v.parent = NS(client=cl, is_local=True)
    v.client = cl
    v.media_source = source
    v.auth_via_header = True
    return v


def _url(path, exists):
    from jellyfin_mpv_shim import media

    v = _video(path)
    with mock.patch.object(media.settings, "direct_paths", True), \
            mock.patch.object(media.settings, "path_substitutions", []), \
            mock.patch("os.path.isfile", return_value=exists):
        return v, v._get_url_from_source()


class DriveLetterDirectPathTest(unittest.TestCase):
    def test_unmounted_drive_falls_back_to_the_stream(self):
        for path in (r"D:\Movies\x.mkv", "D:/Movies/x.mkv"):
            with self.subTest(path=path):
                v, url = _url(path, exists=False)
                self.assertFalse(v.direct_path)
                self.assertTrue(url.startswith(SERVER + "/Videos/item1/stream"))

    def test_mounted_drive_plays_the_file(self):
        v, url = _url(r"D:\Movies\x.mkv", exists=True)
        self.assertTrue(v.direct_path)
        self.assertEqual(url, r"D:\Movies\x.mkv")

    def test_a_real_scheme_is_still_a_remote_direct_path(self):
        v, url = _url("smb://nas/media/x.mkv", exists=False)
        self.assertTrue(v.direct_path)


if __name__ == "__main__":
    unittest.main()
