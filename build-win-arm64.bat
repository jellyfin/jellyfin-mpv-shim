@echo off
rem Native ARM64 build. Identical to build-win.bat except that ISCC is given
rem /DArm64 (see "Jellyfin MPV Shim.iss") and is located rather than assumed:
rem the ARM64 runner image has Inno Setup preinstalled by Chocolatey, and the
rem other jobs install it with winget, which do not have to agree on a path.
rem
rem PyInstaller targets whatever the running Python is, so an ARM64 Python and
rem the aarch64 libmpv are the whole of what makes this build ARM64.
rd /s /q __pycache__ dist build
set PATH=%PATH%;%CD%
pyinstaller -w --manifest hidpi.manifest --add-binary "mpv-2.dll;." --add-data "jellyfin_mpv_shim\systray.png;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\logo.png;jellyfin_mpv_shim" --hidden-import pystray._win32 --add-data "jellyfin_mpv_shim\lua_probe.lua;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\mouse.lua;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\trickplay-osc.lua;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\thumbfast.lua;jellyfin_mpv_shim" --add-data "jellyfin_mpv_shim\mpvtk\renderer.lua;jellyfin_mpv_shim\mpvtk" --add-data "jellyfin_mpv_shim\default_shader_pack;jellyfin_mpv_shim\default_shader_pack" --add-data "jellyfin_mpv_shim\messages;jellyfin_mpv_shim\messages" --add-data "jellyfin_mpv_shim\themes;jellyfin_mpv_shim\themes" --icon jellyfin.ico run.py
if %errorlevel% neq 0 exit /b %errorlevel%

set ISCC=
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe
if not defined ISCC (
    echo Could not find ISCC.exe. Install Inno Setup 6.
    exit /b 1
)

"%ISCC%" /DArm64 "Jellyfin MPV Shim.iss"
if %errorlevel% neq 0 exit /b %errorlevel%
