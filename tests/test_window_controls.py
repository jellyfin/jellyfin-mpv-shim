"""Client-side decorations: the top bar standing in for a title bar.

Two halves, tested apart because they fail apart.

**Deciding.** ``WindowMixin.window_controls_wanted`` answers "does this
window have a title bar of its own" by reading mpv's ``border``, not by
sniffing ``XDG_CURRENT_DESKTOP``. That is not a shortcut, it is the more
correct answer: mpv writes ``border=false`` itself on a Wayland compositor
with no ``zxdg_decoration_manager_v1`` (mutter, i.e. every GNOME session)
and otherwise writes back the mode the compositor actually *granted*. So
one property read covers GNOME Wayland, KDE Wayland, X11, win32, and the
user who simply passed ``--border=no`` -- and none of them need a special
case here.

**Drawing.** The bar has to gain the controls, become draggable, and give
the press something to land on -- that last one being the part with a real
trap in it, since the bar drops its background entirely under a gradient
theme.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import unittest

from jellyfin_mpv_shim.conf import settings
from jellyfin_mpv_shim.mpvtk.layout import layout
from jellyfin_mpv_shim.mpvtk.widgets import Row, Text
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
from jellyfin_mpv_shim.player_window import WindowMixin

from tests._shell_harness import FakeController, FakeSource, build_scene, ids


class FakeMpv:
    """Just the four window properties this asks about. Missing ones raise,
    which is what an older mpv does -- ``title_bar`` is 0.38+."""

    def __init__(self, **props):
        self._props = props

    def __getattr__(self, name):
        try:
            return self.__dict__["_props"][name]
        except KeyError:
            raise AttributeError(name)


class Window(WindowMixin):
    """WindowMixin on its own. It is a mixin on PlayerManager in production,
    but nothing below touches anything outside this file."""

    def __init__(self, player=None, alive=True):
        self._player = player
        self._mpv_alive = alive
        self.on_decorations_changed = None


class WindowSettingCase(unittest.TestCase):
    def setUp(self):
        prior = settings.window_controls
        self.addCleanup(setattr, settings, "window_controls", prior)


class TestDecorationDetection(WindowSettingCase):
    def setUp(self):
        super().setUp()
        settings.window_controls = "auto"

    def test_a_window_with_no_border_gets_our_own_title_bar(self):
        # The GNOME Wayland case: mutter implements no xdg-decoration
        # protocol at all, so mpv writes border=false into the option itself
        # and the window has nothing to drag or close by.
        w = Window(FakeMpv(border=False, title_bar=True, fullscreen=False))
        self.assertTrue(w.window_controls_wanted())

    def test_a_decorated_window_is_left_alone(self):
        # KDE/sway Wayland (which grant server-side decorations) and X11.
        # Drawing controls over a real title bar is the worse failure of the
        # two, so this direction matters more than the one above.
        w = Window(FakeMpv(border=True, title_bar=True, fullscreen=False))
        self.assertFalse(w.window_controls_wanted())

    def test_a_missing_title_bar_counts_even_with_a_border(self):
        # win32 can drop the title bar and keep a resizable frame. A window
        # with no title bar is one with nothing to drag or close by, which is
        # the whole question -- so `border` alone is not enough.
        w = Window(FakeMpv(border=True, title_bar=False, fullscreen=False))
        self.assertTrue(w.window_controls_wanted())

    def test_an_mpv_without_title_bar_answers_from_border_alone(self):
        # mpv < 0.38 has no `title-bar`. Its absence must read as "this build
        # only has `border`", not as "there is no title bar" -- which would
        # have put controls on every window on every older mpv.
        w = Window(FakeMpv(border=True, fullscreen=False))
        self.assertFalse(w.window_controls_wanted())
        w = Window(FakeMpv(border=False, fullscreen=False))
        self.assertTrue(w.window_controls_wanted())

    def test_an_unanswerable_window_is_left_alone(self):
        # No properties at all, or no mpv. Silence is not evidence that the
        # desktop drew nothing.
        self.assertFalse(Window(FakeMpv()).window_controls_wanted())
        self.assertFalse(Window(None).window_controls_wanted())
        self.assertFalse(
            Window(FakeMpv(border=False), alive=False)
            .window_controls_wanted())

    def test_fullscreen_has_no_title_bar_to_stand_in_for(self):
        w = Window(FakeMpv(border=False, title_bar=False, fullscreen=True))
        self.assertFalse(w.window_controls_wanted())


class TestWindowControlsSetting(WindowSettingCase):
    def test_never_declines_an_undecorated_window(self):
        settings.window_controls = "never"
        w = Window(FakeMpv(border=False, fullscreen=False))
        self.assertFalse(w.window_controls_wanted())

    def test_always_overrides_a_decorated_one(self):
        settings.window_controls = "always"
        w = Window(FakeMpv(border=True, title_bar=True, fullscreen=False))
        self.assertTrue(w.window_controls_wanted())

    def test_always_still_stands_down_in_fullscreen(self):
        # "always" answers "does this desktop decorate my windows", which is
        # not a request for furniture over the top of a fullscreen video.
        settings.window_controls = "always"
        w = Window(FakeMpv(border=True, fullscreen=True))
        self.assertFalse(w.window_controls_wanted())

    def test_an_unrecognised_value_behaves_as_auto(self):
        settings.window_controls = "sometimes"
        self.assertTrue(
            Window(FakeMpv(border=False, fullscreen=False))
            .window_controls_wanted())
        self.assertFalse(
            Window(FakeMpv(border=True, fullscreen=False))
            .window_controls_wanted())

    def test_an_empty_value_behaves_as_auto(self):
        settings.window_controls = ""
        self.assertTrue(
            Window(FakeMpv(border=False, fullscreen=False))
            .window_controls_wanted())


class TestWindowChromeState(WindowSettingCase):
    """One call, because each of these is an mpv property read and on the
    jsonipc backend that is an IPC round trip -- the UI has to be able to
    snapshot on a change event instead of reading per frame."""

    def setUp(self):
        super().setUp()
        settings.window_controls = "auto"

    def test_reports_controls_and_maximized_together(self):
        w = Window(FakeMpv(border=False, fullscreen=False,
                           window_maximized=True))
        self.assertEqual(w.window_chrome_state(),
                         {"controls": True, "maximized": True})

    def test_an_unreadable_maximized_is_false_not_an_error(self):
        w = Window(FakeMpv(border=False, fullscreen=False))
        self.assertEqual(w.window_chrome_state(),
                         {"controls": True, "maximized": False})


class TestDecorationChangeNotifies(unittest.TestCase):
    """On Wayland this is not something we did: mpv writes `border` from the
    compositor's configure event, which can land after the window is up."""

    def test_the_observer_fires_the_callback(self):
        seen = []
        w = Window(FakeMpv(border=False))
        w.on_decorations_changed = lambda: seen.append(1)
        w._on_border_change("border", False)
        self.assertEqual(seen, [1])

    def test_an_unset_callback_is_not_an_error(self):
        Window(FakeMpv())._on_border_change("border", False)

    def test_a_raising_callback_does_not_reach_mpvs_event_thread(self):
        def boom():
            raise RuntimeError("nope")

        w = Window(FakeMpv())
        w.on_decorations_changed = boom
        w._on_border_change("border", False)     # must not raise


