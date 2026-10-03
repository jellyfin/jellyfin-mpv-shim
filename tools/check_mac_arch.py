#!/usr/bin/env python3
"""Assert that the running Python and the given Mach-O files are one architecture.

Mirrors tools/check_win_arch.py for macOS. On Apple Silicon runners, rosetta or
an x86_64 interpreter could build an x86_64 bundle without failing. This tool
verifies the interpreter and the Mach-O binary architectures.
"""

import argparse
import platform
import subprocess
import sys
from typing import List, Optional


def interpreter_machine() -> str:
    raw = platform.machine().lower()
    if raw in ("arm64", "aarch64"):
        return "arm64"
    if raw in ("x86_64", "amd64"):
        return "x64"
    return raw


def macho_archs(path: str) -> List[str]:
    """Return architectures reported by lipo for the given file."""
    try:
        out = subprocess.check_output(
            ["lipo", "-archs", path],
            stderr=subprocess.STDOUT,
            text=True,
        ).strip()
        return out.split()
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        raise RuntimeError(f"{path}: failed to read architecture: {error}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--expect",
        default="arm64",
        help="the architecture everything must be (default: arm64)",
    )
    parser.add_argument(
        "--skip-interpreter",
        action="store_true",
        help="only check the files, not the Python running this",
    )
    parser.add_argument("files", nargs="*", help="Mach-O files to check")
    args = parser.parse_args(argv)

    problems = []

    if not args.skip_interpreter:
        found = interpreter_machine()
        print(f"interpreter: {found} ({sys.version.split()[0]})")
        if found != args.expect.lower():
            problems.append(f"interpreter is {found}, expected {args.expect}")

    for path in args.files:
        try:
            archs = macho_archs(path)
        except Exception as error:
            problems.append(str(error))
            continue
        print(f"{path}: {' '.join(archs)}")
        if args.expect.lower() not in [a.lower() for a in archs]:
            problems.append(f"{path} architectures {archs} do not include {args.expect}")

    if problems:
        print("", file=sys.stderr)
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
