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

No package is published. Build the Flatpak locally:

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
