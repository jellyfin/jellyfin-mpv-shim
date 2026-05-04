@echo off
rd /s /q __pycache__ build 
rd /s /q dist\run
set PATH=%PATH%;%CD%
pyinstaller -c --add-binary "mpv-2.dll;." --add-data "jellyfin_mpv_shim\mouse.lua;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\logo.png;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\default_shader_pack;jellyfin_mpv_shim\default_shader_pack" --add-data "jellyfin_mpv_shim\messages;jellyfin_mpv_shim\messages" --icon jellyfin.ico run.py
