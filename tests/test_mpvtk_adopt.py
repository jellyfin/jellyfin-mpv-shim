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

    def _scroll_config(self, ext, **settings):
        """The mpvtk-wheel payload the app forwards for a backend flavour."""
        import json

        from jellyfin_mpv_shim import conf

        app = MpvtkApp.attach(FakeMPV(), ext=ext)
        saved = {k: getattr(conf.settings, k) for k in settings}
        for key, value in settings.items():
            setattr(conf.settings, key, value)
        try:
            app.push_scroll_config()
        finally:
            for key, value in saved.items():
                setattr(conf.settings, key, value)
        payload = next(c for c in app.backend.mpv.commands
                       if c[0] == "script-message" and c[1] == "mpvtk-wheel")
        return json.loads(payload[2])

    def test_the_backend_does_not_decide_how_scrolling_looks(self):
        """Out of process an image is a file mpv opens and mmaps rather than
        an address in this process, and a scrolling frame re-issues every
        visible one — which used to be reason enough to force quantizing
        there, sight unseen.

        The renderer times its own frames now, and that mmap happens inside
        the overlay-add calls it times, so an external mpv is observed to be
        expensive rather than assumed to be — confirmed on a real one, where
        continuous scrolling holds up. So the payload depends on the setting
        and nothing else, and both backends answer identically."""
        for mode in ("continuous", "aligned", "row"):
            self.assertEqual(self._scroll_config(ext=True, scroll_mode=mode),
                             self._scroll_config(ext=False, scroll_mode=mode),
                             "%r differs between backends" % mode)

    def test_continuous_asks_the_renderer_for_neither_mitigation(self):
        cfg = self._scroll_config(ext=False, scroll_mode="continuous")
        self.assertFalse(cfg["snapped"])
        self.assertFalse(cfg["force_snap"])

    def test_aligned_quantizes_the_drawing_but_not_the_notch(self):
        """The distinction the two old booleans kept losing: the wheel still
        moves by pixels and the scrollbar still glides, and only what is
        drawn is pulled onto a row."""
        cfg = self._scroll_config(ext=False, scroll_mode="aligned")
        self.assertFalse(cfg["snapped"])
        self.assertTrue(cfg["force_snap"])

    def test_a_row_per_notch_is_drawn_aligned_too(self):
        """Not redundant, and the reason the setting is three states rather
        than two checkboxes: stepping a whole row per notch means every
        offset is already on a boundary, so asking for both is the one
        combination that never meant anything."""
        cfg = self._scroll_config(ext=False, scroll_mode="row")
        self.assertTrue(cfg["snapped"])
        self.assertTrue(cfg["force_snap"])

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
