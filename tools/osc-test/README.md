# OSC test harness

Headless test rig for the shim's OSC lua scripts
(`trickplay-jf-osc.lua`, `trickplay-osc.lua`, `thumbfast.lua`). It runs
the real `mpv` binary under Xvfb, injects **real X11 input** with
xdotool (synthetic `mouse`/`keypress` mpv commands do *not* traverse the
same input path — they miss section/mouse-area routing entirely), and
asserts player state over the JSON IPC socket.

Because it drives the external mpv binary via `--script` +
`--input-ipc-server`, the lua behavior it validates is exactly what the
`mpv_ext` (jsonipc) backend runs; libmpv loads the same scripts into the
same player core. The Python↔lua integration across both shim backends
is covered separately by `tests/integration/test_realmpv_smoke.py` and
`tests/integration/test_jf_osc_script.py`.

Requires: `mpv`, `Xvfb`, `xdotool`, `socat`, `python3`.

```bash
# screenshot the OSC with representative fake Jellyfin state
tools/osc-test/osc-test.sh shot out.png            # bare player bar
tools/osc-test/osc-test.sh shot out.png settings   # with the gear sheet open

# click routing: video-click pause, buttons, dead space
tools/osc-test/osc-test.sh clicks

# idle -> new file cycle, then clicks (regression: mpv raises an input
# section to the top of the stack every time it is re-enabled, which
# once left click-to-pause shadowing all the buttons after a restart)
tools/osc-test/osc-test.sh restart

# trickplay overlay churn while hovering the seekbar paused
# (regression: the volumebar — a second slider — used to clear the
# preview the seekbar had just requested, flickering on every tick)
tools/osc-test/osc-test.sh flicker
```

Useful knobs:

- `OSC_SCRIPT=... ` — test a different OSC script.
- `KEEP_LOG=1` — keep and print the mpv log path (the OSC logs every
  input event it processes when `JMS_JF_OSC_DEBUG=1`, which the harness
  sets; `thumbfast.lua` logs overlay add/dedup/clear decisions too).
- `DISPLAY_NUM=:99` — pin the Xvfb display if :77 is taken.

Caveat learned the hard way: under Xvfb/XTEST, mpv can deliver a quick
click's *release* to lua scripts noticeably late (the OSC now judges
releases by the press position, so actions still land). If a click test
behaves strangely, check the event timeline in the log before blaming
the script.
