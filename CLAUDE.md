# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

This file is a **summary and an arrival brief**: the commands, the shape of the app,
and the rules that apply tree-wide. Subsystem detail lives in `docs/`, because it is
worth reading when you are working on that subsystem and not otherwise.

## Required Reading (VERY IMPORTANT)

**You MUST read these before starting work** — this CLAUDE.md is a summary only, and
each of these docs carries footguns that have **no line to sit on**: the danger is in
the line you are about to add, so an inline comment cannot warn you.

| Before doing... | Read this first |
|-----------------|-----------------|
| Editing `player.py` or its mixins (`player_audio.py`, `player_reporting.py`, `player_window.py`, `mpv_options.py`, `mpv_events.py`) | [docs/mpv-backends.md](docs/mpv-backends.md) |
| Any browser shell work — routes, pages, loading, navigation (`mpvtk_browser/`) | [docs/browser-shell.md](docs/browser-shell.md) |
| Drawing anything new with the toolkit (widgets, layout, overlays) | [jellyfin_mpv_shim/mpvtk/GUIDE.md](jellyfin_mpv_shim/mpvtk/GUIDE.md) |
| Adding or changing a server query or `Fields` list | [docs/jellyfin-api-notes.md](docs/jellyfin-api-notes.md) |
| Touching watched/played state, or the download catalog (`sync/`) | [docs/offline-sync.md](docs/offline-sync.md) |
| Writing a test, or adding/extending a fake | [docs/testing.md](docs/testing.md) |

**`docs/mpv-backends.md` is essential before any player work.** Three examples of
what is in it and cannot be inlined: mpv is **not re-created between queue items**, so
any global option written for one item is still set for the next (this has leaked an
auth token to a third-party host, clobbered a user's own `mpv.conf`, and made every
film play stretched); a **bound method cannot be a `property_observer`** on libmpv, so
it fails on exactly one backend; and an input section without `allow-hide-cursor`
means **the mouse cursor never hides again** for the rest of the session.

## Common commands

- Run from source (no install): `./run.py`
- Build: `./gen_pkg.sh` — downloads `default-shader-pack`, compiles `.po` → `.mo`,
  checks the version matches across `constants.py`, the Inno Setup script and the
  appdata XML, then runs `python3 -m build`.
  - `--skip-build` does prep only (translations + shader pack). Use on Windows, or
    when you only want `.mo` files.
  - `--install` runs `pip3 install .[all]` (with `sudo` if available; `--local`
    skips sudo).
  - `--get-pyinstaller` / `--gen-fingerprint` are CI helpers for the Windows cache.
- Regenerate the translation template after changing user-facing strings:
  `./regen_pot.sh`. It updates `jellyfin_mpv_shim/messages/base.pot` **and nothing
  else** — committing the `.pot` is the whole i18n obligation of a feature branch.
- Windows build (after `gen_pkg.sh --skip-build`): `build-win.bat` (`-32`, `-arm64`,
  `-dbg` variants). It needs `fribidi-0.dll` beside it —
  `python tools/build_win_fribidi.py` builds it in about a second.
  **Pillow's wheels ship no FriBiDi**, and without the DLL `ImageFont.truetype`
  silently returns a Basic-layout font: RTL text draws reversed, and — the one with
  reach — nothing gets kerned, so `mpvtk.metrics` feeds unkerned measurements to every
  ellipsize and wrap decision. See `docs/packaging.md`.
- Flatpak bundle of the working tree:
  `flatpak-builder build flatpak/com.github.iwalton3.jellyfin-mpv-shim.json --force-clean --repo=repo --user`,
  then `./artifacts.sh flatpak amd64`. Clone `flathub/shared-modules` into
  `flatpak/shared-modules` first. `tools/check_flatpak_pins.py [manifest]` verifies
  every pinned sha256 in seconds — pass it the Flathub manifest, which is usually the
  one that has drifted. See `docs/packaging.md`.
- Tests: `xvfb-run -a python3 -m unittest discover tests` (stdlib unittest, no extra
  deps). Integration matrix:
  `xvfb-run -a python3 tests/integration/run_integration.py`.
