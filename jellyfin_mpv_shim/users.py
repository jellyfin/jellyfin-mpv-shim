"""Local multi-user support ("fast user switching").

Jellyfin (and jellyfin-web) has no concept of switching between local accounts
on one device. Because this client owns its own UI we can offer it: a *user*
here is a local grouping of one or more server logins that are connected
together. Only one user is active at a time; switching disconnects the active
user's servers and connects the target user's.

Each user carries its own Jellyfin **device id** so that two users logged into
the same physical server don't collide on one server-side session (which would
make them fight over playback/remote-control state). The migrated "(default)"
user keeps the original ``settings.client_uuid`` so its existing sessions and
saved tokens keep working untouched; every other user gets a fresh device id.

A user may be PIN-protected (a parental-control affordance, *not* a security
boundary — the PIN is only salted-hashed): switching *into* a locked user
requires the PIN, and optionally the PIN can also be demanded at startup.

Persistence lives in ``users.json`` next to ``cred.json``. On first run with
this feature the existing ``cred.json`` is migrated into a "(default)" user.
"""

import hashlib
import hmac
import json
import logging
import os
import os.path
import threading
import uuid

from . import conffile
from .conf import settings
from .constants import APP_NAME
from .i18n import _

log = logging.getLogger("users")

USERS_FILE = "users.json"
DEFAULT_USER_NAME = _("(default)")

# PBKDF2 parameters. This gates a parental-control PIN, not a real secret, so
# the cost is kept modest; it still beats storing the PIN in the clear.
_PBKDF2_ROUNDS = 200000


def _hash_pin(pin: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex), _PBKDF2_ROUNDS
    ).hex()


def _new_id() -> str:
    return str(uuid.uuid4())


