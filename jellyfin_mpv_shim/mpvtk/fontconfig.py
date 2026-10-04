"""Ask fontconfig where the fonts are, for Pillow, which cannot.

Pillow searches ``$XDG_DATA_DIRS/*/fonts`` and nothing else. That is where
fonts live on most distributions and not on NixOS, where they are in
``/nix/store`` and only fontconfig's own configuration lists them (#786), and
not inside a Flatpak either (see pilfont's ``_HOST_FONT_DIRS``). fontconfig
is the one thing on a Linux desktop that always knows, and libass is already
asking it for the half of the UI mpv draws.

**The query runs in a child process**, so no libfontconfig -- an unexpected
build, a future ABI, a signature this file has wrong -- can crash the app:
ctypes has no safety net, and a segfault in the child is a failed lookup here.
The child gets the path of the libfontconfig this process already has mapped
(libmpv links it through libass), which is what finds it on NixOS, where
there is no linker cache to find it by name. If the child fails, ``fc-list``
is tried for the file list alone; if that fails too, the answer is empty and
every caller keeps its hard-coded candidates.

Used for *finding* files, not for *choosing* them: fontconfig's pick for
``sans-serif:lang=ja`` on a Debian box is a Chinese face (the Han unification
problem pilfont's candidate comments describe), so its matches go after the
curated candidates and still have to pass pilfont's coverage check.
"""

import json
import logging
import os
import subprocess
import sys
import time

log = logging.getLogger("mpvtk.fontconfig")

#: Longer than a warm query takes by two orders of magnitude. A cold cache
#: can take seconds to build, and waiting that out once is better than
#: drawing boxes for the rest of the session -- but not unboundedly.
TIMEOUT = 10.0

FONT_EXTENSIONS = (".ttf", ".otf", ".ttc", ".otc")

#: The ctypes query, as a script of its own (see its docstring). A crash
#: there is a failed lookup here, which is why it is a process at all.
_CHILD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "_fontconfig_child.py")

_result = None


def _mapped_library():
    """The libfontconfig this process already has loaded, or ''."""
    try:
        with open("/proc/self/maps") as maps:
            for line in maps:
                path = line.rstrip("\n").split(None, 5)[-1]
                if os.path.basename(path).startswith("libfontconfig.so"):
                    return path
    except OSError:
        pass
    return ""


def _from_child(queries):
    if getattr(sys, "frozen", False):
        return None             # sys.executable is the app, not a python
    try:
        done = subprocess.run(
            [sys.executable, "-I", _CHILD_PATH, _mapped_library()],
            input=json.dumps(list(queries)), capture_output=True,
            text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        log.info("fontconfig query did not run: %s", e)
        return None
    if done.returncode != 0:
        log.info("fontconfig query failed (exit %s): %s", done.returncode,
                 (done.stdout + done.stderr).strip()[-300:])
        return None
    try:
        answer = json.loads(done.stdout)
        return {"files": [str(f) for f in answer["files"]],
                "match": {str(k): str(v) for k, v in answer["match"].items()
                          if v},
                "via": "%s %s" % (answer.get("lib"), answer.get("version"))}
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        log.info("fontconfig query answered nonsense: %s", e)
        return None


def _from_fc_list():
    try:
        done = subprocess.run(["fc-list", "--format", "%{file}\n"],
                              capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return {"files": [f for f in done.stdout.splitlines() if f],
            "match": {}, "via": "fc-list"}


def lookup(queries=()):
    """``{"files": {basename.lower(): path}, "match": {query: path}}``.

    Runs at most once per process, with every query it will ever be asked,
    because the cost is the process spawn and not the queries. Empty on a
    host with no fontconfig (Windows, macOS) and on any failure.
    """
    global _result
    if _result is not None:
        return _result
    _result = {"files": {}, "match": {}}
    if sys.platform in ("win32", "darwin"):
        return _result
    start = time.monotonic()
    answer = _from_child(queries) or _from_fc_list()
    if answer is None:
        log.info("fontconfig is not available; using built-in font paths only")
        return _result
    files = {}
    for path in answer["files"]:
        if path.lower().endswith(FONT_EXTENSIONS):
            # First wins, matching fontconfig's own order.
            files.setdefault(os.path.basename(path).lower(), path)
    _result = {"files": files, "match": answer["match"]}
    log.info("fontconfig (%s): %d font files, %d matches in %.0f ms",
             answer["via"], len(files), len(answer["match"]),
             (time.monotonic() - start) * 1000)
    return _result


def reset():
    """Forget the answer, for tests."""
    global _result
    _result = None
