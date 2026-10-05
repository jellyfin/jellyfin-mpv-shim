"""fontconfig.py's child: one fontconfig query through ctypes, as JSON.

Run as a script by ``fontconfig._from_child`` (``python -I <this file> <lib>``),
never imported -- so PyInstaller leaves it out of a frozen build, which
skips the child anyway. Standard library only: ``-I`` hides the package.
"""
# Only calls whose signatures have not changed since fontconfig 2.0, and none
# of the variadic ones (FcObjectSetBuild): a variadic call through ctypes is
# where the calling convention is easiest to get wrong. The major version is
# checked before anything else is called.
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
