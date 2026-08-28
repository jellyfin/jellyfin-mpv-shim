#!/usr/bin/env python3
"""Build the FriBiDi DLL the Windows installer ships, from pinned source.

Pillow's wheels carry libraqm and HarfBuzz but load FriBiDi at runtime, and
Windows has no such DLL -- so a stock Windows build of this app cannot shape
text at all: right-to-left scripts render reversed and disconnected, and no
script gets GPOS kerning, which the UI's own text measurements depend on.
``jellyfin_mpv_shim/win_fribidi.py`` has the full account; this is the build
half.

**Built rather than downloaded.** MSYS2 publishes a usable
``libfribidi-0.dll`` for all three architectures, and it was the first thing
tried -- but the 32-bit one links ``libgcc_s_dw2-1.dll``, so that route ships
a second file on one architecture and not the others, and it means
redistributing someone else's binary for something that takes under a second
to compile. FriBiDi is meson with **no dependencies**: measured at 0.29s to
configure and 0.54s to build.

**Architecture follows the running interpreter, not a flag.** What has to
match is the Python that loads Pillow -- not the machine, and notably not
mpv, which is a separate question with a separate answer (see
``build_win_vulkan_loader.verify``). The build targets the host through
meson's ``--vsenv`` and the result is then checked against
``sys.executable``; a mismatch is an error rather than a silently mis-filed
DLL, which is the same discipline ``check_win_arch.py`` exists for on
Windows on Arm -- and which the removed 32-bit job, shipping an x64
executable for five green years, is the case history for
(docs/packaging.md section 1).
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request

#: Pinned, with its hash. A build input fetched over the network is a supply
#: chain, and the version is bumped deliberately -- not followed.
VERSION = "1.0.16"
URL = ("https://github.com/fribidi/fribidi/releases/download/"
       "v%s/fribidi-%s.tar.xz" % (VERSION, VERSION))
SHA256 = "1b1cde5b235d40479e91be2f0e88a309e3214c8ab470ec8a2744d82a5a9ea05c"

#: What meson emits on Windows, by toolchain. MSVC drops the ``lib`` prefix;
#: both names are ones Pillow's loader tries, so either is shippable.
_WINDOWS_NAMES = ("fribidi-0.dll", "libfribidi-0.dll")


def _output_names():
    """Accepted library filenames for this platform.

    Windows is the platform this exists for, but the whole script up to the
    last two steps is platform-neutral -- so it stays runnable on Linux and
    macOS, where it produces the ``.so``/``.dylib`` and skips the
    architecture check. That is what lets the fetch, the hash pin and the
    meson invocation be exercised somewhere other than a CI runner, which is
    the only place the Windows path can be.
    """
    if os.name == "nt":
        return _WINDOWS_NAMES
    if sys.platform == "darwin":
        return ("libfribidi.0.dylib", "libfribidi.dylib")
    return ("libfribidi.so.0.4.0", "libfribidi.so.0", "libfribidi.so")


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fetch(dest_dir):
    """Download and verify the source tarball. Returns the extracted dir."""
    os.makedirs(dest_dir, exist_ok=True)
    archive = os.path.join(dest_dir, "fribidi-%s.tar.xz" % VERSION)
    if not os.path.exists(archive):
        print("downloading %s" % URL)
        with urllib.request.urlopen(URL, timeout=120) as response:
            data = response.read()
        with open(archive, "wb") as handle:
            handle.write(data)
    else:
        data = open(archive, "rb").read()

    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        # Removed, so a retry re-downloads rather than re-checking a bad file
        # that is now cached.
        os.unlink(archive)
        raise SystemExit(
            "error: %s hashed %s, expected %s -- refusing to build it"
            % (archive, digest, SHA256))

    source = os.path.join(dest_dir, "fribidi-%s" % VERSION)
    if not os.path.isdir(source):
        with tarfile.open(archive) as tar:
            # `filter` is the 3.12+ spelling and the default from 3.14; named
            # so this does not depend on which of those the runner has.
            try:
                tar.extractall(dest_dir, filter="data")
            except TypeError:
                tar.extractall(dest_dir)
    return source


def build(source, jobs=None):
    """Configure and build. Returns the path to the DLL meson produced."""
    build_dir = os.path.join(source, "_build")
    setup = [
        sys.executable, "-m", "mesonbuild.mesonmain", "setup", build_dir,
        # Everything off but the library: the CLI tool, the docs and the
        # test suite are all build time we have no use for, and `bin` in
        # particular would want a C compiler feature set we do not need.
        "-Ddocs=false", "-Dbin=false", "-Dtests=false",
        "--buildtype=release", "--default-library=shared",
    ]
    if os.name == "nt":
        # Activate the host's Visual Studio environment. Deliberately the
        # HOST's: see the module docstring -- the target is checked against
        # the interpreter afterwards rather than asserted from a flag here.
        setup.append("--vsenv")
    if os.path.isdir(build_dir):
        setup.append("--wipe")

    subprocess.run(setup, cwd=source, check=True)
    compile_cmd = [sys.executable, "-m", "mesonbuild.mesonmain",
                   "compile", "-C", build_dir]
    if jobs:
        compile_cmd += ["-j", str(jobs)]
    subprocess.run(compile_cmd, cwd=source, check=True)

    wanted = _output_names()
    for root, _dirs, files in os.walk(build_dir):
        for name in wanted:
            # By preference order, not by walk order: a build tree can hold
            # both a versioned library and a symlink to it.
            if name in files:
                path = os.path.join(root, name)
                if not os.path.islink(path):
                    return path
    raise SystemExit(
        "error: the build produced none of %s under %s -- meson may have "
        "built a static library" % (", ".join(wanted), build_dir))


def verify(dll):
    """Refuse a DLL the app's own interpreter could not load."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from check_win_arch import interpreter_machine, pe_machine

    want = interpreter_machine()
    got = pe_machine(dll)
    if want != got:
        raise SystemExit(
            "error: built a %s FriBiDi for a %s interpreter. Pillow is loaded "
            "by that interpreter, so this DLL would be refused at runtime and "
            "the build would ship with text shaping silently off."
            % (got, want))
    print("%s: %s, matching the interpreter" % (os.path.basename(dll), got))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", default=None,
                        help="where to fetch and build (default: ./build/fribidi)")
    parser.add_argument("--output-dir", default=None,
                        help="where to place the DLL (default: the repo root, "
                             "beside mpv-2.dll)")
    parser.add_argument("-j", "--jobs", type=int, default=None)
    args = parser.parse_args(argv)

    root = _repo_root()
    work = args.work_dir or os.path.join(root, "build", "fribidi")
    out_dir = args.output_dir or root

    if shutil.which("ninja") is None:
        print("note: ninja is not on PATH; meson may not find a backend",
              file=sys.stderr)

    dll = build(fetch(work), jobs=args.jobs)
    if os.name == "nt":
        verify(dll)

    os.makedirs(out_dir, exist_ok=True)
    # Normalised on Windows, NOT copied under whatever meson called it. The
    # four build-win*.bat files name this file in an --add-binary, and
    # PyInstaller errors on a source path that does not exist -- so the name
    # has to be one thing, not "whichever of the two the toolchain picked".
    # Pillow's loader accepts both spellings, so the choice is free.
    name = "fribidi-0.dll" if os.name == "nt" else os.path.basename(dll)
    target = os.path.join(out_dir, name)
    shutil.copy2(dll, target)
    print("wrote %s" % target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
