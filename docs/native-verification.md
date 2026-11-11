# Native verification, signing and what is still missing

Packaging was prepared and partly built **on one Debian 13 amd64 laptop**. This file is the honest ledger of what that
does and does not show, the short checklist for each platform, and what signing needs. Nothing here has been
published, pushed or run in CI.

## Status ledger

| | Packaging implemented | Artifact built | Installed + launched on the target | Playback tested on the target |
|---|---|---|---|---|
| **Debian 13 amd64 `.deb`** | yes (`linux/`) | **yes**, reproducible (two builds, identical SHA-256) | **no**: unpacked and run headlessly only (below); not installed with apt, no desktop window opened | **no** for the package; the same app code and libmpv 0.40.0 were played with `vo=gpu` earlier on this machine from a private venv |
| **Flatpak** | yes (existing manifest, unchanged) | **no**: needs `flatpak-builder`, the GNOME 51 SDK/Platform (only Platform 49 is here) and a from-source mpv build | no | no |
| **Windows x64 / ARM64 installer** | yes (Inno Setup + PyInstaller, existing; notices and uninstall icon added) | **no**: cannot be built on Linux | no | no |
| **macOS arm64 / x86_64 app + DMG** | yes (identity corrected this session) | **no** | no | no |

What "run headlessly" means for the `.deb`: `dpkg-deb -x` into a temp directory, then `usr/bin/jellyhome-player
--version` and `--help`, and a Python session using the launcher's own interpreter flags (`-sE`) that imported every
bundled library from the package (not from `~/.local`), confirmed Pillow 12.3.0 with Raqm, FriBiDi and HarfBuzz, loaded
the system libmpv 0.40.0 through the bundled python-mpv and created and destroyed a `vo=null` instance.

### The one window launch that was done

