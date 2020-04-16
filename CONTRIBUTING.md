# Contributing to Jellyfin MPV Shim

Thank you for interest in contributing to this project! Contributing is the best way to have functionality
quickly added to the project.

## Issues

Feel free to create an issue for any problems or feature requests. Please include as much information as
possible and be sure to check to make sure you aren't creating a duplicate issue. Providing log messages
is also important, but please be sure to avoid posting API keys and auth tokens that could allow someone
to access your media server!

## Adding Major Features

The core use-case of this application is to allow someone to cast media from their Jellyfin server to MPV.
The most basic version of this is command-line only. I would like to retain this "degraded" mode of operation
regardless of what features are added, despite the default (provided the required dependencies are installed) 
being to run with a system tray and GUI.

If you would like to add additional features that disrupt or make the command-line workflow impossible, please
allow the features to be disabled from the config file. For instance, the GUI and CLI are two separate modules
that are swapped between depending on the situation.

## Adding Dependencies

One of the major concerns with this project is allowing it to run on as many platforms as possible. Currently,
the project is designed to run on Windows, Linux, and Mac OSX. Currently the project is packaged using PIP for
OSX and Linux and PyInstaller for Windows. Additionally, I want the project to have as long of a life as possible.
If a dependency becomes uninstallable or prone to crashing, I would like to avoid the project being broken.

If you wish to add a dependency, please gracefully handle the dependency not being installed. This is the
policy I've used for most dependencies. If you cannot make a dependency optional, please contact me before
starting development on a feature. If you PR a feature with required dependencies, I may refactor them into
optional ones.

Current Dependencies:
 - `python-mpv` - Provides `libmpv1` playback backend.
 - `python-mpv-jsonipc` - Provides `mpv` playback backend. (First-Party)
 - `jellyfin-apiclient-python` - Provides API client to Jellyfin. (First-Party)
 - `pywin32` - Allows window management on Windows. (Optional)
 - `pystray` - Provides systray icon. (Optional)
 - `tkinter` - Provides GUI for adding servers and viewing logs. (Optional)
 - `Jinja2` - Renders HTML for display mirroring. (Optional)
 - `pywebview` - Displays HTML for display mirroring or webclient. (Optional)
 - `Flask` - Used to serve the webclient in desktop mode. (Optional)
 - `Werkzeug` - Used to serve the webclient in desktop mode. (Optional)

## Project Overview

 - `action_thread.py` - Thread to process events for the player from key input.
 - `bulk_subtitle.py` - Manages full-season bulk subtitle updates from the player menu.
 - `cli_mgr.py` - Command-line UI provider if GUI is not available or disabled.
 - `clients.py` - Manages auth tokens and Jellfyin client connections.
 - `conf.py` - The configuration file object. Contains configuration defaults and support code.
 - `conffile.py` - Generic module for getting settings folder locations on different platforms.
 - `constants.py` - Constant values for the application that apply to multiple modules.
 - `event_handler.py` - Handles remote control events from the Jellyfin websocket connection.
 - `gui_mgr.py` - Provides systray icon and tkinter GUI.
     - Note: This is a mess of `multiprocessing` processes and event threads to work around various bugs/issues.
 - `media.py` - Contains classes that manage media and playlists from Jellyfin.
 - `menu.py` - Implements the menu interface for changing options and playback parameters.
     - This works by drawing the menu as text on MPV and responding to keypress/remote control events.
 - `mpv_shim.py` - The main entry-point for the application.
     - Note: `run.py` is the entry-point for running in development and PyInstaller.
 - `player.py` - Implements player logic that controls MPV. Also owns the media playlist objects.
 - `timeline.py` - Thread to trigger playback events to the Jellyfin server.
     - Note: `player.py` is where the actual response is created.
 - `utils.py` - Contains the playback profile and various utilities for other modules.
 - `win_utils.py` - Implements window management workarounds for Windows.
 - `display_mirror` - Package that implements the full-screen display mirroring.
 - `webclient_view` - Package that implements the webclient UI. (The actual webclient is a [separate repo](https://github.com/iwalton3/jellyfin-web).)

## Building the Project

Please see the README for instructions.

## OSX Work

If you have access to a machine running Max OSX and can work on this project, you may be able to greatly improve
the experience for OSX users. There may be open issues for OSX that only you can work on. It would also be nice
to find a better way to make this application available for OSX users, as the current procedure is a pain.