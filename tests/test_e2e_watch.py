"""The e2e harness's watch mode (tests/e2e/_app.py `_Watch`): narration for
a person watching the real window, which must stay silent when off and
never write a masked field's text."""

import importlib.util
import os
import tempfile
import unittest
from unittest import mock

# By file path, not by putting tests/e2e on sys.path (see test_e2e_relay).
_spec = importlib.util.spec_from_file_location(
    "_e2e_app", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "e2e", "_app.py"))
_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_app)


class _Keys:
    def __init__(self):
        self.sent = []

    def command(self, *args):
        self.sent.append(args)


class WatchModeTest(unittest.TestCase):
    def _watch(self, **env):
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        self.addCleanup(os.remove, path)
        env = dict(env, JMS_E2E_NARRATE=path)
        with mock.patch.dict(os.environ, env, clear=False):
            for k in ("JMS_E2E_WATCH", "JMS_E2E_STEP"):
                if k not in env:
                    os.environ.pop(k, None)
            watch = _app._Watch()
        app = _app.App.__new__(_app.App)
        app.mpv = _Keys()
        return watch, app, path

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_off_it_says_nothing_and_waits_for_nothing(self):
        watch, app, path = self._watch()
        with mock.patch.object(_app, "WATCH", watch):
            app.key("TAB")
            app.type("abc")
        self.assertEqual("", self.read(path))
        self.assertEqual(4, len(app.mpv.sent))

    def test_on_it_narrates_each_action_but_not_a_password(self):
        watch, app, path = self._watch(JMS_E2E_WATCH="0.001")
        with mock.patch.object(_app, "WATCH", watch):
            app.key("TAB")
            app.type("hunter2", masked=True)
        text = self.read(path)
        self.assertIn("key TAB", text)
        self.assertIn("type *******", text)
        self.assertNotIn("hunter2", text)
        # One line for the whole string, not one per key inside it.
        self.assertEqual(2, len(text.splitlines()))
