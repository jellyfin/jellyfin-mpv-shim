# Packaging and platform builds

`CLAUDE.md` carries the commands. This is what is behind them: the platform traps,
why the ARM64 job is shaped differently from the x86 ones, and the version-spelling
rules.

## 1. Windows

Build with `build-win.bat` after `gen_pkg.sh --skip-build`
(`build-win-arm64.bat` for ARM64, `build-win-dbg.bat` for debug). The installer is
Inno Setup, from `Jellyfin MPV Shim.iss`; the ARM64 script passes `/DArm64`, which is
the only thing that file branches on.

Three installers ship: the standard x86-64-v3 one, **LEGACY64** (plain x86-64, for
CPUs without AVX2 — a live and used build, ~5% of downloads), and ARM64.

### There is no 32-bit build, and the reason is a case history

`build-win-32.bat`, the `build-win32-legacy` job and the LEGACY32 installer were
removed in August 2026. It is worth knowing why, because the failure is a shape this
project can repeat.

The build existed **by request**: #41, April 2020, one user with a 32-bit Windows 10
tablet. It worked while that user was testing each build by hand — including catching
the first version of this exact bug, where "PyInstaller ignored the directive to use
the 32 bit version of MPV and instead packaged the 64 bit version".

Then it broke, in two steps, and stayed broken for over five years:

* **2021-04-20, `e55e1830` "Switch to GitHub Actions".** The Azure pipeline's
  `LegacyWindows` job set `architecture: 'x86'` on its Python. The migration to
  GitHub Actions dropped that line and never replaced it, so every LEGACY32 build
  after it was an **x64 executable**. The Inno installer is 32-bit whatever the
  payload is (see the `#ifdef Arm64` comment in the .iss), so it installs happily on
  32-bit Windows and then fails launching the payload with `CreateProcess` **error
  216**, `ERROR_EXE_MACHINE_TYPE_MISMATCH`. That is #278, filed 2022-06-29 against
  "any version above 1.10.4" — and v1.10.4 shipped six days before that commit.
* **2021-12-22, `847f8273` "Upgrade to MPV version 20211219 fd63bf3".** A routine
  version bump that also switched the legacy job's download from `mpv-dev-x86_64` to
  `mpv-dev-i686`. Until then the x64 exe at least had an x64 libmpv, so the installer
  worked on 64-bit Windows (as a duplicate of the standard build). After it, the x64
  exe bundles a 32-bit `mpv-2.dll`: `ctypes` cannot load it, `player.py` reads that as
  "no libmpv", the external backend looks for an `mpv.exe` this build has never
  shipped, and the client dies at startup. The same end state the missing Vulkan
  loader produced, from the other direction.

**Neither step could fail the build**, which is the lesson. Architecture mismatches
are not build errors — every job stayed green for five years, and CI cannot notice
that the file it just packaged is for a different machine. `tools/check_win_arch.py`
exists because the ARM64 job has the identical exposure; it is run there against both
the fetched libmpv and the built executable, and that is the pattern any new
architecture must copy on day one.

