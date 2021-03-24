from setuptools import setup

APP = ["/usr/local/bin/jellyfin-mpv-desktop"]
OPTIONS = {
    "argv_emulation": True,
    "iconfile": "jellyfin.icns",
    "resources": [
        "/usr/local/bin/mpv",
        "/usr/local/bin/jellyfin-mpv-shim",
        "/usr/local/lib/python3.9/site-packages/jellyfin_mpv_shim/webclient_view",
    ],
    "packages": ["pkg_resources"],
}

setup(
    app=APP,
    name="Jellyfin MPV Desktop",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
