"""What kind of install this is, asked of the host rather than guessed.

Two questions, one file between them. **Is this a Flatpak that updates
itself**, which decides whether an update notice can say anything useful (a
Flathub install is updated by ``flatpak update`` or by the desktop's own
updater, not from our releases page). And **is the host SteamOS**, where the
gamepad is the only pointing device most users have.

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
