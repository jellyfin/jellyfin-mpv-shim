#!/bin/bash
# No InstallerLegacy: the LEGACY32 build was dropped -- see docs/packaging.md
# section 1. LEGACY64 is a different thing and lands in publish/Installer.
mkdir -p publish publish/Installer publish/Debug publish/Flatpak publish/macOS publish/Linux
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
elif [[ "$1" == "deb" ]]
then
    # linux/build-deb.sh already wrote the package; this only checks it is there, it never rebuilds it.
    # linux/build-deb.sh builds amd64 only, and names the file with the Debian version ("pre" becomes "~pre").
    if [[ "$2" != "amd64" ]]
    then
        echo "artifacts.sh: unsupported deb architecture '$2' (only amd64 is built)" >&2
        exit 1
    fi
    deb="publish/Linux/jellyhome-player_${version/pre/~pre}-1_${2}.deb"
    for f in "$deb" "$deb.sha256"
    do
        if [[ ! -f "$f" ]]
        then
            echo "artifacts.sh: expected $f was not built" >&2
            exit 1
        fi
    done
elif [[ "$1" == "macos" ]]
then
    # $2 is the architecture label (arm64/x86_64), as for flatpak.
    cp dist/jellyhome-player_version.dmg publish/macOS/jellyhome-player_${version}_macOS_${2}.dmg || exit 1
else
    echo "artifacts.sh: unknown artifact type '$1' (standard, legacy64, arm64, flatpak, macos or deb)" >&2
    exit 1
fi
