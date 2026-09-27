"""The server switcher's Offline entry (D3, ruled 2026-09-26).

Drop to offline while a server is unreliable, and come back, without a
restart. The entry is offered only when something is downloaded (case 4);
picking it browses the downloads (case 2) and it reads as selected there;
picking a server from offline reconnects (case 3). A chosen Offline is not
undone by a server bouncing back -- that is when it is chosen. End to end:
tests/e2e/test_offline_ui.TheOfflineEntryTest.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser import ui as ui_mod  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.repository import (  # noqa: E402
    OfflineLibrarySource,
)
from jellyfin_mpv_shim.sync.db import SyncDB  # noqa: E402
from tests._shell_harness import (  # noqa: E402
    FakeController, FakeSource, _SyncPool, build_scene,
)


class _Ctl(FakeController):
    """One saved server, and a download catalog that may or may not hold
    anything. ``up`` is whether the server answers a reconnect."""

    def __init__(self, offline, downloads=True, up=True):
        super().__init__()
        self.offline = offline
        self.downloads = downloads
        self.up = up
        self.retried = []
        self.offline_asked = 0

    def switcher_servers(self):
        return [{"uuid": "srv1", "name": "Home", "connected": self.up}]

    list_servers = switcher_servers

    def has_downloads(self):
        return self.downloads

    def offline_source(self):
        self.offline_asked += 1
        return self.offline if self.downloads else None

    def retry_server(self, uuid):
        self.retried.append(uuid)
        return (True, None) if self.up else (False, None)

    def rebuild_source(self):
        return FakeSource() if self.up else None


class _Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "catalog.db")
        SyncDB(path).close()
        self.offline = OfflineLibrarySource(path)

    def browser(self, **kw):
        ctl = _Ctl(self.offline, **kw)
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b.server = "srv1"
        return b, ctl

    @staticmethod
    def switcher(b):
        nodes, handlers = build_scene(b, size=(1600, 900))
        node = next((n for n in nodes if n.get("id") == "nav-server"), None)
        return node, handlers

    def pick(self, b, label):
        node, handlers = self.switcher(b)
        # By name: a server that is down reads "Home (needs reconnect)".
        i = next(i for i, e in enumerate(node["items"])
                 if e == label or e.startswith(label + " ("))
        handlers["nav-server"]["select"](i, node["items"][i])
        t = b._long_thread
        if t is not None:
            t.join(5)


class OfferedWhenThereIsSomethingToBrowseTest(_Case):
    def test_not_offered_with_nothing_downloaded(self):
        b, _ctl = self.browser(downloads=False)
        node, _h = self.switcher(b)
        self.assertIsNone(node, "a one-server switcher with nothing "
                                "downloaded has nowhere else to go")

    def test_offered_last_with_a_folder_once_something_is_downloaded(self):
        b, _ctl = self.browser()
        node, _h = self.switcher(b)
        self.assertEqual(["Home", "Offline"], node["items"])
        self.assertEqual(0, node["sel"])
        self.assertNotEqual(node["icons"][0], node["icons"][1])


class PickingItTest(_Case):
    def test_it_browses_the_downloads_and_reads_as_selected(self):
        b, ctl = self.browser()
        self.pick(b, "Offline")
        self.assertTrue(b.offline)
        self.assertIs(self.offline, b.source)
        self.assertTrue(b.offline_chosen)
        node, _h = self.switcher(b)
        self.assertEqual(["Home", "Offline"], node["items"],
                         "the server must stay listed: it is the way back")
        self.assertEqual(1, node["sel"])

    def test_nothing_to_browse_says_so_and_stays(self):
        b, ctl = self.browser()
        ctl.downloads = True           # offered...
        ctl.offline_source = lambda: None   # ...and gone by the time it loads
        before = b.source
        self.pick(b, "Offline")
        self.assertIs(before, b.source)
        self.assertFalse(b.offline)
        self.assertIn("Nothing is downloaded", b.status)


class ComingBackTest(_Case):
    def test_picking_the_server_reconnects_and_leaves_offline(self):
        b, ctl = self.browser()
        self.pick(b, "Offline")
        self.pick(b, "Home")
        self.assertEqual(["srv1"], ctl.retried)
        self.assertFalse(b.offline)
        self.assertFalse(b.offline_chosen)

    def test_a_server_that_is_still_down_leaves_you_offline(self):
        b, ctl = self.browser()
        self.pick(b, "Offline")
        ctl.up = False
        self.pick(b, "Home")
        self.assertEqual(["srv1"], ctl.retried)
        self.assertTrue(b.offline)
        self.assertIs(self.offline, b.source)


class ABounceDoesNotUndoItTest(_Case):
    """ui._on_server_connected rebuilds the live source whenever a server
    (re)connects in the background -- a health check, a websocket redial.
    Several times over, because it fires on every bounce."""

    def _ui(self, b):
        ui = ui_mod.UserInterface.__new__(ui_mod.UserInterface)
        ui._browser = b
        return ui

    def test_a_chosen_offline_survives_every_reconnect(self):
        b, _ctl = self.browser()
        self.pick(b, "Offline")
        ui = self._ui(b)
        with mock.patch.object(ui_mod, "PlayerGateway") as gw:
            gw.return_value.rebuild_source.return_value = FakeSource()
            for _ in range(3):
                ui._on_server_connected()
                self.assertTrue(b.offline)
                self.assertIs(self.offline, b.source)

    def test_an_offline_nobody_chose_still_comes_back_by_itself(self):
        b, _ctl = self.browser()
        b.set_source(self.offline)           # launched with nothing up
        self.assertFalse(b.offline_chosen)
        live = FakeSource()
        with mock.patch.object(ui_mod, "PlayerGateway") as gw:
            gw.return_value.rebuild_source.return_value = live
            self._ui(b)._on_server_connected()
        self.assertFalse(b.offline)
        self.assertIs(live, b.source)


class _Manager:
    """clientManager, as far as retry_server reads it: one saved server
    whose client is still registered -- nothing has noticed yet that the
    network under it is gone, which is the moment right after a cut."""

    def __init__(self, alive):
        self.cred = {"uuid": "srv1", "address": "http://h"}
        self.credentials = [self.cred]
        self.clients = {"srv1": object()}
        self.alive = alive
        self.validated = []
        self.connects = 0

    def validate_client(self, client, dry_run=False, server=None):
        self.validated.append(server)
        if not self.alive:
            self.clients.pop("srv1", None)     # what the real one does
        return self.alive

    def connect_client(self, server, do_retries=True):
        self.connects += 1
        return server["uuid"] in self.clients or self.alive

    def connection_problem(self, uuid):
        return "unreachable"


class RetryAsksTheServerTest(unittest.TestCase):
    """retry_server answered from the registry: a client registered before
    a cut read as a successful reconnect, so picking the server from
    Offline switched to a Home that spun ~20 s and then fell back to the
    downloads without a word (scenario 11). It asks the server now."""

    def _retry(self, alive):
        from jellyfin_mpv_shim.mpvtk_browser import gateway
        from jellyfin_mpv_shim.mpvtk_browser.gateway import deps
        mgr = _Manager(alive)
        with mock.patch.object(deps, "clientManager", mgr):
            return gateway.PlayerGateway().retry_server("srv1"), mgr

    def test_a_registered_client_that_does_not_answer_is_a_failure(self):
        (ok, problem), mgr = self._retry(alive=False)
        self.assertFalse(ok)
        self.assertEqual("unreachable", problem)
        self.assertEqual([mgr.cred], mgr.validated)

    def test_a_registered_client_that_answers_is_a_success(self):
        (ok, _p), mgr = self._retry(alive=True)
        self.assertTrue(ok)
        self.assertEqual([mgr.cred], mgr.validated)


if __name__ == "__main__":
    unittest.main()
