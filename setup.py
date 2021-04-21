from setuptools import setup

with open("README.md", "r") as fh:
    long_description = fh.read()

setup(
    name="jellyfin-mpv-shim",
    version="2.0.1",
    author="Ian Walton",
    author_email="iwalton3@gmail.com",
    description="Cast media from Jellyfin Mobile and Web apps to MPV.",
    license="GPLv3",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/jellyfin/jellyfin-mpv-shim",
    packages=[
        "jellyfin_mpv_shim",
        "jellyfin_mpv_shim.display_mirror"
    ],
    package_data={
        "jellyfin_mpv_shim.display_mirror": ["*.css", "*.html"],
        "jellyfin_mpv_shim": ["systray.png"],
    },
    entry_points={
        "console_scripts": [
            "jellyfin-mpv-shim=jellyfin_mpv_shim.mpv_shim:main"
        ]
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: OS Independent",
    ],
    extras_require={
        "gui": ["pystray", "PIL"],
        "mirror": ["Jinja2", "pywebview>=3.3.1"],
        "discord": ["pypresence"],
        "all": [
            "Jinja2",
            "pywebview>=3.3.1",
            "pystray",
            "pypresence",
        ],
    },
    python_requires=">=3.6",
    install_requires=[
        "python-mpv",
        "jellyfin-apiclient-python>=1.7.2",
        "python-mpv-jsonipc>=1.1.9",
        "requests",
    ],
    include_package_data=True,
)
