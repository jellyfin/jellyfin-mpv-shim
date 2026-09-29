"""`tools/e2e_preclean.py`'s filesystem half: what it may remove.

The half that deletes from disk, so it is pinned here; the API half needs a
server and is exercised by running the tool against one."""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import os
import shutil
import sys
import tempfile
import unittest

sys.argv = [sys.argv[0]]

TOOLS = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools")


def load_tool():
    import importlib.util
    path = os.path.join(TOOLS, "e2e_preclean.py")
    spec = importlib.util.spec_from_file_location("e2e_preclean", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LitterFoldersTest(unittest.TestCase):
    def setUp(self):
        self.tool = load_tool()
        self.state = tempfile.mkdtemp(prefix="preclean-")
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)

    def playlist(self, relative, name, xml=True):
        # Joined per component: a literal "data/data/playlists" kept its
        # slashes on Windows while os.walk answers in backslashes.
        path = os.path.join(self.state, *relative.split("/"), name)
        os.makedirs(path)
        if xml:
            with open(os.path.join(path, "playlist.xml"), "w",
                      encoding="utf-8") as fh:
                fh.write("<Item/>")
        return path

    def test_only_the_suites_own_folders_with_a_playlist_in_them(self):
        ours = self.playlist("data/data/playlists", "jms-e2e-music11")
        self.playlist("data/data/playlists", "My Road Trip")
        self.playlist("data/data/playlists", "jms-e2e-empty", xml=False)
        self.assertEqual([ours], self.tool.litter_folders(self.state))

    def test_both_server_layouts_are_found(self):
        serve = self.playlist("data/data/playlists", "jms-e2e-a")
        container = self.playlist("config/data/playlists", "jms-e2e-b")
        self.assertEqual(sorted([serve, container]),
                         self.tool.litter_folders(self.state))

    def test_a_folder_of_that_name_that_is_not_a_playlist_store(self):
        """A `playlists` directory with no playlist.xml under it is not
        Jellyfin's store, whatever its children are called."""
        os.makedirs(os.path.join(self.state, "elsewhere", "playlists",
                                 "jms-e2e-x"))
        self.assertEqual([], self.tool.litter_folders(self.state))


if __name__ == "__main__":
    unittest.main()
