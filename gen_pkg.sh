#!/bin/bash
# This script:
# - Checks the current version
# - Verifies all versions match and are newer
# - Downloads/updates web client
# - Download/updates default-shader-pack
# - Generates locales
# - Builds the python package

cd "$(dirname "$0")"

function download_compat {
    if [[ "$(which wget)" != "" ]]
    then
        wget -qO "$1" "$2"
    else [[ "$(which curl)" != "" ]]
        curl -s "$2" > "$1"
    fi
}

function get_resource_version {
    curl -s --head https://github.com/iwalton3/"$1"/releases/latest | \
        grep -i '^location: ' | sed 's/.*tag\///g' | tr -d '\r'
}

# Verify versioning
current_version=$(get_resource_version jellyfin-mpv-shim)
current_version=${current_version:1}
constants_version=$(cat jellyfin_mpv_shim/constants.py | grep '^CLIENT_VERSION' | cut -d '"' -f 2)
setup_version=$(grep 'version=' setup.py | cut -d "'" -f 2)
iss_version=$(grep '^#define MyAppVersion' "Jellyfin MPV Desktop.iss" | cut -d '"' -f 2)
appdata_version=$(grep 'release version="' jellyfin_mpv_shim/integration/com.github.iwalton3.jellyfin-mpv-shim.appdata.xml | \
    head -n 1 | cut -d '"' -f 2)

if [[ "$current_version" == "$constants_version" ]]
then
    echo "Warning: This version matches the current published version."
    echo "If you are building a release, the publish will not succeed."
fi

if [[ "$constants_version" != "$setup_version" || "$setup_version" != "$iss_version" || "$iss_version" != "$appdata_version" ]]
then
    echo "Error: The release does not have the same version numbers in all files!"
    echo "Please correct this before releasing!"
    echo "Constants: $constants_version, Setup: $setup_version, ISS: $iss_version, Flatpak: $appdata_version"
fi

# Generate translations
find -iname '*.po' | while read -r file
do
    msgfmt "$file" -o "${file%.*}.mo"
done

# Download web client
update_web_client="no"
if [[ ! -e "jellyfin_mpv_shim/webclient_view/webclient" ]]
then
    update_web_client="yes"
elif [[ -e ".last_wc_version" ]]
then
    if [[ "$(get_resource_version jellyfin-web)" != "$(cat .last_wc_version)" ]]
    then
        update_web_client="yes"
    fi
fi

if [[ "$update_web_client" == "yes" ]]
then
    echo "Downloading web client..."
    wc_version=$(get_resource_version jellyfin-web)
    download_compat dist.zip "https://github.com/iwalton3/jellyfin-web/releases/download/$wc_version/dist.zip"
    rm -r jellyfin_mpv_shim/webclient_view/webclient 2> /dev/null
    unzip dist.zip > /dev/null && rm dist.zip
    mv dist jellyfin_mpv_shim/webclient_view/webclient
    echo "$wc_version" > .last_wc_version
fi

# Download default-shader-pack
update_shader_pack="no"
if [[ ! -e "jellyfin_mpv_shim/default_shader_pack" ]]
then
    update_shader_pack="yes"
elif [[ -e ".last_sp_version" ]]
then
    if [[ "$(get_resource_version default-shader-pack)" != "$(cat .last_sp_version)" ]]
    then
        update_shader_pack="yes"
    fi
fi

if [[ "$update_shader_pack" == "yes" ]]
then
    echo "Downloading shaders..."
    sp_version=$(get_resource_version default-shader-pack)
    download_compat release.zip "https://github.com/iwalton3/default-shader-pack/archive/$sp_version.zip"
    rm -r jellyfin_mpv_shim/default_shader_pack 2> /dev/null
    (
        mkdir default_shader_pack
        cd default_shader_pack
        unzip ../release.zip > /dev/null && rm ../release.zip
        mv default-shader-pack-*/* ./
        rm -r default-shader-pack-*
    )
    mv default_shader_pack jellyfin_mpv_shim/
    echo "$sp_version" > .last_sp_version
fi

# Generate package
if [[ "$1" == "--install" ]]
then
    if [[ "$(which sudo 2> /dev/null)" != "" && ! "$*" =~ "--local" ]]
    then
        sudo pip3 install .[all]
    else
        pip3 install .[all]
    fi

elif [[ "$1" != "--skip-build" ]]
then
    rm -r build/ dist/ .eggs 2> /dev/null
    mkdir build/ dist/
    echo "Building release package."
    python3 setup.py sdist bdist_wheel > /dev/null
fi

