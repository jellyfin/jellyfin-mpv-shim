# Installing, upgrading and uninstalling JellyHome Player

JellyHome Player is a fork of Jellyfin MPV Shim and installs **beside** it: a different config directory, device
id, installer id, desktop-entry id, single-instance lock and executable name.

## Windows

1. Download `jellyhome-player_<version>_installer.exe` (or the `_ARM64_` / `_LEGACY64_` variant for ARM PCs or
   CPUs without x86-64-v3) from this repository's Releases page.
2. Run it. It installs **for the current user, without administrator rights**, to
   `%LOCALAPPDATA%\Programs\JellyHome Player` (choose "all users" in the dialog if you prefer). Tick *Create a
   desktop shortcut* if wanted; leave *Launch JellyHome Player* ticked to start it.
3. Upgrade: run a newer installer. It asks (does not force) to close JellyHome Player if it is running, and only
   considers `JellyHomePlayer.exe`/`mpv-2.dll` in its own folder, so other programs are never closed.
4. Uninstall: Settings → Apps → JellyHome Player. This removes the program and its "Start with computer" entry; your
   settings in `%APPDATA%\jellyhome-player` are left (delete the folder to remove saved logins).

Builds are **unsigned** unless a release says otherwise, so SmartScreen may warn. Check the download source;
do not disable Windows security features. Code signing needs a certificate issued to the publisher (an OV/EV
certificate or a service such as Azure Trusted Signing); none is configured in this repository and no secret
belongs in it. Add a signing step to CI using repository secrets once a publisher identity exists.

## Linux

**Debian 13 (trixie), amd64: a `.deb` is built by `linux/build-deb.sh`.** It installs an application-menu launcher
("JellyHome Player", Video category) with the app icon, the `jellyhome-player` command and the licence notices. It needs
no Python installation or `pip` from you: apt pulls in `python3` (3.13), `libmpv2` and `libfribidi0`, and everything else
(the Jellyfin client library, Pillow, requests, ...) is bundled and hash-pinned.

```sh
linux/build-deb.sh                      # -> publish/Linux/jellyhome-player_<version>-1_amd64.deb (+ .sha256, manifest)
sudo apt install ./publish/Linux/jellyhome-player_3.3.0-1_amd64.deb     # apt resolves the dependencies
# open "JellyHome Player" from the application menu, or run: jellyhome-player
sudo apt remove jellyhome-player        # removes the program, menu entry and icons; your settings stay
```

* **Settings, logins and caches** are per user: `~/.config/jellyhome-player` and `~/.cache/jellyhome-player` (or
  `$XDG_CONFIG_HOME` / `$XDG_CACHE_HOME`). The package never creates, reads or removes them, so an upgrade keeps your
  settings and `apt remove`/`purge` leaves them; delete the two folders to forget saved logins.
* **Autostart** is not installed. *Start With Computer* in Settings (off by default) writes
  `~/.config/autostart/io.github.braisravina.JellyHomePlayer.desktop` pointing at the packaged launcher.
* **Startup errors**: launched from the menu there is no terminal, so a fatal startup error is shown in a dialog
  (needs `zenity` or `kdialog`, both recommended) with the one thing to try and the log location.
* **mpv version**: Debian 13 provides libmpv 0.40; the package asks only for 0.38 or newer, the oldest the app supports.
  The app prefers 0.41+, which can give up its window without restarting. With 0.40, hiding the window to the tray
  quits the mpv instance instead (the log says so at start-up); the app itself keeps running as a cast target and opens a
  new window when you play something or use the tray. Everything else was unaffected in what was tried.
* **Fonts**: text is drawn with system fonts. The package recommends `fonts-dejavu-core` or `fonts-noto-core`; without any
  font the app falls back to a small built-in one. For Chinese/Japanese/Korean titles and emoji install `fonts-noto-cjk`
  and `fonts-noto-color-emoji` (suggested).
* **Other Debian-family releases** (Ubuntu, Debian 12) carry a different Python and are **not** supported by this
  package: its dependency is `python3 (>= 3.13), (<< 3.14)` because the bundled Pillow and charset-normalizer are
  CPython 3.13 wheels. Use the Flatpak below.

**Flatpak** (the route for other distributions): the manifest `flatpak/io.github.braisravina.JellyHomePlayer.json` is
complete and is built by CI for amd64 and arm64, but it has **not been built or run on the machine this packaging was
prepared on** (it needs `flatpak-builder`, the GNOME 51 runtime and SDK, and a from-source build of mpv). To build it:

