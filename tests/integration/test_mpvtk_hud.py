"""Phase 9.0 exit test: the playback-HUD lifecycle over REAL video.

Proves the attached-but-idle inversion end to end on a live mpv, per
backend: yielding to video with ``osc_style: mpvtk`` puts the renderer
in HUD-idle (blank scene, summon bindings only), an arrow keypress
summons the HUD (full input sections + the browser's HUD scene, focus
landing on play/pause), ENTER activates the focused transport button,
ESC hides it, ~4s of inactivity auto-hides it, and stopping playback
drops HUD mode entirely as browse resumes. No player.py, no server —
a fake controller records the transport calls.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import _harness as h  # noqa: E402
from test_mpvtk_browser import _make_source, _spawn_handle  # noqa: E402


class FakeController:
    """Records transport calls; opts into the HUD like the real
    _PlayerController does when osc_style is "mpvtk"."""

    def __init__(self):
        self.calls = []
        self.trickplay_meta = None
        self.menu_state = None
        self.chapter_list = []
        # tests drive summons with arrow keypresses, so opt into the
        # grab (the no-grab default has its own test)
        self.key_opts = {"grab": True, "key": "ENTER"}

    def hud_key_opts(self):
        return dict(self.key_opts)

    def trickplay(self):
        return self.trickplay_meta

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
        self._press_until("LEFT", lambda: self.browser._hud_shown,
                          msg="summon never reached the browser")
        self._wait(lambda: self._state().get("nav") == "hud-seek",
                   msg="focus did not land on the seek bar: %r"
                   % self._state().get("nav"))
        st = self._state()
        self.assertTrue(st.get("phud_shown"))
        self.assertTrue(st.get("active"))
        self.assertIn("refresh", self.ctl.calls)

        # --- DOWN steps off the bar into the transport row (spatial
        # nav picks the nearest control below); LEFT walks to
        # play/pause and ENTER activates it
        self._press_until(
            "DOWN", lambda: self._state().get("nav")
            not in (None, "hud-seek"),
            msg="DOWN never moved focus off the seek bar")
        self._press_until(
            "LEFT", lambda: self._state().get("nav") == "hud-pp",
            msg="LEFT never reached play/pause: %r"
            % self._state().get("nav"))
        self._press_until(
            "ENTER", lambda: "toggle_pause" in self.ctl.calls,
            msg="ENTER on play/pause never reached the controller")

        # --- ESC hides the HUD (back to idle, still summonable)
        self._press_until("ESC", lambda: not self.browser._hud_shown,
                          msg="ESC did not hide the HUD")
        st = self._state()
        self.assertTrue(st.get("phud_mode"))
        self.assertFalse(st.get("phud_shown"))
        self.assertFalse(st.get("active"))

        # --- summonable again; auto-hides after ~4s without input
        self._press_until("UP", lambda: self.browser._hud_shown,
                          msg="second summon failed")
        self._wait(lambda: not self.browser._hud_shown, timeout=8,
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
        # the TrickPlay worker writes to raw_images.bin).
        tw, th, count = 64, 36, 10
        raw = os.path.join(self.tmp, "raw_images.bin")
        with open(raw, "wb") as fh:
            for i in range(count):
                px = bytes((20 * i % 256, 128, 255 - 20 * i % 256, 255))
                fh.write(px * (tw * th))
        self.ctl.trickplay_meta = {
            "count": count, "multiplier": 3000,
            "width": tw, "height": th, "file": raw,
        }

        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="renderer never entered HUD-idle")
        self._press_until("LEFT", lambda: self.browser._hud_shown,
                          msg="summon failed")
        self._wait(lambda: self._state().get("nav") == "hud-seek",
                   msg="focus never landed on the seek bar")

        # The bar wakes already in adjust mode: LEFT scrubs 5% back —
        # a 'change' that must NOT seek, only pause, set the pending
        # scrub target, and float the preview thumbnail.
        self._press_until(
            "LEFT", lambda: self.browser._hud_scrub is not None,
            msg="adjust-mode scrub never reached the browser")
        seeks = [c for c in self.ctl.calls if isinstance(c, tuple)
                 and c[0] == "seek"]
        self.assertEqual(seeks, [], "scrubbing must not seek mid-gesture")
        self.assertIn(("set_paused", (True,)), [
            c for c in self.ctl.calls if isinstance(c, tuple)],
            "scrub start must pause playback")
        self._wait(lambda: self.app.node_rect("hud-preview") is not None,
                   msg="trickplay preview never appeared")

        # ENTER commits: exactly one seek at the scrubbed position.
        target = self.browser._hud_scrub
        self._keypress("ENTER")
        self._wait(lambda: any(isinstance(c, tuple) and c[0] == "seek"
                               for c in self.ctl.calls),
                   msg="commit never seeked")
        seeks = [c for c in self.ctl.calls if isinstance(c, tuple)
                 and c[0] == "seek"]
        self.assertEqual(len(seeks), 1)
        self.assertAlmostEqual(seeks[0][1], target, delta=2.0)
        self.assertIsNone(self.browser._hud_scrub)

        # Second gesture, abandoned with ESC: no new seek, preview
        # cleared, HUD still up. (The always-adjust bar is still live
        # after the commit — no arming press needed.)
        self._press_until(
            "LEFT", lambda: self.browser._hud_scrub is not None,
            msg="second scrub never started")
        # single press: the ESC binding has been stable since summon (no
        # rebind race), and a second ESC would hide the whole HUD
        self._keypress("ESC")
        self._wait(lambda: self.browser._hud_scrub is None,
                   msg="ESC never cancelled the scrub")
        self.assertTrue(self.browser._hud_shown,
                        "cancelling a scrub must not hide the HUD")
        seeks = [c for c in self.ctl.calls if isinstance(c, tuple)
                 and c[0] == "seek"]
        self.assertEqual(len(seeks), 1, "cancel must not seek")
        self._wait(lambda: self.app.node_rect("hud-preview") is None,
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
        self._press_until("LEFT", lambda: self.browser._hud_shown,
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
        self.app.debug(cmd="click", id="hud-skip")
        self._wait(lambda: ("hud_action", "skip-segment", None)
                   in self.ctl.calls,
                   msg="skip button never dispatched: %r" % self.ctl.calls)

    def test_seek_hover_bubble(self):
        self.ctl.chapter_list = [{"title": "Opening", "time": 0.0},
                                 {"title": "Late", "time": 20.0}]
        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="never entered HUD-idle")
        self._press_until("LEFT", lambda: self.browser._hud_shown,
                          msg="summon failed")
        self._wait(lambda: self.app.node_rect("hud-seek") is not None,
                   msg="seek bar never materialized")
        # park the pointer on the middle of the seek bar: throttled
        # hover events flow to the browser, which floats the bubble
        self.app.debug(cmd="hover", id="hud-seek")
        self._wait(lambda: self.browser._hud_hover is not None,
                   msg="hover position never reached the browser")
        self.assertAlmostEqual(self.browser._hud_hover, 15.0, delta=3.0)
        self._wait(lambda: self.app.node_rect("hud-preview") is not None,
                   msg="hover bubble never appeared")
        # moving off the bar retracts it
        self.app.debug(cmd="hover", id="hud-pp")
        self._wait(lambda: self.browser._hud_hover is None,
                   msg="hover_end never reached the browser")
        self._wait(lambda: self.app.node_rect("hud-preview") is None,
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
        self._press_until("LEFT", lambda: self.browser._hud_shown,
                          msg="summon failed")
        self._wait(lambda: self.app.node_rect("hud-settings") is not None,
                   msg="gear button never materialized")
        self.app.debug(cmd="click", id="hud-settings")
        self._wait(lambda: self.browser._hud_menu == "root",
                   msg="gear click never opened the settings menu")
        self._wait(lambda: self._state().get("menu_open"),
                   msg="menu never reached the renderer")
        # DOWN highlights row index 1 (menu nav starts un-highlighted,
        # so the first DOWN lands past row 0) = Playback Speed; ENTER
        # swaps in its submenu
        self._keypress("DOWN")
        self._press_until(
            "ENTER", lambda: self.browser._hud_menu == "speed",
            msg="menu selection never opened the speed submenu")
        # ESC steps back out of the menu without hiding the HUD
        self._wait(lambda: self._state().get("menu_open"),
                   msg="submenu never reached the renderer")
        self._keypress("ESC")
        self._wait(lambda: self.browser._hud_menu is None,
                   msg="ESC never dismissed the menu")
        self.assertTrue(self.browser._hud_shown,
                        "dismissing the menu must not hide the HUD")

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
        self.assertFalse(self.browser._hud_shown,
                         "LEFT must not summon with grab off")
        # the wake key still summons (it also pause-toggles; not
        # asserted here — a retried press would make the count racy)
        self._press_until("ENTER", lambda: self.browser._hud_shown,
                          msg="wake key never summoned")
        # drop back to idle, then a remote Move (script-message path)
        # summons even though arrows aren't grabbed
        self._press_until("ESC", lambda: not self.browser._hud_shown,
                          msg="could not hide the HUD")
        self.handle.command("script-message", "mpvtk-hud-summon", "nav")
        self._wait(lambda: self.browser._hud_shown,
                   msg="remote summon path failed with grab off")

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
        # still live: pointer movement re-shows the button, not the
        # whole HUD. (A retried ENTER may have summoned after the
        # overlay hid — drop back to idle first.)
        if self.browser._hud_shown:
            self._press_until("ESC",
                              lambda: not self.browser._hud_shown,
                              msg="could not hide the HUD again")
        self.app.debug(cmd="phud", action="mousemove", x=200, y=200)
        self._wait(lambda: self._state().get("phud_skip"),
                   msg="mouse motion never re-showed the overlay")
        self.assertFalse(self._state().get("phud_shown"))

        # ~6s without input hides it again; HUD stays idle
        self._wait(lambda: not self._state().get("phud_skip"),
                   timeout=10, msg="overlay never auto-hid")
        self.assertTrue(self._state().get("phud_mode"))
        self.assertFalse(self.browser._hud_shown)

        # segment ends: label clears, and pointer movement goes back to
        # summoning the full HUD
        self.browser.on_playstate(dict(VIDEO_STATE))
        self._wait(lambda: not self._state().get("phud_intro"),
                   msg="intro label never cleared")
        self.app.debug(cmd="phud", action="mousemove", x=300, y=300)
        self._wait(lambda: self.browser._hud_shown,
                   msg="mouse motion should summon once the segment "
                       "ended")

    def test_paused_video_keeps_hud_up(self):
        self._play_video()
        self._wait(lambda: self._state().get("phud_mode"),
                   msg="renderer never entered HUD-idle")
        self._set_pause(True)
        self._press_until("RIGHT", lambda: self.browser._hud_shown,
                          msg="summon failed")
        # Auto-hide re-arms instead of hiding while paused.
        time.sleep(5.5)
        self.assertTrue(self.browser._hud_shown,
                        "HUD auto-hid while the video was paused")
        self._set_pause(False)
        self._wait(lambda: not self.browser._hud_shown, timeout=10,
                   msg="HUD never auto-hid after unpausing")


if __name__ == "__main__":
    unittest.main()
