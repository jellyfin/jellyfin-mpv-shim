# Packaging and platform builds

`CLAUDE.md` carries the commands. This is what is behind them: the platform traps,
why the ARM64 job is shaped differently from the x86 ones, and the version-spelling
rules.

## 1. Windows

Build with `build-win.bat` after `gen_pkg.sh --skip-build`
(`build-win-32.bat` for 32-bit, `build-win-arm64.bat` for ARM64,
`build-win-dbg.bat` for debug). The installer is Inno Setup, from
`Jellyfin MPV Shim.iss`; the ARM64 script passes `/DArm64`, which is the only thing
that file branches on.

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
