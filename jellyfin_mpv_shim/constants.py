APP_NAME = "jellyfin-mpv-shim"
USER_APP_NAME = "Jellyfin MPV Shim"
# Reverse-DNS desktop-entry id. Must match the basename of the installed
# .desktop file (integration/) and its StartupWMClass, because that match is
# how a Linux desktop finds the window's icon — see player.py's x11_name /
# wayland_app_id. Changing one without the others loses the icon silently.
DESKTOP_ID = "com.github.iwalton3.jellyfin-mpv-shim"
CLIENT_VERSION = "2.10.0"
USER_AGENT = "Jellyfin-MPV-Shim/%s" % CLIENT_VERSION
CAPABILITIES = {
    "PlayableMediaTypes": ["Video"],
    "SupportsMediaControl": True,
    "SupportsPersistentIdentifier": True,
    "SupportedCommands": [
        "MoveUp",
        "MoveDown",
        "MoveLeft",
        "MoveRight",
        "Select",
        "Back",
        "ToggleFullscreen",
        "GoHome",
        "GoToSettings",
        "TakeScreenshot",
        "VolumeUp",
        "VolumeDown",
        "ToggleMute",
        "SetAudioStreamIndex",
        "SetSubtitleStreamIndex",
        "Mute",
        "Unmute",
        "SetVolume",
        "DisplayContent",
        "Play",
        "Playstate",
        "PlayNext",
        "PlayMediaSource",
    ],
}
