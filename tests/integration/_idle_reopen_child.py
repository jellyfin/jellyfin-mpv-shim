"""Child process for the idle-quit → re-open end-to-end EOF check.

Runs in its OWN interpreter so a regression that wedged eof-reached on the
shared player singleton (permanent for the process) couldn't take down every
real-mpv test that runs after it. Drives:

  play → stop → idle_quit() → play (re-open) → stop
  → play a clip WITH a next item → pump for EOF auto-advance.

Prints exactly one line — ``ADVANCED`` (eof fired, queue advanced) or
``STALLED`` (eof never fired on the re-opened player) — and exits 0/1. After the
idle-quit fix (012961c), both backends ADVANCE: on libmpv idle_quit() no-ops
(in-process can't be re-created), on jsonipc it re-opens a fresh mpv process.

Backend via JMS_TEST_BACKEND; display inherited from the parent's xvfb.
"""

import os
import sys
import tempfile
import threading
import time

sys.argv = [sys.argv[0]]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import _harness as h  # noqa: E402
from unittest import mock  # noqa: E402
import tests.integration.test_realmpv_smoke as T  # noqa: E402


def main():
    pm_mod = T._import_real_player()
    pm = pm_mod.playerManager
    pm.action_trigger = threading.Event()
    pm.timeline_trigger = threading.Event()

    tmp = tempfile.mkdtemp(prefix="jms-idle-child-")
    clip1 = h.make_test_clip(os.path.join(tmp, "a.mp4"), duration=2)
    clip2 = h.make_test_clip(os.path.join(tmp, "b.mp4"), duration=2)
    client = T.FakeClient()

    def pump(pred, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            pm.update()
            if pred():
                return True
            time.sleep(0.05)
        pm.update()
        return pred()

    with mock.patch.object(pm_mod.settings, "mpv_idle_quit", True), \
            mock.patch.object(pm_mod.settings, "mpv_idle_quit_secs", 0), \
            mock.patch.object(pm_mod.settings, "auto_play", True):
        # Idle-quit then re-open on the very next play, with no settle — the
        # realistic path when a cast Play lands right after the idle timeout.
        v = T.RealVideo(clip1, client, item_id="idle")
        pm.play(v, is_initial_play=True)
        pm.stop()
        pm.idle_quit()
        v2 = T.RealVideo(clip1, client, item_id="idle-reopen")
        pm.play(v2, is_initial_play=True)
        pm.stop()

        # Now a normal queued playback on the re-opened player: EOF must fire
        # and auto-advance to the next clip.
        second = T.RealVideo(clip2, client, item_id="second")
        first = T.RealVideo(clip1, client, item_id="first", next_video=second)
        pm.play(first, is_initial_play=True)
        advanced = pump(lambda: pm._video is second, timeout=20)

    sys.stdout.write("ADVANCED\n" if advanced else "STALLED\n")
    sys.stdout.flush()
    try:
        pm.terminate()
    except Exception:
        pass
    return 0 if advanced else 1


if __name__ == "__main__":
    sys.exit(main())
