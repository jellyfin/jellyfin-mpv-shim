#!/usr/bin/env python3
"""Assert that a Windows libmpv imports nothing the installer does not ship.

The shim loads ``mpv-2.dll`` with ctypes at runtime, so a DLL it cannot load
is not a build error -- it is a client that dies at startup. And the way it
dies hides the cause twice over: ``python-mpv`` raises ``OSError``, ``player``
reads that as "no libmpv" and switches to the external backend, and the
external backend fails looking for an ``mpv.exe`` the Windows build has never
shipped. The traceback that reaches the user names ``subprocess``.

That is exactly what shipped in 3.0.0pre12. shinchiro's builds from 20260808
onward link ``vulkan-1.dll`` as a *hard* import rather than loading it
dynamically, and the Vulkan loader is installed by the GPU driver -- so on a
machine without one, ``LoadLibrary`` refuses ``mpv-2.dll`` outright.

Nothing downstream can notice: the build machine never loads the DLL, and a
developer's machine almost certainly has a Vulkan loader, so the one place the
question can be asked cheaply is the import table itself.

So: read the imports, and check every name against the set of DLLs a stock
Windows install actually has. A name that is not on that list is either
something to ship beside the DLL or something to confirm is a system component
and add to ``SYSTEM_DLLS`` -- it is a lead, not a verdict.
"""

import argparse
import struct
import sys
from typing import List, Optional, Set, Tuple

# DLLs present on a stock Windows install, so an import of one needs nothing
# shipped with it. This is the union of what libmpv has been observed to want
# across the builds this project has used; add to it only after confirming the
# name really is a Windows component, because the whole value of the check is
# that an unfamiliar name stops the build.
SYSTEM_DLLS: Set[str] = {
    "advapi32.dll",
    "avicap32.dll",
    "avrt.dll",
    "bcrypt.dll",
    "bcryptprimitives.dll",
    "cfgmgr32.dll",
    "crypt32.dll",
    "d2d1.dll",
    "d3d11.dll",
    "dwmapi.dll",
    "dwrite.dll",
    "dxgi.dll",
    "gdi32.dll",
    "imm32.dll",
    "iphlpapi.dll",
    "kernel32.dll",
    "msvcrt.dll",
    "normaliz.dll",
    "ntdll.dll",
    "ole32.dll",
    "oleaut32.dll",
    "opengl32.dll",
    "secur32.dll",
    "setupapi.dll",
    "shcore.dll",
    "shell32.dll",
    "shlwapi.dll",
    "user32.dll",
    "uxtheme.dll",
    "version.dll",
    "winmm.dll",
    "wldap32.dll",
    "ws2_32.dll",
}

# The API set / MinWin contract stubs. These are resolved by the loader
# against whatever implements them on the running system rather than being
# files anyone ships, so match them by prefix instead of listing hundreds.
SYSTEM_PREFIXES: Tuple[str, ...] = ("api-ms-win-", "ext-ms-win-")

# Data directory indices from the PE spec.
DIR_IMPORT = 1
DIR_DELAY_IMPORT = 13


class NotAPeFile(Exception):
    pass


