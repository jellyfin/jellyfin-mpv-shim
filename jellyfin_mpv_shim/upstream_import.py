"""Explicit, one-shot import of *settings* from an upstream Jellyfin MPV Shim.

JellyHome Player keeps its own configuration directory so it can sit beside
the upstream app. Nothing is imported implicitly. Running
``jellyhome-player import-upstream`` copies, from the upstream directory:

* ``conf.json`` -- preferences only. Never copied: ``client_uuid`` (this
  install's device identity) and anything privacy-related is reset to the
  fork's defaults (automatic update checks and Discord Rich Presence are
  switched off).
* ``mpv.conf`` and ``input.conf`` -- your MPV options and key bindings.

**Not** copied, on purpose: ``users.json`` / ``cred.json`` (saved servers and
access tokens -- they are tied to the upstream app's device identity, so you
sign in again, which takes a Quick Connect code), downloads, caches, logs.

It never overwrites a file that already exists in the JellyHome directory.
"""

import json
import logging
import os
import shutil

from . import conffile
from .constants import APP_NAME, UPSTREAM_APP_NAME

log = logging.getLogger("upstream_import")

#: conf.json keys that are dropped from the import.
DROP_KEYS = ("client_uuid",)
#: conf.json keys forced to the fork's privacy-preserving value.
FORCE_KEYS = {"check_updates": False, "discord_presence": False}
COPY_VERBATIM = ("mpv.conf", "input.conf")


def upstream_dir():
    return conffile.confdir(UPSTREAM_APP_NAME)


def run(src=None, dst=None):
    """Perform the import. Returns a list of human-readable result lines."""
    src = src or upstream_dir()
    dst = dst or conffile.confdir(APP_NAME)
    out = []
    if not src or not os.path.isdir(src):
        return ["No upstream Jellyfin MPV Shim settings found at %s." % src]
    if os.path.abspath(src) == os.path.abspath(dst):
        return ["Source and destination are the same directory; nothing to do."]
    os.makedirs(dst, exist_ok=True)

    conf_src = os.path.join(src, "conf.json")
    conf_dst = os.path.join(dst, "conf.json")
    if os.path.exists(conf_src):
        if os.path.exists(conf_dst):
            out.append("Kept existing conf.json (not overwritten).")
        else:
            try:
                with open(conf_src, encoding="utf-8-sig") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as exc:
                out.append("Could not read upstream conf.json (%s); skipped."
                           % exc)
            else:
                for key in DROP_KEYS:
                    data.pop(key, None)
                data.update(FORCE_KEYS)
                with open(conf_dst, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=4, sort_keys=True)
                _private(conf_dst)
                out.append("Imported settings from conf.json (device "
                           "identity dropped; update checks and Discord "
                           "presence left off).")
    for name in COPY_VERBATIM:
        a, b = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isfile(a) and not os.path.exists(b):
            shutil.copyfile(a, b)
            out.append("Copied %s." % name)
        elif os.path.exists(b) and os.path.isfile(a):
            out.append("Kept existing %s (not overwritten)." % name)
    out.append("Saved servers and logins were NOT copied; sign in again "
               "from the app (Quick Connect works).")
    return out


def _private(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
