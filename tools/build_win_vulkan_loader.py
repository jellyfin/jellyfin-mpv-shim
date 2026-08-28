#!/usr/bin/env python3
"""Build the Vulkan loader DLL the Windows installer ships, from pinned source.

shinchiro's libmpv links ``vulkan-1.dll`` as a **hard** import from the
20260808 release onward -- twelve ``vk*`` symbols in the import table, empty
delay-load directory. The Vulkan loader is installed by the GPU driver and is
not part of Windows, so on a machine without one the loader refuses
``mpv-2.dll`` before a line of mpv runs, and the way that surfaces hides the
cause twice over (``tools/check_win_libmpv_deps.py`` has the full account).
Shipping one beside the DLL is what lets the mpv pin move again -- and the
pin has to move, because the fix for #687 exists only in mpv master.

**Not a workaround for a bug we expect to go away.** Upstream mpv's own
Windows builds ship ``vulkan-1.dll`` next to ``mpv.exe`` -- their
``ci/build-win32.ps1`` builds the loader as a subproject and copies it into
the artifact -- so bundling it is what the platform's reference build already
does, not a liberty this project is taking.

**Measured, under wine, against shinchiro's 20260828 libmpv** (Windows Python
embeddable, ``ctypes.CDLL("mpv-2.dll")``, ``WINEDLLOVERRIDES=vulkan-1=n`` so
the builtin loader cannot answer):

* no loader anywhere: ``Could not find module 'mpv-2.dll' (or one of its
  dependencies)`` -- the exact misleading message users reported;
* a 536 KB ``vulkan-1.dll`` beside the executable: loads, and
  ``mpv_initialize`` with ``vo=gpu-next --gpu-api=vulkan`` returns 0.

The DLL sits beside ``mpv-2.dll``, which under PyInstaller is beside the
executable -- the first directory Windows searches. It resolves ICDs through
the registry like any loader, so an app-local copy drives whatever driver the
machine has.

**Built rather than downloaded**, the same call ``build_win_fribidi.py``
made and for the same reasons: the alternative is redistributing someone
else's binary, and the sources are two tarballs and a CMake run.

**Architecture follows the toolchain CMake picks**, which on a runner is the
Visual Studio environment the job is already in, and the result is then
checked -- against ``mpv-2.dll``, which is the file that imports it. Not a
formality: the 32-bit job shipped an x64 executable with an i686 libmpv for
five years and no build ever went red over it, because a machine-type
mismatch is not a build error anywhere (docs/packaging.md section 1).
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request

#: Pinned, with hashes. A build input fetched over the network is a supply
#: chain, and the version is bumped deliberately -- not followed. The loader
#: and the headers move together: they are one SDK tag, and a loader built
#: against mismatched headers is the one combination that compiles and then
#: reports the wrong API version at runtime.
VERSION = "1.4.357.0"
_TAG = "vulkan-sdk-%s" % VERSION
_ARCHIVE = ("https://github.com/KhronosGroup/%s/archive/refs/tags/"
            "%s.tar.gz")
SOURCES = {
    "Vulkan-Headers":
        "e87dce08116151f6b6d7de6b6faf41498e87e6cf848ff16fa3bd5402190ad4a3",
    "Vulkan-Loader":
        "54f2537df22313768da0317dda2abdaaab7711b4081c48c869a79db343d0ae70",
}

#: What CMake emits, by platform. Only the Windows name is shipped; the
#: others exist so the fetch, the hash pin and the CMake invocation stay
#: runnable on a developer's machine, which is the only place they get
#: exercised outside a CI runner.
_OUTPUT_NAMES = {
    "nt": ("vulkan-1.dll",),
    "darwin": ("libvulkan.1.dylib", "libvulkan.dylib"),
}
_DEFAULT_OUTPUTS = ("libvulkan.so.1", "libvulkan.so")


def _output_names():
    if os.name == "nt":
        return _OUTPUT_NAMES["nt"]
    if sys.platform == "darwin":
        return _OUTPUT_NAMES["darwin"]
    return _DEFAULT_OUTPUTS


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fetch_one(dest_dir, name, sha256):
    archive = os.path.join(dest_dir, "%s-%s.tar.gz" % (name, _TAG))
    url = _ARCHIVE % (name, _TAG)
    if os.path.exists(archive):
        data = open(archive, "rb").read()
    else:
        print("downloading %s" % url)
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        with open(archive, "wb") as handle:
            handle.write(data)

    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256:
        # Removed, so a retry re-downloads rather than re-checking a bad file
        # that is now cached.
        os.unlink(archive)
        raise SystemExit(
            "error: %s hashed %s, expected %s -- refusing to build it"
            % (archive, digest, sha256))

    source = os.path.join(dest_dir, "%s-%s" % (name, _TAG))
    if not os.path.isdir(source):
        with tarfile.open(archive) as tar:
            # `filter` is the 3.12+ spelling and the default from 3.14; named
            # so this does not depend on which of those the runner has.
            try:
                tar.extractall(dest_dir, filter="data")
            except TypeError:
                tar.extractall(dest_dir)
    return source


def fetch(dest_dir):
    """Download and verify both tarballs. Returns (headers, loader) dirs."""
    os.makedirs(dest_dir, exist_ok=True)
    return tuple(_fetch_one(dest_dir, name, SOURCES[name])
                 for name in ("Vulkan-Headers", "Vulkan-Loader"))


def _cmake(args, cwd):
    subprocess.run(["cmake"] + args, cwd=cwd, check=True)


def build(headers_src, loader_src, jobs=None):
    """Configure and build. Returns the path to the loader CMake produced."""
    prefix = os.path.join(os.path.dirname(headers_src), "_prefix")

    # The headers are a header-only install, but the loader wants them
    # *installed* -- VULKAN_HEADERS_INSTALL_DIR is a prefix, not a source
    # tree. Done here rather than with UPDATE_DEPS=ON, which is how
    # upstream's own CI does it: that clones a revision chosen by a file in
    # the tarball, at build time, with no hash. The whole point of pinning
    # is lost if the build then fetches its own dependency.
    hdr_build = os.path.join(headers_src, "_build")
    _cmake(["-S", ".", "-B", hdr_build, "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_INSTALL_PREFIX=" + prefix], headers_src)
    _cmake(["--install", hdr_build], headers_src)

    build_dir = os.path.join(loader_src, "_build")
    configure = [
        "-S", ".", "-B", build_dir,
        "-DCMAKE_BUILD_TYPE=Release",
        "-DVULKAN_HEADERS_INSTALL_DIR=" + prefix,
        # Off: the test suite wants googletest (another unpinned fetch) and
        # proves nothing about a loader we are only asking to load.
        "-DBUILD_TESTS=OFF",
        # Refuse the network explicitly. Without it a missing dependency is
        # a silent clone rather than a failed build, which is the one
        # outcome a pinned build must not have.
        "-DUPDATE_DEPS=OFF",
    ]
    if os.name != "nt":
        # WSI backends the shipped artifact has no use for, and which drag
        # in X11/Wayland headers that a build machine need not have. On
        # Windows the only surface is Win32 and there is nothing to turn
        # off.
        configure += ["-DBUILD_WSI_XCB_SUPPORT=OFF",
                      "-DBUILD_WSI_XLIB_SUPPORT=OFF",
                      "-DBUILD_WSI_WAYLAND_SUPPORT=OFF"]
    _cmake(configure, loader_src)

    compile_cmd = ["--build", build_dir, "--config", "Release"]
    if jobs:
        compile_cmd += ["--parallel", str(jobs)]
    _cmake(compile_cmd, loader_src)

    wanted = _output_names()
    for root, _dirs, files in os.walk(build_dir):
        for name in wanted:
            # By preference order, not by walk order. Resolved rather than
            # skipped-if-a-symlink: the ELF build's real file is
            # `libvulkan.so.<version>`, which is a name this cannot know,
            # and both the names it does know are links to it. (Windows,
            # the platform this ships for, has one real `vulkan-1.dll` and
            # realpath is a no-op there.)
            if name in files:
                return os.path.realpath(os.path.join(root, name))
    raise SystemExit("error: the build produced none of %s under %s"
                     % (", ".join(wanted), build_dir))


def verify(dll, against=None):
    """Refuse a loader of the wrong architecture.

    Checked against **mpv-2.dll**, not against the interpreter -- and that
    is the one place this differs from ``build_win_fribidi.verify``. FriBiDi
    is loaded by Pillow, so it must match the Python that runs; this is a
    hard import *of libmpv*, resolved by Windows while loading that file, so
    what it must match is libmpv. In a correct build those are the same
    thing, and asking the wrong one anyway is free -- the removed 32-bit job
    was a build where they differed for five years running
    (docs/packaging.md section 1).

    Falls back to the interpreter when mpv-2.dll is not beside us, so the
    tool stays runnable on its own.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from check_win_arch import NotAPeFile, interpreter_machine, pe_machine

    subject = "the interpreter"
    want = interpreter_machine()
    if against and os.path.exists(against):
        try:
            want = pe_machine(against)
            subject = os.path.basename(against)
        except (OSError, NotAPeFile):
            pass
    got = pe_machine(dll)
    if want != got:
        raise SystemExit(
            "error: built a %s Vulkan loader against a %s %s. libmpv "
            "hard-imports this, so Windows would refuse mpv-2.dll and the "
            "build would ship a client that cannot start."
            % (got, want, subject))
    print("%s: %s, matching %s" % (os.path.basename(dll), got, subject))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--output", default=None,
                        help="where to put the library (default: repo root)")
    parser.add_argument("--work", default=None,
                        help="scratch directory for the source tree")
    parser.add_argument("-j", "--jobs", type=int, default=None)
    args = parser.parse_args(argv)

    root = _repo_root()
    work = args.work or os.path.join(root, "_vulkan_loader_build")
    headers_src, loader_src = fetch(work)
    built = build(headers_src, loader_src, jobs=args.jobs)
    if os.name == "nt":
        verify(built, os.path.join(root, "mpv-2.dll"))

    output = args.output or os.path.join(root, os.path.basename(built))
    if os.path.isdir(output):
        output = os.path.join(output, os.path.basename(built))
    shutil.copy2(built, output)
    print("wrote %s (%d bytes)" % (output, os.path.getsize(output)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