class TestWindowDragMarker(unittest.TestCase):
    """``window_drag`` has to produce a node for the press to land on."""

    def test_a_bar_with_no_background_still_gets_a_hit_rect(self):
        # The trap: the top bar drops its fill entirely on themes that paint
        # a gradient behind it. If `wdrag` rode on `bg` the title bar would
        # silently stop being draggable on exactly those themes.
        nodes, _h = layout(Row([Text("hi")], h=60, bg=None, window_drag=True),
                           800, 600)
        rects = [n for n in nodes if n["t"] == "rect" and n.get("wdrag")]
        self.assertEqual(len(rects), 1, "no node for the drag to land on")
        self.assertEqual(rects[0]["w"], 800)

    def test_an_ordinary_bar_is_not_draggable(self):
        nodes, _h = layout(Row([Text("hi")], h=60, bg="112233"), 800, 600)
        self.assertFalse([n for n in nodes if n.get("wdrag")])

    def test_the_marker_does_not_disturb_the_fill(self):
        nodes, _h = layout(Row([Text("hi")], h=60, bg="112233",
                               window_drag=True), 800, 600)
        rect = [n for n in nodes if n["t"] == "rect"][0]
        self.assertEqual(rect["fill"], "112233")
        self.assertTrue(rect["wdrag"])


