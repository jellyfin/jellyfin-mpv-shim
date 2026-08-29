#!/usr/bin/env python3
"""Verify that the pinned sources in a Flatpak manifest still resolve.

flatpak-builder refuses to build when a pinned sha256 no longer matches what
the host serves, and it finds that out one source at a time, minutes into a
cold build -- the failure that surfaced mujs. This checks every pin up front.

Three kinds are checked:

  url + sha256    Downloaded and hashed. A mismatch is usually a forge
                  regenerating an archive (Codeberg does: the tar stream
                  inside stays byte-identical, the gzip wrapper does not),
                  but that is indistinguishable from tampering from here.
                  Compare the contents before repinning -- and prefer
                  repinning to a git commit, which no recompression can move.

  tag + commit    git ls-remote, to confirm the tag still names the pinned
                  commit. A moved tag does not break the build, since
                  flatpak-builder fetches the commit, but it is the shape a
                  retagged upstream would take, so it is worth seeing.

  branch + commit git ls-remote against the branch head. This one DOES break
                  the build: flatpak-builder verifies that a named branch is
                  at the pinned commit and refuses otherwise, so pinning a
                  commit out of the history of a branch that is still moving
                  fails as soon as upstream pushes. Drop the branch key and
                  keep the commit -- flatpak-builder then fetches the whole
                  repo and checks that commit out directly.

Usage:
    tools/check_flatpak_pins.py [manifest.json ...]

With no arguments it checks flatpak/'s manifest, which pins mpv and its
dependencies. Point it at the Flathub packaging repo's manifest to check that
one -- it pins every wheel too, via pypi-dependencies.json:

    tools/check_flatpak_pins.py ~/src/flathub-jellyfin-mpv-shim/com.github.iwalton3.jellyfin-mpv-shim.json

Referenced module files (shared-modules/..., pypi-*.json) are followed
relative to the manifest that names them. Exits nonzero if any pin is stale.
"""

import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# Codeberg answers Python-urllib with a challenge page, which would hash as a
# mismatch and send you hunting for a source change that never happened.
USER_AGENT = "check-flatpak-pins/1 (+https://github.com/jellyfin/jellyfin-mpv-shim)"
WORKERS = 8

DEFAULT_MANIFEST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "flatpak",
    "com.github.iwalton3.jellyfin-mpv-shim.json",
)


def shorten(path):
    """Path relative to the cwd, unless that is the longer way to say it."""
    relative = os.path.relpath(path)
    return path if relative.startswith("..") else relative


def collect(path, hashed, tagged, branched, visited):
    """Walk a manifest, gathering its pins and those of the modules it names."""
    path = os.path.abspath(path)
    if path in visited:
        return
    visited.add(path)
    base = os.path.dirname(path)
    try:
        with open(path) as handle:
            doc = json.load(handle)
    except (OSError, ValueError) as error:
        print(f"skip  {path}: {error}", file=sys.stderr)
        return

    def walk(node):
        if isinstance(node, dict):
            if "url" in node and "sha256" in node:
                hashed.append((path, node["url"], node["sha256"]))
            elif node.get("type") == "git" and "commit" in node:
                if "tag" in node:
                    tagged.append((path, node["url"], node["tag"], node["commit"]))
                elif "branch" in node:
                    branched.append((path, node["url"], node["branch"], node["commit"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                # A module may be given as a path to another manifest file.
                if isinstance(value, str) and value.endswith(".json"):
                    collect(os.path.join(base, value), hashed, tagged,
                            branched, visited)
                else:
                    walk(value)

    walk(doc)


def check_hash(url, want):
    digest = hashlib.sha256()
    size = 0
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            while chunk := response.read(1 << 16):
                digest.update(chunk)
                size += len(chunk)
    except (urllib.error.URLError, OSError) as error:
        return None, f"{error}"
    got = digest.hexdigest()
    return got == want, f"got {got} ({size} bytes)"


def check_tag(url, tag, want):
    try:
        result = subprocess.run(
            ["git", "ls-remote", url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
            capture_output=True, text=True, timeout=120, check=True,
        )
    except (subprocess.SubprocessError, OSError) as error:
        return None, f"{error}"
    refs = dict(
        (line.split("\t")[1], line.split("\t")[0])
        for line in result.stdout.strip().splitlines() if "\t" in line
    )
    # An annotated tag resolves through its peeled ref; a lightweight one
    # names the commit directly.
    got = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")
    if got is None:
        return False, f"tag {tag} is gone"
    return got == want, f"tag {tag} names {got}"


def check_branch(url, branch, want):
    try:
        result = subprocess.run(
            ["git", "ls-remote", url, f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=120, check=True,
        )
    except (subprocess.SubprocessError, OSError) as error:
        return None, f"{error}"
    lines = result.stdout.strip().splitlines()
    if not lines:
        return False, f"branch {branch} is gone"
    got = lines[0].split("\t")[0]
    return got == want, f"branch {branch} is at {got}"


def main(argv):
    manifests = argv[1:] or [DEFAULT_MANIFEST]
    hashed, tagged, branched, visited = [], [], [], set()
    for manifest in manifests:
        collect(manifest, hashed, tagged, branched, visited)

    print(f"{len(hashed)} hashed sources, {len(tagged)} tagged git sources, "
          f"{len(branched)} branch-pinned git sources")
    stale = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        hash_results = pool.map(lambda item: check_hash(item[1], item[2]), hashed)
        tag_results = pool.map(lambda item: check_tag(item[1], item[2], item[3]), tagged)
        branch_results = pool.map(
            lambda item: check_branch(item[1], item[2], item[3]), branched
        )

        for (origin, url, want), (ok, detail) in zip(hashed, hash_results):
            if ok:
                continue
            stale += 1
            label = "ERROR" if ok is None else "STALE"
            print(f"{label} {url}\n      in   {shorten(origin)}"
                  f"\n      want {want}\n      {detail}")

        for (origin, url, tag, want), (ok, detail) in zip(tagged, tag_results):
            if ok:
                continue
            stale += 1
            label = "ERROR" if ok is None else "MOVED"
            print(f"{label} {url}\n      in   {shorten(origin)}"
                  f"\n      want {want}\n      {detail}")

        # Unlike a moved tag, this one fails the build outright -- and a
        # branch that still matches only means upstream has not pushed yet,
        # so it is worth saying either way.
        for (origin, url, branch, want), (ok, detail) in zip(branched, branch_results):
            if ok:
                print(f"FRAGILE {url}\n      in   {shorten(origin)}"
                      f"\n      {detail}, which matches for now, but it will move"
                      f"\n      drop the branch key and keep the commit")
                continue
            stale += 1
            label = "ERROR" if ok is None else "BROKEN"
            print(f"{label} {url}\n      in   {shorten(origin)}"
                  f"\n      want {want}\n      {detail}"
                  f"\n      flatpak-builder verifies this and refuses to build;"
                  f" drop the branch key and keep the commit")

    print("all pins current" if not stale else f"{stale} pin(s) need attention")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
