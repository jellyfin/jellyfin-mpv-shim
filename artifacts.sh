#!/bin/bash
# No InstallerLegacy: the LEGACY32 build was dropped -- see docs/packaging.md
# section 1. LEGACY64 is a different thing and lands in publish/Installer.
mkdir -p publish publish/Installer publish/Debug publish/Flatpak publish/macOS
version=$(cat jellyfin_mpv_shim/constants.py | grep '^CLIENT_VERSION' | cut -d '"' -f 2)
if [[ "$1" == "standard" ]]
then
    cp dist/jellyhome-player_version_installer.exe publish/Installer/jellyhome-player_${version}_installer.exe || exit 1
    #mv dist/JellyHomePlayer publish/Debug/ || exit 1
elif [[ "$1" == "legacy64" ]]
then
    cp dist/jellyhome-player_version_installer.exe publish/Installer/jellyhome-player_${version}_LEGACY64_installer.exe || exit 1
elif [[ "$1" == "arm64" ]]
then
    cp dist/jellyhome-player_version_installer.exe publish/Installer/jellyhome-player_${version}_ARM64_installer.exe || exit 1
elif [[ "$1" == "flatpak" ]]
then
    # $2 is the architecture label (amd64/arm64). The bundle is exported from
    # the ostree repo flatpak-builder wrote, not copied out of dist/.
    flatpak build-bundle repo \
        "publish/Flatpak/jellyhome-player_v${version}_${2}.flatpak" \
        io.github.braisravina.JellyHomePlayer || exit 1
elif [[ "$1" == "macos" ]]
then
    # $2 is the architecture label (arm64/x86_64), as for flatpak.
    cp dist/jellyhome-player_version.dmg publish/macOS/jellyhome-player_${version}_macOS_${2}.dmg || exit 1
fi