class TestWindowResizeMarker(unittest.TestCase):
    """``window_resize`` has to produce a node too, and for a harder reason
    than ``window_drag``: the grip has no background at all. It is an
    invisible corner with three dots drawn over it, so the marker is the
    only thing that can conjure a hit rect."""

    def test_an_empty_corner_still_gets_a_hit_rect(self):
        nodes, _h = layout(Row([], w=22, h=22, window_resize="se"), 800, 600)
        rects = [n for n in nodes if n["t"] == "rect" and n.get("wsize")]
        self.assertEqual(len(rects), 1, "no node for the resize to land on")
        self.assertEqual(rects[0]["wsize"], "se")

    def test_an_ordinary_box_does_not_resize_the_window(self):
        nodes, _h = layout(Row([Text("hi")], h=60, bg="112233"), 800, 600)
        self.assertFalse([n for n in nodes if n.get("wsize")])


class ChromeCase(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)

    def _bar(self, nodes):
        return [n for n in nodes if n.get("wdrag")]


class TestTopBarAsTitleBar(ChromeCase):
    def test_a_decorated_window_gets_no_controls_and_no_drag(self):
        nodes, _h = build_scene(self.b)
        self.assertNotIn("win-close", ids(nodes))
        self.assertFalse(self._bar(nodes),
                         "the top bar drags a window that has a title bar")

    def test_an_undecorated_window_gets_all_three_and_a_drag_handle(self):
        self.b._csd = True
        nodes, _h = build_scene(self.b)
        got = ids(nodes)
        for node_id in ("win-min", "win-max", "win-close"):
            self.assertIn(node_id, got)
        self.assertTrue(self._bar(nodes), "the title bar cannot be dragged")

    def _grip(self, nodes):
        return [n for n in nodes if n.get("wsize")]

    def test_an_undecorated_window_gets_a_resize_corner(self):
        self.b._csd = True
        nodes, _h = build_scene(self.b, (1280, 720))
        grip = self._grip(nodes)
        self.assertEqual(len(grip), 1, "nothing to resize the window by")
        # The window's own corner, not the corner of whatever the page
        # happens to end in.
        self.assertEqual(grip[0]["x"] + grip[0]["w"], 1280)
        self.assertEqual(grip[0]["y"] + grip[0]["h"], 720)

    def test_a_decorated_window_gets_no_resize_corner(self):
        nodes, _h = build_scene(self.b, (1280, 720))
        self.assertFalse(self._grip(nodes),
                         "a window with a frame grew a second resize handle")

    def test_a_maximized_window_gets_no_resize_corner(self):
        # Writing geometry there un-maximizes the window rather than
        # resizing it, which is not what dragging a corner asks for.
        self.b._csd = True
        self.b._maximized = True
        nodes, _h = build_scene(self.b, (1280, 720))
        self.assertFalse(self._grip(nodes))

    def test_the_dots_do_not_swallow_the_grip(self):
        # node_at returns the TOPMOST interactive node, and the dots are
        # drawn over the hit rect. They are plain filled boxes with no
        # interaction of their own, which is what keeps the corner grabbable
        # rather than dead in three places.
        self.b._csd = True
        nodes, _h = build_scene(self.b, (1280, 720))
        grip = self._grip(nodes)[0]
        over = [n for n in nodes[nodes.index(grip) + 1:]
                if n.get("click") or n.get("wsize") or n.get("hover")]
        self.assertFalse(over, "something is drawn over the resize corner")

    def test_the_controls_sit_outboard_of_the_apps_own_buttons(self):
        # Window furniture goes last, past Settings, as it does on every
        # desktop -- so the app's own controls do not move when the desktop
        # turns out to be undecorated.
        self.b._csd = True
        nodes, _h = build_scene(self.b)
        by_id = {n["id"]: n for n in nodes if n.get("id")}
        self.assertLess(by_id["nav-settings"]["x"], by_id["win-min"]["x"])
        self.assertLess(by_id["win-min"]["x"], by_id["win-max"]["x"])
        self.assertLess(by_id["win-max"]["x"], by_id["win-close"]["x"])

    def test_the_maximize_button_shows_which_state_it_is_in(self):
        # The one piece of state a title bar actually shows. A button that
        # does not change when the window does reads as broken.
        from jellyfin_mpv_shim.mpvtk.vector import svg_path_to_ass
        from jellyfin_mpv_shim.ui_icon_paths import ICON_PATHS

        self.b._csd = True

        def glyph():
            # An icon node carries no name -- only the drawn path and `hb`,
            # the button it lights up with.
            nodes, _h = build_scene(self.b)
            found = [n["path"] for n in nodes
                     if n["t"] == "icon" and n.get("hb") == "win-max"]
            self.assertEqual(len(found), 1, "the maximize button has no icon")
            return found[0]

        def drawn(name):
            return svg_path_to_ass(ICON_PATHS[name])

        self.b._maximized = False
        self.assertIn(drawn("crop_square"), glyph())
        self.b._maximized = True
        self.assertIn(drawn("filter_none"), glyph())

    def test_the_controls_take_room_from_the_bar_not_from_the_window(self):
        # "Narrow the bar slightly to fit them": they go through the same fit
        # probe as everything else, so a bar that no longer has room for its
        # labels goes icon-only rather than overflowing or clipping the
        # title.
        from jellyfin_mpv_shim.mpvtk_browser import window_chrome

        plain = window_chrome.chrome_bar(self.b, compact=False, probe=True)
        self.b._csd = True
        with_controls = window_chrome.chrome_bar(self.b, compact=False,
                                                 probe=True)
        from jellyfin_mpv_shim.mpvtk.layout import natural_size

        self.assertGreater(natural_size(with_controls)[0],
                           natural_size(plain)[0],
                           "the window controls cost the bar nothing, so "
                           "they are not being measured")