class _Image:
    """Just enough of a PE image to walk its import descriptors."""

    def __init__(self, data: bytes):
        if len(data) < 0x40 or data[:2] != b"MZ":
            raise NotAPeFile("not a PE image (no MZ signature)")
        (pe_offset,) = struct.unpack_from("<I", data, 0x3C)
        if data[pe_offset:pe_offset + 4] != b"PE\0\0":
            raise NotAPeFile(f"no PE signature at {pe_offset:#x}")

        coff = pe_offset + 4
        section_count, = struct.unpack_from("<H", data, coff + 2)
        optional_size, = struct.unpack_from("<H", data, coff + 16)
        optional = coff + 20

        magic, = struct.unpack_from("<H", data, optional)
        if magic == 0x20B:  # PE32+
            directories = optional + 112
        elif magic == 0x10B:  # PE32
            directories = optional + 96
        else:
            raise NotAPeFile(f"unknown optional header magic {magic:#06x}")

        self._data = data
        self._directories = directories
        # (virtual address, virtual size, raw offset, raw size) per section,
        # which is all an RVA -> file offset translation needs.
        self._sections = []
        table = optional + optional_size
        for index in range(section_count):
            entry = table + index * 40
            virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
                "<IIII", data, entry + 8
            )
            self._sections.append((virtual_address, virtual_size, raw_offset, raw_size))

    def directory(self, index: int) -> Tuple[int, int]:
        """The (RVA, size) of one data directory entry."""
        return struct.unpack_from("<II", self._data, self._directories + index * 8)

    def offset_of(self, rva: int) -> Optional[int]:
        for virtual_address, virtual_size, raw_offset, raw_size in self._sections:
            if virtual_address <= rva < virtual_address + max(virtual_size, raw_size):
                offset = raw_offset + (rva - virtual_address)
                if offset < len(self._data):
                    return offset
        return None

    def string_at(self, rva: int) -> Optional[str]:
        offset = self.offset_of(rva)
        if offset is None:
            return None
        end = self._data.find(b"\0", offset)
        if end < 0:
            return None
        return self._data[offset:end].decode("ascii", "replace")


def _walk_descriptors(image: _Image, directory: int, stride: int, name_field: int):
    """Yield the DLL name from each descriptor in one import directory.

    The two directories differ only in their record layout -- the ordinary
    import descriptor is 20 bytes with the name at offset 12, the delay-load
    one is 32 bytes with the name at offset 4 -- and both are terminated by an
    all-zero record.
    """
    rva, size = image.directory(directory)
    if not rva or not size:
        return
    offset = image.offset_of(rva)
    if offset is None:
        return
    while True:
        record = image._data[offset:offset + stride]
        if len(record) < stride or not any(record):
            return
        (name_rva,) = struct.unpack_from("<I", record, name_field)
        name = image.string_at(name_rva)
        if name:
            yield name
        offset += stride


def imported_dlls(path: str) -> Tuple[List[str], List[str]]:
    """Return (hard imports, delay-loaded imports) for a PE file.

    The two are kept apart because only the first is fatal. A delay-loaded
    dependency is resolved on first use, so a missing one costs the feature
    that needs it; a hard one costs the whole DLL, at load time, before any
    code of ours runs.
    """
    with open(path, "rb") as handle:
        image = _Image(handle.read())
    hard = list(_walk_descriptors(image, DIR_IMPORT, 20, 12))
    delayed = list(_walk_descriptors(image, DIR_DELAY_IMPORT, 32, 4))
    return hard, delayed


def is_system_dll(name: str) -> bool:
    lowered = name.lower()
    if lowered in SYSTEM_DLLS:
        return True
    return lowered.startswith(SYSTEM_PREFIXES)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        help="an additional DLL name to accept, for one that is shipped "
             "beside the file being checked (repeatable)",
    )
    parser.add_argument("files", nargs="+", help="PE files (.dll/.exe) to check")
    args = parser.parse_args(argv)

    allowed = {name.lower() for name in args.allow}
    problems = []

    for path in args.files:
        try:
            hard, delayed = imported_dlls(path)
        except (OSError, NotAPeFile, struct.error) as error:
            problems.append(f"{path}: {error}")
            continue

        unknown = [
            name for name in hard
            if not is_system_dll(name) and name.lower() not in allowed
        ]
        print(f"{path}: {len(hard)} imports, {len(delayed)} delay-loaded")
        for name in sorted(delayed, key=str.lower):
            if not is_system_dll(name) and name.lower() not in allowed:
                # Not an error: a delay-loaded dependency that is absent
                # costs the feature that uses it, not the load.
                print(f"  note: delay-loads {name}, which Windows may not have")
        for name in sorted(unknown, key=str.lower):
            problems.append(
                f"{path} hard-imports {name}, which is not a Windows system "
                f"DLL and is not shipped with it -- the file will not load on "
                f"a machine without it"
            )

    if problems:
        print("", file=sys.stderr)
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
