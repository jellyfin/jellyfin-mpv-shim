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

The shaders included in the shader pack are also available under verious open source licenses,
[which you can read about here](https://github.com/iwalton3/default-shader-pack/blob/master/LICENSE.md).

## Local Dev Installation

On Windows, see [Building on Windows](#building-on-windows).

1. Install the dependencies: `pip3 install --upgrade python-mpv jellyfin-apiclient-python pystray pillow python-mpv-jsonipc pypresence`.
    - If you run `./gen_pkg.sh --install`, it will also fetch these for you.
    - Note: Recent distributions make pip unusable by default. Consider using conda or add a virtualenv to your user's path.
2. Clone this repository: `git clone https://github.com/jellyfin/jellyfin-mpv-shim`
    - You can also download a zip build.
3. `cd` to the repository: `cd jellyfin-mpv-shim`
4. Run prepare script: `./gen_pkg.sh`
    - It downloads the shader pack and compiles the translations.
5. Ensure you have a copy of `libmpv` or `mpv` available.
6. Install any platform-specific dependencies from the respective install tutorials.
7. You should now be able to run the program with `./run.py`. Installation is possible with `sudo pip3 install .`.
    - You can also install the package with `./gen_pkg.sh --install`.

## Building on Windows

The releases already include Windows builds; you only need this to work on the client itself.
These steps mirror `.github/workflows/main.yml`, which produces the releases — **if the two
disagree, the workflow is right.**

Python and libmpv must be the same architecture. On an Arm machine an x64 Python installs and
builds without complaint and produces the wrong client; `tools/check_win_arch.py` checks this.

1. Install Git for Windows, then in Git Bash:
   `git clone https://github.com/jellyfin/jellyfin-mpv-shim; cd jellyfin-mpv-shim`.
2. Install [Python 3](https://www.python.org/downloads/) with PATH enabled (CI uses 3.14),
   [7-Zip](https://www.7-zip.org/) and [Inno Setup](https://jrsoftware.org/isinfo.php)
   (`winget install --id JRSoftware.InnoSetup -e -s winget`).
3. In `cmd`: `pip install wheel` then `pip install .[all] pywin32`.
4. Download the **`mpv-dev-*`** archive (not the player) from
   [shinchiro/mpv-winbuild-cmake](https://github.com/shinchiro/mpv-winbuild-cmake/releases) —
   preferably the release `main.yml` pins. Use `mpv-dev-x86_64-v3-*` for current CPUs, the plain
   `mpv-dev-x86_64-*` for older ones (the `LEGACY64` release), or `mpv-dev-aarch64-*` for ARM64.
   Extract `libmpv-2.dll` into the repository and **rename it to `mpv-2.dll`**.
5. Build the two DLLs libmpv and Pillow need beside it, which Windows does not provide:
   - `python tools/build_win_vulkan_loader.py` — `vulkan-1.dll`. libmpv hard-imports it, and
     without it the client dies at startup on machines whose GPU driver did not install one.
   - `pip install meson ninja` then `python tools/build_win_fribidi.py` — `fribidi-0.dll`.
     Without it, right-to-left text renders reversed and nothing is kerned.

   `tools/check_win_libmpv_deps.py --allow vulkan-1.dll mpv-2.dll` and
   `tools/check_win_raqm.py --require-bundled` verify them. See
   [packaging.md](packaging.md) section 1.
6. In Git Bash, build the PyInstaller bootloader from source:
   ```bash
   ./gen_pkg.sh --get-pyinstaller
   cd pyinstaller/bootloader && python ./waf distclean all && cd .. && pip install .
   cd ..
   ```
   A stock `pip install pyinstaller` also works, but its prebuilt bootloader is often flagged
   by antivirus software. On ARM64, add `--target-arch=64bit-arm --check-c-compiler=msvc` to
   the `waf` command, or it may silently build an x64 bootloader.
7. In Git Bash, run `./gen_pkg.sh --skip-build` (translations and shader pack). Without GNU
   `msgfmt` it falls back to `tools/msgfmt.py`, which produces the same catalogs.
8. In `cmd`, run `build-win.bat` (`build-win-arm64.bat` for ARM64, `build-win-dbg.bat` for a
   console-attached debug build). There is no 32-bit build.

## Translation

This project uses gettext for translation. The current template language file is `base.pot` in `jellyfin_mpv_shim/messages/`.

To regenerate `base.pot` after adding or changing user-facing strings:

```bash
./regen_pot.sh
```

That is all a pull request needs. If a word means two different things in
two places — a form label and a button verb, say — reach for
`i18n._p(context, string)` rather than `_()`, so translators can tell them
apart. The per-locale `.po` files are managed by
[Weblate](https://translate.jellyfin.org/projects/jellyfin/jellyfin-mpv-shim/),
which reads `base.pot` to discover new strings — please leave them alone.

Maintainers doing a translation sync can merge the template into every locale
with `./regen_pot.sh --merge`, folding in `master`'s translations first so
volunteer work is never lost (override the ref with `MASTER_REF=origin/master`).
That rewrites all 86 `.po` files, so it wants to be committed and merged
promptly or it goes stale against Weblate.

To compile all `*.po` files to `*.mo`:

```bash
./gen_pkg.sh --skip-build
```

## Testing

Two suites, deliberately separate.

**Unit suite** — stdlib `unittest`, no extra packages. Run it under `xvfb-run`: a few
modules import `player.py`, and importing it opens a real mpv window.

```bash
xvfb-run -a python3 -m unittest discover tests          # ~2 minutes
xvfb-run -a python3 tools/run_tests_parallel.py         # the same tests, one process per module
```

Select tests with `-k`, not by module name: a module name stays in `sys.argv`, which the app
parses at import time.

**Integration matrix** — fake and real mpv, run once per backend in its own
process (`player.py` selects its backend at import time, so a subprocess is
the only clean way to get a pristine import per backend):

```bash
xvfb-run -a python3 tests/integration/run_integration.py           # full matrix, ~10 minutes
python3 tests/integration/run_integration.py --list                # what it will run
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
  `docs/archive/REFACTORING_METHOD.md` §1.4.
- `tests/integration/test_harness_isolation.py` — fails when the test harness
  drifts from `PlayerManager.__init__`.

`docs/archive/REFACTORING_METHOD.md` explains how these are meant to be used together
during the decomposition described in `docs/archive/ARCHITECTURE_TARGET.md`.


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
