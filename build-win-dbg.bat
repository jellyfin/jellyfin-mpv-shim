@echo off
rd /s /q __pycache__ build 
rd /s /q dist\run-desktop
set PATH=%PATH%;%CD%
rem Edge-based build
rem pyinstaller -c --add-binary "mpv-1.dll;." --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\display_mirror\index.html;jellyfin_mpv_shim\display_mirror" --add-data "jellyfin_mpv_shim\webclient_view\webclient;jellyfin_mpv_shim\webclient_view\webclient" --add-data "jellyfin_mpv_shim\display_mirror\jellyfin.css;jellyfin_mpv_shim\display_mirror" --add-binary "Microsoft.Toolkit.Forms.UI.Controls.WebView.dll;." --icon jellyfin.ico run-desktop-edge.py
rem CEF-based build
pyinstaller -c --add-binary "mpv-1.dll;." --add-data "jellyfin_mpv_shim\mouse.lua;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\default_shader_pack;jellyfin_mpv_shim\default_shader_pack" --add-data "jellyfin_mpv_shim\messages;jellyfin_mpv_shim\messages" --add-data "jellyfin_mpv_shim\display_mirror\index.html;jellyfin_mpv_shim\display_mirror" --add-data "jellyfin_mpv_shim\webclient_view\webclient;jellyfin_mpv_shim\webclient_view\webclient" --add-data "jellyfin_mpv_shim\display_mirror\jellyfin.css;jellyfin_mpv_shim\display_mirror" --icon jellyfin.ico run-desktop.py
xcopy /E /Y cef\cefpython3 dist\run-desktop\
del dist\run-desktop\run-desktop.exe.manifest
copy hidpi.manifest dist\run-desktop\run-desktop.exe.manifest
rem rd /s /q __pycache__ build
rem pyinstaller -cF --add-binary "mpv-1.dll;." --add-data "jellyfin_mpv_shim\mouse.lua;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\default_shader_pack;jellyfin_mpv_shim\default_shader_pack" --add-data "jellyfin_mpv_shim\messages;jellyfin_mpv_shim\messages" --add-data "jellyfin_mpv_shim\display_mirror\index.html;jellyfin_mpv_shim\display_mirror" --add-data "jellyfin_mpv_shim\display_mirror\jellyfin.css;jellyfin_mpv_shim\display_mirror" --add-binary "Microsoft.Toolkit.Forms.UI.Controls.WebView.dll;." --exclude-module cefpython3 --icon jellyfin.ico run.py