class TestControlsReachThePlayer(ChromeCase):
    def setUp(self):
        super().setUp()
        self.b._csd = True
        self.called = []
        for name in ("close_window", "minimize_window",
                     "toggle_window_maximized"):
            setattr(self.ctl, name,
                    (lambda n: lambda: self.called.append(n))(name))

    def _click(self, node_id):
        _nodes, handlers = build_scene(self.b)
        handlers[node_id]["click"]()

    def test_close_goes_through_the_same_path_as_mpvs_own_close(self):
        # Not its own idea of "close": close_to_tray and the no-tray
        # safeguard have to decide, once, in one place. A second close button
        # that quits directly is how the two drift apart.
        self._click("win-close")
        self.assertEqual(self.called, ["close_window"])

    def test_minimize_iconifies_rather_than_hiding_to_the_tray(self):
        # NOT the app's own minimize(), which tears the window down and
        # keeps running headless. The title bar's button has to mean what
        # that button means in every other window.
        self._click("win-min")
        self.assertEqual(self.called, ["minimize_window"])
        self.assertEqual(self.ctl.minimized, 0,
                         "the title bar's minimize hid the app to the tray")

    def test_maximize_toggles_the_window(self):
        self._click("win-max")
        self.assertEqual(self.called, ["toggle_window_maximized"])


