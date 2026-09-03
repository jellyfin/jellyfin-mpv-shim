"""Phase 9.0 exit test: the playback-HUD lifecycle over REAL video.

Proves the attached-but-idle inversion end to end on a live mpv, per
backend: yielding to video with ``osc_style: mpvtk`` puts the renderer
in HUD-idle (blank scene, summon bindings only), an arrow keypress
summons the HUD (full input sections + the browser's HUD scene, focus
landing on play/pause), ENTER activates the focused transport button,
ESC hides it, ~4s of inactivity auto-hides it, and stopping playback
drops HUD mode entirely as browse resumes. No player.py, no server —
a fake controller records the transport calls.

It is also where the WHEEL is proved, because ownership of a key is not
a thing a fake mpv can model: the three tests near the end install the
reporter's own `WHEEL_DOWN add volume -5` with mpv's `keybind` and press
it through mpv's own dispatch, so what they measure is which section won
(#711). All three fail on the pre-fix renderer.
"""

import copy
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
# ...and the repo root. Run as a script -- which the __main__ block at the
# bottom invites -- `sys.path[0]` is this directory and the root is on the
# path nowhere, so `jellyfin_mpv_shim` resolves to whatever is pip-installed:
# silently, and it *runs*, against the previous release. Measured once as a
# renderer.lua from a fortnight ago failing a test about this tree.
# run_integration.py is unaffected (it spawns -m unittest with cwd=root).
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
import _harness as h  # noqa: E402
from test_mpvtk_browser import _make_source, _spawn_handle  # noqa: E402


class FakeController:
    """Records transport calls; opts into the HUD like the real
    PlayerGateway does when osc_style is "mpvtk"."""

    def __init__(self):
        self.calls = []
        self.menu_state = None
        self.chapter_list = []
        # tests drive summons with arrow keypresses, so opt into the
        # grab (the no-grab default has its own test).
        #
        # hide/mode are conf.py's defaults rather than omitted: an absent
        # `hide` means zero, which the renderer floors at 0.5s -- and a
        # HUD that hides half a second after each keypress cannot be
        # walked with the arrow keys at all.
        self.key_opts = {"grab": True, "key": "ENTER",
                         "hide": 4, "mode": "hover"}

    def hud_key_opts(self):
        return dict(self.key_opts)

    def hud_menu_state(self):
        return self.menu_state

    def hud_action(self, verb, arg=None):
        self.calls.append(("hud_action", verb, arg))

    def chapters(self):
        return list(self.chapter_list)

    def use_hud(self):
        return True

    def on_browse_enter(self):
        self.calls.append("enter")

    def on_browse_leave(self):
        self.calls.append("leave")

    def refresh_playstate(self):
        self.calls.append("refresh")

    def toggle_pause(self):
        self.calls.append("toggle_pause")

    def stop(self):
        self.calls.append("stop")

    def next(self):
        self.calls.append("next")

    def prev(self):
        self.calls.append("prev")

    def seek(self, secs):
        self.calls.append(("seek", secs))

    def set_paused(self, paused):
        self.calls.append(("set_paused", (paused,)))

    def check_updates(self):
        pass

    def playback_info(self):
        """What the Playback Info panel reads.

        The shell suite's own fixture, imported rather than retyped: the
        wheel test below needs the panel's body to OVERFLOW to have
        anything to scroll, and a locally-invented blob that happens to
        fit would leave that test passing against a panel with nowhere to
        go. Imported lazily, like test_settings_screen.py does.
        """
        from tests._shell_harness import HUD_PLAYBACK_INFO
        return copy.deepcopy(HUD_PLAYBACK_INFO)

    def player_stats(self):
        from tests._shell_harness import HUD_PLAYER_STATS
        return dict(HUD_PLAYER_STATS)


VIDEO_STATE = {
    "stopped": False, "is_audio": False, "title": "HUD Clip",
    "position": 3.0, "duration": 30.0, "paused": False,
}


