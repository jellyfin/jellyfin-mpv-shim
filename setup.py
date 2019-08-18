from setuptools import setup, Extension

setup(
    name='Plex MPV Shim',
    version='0.2',
    packages=['plex_mpv_shim',],
    license='MIT',
    long_description=open('README.md').read(),
    author="Ian Walton",
    author_email="iwalton3@github",
    url="https://github.com/iwalton3/plex-mpv-shim",
    zip_safe=False,
    entry_points = {
        'console_scripts': [
            'plex-mpv-shim=plex_mpv_shim.mpv_shim:main',
        ]
    },
    include_package_data=True,
    install_requires = ['python-mpv', 'requests']
)