class TestTheHudGetsThemToo(unittest.TestCase):
    """A windowed video on an undecorated desktop is otherwise a window
    with no way to close, minimize or move it. ESC does get you back to
    the library, but "press an undocumented key first" is not a close
    button -- so the HUD's header grows the same three."""

    def _playing(self, csd=True):
        from tests._shell_harness import HudController

        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=HudController())
        b._browsing = False
        b.hud.shown = True
        b.hud.state = {"stopped": False, "is_audio": False,
                       "title": "Movie", "position": 50.0,
                       "duration": 100.0, "paused": False}
        b._csd = csd
        return b

    def test_the_hud_header_carries_the_controls(self):
        nodes, _h = build_scene(self._playing(), (1280, 720))
        got = ids(nodes)
        for node_id in ("hud-win-min", "hud-win-max", "hud-win-close"):
            self.assertIn(node_id, got)

    def test_a_decorated_window_leaves_the_hud_alone(self):
        nodes, _h = build_scene(self._playing(csd=False), (1280, 720))
        self.assertNotIn("hud-win-close", ids(nodes))
        self.assertFalse([n for n in nodes if n.get("wdrag")])

    def test_the_hud_header_drags_the_window(self):
        nodes, _h = build_scene(self._playing(), (1280, 720))
        bar = [n for n in nodes if n.get("id") == "hud-topbar"]
        self.assertEqual(len(bar), 1, "the HUD header has no node")
        self.assertTrue(bar[0].get("wdrag"),
                        "the video window cannot be dragged by its header")

    def test_the_hud_gets_the_resize_corner_too(self):
        # A windowed video on an undecorated desktop is otherwise a window
        # that can be moved but never resized.
        nodes, _h = build_scene(self._playing(), (1280, 720))
        self.assertEqual(len([n for n in nodes if n.get("wsize")]), 1)

    def test_they_are_smaller_than_the_huds_own_buttons(self):
        # Window furniture sits below the content controls everywhere. At
        # transport size these read as three more playback actions.
        nodes, _h = build_scene(self._playing(), (1280, 720))
        by_id = {n["id"]: n for n in nodes if n.get("id")}
        self.assertLess(by_id["hud-win-close"]["w"], by_id["hud-back"]["w"])

    def test_the_ids_do_not_collide_with_the_library_bars(self):
        # A scene may not hold two nodes with one id, and the two bars are
        # built from the same helper.
        from jellyfin_mpv_shim.mpvtk_browser import window_chrome

        b = self._playing()
        chrome = window_chrome.window_controls(b)
        hud = window_chrome.window_controls(b, prefix="hud-win")
        chrome_ids = {c.id for c in chrome if getattr(c, "id", None)}
        hud_ids = {c.id for c in hud if getattr(c, "id", None)}
        self.assertTrue(chrome_ids and hud_ids)
        self.assertFalse(chrome_ids & hud_ids)

    def test_both_bars_reach_the_same_three_actions(self):
        # One helper, parameterized, precisely so these cannot drift into a
        # window you can close from one screen and not the other.
        b = self._playing()
        called = []
        b.close_window = lambda: called.append("close")
        b.minimize_window = lambda: called.append("min")
        b.toggle_maximized = lambda: called.append("max")
        _nodes, handlers = build_scene(b, (1280, 720))
        for node_id in ("hud-win-min", "hud-win-max", "hud-win-close"):
            handlers[node_id]["click"]()
        self.assertEqual(sorted(called), ["close", "max", "min"])


class TestRefreshFromThePlayer(ChromeCase):
    def test_the_snapshot_is_taken_from_the_controller(self):
        self.ctl.window_chrome_state = lambda: {"controls": True,
                                                "maximized": True}
        self.b.refresh_window_controls()
        self.assertTrue(self.b.window_controls)
        self.assertTrue(self.b.maximized)

    def test_a_change_asks_for_a_repaint(self):
        # A Button's glyph is drawn from `maximized`; only a redraw can move
        # it, and nothing else in the shell is going to ask for one when a
        # compositor configure event lands.
        painted = []
        self.b.invalidate = lambda: painted.append(1)
        self.ctl.window_chrome_state = lambda: {"controls": True,
                                                "maximized": False}
        self.b.refresh_window_controls()
        self.assertTrue(painted, "the new title bar never got drawn")

    def test_an_unchanged_snapshot_does_not_repaint(self):
        painted = []
        self.ctl.window_chrome_state = lambda: {"controls": False,
                                                "maximized": False}
        self.b.invalidate = lambda: painted.append(1)
        self.b.refresh_window_controls()
        self.assertEqual(painted, [])

    def test_no_player_behind_us_is_not_an_error(self):
        # Offline stand-ins and test doubles have no window to ask about.
        self.b.refresh_window_controls()
        self.assertFalse(self.b.window_controls)

    def test_a_raising_controller_leaves_the_bar_as_it_was(self):
        def boom():
            raise RuntimeError("no mpv")

        self.b._csd = True
        self.ctl.window_chrome_state = boom
        self.b.refresh_window_controls()
        self.assertTrue(self.b.window_controls)


if __name__ == "__main__":
    unittest.main()


class TestTheGripCostsThePageNothing(ChromeCase):
    """A Float is drawn absolutely, but a Column still divides its main axis
    by what its children DECLARE. Declaring a height on the Float reserved
    22px of page for something positioned outside the flow: every scroll
    viewport lost it, and the now-playing bar floated that far off the
    bottom of the window with an empty strip beneath it."""

    def _viewport(self, csd):
        self.b._csd = csd
        nodes, _h = build_scene(self.b, (1280, 720))
        scrolls = [n for n in nodes if n.get("t") == "scroll"]
        self.assertTrue(scrolls, "no scroll container on the home screen")
        return max(n["y"] + n["h"] for n in scrolls)

    def test_the_content_still_reaches_the_bottom_of_the_window(self):
        self.assertEqual(self._viewport(csd=True), self._viewport(csd=False))
