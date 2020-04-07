@echo off
rd /s /q __pycache__ dist build
set PATH=%PATH%;%CD%

rem ATTENTION: This file is broken. PyInstaller still packages the 64 bit version of libmpv for some reason.

rem Edge-based build
rem "C:\Program Files (x86)\Python37-32\Scripts\pyinstaller" -w --add-binary "mpv32\mpv-1.dll;." --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\webclient_view\webclient;jellyfin_mpv_shim\webclient_view\webclient" --add-data "jellyfin_mpv_shim\display_mirror\index.html;jellyfin_mpv_shim\display_mirror" --add-data "jellyfin_mpv_shim\display_mirror\jellyfin.css;jellyfin_mpv_shim\display_mirror" --add-binary "Microsoft.Toolkit.Forms.UI.Controls.WebView.dll;." --icon jellyfin.ico run-desktop-edge.py
rem CEF-based build
"C:\Program Files (x86)\Python37-32\Scripts\pyinstaller" -w --add-binary "mpv32\mpv-1.dll;." --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\webclient_view\webclient;jellyfin_mpv_shim\webclient_view\webclient" --add-data "jellyfin_mpv_shim\display_mirror\index.html;jellyfin_mpv_shim\display_mirror" --add-data "jellyfin_mpv_shim\display_mirror\jellyfin.css;jellyfin_mpv_shim\display_mirror" --icon jellyfin.ico run-desktop.py
xcopy /E /Y cef32\cefpython3 dist\run-desktop\
rd /s /q __pycache__ build
"C:\Program Files (x86)\Python37-32\Scripts\pyinstaller" -wF --add-binary "mpv32\mpv-1.dll;." --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\display_mirror\index.html;jellyfin_mpv_shim\display_mirror" --add-data "jellyfin_mpv_shim\display_mirror\jellyfin.css;jellyfin_mpv_shim\display_mirror" --add-binary "Microsoft.Toolkit.Forms.UI.Controls.WebView.dll;." --exclude-module cefpython3 --icon jellyfin.ico run.py
"C:\Program Files (x86)\NSIS\makensis.exe" "Jellyfin MPV Desktop.nsi"
