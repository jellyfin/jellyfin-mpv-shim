"""The external mpv's socket closing ends the app like a shutdown event (B7).

mpv's "shutdown" event is not guaranteed to reach an IPC client before the
socket closes, and on the external backend it was the app's only exit
signal: a window closed after a download or a playback left the app running
with no window. `PlayerManager._on_ipc_closed` runs the handle's "shutdown"
handlers when the socket goes. What must NOT trigger it is pinned as
carefully as what must: our own teardown, a superseded handle, and an event
that already arrived -- each of which would shut down a live session.

End to end: tests/e2e/test_app_smoke (the window-close test, jsonipc leg).
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

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.player import PlayerManager  # noqa: E402


class _Inter:
    _stopping = False


class _Handle:
    """What python-mpv-jsonipc's MPV holds that the handler reads."""

    def __init__(self):
        self.mpv_inter = _Inter()
        self.calls = []
        self.event_bindings = {"shutdown": {self._on_shutdown}}

    def _on_shutdown(self, event):
        self.calls.append(event)


class IpcClosedTest(unittest.TestCase):
    def _pm(self, handle):
        pm = PlayerManager.__new__(PlayerManager)
        pm._player = handle
        return pm

    def test_a_closed_socket_runs_the_shutdown_handlers(self):
        h = _Handle()
        self._pm(h)._on_ipc_closed(h)
        self.assertEqual([None], h.calls)

    def test_not_when_we_are_tearing_it_down_ourselves(self):
        h = _Handle()
        h.mpv_inter._stopping = True
        self._pm(h)._on_ipc_closed(h)
        self.assertEqual([], h.calls)

    def test_not_for_a_handle_that_has_been_replaced(self):
        old, new = _Handle(), _Handle()
        self._pm(new)._on_ipc_closed(old)
        self.assertEqual([], old.calls)
        self.assertEqual([], new.calls)

    def test_not_again_after_the_event_itself_arrived(self):
        h = _Handle()
        h._jms_shutdown_seen = True
        self._pm(h)._on_ipc_closed(h)
        self.assertEqual([], h.calls)

    def test_the_shutdown_handler_runs_once_per_handle(self):
        """Both routes can fire: the event, and then the socket closing."""
        h = _Handle()
        pm = self._pm(h)
        pm._idle_quit = False
        tasks = []
        pm.put_task = tasks.append
        pm._terminate_mpv = lambda player=None: None
        for _ in range(3):
            pm._on_shutdown_event(None)
        self.assertEqual(1, len(tasks), "the shutdown teardown ran %d times"
                         % len(tasks))


if __name__ == "__main__":
    unittest.main()
