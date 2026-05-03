# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

- Run from source (no install): `./run.py`
- Prepare build artifacts (downloads `default-shader-pack`, compiles `.po` → `.mo`, validates that versions match across `constants.py`, the Inno Setup script, and the appdata XML, then runs `python3 -m build` to produce sdist + wheel): `./gen_pkg.sh`
  - `--skip-build` does prep only (translations + shader pack), no sdist/wheel. Use this on Windows or when you only want `.mo` files.
  - `--install` runs `pip3 install .[all]` (with `sudo` if available; add `--local` to skip sudo).
  - `--get-pyinstaller` / `--gen-fingerprint` are CI helpers for the Windows build cache.
- Regenerate translation template and merge into existing `.po` files: `./regen_pot.sh`
- Windows build (after `gen_pkg.sh --skip-build`): `build-win.bat` (`build-win-32.bat` for 32-bit, `build-win-dbg.bat` for debug). Installer is built with Inno Setup from `Jellyfin MPV Shim.iss`.
- There is no test suite and no linter config — code style is `black` (per the README badge), but `black` is not wired into the repo.

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
- `user_interface` — selected at startup: `gui_mgr.user_interface` if `enable_gui` and the GUI deps import cleanly, otherwise `cli_mgr.user_interface`. The GUI module uses `multiprocessing` and event threads to work around tkinter/pystray quirks (per CONTRIBUTING.md).
- `mirror` (`display_mirror/`) — optional Chromecast-like preview window, only loaded if `display_mirroring` is enabled and `Jinja2` + `pywebview` are installed. When present, `mirror.run()` becomes the main loop; otherwise main blocks on a `halt` Event.

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
1. `./regen_pot.sh` — updates `jellyfin_mpv_shim/messages/base.pot` and merges into existing per-locale `.po` files.
2. `./gen_pkg.sh --skip-build` (or `gen_pkg.sh` itself) compiles `.po` → `.mo`. `.mo` files are gitignored and regenerated at build time.

Translations are managed via Weblate (jellyfin/jellyfin-mpv-shim project); commits like "Translated using Weblate (...)" come from there — don't hand-edit `.po` files for in-flight translations.
