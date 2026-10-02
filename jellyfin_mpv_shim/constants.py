APP_NAME = "jellyfin-mpv-shim"
USER_APP_NAME = "Jellyfin MPV Shim"
# Reverse-DNS desktop-entry id. Must match the basename of the installed
# .desktop file (integration/) and its StartupWMClass, because that match is
# how a Linux desktop finds the window's icon — see player.py's x11_name /
# wayland_app_id. Changing one without the others loses the icon silently.
DESKTOP_ID = "com.github.iwalton3.jellyfin-mpv-shim"
CLIENT_VERSION = "3.0.0"

#: The uuid the downloads browser uses where a live source would name a
#: server. It is not a server: nothing connects to it, no credential carries
#: it, and `UserManager.server_id_for` cannot translate it. Here rather than
#: as a literal in four files because the catalog has to recognise it -- a
#: read scoped to "offline" must answer for every server, while a read scoped
#: to a login the registry does not know must answer for none, and those two
#: were the same `None` until they were told apart.
OFFLINE_SERVER_UUID = "offline"

#: How the offline library spells a downloaded playlist's id.
#:
#: **Jellyfin derives a playlist id from its NAME**, so two unrelated servers
#: hand out the same one (measured; docs/offline-sync.md section 4). The
#: catalog keys a
#: playlist on `(playlist_id, server_id)` for that reason -- but the offline
#: library is ONE pseudo-server showing every download at once, and the
#: browser routes a tile by its DTO `Id`. Two tiles sharing an id are one
#: tile as far as routing is concerned: whichever server's items were built
#: last answered for both.
#:
#: So the offline DTO carries a scoped id, in the manner of `offline:movies`
#: and `offline:books` beside it. A real id never contains a colon, which is
#: what makes the round trip unambiguous.
#:
#: Here rather than in `mpvtk_browser.repository`, because the badge renderer
#: and the delete gesture both have to reverse it and neither should import
#: the offline library to do it.
OFFLINE_PLAYLIST_PREFIX = "offline:playlist:"


def offline_playlist_id(playlist_id, content_server_id):
    """The offline library's id for one server's copy of a playlist."""
    return "%s%s:%s" % (OFFLINE_PLAYLIST_PREFIX, content_server_id or "",
                        playlist_id)


def split_offline_playlist_id(item_id):
    """``(playlist_id, content_server_id)`` for an offline playlist id.

    ``(item_id, None)`` for anything else, so a caller can pass any id it
    holds: an online DTO's id comes back unchanged, which is what every
    caller wants to do with it anyway.
    """
    if not item_id or not item_id.startswith(OFFLINE_PLAYLIST_PREFIX):
        return (item_id, None)
    rest = item_id[len(OFFLINE_PLAYLIST_PREFIX):]
    scope, _, playlist_id = rest.partition(":")
    return (playlist_id or item_id, scope or None)

#: Why a saved server did not connect, as far as anything can tell from
#: outside. `clients.py` decides it and the browser's Servers tab and server
#: switcher read it -- here rather than in `clients.py` because nothing under
#: `mpvtk_browser/` may import that module (the gateway is the one door, and
#: `tests/test_source_invariants.py` enforces it), and a pair of bare string
#: literals on the reading side is a contract nothing checks.
#:
#: The distinction is the whole of it: one of these is waited out and the
#: other needs the user's password, and offering only the wrong one is what
#: made "remove the server and add it again" the single way back.
CONNECT_UNREACHABLE = "unreachable"
CONNECT_SIGNED_OUT = "signed_out"
#: Not a failure: a connect to this server is in flight right now, started by
#: something other than whoever is asking -- the periodic health check, a
#: websocket reconnect, the cast verifier. Its own answer, because the two
#: above send the user to do something and this one asks them to wait, and
#: answering "unreachable" for it told a user their server may be switched off
#: while it was being connected to. CR10.
CONNECT_BUSY = "busy"

#: Why a re-authentication was refused, for the sign-in form. Only reasons
#: the user can act on differently: everything else is "it did not work",
#: which the form already says.
REAUTH_WRONG_SERVER = "wrong_server"
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
        # The two buttons jellyfin-web's remote draws but would not send:
        # its hamburger (a tile's context menu here, and the HUD's settings
        # menu during playback) and its search.
        "ToggleContextMenu",
        "GoToSearch",
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
