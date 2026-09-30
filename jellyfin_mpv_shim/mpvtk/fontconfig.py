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

# Only calls whose signatures have not changed since fontconfig 2.0, and none
# of the variadic ones (FcObjectSetBuild): a variadic call through ctypes is
# where the calling convention is easiest to get wrong. The major version is
# checked before anything else is called.
_CHILD = r"""
import ctypes, ctypes.util, json, sys

def open_lib():
    names = [n for n in (sys.argv[1], "libfontconfig.so.1") if n]
    found = ctypes.util.find_library("fontconfig")
    if found:
        names.append(found)
    for name in names:
        try:
            return ctypes.CDLL(name), name
        except OSError:
            pass
    sys.exit(2)

fc, name = open_lib()
P = ctypes.c_void_p
fc.FcGetVersion.restype = ctypes.c_int
fc.FcGetVersion.argtypes = []
version = fc.FcGetVersion()
if not 20000 <= version < 30000:
    print(json.dumps({"lib": name, "version": version}))
    sys.exit(3)

class FcFontSet(ctypes.Structure):
    _fields_ = [("nfont", ctypes.c_int), ("sfont", ctypes.c_int),
                ("fonts", ctypes.POINTER(P))]

fc.FcPatternCreate.restype = P
fc.FcPatternCreate.argtypes = []
fc.FcPatternDestroy.restype = None
fc.FcPatternDestroy.argtypes = [P]
fc.FcObjectSetCreate.restype = P
fc.FcObjectSetCreate.argtypes = []
fc.FcObjectSetAdd.restype = ctypes.c_int
fc.FcObjectSetAdd.argtypes = [P, ctypes.c_char_p]
fc.FcObjectSetDestroy.restype = None
fc.FcObjectSetDestroy.argtypes = [P]
fc.FcFontList.restype = ctypes.POINTER(FcFontSet)
fc.FcFontList.argtypes = [P, P, P]
fc.FcFontSetDestroy.restype = None
fc.FcFontSetDestroy.argtypes = [ctypes.POINTER(FcFontSet)]
fc.FcPatternGetString.restype = ctypes.c_int
fc.FcPatternGetString.argtypes = [P, ctypes.c_char_p, ctypes.c_int,
                                  ctypes.POINTER(ctypes.c_char_p)]
fc.FcNameParse.restype = P
fc.FcNameParse.argtypes = [ctypes.c_char_p]
fc.FcConfigSubstitute.restype = ctypes.c_int
fc.FcConfigSubstitute.argtypes = [P, P, ctypes.c_int]
fc.FcDefaultSubstitute.restype = None
fc.FcDefaultSubstitute.argtypes = [P]
fc.FcFontMatch.restype = P
fc.FcFontMatch.argtypes = [P, P, ctypes.POINTER(ctypes.c_int)]

def file_of(pattern):
    out = ctypes.c_char_p()
    if fc.FcPatternGetString(pattern, b"file", 0, ctypes.byref(out)) != 0:
        return None
    return out.value.decode("utf-8", "surrogateescape") if out.value else None

files = []
pattern, objects = fc.FcPatternCreate(), fc.FcObjectSetCreate()
if pattern and objects and fc.FcObjectSetAdd(objects, b"file"):
    listed = fc.FcFontList(None, pattern, objects)
    if listed:
        for i in range(listed.contents.nfont):
            path = file_of(listed.contents.fonts[i])
            if path:
                files.append(path)
        fc.FcFontSetDestroy(listed)
if objects:
    fc.FcObjectSetDestroy(objects)
if pattern:
    fc.FcPatternDestroy(pattern)

match = {}
for query in json.loads(sys.stdin.read()):
    pattern = fc.FcNameParse(query.encode())
    if not pattern:
        continue
    fc.FcConfigSubstitute(None, pattern, 0)      # FcMatchPattern
    fc.FcDefaultSubstitute(pattern)
    result = ctypes.c_int()
    found = fc.FcFontMatch(None, pattern, ctypes.byref(result))
    if found:
        match[query] = file_of(found)
        fc.FcPatternDestroy(found)
    fc.FcPatternDestroy(pattern)

print(json.dumps({"lib": name, "version": version, "files": files,
                  "match": match}))
"""

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
            [sys.executable, "-I", "-c", _CHILD, _mapped_library()],
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
