@echo off
rd /s /q __pycache__ dist build
set PATH=%PATH%;%CD%
rem Edge-based build
rem pyinstaller -c --add-binary "mpv-1.dll;." --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\display_mirror\index.html;jellyfin_mpv_shim\display_mirror" --add-data "jellyfin_mpv_shim\webclient_view\webclient;jellyfin_mpv_shim\webclient_view\webclient" --add-data "jellyfin-chromecast\css\jellyfin.css;jellyfin_mpv_shim\display_mirror" --add-binary "Microsoft.Toolkit.Forms.UI.Controls.WebView.dll;." --icon media.ico run-desktop-edge.py
rem CEF-based build
pyinstaller -c --add-binary "mpv-1.dll;." --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\display_mirror\index.html;jellyfin_mpv_shim\display_mirror" --add-data "jellyfin_mpv_shim\webclient_view\webclient;jellyfin_mpv_shim\webclient_view\webclient" --add-data "jellyfin-chromecast\css\jellyfin.css;jellyfin_mpv_shim\display_mirror" --icon media.ico run-desktop.py
xcopy /E /Y cef\cefpython3 dist\run-desktop\
rd /s /q __pycache__ build
pyinstaller -cF --add-binary "mpv-1.dll;." --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\display_mirror\index.html;jellyfin_mpv_shim\display_mirror" --add-data "jellyfin-chromecast\css\jellyfin.css;jellyfin_mpv_shim\display_mirror" --add-binary "Microsoft.Toolkit.Forms.UI.Controls.WebView.dll;." --icon media.ico run.py
pause
