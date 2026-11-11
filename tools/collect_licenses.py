#!/usr/bin/env python3
"""Collect the licence files and a summary table for the Python packages in a site-packages directory.

    tools/collect_licenses.py SITE_PACKAGES OUT_DIR [--table FILE]

Used by the Linux .deb build so that what ships carries the licence text of what it bundles. Pillow's wheel
includes the licences of the native libraries it carries (FreeType, HarfBuzz, libpng, ...) in its own LICENSE
file, so copying package licence files copies those too. Exits non-zero if a package has no licence file or no
licence metadata: shipping something we cannot attribute is a build error, not a warning.
"""
import email
import glob
import os
import shutil
import sys

LICENCE_NAMES = ("LICEN", "COPYING", "NOTICE", "AUTHORS")


def packages(site):
    for dist in sorted(glob.glob(os.path.join(site, "*.dist-info"))):
        with open(os.path.join(dist, "METADATA"), encoding="utf-8") as fh:
            meta = email.message_from_file(fh)
        yield dist, meta


def licence_files(dist):
    found = []
    for base, _dirs, files in os.walk(dist):
        for name in files:
            if name.upper().startswith(LICENCE_NAMES) and not name.endswith(".pyc"):
                found.append(os.path.join(base, name))
    return sorted(found)


def declared(meta):
    for key in ("License-Expression", "License"):
        value = (meta.get(key) or "").strip()
        if value and "\n" not in value and len(value) < 120:
            return value
    for cls in meta.get_all("Classifier") or []:
        if cls.startswith("License ::"):
            return cls.split("::")[-1].strip()
    return ""


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 64
    site, out = argv[1], argv[2]
    table = argv[argv.index("--table") + 1] if "--table" in argv else None
    os.makedirs(out, exist_ok=True)
    rows, problems = [], []
    for dist, meta in packages(site):
        name, version = meta["Name"], meta["Version"]
        files = licence_files(dist)
        dest = os.path.join(out, name)
        os.makedirs(dest, exist_ok=True)
        for f in files:
            shutil.copyfile(f, os.path.join(dest, os.path.basename(f)))
        lic = declared(meta)
        if not files:
            problems.append("%s %s: no licence file in its dist-info" % (name, version))
        if not lic:
            problems.append("%s %s: no licence metadata" % (name, version))
        rows.append((name, version, lic or "?", ", ".join(os.path.basename(f) for f in files) or "NONE"))
    if table:
        with open(table, "w", encoding="utf-8") as fh:
            fh.write("| Package | Version | Declared licence | Licence files shipped |\n|---|---|---|---|\n")
            for r in rows:
                fh.write("| %s | %s | %s | %s |\n" % r)
    for r in rows:
        print("%-28s %-12s %-40s %s" % r)
    for p in problems:
        print("PROBLEM:", p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
