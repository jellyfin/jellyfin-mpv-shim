#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

rm -rf __pycache__ dist build

pyinstaller -w \
  --icon jellyfin.icns \
  --name "Jellyfin MPV Shim" \
  --osx-bundle-identifier "com.github.iwalton3.jellyfin-mpv-shim" \
  --hidden-import pystray._darwin \
  --add-data "jellyfin_mpv_shim/systray.png:jellyfin_mpv_shim" \
  --add-data "jellyfin_mpv_shim/logo.png:jellyfin_mpv_shim" \
  --add-data "jellyfin_mpv_shim/lua_probe.lua:jellyfin_mpv_shim" \
  --add-data "jellyfin_mpv_shim/mouse.lua:jellyfin_mpv_shim" \
  --add-data "jellyfin_mpv_shim/trickplay-osc.lua:jellyfin_mpv_shim" \
  --add-data "jellyfin_mpv_shim/thumbfast.lua:jellyfin_mpv_shim" \
  --add-data "jellyfin_mpv_shim/mpvtk/renderer.lua:jellyfin_mpv_shim/mpvtk" \
  --add-data "jellyfin_mpv_shim/default_shader_pack:jellyfin_mpv_shim/default_shader_pack" \
  --add-data "jellyfin_mpv_shim/messages:jellyfin_mpv_shim/messages" \
  --add-data "jellyfin_mpv_shim/themes:jellyfin_mpv_shim/themes" \
  run.py

APP_PATH="dist/Jellyfin MPV Shim.app"

MPV_APP=$(find mpv-bundle -name "mpv.app" -type d 2>/dev/null | head -n 1)
if [ -n "$MPV_APP" ] && [ -d "$MPV_APP" ]; then
    echo "Bundling mpv from ${MPV_APP} into ${APP_PATH}..."
    cp "${MPV_APP}/Contents/MacOS/mpv" "${APP_PATH}/Contents/MacOS/mpv"
    cp -R "${MPV_APP}/Contents/MacOS/lib" "${APP_PATH}/Contents/MacOS/"
    mkdir -p "${APP_PATH}/Contents/Resources"
    ln -sf ../MacOS/mpv "${APP_PATH}/Contents/Resources/mpv"
    chmod +x "${APP_PATH}/Contents/MacOS/mpv"
fi

# Bundle MoltenVK driver
MOLTENVK_LIB=""
if command -v brew >/dev/null 2>&1; then
    BREW_PREFIX=$(brew --prefix molten-vk 2>/dev/null || true)
    if [ -n "$BREW_PREFIX" ] && [ -f "${BREW_PREFIX}/lib/libMoltenVK.dylib" ]; then
        MOLTENVK_LIB="${BREW_PREFIX}/lib/libMoltenVK.dylib"
    fi
fi
if [ -z "$MOLTENVK_LIB" ]; then
    if [ -f "/opt/homebrew/lib/libMoltenVK.dylib" ]; then
        MOLTENVK_LIB="/opt/homebrew/lib/libMoltenVK.dylib"
    elif [ -f "/usr/local/lib/libMoltenVK.dylib" ]; then
        MOLTENVK_LIB="/usr/local/lib/libMoltenVK.dylib"
    fi
fi

if [ -n "$MOLTENVK_LIB" ] && [ -f "$MOLTENVK_LIB" ]; then
    echo "Bundling MoltenVK driver from ${MOLTENVK_LIB} into ${APP_PATH}..."
    mkdir -p "${APP_PATH}/Contents/MacOS/lib"
    cp "${MOLTENVK_LIB}" "${APP_PATH}/Contents/MacOS/lib/libMoltenVK.dylib"
    chmod 755 "${APP_PATH}/Contents/MacOS/lib/libMoltenVK.dylib"
    install_name_tool -id "@rpath/libMoltenVK.dylib" "${APP_PATH}/Contents/MacOS/lib/libMoltenVK.dylib" 2>/dev/null || true
    mkdir -p "${APP_PATH}/Contents/Resources/vulkan/icd.d"
    cat << 'EOF' > "${APP_PATH}/Contents/Resources/vulkan/icd.d/MoltenVK_icd.json"
{
    "file_format_version": "1.0.0",
    "ICD": {
        "library_path": "../../../MacOS/lib/libMoltenVK.dylib",
        "api_version": "1.4.0",
        "is_portability_driver": true
    }
}
EOF
else
    echo "ERROR: MoltenVK driver not found. Install molten-vk (e.g. brew install molten-vk) to build self-contained macOS bundle." >&2
    exit 1
fi

codesign --force --deep --sign - "${APP_PATH}"
