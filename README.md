# Jellyfin MPV Shim

[![Current Release](https://img.shields.io/github/release/jellyfin/jellyfin-mpv-shim.svg)](https://github.com/jellyfin/jellyfin-mpv-shim/releases)
[![PyPI](https://img.shields.io/pypi/v/jellyfin-mpv-shim)](https://pypi.org/project/jellyfin-mpv-shim/)
[![Translation Status](https://translate.jellyfin.org/widgets/jellyfin/-/jellyfin-mpv-shim/svg-badge.svg)](https://translate.jellyfin.org/projects/jellyfin/jellyfin-mpv-shim/)

Jellyfin MPV Shim is a cross-platform client for Jellyfin. It can run in the background as a cast target or act as a fully-featured desktop client with offline sync support.

It has support for all your advanced media files without transcoding, as well as tons of
features which set it apart from other multimedia clients:

- Direct play most media using MPV.
- Watch videos with friends using SyncPlay.
- Offers a shim mode which runs in the background.
- The Jellyfin mobile apps can fully control the client.
- Prevents having to regularly change subtitles/audio settings for each episode.
- Supports all of the [MPV keyboard shortcuts](https://github.com/jellyfin/jellyfin-mpv-shim#keyboard-shortcuts).
- Enhance your video with [Shader Packs](https://github.com/jellyfin/jellyfin-mpv-shim#shader-packs) and [SVP Integration](https://github.com/jellyfin/jellyfin-mpv-shim#svp-integration).
- Optionally share your media activity with friends using Discord Rich Presence.
- Most features, as well as MPV itself, [can be extensively configured](docs/configuration.md).
- You can configure the player to use an [external MPV player](docs/configuration.md#external-mpv) of your choice.
- Chromecast-like [Display Mirroring](https://github.com/jellyfin/jellyfin-mpv-shim#display-mirroring), on by default.
- You can [trigger commands to run](docs/configuration.md#shell-command-triggers) when certain events happen.

To learn more, keep reading. This README explains everything, including [configuration](docs/configuration.md), [tips & tricks](https://github.com/jellyfin/jellyfin-mpv-shim#tips-and-tricks), and [development information](https://github.com/jellyfin/jellyfin-mpv-shim#development).

## Getting Started

If you are on Windows, simply [download the binary](https://github.com/jellyfin/jellyfin-mpv-shim/releases).
If you are using Linux, you can [install via flathub](https://flathub.org/apps/details/com.github.iwalton3.jellyfin-mpv-shim) or [install via pip](https://github.com/jellyfin/jellyfin-mpv-shim#linux-installation). If you are on macOS, see the [macOS Installation](https://github.com/jellyfin/jellyfin-mpv-shim#osx-installation)
section below.

To use the client, simply launch it and log into your Jellyfin server. You’ll need to enter the
URL to your server, for example `http://server_ip:8096` or `https://secure_domain`. Make sure to
include the subdirectory and port number if applicable. You can then cast your media
from another Jellyfin application.

If your account has no password (for example, users who sign in through an SSO provider), use
**Quick Connect** instead of typing a password. In the GUI, enter the server URL and click
**Quick Connect**; on the CLI, pass `--quick-connect` (optionally with `--server URL`). A code is
shown — open Jellyfin in a browser where you are already signed in, go to your user menu →
*Quick Connect*, and enter the code. Quick Connect must be enabled by an administrator on the
server.

The application runs with a notification icon by default, you can disable this in the settings page if
you would like the client to not run in the background and listen for casts from mobile/web clients.

Note: Due to the huge number of questions and issues that have been submitted about URLs, I now tolerate
bare IP addresses and not specifying the port by default. If you want to connect to port 80 instead of
8096, you must add the `:80` to the URL because `:8096` is now the default.

## Limitations

- Live TV is partially supported. The home screen's "On Now" row appears when your server has a
  tuner, and playing an entry from it tunes the channel. There is no channel guide, no Live TV
  library browsing and no DVR/recording management — for those, use the web client.
- A single active session still reports as one device to a given server. For sharing the player between
  people, see [Fast User Switching](#fast-user-switching), which keeps each local user on its own device
  identity. ([Related issue.](https://features.jellyfin.org/posts/319/mark-device-as-shared))

### Known Issues

Please note the following issues with controlling SyncPlay:

- If you attempt to join a SyncPlay group when casting to MPV Shim, it will play the media but it will not activate SyncPlay.
  - You can, however, proceed to activate SyncPlay [using the menu within MPV](https://github.com/jellyfin/jellyfin-mpv-shim#menu).
- If you would like to create a group or join a group for currently playing media, [use menu within MPV](https://github.com/jellyfin/jellyfin-mpv-shim#menu).
- SyncPlay can still be fragile. You may need to rejoin or even restart the client. Please report any issues you find.

Music playback works, but gapless playback is not planned at this time.

The shader packs feature is sensitive to graphics hardware. It may simply just not work on your computer.
You may be able to use the log files to get some more diagnostic information. If you're really unlucky,
you'll have to disable the feature by pressing `k` to restore basic functionality.
If you find the solution for your case, *please* send me any information you can provide, as every test case helps.

## Advanced Features

### Menu

Most of these are also reachable from the player UI's settings (gear) menu, which is usually
easier. This menu is the older text-based one, and it still covers a few things the player UI
does not.

To open the menu, press **c** on your computer or use the navigation controls
in the mobile/web app.

The menu enables you to:

- Adjust video transcoding quality.
- Change the default transcoder settings.
- Change subtitles or audio, while knowing the track names.
- Change subtitles or audio for an entire series at once.
- Mark the media as unwatched and quit.
- Enable and disable SyncPlay.
- Configure shader packs and SVP profiles.
- Take screenshots.

On your computer, use the mouse or arrow keys, enter, and escape to navigate.
On your phone, use the arrow buttons, ok, back, and home to navigate.

### Fast User Switching

The local library browser can hold several **users**, letting more than one person share the same
player without their servers, sessions, and remote-control state colliding. Jellyfin (and jellyfin-web)
has no built-in fast user switching; because this client owns its own UI, it can.

A *user* is a local grouping of one or more server logins that connect together. Only one user is active
at a time. Switching disconnects the active user's servers and connects the selected user's, then updates
the server selector.

- **Managing users** — open **Settings → Servers**. The existing server(s) are kept as a `(default)` user
  (which you can rename). Use **Add User** to create more, then **Switch** to a user and add its servers
  with the normal *Add a server* form (each user's servers are managed while that user is active). Any
  server address already used by another user is offered under *Previously added servers* with **Use** and
  **Quick Connect** shortcuts, so you don't retype URLs when provisioning a new account.
- **Switching** — a user drop-down appears to the left of the server selector in the top bar once you have
  more than one user. Pick a user to switch to it.
- **Separate device identity** — each non-default user gets its own Jellyfin device id (and a device name
  like `hostname (Kids)`), so two users logged into the *same* server don't fight over one server-side
  session. The `(default)` user keeps the original device id, so its existing sessions and tokens are
  untouched.
- **PIN protection (parental controls)** — a user can be given a PIN (**Set PIN**). Switching *into* a
  locked user always requires the PIN. You can additionally tick *Require this PIN at startup and when
  reopening the window*, which re-locks the browser whenever the app starts or the window is reopened from
  the tray, so a locked profile can't be resumed without the PIN. This is a parental-control convenience,
  **not** a security boundary — the PIN is only salted-hashed in the config, and the media itself is not
  encrypted.

The first time you close the browser window, you're asked whether closing should **Minimize to Tray**
(keep the app running as a cast target) or **Exit**. Your choice is remembered and can be changed later
via **Close to Tray (keep running)** in *Settings → Interface*.

Users are stored in `users.json` in the config folder (next to `cred.json`). On first run with this
feature, your existing `cred.json` is migrated into the `(default)` user automatically.

### Shader Packs

Shader packs let you use advanced video shaders and video quality settings without the
configuration they normally require. MPV Shim's default shader pack comes with
[FSRCNNX](https://github.com/igv/FSRCNN-TensorFlow) and [Anime4K](https://github.com/bloc97/Anime4K)
preconfigured. Try experimenting with video profiles! It may greatly improve your experience.

To use, navigate to the **Video Playback Profiles** option and select a profile.

Profiles leave your graphics API alone, so HDR output keeps working. If video breaks
when you load one, pick a different API under Settings → Video Enhancement → **Graphics
API for Shaders** (`shader_pack_gpu_api`); `opengl` is the most compatible.

For details on the shader settings, please see [default-shader-pack](https://github.com/iwalton3/default-shader-pack).
If you would like to customize the shader pack, there are details in the configuration section.

### SVP Integration

SVP integration allows you to easily configure SVP support, change profiles, and enable/disable
SVP without having to exit the player. It is not enabled by default, please see the configuration
instructions for instructions on how to enable it.

### Display Mirroring

Casting an item from another Jellyfin client shows it on your display before you play it,
similar to Chromecast. **This is on by default and needs no configuration** — the item's page
opens in the library browser, and you can drive it from there with the remote's arrow keys.
Casting never starts or interrupts playback; it only navigates.

### Cast-target mode (`headless`)

For a box that should *only* be a cast target — a TV in a shared space, say — set `headless`
to `true` in the config file. The player then shows a "Ready to cast" backdrop instead of the
library, and the library cannot be reached from the machine itself: no browsing, no search, no
settings, and no queue view. Casting, playback and the player controls all work normally,
including transport controls for music.

Closing the window in this mode keeps the app running and castable rather than exiting, with or
without a system tray — a cast target that quits when someone closes a window has stopped doing
its job. Set `close_to_tray` to `false` if you would rather it quit, and use
`jellyfin-mpv-shim stop` to shut one down.

**This is not a security feature.** It stops someone plugging in a mouse and playing random
things from your library, which is what it is for. It does not stop anyone with real access to
the machine: the config file is editable, and the systray menu still reaches Settings and the
log viewer. If you need the box genuinely locked down, use the operating system for that.

### Keyboard Shortcuts

This program supports most of the [keyboard shortcuts from MPV](https://mpv.io/manual/stable/#interactive-control). The custom keyboard shortcuts are:

- < > to skip episodes
- q to close player
- w to mark watched and skip
- u to mark unwatched and quit
- c to open the menu
- k disable shader packs

Here are the notable MPV keyboard shortcuts:

- space - Pause/Play
- left/right - Seek by 5 seconds
- up/down - Seek by 1 minute
- s - Take a screenshot
- S - Take a screenshot without subtitles
- f - Toggle fullscreen
- ,/. - Seek by individual frames
- \[/\] - Change video speed by 10%
- {/} - Change video speed by 50%
- backspace - Reset speed
- m - Mute
- d - Enable/disable deinterlace
- Ctrl+Shift+Left/Right - Adjust subtitle delay.

## Configuration

Most settings are editable in the app under **Settings**, so you rarely need to touch the
config file. The full list of options, including the ones with no UI, is in
**[docs/configuration.md](docs/configuration.md)**.

The config file lives in a per-platform folder — the systray icon can open it for you, and
the path is printed at startup on Linux and macOS. See
[the reference](docs/configuration.md) for the locations.

## Tips and Tricks

Various tips have been found that allow the media player to support special
functionality, albeit with more configuration required.

### Open on Specific Monitor (#19)

Please note: Edits to the `mpv.conf` will not take effect until you restart the application. You can open the config directory by using the menu option in the system tray icon.

**Option 1**: Select fullscreen output screen through MPV.
Determine which screen you would like MPV to show up on.

- If you are on Windows, right click the desktop and select "Display Settings". Take the monitor number and subtract one.
- If you are on Linux, run `xrandr`. The screen number is the number you want. If there is only one proceed to **Option 2**.

Add the following to your `mpv.conf` in the [config directory](docs/configuration.md#mpv-configuration), replacing `0` with the number from the previous step:

```
fs=yes
fs-screen=0
```

**Option 2**: (Linux Only) If option 1 does not work, both of your monitors are likely configured as a single "screen".

Run `xrandr`. It should look something like this:

```
Screen 0: minimum 8 x 8, current 3520 x 1080, maximum 16384 x 16384
VGA-0 connected 1920x1080+0+0 (normal left inverted right x axis y axis) 521mm x 293mm
   1920x1080     60.00*+
   1680x1050     59.95
   1440x900      59.89
   1280x1024     75.02    60.02
   1280x960      60.00
   1280x800      59.81
   1280x720      60.00
   1152x864      75.00
   1024x768      75.03    70.07    60.00
   800x600       75.00    72.19    60.32    56.25
   640x480       75.00    59.94
LVDS-0 connected 1600x900+1920+180 (normal left inverted right x axis y axis) 309mm x 174mm
   1600x900      59.98*+
```

If you want MPV to open on VGA-0 for instance, add the following to your `mpv.conf` in the [config directory](docs/configuration.md#mpv-configuration):

```
fs=yes
geometry=1920x1080+0+0
```

**Option 3**: (Linux Only) If your window manager supports it, you can tell the window manager to always open on a specific screen.

- For OpenBox: https://forums.bunsenlabs.org/viewtopic.php?id=1199
- For i3: https://unix.stackexchange.com/questions/96798/i3wm-start-applications-on-specific-workspaces-when-i3-starts/363848#363848

### Control Volume with Mouse Wheel (#48)

Add the following to `input.conf`:

```
WHEEL_UP add volume 5
WHEEL_DOWN add volume -5
```

### MPRIS Plugin (#54)

Set `mpv_ext` to `true` in the config. Add `script=/path/to/mpris.so` to `mpv.conf`.

### Run Multiple Instances (#45)

Pass `--config /path/to/folder` to run another copy of the player.

Each config directory gets its own instance: the single-instance guard is a lock inside the
config directory, so copies pointed at different folders coexist by design. Launching a second
copy with the *same* config directory instead raises the window of the one already running,
which is what makes the desktop launcher and the tray behave sensibly.

To shut one down, run `jellyfin-mpv-shim stop` (with the same `--config` folder, if you used
one). It reaches the instance owning that directory over the same channel a second launch uses
to raise the window, so it stops the right copy without hunting for a process id, and the app
runs its normal shutdown rather than being killed. It exits non-zero only if an instance is
holding the lock but not answering.

### Audio Passthrough

This is built in now — see [Audio Output](docs/configuration.md#audio-output). Set `audio_mode` to `hdmi` or
`optical` in Settings and tick the formats your receiver accepts; there is no need to hand-edit
`mpv.conf`.

This section used to recommend an `mpv.conf` snippet setting `audio-spdif` and
`af=lavcac3enc` together. **Don't do that.** The two are mutually exclusive per track: the AC3
encoder is handed a compressed frame it cannot convert, the filter chain fails to build, and mpv
recovers by silently disabling the filter — so the encoder never runs and nothing tells you why.
`audio_mode=optical` handles this properly by choosing between passthrough and the encoder per
track, based on what the track actually is.

### MPV Crashes with "The sub-scale option must be a floating point number or a ratio"

Run the jellyfin-mpv-shim program with LC_NUMERIC=C.

## Development

Build instructions, dev installation, packaging and translation are in
**[docs/development.md](docs/development.md)**.

## Linux Installation

You can [install the software from flathub](https://flathub.org/apps/details/com.github.iwalton3.jellyfin-mpv-shim). The pip installation is less integrated but takes up less space if you're not already using flatpak.

If you are on Linux, you can install via pip. You'll need [libmpv](https://github.com/Kagami/mpv.js#get-libmpv) or `mpv` installed.

MPV 0.41 or newer is recommended. Older versions work, with two differences:
minimizing the library to the tray quits MPV rather than just dropping its
window (it comes back on the next play, or from the tray), and copy/paste in
text fields needs `wl-clipboard` on Wayland or `xclip`/`xsel` on X11, because
MPV had no X11 clipboard of its own before 0.41.

```bash
sudo pip3 install --upgrade jellyfin-mpv-shim
```

That is the whole application: the library browser, the playback HUD and the cast screen are all
drawn inside the player's own mpv window, and Pillow (which rasterizes them) is a required
dependency. Tkinter is *not* required — if you previously installed `python3-tk` only for this
application, you can remove it.

The one piece that is optional is the system tray icon:

```bash
sudo pip3 install 'jellyfin-mpv-shim[systray]'
```

It additionally needs PyGObject and an AppIndicator typelib **from your
distribution** — pystray reaches the system library through `gi`, and neither piece can come from
pip. On Debian and Ubuntu:

```bash
sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1
```

Install the *Ayatana* package specifically. pystray also accepts the older
`gir1.2-appindicator3-0.1` (Canonical's libappindicator, unmaintained since 2017) and prefers it
when both are installed, so having the old one present is worse than not having it at all.

Because `gi` comes from the distribution rather than pip, a virtualenv only sees it when created
with `--system-site-packages` — for pipx, `pipx install --system-site-packages
'jellyfin-mpv-shim[systray]'`. Without it everything else still works; you just lose the tray,
which means closing the library window quits the application instead of leaving it running as a
cast target.

Discord rich presence support:

```bash
sudo pip3 install jellyfin-mpv-shim[discord]
```

If your distribution ships an old MPV, building it from source gets you better codec support and
the current renderer. Follow the instructions in
[mpv-build](https://github.com/mpv-player/mpv-build) — it builds MPV together with matching
FFmpeg, libass and libplacebo, and its README lists the build dependencies for your distribution.

`libmpv` is what this client loads, and mpv-build produces it by default (`libmpv` defaults to
true in MPV's own meson options), so no extra configuration is needed. Afterwards run
`sudo ldconfig` so the new library is picked up.

> Older versions of this guide told you to run `echo --enable-libmpv-shared > mpv_options`.
> **Don't** — that was a flag for MPV's old waf build. mpv-build uses meson now, and meson
> rejects the flag, so it breaks the build rather than doing nothing.

## <h2 id="osx-installation">macOS Installation</h2>
Currently on macOS only the external MPV backend seems to be working. I cannot test on macOS, so please report any issues you find.

To install the CLI version:

1. Install brew. ([Instructions](https://brew.sh/))
2. Install python3 and mpv. `brew install python mpv`
3. Install pipx. `brew install pipx`
4. Set path `pipx ensurepath`
5. Install jellyfin-mpv-shim. `pipx install jellyfin-mpv-shim`
6. Run `jellyfin-mpv-shim`.

If you'd like the menu bar icon as well:

1. Install mpv. `brew install mpv`
2. Install python3. `brew install python`
3. Install pipx. `brew install pipx`
4. Set path `pipx ensurepath`
5. Install jellyfin-mpv-shim and pystray. `pipx install 'jellyfin-mpv-shim[systray]'`
6. Run `jellyfin-mpv-shim`.

Display mirroring is not tested on macOS, but may be installable with 'pipx install 'jellyfin-mpv-shim[mirror]'`.

## Building on Windows

There is a prebuilt version for Windows in the releases section, so you only need this if you are
working on the client itself.

These steps mirror `.github/workflows/main.yml`, which is what actually produces the releases.
**If this section and the workflow ever disagree, the workflow is right** — check it first.

Make sure Python and libmpv are both 64-bit or both 32-bit; mismatching them fails at runtime.

1. Install Git for Windows. Open Git Bash and run `git clone https://github.com/jellyfin/jellyfin-mpv-shim; cd jellyfin-mpv-shim`.
    - You can update the project later with `git pull`.
2. Install [Python 3](https://www.python.org/downloads/) with PATH enabled (CI builds on 3.14) and [7zip](https://www.7-zip.org/).
3. Install [Inno Setup](https://jrsoftware.org/isinfo.php) — it builds the installer at the end.
    - CI does this with `winget install --id JRSoftware.InnoSetup -e -s winget`.
4. Open `cmd` and run `pip install wheel` then `pip install .[all] pywin32`.
5. Download libmpv from the [shinchiro/mpv-winbuild-cmake releases](https://github.com/shinchiro/mpv-winbuild-cmake/releases)
   — the `mpv-dev-*` archive, **not** the player build.
    - 64-bit: `mpv-dev-x86_64-v3-*.7z`. The `v3` builds need a CPU supporting x86-64-v3; for older
      hardware use the plain `mpv-dev-x86_64-*-git-*.7z` (this is what the "legacy64" release is).
    - 32-bit: `mpv-dev-i686-*.7z`.
6. Extract it and move `libmpv-2.dll` into the `jellyfin-mpv-shim` folder, **renaming it to
   `mpv-2.dll`**. The build scripts look for that name.
7. In Git Bash, build the PyInstaller bootloader from source:
   ```bash
   ./gen_pkg.sh --get-pyinstaller
   cd pyinstaller/bootloader && python ./waf distclean all && cd .. && pip install .
   cd ..
   ```
    - PyInstaller is only needed to produce the `.exe`. It is not a dependency of the
      application, and nobody running the client needs it.
    - A stock `pip install pyinstaller` also works, but ships a prebuilt bootloader that
      antivirus products have a long history of flagging. Building it locally gives the
      installer a bootloader that isn't already on every heuristic blocklist, which is why CI
      does it this way.
8. In Git Bash, run `./gen_pkg.sh --skip-build`.
    - This builds the translation files and downloads the shader packs.
9. Run `build-win.bat` from `cmd` (`build-win-32.bat` for 32-bit, `build-win-dbg.bat` for a
   console-attached debug build).
    - The 32-bit script reads the same `mpv-2.dll` in the same place — just extract the i686 one
      instead. There is no separate `mpv32` folder.
