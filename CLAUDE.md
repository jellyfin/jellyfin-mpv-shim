# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

- Run from source (no install): `./run.py`
- Prepare build artifacts (downloads `default-shader-pack`, compiles `.po` → `.mo`, validates that versions match across `constants.py`, the Inno Setup script, and the appdata XML, then runs `python3 -m build` to produce sdist + wheel): `./gen_pkg.sh`
  - `--skip-build` does prep only (translations + shader pack), no sdist/wheel. Use this on Windows or when you only want `.mo` files.
  - `--install` runs `pip3 install .[all]` (with `sudo` if available; add `--local` to skip sudo).
  - `--get-pyinstaller` / `--gen-fingerprint` are CI helpers for the Windows build cache.
- Regenerate translation template and merge into existing `.po` files (also folds in `master`'s translations so volunteer work isn't lost on a feature branch): `./regen_pot.sh`
- Windows build (after `gen_pkg.sh --skip-build`): `build-win.bat` (`build-win-32.bat` for 32-bit, `build-win-dbg.bat` for debug). Installer is built with Inno Setup from `Jellyfin MPV Shim.iss`.
- Run the test suite (stdlib unittest, no extra deps): `python3 -m unittest discover tests`. It covers pure-logic pieces (credential cleaning, SyncPlay teardown, wait_property, queue inserts, menu indexing); playback/server behavior still needs hand testing against a real server.
- There is no linter config.

The Python build uses PEP 517 / pyproject.toml with `setuptools` as the backend. The full build path requires the `build` package (`pip install build`); `pip install .[all]` and `pip install -e .` both work without it.

