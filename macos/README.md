# macOS (DMG)

`build.sh` builds a PyInstaller app from the working tree, with mpv inside it,
and packs it into a DMG. CI runs it in the `build-macos` job for arm64 and
x86_64.

## Building

```sh
pip install '.[all]' pyinstaller dmgbuild meson ninja
./gen_pkg.sh --skip-build                          # fetches the shader pack
MACOSX_DEPLOYMENT_TARGET=15.0 python tools/build_win_fribidi.py --output-dir macos
curl -Lo macos/mpv.zip https://github.com/mpv-player/mpv/releases/download/v0.41.0/mpv-v0.41.0-macos-15-arm.zip
unzip -o macos/mpv.zip -d macos && tar xzf macos/mpv.tar.gz -C macos
./macos/build.sh
./artifacts.sh macos arm64   # -> publish/macOS/jellyfin-mpv-shim_<version>_macOS_arm64.dmg
```

Run the commands from the repository root. Use the `macos-15-intel` mpv asset
for an Intel build. The downloads, `macos/mpv.app` and
`macos/libfribidi.0.dylib` are gitignored.

Do not run the test suite on a Mac desktop. There is no `xvfb-run`, so it opens
real mpv windows.

## Inputs

- **mpv** is upstream's own macOS build from the
  [mpv releases](https://github.com/mpv-player/mpv/releases), pinned like the
  Windows jobs' mpv. Homebrew's mpv cannot be bundled: it loads its libraries
  from `/opt/homebrew`, built for the machine that installed them. Upstream's
  carries them inside `mpv.app`.
- **FriBiDi** is built from the same pinned source as the Windows one. A Mac
  has none unless Homebrew installed it, and without it Pillow draws right to
  left text backwards and kerns nothing (`docs/packaging.md` section 1).

## What is in this folder

| File | Purpose |
|---|---|
| `build.sh` | The build. Everything macOS specific happens here, at build time. |
| `pyi_rth_macos.py` | PyInstaller runtime hook. Preloads the bundled FriBiDi, which must be named `libfribidi.dylib` because that is the bare name Pillow dlopens. Fails `import mpv` with `OSError`, so the app takes its existing external mpv path. |
| `jellyfin.icon` | The app icon for macOS 26, from the official Jellyfin SVG. macOS 26 shows `jellyfin.icns` on a gray platter because its corners are not Apple's shape. Compiling it needs Xcode 26, otherwise the build warns and keeps `jellyfin.icns`. |
| `dmg_settings.py` | The DMG window for [dmgbuild](https://github.com/dmgbuild/dmgbuild): size, icon positions and disk icon. |
| `systray.png` | The menu bar icon: the tray artwork in white, scaled to native menu bar size, since pystray stretches the image to the full bar height. Shipped in place of `jellyfin_mpv_shim/systray.png`. |
| `background.png`, `background@2x.png` | The DMG window background. Its arrow must line up with the icon positions in `dmg_settings.py`. It is light because Finder draws icon labels black in light mode. |

The libmpv binding is excluded from the bundle because PyInstaller would
otherwise pack the build machine's libmpv. macOS always uses an external mpv.

`build.sh` also sets `LSUIElement` so the app has no Dock icon of its own.
mpv's window has one.

## Minimum macOS

`build.sh` reads the minimum macOS off every binary in the bundle and writes
the highest into `Info.plist`. In CI that is the mpv asset's, 15.0 for both
architectures. FriBiDi is built with `MACOSX_DEPLOYMENT_TARGET` so it does not
raise it. A Homebrew Python does, to the build machine's macOS, so a DMG for
other Macs should come from CI or a python.org Python.

## Signing and Gatekeeper

The app is signed ad hoc and the DMG is not notarized. A downloaded copy is
blocked on first launch with "Apple could not verify…". To open it, try once,
then use **System Settings > Privacy & Security > Open Anyway**. Or clear the
quarantine flag:

```sh
xattr -dr com.apple.quarantine "/Applications/Jellyfin MPV Shim.app"
```

A DMG built on the same Mac is not quarantined and opens directly.

To remove the prompt for everyone, the app has to be signed with a Developer ID
and notarized:

1. Join the Apple Developer Program, then create a **Developer ID
   Application** certificate and an App Store Connect API key.
2. In `build.sh`, replace the ad hoc `codesign` with signing inside out: each
   dylib in `Contents/Frameworks`, then `Contents/Helpers/mpv.app`, then the
   app. Use `--options runtime --timestamp --sign "Developer ID Application:
   …"` and not `--deep`. mpv needs the `com.apple.security.cs.allow-jit`
   entitlement for LuaJIT. The Python side may need
   `com.apple.security.cs.allow-unsigned-executable-memory` for ctypes
   callbacks.
3. Sign the DMG, then `xcrun notarytool submit <dmg> --wait` and
   `xcrun stapler staple <dmg>`.
4. In CI, store the certificate, its password and the API key as secrets and
   import them into a temporary keychain before `build.sh`.

The signing identity is the publisher every user sees, so it should belong to
the project rather than to one contributor.