@h.require_real_mpv
class TestPlaybackHudLifecycle(h.TmpDirTest):
    def setUp(self):
        super().setUp()
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
        from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore, cache_dir
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore

        self.handle, ext = _spawn_handle()
        self.app = MpvtkApp.attach(self.handle, ext=ext)
        strips = (StripStore(mem_store=MemoryStore()) if self.app.in_process
                  else StripStore(cache_dir=cache_dir("mpvtk-itest-")))
        self.ctl = FakeController()
        self.browser = MpvtkBrowser(self.app, _make_source(), strips=strips,
                                    controller=self.ctl)
        self._thread = threading.Thread(
            target=lambda: self.app.run(self.browser.build), daemon=True)
        self._thread.start()
        self.assertTrue(self.app.ready.wait(15),
                        "renderer never became ready in the attached mpv")

    def tearDown(self):
        try:
            self.app.quit()
            self._thread.join(timeout=5)
        finally:
            self.browser.shutdown()
            try:
                self.handle.terminate()
            except Exception:
                pass
        super().tearDown()

    # ----------------------------------------------------------- helpers

    def _play_video(self):
        """Real video in the window + the playstate push that yields."""
        clip = h.make_test_clip(
            os.path.join(self.tmp, "clip.mp4"), duration=30)
        self.handle.command("loadfile", clip)
        self.browser.on_playstate(dict(VIDEO_STATE))

    def _state(self):
        st = self.app.debug_state()
        self.assertIsNotNone(st, "no debug state from renderer")
        return st

    def _wait(self, cond, timeout=6, msg=""):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return
            time.sleep(0.15)
        self.fail(msg or "condition never became true")

    def _click_when_hittable(self, node_id, timeout=6):
        """Click a node once the RENDERER can hit-test it.

        node_rect() reads the last scene PYTHON pushed, but debug(cmd=click)
        resolves the id through the renderer's own state.byid. Those are not
        the same instant: the renderer processes scenes asynchronously, so
        under load a click can land before reconcile and center_of() returns
        nil — the click is silently dropped and the test fails somewhere
        unrelated, reporting only that the action never happened.

        Hovering proves the renderer has the node, because hover and click
        share center_of().
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.app.debug(cmd="hover", id=node_id)
            if (self._state() or {}).get("hover") == node_id:
                self.app.debug(cmd="click", id=node_id)
                return
            time.sleep(0.15)
        self.fail("%s never became hit-testable in the renderer" % node_id)

    def _keypress(self, key):
        self.handle.command("keypress", key)

    def _set_pause(self, paused):
        # libmpv's command() validates "pause" as a native bool and
        # rejects both Python bools and "yes"/"no" via command nodes;
        # property assignment works there. jsonipc has no attribute
        # protocol shortcut, so it keeps the command form.
        if h.BACKEND == "jsonipc":
            self.handle.command("set_property", "pause", paused)
        else:
            self.handle.pause = paused

    def _menu_row_index(self, label):
        """Where ``label`` sits in the open menu, read from the scene the
        renderer was actually given — not from the row builder, which
        would be this test agreeing with itself."""
        menu = next((n for n in (self.app._nodes or [])
                     if n.get("id") == "hud-menu"), None)
        self.assertIsNotNone(menu, "no menu in the pushed scene")
        for i, item in enumerate(menu["items"]):
            if label.lower() in item.lower():
                return i
        self.fail("no %r row in %r" % (label, menu["items"]))

    def _highlight_menu_row(self, label):
        """Walk the keyboard highlight onto ``label``'s row, and prove it
        landed there before the caller presses ENTER.

        The highlight starts *unset* and the first arrow computes from 0,
        so one DOWN lands on row 1 while one UP clamps onto row 0 — which
        makes row 0 the one row a DOWN cannot reach first.
        """
        idx = self._menu_row_index(label)
        for _ in range(1 if idx == 0 else idx):
            self._keypress("UP" if idx == 0 else "DOWN")
        self._wait(lambda: self._state().get("nav_pidx") == idx,
                   msg="highlight never reached the %r row" % label)

    def _press_until(self, key, cond, timeout=6, msg=""):
        """Press ``key`` until ``cond`` holds. mpv applies script
        key-binding section updates asynchronously, so a single press
        can race the (un)bind that a lifecycle transition just issued —
        exactly like a real user's press can, whose remedy is also to
        press again."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._keypress(key)
            for _ in range(6):
                if cond():
                    return
                time.sleep(0.1)
        self.fail(msg or "%s never took effect" % key)

    # ------------------------------------------------------------- tests

    def test_full_lifecycle(self):
        # --- yield to video -> HUD-idle (attached, blank, summonable)
        self._play_video()
        self._wait(lambda: self._state().get("phud_mode")
                   and not self._state().get("active"),
                   msg="renderer never entered HUD-idle")
        st = self._state()
        self.assertFalse(st.get("phud_shown"))
        self.assertIn("leave", self.ctl.calls)

        # --- arrow keypress summons; the seek bar wakes focused AND
        # active (adjust mode) so remote arrows scrub immediately
        self._press_until("LEFT", lambda: self.browser.hud.shown,
                          msg="summon never reached the browser")
        self._wait(lambda: self._state().get("nav") == "hud-seek",
                   msg="focus did not land on the seek bar: %r"
                   % self._state().get("nav"))
        st = self._state()
        self.assertTrue(st.get("phud_shown"))
        self.assertTrue(st.get("active"))
        self.assertIn("refresh", self.ctl.calls)

        # --- DOWN steps off the bar and lands ON play/pause, then
        # ENTER activates it.
        #
        # On it, not merely somewhere in the transport row: the button
        # declares nav_gravity, so the arrow no longer depends on which
        # control happens to sit nearest the middle -- which moved with
        # the window width, because the chapter and seek buttons come and
        # go with it. This used to press LEFT until it arrived, which is
        # what a user had to do too.
        # ONE press, then wait. `_press_until` is the wrong instrument for
        # a which-node claim: it presses again every 0.6s for six seconds,
        # so without the gravity a later DOWN can walk out of the row and
        # `nav_wrap` can bring focus back around to play/pause anyway --
        # which would weaken "lands on play/pause" to "is reachable by
        # mashing DOWN". The bind race `_press_until` exists for was
        # already waited out by the seek-bar focus check above.
        self._press_until(
            "DOWN", lambda: self._state().get("nav") != "hud-seek",
            msg="DOWN never moved focus off the seek bar")
        self.assertEqual(
            self._state().get("nav"), "hud-pp",
            "DOWN off the seek bar did not land on play/pause")
        self._press_until(
            "ENTER", lambda: "toggle_pause" in self.ctl.calls,
            msg="ENTER on play/pause never reached the controller")

        # --- ESC hides the HUD (back to idle, still summonable)
        self._press_until("ESC", lambda: not self.browser.hud.shown,
                          msg="ESC did not hide the HUD")
        st = self._state()
        self.assertTrue(st.get("phud_mode"))
        self.assertFalse(st.get("phud_shown"))
        self.assertFalse(st.get("active"))

        # --- summonable again; auto-hides after ~4s without input
        self._press_until("UP", lambda: self.browser.hud.shown,
                          msg="second summon failed")
        self._wait(lambda: not self.browser.hud.shown, timeout=8,
                   msg="HUD never auto-hid")
        self.assertTrue(self._state().get("phud_mode"))

        # --- stop -> browse resumes, HUD mode fully dropped
        self.browser.on_playstate({"stopped": True})
        self._wait(lambda: self._state().get("active")
                   and not self._state().get("phud_mode"),
                   msg="browse never took the window back")
        self.assertIn("enter", self.ctl.calls)
        self.assertTrue(self.browser._browsing)

    def test_scrub_commit_cancel_and_preview(self):
        # Fake trickplay data: 10 raw-BGRA frames, 3s apart (the format
        # the TrickPlay worker writes to raw_images.<seq>.bin), announced
        # exactly as the worker announces it. Nothing in Python reads this
        # file -- the renderer opens it itself and hands mpv a byte offset
        # into it, which is the point of #618.
        tw, th, count = 64, 36, 10
        raw = os.path.join(self.tmp, "raw_images.bin")
        with open(raw, "wb") as fh:
            for i in range(count):
                px = bytes((20 * i % 256, 128, 255 - 20 * i % 256, 255))
                fh.write(px * (tw * th))
        self.handle.command("script-message", "shim-trickplay-bif",
                            str(count), "3000", str(tw), str(th), raw)

        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="renderer never entered HUD-idle")
        self._press_until("LEFT", lambda: self.browser.hud.shown,
                          msg="summon failed")
        self._wait(lambda: self._state().get("nav") == "hud-seek",
                   msg="focus never landed on the seek bar")

        # The bar wakes already in adjust mode: LEFT scrubs 5% back —
        # a 'change' that must NOT seek, only pause, set the pending
        # scrub target, and float the preview thumbnail.
        self._press_until(
            "LEFT", lambda: self.browser.hud.scrub is not None,
            msg="adjust-mode scrub never reached the browser")
        seeks = [c for c in self.ctl.calls if isinstance(c, tuple)
                 and c[0] == "seek"]
        self.assertEqual(seeks, [], "scrubbing must not seek mid-gesture")
        self.assertIn(("set_paused", (True,)), [
            c for c in self.ctl.calls if isinstance(c, tuple)],
            "scrub start must pause playback")
        self._wait(lambda: self._state().get("preview") is not None,
                   msg="trickplay preview never appeared")
        # ...with a real frame in it, read straight out of the tile file.
        self.assertIsNotNone(self._state()["preview"].get("frame"),
                             "the bubble drew no trickplay frame")

        # ENTER commits: exactly one seek at the scrubbed position.
        target = self.browser.hud.scrub
        self._keypress("ENTER")
        self._wait(lambda: any(isinstance(c, tuple) and c[0] == "seek"
                               for c in self.ctl.calls),
                   msg="commit never seeked")
        seeks = [c for c in self.ctl.calls if isinstance(c, tuple)
                 and c[0] == "seek"]
        self.assertEqual(len(seeks), 1)
        self.assertAlmostEqual(seeks[0][1], target, delta=2.0)
        self.assertIsNone(self.browser.hud.scrub)

        # Second gesture, abandoned with ESC: no new seek, preview
        # cleared, HUD still up. (The always-adjust bar is still live
        # after the commit — no arming press needed.)
        self._press_until(
            "LEFT", lambda: self.browser.hud.scrub is not None,
            msg="second scrub never started")
        # single press: the ESC binding has been stable since summon (no
        # rebind race), and a second ESC would hide the whole HUD
        self._keypress("ESC")
        self._wait(lambda: self.browser.hud.scrub is None,
                   msg="ESC never cancelled the scrub")
        self.assertTrue(self.browser.hud.shown,
                        "cancelling a scrub must not hide the HUD")
        seeks = [c for c in self.ctl.calls if isinstance(c, tuple)
                 and c[0] == "seek"]
        self.assertEqual(len(seeks), 1, "cancel must not seek")
        self._wait(lambda: self._state().get("preview") is None,
                   msg="preview never cleared after cancel")

    def test_pickers_chapters_and_skip_button(self):
        self.ctl.menu_state = {
            "has_media": True,
            "audio": [
                {"id": 1, "label": "English 5.1", "selected": True},
                {"id": 2, "label": "Commentary", "selected": False},
            ],
            "subtitles": [
                {"id": -1, "label": "None", "selected": True},
                {"id": 3, "label": "English", "selected": False},
            ],
            "quality": {"current": "No Transcode", "options": [
                {"id": "none", "label": "No Transcode", "selected": True},
                {"id": 20, "label": "20 Mbps", "selected": False},
            ]},
        }
        self.ctl.chapter_list = [
            {"title": "Opening", "time": 0.0},
            {"title": "Part Two", "time": 12.0},
        ]
        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="renderer never entered HUD-idle")
        self._press_until("LEFT", lambda: self.browser.hud.shown,
                          msg="summon failed")
        for nid in ("hud-audio", "hud-sub", "hud-quality", "hud-chapters"):
            self._wait(lambda nid=nid: self.app.node_rect(nid) is not None,
                       msg="picker %s never materialized" % nid)

        # audio picker: open the popup, choose the second track — must
        # route through osc_bridge's dispatcher verb
        self.app.debug(cmd="click", id="hud-audio")
        self._wait(lambda: self._state().get("dd_open") == "hud-audio",
                   msg="audio popup never opened")
        self.app.debug(cmd="popup", index=1)
        self._wait(lambda: ("hud_action", "set-audio", 2) in self.ctl.calls,
                   msg="audio selection never dispatched: %r"
                   % self.ctl.calls)

        # chapter picker: choosing a chapter seeks to its start
        self.app.debug(cmd="click", id="hud-chapters")
        self._wait(lambda: self._state().get("dd_open") == "hud-chapters",
                   msg="chapter popup never opened")
        self.app.debug(cmd="popup", index=1)
        self._wait(lambda: ("seek", 12.0) in self.ctl.calls,
                   msg="chapter selection never seeked: %r" % self.ctl.calls)

        # skip button appears with the playstate's skip_label and fires
        # the skip verb
        self.browser.on_playstate(dict(VIDEO_STATE,
                                       skip_label="Skip Intro"))
        self._wait(lambda: self.app.node_rect("hud-skip") is not None,
                   msg="skip button never appeared")
        self._click_when_hittable("hud-skip")
        self._wait(lambda: ("hud_action", "skip-segment", None)
                   in self.ctl.calls,
                   msg="skip button never dispatched: %r" % self.ctl.calls)

    def test_seek_hover_bubble(self):
        """Hovering the bar floats the bubble WITHOUT touching Python.

        The chapters come from mpv, not from the fake controller: the
        renderer reads chapter-list itself, so nothing here has to send
        them. Nothing reaches the browser at all -- the assertion is that
        the bubble exists in the renderer and that no scene node does.
        """
        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="never entered HUD-idle")
        self._press_until("LEFT", lambda: self.browser.hud.shown,
                          msg="summon failed")
        self._wait(lambda: self.app.node_rect("hud-seek") is not None,
                   msg="seek bar never materialized")
        # park the pointer on the middle of the seek bar
        self.app.debug(cmd="hover", id="hud-seek")
        self._wait(lambda: self._state().get("preview") is not None,
                   msg="hover bubble never appeared")
        bubble = self._state()["preview"]
        self.assertAlmostEqual(bubble["secs"], 15.0, delta=3.0)
        self.assertIsNone(self.app.node_rect("hud-preview"),
                          "the bubble must not be a scene node any more")
        # moving off the bar retracts it
        self.app.debug(cmd="hover", id="hud-pp")
        self._wait(lambda: self._state().get("preview") is None,
                   msg="hover bubble never cleared")

    def test_settings_menu_keyboard_flow(self):
        self.ctl.menu_state = {"has_media": True, "quality": {
            "current": "No Transcode", "options": [
                {"id": "none", "label": "No Transcode", "selected": True},
                {"id": 20, "label": "20 Mbps", "selected": False},
            ]}}
        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="never entered HUD-idle")
        self._press_until("LEFT", lambda: self.browser.hud.shown,
                          msg="summon failed")
        self._wait(lambda: self.app.node_rect("hud-settings") is not None,
                   msg="gear button never materialized")
        self.app.debug(cmd="click", id="hud-settings")
        self._wait(lambda: self.browser.hud.menu == "root",
                   msg="gear click never opened the settings menu")
        self._wait(lambda: self._state().get("menu_open"),
                   msg="menu never reached the renderer")
        # Walk the highlight to the Playback Speed row, then ENTER to swap
        # in its submenu. Located by label rather than by a fixed index:
        # the gear's root is conditional (a row drops out when the bar has
        # its own button for it, or when the state offers nothing), so a
        # hardcoded index quietly selects a neighbour and the test then
        # fails somewhere other than where it broke.
        self._highlight_menu_row("Playback Speed")
        self._press_until(
            "ENTER", lambda: self.browser.hud.menu == "speed",
            msg="menu selection never opened the speed submenu")
        # ESC steps back out of the menu without hiding the HUD
        self._wait(lambda: self._state().get("menu_open"),
                   msg="submenu never reached the renderer")
        self._keypress("ESC")
        self._wait(lambda: self.browser.hud.menu is None,
                   msg="ESC never dismissed the menu")
        self.assertTrue(self.browser.hud.shown,
                        "dismissing the menu must not hide the HUD")

    # ----------------------------------------------- the wheel (#711)

    def _volume(self):
        return float(getattr(self.handle, "volume"))

    def _bind_user_wheel(self):
        """The reporter's `WHEEL_DOWN add volume -5`, installed the one way
        a test can.

        `keybind` lands where input.conf lands, so a forced script section
        still outranks it -- which is the whole of #711. It also means the
        notch has to go through mpv's own dispatch (`keypress`): the
        renderer's debug wheel hook calls on_wheel directly, so it would
        pass whoever owns the section and prove nothing about ownership.
        """
        self.handle.command("keybind", "WHEEL_DOWN", "add volume -5")

    def _summon(self):
        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="never entered HUD-idle")
        self._press_until("LEFT", lambda: self.browser.hud.shown,
                          msg="summon failed")

    def _open_info_panel(self):
        """Through the gear menu, as a viewer reaches it."""
        self._wait(lambda: self.app.node_rect("hud-settings") is not None,
                   msg="gear button never materialized")
        self.app.debug(cmd="click", id="hud-settings")
        self._wait(lambda: self._state().get("menu_open"),
                   msg="gear click never opened the settings menu")
        self._highlight_menu_row("Playback Info")
        self._press_until("ENTER", lambda: self.browser.hud.info,
                          msg="the menu never opened the panel")
        self._wait(lambda: self.app.node_rect("hud-info-scroll") is not None,
                   msg="the panel never reached the renderer")
        rect = self.app.node_rect("hud-info-scroll")
        # The precondition, asserted rather than assumed: a panel whose body
        # fits has nothing to scroll, and every wheel assertion below would
        # then pass against a container that could not have moved.
        self.assertGreater(rect["ch"], rect["h"],
                           "the info panel does not overflow, so nothing "
                           "here is measuring a scroll")
        return rect

    def test_a_bare_hud_leaves_the_wheel_to_the_user(self):
        """#711: the forced wheel section swallowed every notch for as long
        as the controls were up, so `WHEEL_UP add volume 5` worked only
        while they were hidden. Nothing on the bar scrolls, so it is not
        ours to hold."""
        self._summon()
        self._bind_user_wheel()
        before = self._volume()
        self._press_until(
            "WHEEL_DOWN", lambda: self._volume() < before,
            msg="the summoned HUD swallowed the user's own wheel binding")
        self.assertTrue(self.browser.hud.shown,
                        "the wheel should not have dismissed the bar")

    def test_the_info_panel_takes_the_wheel_and_gives_it_back(self):
        """The other half, and the half that breaks silently: releasing the
        wheel must not cost the one thing on the HUD that does scroll.

        Both directions in one test on purpose -- "the panel scrolls" and
        "the volume binding works" pass individually against a section that
        is permanently on and permanently off respectively, and it is the
        handover between them that has no other coverage."""
        self._summon()
        self._bind_user_wheel()
        rect = self._open_info_panel()
        # scroll_at hit-tests against the pointer, so the pointer has to be
        # on the panel -- a notch over the bar would find nothing whatever
        # the section says. By COORDINATE, not by id: `moveto id=` resolves
        # through the renderer's own state.byid, which a scene it has not
        # reconciled yet does not contain, and a moveto that resolves to
        # nothing is dropped in silence (see _click_when_hittable). The
        # pointer sticks where it is put, so the retry below covers the
        # reconcile.
        self.app.debug(cmd="moveto",
                       x=rect["x"] + rect["w"] / 2,
                       y=rect["y"] + rect["h"] / 2)
        off0 = (self.app.scroll_offsets() or {}).get("hud-info-scroll", 0)
        before = self._volume()
        self._press_until(
            "WHEEL_DOWN",
            lambda: (self.app.scroll_offsets() or {}).get(
                "hud-info-scroll", 0) > off0,
            msg="the open Playback Info panel would not scroll")
        self.assertEqual(self._volume(), before,
                         "the panel scrolled AND the notch reached mpv: the "
                         "section is not taking the wheel back")
        self.assertLessEqual(
            (self.app.scroll_offsets() or {}).get("hud-info-scroll", 0),
            rect["ch"] - rect["h"] + 1,
            "scrolled past the end of the panel")

        # ...and closing it hands the wheel back. This is the assertion that
        # fails if the release is ever made unconditional-on-HUD again.
        self._keypress("ESC")
        self._wait(lambda: not self.browser.hud.info,
                   msg="ESC never closed the panel")
        self._press_until(
            "WHEEL_DOWN", lambda: self._volume() < before,
            msg="the wheel stayed claimed after the panel closed")

    def test_an_open_picker_takes_the_wheel_over_the_hud(self):
        """The third surface, and the other branch of the predicate: a
        dropdown popup is handed the notch before any hit test, so this is
        the one place on the HUD where the wheel works without the pointer
        being over anything in particular."""
        # Enough chapters that the popup is CLIPPED -- a list that fits
        # scrolls nowhere, and the assertion below would pass against a
        # popup that never moved.
        self.ctl.chapter_list = [{"title": "Chapter %d" % i,
                                  "time": float(i) * 0.5} for i in range(40)]
        self._summon()
        self._bind_user_wheel()
        self._wait(lambda: self.app.node_rect("hud-chapters") is not None,
                   msg="chapter picker never materialized")
        self.app.debug(cmd="click", id="hud-chapters")
        self._wait(lambda: self._state().get("dd_open") == "hud-chapters",
                   msg="chapter popup never opened")
        # `dd_open` is set by the click; `dd_geo` is computed by the next
        # RENDER, and the two are not the same instant. Reading the geometry
        # off the earlier one gives `{}` -- n=0, count=0, and an assertion
        # about clipping that fails on a popup that is merely not drawn yet.
        # (Seen once, in the whole-suite leg, which is where the renderer is
        # slowest.)
        self._wait(
            lambda: ((self._state() or {}).get("dd_geo") or {}).get(
                "count", 0) > 0,
            msg="the popup never reported its geometry")
        geo = (self._state() or {}).get("dd_geo") or {}
        self.assertLess(geo.get("n", 0), geo.get("count", 0),
                        "the popup is not clipped, so nothing here is "
                        "measuring a scroll")
        # From where it OPENED, not from zero: a popup scrolls to its
        # selected row on open, so `off > 0` can already be true before a
        # notch has been delivered -- and this test then passes with the
        # section disabled, which is the state it exists to catch.
        off0 = geo.get("off", 0)
        before = self._volume()
        self._press_until(
            "WHEEL_DOWN",
            lambda: ((self._state() or {}).get("dd_geo") or {}).get(
                "off", 0) > off0,
            msg="the open chapter picker would not scroll")
        self.assertEqual(self._volume(), before,
                         "the popup scrolled AND the notch reached mpv")

        self._keypress("ESC")
        self._wait(lambda: self._state().get("dd_open") is None,
                   msg="ESC never closed the popup")
        self._press_until(
            "WHEEL_DOWN", lambda: self._volume() < before,
            msg="the wheel stayed claimed after the picker closed")

    def test_default_no_grab_only_wake_key_summons(self):
        """With hud_grab_keys off (the shipped default), idle arrows
        keep their mpv meaning; only the wake key (ENTER) summons —
        and it toggles pause — while remote arrows still summon via
        the script-message path."""
        self.ctl.key_opts = {"grab": False, "key": "ENTER"}
        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="never entered HUD-idle")
        # arrows are NOT taken over: no summon
        for _ in range(3):
            self._keypress("LEFT")
            time.sleep(0.2)
        self.assertFalse(self.browser.hud.shown,
                         "LEFT must not summon with grab off")
        # the wake key still summons (it also pause-toggles; not
        # asserted here — a retried press would make the count racy)
        self._press_until("ENTER", lambda: self.browser.hud.shown,
                          msg="wake key never summoned")
        # drop back to idle, then a remote Move (script-message path)
        # summons even though arrows aren't grabbed
        self._press_until("ESC", lambda: not self.browser.hud.shown,
                          msg="could not hide the HUD")
        self.handle.command("script-message", "mpvtk-hud-summon", "nav")
        self._wait(lambda: self.browser.hud.shown,
                   msg="remote summon path failed with grab off")

    def test_mouse_summon_leaves_the_arrows_to_mpv(self):
        """The HUD coming up under the pointer must not take the seek
        keys with it. hud_grab_keys off means the arrows stay mpv's
        whenever the user never asked for keyboard driving — and
        merely moving the mouse is not asking. The wake key still
        upgrades a HUD that is already up to keyboard driving."""
        self.ctl.key_opts = {"grab": False, "key": "ENTER"}
        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="never entered HUD-idle")

        # pointer movement raises the HUD, pointer-driven
        self.app.debug(cmd="phud", action="mousemove", x=200, y=200)
        self._wait(lambda: self._state().get("phud_shown"),
                   msg="pointer movement never summoned the HUD")
        st = self._state()
        self.assertFalse(st.get("phud_kbd"),
                         "a mouse summon must not grab the arrows")
        self.assertIsNone(st.get("nav"),
                          "no focus ring without keyboard driving")

        # The arrows pass through to mpv. What is asserted here is the
        # renderer's half — it never bound them — because this harness
        # spawns mpv with input_default_bindings=no (see _SPAWN_OPTS),
        # so mpv's own seek cannot fire to be observed; the real player
        # spawns with them on (player.py). It is a real discriminator
        # even so: with the arrows bound, RIGHT reaches nav_move, which
        # anchors focus onto the nearest node when nothing is focused —
        # exactly the bug, where a mouse summon left the HUD keyboard-
        # driven.
        self._keypress("RIGHT")
        time.sleep(0.5)
        self.assertIsNone(self._state().get("nav"),
                          "RIGHT moved HUD focus with grab off")

        # the wake key takes keyboard control of the HUD already up
        self._press_until("ENTER", lambda: self._state().get("phud_kbd"),
                          msg="wake key never took keyboard control")
        self.assertIsNotNone(self._state().get("nav"),
                             "taking control must land focus")

        # ...and hiding gives the arrows back. That is the escape
        # hatch: keyboard control lasts for the HUD that is up, not
        # forever, so it can't quietly keep the seek keys after the
        # HUD auto-hides.
        self._press_until("ESC", lambda: not self._state().get("phud_shown"),
                          msg="could not hide the HUD")
        self.assertFalse(self._state().get("phud_kbd"),
                         "keyboard control must not survive the hide")
        self.app.debug(cmd="phud", action="mousemove", x=210, y=210)
        self._wait(lambda: self._state().get("phud_shown"),
                   msg="pointer never re-summoned the HUD")
        self.assertFalse(self._state().get("phud_kbd"),
                         "re-summon under the pointer is mouse-driven")

    def test_grab_on_keeps_the_arrows_on_a_mouse_summon(self):
        """hud_grab_keys is the explicit opt-in, so it applies however
        the HUD got raised."""
        self.ctl.key_opts = {"grab": True, "key": "ENTER"}
        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="never entered HUD-idle")
        self.app.debug(cmd="phud", action="mousemove", x=200, y=200)
        self._wait(lambda: self._state().get("phud_shown"),
                   msg="pointer movement never summoned the HUD")
        self.assertTrue(self._state().get("phud_kbd"),
                        "grab opts into the arrows regardless of source")

    def test_idle_skip_overlay(self):
        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="never entered HUD-idle")

        # a skippable segment starts while idle: the standalone
        # renderer-drawn button auto-shows without summoning the HUD
        self.browser.on_playstate(dict(VIDEO_STATE,
                                       skip_label="Skip Intro"))
        self._wait(lambda: self._state().get("phud_skip"),
                   msg="skip overlay never auto-showed")
        st = self._state()
        self.assertFalse(st.get("phud_shown"),
                         "overlay must not summon the HUD")
        self.assertEqual(st.get("phud_intro"), "Skip Intro")

        # ENTER (what a remote Select arrives as) skips instead of
        # summoning
        self._press_until(
            "ENTER",
            lambda: ("hud_action", "skip-segment", None)
            in self.ctl.calls,
            msg="ENTER on the overlay never skipped")
        self._wait(lambda: not self._state().get("phud_skip"),
                   msg="overlay never dropped after the skip")

        # the fake controller doesn't actually skip, so the segment is
        # still live — pointer movement summons the full HUD, which
        # carries its own Skip button (the scene node)
        self.app.debug(cmd="phud", action="mousemove", x=200, y=200)
        self._wait(lambda: self.browser.hud.shown,
                   msg="mouse motion never summoned during a segment")
        self._wait(lambda: self.app.node_rect("hud-skip") is not None,
                   msg="the summoned HUD has no Skip button")

        # segment ends: label clears, pointer movement still summons
        self._press_until("ESC", lambda: not self.browser.hud.shown,
                          msg="could not hide the HUD again")
        self.browser.on_playstate(dict(VIDEO_STATE))
        self._wait(lambda: not self._state().get("phud_intro"),
                   msg="intro label never cleared")
        self.app.debug(cmd="phud", action="mousemove", x=300, y=300)
        self._wait(lambda: self.browser.hud.shown,
                   msg="mouse motion should summon once the segment "
                       "ended")

    def test_skip_overlay_spans_the_hud(self):
        """The Skip button must not blink out across a summon, and the
        segment window runs whether or not the bar is up."""
        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="never entered HUD-idle")
        self.browser.on_playstate(dict(VIDEO_STATE,
                                       skip_label="Skip Intro"))
        self._wait(lambda: self._state().get("phud_skip"),
                   msg="skip overlay never auto-showed")

        # summoning must not drop the window: the renderer's overlay
        # keeps drawing until the scene's own hud-skip node lands, so
        # some Skip button is on screen the whole way through
        self.app.debug(cmd="phud", action="mousemove", x=200, y=200)
        self._wait(lambda: self.browser.hud.shown,
                   msg="pointer movement never summoned")
        self.assertTrue(
            self._state().get("phud_skip"),
            "the segment window must survive a summon (it governs "
            "what happens once the bar auto-hides)")
        self._wait(lambda: self.app.node_rect("hud-skip") is not None,
                   msg="the summoned HUD has no Skip button")

        # ...and hiding the bar hands it straight back to the overlay
        self._press_until("ESC", lambda: not self.browser.hud.shown,
                          msg="could not hide the HUD")
        self.assertTrue(self._state().get("phud_skip"),
                        "the overlay must resume when the bar hides")

        # ENTER is the overlay's again now that the scene is gone
        self._press_until(
            "ENTER",
            lambda: ("hud_action", "skip-segment", None) in self.ctl.calls,
            msg="ENTER never skipped after the HUD hid")

    def test_skip_window_armed_while_the_hud_is_up(self):
        """A segment starting while the bar is already summoned arms the
        same window, so the offer outlives the bar's auto-hide."""
        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="never entered HUD-idle")
        self._press_until("LEFT", lambda: self.browser.hud.shown,
                          msg="never summoned the HUD")
        self.browser.on_playstate(dict(VIDEO_STATE,
                                       skip_label="Skip Intro"))
        self._wait(lambda: self._state().get("phud_skip"),
                   msg="a segment starting under the HUD never armed "
                       "the standalone window")
        self._press_until("ESC", lambda: not self.browser.hud.shown,
                          msg="could not hide the HUD")
        self.assertTrue(self._state().get("phud_skip"),
                        "the offer must survive the bar going away")

        # and the window expires on its own clock, leaving HUD-idle
        self._wait(lambda: not self._state().get("phud_skip"),
                   timeout=15, msg="overlay never auto-hid")
        self.assertTrue(self._state().get("phud_mode"))
        self.assertFalse(self.browser.hud.shown)

    def test_paused_video_keeps_hud_up(self):
        """hud_autohide "paused", end to end through hud_key_opts.

        #620 turned this from a rule into a mode: pausing used to hold the
        controls up for everybody, which is wrong if what you paused to look
        at is behind them. Asking for it still works, and the whole path --
        setting to gateway to engage message to renderer -- is what this
        covers; the modes themselves are pinned in tests/lua.
        """
        self.ctl.key_opts = dict(self.ctl.key_opts, hide=4, mode="paused")
        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="renderer never entered HUD-idle")
        self._set_pause(True)
        self._press_until("RIGHT", lambda: self.browser.hud.shown,
                          msg="summon failed")
        # Auto-hide re-arms instead of hiding while paused.
        time.sleep(5.5)
        self.assertTrue(self.browser.hud.shown,
                        "HUD auto-hid while the video was paused")
        self._set_pause(False)
        self._wait(lambda: not self.browser.hud.shown, timeout=10,
                   msg="HUD never auto-hid after unpausing")

    def test_the_default_hides_on_a_paused_video(self):
        """...and the default does not. The other half of the same change."""
        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="renderer never entered HUD-idle")
        self._set_pause(True)
        self._press_until("RIGHT", lambda: self.browser.hud.shown,
                          msg="summon failed")
        self._wait(lambda: not self.browser.hud.shown, timeout=10,
                   msg="the default mode kept the HUD up on a paused video")

    def _set_console(self, open_):
        """mpv-console's presence flag, as the console script itself sets it.

        Split by backend for the same reason ``_set_pause`` is: libmpv's
        command() stringifies its arguments, so a bool has to go through the
        property API to arrive as one -- and the renderer observes this as
        MPV_FORMAT_FLAG, where a string node simply reads as nil.
        """
        name = "user-data/mpv/console/open"
        if h.BACKEND == "jsonipc":
            self.handle.command("set_property", name, open_)
        else:
            # NOT handle._set_property: python-mpv sends every scalar as a
            # STRING, and a user-data node holding "yes" reads back as nil
            # under MPV_FORMAT_FLAG -- which is the format the renderer
            # observes it in, because that is what mpv's console writes. The
            # test then sets nothing the renderer can see and passes whatever
            # the code does.
            import ctypes
            from mpv import MpvFormat, _mpv_set_property
            flag = ctypes.c_int(1 if open_ else 0)
            _mpv_set_property(self.handle.handle, name.encode("utf-8"),
                              MpvFormat.FLAG, ctypes.byref(flag))
        time.sleep(0.4)

    def _real_click(self, node_id):
        """A press and a release through mpv's own input stack, at the
        node's real coordinates -- so the SECTION STACK decides who gets the
        button. ``app.debug(cmd="click")`` calls the handlers directly and
        would answer yes however the bindings were left."""
        node = next((n for n in (self.app._nodes or [])
                     if n.get("id") == node_id), None)
        self.assertIsNotNone(node, "%s is not in the pushed scene" % node_id)
        cx = int(node["x"] + node["w"] / 2)
        cy = int(node["y"] + node["h"] / 2)
        self.handle.command("mouse", cx, cy)
        time.sleep(0.3)
        self.handle.command("keydown", "MBTN_LEFT")
        time.sleep(0.2)
        self.handle.command("keyup", "MBTN_LEFT")

    def test_the_console_gives_back_the_hud_it_left_with(self):
        """The keyboard loan is a snapshot, and the lifecycle moves under it.

        mpv-console wants the keys our forced bindings outrank, so the
        renderer hands the three groups over for as long as it is up and
        takes them back on close. But the pointer can summon the HUD while
        the console is open -- and replaying "the idle summon surface was
        bound" onto a HUD that is now SHOWN re-takes mbtn_left for
        click-to-pause and replaces the wake key's upgrade-to-keyboard
        binding with a cold summon that is already a no-op. The bar is on
        screen and answers neither the mouse nor the keyboard.

        Only a real mpv can settle this: which binding wins is its section
        stack, which the Lua fake does not model.
        """
        self.ctl.key_opts = {"grab": False, "key": "ENTER",
                             # long enough that auto-hide cannot clear the
                             # wedge before the assertions run
                             "hide": 60, "mode": "hover"}
        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="never entered HUD-idle")

        self._set_console(True)
        self.app.debug(cmd="phud", action="mousemove", x=200, y=200)
        self._wait(lambda: self._state().get("phud_shown"),
                   msg="pointer movement never summoned the HUD")
        self._set_console(False)

        self._wait(lambda: any(n.get("id") == "hud-pp"
                               for n in (self.app._nodes or [])),
                   msg="the HUD scene never reached the renderer")
        self.ctl.calls = []
        self._real_click("hud-pp")
        self._wait(lambda: "toggle_pause" in self.ctl.calls,
                   msg="a click on play/pause never reached the button: %r"
                       % (self.ctl.calls,))

        # ...and the wake key still upgrades this HUD to keyboard driving
        # rather than trying to summon one that is already up.
        self._press_until("ENTER", lambda: self._state().get("phud_kbd"),
                          msg="the wake key stopped taking keyboard control")


if __name__ == "__main__":
    unittest.main()
