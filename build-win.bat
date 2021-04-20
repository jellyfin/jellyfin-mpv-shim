@echo off
rd /s /q __pycache__ dist build
set PATH=%PATH%;%CD%
pyinstaller -w --add-binary "mpv-1.dll;." --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --hidden-import pystray._win32 --add-data "jellyfin_mpv_shim\mouse.lua;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\default_shader_pack;jellyfin_mpv_shim\default_shader_pack" --add-data "jellyfin_mpv_shim\messages;jellyfin_mpv_shim\messages" --add-data "jellyfin_mpv_shim\display_mirror\index.html;jellyfin_mpv_shim\display_mirror" --add-data "jellyfin_mpv_shim\display_mirror\jellyfin.css;jellyfin_mpv_shim\display_mirror" --add-binary "Microsoft.Toolkit.Forms.UI.Controls.WebView.dll;." --icon jellyfin.ico run.py
if %errorlevel% neq 0 exit /b %errorlevel%
rd /s /q __pycache__ build
pyinstaller -wF --add-binary "mpv-1.dll;." --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --hidden-import pystray._win32 --add-data "jellyfin_mpv_shim\mouse.lua;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\default_shader_pack;jellyfin_mpv_shim\default_shader_pack" --add-data "jellyfin_mpv_shim\messages;jellyfin_mpv_shim\messages" --add-data "jellyfin_mpv_shim\display_mirror\index.html;jellyfin_mpv_shim\display_mirror" --add-data "jellyfin_mpv_shim\display_mirror\jellyfin.css;jellyfin_mpv_shim\display_mirror" --add-binary "Microsoft.Toolkit.Forms.UI.Controls.WebView.dll;." --icon jellyfin.ico run.py
if %errorlevel% neq 0 exit /b %errorlevel%
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" "Jellyfin MPV Shim.iss"
if %errorlevel% neq 0 exit /b %errorlevel%
