#!/bin/bash
mkdir -p publish publish/Installer publish/InstallerLegacy publish/Debug publish/Flatpak
version=$(cat jellyfin_mpv_shim/constants.py | grep '^CLIENT_VERSION' | cut -d '"' -f 2)
if [[ "$1" == "standard" ]]
then
    cp dist/jellyfin-mpv-shim_version_installer.exe publish/Installer/jellyfin-mpv-shim_${version}_installer.exe || exit 1
    #mv dist/run publish/Debug/ || exit 1
elif [[ "$1" == "legacy" ]]
then
    cp dist/jellyfin-mpv-shim_version_installer.exe publish/InstallerLegacy/jellyfin-mpv-shim_${version}_LEGACY32_installer.exe || exit 1
elif [[ "$1" == "legacy64" ]]
then
    cp dist/jellyfin-mpv-shim_version_installer.exe publish/Installer/jellyfin-mpv-shim_${version}_LEGACY64_installer.exe || exit 1
elif [[ "$1" == "arm64" ]]
then
    cp dist/jellyfin-mpv-shim_version_installer.exe publish/Installer/jellyfin-mpv-shim_${version}_ARM64_installer.exe || exit 1
elif [[ "$1" == "flatpak" ]]
then
    # $2 is the architecture label (amd64/arm64). The bundle is exported from
    # the ostree repo flatpak-builder wrote, not copied out of dist/.
    flatpak build-bundle repo \
        "publish/Flatpak/jellyfin-mpv-shim_v${version}_${2}.flatpak" \
        com.github.iwalton3.jellyfin-mpv-shim || exit 1
fi
