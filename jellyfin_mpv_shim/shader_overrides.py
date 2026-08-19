"""Per-library and per-series shader profile overrides.

There is no one right Anime4K profile -- there is a right one *per kind of
source* -- and a single global choice makes the user re-pick by hand or
accept the wrong one. Its own JSON file rather than a config key because
``settings_base`` has no ``dict`` type and, more importantly, because these
overrides are **device-local**: which profile runs well depends on this
machine's GPU. Keys carry the server uuid, since item ids are only unique per
server. Both decisions are recorded in ``docs/UI_FIXES_4.md`` §15.

**Absent and null mean different things.** A key that is not in the file
inherits from the next scope out; a key holding ``null`` is an override that
says "no shaders for this", which is a thing a user can want. Everything here
turns on keeping those apart, so the read API answers with a sentinel rather
than with ``None``.
"""

import json
import logging
import os

log = logging.getLogger("shader_overrides")

#: Narrowest first. This *is* the resolution order — series beats library
#: beats the global setting — so it is one list rather than an order
#: repeated in the resolver and in the menu.
SCOPES = ("series", "library")

#: What :meth:`ShaderOverrides.get` answers when a scope says nothing about
#: an item, as distinct from a scope that says "no profile".
UNSET = object()


def key_for(server_uuid, item_id):
    """The storage key for an item on a server, or None if either is
    missing. A key with an empty half would collide with every other
    key with an empty half, which is a cross-server override by accident."""
    if not server_uuid or not item_id:
        return None
    return "%s/%s" % (server_uuid, item_id)


class ShaderOverrides:
    """The override file, read once and written on change.

    Held by :class:`~.video_profile.VideoProfileManager`. Not a singleton:
    the tests want their own, and the path is injectable for the same
    reason.
    """

    FILENAME = "shader_profiles.json"

    def __init__(self, path=None):
        self.path = path
        #: scope -> {key: profile name or None}
        self._data = {scope: {} for scope in SCOPES}
        self.load()

    # -- storage ---------------------------------------------------------

    def load(self):
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            # A file somebody hand-edited into invalid JSON must not stop
            # playback. Empty overrides means the global profile, which is
            # the behaviour this feature is an addition to.
            log.warning("Could not read %s; ignoring shader overrides.",
                        self.path, exc_info=True)
            return
        if not isinstance(raw, dict):
            return
        for scope in SCOPES:
            table = raw.get(scope)
            if not isinstance(table, dict):
                continue
            self._data[scope] = {
                str(k): (v if isinstance(v, str) else None)
                for k, v in table.items()
            }

    def save(self):
        if not self.path:
            return
        payload = {"version": 1}
        payload.update({scope: self._data[scope] for scope in SCOPES})
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=4)
        except OSError:
            log.warning("Could not write %s.", self.path, exc_info=True)

    # -- reads -----------------------------------------------------------

    def get(self, scope, key):
        """This scope's answer for ``key``: a profile name, ``None`` for an
        explicit "no shaders", or :data:`UNSET` for "inherit"."""
        if key is None:
            return UNSET
        table = self._data.get(scope) or {}
        return table[key] if key in table else UNSET

    def has_any(self, scope):
        """Whether this scope holds *any* override.

        The reason this exists: resolving an item's library costs a request
        to the server, and asking it for a user who has never set a
        library override would be a request per playback for nothing.
        """
        return bool(self._data.get(scope))

    def resolve(self, keys, default):
        """``(scope, profile)`` for an item, narrowest scope that speaks.

        ``keys`` is ``{scope: key}`` — a scope missing from it, or mapped to
        None, is one that does not apply to this item (a film has no
        series). ``default`` is the global ``shader_pack_profile``, and the
        scope it is returned under is ``"default"``.
        """
        for scope in SCOPES:
            found = self.get(scope, (keys or {}).get(scope))
            if found is not UNSET:
                return scope, found
        return "default", default

    # -- writes ----------------------------------------------------------

    def set(self, scope, key, profile):
        """Override ``key`` at ``scope``. ``profile`` may be None, which is
        an override meaning "no shaders here" — see the module docstring."""
        if scope not in self._data or key is None:
            return False
        self._data[scope][key] = profile
        self.save()
        return True

    def clear(self, scope, key):
        """Drop ``key``'s override at ``scope``, so it inherits again."""
        if scope not in self._data or key is None:
            return False
        if key not in self._data[scope]:
            return False
        del self._data[scope][key]
        self.save()
        return True
