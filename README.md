# JellyHome Player

An MPV-based Jellyfin desktop client for easy home streaming, with privacy-focused defaults and simple setup. Fork of Jellyfin MPV Shim.

> **JellyHome Player is an independent fork of [Jellyfin MPV Shim](https://github.com/jellyfin/jellyfin-mpv-shim)** by Izzie Walton and contributors, licensed GPL-3.0-or-later (see [LICENSE.md](LICENSE.md)). It is not an official Jellyfin project and is not endorsed by Jellyfin. The Python package keeps the upstream name (`jellyfin_mpv_shim`) so upstream changes can be merged. See [docs/privacy.md](docs/privacy.md) for exactly what this fork does and does not contact, and [docs/implementation-summary.md](docs/implementation-summary.md) for what differs from upstream.

What is different from upstream:

- **No third-party lookups.** The public-IP request to `checkip.amazonaws.com` is gone. Whether a server is "Local" or "Remote" (a bandwidth choice only) is decided from the server's address and the Jellyfin server itself, with a per-server **Auto / Local / Remote** override.
- **No background update checks by default.** Opt in under Settings; **Check for Updates Now** works on demand.
- **Its own identity**, so it installs and runs beside upstream without sharing settings, logins or the running instance.
- **Per-user Windows installer** (no administrator rights) and an optional, off-by-default **Start With Computer**.
- Discord Rich Presence stays **off** unless you turn it on.

## Features

- **Uncompromising Playback**: MPV's rendering and video pipelines are completely untouched. It's not redirected, broken, or embedded in a toolkit. Native HDR and Dolby Vision are supported on Windows and Linux.
- **Browse and play your entire library** including films, shows, music, photos, [Live TV](#live-tv)
  and [books, comics and audiobooks](#books-comics-and-audiobooks).
- **Offline sync**: download media for offline viewing, automatically if desired.
- **Cast target**: Fully controllable from Jellyfin web and mobile apps.
- **SyncPlay**: Watch media with friends, without screen shares or awkward syncing.
- **Smart Subtitles**: No need to set for every episode, they're remembered by default and
  [can be forced globally](docs/configuration.md#language-config-power-user).
- **Video Enhancement**: [video upscaling](#shader-packs-and-svp), debanding, and motion interpolation.
- **Advanced Audio**: Audio passthrough and night mode so you can actually hear people talking.
- **Seamless User & Server Switching** via [fast user switching](#fast-user-switching) and
  optional PINs.
- Keyboard, mouse, Jellyfin remote and game controller support, and all of
  [MPV's shortcuts](#keyboard-shortcuts).
- MPRIS media controls on Linux, Discord Rich Presence, [shell
  commands](docs/configuration.md#shell-command-triggers) on playback events, and an
  [external MPV](docs/configuration.md#external-mpv) if you prefer your own.

## Getting started

Install it (below), launch it and log in. Enter your server's address, e.g.
`http://192.168.1.10:8096` or `https://jellyfin.example.com`, including any subpath; port
8096 is assumed for plain `http://` addresses (a home-network address with no scheme gets `http://…:8096`; any other address typed without a scheme gets `https://`), so a server on port 80 needs `:80`. Quick Connect works
too.

Then browse from the window, or cast to it from another Jellyfin app. When a system tray is
available, closing the window keeps it running there as a cast target; you can change that in
Settings.

### Windows

Download the installer from this fork's [Releases](https://github.com/braisravina/jellyhome-player/releases)
(once a release has been published there; see [docs/installation.md](docs/installation.md)).
It installs for your user without administrator rights, adds a Start-menu shortcut (desktop
shortcut optional) and can launch the app when it finishes. MPV is bundled; you do not need
Python or MPV. There are `ARM64` builds, and `LEGACY64` builds for CPUs without x86-64-v3
support (roughly pre-2013). Builds are **unsigned** unless a release says otherwise, so
Windows SmartScreen may show an "unrecognized app" screen; verify the download came from the
releases page above rather than turning off any protection.

### Linux

On **Debian 13 (amd64)** build a `.deb` with `linux/build-deb.sh` and install it with
`sudo apt install ./publish/Linux/jellyhome-player_*.deb`: it adds a "JellyHome Player" entry to the application menu,
needs no Python or pip steps from you, and keeps your settings when removed or upgraded. A Flatpak can also be built
from this repository (`flatpak/`); neither is published, and Flathub does not carry this fork. See
[docs/installation.md](docs/installation.md). To install with pip from a checkout you need `libmpv` (or
`mpv`). **MPV 0.41 or newer is recommended**:

```bash
pip3 install --upgrade .
```

Optional extras:

- **Tray icon**: keeps it running in the background once the window closes (or enable
  **Keep Running in Background** in Settings instead):
  `pip3 install '.[systray]'`, plus GObject and AppIndicator from your
  distribution (on Debian: `python3-gi gir1.2-ayatanaappindicator3-0.1`). The virtualenv must
  have `--system-site-packages`; with pipx,
  `pipx install --system-site-packages 'jellyhome-player[systray]'`.
- **Discord Rich Presence**: `pip3 install '.[discord]'`, then enable it in
  Settings.

With MPV older than 0.41, minimizing to the tray closes MPV rather than hiding its window, and
copy/paste in text fields needs `wl-clipboard` (Wayland) or `xclip`/`xsel` (X11).

If your distribution's MPV is old, [mpv-build](https://github.com/mpv-player/mpv-build) builds
a current one with `libmpv` by default; run `sudo ldconfig` afterwards. Add
`-Dsdl2-gamepad=enabled` to its `mpv_options` for controller support.

### macOS

MacOS is not officially supported or released by this project, but unsigned builds are provided on a
best-effort basis via CI. If anything breaks, feel free to to submit a PR (AI is allowed subject to review) to fix it.

No macOS builds of this fork are published. Build one yourself from a checkout (`setup-mac.py`),
then, since it is unsigned, use **System Settings > Privacy & Security > Open Anyway**
to run the application or use the following command in your terminal to allow it:

```bash
xattr -dr com.apple.quarantine "/Applications/JellyHome Player.app"
```

Or install from a checkout:

```bash
brew install python mpv pipx
pipx ensurepath
pipx install '.[systray]'   # drop [systray] if you don't want the menu bar icon
jellyhome-player
```

## Using it

### Controls and remotes

Use the mouse, or the arrow keys, Enter and Esc. The mouse's thumb buttons go back and forward
a page; right-click **Back** (or **Home** at the top level) to jump anywhere in the history.

From a Jellyfin remote, use the arrows, OK, Back and Home. **Menu** opens the context menu of
the selected item (Play, Queue, Mark Watched, Favorite, Download) or, during playback, the
player's settings menu. **Search** jumps to the search box. Opening a film or episode lands
on its Play button, so OK twice plays it.

During playback the controls have audio, subtitle and quality pickers, and the gear menu (or
`c`) holds speed, aspect, shader profiles, subtitle style, SyncPlay and screenshots.

### Keyboard shortcuts

Most of [MPV's keyboard shortcuts](https://mpv.io/manual/stable/#interactive-control) work.
The app adds:

| Key | Action | | Key | Action |
|---|---|---|---|---|
| `<` `>` | Previous / next item | | `c` | Player settings menu |
| `q` | Stop | | `k` | Unload the shader profile |
| `w` | Mark watched and skip | | `u` | Mark unwatched and stop |

And the MPV ones you are most likely to want: `space` pause, `←`/`→` seek 5 s, `↑`/`↓` seek
1 min, `f` fullscreen, `m` mute, `s` / `S` screenshot with / without subtitles, `,` `.` step a
frame, `[` `]` speed ±10%, `backspace` reset speed, `d` deinterlace, `Ctrl+Shift+←/→`
subtitle delay.

All of these can be changed. See [Keyboard shortcuts](docs/configuration.md#keyboard-shortcuts)
and [`input.conf`](docs/configuration.md#mpv-configuration).

### Live TV

With a tuner on your server, **Live TV** appears with your libraries, with the web client's
six screens: **Programs**, **Guide**, **Channels**, **Recordings**, **Schedule** and
**Series** (recording rules). Right-click any programme to record it or the series.
**Guide Settings** are stored on the server, shared with the web client.

The guide shows one window of time and pages with the arrows, rather than scrolling sideways
through 24 hours.

### Books, comics and audiobooks

Books libraries browse by folder, as in the web client.

- **Epub** opens in a built-in reader with adjustable size, page colour and justification.
- **Comics** (`cbz`, `cbt`) open in a built-in reader with fit-width and fit-page zoom.
- **Other formats** (`pdf`, `mobi`, `azw3`, `cbr`, `cb7`) open in your desktop's own
  application.
- **Audiobooks** play like music, with chapter navigation, skip back 10 s / forward 30 s, and
  chapter marks on the seek bar. Resume continues from where you stopped; **Play from
  Beginning** is a separate button so you don't lose your place by accident.

The built-in readers save your position to the server as you turn pages, in the same form the
web client uses, so you can carry on elsewhere. For formats read outside the app, use
**Progress…** on the book's page to pull or push your page.

Jellyfin can only hand over a book as a whole file, so reading downloads it first, and **your
account needs the "Allow media downloads" permission** to read books at all.

### Fast user switching

Several people can share one player, each with their own servers and logins. In **Settings →
Servers**, **Add User**, switch to it and add its servers. Once there is more than one user, a
user picker appears beside the server picker in the top bar.

- Each user gets its own device identity, so two users on the same server don't share a session.
- **Set PIN** locks a user; optionally require it at startup too. This is a parental-control
  convenience, **not** security. Nothing is encrypted (although PINs are hashed).

### Cast-target mode (`headless`)

For a machine that should *only* be a cast target, such as a shared TV, set `headless` to
`true` in the config file. It then shows "Ready to cast" instead of the library: casting,
playback and the player controls work; browsing, search and settings are not reachable from
the machine.

**This is not a security feature.** It stops casual use, not anyone with access to the config
file or the tray menu.

### Shader packs and SVP

The bundled [shader pack](https://github.com/iwalton3/default-shader-pack) offers upscaling and
enhancement profiles (FSRCNNX, Anime4K). Choose one from the player's gear menu under **Change Video
Playback Profile**. If video breaks, pick a different **Graphics API for Shaders** in Settings →
Playback → Video Enhancement; `opengl` is the most compatible. If the app won't start after
choosing a profile, launch it once with `--reset-shaders`; if the picture is merely garbled,
press `k`.

[SVP](https://www.svp-team.com/) can be controlled from the menu too; it needs some setup. See [SVP integration](docs/configuration.md#svp-integration). Please note that I don't have an SVP license (it used to be free on Linux), so the support is untested.

## Configuration

Almost everything is in **Settings**, which has a search box. The complete reference,
including options with no UI, is **[docs/configuration.md](docs/configuration.md)**, along with
where the config folder is (the tray icon can open it).

## Tips and troubleshooting

### Open on a specific monitor

In `mpv.conf` in the [config folder](docs/configuration.md#the-config-file), add `fs-screen=N`
(with `fs=yes` to open fullscreen), where `N` counts from 0. On Windows, the monitor number in
Display Settings minus one. If X11 reports your monitors as one screen, use `geometry=` with the
position `xrandr` shows instead, e.g. `geometry=1920x1080+0+0`. Restart the app after editing.

### Volume on the mouse wheel

Add to `input.conf`:

```
WHEEL_UP add volume 5
WHEEL_DOWN add volume -5
```

### Run multiple instances

`--config /path/to/folder` runs another, independent copy. Launching again with the *same*
folder just brings the running one forward. `jellyhome-player stop` (with the same
`--config`, if any) shuts an instance down cleanly.

### Arabic or Hebrew text drawn backwards

Some text is drawn with Pillow, which needs **FriBiDi** for right-to-left scripts and kerning;
without it nothing errors, but RTL titles come out reversed and captions are slightly
mis-measured. The Windows installer and the Flatpak include it. Elsewhere, check:

```bash
python3 -c "from PIL import features; print(features.check('raqm'))"
```

If this prints `False`, install FriBiDi (`libfribidi0` on Debian/Ubuntu, `fribidi` on Fedora
and Arch). If pip built Pillow from source rather than using a wheel, also install libraqm's
development package and run `pip3 install --force-reinstall --no-binary=pillow pillow`.

## Known issues

- Joining a SyncPlay group from a client that is casting to this one plays the media without
  enabling SyncPlay. Join from this player's own **SyncPlay** menu instead.
- Stopping playback leaves the group's *content*, not the group, as in the web client: **SyncPlay
  → Resume local playback** rejoins, and playing something else takes the group with you. Use
  **SyncPlay → Leave** to leave. SyncPlay can still be fragile — please report problems.
- Music has no gapless playback.
- A player counts as one device on a server; [fast user switching](#fast-user-switching) gives
  each local user its own.

## Development

Building, translating and testing: **[docs/development.md](docs/development.md)**.
