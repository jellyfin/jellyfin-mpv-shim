"""Unit tests for mpvtk's attach-to-existing-mpv path (MpvtkApp.attach /
AdoptBackend) — the production wiring where the browser UI shares the
player's own mpv window instead of spawning a second one.

Uses the integration harness's FakeMPV as a stand-in for the player's
handle: it records commands and lets us fire client-message / shutdown
events the way a real renderer / mpv would. No real mpv, no window, so
these run in the fast suite (``python3 -m unittest discover tests``).
"""

import json
import os
import unittest

from tests.integration._harness import FakeMPV

from jellyfin_mpv_shim.mpvtk.app import MpvtkApp, AdoptBackend, _RENDERER
from jellyfin_mpv_shim.mpvtk.widgets import Box, Column


def _click_scene(app, clicks):
    """Give the app a one-node clickable scene and render it (populating
    the handler registry + pushing the scene to the fake handle)."""
    app.size = (400, 300)
    app._build = lambda size: Column(
        [Box(w=50, h=50, bg="222222", id="btn",
             on_click=lambda: clicks.append(1))],
        w=size[0], h=size[1],
    )
    app._render()


class TestAdoptBackend(unittest.TestCase):
    def test_loads_renderer_into_shared_handle(self):
        fake = FakeMPV()
        MpvtkApp.attach(fake, ext=False)
        self.assertIn(("load-script", _RENDERER), fake.commands)

    def test_in_process_tracks_backend_flavor(self):
        # libmpv (ext=False) => in-process memory images; jsonipc (ext=True)
        # => scratch files.
        self.assertTrue(MpvtkApp.attach(FakeMPV(), ext=False).in_process)
        self.assertFalse(MpvtkApp.attach(FakeMPV(), ext=True).in_process)

    def test_attach_requires_ext(self):
        with self.assertRaises(ValueError):
            MpvtkApp(mpv_handle=FakeMPV())  # ext omitted

    def test_stop_does_not_terminate_shared_handle(self):
        fake = FakeMPV()
        app = MpvtkApp.attach(fake, ext=False)
        app.backend.stop()
        self.assertFalse(
            fake.terminated,
            "attach() must never terminate the player's shared mpv",
        )

    def test_renderer_click_reaches_python_handler(self):
        # End-to-end of the attach path: renderer reports a click via a
        # client-message -> AdoptBackend decode -> app queue -> dispatch
        # -> the node's on_click.
        fake = FakeMPV()
        app = MpvtkApp.attach(fake, ext=False)
        clicks = []
        _click_scene(app, clicks)

        # The renderer speaks to us as an mpv client-message.
        fake.fire_event(
            "client-message",
            {"args": ["mpvtk-event", json.dumps({"t": "click", "id": "btn"})]},
        )
        kind, evt = app._queue.get_nowait()
        self.assertEqual(kind, "evt")
        app._dispatch(evt)
        self.assertEqual(clicks, [1])

    def test_scene_push_goes_to_shared_handle(self):
        fake = FakeMPV()
        app = MpvtkApp.attach(fake, ext=False)
        _click_scene(app, [])
        pushed = [c for c in fake.commands
                  if c[:2] == ("script-message", "mpvtk-scene")]
        self.assertTrue(pushed, "the scene must be pushed to the shared handle")
        scene = json.loads(pushed[-1][2])
        self.assertTrue(any(n.get("id") == "btn" for n in scene["nodes"]))

    def test_shutdown_event_quits_the_loop(self):
        fake = FakeMPV()
        app = MpvtkApp.attach(fake, ext=False)
        # drain the initial nothing; fire mpv shutdown
        fake.fire_event("shutdown", None)
        # The quit hook enqueues the sentinel the run loop breaks on.
        drained = []
        while not app._queue.empty():
            drained.append(app._queue.get_nowait())
        self.assertIn(("__quit", None), drained)

    def test_ext_jsonipc_decode(self):
        # jsonipc delivers a plain dict; ext=True path reads ["args"].
        fake = FakeMPV()
        app = MpvtkApp.attach(fake, ext=True)
        got = []
        app.backend.on_client_message(got.append)
        fake.fire_event("client-message", {"args": ["mpvtk-event", "{}"]})
        self.assertEqual(got, [["mpvtk-event", "{}"]])

    def test_libmpv_struct_decode(self):
        # libmpv delivers a struct exposing as_dict() with byte args.
        class _Evt:
            def as_dict(self):
                return {"args": [b"mpvtk-event", b"{}"]}

        fake = FakeMPV()
        app = MpvtkApp.attach(fake, ext=False)
        got = []
        app.backend.on_client_message(got.append)
        fake.fire_event("client-message", _Evt())
        self.assertEqual(got, [["mpvtk-event", "{}"]])


if __name__ == "__main__":
    unittest.main()