- Profile a server's artwork handling: `tools/bench_image_loading.py`. Read-only; uses
  the saved credentials. See `docs/artwork-pipeline.md`.
- There is no linter config.

Two test rules, because both cost a session when missed:

- **Select with `-k`, never a module name and never `-p`.** Importing almost anything
  under `jellyfin_mpv_shim` reaches `args.get_args()` at import time, which parses the
  real `sys.argv`. `discover tests` is safe because unittest replaces argv first and
  consumes `-k`/`-v` itself; a module name stays in argv as a positional and dies with
  the app's own usage line, which reads as a broken test module and is not.
  (`tests/e2e/` is exempt and *is* named directly.)
- **Always `xvfb-run`, including for the unit suite.** `player.py` builds its
  `playerManager` singleton at module scope and `__init__` ends with `_init_mpv()`, so
  *importing* it opens a real mpv window. Eight unit modules import it.

The Python build is PEP 517 / `pyproject.toml` with `setuptools`. The full path needs
`pip install build`; `pip install .[all]` and `pip install -e .` work without it.
`gen_pkg.sh` fetches `jellyfin_mpv_shim/default_shader_pack/` from a GitHub release;
it is not in git.

## Bumping the version

`jellyfin_mpv_shim/constants.py:CLIENT_VERSION` is the single source of truth for the
Python package — `pyproject.toml` reads it via `tool.setuptools.dynamic`. The other
two are not derived and `gen_pkg.sh` warns loudly if they drift:

- `jellyfin_mpv_shim/constants.py` → `CLIENT_VERSION`
- `Jellyfin MPV Shim.iss` → `#define MyAppVersion`
- `jellyfin_mpv_shim/integration/com.github.iwalton3.jellyfin-mpv-shim.appdata.xml`
  → first `<release version="...">`

A version containing `pre` skips the appdata (it is Flathub's changelog and
pre-releases are not published there) and normalizes to `rc` in the wheel. Both rules
and their traps: `docs/packaging.md` §4.

## Architecture

Entry point is `jellyfin_mpv_shim/mpv_shim.py:main`. It wires a set of
**module-level singletons** that talk to each other via direct references and
`threading.Event` triggers — no DI container, no event bus, just imports. Each is a
module-level instance, not a class to instantiate:

- `clientManager` (`clients.py`) — one `JellyfinClient` per logged-in server,
  persists creds to `cred.json`, runs a health-check thread, forwards websocket
  events via `clientManager.callback`.
- `eventHandler` (`event_handler.py`) — receives those and dispatches to
  `playerManager`. Add a remote-control event by decorating a method with
  `@bind("EventName")`.
- `playerManager` (`player.py`) — wraps mpv, owns the current playlist (a `Media`
  from `media.py`), exposes the operations the rest of the app calls. The largest and
  most central module.
- `timelineManager` (`timeline.py`) — posts playback progress to Jellyfin and fires
  the `idle_cmd` / `idle_ended_cmd` hooks.
- `actionThread` (`action_thread.py`) — pumps `playerManager.update()` so mpv property
  changes can trigger Python work without re-entering mpv's callback context.
- `user_interface` — `mpvtk_browser.ui` if `enable_gui`, Pillow imports, **and this
  mpv has lua**; otherwise `cli_mgr`. The third condition is not a nicety: everything
  the shim draws is lua, and an mpv built `-Dlua=disabled` has no `--osc` option and
  *refuses to start* when told to set one.

`menu.py` draws the in-player OSD config menu; `mouse.lua` forwards mouse hits back.
`syncplay.py` implements the SyncPlay timing loop. `bulk_subtitle.py` and
`video_profile.py` are menu-driven features.