With permission, the unpacked launcher was started once in a throw-away config directory **with networking disabled**
(`unshare -rn`) inside `limited_run.sh`. Observed in the log: libmpv 0.40.0 backend, mpv window created, MPRIS registered
under `org.mpris.MediaPlayer2.io.github.braisravina.JellyHomePlayer`, `no servers configured; showing login` (the
first-run screen), text shaping `raqm=yes`, and the sign-in form was used by hand (the attempts failed with "server
unreachable", as intended). The scope was then stopped by `limited_run.sh`: the app logged each shutdown step and
`Shutdown complete.` within about half a second and nothing was left. **Not covered by that run**: the app's own
`stop` command (my driver's socket path was longer than the 107-byte unix-socket limit, so it never connected and the
scripted screenshot and `stop` did not happen), playback, the menu entry, and a real `apt install`.
Also seen there: `System tray is unavailable` (expected without `python3-pystray`/AppIndicator) and `This mpv cannot give
up its window on request (needs 0.41+); minimizing will quit mpv instead`: **Debian 13 ships libmpv 0.40**, so on this
package minimizing the player window quits the player. That is a known limit of the platform's libmpv, listed below.

## Linux (Debian 13, amd64) - five minutes, on your laptop

```sh
linux/build-deb.sh                       # or use the one already built: publish/Linux/
sha256sum -c publish/Linux/jellyhome-player_3.3.0-1_amd64.deb.sha256
sudo apt install ./publish/Linux/jellyhome-player_3.3.0-1_amd64.deb
```

1. The application menu has **JellyHome Player** with the Jellyfin icon (log out/in if the menu does not refresh).
2. Open it from the menu, not a terminal: the first-run sign-in form appears in the mpv window. Sign in.
3. Play something that should Direct Play; confirm playback, pause, seek, close. Closing the window must leave no
   process: `pgrep -af jellyhome` prints nothing a few seconds later.
4. Settings → General → *Start With Computer* is **off**. Turn it on and off: `~/.config/autostart/` gains and loses
   `io.github.braisravina.JellyHomePlayer.desktop`, whose `Exec=` is `/usr/bin/jellyhome-player --minimized`.
5. Error path (optional): `sudo apt remove libmpv2` is not offered while the package depends on it, so this cannot be
   forced from the menu; the dialog text is unit-tested in `tests/test_startup_error.py`.
6. `sudo apt remove jellyhome-player`: the menu entry disappears; `~/.config/jellyhome-player` is still there.

## Windows (x64 and ARM64) - needs a Windows machine or the CI artifact

Build: the `build-win64` / `build-win-arm64` jobs of `.github/workflows/main.yml`, or locally per `docs/packaging.md`.

1. Run `jellyhome-player_<version>_installer.exe` as a **standard user** (no UAC prompt is the pass condition).
2. Start menu has **JellyHome Player**; the desktop shortcut exists only if ticked; the app launches from the final page.
3. First run: sign-in form appears. Sign in, Direct Play something, close the window; Task Manager shows no
   `JellyHomePlayer.exe` or `mpv` afterwards.
4. Provoke the start-up error path on a VM without a GPU driver: a dialog should name the graphics driver (this is the
   Vulkan-loader failure the project has hit before; the loader is bundled, so a normal machine will not show it).
5. Upgrade: install an older build, sign in, install the newer one over it: settings and login survive
   (`%APPDATA%\jellyhome-player` is not touched by the installer).
6. Uninstall from Settings → Apps: program, Start menu entry and any "Start with computer" value are gone; the
   settings folders stay by design.
7. On ARM64: `python tools/check_win_arch.py` is run by CI; also confirm Task Manager shows the process as ARM64.

## macOS (arm64 and x86_64, macOS 15 or newer) - needs a Mac

Build: the `build-macos` job (it now also asserts the architecture, bundle id, signature and `--version`), or
`macos/README.md` locally. **Two DMGs, one per CPU; no universal binary.**

1. Open the DMG, drag **JellyHome Player** to Applications, open it. A downloaded, unnotarized app is blocked once:
   System Settings → Privacy & Security → *Open Anyway*.
2. Confirm the first-run sign-in appears and that Direct Play works; close and check Activity Monitor for leftovers.
3. **Check the Dock/menu-bar identity**: `LSUIElement` is set, and the video window belongs to the bundled `mpv.app`
   helper, so the Dock may show mpv's icon and name rather than JellyHome Player's. This is an identity issue that cannot
   be assessed from Linux; if it shows, the fix is in `macos/build.sh`, not the app.
4. `lipo -archs "/Applications/JellyHome Player.app/Contents/MacOS/JellyHome Player"` equals the DMG's architecture.
5. Delete the app: `~/Library/Application Support/jellyhome-player` and `~/Library/Caches/jellyhome-player` remain.

## Signing, notarization and credentials still needed

None of these exist in the repository, and **no secret belongs in it**; they would be CI repository secrets.

| Platform | Needed | Without it |
|---|---|---|
| Windows | An OV/EV code-signing certificate, or Azure Trusted Signing, in the publisher's name; a `signtool` step before Inno Setup compresses the app and again on the installer; set `AppPublisher` in the `.iss` (currently "JellyHome Player contributors") | SmartScreen shows "unrecognized app" |
| macOS | Apple Developer Program membership; a **Developer ID Application** certificate; an App Store Connect API key for `notarytool`; sign every Mach-O inside out with `--options runtime --timestamp`, entitlements `com.apple.security.cs.allow-jit` (mpv's LuaJIT) and possibly `allow-unsigned-executable-memory` (ctypes); notarize and staple the DMG (`macos/README.md` has the steps) | Gatekeeper blocks first launch |
| Linux `.deb` | A GPG key to sign an apt repository (only if you host one); a lone `.deb` needs none | `apt` installs a local file without verifying a signature |
| Flatpak | A Flathub submission (their own signing); a self-hosted repo needs a GPG key | local bundles install unsigned |

## Release blockers (honest list)

1. **No platform has been installed-and-launched from its final package by anyone** except the headless unpack above.
   Windows and macOS have not been built at all from this tree. A first CI run is the next step, and then a pass of the
   checklists above on real machines.
2. **The icon and name are Jellyfin's.** The fork ships Jellyfin's logo as its icon (`jellyfin.ico`, `.icns`,
   `jellyfin-*.png`). Jellyfin's artwork has its own licence and the name is theirs; a fork that wants to be distributed
   beyond a personal machine should get an icon of its own. `THIRD_PARTY_NOTICES.md` flags this.
3. **Bundled mpv builds need their own licence/source files verified** per release (`THIRD_PARTY_NOTICES.md`, items
   marked *verify*): shinchiro's Windows archives and mpv-player's macOS archives are GPL builds made by others.
4. **Unsigned installers** (above).
5. **Debian 13's libmpv is 0.40**; the app wants 0.41+ for "give up the window on minimize", so on the `.deb`
   minimizing the player quits it. (The Flatpak builds mpv from a pinned master commit and the Windows/macOS builds bundle
   0.41+, so they do not have this limit.)
6. **The `.deb` is Debian 13 amd64 only** (CPython 3.13 wheels; `python3 (>= 3.13), (<< 3.14)`). Ubuntu 24.04 and
   Debian 12 users need the Flatpak, which has not been built here.
7. **The shader pack is "latest at build time"** for every platform (`gen_pkg.sh`); only the `.deb` script records the
   tag and accepts `SHADER_PACK_TAG` to pin it. Releases should pin it.
8. **Maintainer address** in the `.deb` control file is a placeholder no-reply address; set a real one.
9. **Startup errors on Windows and macOS** use a native dialog (`MessageBoxW`, `osascript`) that is unit-tested only for
   the text it builds and the command it chooses; the dialogs themselves have not been seen.
