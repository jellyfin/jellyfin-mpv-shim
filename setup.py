from setuptools import setup

with open("README.md", "r") as fh:
    long_description = fh.read()

setup(
    name='jellyfin-mpv-shim',
    version='1.5.9',
    author="Ian Walton",
    author_email="iwalton3@gmail.com",
    description="Cast media from Jellyfin Mobile and Web apps to MPV. (Unofficial)",
    license='GPLv3',
    long_description=open('README.md').read(),
    long_description_content_type="text/markdown",
    url="https://github.com/iwalton3/jellyfin-mpv-shim",
    packages=[
        'jellyfin_mpv_shim',
        'jellyfin_mpv_shim.display_mirror',
        'jellyfin_mpv_shim.webclient_view'
    ],
    package_data={
        'jellyfin_mpv_shim.display_mirror': ['*.css', '*.html'],
        'jellyfin_mpv_shim': ['systray.png'],
    },
    entry_points={
        'console_scripts': [
            'jellyfin-mpv-shim=jellyfin_mpv_shim.mpv_shim:main',
            'jellyfin-mpv-desktop=jellyfin_mpv_shim.mpv_shim:main_desktop',
        ]
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: OS Independent",
    ],
    extras_require = {
        'gui':  ['pystray'],
        'mirror':  ['Jinja2', 'pywebview'],
        'desktop':  ['Flask', 'pywebview', 'Werkzeug'],
        'all': ['Jinja2', 'pywebview', 'pystray', 'Flask', 'Werkzeug'],
    },
    python_requires='>=3.6',
    install_requires=['python-mpv', 'jellyfin-apiclient-python>=1.4.0', 'python-mpv-jsonipc>=1.1.9'],
    include_package_data=True
)
