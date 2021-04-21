APP_NAME = "jellyfin-mpv-shim"
USER_APP_NAME = "Jellyfin MPV Shim"
CLIENT_VERSION = "2.0.1"
USER_AGENT = "Jellyfin-MPV-Shim/%s" % CLIENT_VERSION
CAPABILITIES = {
    "PlayableMediaTypes": "Video",
    "SupportsMediaControl": True,
    "SupportedCommands": (
        "MoveUp,MoveDown,MoveLeft,MoveRight,Select,"
        "Back,ToggleFullscreen,"
        "GoHome,GoToSettings,TakeScreenshot,"
        "VolumeUp,VolumeDown,ToggleMute,"
        "SetAudioStreamIndex,SetSubtitleStreamIndex,"
        "Mute,Unmute,SetVolume,DisplayContent,"
        "Play,Playstate,PlayNext,PlayMediaSource"
    ),
}
