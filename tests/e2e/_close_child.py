"""Child process for the mpv-close scenarios. Run by `test_mpv_reopen`.

These run out-of-process because **one of the outcomes under test is a
segmentation fault**, which no amount of `assertRaises` survives: closing mpv
mid-playback has `finished_callback` issue `self._player.command("stop")` on
the same libmpv handle that `_terminate_mpv` is concurrently destroying, and a
use-after-free takes the interpreter with it. A child lets the parent report
that as a normal failure with a useful message instead of losing the run.

Same reasoning as `tests/integration/_idle_reopen_child.py`.

Usage:  _close_child.py <mode>
Modes:  reopen | abandon-long | abort-report-long | abort-report-short
Prints: `RESULT: <PASS|FAIL> <detail>` and exits 0 on PASS.
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import _e2e  # noqa: E402


#: The child's player, so `verdict` can stop it before the interpreter goes.
_PM = None


def verdict(ok, detail):
    """Report, stop playback, exit.

    The stop is not tidiness. Leaving a file decoding means the atexit
    teardown destroys the libmpv handle while it is still running, and that
    races the same way `finished_callback` did — a SIGSEGV *after* the verdict
    was printed, which surfaces as "the child died on signal 11" on a run
    whose assertions all passed. Roughly one run in four before this.
    The real app stops before it terminates; so does this.
    """
    if _PM is not None:
        try:
            _PM.stop()
        except Exception:
            pass
    print("RESULT: %s %s" % ("PASS" if ok else "FAIL", detail), flush=True)
    sys.exit(0 if ok else 1)


def close_mpv(pm):
    """Quit mpv out from under the shim, as closing the window does.

    mpv's default CLOSE_WIN binding is `quit`, so this is the event the shim
    sees when a user clicks the X — deliberately not `pm.stop()`, which is the
    orderly path these bugs do not live on. The command may fail: the socket
    can die between accepting and answering, which is the condition under
    test, not an error.
    """
    try:
        pm._player.command("quit")
    except Exception:
        pass
    _e2e.pump_until(pm, lambda: pm._video is None or not pm._mpv_alive,
                    timeout=20)


def main():
    global _PM
    mode = sys.argv[1]
    _e2e.isolate_config()
    player_module = _e2e.ensure_real_player()
    pm = _PM = player_module.playerManager
    pm.action_trigger = threading.Event()
    pm.timeline_trigger = threading.Event()

    session = _e2e.Session()

    if mode == "reopen":
        eps = session.episodes("Date Based Show", season=2019)
        ids = [e["Id"] for e in eps[:2]]
        session.reset_played(*ids)
        try:
            # 1) Playing normally.
            pm.play(_e2e.build_media(session, ids).video, is_initial_play=True)
            if not pm._player.duration:
                verdict(False, "no duration before the close")
            _e2e.pump_until(
                pm, lambda: (pm._player.playback_time or 0) >= 1.0, timeout=20)

            # 2) The window goes away mid-file.
            close_mpv(pm)

            # 3) "Cast again" — must build a fresh mpv, not talk to the dead one.
            pm.play(_e2e.build_media(session, ids).video, is_initial_play=True)
            if not (pm._player.duration and pm._player.duration > 0):
                verdict(False, "the re-opened mpv never reported a duration")
            if pm._video is None or pm._video.item_id != ids[0]:
                verdict(False, "wrong video after the re-open")

            # 4) What the bug was actually about: EOF still advances.
            advanced = _e2e.pump_until(
                pm, lambda: pm._video is not None
                and pm._video.item_id == ids[1], timeout=45)
            if not advanced:
                verdict(False, "auto-advance is dead after close/re-open")
            played = _e2e.wait_for(
                lambda: session.user_data(ids[0]).get("Played"), timeout=20)
            if not played:
                verdict(False, "the episode that played out was not marked "
                               "watched on the server")
            verdict(True, "re-opened and auto-advanced")
        finally:
            session.reset_played(*ids)

    elif mode in ("abort-report-long", "abort-report-short"):
        # Drive the abort-report path directly. The realistic trigger is a
        # window close, but that is a race between finished_callback and the
        # shutdown teardown — either can win, so a test built on it passes
        # about a third of the time. send_timeline_stopped(finished=True) is
        # the seam the close path reaches, minus the race.
        if mode == "abort-report-long":
            item = session.find("Three hours", library="Test Media")
        else:
            item = session.episodes("Double Episode Show", season=1)[0]
        item_id = item["Id"]
        runtime = (item.get("RunTimeTicks") or 0) / 1e7
        session.reset_played(item_id)
        try:
            pm.play(_e2e.build_media(session, [item_id]).video,
                    is_initial_play=True)
            if not pm._player.duration:
                verdict(False, "no duration")
            if not _e2e.pump_until(
                    pm, lambda: (pm._player.playback_time or 0) >= 2.5,
                    timeout=25):
                verdict(False, "never reached the 2.5s mark")
            position = pm._player.playback_time
            pm.send_timeline_stopped(True)
            _e2e.wait_for(lambda: session.user_data(item_id).get("PlayCount"),
                          timeout=10)
            time.sleep(1.0)
            played = session.user_data(item_id).get("Played")
            verdict(not played, "runtime=%.1fs aborted_at=%.1fs played=%s"
                    % (runtime, position or -1, played))
        finally:
            session.reset_played(item_id)

    elif mode == "abandon-long":
        item = session.find("Three hours", library="Test Media")
        item_id = item["Id"]
        runtime = (item.get("RunTimeTicks") or 0) / 1e7
        session.reset_played(item_id)
        try:
            pm.play(_e2e.build_media(session, [item_id]).video,
                    is_initial_play=True)
            if not pm._player.duration:
                verdict(False, "no duration")
            got = _e2e.pump_until(
                pm, lambda: (pm._player.playback_time or 0) >= 2.5, timeout=25)
            if not got:
                verdict(False, "never reached the 2.5s mark")
            position = pm._player.playback_time

            close_mpv(pm)

            # Let the stop report land before believing the answer.
            _e2e.wait_for(lambda: session.user_data(item_id).get("PlayCount"),
                          timeout=10)
            time.sleep(1.0)
            played = session.user_data(item_id).get("Played")
            detail = ("runtime=%.1fs abandoned_at=%.1fs played=%s"
                      % (runtime, position or -1, played))
            verdict(not played, detail)
        finally:
            session.reset_played(item_id)

    else:
        verdict(False, "unknown mode %r" % mode)


if __name__ == "__main__":
    main()
