#!/bin/bash
mkdir -p publish publish/Shim publish/DesktopInstaller publish/DesktopInstallerLegacy publish/DesktopDebug
version=$(cat jellyfin_mpv_shim/constants.py | grep '^CLIENT_VERSION' | cut -d '"' -f 2)
if [[ "$1" == "standard" ]]
then
    cp dist/jellyfin-mpv-desktop_version_installer.exe publish/DesktopInstaller/jellyfin-mpv-desktop_${version}_installer.exe || exit 1
    cp dist/run.exe publish/Shim/jellyfin-mpv-shim_${version}.exe || exit 1
    mv dist/run-desktop publish/DesktopDebug/ || exit 1
elif [[ "$1" == "legacy" ]]
then
    cp dist/jellyfin-mpv-desktop_version_installer.exe publish/DesktopInstallerLegacy/jellyfin-mpv-desktop_${version}_LEGACY32_installer.exe || exit 1
fi
