from setuptools import setup

APP = ["/usr/local/bin/jellyfin-mpv-shim"]
OPTIONS = {
    "argv_emulation": True,
    "iconfile": "jellyfin.icns",
    "resources": ["/usr/local/bin/mpv", "/usr/local/bin/jellyfin-mpv-shim"],
    "packages": ["pkg_resources"],
}

setup(
    app=APP,
    name="Jellyfin MPV Shim",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
