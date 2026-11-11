# Third-party notices

JellyHome Player is licensed **GPL-3.0-or-later** (see [LICENSE.md](LICENSE.md)). It is an independent fork of
[Jellyfin MPV Shim](https://github.com/jellyfin/jellyfin-mpv-shim) by Izzie Walton and contributors, which in turn
incorporates MIT-licensed code from Plex MPV Shim / Omplex (Weston Nielson) and GPLv3 code from Jellyfin Kodi. It is
not an official Jellyfin project.

This file lists what the **installers and packages** redistribute in addition to the application's own code, under
what licence, and where the corresponding source is. Anything marked **verify** is a fact that has to be checked
against the exact release archive when a release is cut; this file does not guess at it.

Each package/installer also carries the licence texts it can carry automatically (the Linux `.deb` ships
`/usr/share/doc/jellyhome-player/licenses/<library>/` and `python-licences.md`, generated at build time by
`tools/collect_licenses.py`, which fails the build for a bundled library with no licence file).

## What is bundled where

| Component | Linux `.deb` | Flatpak | Windows installer | macOS app |
|---|---|---|---|---|
| Application, Lua scripts, themes | yes | yes | yes | yes |
| Python runtime | system `python3` (a dependency) | GNOME runtime | bundled (PyInstaller) | bundled (PyInstaller) |
| Python libraries (table below) | bundled, hash-locked | pip at build | bundled | bundled |
| libmpv / mpv | system `libmpv2` (a dependency, **not redistributed**) | built from source in the manifest | `mpv-2.dll` (shinchiro build) | `mpv.app` (upstream mpv-player build) |
| FFmpeg, libplacebo, libass, ... | inside system libmpv | built from source | inside `mpv-2.dll` | inside `mpv.app` |
| FriBiDi | system `libfribidi0` (a dependency) | GNOME runtime | `fribidi-0.dll` built from pinned source | `libfribidi.dylib` built from pinned source |
| Vulkan loader | system | GNOME runtime | `vulkan-1.dll` built from pinned source | none shipped by this project (**verify** what `mpv.app` carries) |
| PyInstaller bootloader | n/a | n/a | yes | yes |
| Shader pack (optional video profiles) | yes | yes | yes | yes |

## Python libraries bundled in the Linux `.deb` (and installed by pip elsewhere)

| Package | Licence | Notes |
|---|---|---|
| python-mpv 1.0.8 | GPL-2.0-or-later **or** LGPL-2.1-or-later | libmpv binding |
| python-mpv-jsonipc 1.5.0 | Apache-2.0 | external-mpv backend |
| jellyfin-apiclient-python 1.21.0 | GPL-3.0 | Jellyfin API client |
| Pillow 12.3.0 | MIT-CMU | The manylinux wheel also carries FreeType (FreeType Licence or GPLv2), HarfBuzz (Old MIT), libraqm (MIT), libpng, libjpeg-turbo, libtiff, libwebp, zlib, lcms2 and others; their notices are in the wheel's `LICENSE`, shipped as `licenses/pillow/LICENSE`. |
| requests 2.34.2 | Apache-2.0 | ships a `NOTICE` |
| urllib3 2.8.0 | MIT | |
| idna 3.20 | BSD-3-Clause | |
| charset-normalizer 3.5.2 | MIT | |
| certifi 2026.10.23 | MPL-2.0 | Mozilla CA bundle; the source is the package itself |
| websocket-client 1.9.2 | Apache-2.0 | |

The Windows and macOS builds additionally include `pystray` (LGPL-3.0-or-later), `pypresence` (MIT) and, on Windows,
`pywin32` (PSF-2.0). Python itself is PSF-2.0. **Verify** the exact versions in the build logs of the release.

## mpv, FFmpeg and the other native libraries (GPL/LGPL source obligations)

mpv is **GPL-2.0-or-later** as normally built (LGPL-2.1-or-later only for an `--enable-lgpl` build); FFmpeg is
LGPL-2.1-or-later or GPL-2.0-or-later depending on how it was configured; libplacebo is LGPL-2.1-or-later; libass is
ISC. The Windows and macOS installers redistribute binary builds made by others, so the project must be able to
point users at the corresponding source:

* **Windows**: `mpv-dev-x86_64-v3-*`, `mpv-dev-x86_64-*` and `mpv-dev-aarch64-*` archives from
  [shinchiro/mpv-winbuild-cmake](https://github.com/shinchiro/mpv-winbuild-cmake), release tag pinned in
  `.github/workflows/main.yml` (currently `20261017`). That repository's build scripts and the release notes name every
  component and its source. **Verify** the licence file inside the archive at release time and ship it.
* **macOS**: `mpv-v0.41.0-macos-15-{arm,intel}.zip` from the
  [mpv releases](https://github.com/mpv-player/mpv/releases/tag/v0.41.0); source is the tag `v0.41.0` of
  [mpv-player/mpv](https://github.com/mpv-player/mpv). **Verify** the libraries inside `mpv.app` and their licences.
* **Flatpak**: built from the sources pinned in `flatpak/io.github.braisravina.JellyHomePlayer.json`, so the source
  is exactly what that manifest names.
* **Linux `.deb`**: nothing native is redistributed; `libmpv2` and `libfribidi0` are installed from Debian, which
  provides their source.

The GPL requires that a recipient be able to replace the LGPL libraries: they are separate files (`mpv-2.dll`,
`fribidi-0.dll`, `vulkan-1.dll`, `libfribidi.dylib`) and are not statically linked into the application.

## Other bundled material

* **FriBiDi** (LGPL-2.1-or-later), built from pinned source by `tools/build_win_fribidi.py`.
* **Khronos Vulkan-Loader** (Apache-2.0, with its NOTICE), built from pinned source by
  `tools/build_win_vulkan_loader.py`.
* **PyInstaller bootloader**: GPL-2.0-or-later with the PyInstaller exception that permits distributing a program
  built with it under other terms; the application itself remains GPL-3.0-or-later.
* **Shader pack** ([iwalton3/default-shader-pack](https://github.com/iwalton3/default-shader-pack)): a collection of
  third-party mpv shaders under several licences; its own `LICENSE.md` lists them and is installed with it
  (`shader-pack-LICENSE.md` in the `.deb`). The `.deb` build pins a release tag and the hash of its contents
  (`linux/build-deb.sh`); the other platforms still take the latest release at build time, so **verify** that file for
  each of those releases.
* **`trickplay-osc.lua`** is adapted from mpv's on-screen controller (LGPL-2.1-or-later), patches marked in the file;
  **`thumbfast.lua`** answers the thumbfast message interface (thumbfast is by po5, MPL-2.0); the file carries no third-party
  header, so **verify** whether any of it was copied from thumbfast and, if so, add that notice;
  the **Flatpak** also installs `mpv_inhibit_gnome` (see its manifest module for the licence).
* **Themes** under `jellyfin_mpv_shim/themes/` are colour tables; `elegance.json` is inspired by ElegantFin and the
  `jf-*` themes by the Jellyfin web client's own themes (**verify** that no third-party text or artwork is copied).
* **Fonts**: none are bundled. Text is drawn with the system's fonts (fontconfig on Linux).
* **Icon and logo**: `jellyfin.ico`, `jellyfin.icns`, `jellyfin_mpv_shim/integration/jellyfin-*.png` and `logo.png`
  derive from the Jellyfin logo. Jellyfin's artwork is published under its own licence (Creative Commons
  BY-SA at the time of writing - **verify** at [jellyfin/jellyfin-ux](https://github.com/jellyfin/jellyfin-ux)), and the
  name and logo are Jellyfin's. This fork is independent; see "Release blockers" in `docs/packaging.md` about
  giving the fork an icon of its own before wider distribution.