Four smaller pure modules, each with its own tests: `keysweep.py` (reads mpv's
*resolved* `input-bindings` so a claim re-issues the user's own binding),
`input_conf.py` (the one-time migration of `seek_*` into the user's `input.conf`),
`items_api.py` (the modern `GET /Items`; `build_query` **raises** on an unknown
keyword, since a silently dropped parameter is a filter that stopped applying), and
`shader_overrides.py` (which shader profile an item gets, by scope).

### The library browser

`mpvtk_browser/` renders **inside the player's mpv window**, in the main process,
attached to `playerManager`'s mpv. No second window, no subprocess. **Nothing in the
package imports tkinter, and `tests/test_no_tkinter.py` enforces that.**
Shape, routing, thread contract, epoch and lock policy: `docs/browser-shell.md`.

**The standing footgun is state that changes between draws.** A screen is rebuilt from
scratch on every repaint, so a widget tree is a snapshot and nothing reconciles it
afterwards. Two failures, both of which have shipped:

- **A handler that captures.** `lambda: play(item, aid=aid)` where `aid` was resolved
  in `render()` fires with whatever was selected when the page last drew. **Read
  mutable state inside the handler, not in the builder.**
- **A handler that writes without asking for a repaint.** The renderer flips a
  Dropdown's selection and a TextBox's text optimistically, so those look
  self-updating; **a `Checkbox` does not** — it is Box-plus-tick coloured from
  `checked`, and only a redraw can move it.

**A scene assertion is not a repaint assertion.** `build_scene` renders when asked, so
it draws a correct tree whether or not the app would ever have redrawn — a handler that
changes state owes a test that `invalidate` was called, and a stray `build_scene(b)`
between a click and its assertion silently refreshes every closure on screen. Both
bugs above passed their scene-based tests, one of them *because* the test rebuilt in
between.

`tools/audit_stale_captures.py` (run by `tests/test_no_stale_captures.py`) catches the
capturing half from the source. A finding is not automatically a bug — read the state
inside the handler, or add it to that file's `ACCEPTED` with a reason. It says nothing
about the writing half.

### Two things about the Jellyfin server, before adding a query parameter

Both measured (`tests/e2e/test_filter_matrix.py`):

- **Jellyfin drops what it does not recognise.** An unknown parameter name and an
  unparseable enum value both answer exactly as sending nothing does, so a typo turns
  a filter *off* and the screen looks normal. The evidence that a value parses is the
  contrast with a deliberately bogus one.
- `Filters=IsUnplayed,IsPlayed` is **HTTP 400 on 12.0 and an empty result on 10.11**,
  which is why `dialogs.MUTUALLY_EXCLUSIVE` exists.

More server behaviour — DisplayPreferences, CustomPrefs, UserData, the obsolete
`Users/{id}/Items` route: `docs/jellyfin-api-notes.md`.

## MPV backend selection

`player.py` is composed: `PlayerManager(AudioMixin, ReportingMixin, WindowMixin)`,
with those in `player_audio.py`, `player_reporting.py` and `player_window.py`, and the
option dict built by `mpv_options.py`. Mixins rather than owned objects on purpose:
`self._player` is read ~200 times, 36 methods share one `RLock` via
`@synchronous("_lock")`, and re-entrancy across them is load-bearing. Each mixin
declares what it borrows under `if TYPE_CHECKING:`; **the length of that list is the
coupling metric, so grow it deliberately.**

`player.py` picks a backend at import time:

- default: `import mpv` (the `python-mpv` libmpv binding);
- if `settings.mpv_ext` is set, or libmpv can't load (`OSError`): `python_mpv_jsonipc`,
  with `is_using_ext_mpv = True`. macOS forces this — libmpv isn't reliable there.

Both are aliased as `mpv`. `_mpv_errors` is the tuple to catch on shutdown
(`BrokenPipeError` always, plus `mpv.ShutdownError` only on libmpv). `wait_property`
has separate paths for the two. Everything else that diverges — observer registration,
construction failures, input sections, options that outlive a queue item, version
behaviour — is in `docs/mpv-backends.md`.

**Extracted modules must import the backend globals (`is_using_ext_mpv`,
`_mpv_errors`, `discord_presence`, `win_utils`) per call, inside the method, never at
module scope.** Module-scope binding captures the backend, and the integration harness
swaps a fake mpv in and out by evicting modules from `sys.modules` — a second module
holding a bound copy has to be evicted in lockstep, and it fails only on the
whole-suite leg. Each module has a subprocess guard test pinning this.

`mpv_options.py` imports neither `player` nor a backend, so option behaviour is
testable without opening a window (`tests/test_mpv_options.py` pins that). The dict is
an `OrderedDict` and **insertion order is deliberate**.

### Two ambushes that fire far from the feature that owns them

- **"Is the library on screen" is `_library_showing()`, never `_video is None`.** Audio
  keeps `_video` set *and* keeps the browser up — that is what the now-playing bar is
  for — so the obvious test answers "no" while the user is looking straight at the
  library. `_nav_back` asked it the wrong way and refused BACK for the whole of music
  and audiobook playback, which killed the mouse thumb buttons the moment anything
  played. That predicate's docstring warns about this.
- **`keepaspect` is the *window's* property**, owned by `set_browse_window` (off, so
  the library window resizes freely) and `browse_yield` (on). The comic reader only
  borrows it, so `reset_picture_view` must **not** put it back while something is
  playing. That method runs from `_release_page_grabs` on every browse → video handoff
  through `run_action`, which defers whenever the player lock is busy — and it is busy
  for the whole of a playback start. When the reset landed second, **every film played
  stretched, with no comic anywhere in the session.**
  `tests/test_window_geometry.py:PictureViewHandoffTest` asserts both interleavings.

## Configuration system

`conf.py:Settings` declares every config key as a typed class attribute; defaults live
there. `settings_base.py:SettingsBase` is a homegrown pydantic-lite — it reads
`__annotations__` and coerces via the `object_types` table. **A new config key's type
annotation must be one of the entries in `object_types`** (`bool`, `int`, `str`,
`float`, `list`, or the `Optional[...]` forms of the first four) — anything else
`KeyError`s at load time. The single global instance is `conf.settings`.

`docs/configuration.md` is the user-facing reference and
`tests/test_docs_coverage.py` fails a setting that has no entry there.

Config and credentials live in a per-platform path from `conffile.py`
(`~/.config/jellyfin-mpv-shim/`, `%appdata%\jellyfin-mpv-shim\`,
`~/Library/Application Support/jellyfin-mpv-shim/`).

**Themes are not config keys** — they are JSON files, resolved like shader packs:
built-ins in `jellyfin_mpv_shim/themes/*.json`, shadowed by a same-named file under
`<config>/themes/`. `mpvtk_browser/themes.py` holds the loader and `DEFAULT`, which is
simultaneously the fallback, the schema (a theme may only set keys that appear in it,
coerced to those types) and the merge base. `mpvtk_browser/theme.py` then serves
`theme.ACCENT`-style reads from a dict via a module `__getattr__`.
**Do not reintroduce writing the palette into `globals()`**: it let a theme define
arbitrary module attributes, including over the functions there, and turned a mistyped
colour name into a new global while the real one silently kept its old value.

## Optional dependencies are load-bearing

Project policy (CONTRIBUTING.md): **everything beyond the four required deps must
degrade gracefully** when its package is missing or broken. `mpv_shim.py:main` and
`player.py` both demonstrate the pattern — `try: import optional_thing` inside a guard,
then a feature flag or a fallback.

Required: `python-mpv`, `python-mpv-jsonipc`, `jellyfin-apiclient-python`, `requests`.
Everything else (GUI, mirror, Discord, Windows niceties) is an `extras_require` group.
New features touching outside dependencies follow the same `try/except ImportError` +
fallback pattern; don't add a hard import.

## Testing discipline

Three rules that have each caught real bugs here. The suites, the case histories and
the audit tooling are in `docs/testing.md`.

- **Assert the property over several steps, not the mechanics of one.** The recurring
  bug shape is state feeding back into the input that produced it, and one-step tests
  cannot see it. Anything a scheduler, poller, health check or websocket can re-run
  gets a loop of ≥3 and an assertion that the observable did not walk.
- **A stand-in that omits a field is how a property goes untested.** It does not leave
  a path uncovered — it makes the path unreachable *while reporting a pass*, because
  the thing the test is named after has nowhere to live. The review question for a new
  fake is *which field of the real object did I not model, and is that the field the
  test is named after?* `tools/audit_fake_contracts.py` (via
  `tests/test_no_fake_gaps.py`) checks the half that is checkable.
- **Ordering claims go through `_harness.Journal`**, one stream every fake writes into,
  and **assertions are subsequences, never equality** — a log compared as a whole fails
  the day somebody adds an event.

## i18n

User-facing strings use gettext via `i18n.py`'s `_()`. After adding or changing them:

1. `./regen_pot.sh` — updates `base.pot` and nothing else. Commit it.
2. `./gen_pkg.sh --skip-build` compiles `.po` → `.mo`. `.mo` files are gitignored.

**Never run `./regen_pot.sh --merge`, and never touch the per-locale `.po` files,
unless explicitly asked.** The `.pot` is the template Weblate reads; filling the 86
`.po` files in is Weblate's job. Merging locally rewrites every locale (msgmerge
rewrites references and re-wraps what it touches) — ~10k lines across 86 files even
when no translation changed — which then collides with `master` in files nobody on the
branch edited, unreadably, because the diff is almost entirely reference comments.
It is a maintainer operation that has to be merged within the hour to be worth
anything.

Translations are managed via Weblate; "Translated using Weblate (...)" commits come
from there — don't hand-edit `.po` files for in-flight translations. A `weblate` remote
points at its git export, and `MASTER_REF=weblate/master` merges against the freshest
volunteer work.

**One word, two meanings: use `_p(context, string)`.** gettext keys on the English, so
a string reused in two senses collapses to one entry and no language can tell them
apart. A context is part of the key, so adding one to a string that did not need it
discards every existing translation of it. Known cases: `Record`, `Channels`,
`Download`, `None`. Extraction rationale and seeding: `docs/i18n.md`.

## Documentation Reference

| Doc File | When to Read |
|----------|--------------|
| [docs/browser-shell.md](docs/browser-shell.md) | the browser shell: mixins/Pages, thread contract, epoch, lock policy, refresh-in-place, scroll parking, pollers, live-applied settings |
| [docs/mpv-backends.md](docs/mpv-backends.md) | libmpv vs jsonipc, construction failures, mpv versions, SDL/signals, input sections, options that outlive a queue item, `wait_property`, video-setting defaults |
| [docs/artwork-pipeline.md](docs/artwork-pipeline.md) | strips and the cache, transparent-artwork plating, tile shapes, Cover Size, badges, header baking, grid centring |
| [docs/jellyfin-api-notes.md](docs/jellyfin-api-notes.md) | how the server actually behaves: DisplayPreferences/CustomPrefs, UserData, dropped parameters, Live TV query shapes |
| [docs/readers.md](docs/readers.md) | books, audiobooks, the epub reader and the comic reader |
| [docs/live-tv.md](docs/live-tv.md) | the Live TV screens, guide, timers and self-refresh |
| [docs/offline-sync.md](docs/offline-sync.md) | who may write watched state and in which direction; the sweep schedule; what `UserDataChanged` does and does not announce |
| [docs/testing.md](docs/testing.md) | the SyncPlay suites, the fakes discipline and its case histories, the journal |
| [docs/packaging.md](docs/packaging.md) | Windows/FriBiDi, the ARM64 job, `tools/msgfmt.py`, Flatpak, version spelling |
| [docs/i18n.md](docs/i18n.md) | xgettext, location granularity, `--merge`, seeding from jellyfin-web |
| [docs/configuration.md](docs/configuration.md) | the user-facing settings reference (enforced by a test) |
| [docs/PERMISSION_GAPS.md](docs/PERMISSION_GAPS.md) | what breaks without each Jellyfin user permission |
| [jellyfin_mpv_shim/mpvtk/GUIDE.md](jellyfin_mpv_shim/mpvtk/GUIDE.md) | the mpvtk toolkit itself: widgets, layout, the ASS/bitmap z-order constraint |
