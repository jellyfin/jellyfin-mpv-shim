# Jellyfin MPV Shim

Jellyfin MPV Shim is a simple and lightweight Jellyfin client, with support for Windows
and Linux. Think of it as an open source Chromecast for Jellyfin. You can cast almost
anything from Jellyfin and it will Direct Play. Subtitles are fully supported, and
there are tools to manage them like no other Jellyfin client.

## Getting Started

If you are on Windows, simply [download the binary](https://github.com/iwalton3/jellyfin-mpv-shim/releases).
If you are using Linux, you can [install via flathub](https://flathub.org/apps/details/com.github.iwalton3.jellyfin-mpv-shim) or [install via pip](https://github.com/iwalton3/jellyfin-mpv-shim/blob/master/README.md#linux-installation). If you are on OSX, see the [OSX Installation](https://github.com/iwalton3/jellyfin-mpv-shim/blob/master/README.md#osx-installation)
section below.

### Desktop Client

Launch the client. You should see the Jellyfin web app. Log in to your server and use it as normal.
All videos will load in MPV just like MPV Shim.

Please note: The desktop client for Windows contains significantly more files than MPV Shim, so it
is distributed as an installer. It will work without admin rights.

### MPV Shim

To use the client, simply launch it and log into your Jellyfin server. You’ll need to enter the
URL to your server, for example `http://server_ip:8096` or `https://secure_domain`. Make sure to
include the subdirectory and port number if applicable. You can then cast your media
from another Jellyfin application.

The application runs with a notification icon by default. You can use this to edit the server settings,
view the application log, open the config folder, and open the application menu. Unlike Plex MPV Shim,
authorization tokens for your server are stored on your device, but you are able to cast to the player
regardless of location.

Note: Due to the huge number of questions and issues that have been submitted about URLs, I now tolerate
bare IP addresses and not specifying the port by default. If you want to connect to port 80 instead of
8096, you must add the `:80` to the URL because `:8096` is now the default.

## Limitations

 - Music playback and Live TV are not supported.
 - The client can’t be shared seamlessly between multiple users on the same server. ([Link to issue.](https://features.jellyfin.org/posts/319/mark-device-as-shared))

## Advanced Features

### Menu

To open the menu, press **c** on your computer. Depending on what app you are
using to control Jellyfin, you may also be able to open the menu using that app.
The web application currently doesn't have the required buttons to do so.

The menu enables you to:
 - Adjust video transcoding quality.
 - Change the default transcoder settings.
 - Change subtitles or audio, while knowing the track names.
 - Change subtitles or audio for an entire series at once.
 - Mark the media as unwatched and quit.

On your computer, use the arrow keys, enter, and escape to navigate. On your phone, use
the arrow buttons, ok, back, and home to navigate.

Please also note that the on-screen controller for MPV (if available) cannot change the
audio and subtitle track configurations for transcoded media. It also cannot load external
subtitles. You must either use the menu or the application you casted from.

### Display Mirroring

This feature allows media previews to show on your display before you cast the media,
similar to Chromecast. It is not enabled by default. To enable it, do one of the following:

 - Using the systray icon, click "Application Menu". Go to preferences and enable display mirroring.
     - Use the arrow keys, escape, and enter to navigate the menu.
 - Cast media to the player and press `c`. Go to preferences and enable display mirroring.
 - In the config file (see below), change `display_mirroring` to `true`.

Then restart the application for the change to take effect. To quit the application on Windows with
display mirroring enabled, press Alt+F4.

### Keyboard Shortcuts

This program supports most of the [keyboard shortcuts from MPV](https://mpv.io/manual/stable/#interactive-control). The custom keyboard shortcuts are:

 - < > to skip episodes
 - q to close player
 - w to mark watched and skip
 - u to mark unwatched and quit
 - c to open the menu

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

The configuration file is located in different places depending on your platform. You can also open the
configuration folder using the systray icon if you are using the shim version. When you launch the program
on Linux or OSX from the terminal, the location of the config file will be printed. The locations are:
 - Windows - `%appdata%\jellyfin-mpv-shim\conf.json`
 - Linux - `~/.config/jellyfin-mpv-shim/conf.json`
 - Linux (Flatpak) - `~/.var/app/com.github.iwalton3.jellyfin-mpv-shim/config/jellyfin-mpv-shim/conf.json`
 - Mac OSX - `Library/Application Support/jellyfin-mpv-shim/conf.json`
 - CygWin - `~/.config/jellyfin-mpv-shim/conf.json`

You can specify a custom configuration folder with the `--config` option.

### Transcoding

You can adjust the basic transcoder settings via the menu.

- `always_transcode` - This will tell the client to always transcode. Default: `false`
    - This may be useful if you are using limited hardware that cannot handle advanced codecs.
- `transcode_h265` - Force transcode HEVC videos to h264. Default: `false`
- `transcode_hi10p` - Force transcode 10 bit color videos to 8 bit color. Default: `false`
- `remote_kbps` - Bandwidth to permit for remote streaming. Default: `10000`
- `local_kbps` - Bandwidth to permit for local streaming. Default: `2147483`
- `direct_paths` - Play media files directly from the SMB or NFS source. Default: `false`
    - `remote_direct_paths` - Apply this even when the server is detected as remote. Default: `false`
- `transcode_to_h265` - Allow the server to transcode media *to* `hevc`. Default: `false`

### Shell Command Triggers

You can execute shell commands on media state using the config file:

 - `media_ended_cmd` - When all media has played.
 - `pre_media_cmd` - Before the player displays. (Will wait for finish.)
 - `stop_cmd` - After stopping the player.
 - `idle_cmd` - After no activity for `idle_cmd_delay` seconds.
 - `idle_when_paused` - Consider the player idle when paused. Default: `false`
 - `stop_idle` - Stop the player when idle. (Requires `idle_when_paused`.) Default: `false`

### Subtitle Visual Settings

These settings may not works for some subtitle codecs or if subtitles are being burned in
during a transcode. You can configure custom styled subtitle settings through the MPV config file.

 - `subtitle_size` - The size of the subtitles, in percent. Default: `100`
 - `subtitle_color` - The color of the subtitles, in hex. Default: `#FFFFFFFF`
 - `subtitle_position` - The position (top, bottom, middle). Default: `bottom`

### External MPV

The client now supports using an external copy of MPV, including one that is running prior to starting
the client. This may be useful if your distribution only provides MPV as a binary executable (instead
of as a shared library), or to connect to MPV-based GUI players. Please note that SMPlayer exhibits
strange behaviour when controlled in this manner. External MPV is currently the only working backend
for media playback on OSX.

- `mpv_ext` - Enable usage of the external player by default. Default: `false`
    - The external player may still be used by default if `libmpv1` is not available.
- `mpv_ext_path` - The path to the `mpv` binary to use. By default it uses the one in the PATH. Default: `null`
    - If you are using Windows, make sure to use two backslashes. Example: `C:\\path\\to\\mpv.exe`
- `mpv_ext_ipc` - The path to the socket to control MPV. Default: `null`
    - If unset, the socket is a randomly selected temp file.
    - On Windows, this is just a name for the socket, not a path like on Linux.
- `mpv_ext_start` - Start a managed copy of MPV with the client. Default: `true`
    - If not specified, the user must start MPV prior to launching the client.
    - MPV must be launched with `--input-ipc-server=[value of mpv_ext_ipc]`.

### Other Configuration Options

 - `player_name` - The name of the player that appears in the cast menu. Initially set from your hostname.
 - `client_uuid` - The identifier for the client. Set to a random value on first run.
 - `audio_output` - Currently has no effect. Default: `hdmi`
 - `fullscreen` - Fullscreen the player when starting playback. Default: `true`
 - `enable_gui` - Enable the system tray icon and GUI features. Default: `true`
 - `media_key_seek` - Use the media next/prev keys to seek instead of skip episodes. Default: `false`
 - `enable_osc` - Enable the MPV on-screen controller. Default: `true`
    - It may be useful to disable this if you are using an external player that already provides a user interface.
 - `use_web_seek` - Use the seek times set in Jellyfin web for arrow key seek. Default: `false`
 - `display_mirroring` - Enable webview-based display mirroring (content preview). Default: `false`
 - `log_decisions` - Log the full media decisions and playback URLs. Default: `false`
 - `mpv_log_level` - Log level to use for mpv. Default: `info`
    - Options: fatal, error, warn, info, v, debug, trace
 - `enable_desktop` - Use the desktop client. Default: `false`
    - You can also use it by running the `jellyfin-mpv-desktop`.
    - If you are using the Windows build, you must download the desktop version.
 - `desktop_fullscreen` - Run the desktop client in fullscreen. Default: `false`
 - `desktop_remember_pos` - Remember the position of the desktop client. Default: `true`

### MPV Configuration

You can configure mpv directly using the `mpv.conf` and `input.conf` files. (It is in the same folder as `conf.json`.)
This may be useful for customizing video upscaling, keyboard shortcuts, or controlling the application
via the mpv IPC server.

### Authorization

The `cred.json` file contains the authorization information. If you are having problems with the client,
such as the Now Playing not appearing or want to delete a server, you can delete this file and add the
servers again.

## Tips and Tricks

Various tips have been found that allow the media player to support special
functionality, albeit with more configuration required.

### Open on Specific Monitor (#19)

Please note: Edits to the `mpv.conf` will not take effect until you restart the application. You can open the config directory by using the menu option in the system tray icon.

**Option 1**: Select fullscreen output screen through MPV.
Determine which screen you would like MPV to show up on.
 - If you are on Windows, right click the desktop and select "Display Settings". Take the monitor number and subtract one.
 - If you are on Linux, run `xrandr`. The screen number is the number you want. If there is only one proceed to **Option 2**.

Add the following to your `mpv.conf` in the [config directory](https://github.com/iwalton3/jellyfin-mpv-shim#mpv-configuration), replacing `0` with the number from the previous step:
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

If you want MPV to open on VGA-0 for instance, add the following to your `mpv.conf` in the [config directory](https://github.com/iwalton3/jellyfin-mpv-shim#mpv-configuration):
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

You can pass `--config /path/to/folder` to run another copy of the player. Please 
note that running multiple copies of the desktop client is currently not supported. 

### Audio Passthrough

You can edit `mpv.conf` to support audio passthrough. A [user on Reddit](https://reddit.com/r/jellyfin/comments/fru6xo/new_cross_platform_desktop_client_jellyfin_mpv/fns7vyp) had luck with this config:
```
audio-spdif=ac3,dts,eac3 # (to use the passthrough to receiver over hdmi)
audio-channels=2 # (not sure this is necessary, but i keep it in because it works)
af=scaletempo,lavcac3enc=yes:640:3 # (for aac 5.1 tracks to the receiver)
```

### MPV Crashes with "The sub-scale option must be a floating point number or a ratio"

Run the jellyfin-mpv-shim program with LC_NUMERIC=C.

### Use with gnome-mpv/celluloid (#61)

You can use `gnome-mpv` with MPV Shim, but you must launch `gnome-mpv` separately before MPV Shim. (`gnome-mpv` doesn't support the MPV command options directly.)

Configure MPV Shim with the following options (leave the other ones):
```json
{
    "mpv_ext": true,
    "mpv_ext_ipc": "/tmp/gmpv-socket",
    "mpv_ext_path": null,
    "mpv_ext_start": false,
    "enable_osc": false
}
```
Then within `gnome-mpv`, click the application icon (top left) > Preferences. Configure the following Extra MPV Options:
```
--idle --input-ipc-server=/tmp/gmpv-socket
```

## Development

If you'd like to run the application without installing it, run `./run.py`.
The project is written entierly in Python 3. There are no closed-source
components in this project. It is fully hackable.

The project is dependent on `python-mpv`, `python-mpv-jsonipc`, and `jellyfin-apiclient-python`. If you are
using Windows and would like mpv to be maximize properly, `pywin32` is also needed. The GUI
component uses `pystray` and `tkinter`, but there is a fallback cli mode. The mirroring dependencies
are `Jinja2` and `pywebview`, along with platform-specific dependencies. (See the installation and building
guides for details on platform-specific dependencies for display mirroring.) The desktop client depends on
`pywebview`, `Flask`, and `Werkzeug`.

This project is based Plex MPV Shim, which is based on https://github.com/wnielson/omplex, which
is available under the terms of the MIT License. The project was ported to python3, modified to
use mpv as the player, and updated to allow all features of the remote control api for video playback.

The Jellyfin API client comes from [Jellyfin for Kodi](https://github.com/jellyfin/jellyfin-kodi/tree/master/jellyfin_kodi).
The API client was originally forked for this project and is now a [separate package](https://github.com/iwalton3/jellyfin-apiclient-python).

The css file for desktop mirroring is from [jellyfin-chromecast](https://github.com/jellyfin/jellyfin-chromecast/tree/5194d2b9f0120e0eb8c7a81fe546cb9e92fcca2b) and is subject to GPL v2.0.

### Local Dev Installation

If you are on Windows there are additional dependencies. Please see the Windows Build Instructions.

1. Install the dependencies: `sudo pip3 install --upgrade python-mpv jellyfin-apiclient-python pystray Jinja2 pywebview python-mpv-jsonipc Flask Werkzeug`.
2. Clone this repository: `git clone https://github.com/iwalton3/jellyfin-mpv-shim`
3. `cd` to the repository: `cd jellyfin-mpv-shim`
4. Download the [web client](https://github.com/iwalton3/jellyfin-web/releases) and place the contents of the dist folder inside a folder named `webclient` in the `webclient_view` folder.
5. Ensure you have a copy of `libmpv1` or `mpv` available.
6. Install any platform-specific dependencies from the respective install tutorials.
7. You should now be able to run the program with `./run.py` or `./run-desktop.py`. Installation is possible with `sudo pip3 install .`.

## Linux Installation

You can [install the software from flathub](https://flathub.org/apps/details/com.github.iwalton3.jellyfin-mpv-shim). The pip installation is less integrated but takes up less space if you're not already using flatpak.

If you are on Linux, you can install via pip. You'll need [libmpv1](https://github.com/Kagami/mpv.js/blob/master/README.md#get-libmpv) or `mpv` installed.
```bash
sudo pip3 install --upgrade jellyfin-mpv-shim
```
If you would like the Desktop client (run with `jellyfin-mpv-desktop`), also install:
```
sudo apt install python3-flask python3-webview python3-werkzeug
# -- OR --
sudo pip3 install jellyfin-mpv-shim[desktop]
sudo apt install gir1.2-webkit2-4.0
```
If you would like the GUI and systray features, also install `pystray` and `tkinter`:
```bash
sudo pip3 install pystray
sudo apt install python3-tk
```
If you would like display mirroring support, install the mirroring dependencies:
```bash
sudo apt install python3-jinja2 python3-webview
# -- OR --
sudo pip3 install jellyfin-mpv-shim[mirror]
sudo apt install gir1.2-webkit2-4.0
```

You can build mpv from source to get better codec support. Execute the following:
```bash
sudo pip3 install --upgrade python-mpv
sudo apt install autoconf automake libtool libharfbuzz-dev libfreetype6-dev libfontconfig1-dev libx11-dev libxrandr-dev libvdpau-dev libva-dev mesa-common-dev libegl1-mesa-dev yasm libasound2-dev libpulse-dev libuchardet-dev zlib1g-dev libfribidi-dev git libgnutls28-dev libgl1-mesa-dev libsdl2-dev cmake wget python g++ libluajit-5.1-dev
git clone https://github.com/mpv-player/mpv-build.git
cd mpv-build
echo --enable-libmpv-shared > mpv_options
./rebuild -j4
sudo ./install
sudo ldconfig
```

## OSX Installation
Currently on OSX only the external MPV backend seems to be working. I cannot test on OSX, so please report any issues you find.

To install the CLI version:

1. Install brew. ([Instructions](https://brew.sh/))
2. Install python3 and mpv. `brew install python mpv`
3. Install jellyfin-mpv-shim. `pip3 install --upgrade jellyfin-mpv-shim`
4. Run `jellyfin-mpv-shim`.

If you'd like to install the desktop client (currently requires python from brew):

1. Install brew. ([Instructions](https://brew.sh/))
2. Install python3 and mpv. `brew install python mpv`
3. Install jellyfin-mpv-shim. `pip3 install --upgrade 'jellyfin-mpv-shim[desktop]'`
4. Run `jellyfin-mpv-desktop`.

If you'd like to install the GUI version, you need a working copy of tkinter.

1. Install pyenv. ([Instructions](https://medium.com/python-every-day/python-development-on-macos-with-pyenv-2509c694a808))
2. Install TK and mpv. `brew install tcl-tk mpv`
3. Install python3 with TK support. `FLAGS="-I$(brew --prefix tcl-tk)/include" pyenv install 3.8.1`
4. Set this python3 as the default. `pyenv global 3.8.1`
5. Install jellyfin-mpv-shim and pystray. `pip3 install --upgrade 'jellyfin-mpv-shim[gui]'`
6. Run `jellyfin-mpv-shim`.

Display mirroring is not tested on OSX, but may be installable with 'pip3 install --upgrade 'jellyfin-mpv-shim[mirror]'`.

## Building on Windows

There is a prebuilt version for Windows in the releases section. When
following these directions, please take care to ensure both the python
and libmpv libraries are either 64 or 32 bit. (Don't mismatch them.)

If you'd like to build the installer, please install [Inno Setup](https://jrsoftware.org/isinfo.php) to build
the installer. If you'd like to build a 32 bit version, download the 32 bit version of mpv-1.dll and
copy it into a new folder called mpv32. You'll also need [WebBrowserInterop.x86.dll](https://github.com/r0x0r/pywebview/blob/master/webview/lib/WebBrowserInterop.x86.dll?raw=true).
You may also need to edit the batch file for 32 bit builds to point to the right python executable.

1. Install Git for Windows. Open Git Bash and run `git clone https://github.com/iwalton3/jellyfin-mpv-shim; cd jellyfin-mpv-shim`.
2. Install [Python3](https://www.python.org/downloads/) with PATH enabled. Install [7zip](https://ninite.com/7zip/).
3. After installing python3, open `cmd` as admin and run `pip install --upgrade pyinstaller python-mpv jellyfin-apiclient-python pywin32 pystray Jinja2 pywebview[cef] python-mpv-jsonipc Flask Werkzeug`.
4. Download [libmpv](https://sourceforge.net/projects/mpv-player-windows/files/libmpv/).
5. Extract the `mpv-1.dll` from the file and move it to the `jellyfin-mpv-shim` folder.
6. Open a regular `cmd` prompt. Navigate to the `jellyfin-mpv-shim` folder.
7. (Edge Build, disabled by default) Download [WebBrowserInterop.x64.dll](https://github.com/r0x0r/pywebview/blob/master/webview/lib/WebBrowserInterop.x64.dll?raw=true) and [Winforms Webview](https://www.nuget.org/api/v2/package/Microsoft.Toolkit.Forms.UI.Controls.WebView/6.0.0).
8. (Edge Build, disabled by default) Rename the `*.nupkg` to a `*.zip` file and extract `lib\net462\Microsoft.Toolkit.Forms.UI.Controls.WebView.dll` to the project root.
9. (CEF Desktop Client) Copy the folder `AppData\Local\Programs\Python\Python37\Lib\site-packages\cefpython3` to `cef\cefpython3`.
10. Download the web [client build](https://github.com/iwalton3/jellyfin-web/releases/tag/jwc1.5.2) and unzip it into `jellyfin_mpv_shim\webclient_view\webclient`.
11. Run `build-win.bat`.
