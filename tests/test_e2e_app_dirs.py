"""The e2e harness removes the config dirs it creates, relaunches included.

Ownership used to follow the App object, so a test that relaunched on its
own dir (`_flows.relaunch` and nine direct sites) closed a non-owning App
at the end and the dir stayed. test_store_safety leaves a 1 GiB big.bin in
its; on a RAM-backed /tmp those filled the Kubuntu VM until the OOM killer
took the desktop.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import importlib.util
import os
import shutil
import tempfile
import unittest
from unittest import mock

# By file path, for the reason test_e2e_relay gives.
_spec = importlib.util.spec_from_file_location(
    "_e2e_app", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "e2e", "_app.py"))
_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_app)


class ConfigDirOwnershipTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("JMS_E2E_KEEP_CONFIG", None)

    def test_a_relaunch_on_the_same_dir_removes_it(self):
        first = _app.App()
        self.addCleanup(shutil.rmtree, first.config_dir, True)
        again = _app.App(config_dir=first.config_dir)
        again.close()
        self.assertFalse(os.path.exists(first.config_dir))

    def test_a_dir_the_harness_did_not_make_is_left_alone(self):
        mine = tempfile.mkdtemp(prefix="jms-test-own-")
        self.addCleanup(shutil.rmtree, mine, True)
        _app.App(config_dir=mine).close()
        self.assertTrue(os.path.isdir(mine))

    def test_keep_config_still_keeps_it(self):
        os.environ["JMS_E2E_KEEP_CONFIG"] = "1"
        app = _app.App()
        self.addCleanup(shutil.rmtree, app.config_dir, True)
        _app.App(config_dir=app.config_dir).close()
        self.assertTrue(os.path.isdir(app.config_dir))


if __name__ == "__main__":
    unittest.main()
