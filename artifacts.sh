#!/bin/bash
mkdir -p publish
version=$(cat jellyfin_mpv_shim/constants.py | grep '^CLIENT_VERSION' | cut -d '"' -f 2)
if [[ "$1" == "standard" ]]
then
    cp dist/jellyfin-mpv-desktop_version_installer.exe publish/jellyfin-mpv-desktop_${version}_installer.exe || exit 1
    cp dist/run.exe publish/jellyfin-mpv-shim_${version}.exe || exit 1
elif [[ "$1" == "legacy" ]]
then
    cp dist/jellyfin-mpv-desktop_version_installer.exe publish/jellyfin-mpv-desktop_${version}_LEGACY32_installer.exe || exit 1
fi
