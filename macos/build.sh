#!/bin/bash
# Build the macOS app and DMG. Run from the repository root.
# See macos/README.md.
set -e

app="Jellyfin MPV Shim"
version=$(grep '^CLIENT_VERSION' jellyfin_mpv_shim/constants.py | cut -d '"' -f 2)

for input in macos/mpv.app/Contents/MacOS/mpv macos/libfribidi.0.dylib; do
    if [[ ! -e "$input" ]]; then
        echo "error: $input is missing. See macos/README.md." >&2
        exit 1
    fi
done

rm -rf __pycache__ dist build/pyinstaller build/mac
mkdir -p build/mac

# gen_pkg.sh finds the .po files with GNU find syntax, which macOS lacks.
find jellyfin_mpv_shim/messages -name '*.po' | while read -r po; do
    python3 -I tools/msgfmt.py "$po" -o "${po%.*}.mo"
done

# Pillow dlopens the bare name libfribidi.dylib, so the shipped copy needs
# that file name and install name. macos/pyi_rth_macos.py preloads it.
cp macos/libfribidi.0.dylib build/mac/libfribidi.dylib
install_name_tool -id libfribidi.dylib build/mac/libfribidi.dylib 2> /dev/null
codesign --force --sign - build/mac/libfribidi.dylib

# Data goes to "." because a frozen macOS build resolves get_resource()
# against Contents/Resources. renderer.lua is loaded relative to mpvtk.
pyinstaller --noconfirm --windowed \
    --name "$app" \
    --osx-bundle-identifier com.github.iwalton3.jellyfin-mpv-shim \
    --icon jellyfin.icns \
    --workpath build/pyinstaller \
    --runtime-hook macos/pyi_rth_macos.py \
    --exclude-module mpv \
    --hidden-import pystray._darwin \
    --add-binary "build/mac/libfribidi.dylib:." \
    --add-data "macos/systray.png:." \
    --add-data "jellyfin_mpv_shim/logo.png:." \
    --add-data "jellyfin_mpv_shim/lua_probe.lua:." \
    --add-data "jellyfin_mpv_shim/mouse.lua:." \
    --add-data "jellyfin_mpv_shim/trickplay-osc.lua:." \
    --add-data "jellyfin_mpv_shim/thumbfast.lua:." \
    --add-data "jellyfin_mpv_shim/mpvtk/renderer.lua:jellyfin_mpv_shim/mpvtk" \
    --add-data "jellyfin_mpv_shim/default_shader_pack:default_shader_pack" \
    --add-data "jellyfin_mpv_shim/messages:messages" \
    --add-data "jellyfin_mpv_shim/themes:themes" \
    run.py

bundle="dist/$app.app"
contents="$bundle/Contents"
plist="$contents/Info.plist"

# mpv keeps its own bundle. The app looks for Resources/mpv.
mkdir -p "$contents/Helpers"
cp -R macos/mpv.app "$contents/Helpers/mpv.app"
ln -s ../Helpers/mpv.app/Contents/MacOS/mpv "$contents/Resources/mpv"

# The macOS 26 icon. Needs Xcode 26, otherwise jellyfin.icns is used as is.
if xcrun actool macos/jellyfin.icon --compile "$contents/Resources" \
        --app-icon jellyfin --output-partial-info-plist build/mac/icon.plist \
        --platform macosx --target-device mac \
        --minimum-deployment-target 14.0 \
        --enable-on-demand-resources NO --development-region en > /dev/null
then
    plutil -replace CFBundleIconName -string jellyfin "$plist"
else
    echo "warning: actool could not compile macos/jellyfin.icon" >&2
fi

plutil -replace CFBundleShortVersionString -string "$version" "$plist"
plutil -replace CFBundleVersion -string "$version" "$plist"
plutil -replace LSUIElement -bool true "$plist"

# The minimum macOS is the newest one any bundled binary was built for.
floor=$(find "$bundle" -type f \( -perm -u+x -o -name '*.dylib' -o -name '*.so' \) \
        -exec otool -l {} \; 2> /dev/null |
    awk '/LC_BUILD_VERSION/ {b=1} b && $1 == "minos" {print $2; b=0}' |
    sort -t. -k1,1n -k2,2n | tail -1)
echo "Minimum macOS for this build: $floor"
plutil -replace LSMinimumSystemVersion -string "$floor" "$plist"

# Ad hoc signature only, see macos/README.md.
codesign --force --deep --sign - "$bundle"
codesign --verify --deep --strict "$bundle"

dmgbuild -s macos/dmg_settings.py \
    -D app="$bundle" -D icon="$contents/Resources/jellyfin.icns" \
    "$app" dist/jellyfin-mpv-shim_version.dmg
