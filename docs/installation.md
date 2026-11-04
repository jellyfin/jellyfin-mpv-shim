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
4. Uninstall: Settings → Apps → JellyHome Player. This removes the program; your
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
or host-filesystem access.

Or from a checkout with `libmpv`: `pip3 install '.[all]'` then `jellyhome-player`.

## Coexistence and migration

Both apps can run at once; they share nothing.

## Deployment-specific server address

Set `DEFAULT_SERVER_URL` in `jellyfin_mpv_shim/constants.py` (must be `https://`, no credentials) to pre-fill the
sign-in form; it stays editable and is never connected to automatically. It is empty in this repository.
