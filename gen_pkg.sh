#!/usr/bin/env bash
# This script:
# - Checks the current version
# - Verifies all versions match and are newer
# - Downloads/updates web client
# - Download/updates default-shader-pack
# - Generates locales
# - Builds the python package

cd "$(dirname "$0")" || exit 1

function download_compat {
    if [[ "$AZ_CACHE" != "" ]]
    then
        download_id=$(echo "$2" | md5sum | sed 's/ .*//g')
        if [[ -e "$AZ_CACHE/$3/$download_id" ]]
        then
            echo "Cache hit: $AZ_CACHE/$3/$download_id"
            cp "$AZ_CACHE/$3/$download_id" "$1"
            return
        elif [[ "$3" != "" ]]
        then
            rm -r "$AZ_CACHE/$3" 2> /dev/null
        fi
    fi
    if [[ "$(which wget 2>/dev/null)" != "" ]]
    then
        wget -qO "$1" "$2"
    else [[ "$(which curl)" != "" ]]
        curl -sL "$2" > "$1"
    fi
    if [[ "$AZ_CACHE" != "" ]]
    then
        echo "Saving to: $AZ_CACHE/$3/$download_id"
        mkdir -p "$AZ_CACHE/$3/"
        cp "$1" "$AZ_CACHE/$3/$download_id"
    fi
}

function get_resource_version {
    curl -s --head https://github.com/"$1"/releases/latest | \
        grep -i '^location: ' | sed 's/.*tag\///g' | tr -d '\r'
}

# Both of these have a Python fallback because the Windows ARM64 CI runner has
# no MSYS2 userland. The other Windows jobs get unzip and msgfmt from the Git
# for Windows SDK, which has no usable aarch64 flavor -- so on that runner the
# GNU tools are simply absent. Neither loop below stops on a missing command,
# so without a fallback the ARM64 build would produce an installer with every
# locale empty rather than fail.

# Resolved on first use rather than up front, so that the paths not needing it
# still run on a machine without Python. "python3" is not on PATH under a stock
# Windows Python install; "python" and "py" are.
PYTHON=""
function find_python {
    if [[ "$PYTHON" != "" ]]
    then
        return 0
    fi
    for candidate in python3 python py
    do
        if command -v "$candidate" > /dev/null 2>&1
        then
            PYTHON="$candidate"
            return 0
        fi
    done
    echo "Error: no Python interpreter found on PATH." >&2
    return 1
}

function extract_zip {
    # $1: archive, $2: destination directory (must exist)
    if command -v unzip > /dev/null 2>&1
    then
        unzip "$1" -d "$2" > /dev/null
    else
        find_python && "$PYTHON" -m zipfile -e "$1" "$2"
    fi
}

function compile_po {
    # $1: .po file, $2: .mo file to write
    if command -v msgfmt > /dev/null 2>&1
    then
        msgfmt "$1" -o "$2"
    else
        find_python && "$PYTHON" "$(dirname "$0")/tools/msgfmt.py" "$1" -o "$2"
    fi
}

# Say which one compiled the catalogs. The two are meant to be
# indistinguishable in their output, which also makes it impossible to tell
# from a build log which of them ran -- and "the fallback is exercised in CI"
# is then a belief rather than an observation.
function report_po_compiler {
    if command -v msgfmt > /dev/null 2>&1
    then
        echo "Compiling translations with GNU msgfmt."
    else
        echo "Compiling translations with tools/msgfmt.py (no GNU msgfmt on PATH)."
    fi
}

if [[ "$1" == "--get-pyinstaller" ]]
then
    echo "Downloading pyinstaller..."
    pi_version=$(get_resource_version pyinstaller/pyinstaller)
    download_compat release.zip "https://github.com/pyinstaller/pyinstaller/archive/$pi_version.zip" "pi"
    (
        mkdir pyinstaller
        cd pyinstaller
        extract_zip ../release.zip . && rm ../release.zip
        mv pyinstaller-*/* ./
        rm -r pyinstaller-*
    )
    exit 0
elif [[ "$1" == "--gen-fingerprint" ]]
then
    (
        get_resource_version pyinstaller/pyinstaller
        get_resource_version iwalton3/default-shader-pack
    ) | tee az-cache-fingerprint.list
    exit 0
fi

# Verify versioning
# Note: pyproject.toml derives the version dynamically from constants.py, so it
# is not checked here. constants.py is the single source of truth for the
# Python package; the Inno Setup and Flatpak appdata files must be kept in sync
# manually.
current_version=$(get_resource_version jellyfin/jellyfin-mpv-shim)
current_version=${current_version:1}
constants_version=$(cat jellyfin_mpv_shim/constants.py | grep '^CLIENT_VERSION' | cut -d '"' -f 2)
iss_version=$(grep '^#define MyAppVersion' "Jellyfin MPV Shim.iss" | cut -d '"' -f 2)
appdata_version=$(grep 'release version="' jellyfin_mpv_shim/integration/com.github.iwalton3.jellyfin-mpv-shim.appdata.xml | \
    head -n 1 | cut -d '"' -f 2)

if [[ "$current_version" == "$constants_version" ]]
then
    echo "Warning: This version matches the current published version."
    echo "If you are building a release, the publish will not succeed."
fi

if [[ "$constants_version" != "$iss_version" ]]
then
    echo "Error: The release does not have the same version numbers in all files!"
    echo "Please correct this before releasing!"
    echo "Constants: $constants_version, ISS: $iss_version"
fi

# The appdata is Flathub's changelog, and pre-releases are not published there,
# so it is expected to lag behind a "pre" version rather than match it. It is
# still checked for a stable release, and a pre-release that *did* get an entry
# is flagged, since that entry would ship to Flathub with the next stable build.
if [[ "$constants_version" == *pre* ]]
then
    if [[ "$appdata_version" == "$constants_version" ]]
    then
        echo "Warning: The Flatpak appdata has a release entry for pre-release"
        echo "$constants_version. Pre-releases are not published to Flathub;"
        echo "remove the entry unless you mean to ship it."
    fi
elif [[ "$constants_version" != "$appdata_version" ]]
then
    echo "Error: The release does not have the same version numbers in all files!"
    echo "Please correct this before releasing!"
    echo "Constants: $constants_version, Flatpak: $appdata_version"
fi

# Generate translations. Read from a process substitution rather than a pipe so
# that a failure here stops the build: the loop body of a pipeline runs in a
# subshell, where "exit" ends only the subshell and the script goes on to
# package an installer with no translations in it.
report_po_compiler
while read -r file
do
    compile_po "$file" "${file%.*}.mo" || exit 1
done < <(find -iname '*.po')

# Download default-shader-pack
update_shader_pack="no"
if [[ ! -e "jellyfin_mpv_shim/default_shader_pack" ]]
then
    update_shader_pack="yes"
elif [[ -e ".last_sp_version" ]]
then
    if [[ "$(get_resource_version iwalton3/default-shader-pack)" != "$(cat .last_sp_version)" ]]
    then
        update_shader_pack="yes"
    fi
fi

if [[ "$update_shader_pack" == "yes" ]]
then
    echo "Downloading shaders..."
    sp_version=$(get_resource_version iwalton3/default-shader-pack)
    download_compat release.zip "https://github.com/iwalton3/default-shader-pack/archive/$sp_version.zip" "sp"
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
    pip3 install .[all]
elif [[ "$1" != "--skip-build" ]]
then
    rm -r build/ dist/ .eggs 2> /dev/null
    mkdir build/ dist/
    echo "Building release package."
    python3 -m build > /dev/null
fi