The evidence for removing rather than fixing: three downloads of LEGACY32 on
v3.0.0pre13 against 382 standard and 35 ARM64; one report in five years (#278) with
no second voice; and the last comment mentioning 32-bit anywhere in the tracker dated
2020-08-16, eight months *before* the build stopped working. Shipping it broken was
worse than not shipping it — a 32-bit user was downloading something official-looking
that failed cryptically instead of reading "not supported".


### FriBiDi is required, and its absence is silent

`fribidi-0.dll` must sit beside the batch file. `python tools/build_win_fribidi.py`
builds it from pinned source in about a second, and
`tools/check_win_raqm.py --require-bundled` fails the build without it.

**Pillow's wheels ship no FriBiDi on any platform.** libraqm and HarfBuzz are
compiled into `_imagingft`, FriBiDi alone is loaded at runtime by a vendored shim,
and Windows has no such DLL — so `ImageFont.truetype` silently returns a *Basic*-layout
font.

That costs two things and only the first is obvious:

1. **Right-to-left text draws in logical order with isolated letterforms** (#689 —
   Arabic tile captions reversed while the same string as an ASS node is correct,
   because libass carries its own).
2. **HarfBuzz is reachable only through Raqm**, so no script gets GPOS kerning. This
   is the one with reach: `mpvtk.metrics` measures with Pillow to model what libass
   will draw, so unkerned measurements feed **every** ellipsize and wrap decision.
   Measured, DejaVuSans at 20px: `AVATAR` is 81.94px unkerned against 75.20px kerned.

`jellyfin_mpv_shim/win_fribidi.py` loads it **by absolute path** before anything
imports `PIL.ImageFont`. The shim's `LoadLibrary` takes a bare name, so leaving it to
the DLL search path would make this a property of PyInstaller's bootloader rather than
something the app states.

### The Vulkan loader is shipped, and its absence is fatal

`vulkan-1.dll` must sit beside the batch file, exactly like `fribidi-0.dll`.
`python tools/build_win_vulkan_loader.py` builds it from pinned Vulkan-Loader +
Vulkan-Headers source (~500 KB), and
`tools/check_win_libmpv_deps.py --allow vulkan-1.dll mpv-2.dll` fails the build
without it — the `--allow` is *checked*, so claiming to ship it and not shipping it
is the same failure as not claiming it.

**shinchiro's libmpv hard-imports it from the 20260808 release on** — twelve `vk*`
symbols in the import table, delay-load directory empty. The Vulkan loader is
installed by the GPU driver, not by Windows, so on a machine without one
`LoadLibrary` refuses `mpv-2.dll` outright, before any mpv code runs. The way that
surfaces hides the cause twice: `python-mpv` raises `OSError`, `player.py` reads
that as "no libmpv" and switches to the external backend, and the external backend
fails looking for an `mpv.exe` the Windows build has never shipped. **The traceback
the user gets names `subprocess`.** That is what shipped in 3.0.0pre12, and it is
why the mpv pin sat at 20260610 for two releases.

Bundling the loader is what unstuck it, and the pin has to move: the fix for #687
(SDR trickplay frames blown out over HDR video, because `vo_gpu_next` tagged every
`overlay-add` bitmap with the *video's* colorspace) is in mpv master only.

Two things worth knowing before touching this:

* **Upstream mpv ships a loader with its own Windows builds** — `ci/build-win32.ps1`
  builds Vulkan-Loader as a subproject and copies `vulkan.dll` to `vulkan-1.dll`
  beside `mpv.exe`. This is the reference platform build's answer, not a liberty.
* **The architecture must match `mpv-2.dll`, not the interpreter.** FriBiDi is the
  other way round — it is loaded by Pillow, so it follows the Python that runs. This
  is a hard import *of libmpv*, resolved by Windows while loading that file. In a
  correct build the two coincide, and `build_win_vulkan_loader.verify()` still asks
  the question the right way round rather than assuming they do — the 32-bit job
  spent five years being a build where they did not (above), and the check would have
  caught it in one run.

Measured under wine, against shinchiro's 20260828 libmpv (Windows Python embeddable,
`ctypes.CDLL("mpv-2.dll")`, `WINEDLLOVERRIDES=vulkan-1=n` so wine's builtin loader
cannot answer): with no loader anywhere, `Could not find module 'mpv-2.dll' (or one
of its dependencies)` — the users' exact message; with a 536 KB `vulkan-1.dll` beside
the executable, it loads and `mpv_initialize` with `vo=gpu-next --gpu-api=vulkan`
returns 0.

### The ARM64 job, and why every difference is forced

Windows on Arm emulates x64, so **every way of getting the architecture wrong produces
a working build of the wrong thing rather than an error.** That is why:

- `tools/check_win_arch.py` runs **twice** in that job — on `mpv-2.dll` before
  building, on `run.exe` after — instead of the flags being trusted;
- the waf invocation is pinned with `--target-arch=64bit-arm --check-c-compiler=msvc`
  for the same reason;
- there is **no `setup-git-for-windows-sdk` step**: that action's `aarch64` resolves to
  artifacts `git-sdk-arm64` does not publish, and using the x86_64 SDK under emulation
  means relying on MSYS2's `fork()` emulation, which is the part that does not hold up.

Consequently the ARM64 runner has no `msgfmt` and no `unzip`, so `gen_pkg.sh` falls
back to `tools/msgfmt.py` and `python -m zipfile`.

## 2. `tools/msgfmt.py`

A `.po` → `.mo` compiler, used wherever GNU gettext is absent.

`tests/test_msgfmt.py` compiles all 86 real catalogs with both it and GNU `msgfmt` and
asserts the translations match, **because nothing downstream would notice it being
wrong**: a short catalog still loads and the app just shows English.

The two places it deliberately matches gettext rather than the naive reading:

- it drops `POT-Creation-Date` from the header;
- it draws entry boundaries at **blank/comment lines**, not at the `msgctxt`/`msgid`
  keyword — because `#, fuzzy` *precedes* those keywords, so a boundary there discards
  the flag and leaks unreviewed translations.

## 3. Flatpak

```
flatpak-builder build flatpak/com.github.iwalton3.jellyfin-mpv-shim.json \
    --force-clean --repo=repo --user
./artifacts.sh flatpak amd64
```

Clone `flathub/shared-modules` into `flatpak/shared-modules` first — see
`flatpak/README.md`. CI builds this for amd64 + arm64.

`tools/check_flatpak_pins.py [manifest]` verifies every pinned sha256 and tag in
seconds, which beats finding a stale one minutes into a cold build. It takes the
Flathub repo's manifest as an argument, and **that is usually the one that has
drifted**.

It also reports a git source pinned by `branch` *and* `commit`. flatpak-builder
**verifies** that pairing — "If branch is also specified, then it is verified that
the branch/tag is at this specific commit" — so pinning a commit out of the history
of a moving branch is a build that breaks on the next upstream push, with
`Git commit for branch master is <head>, but expected <pin>`. mpv is pinned to a
master commit here (see the manifest's `//mpv-pin-note`), so it carries the commit
alone; flatpak-builder finds no ref at it and mirrors the full repo instead of
shallow-cloning. A `tag` is safe to name, because a tag does not move.

**This manifest is not the Flathub one.** It pip-installs the checkout with
`--share=network` instead of the pinned wheel set — convenient here and disqualifying
there.

## 4. Version spelling

`jellyfin_mpv_shim/constants.py:CLIENT_VERSION` is the single source of truth for the
Python package; `pyproject.toml` reads it via `tool.setuptools.dynamic`. The Inno Setup
and Flatpak appdata files are not derived and must be kept in sync by hand — see the
checklist in `CLAUDE.md`. `gen_pkg.sh` warns loudly on drift.

### Pre-releases skip the appdata

A version containing `pre` (`3.0.0pre10`) is **not published to Flathub**, and the
appdata is Flathub's changelog — so it is *expected* to sit at the last stable version,
and `gen_pkg.sh` exempts it from the match check for those.

It warns the other way instead: an appdata entry that names the current pre-release
would ship to Flathub with the next stable build. Constants ↔ Inno Setup are still
checked unconditionally.

### `pre` normalizes to `rc`

`pre` is a legal PEP 440 pre-release spelling (an alias for `rc`), so setuptools
accepts `3.0.0pre10` and normalizes it — **the wheel and sdist come out as
`3.0.0rc10`** while the tag, the Inno Setup installer and `artifacts.sh`'s filename
keep the `pre` spelling.

`version.py` parses both spellings, so the update check orders a pre-release correctly
against stable tags rather than offering 2.10.0 as an "upgrade".

## 5. The Python build

PEP 517 / `pyproject.toml` with `setuptools` as the backend. The full build path
requires the `build` package (`pip install build`); `pip install .[all]` and
`pip install -e .` both work without it.

`gen_pkg.sh` also fetches `jellyfin_mpv_shim/default_shader_pack/` from the
[`iwalton3/default-shader-pack`](https://github.com/iwalton3/default-shader-pack) GitHub
release; that directory is not in git (see `.gitignore`).
