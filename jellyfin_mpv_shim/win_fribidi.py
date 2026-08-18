"""Load FriBiDi on Windows, so Pillow's Raqm layout engine is available.

**Pillow's binary wheels do not ship FriBiDi on any platform.** Its own
installation docs say so outright ("Raqm support requires FriBiDi to be
installed separately"), and the Windows wheel really does contain nothing but
eight ``.pyd`` files. libraqm and HarfBuzz *are* compiled into
``_imagingft``; FriBiDi alone is loaded at runtime, by a vendored shim
(``src/thirdparty/fribidi-shim/fribidi.c``) that tries::

    LoadLibrary("fribidi")  ->  "fribidi-0"  ->  "libfribidi-0"   /* MSYS2 */

and then sets ``have_raqm = !!p_fribidi``. Windows ships no such DLL, so on
a stock box ``have_raqm`` is 0 and ``ImageFont.truetype`` **silently**
downgrades to ``Layout.BASIC`` -- no exception, no warning (``ImageFont.py``
only warns when RAQM is asked for by name, which nothing here does).

Two things break, and the second is the larger:

* **Bidi and shaping are gone.** Arabic and Hebrew render in logical order
  with isolated letterforms -- visually reversed, and with the cursive
  joining absent. That is issue #689: the same string drawn as an ASS text
  node (libass, which has its own FriBiDi and HarfBuzz) is correct on the
  same page, while anything baked into a bitmap by Pillow -- tile captions,
  Cast & Crew, the hero heading over a backdrop -- is not.

* **HarfBuzz is only reachable THROUGH Raqm**, so a Windows build applies no
  GPOS shaping to any script, Latin included. Measured on DejaVuSans at
  20px: "AVATAR" comes out 81.94px unkerned against 75.20px kerned, 9% wide.
  That matters beyond looks, because ``mpvtk.metrics`` measures with Pillow
  to build the model ``mpvtk.layout.text_width`` uses to *predict* what
  libass will draw -- so on Windows every ellipsize, wrap and pack decision
  was computed against widths that are too wide, and captions truncated
  earlier than they needed to.

So the fix is one ~150KB DLL, built from source in CI and shipped beside the
app (see ``tools/check_win_raqm.py``, which fails the build if it goes
missing -- nothing downstream would notice, because English looks perfect
either way).

**Why an explicit load rather than just dropping the file in.** The shim
calls ``LoadLibrary`` with a *bare name*, so whether Pillow finds our copy
depends on the process's DLL search path -- which under PyInstaller is a
property of the bootloader and the onedir/onefile layout, not something this
app states. Loading it here by absolute path settles it: Windows resolves a
subsequent ``LoadLibrary("libfribidi-0")`` against modules already in the
process **by base name**, without searching, so Pillow's own call gets this
handle. Verified against a real Windows Pillow wheel with the DLL in a
directory deliberately left off the search path.

**Ordering is the whole contract.** ``load_fribidi()`` runs once, from
``PyInit__imagingft``, so this has to happen before the first
``import PIL.ImageFont`` anywhere in the process. Every Pillow import in
this package is function-local, which is what makes that achievable at all;
``preload()`` is called from ``mpv_shim.main`` and, belt-and-braces, from
the three modules that load faces (they call it immediately before their own
import, and it is idempotent).
"""

import logging
import os
import sys

log = logging.getLogger("win_fribidi")

#: Tried in order. Both spellings, because which one we ship depends on the
#: compiler: meson names it ``fribidi-0.dll`` under MSVC and
#: ``libfribidi-0.dll`` under mingw, and Pillow's shim accepts either.
_NAMES = ("fribidi-0.dll", "libfribidi-0.dll")

#: Set once, whatever the outcome. A second call must not re-walk the
#: filesystem: this sits in front of font loading, which is on the render
#: path.
_done = False

#: What happened, for the startup log and for tests. None until preload runs.
loaded_from = None


def _candidate_dirs():
    """Where a bundled DLL could be, most specific first.

    ``sys._MEIPASS`` is PyInstaller's unpack directory and is where
    ``--add-binary "...;."`` puts it -- under PyInstaller 6 that is the
    ``_internal`` folder, *not* the folder holding the .exe, which is
    exactly why this is not left to the search path. The executable's own
    directory covers a build that places it alongside, and the package
    directory covers running from a source checkout with the DLL dropped in
    beside ``mpv-2.dll``.
    """
    dirs = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(meipass)
    try:
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
    except Exception:
        pass
    dirs.append(os.path.dirname(os.path.abspath(__file__)))
    dirs.append(os.getcwd())
    # Preserve order, drop repeats -- the source checkout has several of
    # these pointing at the same place.
    return list(dict.fromkeys(d for d in dirs if d))


def preload():
    """Load a bundled FriBiDi if there is one. Returns the path, or None.

    Idempotent, and never raises: a missing DLL costs bidi, shaping and some
    metric fidelity, which is a degraded UI and not a reason to fail a
    launch. That is the same contract every other optional dependency here
    has (CONTRIBUTING.md).
    """
    global _done, loaded_from
    if _done:
        return loaded_from
    _done = True
    if os.name != "nt":
        # Every other platform links FriBiDi properly or finds it on the
        # system: the manylinux wheels dlopen `libfribidi.so.0`, which any
        # desktop has because pango needs it, and a distro Pillow is built
        # against system raqm outright.
        return None
    import ctypes

    for directory in _candidate_dirs():
        for name in _NAMES:
            path = os.path.join(directory, name)
            if not os.path.exists(path):
                continue
            try:
                ctypes.WinDLL(path)
            except OSError:
                # Wrong architecture is the one to expect here, and it is
                # worth a line: on Windows on Arm an x64 DLL is a plausible
                # mistake that fails at exactly this point.
                log.warning("could not load %s", path, exc_info=True)
                continue
            loaded_from = path
            return path
    log.info("no FriBiDi alongside the app; Pillow will draw right-to-left "
             "text unshaped and measure everything unkerned")
    return None


def raqm_available():
    """Whether Pillow will use the Raqm layout engine. None if unknown.

    Its own answer, asked of Pillow rather than inferred from whether
    :func:`preload` found something -- a system copy on the search path
    counts, and so does a distro build with raqm linked in.
    """
    try:
        from PIL import features

        return bool(features.check("raqm"))
    except Exception:
        return None


def describe():
    """One line for the startup log.

    Logged rather than left to be asked for, because the shipped Windows
    build is a PyInstaller bundle with no interpreter to query -- the answer
    to "does your Pillow have Raqm" has to already be in the log a reporter
    sends, or it costs a round trip to find out (#689).
    """
    raqm = raqm_available()
    if raqm is None:
        return "text shaping: Pillow unavailable"
    where = " (%s)" % loaded_from if loaded_from else ""
    return "text shaping: raqm=%s%s" % ("yes" if raqm else "no", where)
