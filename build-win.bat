@echo off
rd /s /q __pycache__ dist build
set PATH=%PATH%;%CD%
pyinstaller -w --add-binary "mpv-2.dll;." --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --hidden-import pystray._win32 --add-data "jellyfin_mpv_shim\mouse.lua;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\trickplay-osc.lua;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\thumbfast.lua;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\default_shader_pack;jellyfin_mpv_shim\default_shader_pack" --add-data "jellyfin_mpv_shim\messages;jellyfin_mpv_shim\messages" --add-data "jellyfin_mpv_shim\display_mirror\index.html;jellyfin_mpv_shim\display_mirror" --add-data "jellyfin_mpv_shim\display_mirror\jellyfin.css;jellyfin_mpv_shim\display_mirror" --icon jellyfin.ico run.py
if %errorlevel% neq 0 exit /b %errorlevel%
del dist\run\run.exe.manifest
copy hidpi.manifest dist\run\run.exe.manifest
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" "Jellyfin MPV Shim.iss"
if %errorlevel% neq 0 exit /b %errorlevel%
