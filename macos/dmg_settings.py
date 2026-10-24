# dmgbuild settings for macos/build.sh, which passes the bundle and the volume
# icon in with -D. Icon positions must match the arrow in background.png.
import os.path

app = defines["app"]  # noqa: F821 (provided by dmgbuild)
name = os.path.basename(app)

format = "UDZO"
files = [app]
symlinks = {"Applications": "/Applications"}
icon = defines["icon"]  # noqa: F821

background = "macos/background.png"
window_rect = ((200, 120), (660, 400))
default_view = "icon-view"
icon_size = 128
text_size = 13
icon_locations = {
    name: (170, 190),
    "Applications": (490, 190),
}