class UserManager:
    def __init__(self):
        # Guards the users list / active id against concurrent switches and the
        # gui_mgr action loop. Never held across network I/O.
        self._lock = threading.RLock()
        self.users = []  # list of user dicts (see _new_user for the shape)
        self.active_id = None
        self._loaded = False

    # -- construction / persistence ---------------------------------------

    @staticmethod
    def _new_user(name, device_id=None, credentials=None, is_default=False):
        return {
            "id": _new_id(),
            "name": name,
            # Non-default users MUST have a device id distinct from the config's
            # client_uuid so their server sessions don't conflict.
            "device_id": device_id or _new_id(),
            "default": is_default,
            "pin_hash": None,
            "pin_salt": None,
            "require_pin_startup": False,
            "credentials": list(credentials or []),
        }

    def _path(self):
        return conffile.get(APP_NAME, USERS_FILE)

    @staticmethod
    def _migrate_legacy_credentials(raw):
        """Bring a raw cred.json payload into the flat list shape clients.py
        expects (mirrors the old ClientManager.load_credentials migration)."""
        if isinstance(raw, dict) and "Servers" in raw:
            migrated = []
            for server in raw["Servers"]:
                server["uuid"] = _new_id()
                server["username"] = ""
                migrated.append(server)
            return migrated
        if isinstance(raw, list):
            return raw
        return []

    def _load_legacy_cred_json(self):
        location = conffile.get(APP_NAME, "cred.json")
        if not os.path.exists(location):
            return []
        try:
            with open(location) as cf:
                return self._migrate_legacy_credentials(json.load(cf))
        except Exception:
            log.warning("Could not read cred.json for migration.", exc_info=True)
            return []

    def load(self):
        """Load users.json, or migrate a pre-existing cred.json into a single
        "(default)" user on first run. Idempotent."""
        with self._lock:
            if self._loaded:
                return
            path = self._path()
            data = None
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        data = json.load(f)
                except Exception:
                    log.warning("Could not read users.json; starting fresh.",
                                exc_info=True)
            if isinstance(data, dict) and data.get("users"):
                self.users = [self._normalize(u) for u in data["users"]]
                self.active_id = data.get("active")
            else:
                # First run with the feature (or a corrupt file): fold the
                # existing single-user credentials into a "(default)" user that
                # keeps the original device id so nothing has to re-authenticate.
                default = self._new_user(
                    DEFAULT_USER_NAME,
                    device_id=settings.client_uuid,
                    credentials=self._load_legacy_cred_json(),
                    is_default=True,
                )
                self.users = [default]
                self.active_id = default["id"]
                self._loaded = True
                self.save()
                return
            # Repair a dangling active pointer.
            if not any(u["id"] == self.active_id for u in self.users):
                self.active_id = self.users[0]["id"] if self.users else None
            self._loaded = True

    @staticmethod
    def _normalize(u):
        """Fill in any keys missing from an older users.json entry."""
        return {
            "id": u.get("id") or _new_id(),
            "name": u.get("name") or DEFAULT_USER_NAME,
            "device_id": u.get("device_id") or _new_id(),
            "default": bool(u.get("default")),
            "pin_hash": u.get("pin_hash"),
            "pin_salt": u.get("pin_salt"),
            "require_pin_startup": bool(u.get("require_pin_startup")),
            "credentials": list(u.get("credentials") or []),
        }

    def save(self):
        with self._lock:
            payload = {"active": self.active_id, "users": self.users}
            try:
                with open(self._path(), "w") as f:
                    json.dump(payload, f, indent=4)
            except Exception:
                log.error("Failed to save users.json", exc_info=True)

    # -- lookups -----------------------------------------------------------

    def get(self, user_id):
        for u in self.users:
            if u["id"] == user_id:
                return u
        return None

    @property
    def active_user(self):
        return self.get(self.active_id)

    @property
    def active_device_id(self):
        u = self.active_user
        return u["device_id"] if u else settings.client_uuid

    def device_name_for(self, user):
        """Device name reported to Jellyfin. The default user keeps the plain
        player_name; every other user appends its name so the sessions are
        distinguishable in the Jellyfin dashboard."""
        base = settings.player_name
        if user is None or user.get("default"):
            return base
        return "{0} ({1})".format(base, user["name"])

    @property
    def active_device_name(self):
        return self.device_name_for(self.active_user)

    def is_locked(self, user_id):
        u = self.get(user_id)
        return bool(u and u.get("pin_hash"))

    def startup_needs_unlock(self):
        """True when the active user is locked AND opted into a startup PIN."""
        u = self.active_user
        return bool(u and u.get("pin_hash") and u.get("require_pin_startup"))

    def public_users(self):
        """User list safe to hand to the UI process — no hashes/credentials."""
        return [
            {"id": u["id"], "name": u["name"], "locked": bool(u.get("pin_hash")),
             "default": bool(u.get("default"))}
            for u in self.users
        ]

    def known_servers(self):
        """Distinct server addresses already used by any user, so a new/other
        user can be provisioned without retyping the URL. Addresses only (no
        tokens); the URL alone grants nothing without credentials."""
        seen = {}
        for u in self.users:
            for c in u.get("credentials", []):
                addr = (c.get("address") or "").rstrip("/")
                if addr and addr not in seen:
                    seen[addr] = {"address": addr,
                                  "name": c.get("Name") or addr}
        return list(seen.values())

    def public_state(self):
        return {
            "active": self.active_id,
            "users": self.public_users(),
            "startup_locked": self.startup_needs_unlock(),
            "known_servers": self.known_servers(),
        }

    # -- credential syncing (called by ClientManager) ----------------------

    def credentials_for_active(self):
        """A copy of the active user's saved credentials for ClientManager to
        connect. Copied so ClientManager's live mutations don't touch the store
        until it explicitly saves back."""
        with self._lock:
            u = self.active_user
            if u is None:
                return []
            return [dict(c) for c in u["credentials"]]

    def set_active_credentials(self, credentials):
        """Replace the active user's stored credentials (already cleaned of
        volatile runtime keys by the caller) and persist."""
        with self._lock:
            u = self.active_user
            if u is None:
                return
            u["credentials"] = [dict(c) for c in credentials]
        self.save()

    # -- mutations ---------------------------------------------------------

    def add_user(self, name):
        name = (name or "").strip() or _("New User")
        with self._lock:
            user = self._new_user(name)
            self.users.append(user)
        self.save()
        return user

    def rename_user(self, user_id, name):
        name = (name or "").strip()
        if not name:
            return False
        with self._lock:
            u = self.get(user_id)
            if u is None:
                return False
            u["name"] = name
        self.save()
        return True

    def delete_user(self, user_id):
        """Remove a user and its saved logins. The active user can't be deleted
        (switch away first) and the last remaining user can't be deleted."""
        with self._lock:
            if user_id == self.active_id:
                return False, _("Switch to another user before deleting this one.")
            if len(self.users) <= 1:
                return False, _("At least one user is required.")
            u = self.get(user_id)
            if u is None:
                return False, _("User not found.")
            self.users = [x for x in self.users if x["id"] != user_id]
        self.save()
        return True, None

    def set_active(self, user_id):
        """Point active at user_id (data only — ClientManager owns connecting).
        Returns the newly active user dict or None."""
        with self._lock:
            u = self.get(user_id)
            if u is None:
                return None
            self.active_id = user_id
        self.save()
        return u

    # -- PIN ---------------------------------------------------------------

    def verify_pin(self, user_id, pin):
        u = self.get(user_id)
        if not u or not u.get("pin_hash") or not u.get("pin_salt"):
            return False
        if pin is None:
            return False
        candidate = _hash_pin(pin, u["pin_salt"])
        return hmac.compare_digest(candidate, u["pin_hash"])

    def set_pin(self, user_id, pin, require_startup=False):
        """Set (or, with a falsy pin, clear) a user's PIN. Returns success."""
        with self._lock:
            u = self.get(user_id)
            if u is None:
                return False
            if not pin:
                u["pin_hash"] = None
                u["pin_salt"] = None
                u["require_pin_startup"] = False
            else:
                salt = os.urandom(16).hex()
                u["pin_hash"] = _hash_pin(pin, salt)
                u["pin_salt"] = salt
                u["require_pin_startup"] = bool(require_startup)
        self.save()
        return True


userManager = UserManager()
