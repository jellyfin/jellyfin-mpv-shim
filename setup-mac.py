from setuptools import setup

APP = ["/usr/local/bin/jellyhome-player"]
OPTIONS = {
    "argv_emulation": True,
    "iconfile": "jellyfin.icns",
    "resources": ["/usr/local/bin/mpv", "/usr/local/bin/jellyhome-player"],
    "packages": ["pkg_resources"],
}

setup(
    app=APP,
    name="JellyHome Player",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
