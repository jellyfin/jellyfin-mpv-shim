from setuptools import setup
import sys
import os

with open("README.md", "r") as fh:
    long_description = fh.read()

extras = {
    "gui": ["pystray", "pillow"],
    "mirror": ["Jinja2", "pywebview>=3.3.1"],
    "discord": ["pypresence"],
    "all": ["Jinja2", "pywebview>=3.3.1", "pystray", "pypresence", "pillow"],
}

if sys.platform.startswith("win32"):
    win_extra = ["pywin32", "clr-loader", "pythonnet"]
    extras["all"] += win_extra
    extras["mirror"] += win_extra

packages = [
    "jellyfin_mpv_shim",
    "jellyfin_mpv_shim.display_mirror",
]

if not sys.platform.startswith("win32"):
    packages.extend(
        [
            "jellyfin_mpv_shim.messages",
            "jellyfin_mpv_shim.default_shader_pack",
            "jellyfin_mpv_shim.default_shader_pack.shaders",
            "jellyfin_mpv_shim.integration",
        ]
    )

    for dir in os.listdir("jellyfin_mpv_shim/messages"):
        if os.path.isdir("jellyfin_mpv_shim/messages/" + dir + "/LC_MESSAGES"):
            packages.append("jellyfin_mpv_shim.messages." + dir + ".LC_MESSAGES")

setup(
    name="jellyfin-mpv-shim",
    version="2.9.0",
    author="Izzie Walton",
    author_email="izzie@iwalton.com",
    description="Cast media from Jellyfin Mobile and Web apps to MPV.",
    license="GPLv3",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/jellyfin/jellyfin-mpv-shim",
    packages=packages,
    package_data={
        "jellyfin_mpv_shim.display_mirror": ["*.css", "*.html"],
        "jellyfin_mpv_shim": ["systray.png"],
    },
    entry_points={
        "console_scripts": ["jellyfin-mpv-shim=jellyfin_mpv_shim.mpv_shim:main"]
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: OS Independent",
    ],
    extras_require=extras,
    python_requires=">=3.7",
    install_requires=[
        "python-mpv>=1.0.7",
        "jellyfin-apiclient-python>=1.11.0",
        "python-mpv-jsonipc>=1.2.0",
        "requests",
    ],
    include_package_data=True,
)
