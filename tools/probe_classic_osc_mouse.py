#!/usr/bin/env python3
"""Who owns the mouse buttons while a classic OSC is on screen? (#724)

A manual probe against a REAL mpv, in the shape of `probe_hover_strand.py`:
it reproduces a state and reports what mpv thinks, rather than asserting
anything. Not a test -- it needs a display and an mpv binary, and what it
answers is a question about mpv's input layer, not about our control flow.

**The question.** With ``osc_style`` set to "mpv" or "default", the reporter
says neither mouse button pauses. renderer.lua's ``mpvtk_mouse`` section
claims ``mbtn_left``/``mbtn_right`` and sets no mouse area, so mpv treats it
as covering the whole screen (input.c ``get_bind_section``:
``{INT_MIN..INT_MAX}``) and prefers it over the builtin binding
(``get_cmd_from_keys``). Both of the renderer's bare-video fall-throughs
gate on ``state.phud.mode``, which classic-OSC modality never enters -- so
while that section is enabled, both buttons are swallowed and do nothing.

That is the mechanism. What was never established is whether the section is
still enabled at the point the user is complaining about, and this probe
splits that in half:

  A. does telling the renderer ``mpvtk-active no`` actually release the
     sections? (this file);
  B. does the app send it, in classic-OSC modality? (a Python question,
     answerable against the fake mpv -- see tests/test_classic_osc_mouse.py)

If A says the renderer releases them, the renderer is not the bug and B is
where to look. If A says it does not, stop: the fix is in renderer.lua.

**Reading the output.** ``input-bindings`` reports a ``priority`` per
binding, and ``-1`` means the owning section is not in ``active_sections``
at all (input.c ``mp_input_get_bindings``) -- i.e. disabled. So a
``mpvtk_mouse`` row at priority -1 is a released section, and one at >= 0 is
a live claim that outranks mpv's own default.

Usage:

    xvfb-run -a python3 tools/probe_classic_osc_mouse.py
    JMS_TEST_BACKEND=jsonipc xvfb-run -a python3 tools/probe_classic_osc_mouse.py
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MOUSE_KEYS = ("MBTN_LEFT", "MBTN_RIGHT", "MBTN_LEFT_DBL", "MBTN_MID",
              "MBTN_BACK", "MBTN_FORWARD", "WHEEL_UP", "WHEEL_DOWN")


def _bindings(handle, keys=MOUSE_KEYS):
    """``{key: [(section, priority, is_weak, cmd), ...]}`` for ``keys``."""
    # The property ACCESSOR, not `command("get_property", ...)`: libmpv
    # rejects the command form of a node-returning property with
    # MPV_ERROR_PROPERTY_FORMAT. This read used to try the command first and
    # fall through, which worked and hid the fact.
    rows = handle.input_bindings
    out = {}
    for row in rows or ():
        key = (row.get("key") or "").upper()
        if key not in keys:
            continue
        out.setdefault(key, []).append((
            row.get("section") or "", int(row.get("priority", -1)),
            bool(row.get("is_weak")), (row.get("cmd") or "")[:40]))
    for entries in out.values():
        entries.sort(key=lambda e: -e[1])
    return out


def _report(label, handle):
    print("\n=== %s ===" % label)
    table = _bindings(handle)
    for key in MOUSE_KEYS:
        entries = table.get(key)
        if not entries:
            print("  %-16s (nothing bound)" % key)
            continue
        winner = entries[0]
        for section, priority, weak, cmd in entries:
            mark = "*" if (section, priority) == winner[:2] and priority >= 0 \
                else " "
            print("  %s %-16s %-18s prio=%-4d weak=%-5s %s"
                  % (mark, key, section or "(default)", priority, weak, cmd))
    # `input-builtin-dragging` is mpv 0.39+. The renderer turns it OFF while
    # it owns the pointer (its own sections would refuse every VO drag
    # otherwise) and hands it back in `ui_suspend`; that hand-back is the
    # classic-OSC half of #726 -- dragging the video to move the window is
    # what mpv does everywhere else, and it is only ours to take while our
    # UI is up.
    try:
        drag = handle.input_builtin_dragging
    except Exception:
        try:
            drag = handle.command("get_property", "input-builtin-dragging")
        except Exception:
            drag = "(unsupported by this mpv)"
    print("  input-builtin-dragging: %s" % (drag,))


def _spawn_handle():
    """A raw mpv handle with the options the PLAYER uses, not the toolkit
    demo's: builtin bindings ON (that is what puts `cycle pause` and friends
    in the default section) and the stock OSC replaced, which is what
    ``osc_style`` "mpv" does."""
    opts = {
        "idle": "yes",
        "force_window": "yes",
        "osc": "no",                    # REPLACES_OSC: the shim's own OSC
        "input_default_bindings": "yes",
        "config": "no",
        "keepaspect_window": "no",
        "title": "jms classic-osc mouse probe",
    }
    if os.environ.get("JMS_TEST_BACKEND") == "jsonipc":
        import python_mpv_jsonipc
        return python_mpv_jsonipc.MPV(start_mpv=True, **opts), True
    import mpv as libmpv
    return libmpv.MPV(**{k.replace("_", "-"): v for k, v in opts.items()}), False


def main():
    from jellyfin_mpv_shim.mpvtk.app import MpvtkApp

    handle, ext = _spawn_handle()

    # The OTHER claimant, and the one the probe originally forgot: in
    # classic-OSC modality the shim also loads its patched stock OSC, whose
    # `input` section binds mbtn_left/mbtn_right and is enabled at LOAD
    # (trickplay-osc.lua: `mp.enable_key_bindings("input")` with
    # `state.input_enabled = true`), before any layout has decided the OSC
    # is invisible. Leaving it out measures a configuration nobody runs.
    if "--no-osc" not in sys.argv:
        osc = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "jellyfin_mpv_shim",
            "trickplay-osc.lua")
        handle.command("load-script", osc)
        time.sleep(0.5)
        _report("stock OSC loaded, no renderer yet", handle)

    app = MpvtkApp.attach(handle, ext=ext)
    thread = threading.Thread(target=lambda: app.run(lambda b: []), daemon=True)
    thread.start()
    try:
        app.ready.wait(10)
        time.sleep(0.5)          # let the renderer install its sections
        _report("renderer attached and ACTIVE (browse)", handle)

        # What `_yield()` does in classic-OSC modality: the HUD is not
        # available, so the renderer is detached outright rather than left
        # attached-but-idle.
        app.set_active(False)
        time.sleep(0.5)
        _report("after `mpvtk-active no` (classic-OSC playback)", handle)

        app.set_active(True)
        time.sleep(0.5)
        _report("back to browse", handle)
    finally:
        try:
            app.quit()
            thread.join(timeout=5)
        except Exception:
            pass
        try:
            handle.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
