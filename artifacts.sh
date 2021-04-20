#!/bin/bash
mkdir -p publish publish/Portable publish/Installer publish/InstallerLegacy publish/Debug
version=$(cat jellyfin_mpv_shim/constants.py | grep '^CLIENT_VERSION' | cut -d '"' -f 2)
if [[ "$1" == "standard" ]]
then
    cp dist/jellyfin-mpv-shim_version_installer.exe publish/Installer/jellyfin-mpv-shim_${version}_installer.exe || exit 1
    cp dist/run.exe publish/Portable/jellyfin-mpv-shim_${version}_portable.exe || exit 1
    mv dist/run publish/Debug/ || exit 1
elif [[ "$1" == "legacy" ]]
then
    cp dist/jellyfin-mpv-shim_version_installer.exe publish/InstallerLegacy/jellyfin-mpv-shim_${version}_LEGACY32_installer.exe || exit 1
fi
