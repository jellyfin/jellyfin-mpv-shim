# Development

See the [README](../README.md) for user-facing documentation, and
[configuration.md](configuration.md) for the settings reference.

If you'd like to run the application without installing it, run `./run.py`.
The project is written entirely in Python 3. There are no closed-source
components in this project. It is fully hackable.

The project is dependent on `python-mpv`, `python-mpv-jsonipc`, `jellyfin-apiclient-python`,
`requests` and `Pillow`. The library browser, the playback HUD and the cast screen are all drawn
inside the player's own mpv window and rasterized with Pillow, which is why that one is required
rather than optional; no Tk, no webview and no second window are involved. If you are
using Windows and would like mpv to maximize properly, `pywin32` is also needed. The systray icon
uses `pystray` (`[systray]`) and is optional — without it the app still runs, it just cannot stay
alive in the background once its window closes.

This project is based Plex MPV Shim, which is based on https://github.com/wnielson/omplex, which
is available under the terms of the MIT License. The project was ported to python3, modified to
use mpv as the player, and updated to allow all features of the remote control api for video playback.

The Jellyfin API client comes from [Jellyfin for Kodi](https://github.com/jellyfin/jellyfin-kodi/tree/master/jellyfin_kodi).
The API client was originally forked for this project and is now a [separate package](https://github.com/iwalton3/jellyfin-apiclient-python).

The css file for desktop mirroring is from [jellyfin-chromecast](https://github.com/jellyfin/jellyfin-chromecast/tree/5194d2b9f0120e0eb8c7a81fe546cb9e92fcca2b) and is subject to GPL v2.0.

The shaders included in the shader pack are also available under verious open source licenses,
[which you can read about here](https://github.com/iwalton3/default-shader-pack/blob/master/LICENSE.md).

## Local Dev Installation

If you are on Windows there are additional dependencies. Please see the Windows Build Instructions.

1. Install the dependencies: `pip3 install --upgrade python-mpv jellyfin-apiclient-python pystray pillow python-mpv-jsonipc pypresence`.
    - If you run `./gen_pkg.sh --install`, it will also fetch these for you.
    - Note: Recent distributions make pip unusable by default. Consider using conda or add a virtualenv to your user's path.
2. Clone this repository: `git clone https://github.com/jellyfin/jellyfin-mpv-shim`
    - You can also download a zip build.
3. `cd` to the repository: `cd jellyfin-mpv-shim`
4. Run prepare script: `./gen_pkg.sh`
    - To do this manually, download the web client, shader pack, and build the language files.
5. Ensure you have a copy of `libmpv` or `mpv` available.
6. Install any platform-specific dependencies from the respective install tutorials.
7. You should now be able to run the program with `./run.py`. Installation is possible with `sudo pip3 install .`.
    - You can also install the package with `./gen_pkg.sh --install`.

## Translation

This project uses gettext for translation. The current template language file is `base.pot` in `jellyfin_mpv_shim/messages/`.

To regenerate `base.pot` and update an existing translation with new strings:

```bash
./regen_pot.sh
```

To compile all `*.po` files to `*.mo`:

```bash
./gen_pkg.sh --skip-build
```

## Testing

Two suites, deliberately separate.

**Fast suite** — stdlib `unittest`, no mpv, no display, no extra packages:

```bash
python3 -m unittest discover tests
```

**Integration matrix** — fake and real mpv, run once per backend in its own
process (`player.py` selects its backend at import time, so a subprocess is
the only clean way to get a pristine import per backend):

```bash
python3 tests/integration/run_integration.py           # full matrix
python3 tests/integration/run_integration.py --list    # what it will run
```

Real-mpv legs run under `xvfb-run` when it is available, both to keep ~25
windows off your desktop and because a real window manager may ignore the
requested geometry. See `tests/integration/README.md`.

### Coverage

No third-party dependency: `tools/coverage_report.py` uses `sys.monitoring`
(CPython 3.12+), the same mechanism modern coverage.py uses.

```bash
tools/coverage_all.sh                  # union across every measurable leg
tools/coverage_all.sh --sort=missing   # biggest gaps first
```

A driver script rather than one command because no single process sees
everything — each mpv backend needs its own interpreter, so the legs are run
separately and their results unioned. A plain
`tools/coverage_report.py` run measures only the fast suite and will badly
understate `player.py`.

To find what to test before touching a module:

```bash
tools/coverage_report.py --merge <dir>/merged.json --functions mpvtk_browser/ui.py
```

Line coverage only, not branch coverage — treat it as "was this reached at
all". The report flags the modules where that distinction matters most.

### Type checking

```bash
tools/mypy_gate.sh            # fails only on NEW findings
tools/mypy_gate.sh --update   # re-baseline after intentional changes
```

Baselined because the tree carries ~90 pre-existing findings from code that
predates any type checking. Sub-second, so it belongs beside the unit suite.

### Structural and characterization tests

Beyond the behavioural tests, four kinds of check exist to catch what review
and hand-testing miss:

- `tests/test_source_invariants.py` — whole-tree AST rules (an orphaned
  docstring, a shared mutable class attribute).
- `tests/test_scene_snapshots.py` — the exact node list `layout()` pushes to
  the renderer, pinned per screen. Because the UI is declarative, an identical
  node list means identical pixels. Regenerate an intended change with
  `python3 tests/test_scene_snapshots.py --update`, then review the diff.
- `tests/test_late_bound_calls.py` — every name resolved only at call time
  (lambda receivers, `self.controller.X`, `ROUTES` loader/renderer strings,
  `_start_daemon` slots) must exist and match the signature. This is the one
  that catches a forgotten call site after a method moves; mypy does not, see
  `docs/REFACTORING_METHOD.md` §1.4.
- `tests/integration/test_harness_isolation.py` — fails when the test harness
  drifts from `PlayerManager.__init__`.

`docs/REFACTORING_METHOD.md` explains how these are meant to be used together
during the decomposition described in `docs/ARCHITECTURE_TARGET.md`.


## Tracing the mpv window

The window is driven from four directions at once — the browser entering and
leaving browse mode, playback starting and stopping, the tray, and mpv's own
OSC — and until now the only way to see the interleaving was
`mpv_log_level: debug`, which buries the six interesting lines in thousands of
decoder ones.

The `window` logger records only the transitions, at INFO, with the caller
that asked for each one:

```
$ grep '^window:' ~/.config/jellyfin-mpv-shim/log.txt
window: CREATE mpv (first) <- jellyfin_mpv_shim.player.__init__:510
window: browse=on (alive=1 video=0 loading=0 mpvtk=1 bg=0) <- ...gateway.playback.on_browse_enter:23
window: browse=off (alive=1 video=0 loading=0 mpvtk=0 bg=1) <- ...gateway.playback.on_minimize:41
window: force_window=False <- ...player_window.set_browse_window:392
```

The caller is the point. Window state alone cannot tell the browser
re-entering browse mode apart from playback re-arming the window, and *which
one asked* is usually the whole finding — a window that re-opens on its own is
a caller you did not expect, not a state you did not expect.

Lines worth knowing:

- `CREATE mpv` — a whole new mpv. `(first)` at startup, `(re-open)` after a
  crash, an idle-quit or the user closing the window.
- `browse=on with no mpv: building a new one` — a window built from the
  *browse* path rather than the play path. Correct when the tray reopens the
  library; a red flag straight after a minimize.
- `force_window=False SUPPRESSED` — something tried to drop the window while
  the in-window UI owned it. Normal and frequent; it is the guard working.
- `releasing the window by QUITTING mpv` — only on mpv < 0.41, which decides
  about its window at startup and never revisits it.

Every read in the trace is a `getattr` with a default, deliberately: a
diagnostic that raises is worse than no diagnostic, and partially-built
players are real (test doubles, and `_init_mpv` runs before `__init__` has
finished).
