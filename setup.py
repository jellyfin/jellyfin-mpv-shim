from setuptools import setup

with open("README.md", "r") as fh:
    long_description = fh.read()

setup(
    name='jellyfin-mpv-shim',
    version='1.1.1',
    author="Ian Walton",
    author_email="iwalton3@gmail.com",
    description="Cast media from Jellyfin Mobile and Web apps to MPV. (Unofficial)",
    license='GPLv3',
    long_description=open('README.md').read(),
    long_description_content_type="text/markdown",
    url="https://github.com/iwalton3/jellyfin-mpv-shim",
    packages=['jellyfin_mpv_shim'],
    entry_points={
        'console_scripts': [
            'jellyfin-mpv-shim=jellyfin_mpv_shim.mpv_shim:main',
        ]
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.6',
    install_requires=['python-mpv', 'jellyfin-apiclient-python>=1.0.0']
)
