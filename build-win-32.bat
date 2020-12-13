@echo off
rd /s /q __pycache__ dist build
set PATH=%PATH%;%CD%

rem ATTENTION: This file is broken. PyInstaller still packages the 64 bit version of libmpv for some reason.
rem The desktop version (and corresponding shim shortcut) do work, however.

rem Edge-based build
rem "C:\Program Files (x86)\Python37-32\Scripts\pyinstaller" -w --add-binary "mpv32\mpv-1.dll;." --add-data "jellyfin_mpv_shim\mouse.lua;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\webclient_view\webclient;jellyfin_mpv_shim\webclient_view\webclient" --add-data "jellyfin_mpv_shim\display_mirror\index.html;jellyfin_mpv_shim\display_mirror" --add-data "jellyfin_mpv_shim\display_mirror\jellyfin.css;jellyfin_mpv_shim\display_mirror" --add-binary "Microsoft.Toolkit.Forms.UI.Controls.WebView.dll;." --icon jellyfin.ico run-desktop-edge.py
rem CEF-based build
pyinstaller -w --add-binary "mpv32\mpv-1.dll;." --add-data "jellyfin_mpv_shim\mouse.lua;jellyfin_mpv_shim" --hidden-import pystray._win32 --add-data "jellyfin_mpv_shim\default_shader_pack;jellyfin_mpv_shim\default_shader_pack" --add-data "jellyfin_mpv_shim\messages;jellyfin_mpv_shim\messages" --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\webclient_view\webclient;jellyfin_mpv_shim\webclient_view\webclient" --add-data "jellyfin_mpv_shim\display_mirror\index.html;jellyfin_mpv_shim\display_mirror" --add-data "jellyfin_mpv_shim\display_mirror\jellyfin.css;jellyfin_mpv_shim\display_mirror" --icon jellyfin.ico run-desktop.py
if %errorlevel% neq 0 exit /b %errorlevel%
xcopy /E /Y cef32\cefpython3 dist\run-desktop\
xcopy /E /Y mpv32\mpv-1.dll dist\run-desktop\
del dist\run-desktop\run-desktop.exe.manifest
copy hidpi.manifest dist\run-desktop\run-desktop.exe.manifest
rem rd /s /q __pycache__ build
rem "C:\Program Files (x86)\Python37-32\Scripts\pyinstaller" -wF --add-binary "mpv32\mpv-1.dll;." --add-data "jellyfin_mpv_shim\mouse.lua;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\display_mirror\index.html;jellyfin_mpv_shim\display_mirror" --add-data "jellyfin_mpv_shim\display_mirror\jellyfin.css;jellyfin_mpv_shim\display_mirror" --add-binary "Microsoft.Toolkit.Forms.UI.Controls.WebView.dll;." --exclude-module cefpython3 --icon jellyfin.ico run.py
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" "Jellyfin MPV Desktop.iss"
if %errorlevel% neq 0 exit /b %errorlevel%