`gen_pkg.sh` also fetches `jellyfin_mpv_shim/default_shader_pack/` from the [`iwalton3/default-shader-pack`](https://github.com/iwalton3/default-shader-pack) GitHub release; that directory is not in git (see `.gitignore`).

## Bumping the version

`jellyfin_mpv_shim/constants.py:CLIENT_VERSION` is the single source of truth for the Python package — `pyproject.toml` reads it via `tool.setuptools.dynamic`. The Inno Setup and Flatpak appdata files are not derived and must be kept in sync manually; `gen_pkg.sh` will warn loudly if they drift. Update all three:
- `jellyfin_mpv_shim/constants.py` → `CLIENT_VERSION`
- `Jellyfin MPV Shim.iss` → `#define MyAppVersion`
- `jellyfin_mpv_shim/integration/com.github.iwalton3.jellyfin-mpv-shim.appdata.xml` → first `<release version="...">`

## Architecture

Entry point is `jellyfin_mpv_shim/mpv_shim.py:main` (invoked via the `jellyfin-mpv-shim` console script or `run.py`). It wires together a set of **module-level singletons** that talk to each other via direct references and `threading.Event` triggers — there is no DI container, no event bus, just imports.

The core singletons (each is a module-level instance, not a class you should instantiate):

- `clientManager` (`clients.py`) — owns one `JellyfinClient` per logged-in server, persists creds to `cred.json`, runs a periodic health-check thread, and forwards websocket events via `clientManager.callback`.
- `eventHandler` (`event_handler.py`) — receives those websocket events and dispatches to `playerManager`. New remote-control events are added by decorating a method with `@bind("EventName")`.
- `playerManager` (`player.py`) — wraps MPV (libmpv or external via JSON IPC; see below), owns the current playlist (a `Media` from `media.py`), and exposes the operations the rest of the app calls (`play`, `seek`, `set_streams`, `menu_action`, …). This module is the largest and most central.
- `timelineManager` (`timeline.py`) — background thread that periodically posts playback progress to Jellyfin and fires the `idle_cmd` / `idle_ended_cmd` shell hooks.
- `actionThread` (`action_thread.py`) — background thread that pumps `playerManager.update()` so MPV property changes from the player thread can trigger Python work without re-entering MPV's callback context.
- `user_interface` — selected at startup: `mpvtk_browser.ui.user_interface` if `enable_gui` and Pillow imports cleanly, otherwise `cli_mgr.user_interface`. There is no second window and no browser subprocess; the tray is the only child process (`tray.py`, because pystray needs the main thread on macOS).
- **Cast screen** (`mpvtk_browser/cast.py`) — the Chromecast-like preview (idle "Ready to cast" backdrop + `DisplayContent` item preview) is a browser **route**, not a separate UI. Backdrop + gradient + text are baked into one full-window bitmap because mpv composites overlay bitmaps *above* all script ASS (mpvtk GUIDE §6), so text drawn as a node would be hidden. It was `display_mirror.py`, which attached its own `MpvtkApp` and ran its own loop — two owners of one window, which is why `display_mirroring` had to fall back to the Tk browser.
- **`headless`** (`conf.py`) — cast-target mode: the cast screen is the only page and the library is unreachable from the machine. Enforced at the single choke point `MpvtkBrowser.navigate()` (plus `enter_browse`, `on_nav_command`, `display_item` and the now-playing bar's Queue button). `tests/test_mpvtk_headless.py` enumerates every door and has a catch-all so a newly added route is refused by default. Not a security boundary — the tray still reaches Settings, deliberately.
- **Home screen sections** (`mpvtk_browser/home_sections.py`) — the home screen's rows are user-configurable and the layout is **stored on the server**, not in `conf.py`: DisplayPreferences under id `usersettings` and client `emby` (jellyfin-web's legacy namespace — any other client string reads a different, empty preference set), keys `homesection0`..`homesection9`. `home_sections.py` is pure logic (resolve/serialize/defaults); the I/O is `LibrarySource.get_home_prefs` / `save_home_layout`. Two encoding rules are load-bearing for interop and easy to regress: an **empty slot means that slot's default, not "none"** (only the literal `"none"` blanks a slot), and a slot holding its own default is written back as `""`. Section types the shim can't draw (Live TV, recordings, books) are preserved on save rather than rewritten, so configuring the shim never degrades the same user's web home screen. Editing UI is the Settings → Home Screen tab.
  - The per-library "Latest" rows are one request each **with** `ParentId`, which bypasses the server's own `LatestItemsExcludes` handling — so that exclusion ("Display in home screen sections") is applied client-side in `get_home_rows`. Continue Watching / Next Up must keep passing **no** `ParentId`, which is what lets the server apply it for them.
- **Library browser** (`mpvtk_browser/`) — renders *inside the player's mpv window*, in the main process, attached to `playerManager`'s mpv. There is no longer a choice of UI: the Tkinter browser and its `browser_ui` setting were removed (see `mpvtk/MIGRATION.md` for the history). **Nothing in the package imports tkinter, and `tests/test_no_tkinter.py` enforces that.**

`menu.py` draws the in-player config menu by writing OSD text on MPV and consuming key/remote events; `mouse.lua` is loaded into MPV to forward mouse hits back. `syncplay.py` implements the SyncPlay timing loop using the time-sync support in `jellyfin-apiclient-python`'s `timesync_manager`. `bulk_subtitle.py` and `video_profile.py` are menu-driven features (season-wide subtitle changes, shader-pack profile switching).

## MPV backend selection

`player.py` picks a backend at import time:
- Default: `import mpv` (the `python-mpv` libmpv binding).
- If `settings.mpv_ext` is set, or libmpv can't load (`OSError`), it falls back to `python_mpv_jsonipc` and sets `is_using_ext_mpv = True`.

Both backends are aliased as `mpv` in the module. The two have different exception types for shutdown — `_mpv_errors` is the tuple to catch (`BrokenPipeError` always, plus `mpv.ShutdownError` only on libmpv). `wait_property` also has separate code paths for the two backends. macOS forces `mpv_ext = True` because libmpv isn't reliable there.

## Configuration system

`conf.py:Settings` declares every config key as a typed class attribute; defaults live there. `settings_base.py:SettingsBase` is a small homegrown pydantic-lite — it only reads `__annotations__` and uses the `object_types` lookup table to coerce values. **If you add a config key, the type annotation must be one of the entries in `object_types` (`bool`, `int`, `str`, `float`, or their `Optional[...]` forms)** — anything else will `KeyError` at load time. The single global instance is `conf.settings`. The README's "Configuration" section is the user-facing reference for the keys; keep them in sync.

Config and credentials live in a per-platform path resolved by `conffile.py` (typically `~/.config/jellyfin-mpv-shim/` on Linux, `%appdata%\jellyfin-mpv-shim\` on Windows, `~/Library/Application Support/jellyfin-mpv-shim/` on macOS).

## Optional dependencies are load-bearing

This project's policy (CONTRIBUTING.md) is that **everything beyond the four required deps must degrade gracefully** when its package is missing or broken. `mpv_shim.py:main` and `player.py` both demonstrate the pattern: `try: import optional_thing` inside a guard, then either set a feature flag or fall back. Required: `python-mpv`, `python-mpv-jsonipc`, `jellyfin-apiclient-python`, `requests`. Everything else (GUI, mirror, Discord, Windows niceties) is an `extras_require` group in `setup.py`. New features touching outside dependencies should follow the same `try/except ImportError` + fallback pattern; don't add a hard import.

## i18n

User-facing strings use gettext via `i18n.py`'s `_()`. After adding/changing strings:
1. `./regen_pot.sh` — updates `jellyfin_mpv_shim/messages/base.pot` and merges into existing per-locale `.po` files. It first folds in each locale's translations from the `master` branch (where Weblate lands) so volunteer work is preserved when running on a feature branch; override the ref with `MASTER_REF=origin/master`.
2. `./gen_pkg.sh --skip-build` (or `gen_pkg.sh` itself) compiles `.po` → `.mo`. `.mo` files are gitignored and regenerated at build time.

Translations are managed via Weblate (jellyfin/jellyfin-mpv-shim project); commits like "Translated using Weblate (...)" come from there — don't hand-edit `.po` files for in-flight translations.
