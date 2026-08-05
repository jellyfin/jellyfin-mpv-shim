#!/usr/bin/env python3
"""Assert that the running Python and the given PE files are one architecture.

The ARM64 Windows build has no failure mode that looks like a failure. Windows
on Arm runs x64 binaries under emulation, so if ``setup-python`` were to fall
back to an x64 interpreter -- which it did on that runner before native ARM64
Python reached the versions manifest -- every later step still succeeds:
PyInstaller happily builds an x64 executable, ISCC happily wraps it, and the
job uploads an installer named ARM64 that is not. Only a user on an Arm laptop
finds out, by getting the emulated build.

libmpv is the other half of the same problem, from the opposite direction: the
shim loads it with ctypes at runtime, so an mpv-2.dll of the wrong
architecture is not a build error, it is a client that starts and then cannot
play anything.

So the CI job runs this between fetching its inputs and building, and again on
the built executable.
"""

import argparse
import platform
import struct
import sys
from typing import Dict, Optional

# IMAGE_FILE_MACHINE_* values from the PE spec, for the targets this project
# actually ships. Anything else is reported by its raw value.
MACHINES: Dict[int, str] = {
    0x014C: "x86",
    0x8664: "x64",
    0xAA64: "ARM64",
    0x01C4: "ARM",
}

# platform.machine() spellings, normalized to the names above.
PLATFORM_NAMES = {
    "AMD64": "x64",
    "x86_64": "x64",
    "x86": "x86",
    "i386": "x86",
    "i686": "x86",
    "ARM64": "ARM64",
    "aarch64": "ARM64",
}


class NotAPeFile(Exception):
    pass


def pe_machine(path: str) -> str:
    """Read the COFF machine type out of a PE file (.exe or .dll)."""
    with open(path, "rb") as handle:
        header = handle.read(0x40)
        if len(header) < 0x40 or header[:2] != b"MZ":
            raise NotAPeFile(f"{path}: not a PE image (no MZ signature)")
        # e_lfanew, the offset of the PE header, lives at 0x3C of the DOS stub.
        (pe_offset,) = struct.unpack_from("<I", header, 0x3C)
        handle.seek(pe_offset)
        signature = handle.read(4)
        if signature != b"PE\0\0":
            raise NotAPeFile(f"{path}: no PE signature at {pe_offset:#x}")
        (machine,) = struct.unpack("<H", handle.read(2))
    return MACHINES.get(machine, f"unknown ({machine:#06x})")


def interpreter_machine() -> str:
    raw = platform.machine()
    return PLATFORM_NAMES.get(raw, raw)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--expect",
        default="ARM64",
        help="the architecture everything must be (default: ARM64)",
    )
    parser.add_argument(
        "--skip-interpreter",
        action="store_true",
        help="only check the files, not the Python running this",
    )
    parser.add_argument("files", nargs="*", help="PE files (.exe/.dll) to check")
    args = parser.parse_args(argv)

    problems = []

    if not args.skip_interpreter:
        found = interpreter_machine()
        print(f"interpreter: {found} ({sys.version.split()[0]})")
        if found != args.expect:
            problems.append(f"interpreter is {found}, expected {args.expect}")

    for path in args.files:
        try:
            found = pe_machine(path)
        except (OSError, NotAPeFile) as error:
            problems.append(str(error))
            continue
        print(f"{path}: {found}")
        if found != args.expect:
            problems.append(f"{path} is {found}, expected {args.expect}")

    if problems:
        print("", file=sys.stderr)
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
