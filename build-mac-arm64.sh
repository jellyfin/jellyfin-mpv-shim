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

codesign --force --deep --sign - "${APP_PATH}"