```sh
git clone --depth=1 https://github.com/flathub/shared-modules.git flatpak/shared-modules
./gen_pkg.sh --skip-build
flatpak-builder build flatpak/io.github.braisravina.JellyHomePlayer.json --force-clean --repo=repo --user
flatpak build-bundle repo jellyhome-player.flatpak io.github.braisravina.JellyHomePlayer
flatpak install --user jellyhome-player.flatpak      # adds the application-menu launcher
flatpak uninstall --user io.github.braisravina.JellyHomePlayer   # add --delete-data to remove settings
```

Sandbox: network, display (Wayland/X11 fallback), DRI, PulseAudio/PipeWire, `--device=all` (game controllers
need it; remove the line if you do not use one), and the GNOME session manager (inhibit screen blanking). No home
or host-filesystem access. "Start with computer" is refused inside the Flatpak (the app says why).
`import-upstream` has no Flatpak-specific handling: the sandbox cannot see the host's upstream settings folder, so it is
expected to find nothing there (not tested inside a Flatpak).

Or from a checkout with `libmpv`: `pip3 install '.[all]'` then `jellyhome-player`.

### Local test build on Debian (Python wheel in a private virtualenv)

This produces a **Python package** (wheel + sdist), not a Flatpak; the Flatpak above was not built on the machine this
was tested on (no `flatpak-builder`) and is unverified. `./gen_pkg.sh --skip-build` compiles the translations, rewrites
the tracked `jellyfin_mpv_shim/locale_index.py` and downloads the shader pack from GitHub into the checkout (it is not
gitignored), so build in a throwaway copy of the committed files:

    copy=$(mktemp -d) && git ls-files -z | xargs -0 cp --parents -t "$copy" && cd "$copy"
    ./gen_pkg.sh --skip-build          # needs network access to github.com
    python3 -m pip install build && python3 -m build --outdir dist .

Install it into a virtualenv and run it with its settings, logins and cache in a directory of its own (nothing under
`~/.config`, and no autostart entry is created). It needs a libmpv: `sudo apt install libmpv2` (plus `mpv` for the
external backend). Pillow is pinned to 12.3.0, the version this was tested with; without the pin pip takes the newest
release (13.0.0 when this was written), which was not tested.

    python3 -m venv test-venv
    test-venv/bin/pip install "pillow==12.3.0" dist/jellyhome_player-3.3.0-py3-none-any.whl
    state=$PWD/test-state
    export XDG_CONFIG_HOME=$state/config XDG_DATA_HOME=$state/data XDG_CACHE_HOME=$state/cache
    test-venv/bin/jellyhome-player            # opens one window
    test-venv/bin/jellyhome-player stop       # or close the window
    rm -r test-venv test-state dist "$copy"    # remove everything

The optional tray (`pystray`, which needs `python3-gi` and an AppIndicator) and MPRIS (`python3-gi`) are not installed in
this virtualenv; the app logs that they are unavailable and carries on. What was and was not checked about a build made
this way is in `docs/testing.md` ("Local Debian test build").

## macOS

`macos/build.sh` builds `JellyHome Player.app` (bundle id `io.github.braisravina.JellyHomePlayer`) with Python and
upstream's mpv inside it, and packs `jellyhome-player_<version>_macOS_<arch>.dmg`. **One DMG per CPU, no universal
binary**: `arm64` (Apple silicon) and `x86_64` (Intel). Both are built by CI against macOS 15.0 as the deployment
target, so **macOS 15 (Sequoia) or newer** is required on either; the build reads the real minimum off every bundled
binary and writes it to `LSMinimumSystemVersion`. Open the DMG, drag the app to *Applications*, open it from there.
The first launch of a downloaded copy is blocked until it is allowed in *System Settings → Privacy & Security* (ad hoc
signature, not notarized: `docs/native-verification.md` lists what notarization needs). Settings, logins and caches are in
`~/Library/Application Support/jellyhome-player` and `~/Library/Caches/jellyhome-player`; deleting the app leaves them.
**Nothing here has been built or run on a Mac** (this was prepared on Linux).

## Coexistence and migration

Both apps can run at once; they share nothing. To copy preferences from an existing upstream install:
`jellyhome-player import-upstream` (copies `conf.json` preferences, `mpv.conf`, `input.conf`; never logins,
tokens, device id or downloads; never overwrites). Then sign in (Quick Connect is offered when your server
has it enabled).

## Deployment-specific server address

Set `DEFAULT_SERVER_URL` in `jellyfin_mpv_shim/constants.py` (must be `https://`, no credentials) to pre-fill the
sign-in form; it stays editable and is never connected to automatically. It is empty in this repository.

## Start with computer / keep running for casting

Settings → General → *Start With Computer* (off by default; Windows and non-Flatpak Linux). *Close to Tray (keep
running)* is the "keep running for casting" switch: closing the window then hides it to the tray and the app stays
a cast target; *Quit* in the tray menu (or `jellyhome-player stop`) exits. With no tray, use *Keep Running in
Background*.
