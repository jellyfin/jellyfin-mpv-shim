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

    def test_an_unknown_mode_falls_back_to_the_default(self):
        """A hand-edited typo must land on continuous, not on a mitigation.

        The trap is that the obvious spelling of the test -- `force_snap =
        mode != "continuous"` -- fails the other way, and silently: the
        settings dropdown falls back to displaying option 0 for a value it
        does not recognise, so "Continuous" with the capital C the dropdown
        itself DISPLAYS would quantize permanently while Settings went on
        showing the mode the user thought they had. `snapped_scrolling` got
        this for free by going through adv_bool; a bare str does not."""
        for junk in ("Continuous", "", "aligne", "true", "row ", "0"):
            cfg = self._scroll_config(ext=False, scroll_mode=junk)
            self.assertFalse(cfg["force_snap"], "%r quantized" % junk)
            self.assertFalse(cfg["snapped"], "%r stepped" % junk)

    def test_the_dropdown_offers_exactly_the_modes_conf_understands(self):
        """Two lists, one meaning. The dropdown is where a user picks a
        value and conf.SCROLL_MODES is what decides behaviour, so a value in
        one and not the other is either an unreachable mode or a menu entry
        that falls back to the default when chosen."""
        from jellyfin_mpv_shim.conf import SCROLL_MODES
        from jellyfin_mpv_shim.mpvtk_browser import config as browser_config

        offered = [v for _label, v in
                   browser_config.LABELED_ENUMS["scroll_mode"]]
        self.assertEqual(offered, list(SCROLL_MODES))

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


class DisabledControlsAbsorbThePointer(unittest.TestCase):
    """A disabled control has to EXIST as a node.

    Button and Checkbox mute themselves in Python -- they drop their
    handler, their hover and (flat) their background -- because a
    composite's colours live in child nodes the renderer cannot recognise.
    That left `layout` with nothing to emit: no bg, no border, no on_click.

    With no node there is nothing to absorb the press, so it reached
    whatever the control sat over. Over the playback HUD that is bare video,
    where renderer.lua toggles pause -- so a disabled flat button paused the
    film. The node also carries the tooltip explaining why the control is
    off, which is the one thing the feature's docstring insists on.
    """

    def _nodes(self, el):
        from jellyfin_mpv_shim.mpvtk.layout import layout
        from jellyfin_mpv_shim.mpvtk.widgets import Column
        nodes, _h = layout(Column([el], w=300, h=60), 300, 60)
        return nodes

    def _node(self, el):
        return next((n for n in self._nodes(el) if n.get("id") == el.id), None)

    def test_a_disabled_flat_button_still_emits_a_node(self):
        from jellyfin_mpv_shim.mpvtk.widgets import Button
        node = self._node(Button("x", id="b", flat=True, disabled=True,
                                 tip="not now", on_click=lambda: None))
        self.assertIsNotNone(node, "nothing for the pointer to land on")
        self.assertTrue(node.get("dis"))
        self.assertEqual(node.get("tip"), "not now",
                         "the reason it is disabled never reached the "
                         "renderer")
        self.assertFalse(node.get("click"), "a disabled button is clickable")

    def test_a_disabled_checkbox_still_emits_a_node(self):
        from jellyfin_mpv_shim.mpvtk.widgets import Checkbox
        node = self._node(Checkbox("x", False, id="c", disabled=True,
                                   on_toggle=lambda: None))
        self.assertIsNotNone(node, "nothing for the pointer to land on")
        self.assertTrue(node.get("dis"))
        self.assertFalse(node.get("click"))

    def test_an_enabled_one_is_unchanged(self):
        from jellyfin_mpv_shim.mpvtk.widgets import Button
        node = self._node(Button("x", id="b", flat=True,
                                 on_click=lambda: None))
        self.assertTrue(node.get("click"))
        self.assertFalse(node.get("dis"))


class TestGamepadPush(unittest.TestCase):
    """The controller binding table on its way to the renderer."""

    def _push(self, **settings):
        import json

        from jellyfin_mpv_shim import conf

        app = MpvtkApp.attach(FakeMPV(), ext=False)
        saved = {k: getattr(conf.settings, k) for k in settings}
        for key, value in settings.items():
            setattr(conf.settings, key, value)
        try:
            app.push_gamepad()
        finally:
            for key, value in saved.items():
                setattr(conf.settings, key, value)
        payload = next(c for c in app.backend.mpv.commands
                       if c[0] == "script-message" and c[1] == "mpvtk-gamepad")
        return json.loads(payload[2])

    def test_it_sends_the_table_the_settings_ask_for(self):
        from jellyfin_mpv_shim import gamepad

        self.assertEqual(self._push(gamepad_swap_confirm=False),
                         gamepad.bindings(False))
        self.assertEqual(self._push(gamepad_swap_confirm=True),
                         gamepad.bindings(True))

    def test_the_swap_actually_changes_what_is_sent(self):
        # Guards the assertion above against both calls resolving to the
        # same thing: a payload built from a captured or defaulted flag
        # would satisfy it and change nothing on the pad.
        self.assertNotEqual(self._push(gamepad_swap_confirm=False),
                            self._push(gamepad_swap_confirm=True))

    def test_a_fresh_renderer_is_told_without_being_asked(self):
        # mpv is re-created (idle-quit, a cast, force_window), and each new
        # handle gets a brand-new renderer with no bindings at all. If the
        # table only ever went out on a settings change, the controller
        # would work until the first re-open and then stop.
        #
        # `_push_metrics` is stubbed out, and has to be: it measures the
        # real font stack and installs the result through layout.set_metrics,
        # which is a MODULE GLOBAL. Letting a `ready` land for real here
        # leaves every later test in the process laying text out against
        # measured widths instead of the heuristic table -- which is not an
        # error anywhere, it just silently moves every scene snapshot in the
        # suite by a few percent. (Found exactly that way: seven snapshot
        # tests failed in the full run and passed on their own.)
        from unittest import mock

        app = MpvtkApp.attach(FakeMPV(), ext=False)
        with mock.patch.object(app, "_push_metrics"):
            app._dispatch({"t": "ready", "w": 1280, "h": 720})
        self.assertTrue(any(c[0] == "script-message"
                            and c[1] == "mpvtk-gamepad"
                            for c in app.backend.mpv.commands))

    def test_the_sticks_report_through_their_own_handlers(self):
        # The renderer sends these two rather than a keypress, so a missing
        # dispatch arm is a stick that does nothing at all.
        app = MpvtkApp.attach(FakeMPV(), ext=False)
        seen = []
        app.on_gamepad_seek = lambda d: seen.append(("seek", d))
        app.on_gamepad_nav = lambda a: seen.append(("nav", a))
        app._dispatch({"t": "gpseek", "dir": "left"})
        app._dispatch({"t": "gpnav", "a": "menu"})
        self.assertEqual(seen, [("seek", "left"), ("nav", "menu")])

    def test_an_unwired_app_ignores_them(self):
        app = MpvtkApp.attach(FakeMPV(), ext=False)
        app._dispatch({"t": "gpseek", "dir": "left"})
        app._dispatch({"t": "gpnav", "a": "menu"})
