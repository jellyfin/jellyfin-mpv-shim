"""What kind of install this is, asked of the host rather than guessed.

Two questions, one file between them. **Does something else deliver this
install's updates** -- a Flatpak remote or the Microsoft Store -- which
decides whether an update notice can say anything useful (a Flathub install
is updated by ``flatpak update`` or by the desktop's own updater, a Store
install by the Store, neither from our releases page). And **is the host
SteamOS**, where the gamepad is the only pointing device most users have.

**Inside a Flatpak, ``/etc/os-release`` describes the runtime and not the
machine.** The host's identity is only at ``/run/host/os-release``, which is
the file jellyfin-media-player reads for the same question
(``src/system/SystemComponent.cpp``). That makes it one probe asked twice.

**There is no origin or remote in ``/.flatpak-info``.** A real one read out of
the installed 3.0.0 sandbox carries ``name``, ``runtime``, ``app-path``,
``app-commit``, ``branch`` and ``[Context]`` and nothing that says where the
app came from, so "installed from Flathub" cannot be read off it. Flathub
builds happen to set ``branch=stable`` while a locally built bundle gets
flatpak-builder's ``master``, and that is deliberately **not** used here: it
is undeclared, and it breaks the first time anyone builds with
``--default-branch``. The marker below is what distinguishes the two, and it
is set by us.
"""

import logging
import os
import sys

log = logging.getLogger("hostinfo")

#: Present inside every Flatpak sandbox.
FLATPAK_MARKER = "/.flatpak-info"

#: The host's os-release, from inside a sandbox. See the module docstring.
HOST_OS_RELEASE = "/run/host/os-release"

#: This repo's own CI and dev manifests set it; Flathub's does not. So it
#: marks a build that was **handed to somebody** -- a CI artifact or a local
#: bundle -- which is exactly the build that cannot update itself and whose
#: user was given it deliberately.
UPDATE_MARKER_ENV = "JMS_UPDATE_CHECK"

#: What GetCurrentPackageFamilyName answers a packaged process asked with a
#: zero-length buffer. An unpackaged one gets APPMODEL_ERROR_NO_PACKAGE (15700).
_ERROR_INSUFFICIENT_BUFFER = 122


def is_flatpak():
    return os.path.exists(FLATPAK_MARKER)


def flatpak_managed():
    """Whether this install gets its updates from a Flatpak remote.

    The one question the notifier and its message both turn on, asked in one
    place so they cannot answer it differently: a marked build is one we
    handed over ourselves (§5.4 of docs/RELEASE_SHAPE_POST_3.0.0.md), it has
    no remote to update from, and telling its user to run ``flatpak update``
    would be wrong.
    """
    return is_flatpak() and not os.environ.get(UPDATE_MARKER_ENV)


def is_msix():
    """Whether this process runs with an MSIX package identity.

    In practice that is a Microsoft Store install: an MSIX cannot be
    installed without a signature Windows trusts, and the Store is what signs
    ours. Asked of Windows rather than read off the install path, which
    package volumes can move.
    """
    return package_family_name() is not None


def package_family_name():
    """This process's MSIX package family name, or None when unpackaged."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        get = ctypes.windll.kernel32.GetCurrentPackageFamilyName
        length = ctypes.c_uint32(0)
        if get(ctypes.byref(length), None) != _ERROR_INSUFFICIENT_BUFFER:
            return None
        buf = ctypes.create_unicode_buffer(length.value)
        if get(ctypes.byref(length), buf) != 0:
            return None
        return buf.value
    except (AttributeError, OSError):
        # Windows 7 has no such function, and nothing there is packaged.
        return None


def outside_path(path):
    """Where ``path`` is for a process outside this package.

    A packaged app's *new* files under ``%APPDATA%`` and ``%LOCALAPPDATA%``
    are redirected to the package's ``LocalCache``, and only the app itself
    sees them at the path it asked for; Explorer, or a reader handed a
    downloaded book, looks at the real path and finds nothing. Files that
    existed before the install are used in place and not redirected
    (docs/packaging.md §6), so the redirected copy is used only if it exists.
    Every path handed to another program, or shown to the user, goes through
    here.
    """
    family = package_family_name()
    local = os.environ.get("LOCALAPPDATA")
    if not family or not local or not path:
        return path
    for env, sub in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local")):
        base = os.environ.get(env)
        if not base:
            continue
        try:
            rel = os.path.relpath(path, base)
        except ValueError:  # another drive
            continue
        if rel == os.curdir or rel.startswith(os.pardir):
            continue
        redirected = os.path.join(local, "Packages", family, "LocalCache",
                                  sub, rel)
        return redirected if os.path.exists(redirected) else path
    return path


def updates_managed():
    """Whether something other than this app delivers its updates.

    What decides the update notice's default. The wording is a separate
    question -- only a Flatpak has a command to tell the user to run -- so
    callers choosing words ask `flatpak_managed` instead.
    """
    return flatpak_managed() or is_msix()


def os_release():
    """The host's os-release text, or "" when it cannot be read.

    Empty rather than raising, and every caller treats empty as "not that
    platform": this answers a question about *defaults*, and a machine that
    will not say what it is gets the ordinary ones.
    """
    path = HOST_OS_RELEASE if is_flatpak() else "/etc/os-release"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def is_steamos():
    """Whether the machine this runs on is SteamOS.

    Matched on the ``NAME=`` line rather than anywhere in the file, because
    ``HOME_URL`` and ``BUG_REPORT_URL`` on a Steam Deck both contain
    "steamos" -- and the same strings appear on a machine that merely
    documents it. ``ID=steamos`` is accepted too: it is the field
    os-release defines for exactly this test, and quoting of ``NAME`` is not
    guaranteed.
    """
    for line in os_release().splitlines():
        key, _sep, value = line.partition("=")
        if key.strip() not in ("NAME", "ID"):
            continue
        if value.strip().strip('"\'').lower() == "steamos":
            return True
    return False
