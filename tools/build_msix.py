#!/usr/bin/env python3
"""Wrap the PyInstaller output (dist/run) in an MSIX for the Microsoft Store.

The package is left **unsigned**: the Store re-signs everything it accepts,
and that signature is the point of shipping there -- Smart App Control trusts
it, and it covers every unsigned .pyd and DLL in the bundle, which signing our
own exe would not (docs/packaging.md §6).

The Identity values have to match what Partner Center reserved for the app,
character for character, or the upload is refused. Until there is an account
they are placeholders; CI passes the repo variables ``MSIX_IDENTITY_NAME`` and
``MSIX_PUBLISHER``, and an empty one falls back to the placeholder.
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape, quoteattr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PLACEHOLDER_NAME = "JellyfinMPVShim.Placeholder"
PLACEHOLDER_PUBLISHER = "CN=00000000-0000-0000-0000-000000000000"

DESCRIPTION = "Cast-target and library browser for Jellyfin, playing through mpv"

#: (file name, size) of every logo the manifest names. The 44px one is the
#: taskbar and Start list; the targetsize variant keeps it sharp at high DPI.
LOGOS = [
    ("StoreLogo.png", (50, 50)),
    ("Square44x44Logo.png", (44, 44)),
    ("Square44x44Logo.targetsize-256.png", (256, 256)),
    ("Square150x150Logo.png", (150, 150)),
    ("Wide310x150Logo.png", (310, 150)),
]

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
         xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
         xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
         IgnorableNamespaces="uap rescap">
  <Identity Name={name} Publisher={publisher} Version={version} ProcessorArchitecture={arch} />
  <Properties>
    <DisplayName>Jellyfin MPV Shim</DisplayName>
    <PublisherDisplayName>{publisher_display}</PublisherDisplayName>
    <Logo>Assets\\StoreLogo.png</Logo>
  </Properties>
  <Dependencies>
    <TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.17763.0" MaxVersionTested="10.0.26100.0" />
  </Dependencies>
  <Resources>
    <Resource Language="en-us" />
  </Resources>
  <Applications>
    <Application Id="JellyfinMPVShim" Executable="run.exe" EntryPoint="Windows.FullTrustApplication">
      <uap:VisualElements DisplayName="Jellyfin MPV Shim" Description={description}
                          BackgroundColor="transparent"
                          Square150x150Logo="Assets\\Square150x150Logo.png"
                          Square44x44Logo="Assets\\Square44x44Logo.png">
        <uap:DefaultTile Wide310x150Logo="Assets\\Wide310x150Logo.png" />
      </uap:VisualElements>
    </Application>
  </Applications>
  <Capabilities>
    <Capability Name="internetClient" />
    <rescap:Capability Name="runFullTrust" />
  </Capabilities>
</Package>
"""


def client_version():
    with open(os.path.join(ROOT, "jellyfin_mpv_shim", "constants.py"),
              encoding="utf-8") as fh:
        match = re.search(r'^CLIENT_VERSION\s*=\s*"([^"]+)"', fh.read(), re.M)
    return match.group(1)


def msix_version(version):
    """``3.2.0`` -> ``3.2.0.0``.

    The fourth field is the Store's: it must be 0 in a submission. A
    pre-release spelling (``3.3.0pre1``) keeps only its numbers, so it is
    indistinguishable from the release it precedes -- fine for a test
    package, and the reason pre-releases are not uploaded (docs/packaging.md
    §6).
    """
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise ValueError("cannot make an MSIX version of %r" % version)
    return "%s.%s.%s.0" % match.groups()


def render_manifest(version, arch, name="", publisher="",
                    publisher_display="Izzie Walton"):
    return MANIFEST.format(
        name=quoteattr(name or PLACEHOLDER_NAME),
        publisher=quoteattr(publisher or PLACEHOLDER_PUBLISHER),
        version=quoteattr(msix_version(version)),
        arch=quoteattr(arch),
        publisher_display=escape(publisher_display),
        description=quoteattr(DESCRIPTION),
    )


def render_logos(dest):
    from PIL import Image

    src = Image.open(os.path.join(ROOT, "jellyfin_mpv_shim", "logo.png"))
    src = src.convert("RGBA")
    os.makedirs(dest, exist_ok=True)
    for filename, (w, h) in LOGOS:
        side = min(w, h)
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        canvas.paste(src.resize((side, side), Image.LANCZOS),
                     ((w - side) // 2, (h - side) // 2))
        canvas.save(os.path.join(dest, filename))


def find_makeappx():
    """The newest SDK's makeappx, preferring one native to this machine.

    The Windows ARM64 runner may only have the x64 tool, which runs there
    under emulation, so any architecture is accepted as a fallback.
    """
    kits = os.path.join(os.environ.get("ProgramFiles(x86)",
                                       r"C:\Program Files (x86)"),
                        "Windows Kits", "10", "bin")
    found = sorted(glob.glob(os.path.join(kits, "10.*", "*", "makeappx.exe")),
                   reverse=True)
    native = "arm64" if "ARM" in os.environ.get(
        "PROCESSOR_ARCHITECTURE", "").upper() else "x64"
    for path in found:
        if os.path.basename(os.path.dirname(path)).lower() == native:
            return path
    if found:
        return found[0]
    raise SystemExit("makeappx.exe not found under %s" % kits)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arch", required=True, choices=("x64", "arm64"))
    parser.add_argument("--dist", default=os.path.join(ROOT, "dist", "run"))
    parser.add_argument("--out", required=True, help="the .msix to write")
    parser.add_argument("--identity-name",
                        default=os.environ.get("MSIX_IDENTITY_NAME", ""))
    parser.add_argument("--publisher",
                        default=os.environ.get("MSIX_PUBLISHER", ""))
    parser.add_argument("--makeappx", help="path to makeappx.exe")
    parser.add_argument("--stage-only", action="store_true",
                        help="lay out the package directory and stop")
    args = parser.parse_args()

    if not os.path.isfile(os.path.join(args.dist, "run.exe")):
        raise SystemExit("%s has no run.exe; run build-win.bat first"
                         % args.dist)
    stage = os.path.join(ROOT, "build", "msix-" + args.arch)
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(args.dist, stage)
    render_logos(os.path.join(stage, "Assets"))
    with open(os.path.join(stage, "AppxManifest.xml"), "w",
              encoding="utf-8") as fh:
        fh.write(render_manifest(client_version(), args.arch,
                                 args.identity_name, args.publisher))
    if not (args.identity_name and args.publisher):
        print("build_msix: Identity is a PLACEHOLDER -- this package cannot "
              "be submitted to the Store as built.")
    if args.stage_only:
        print("build_msix: staged", stage)
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    tool = args.makeappx or find_makeappx()
    print("build_msix: using", tool)
    return subprocess.call([tool, "pack", "/o", "/d", stage, "/p", args.out])


if __name__ == "__main__":
    sys.exit(main())
