"""Local multi-user support ("fast user switching").

Jellyfin has no concept of switching between local accounts on one device;
because this client owns its own UI we can offer it. A *user* here is a local
grouping of one or more server logins that are connected together, only one is
active at a time, and each carries its own Jellyfin **device id** so two users
on the same physical server do not collide on one server-side session.

A user may be PIN-protected -- a parental-control affordance, *not* a security
boundary, since the PIN is only salted-hashed. Persistence is ``users.json``
next to ``cred.json``; on first run the existing ``cred.json`` is migrated
into a "(default)" user, which keeps the original ``settings.client_uuid`` so
its sessions and saved tokens keep working untouched.

What is per-user versus per-server, and how this meets ``clientManager``:
docs/architecture.md section 7.
"""

import hashlib
import hmac
import json
import logging
import os
import os.path
import threading
import time
import uuid

from . import conffile
from .conf import settings
from .constants import APP_NAME
from .i18n import _

log = logging.getLogger("users")

USERS_FILE = "users.json"
DEFAULT_USER_NAME = _("(default)")

#: The copy of the last payload that was written successfully. Ruling R12:
#: *"My goal is for that file to never corrupt, ideally we have a backup we
#: can restore if the power fails mid-write or something."* Distinct from the
#: `.unreadable-<ts>` file `load` sets aside, which rescues the **corrupt**
#: bytes after the fact and can restore nothing.
BACKUP_SUFFIX = ".bak"


def _has_users(data):
    """Is this a registry payload with somebody in it?

    The one predicate for "usable", asked of the primary and of the backup
    with the same words, because the failure this guards against is a rule
    applied to one of them and not the other.
    """
    return bool(isinstance(data, dict) and data.get("users"))


def _fsync_dir(directory):
    """Flush the directory entry itself, so a rename survives a power cut.

    `os.replace` is atomic with respect to other *readers*; it says nothing
    about whether the new name has reached the disk. Without this, a crash
    can leave the old name, the new name, or -- on ext4 with the default
    `data=ordered` -- a correctly renamed file of zero length.

    Best effort on purpose: Windows cannot open a directory as a file and
    raises here, and a filesystem that refuses the descriptor must not stop
    the app saving. The file's own fsync is the part that matters most and it
    has already happened by the time this runs.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)

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
        # UI action loop. Never held across network I/O.
        self._lock = threading.RLock()
        self.users = []  # list of user dicts (see _new_user for the shape)
        self.active_id = None
        self._loaded = False
        #: `users.json` existed and could not be parsed, so this registry is
        #: empty for a reason that is not "there are no logins". Consumers that
        #: would otherwise conclude something from an unknown login have to
        #: ask -- see `sync.manager._actor_resolver`, which withholds the
        #: resolver entirely rather than let a migration read None as nobody.
        self.load_failed = False

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
            # uuid of the server the library was last browsing, so the next
            # launch lands where this user left off instead of on whichever
            # server happened to connect first. None until they pick one.
            "last_server": None,
            # Accounts this profile has unattended downloading on for, as
            # [ServerId, UserId] pairs. `None` is "the global config key has
            # not been adopted yet" -- see _adopt_legacy_auto_download.
            "auto_download": None,
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

    @staticmethod
    def _read_json(path):
        """The parsed contents of one file, or None if it is not there or
        will not parse. The caller decides what either means."""
        try:
            with open(path) as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except Exception:
            log.warning("Could not read %s", path, exc_info=True)
            return None

    @staticmethod
    def _set_aside(path):
        """Move a damaged registry out of the way, keeping the bytes.

        Forensics, not recovery: this is the file R12 points out *"rescues
        the corrupt bytes after the fact"*. The thing that restores anything
        is the backup. Returns whether the move happened.
        """
        aside = "%s.unreadable-%d" % (path, int(time.time()))
        try:
            os.replace(path, aside)
            log.warning("The unreadable users.json is at %s", aside)
            return True
        except OSError:
            log.warning("Could not set the unreadable users.json aside; not "
                        "overwriting it.", exc_info=True)
            return False

    def load(self):
        """Load users.json, or migrate a pre-existing cred.json into a single
        "(default)" user on first run. Idempotent."""
        with self._lock:
            if self._loaded:
                return
            path = self._path()
            data = None
            recovered = False
            # **"Is the primary there" decides only whether there are bytes to
            # read and to set aside -- never whether the backup is consulted.**
            # It used to gate the whole block, and the recovery path opens
            # that window itself: `_set_aside` renames the primary before
            # `save()` rewrites it, so an interruption between the two leaves
            # only the backup. The next launch then skipped the restore, took
            # the first-run migration below, and overwrote the last copy of
            # the user's servers with a default profile.
            present = os.path.exists(path)
            if present:
                data = self._read_json(path)
            if not _has_users(data):
                # Damaged: it is missing, it did not parse, or it parsed to
                # something with nobody in it. This build never writes the
                # third (`delete_user` refuses to remove the last user), so
                # all three are damage rather than a state to honour -- and
                # honouring an empty one would fall through to the first-run
                # migration below and overwrite the backup with it.
                spare = self._read_json(path + BACKUP_SUFFIX)
                if _has_users(spare):
                    log.warning(
                        "users.json is damaged; restoring %d saved "
                        "login(s) from %s.",
                        sum(len(u.get("credentials") or ())
                            for u in spare["users"]),
                        path + BACKUP_SUFFIX)
                    data, recovered = spare, True
                    # Best effort: a save() below rewrites this path
                    # anyway, so failing to move it costs nothing here.
                    # Skipped when there is nothing there -- `os.replace`
                    # would only fail and log that it could not keep bytes
                    # that do not exist.
                    if present:
                        self._set_aside(path)
                elif present:
                    # **`present`, not `data is None`.** A registry that
                    # parses to nobody is the second damage case the comment
                    # above names, and it used to fall through here with
                    # `load_failed` False -- so `_registry_unreadable()`
                    # answered False and the catalog migrated with a resolver
                    # that names nobody for every login. A file that is not
                    # there at all is not damage; it is a first run, and the
                    # migration below is what it needs.
                    #
                    # **Not the same as a first run**, and the difference
                    # is load-bearing twice over. `save()` below would
                    # overwrite the only record the user has of their
                    # servers, and an empty registry that reports itself
                    # loaded makes every `actor_for` answer None -- which
                    # the catalog migration reads as "this queued
                    # position belongs to nobody" and deletes. So: keep
                    # the bytes, and say that we could not read them.
                    self.load_failed = True
                    log.warning("Could not read users.json and no usable "
                                "backup; keeping it aside and starting "
                                "fresh.")
                    if not self._set_aside(path):
                        # Nothing is salvageable if we cannot move it, so
                        # do not write over it either: a fresh registry in
                        # memory still lets the app start.
                        self.users = [self._new_user(
                            DEFAULT_USER_NAME,
                            device_id=settings.client_uuid,
                            credentials=[], is_default=True)]
                        self.active_id = self.users[0]["id"]
                        self._loaded = True
                        return
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
                # A first run *with this feature* is the ordinary upgrade
                # path, not a fresh install: cred.json was just folded in,
                # and the allow-list in conf.json is about those logins.
                self._adopt_legacy_auto_download()
                self.save()
                return
            # Repair a dangling active pointer.
            if not any(u["id"] == self.active_id for u in self.users):
                self.active_id = self.users[0]["id"] if self.users else None
            self._loaded = True
            adopted = self._adopt_legacy_auto_download()
            if recovered or adopted:
                # Put the recovered registry back where it belongs, so the
                # next launch is an ordinary one rather than another restore.
                self.save()

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
            "last_server": u.get("last_server"),
            # Not defaulted to []: None and [] mean different things here
            # (_adopt_legacy_auto_download), and _normalize runs on every
            # load, so defaulting would mark every old file as migrated.
            "auto_download": u.get("auto_download"),
            "credentials": list(u.get("credentials") or []),
        }

    @staticmethod
    def _write_durably(path, payload):
        """Write one JSON file so that a power cut leaves it old or new.

        Write-temp-then-rename: users.json holds every user's server tokens,
        and save() is reachable from several threads (the UI action loop, the
        switch worker, a finishing login). A truncate-in-place write
        interrupted or interleaved would lose all of them.

        **Atomicity is not durability**, which is the distinction R10 drew
        for the download-folder move and this file did not carry. The rename
        is atomic; without the fsync below the *contents* need not have
        reached the disk when it happens, so a crash can leave an atomically
        renamed file that is empty. That is the entry condition for the whole
        loss chain R12 is about, so both fsyncs are the point of this method
        rather than a precaution.

        Returns whether the file landed.
        """
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(payload, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            _fsync_dir(os.path.dirname(path) or ".")
            return True
        except Exception:
            log.error("Failed to write %s", path, exc_info=True)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

    def save(self):
        with self._lock:
            payload = {"active": self.active_id, "users": self.users}
            path = self._path()
            if not self._write_durably(path, payload):
                return
            # Only once the real file has landed, and never before: the
            # invariant is that the two are never both bad. Interrupted
            # here, the backup keeps the *previous* payload and the primary
            # is already the new one -- which is what a backup is for.
            # Interrupted during the backup write, the primary is good and
            # the next save rewrites the copy.
            self._write_durably(path + BACKUP_SUFFIX, payload)

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
             "default": bool(u.get("default")),
             # Exposed so the PIN dialog can seed its checkbox. Without it
             # the dialog always showed "off" and saving a new PIN silently
             # cleared the startup requirement.
             "require_startup": bool(u.get("require_pin_startup"))}
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

    def server_id_for(self, uuid):
        """The Jellyfin ``ServerId`` a saved login belongs to, or None.

        Our ``uuid`` identifies a saved *login*; Jellyfin's ``ServerId``
        identifies the *server*, and one server can carry several uuids --
        two accounts, or two addresses for one box. The download catalog
        scopes content by the second, because "do we hold this item" is a
        property of the server and the media rather than of who asked, so
        this is the translation between them and the only one.

        Single-valued, and that direction on purpose: many logins map to one
        server but never the reverse, so the inverse lookup (which this
        replaces) had to return a set and every caller had to decide what a
        set meant.

        It spans local users because the catalog does: there is one download
        store per machine rather than per user.

        **None means "this login resolves to no server".** It used to be
        documented as "cannot tell", reading as unscoped, and both halves of
        that are now false: `content_id_for` handles the two cases that wanted
        unscoped *before* reaching here -- no login named at all, and the
        browser's ``"offline"`` pseudo-server, which both answer `ANY_SERVER`
        -- and a `None` arriving at a content read now matches **no rows**
        rather than every one. Removing a server does not come through here
        either: its downloads keep the ``content_server_id`` they were written
        with. See `sync/db.py`'s `ANY_SERVER`.

        Read without ``_lock`` deliberately: ``save()`` holds that lock
        across a file write, and this is reached from under the player lock
        during a playback start. Safe because a credential's ``(Id, uuid)``
        pairing is written once and replaced whole, so an unlocked scan can
        miss an entry or see a superseded one but never a torn pair.
        """
        if not uuid:
            return None
        for u in list(self.users):
            for c in (u.get("credentials") or ()):
                if c.get("uuid") == uuid:
                    return c.get("Id") or None
        return None

    def actor_for(self, uuid):
        """The person behind a saved login, as ``(ServerId, UserId)``, or None.

        Both halves come straight off the credential -- ``Id`` is Jellyfin's
        server, ``UserId`` its account -- so this needs no network and works
        with every server unreachable, which is exactly when offline
        playback needs it.

        The pair rather than the uuid, because a uuid is a *delivery
        handle*: one server can carry several for several addresses, so two
        of them can be one person, and progress queued under one would be
        undrainable through the other. ``UserDataChanged`` announces itself
        with the same pair, which is the server agreeing about what
        identifies a person.

        None when the login is unknown, or when the credential names no
        account -- both mean "cannot say who", never "nobody". A caller
        recording progress must not file it against a guess.

        Unlocked for the reason in :meth:`server_id_for`.
        """
        if not uuid:
            return None
        for u in list(self.users):
            for c in (u.get("credentials") or ()):
                if c.get("uuid") == uuid:
                    server_id, user_id = c.get("Id"), c.get("UserId")
                    return (server_id, user_id) if user_id else None
        return None

    def server_name_for(self, server_id):
        """What to call a Jellyfin server on screen, or None.

        From any saved credential for it -- its ``Name``, or its address when
        the server never told us one. **Spans profiles**, like
        :meth:`server_id_for` and for the same reason: the download catalog is
        one store per machine, so a row can belong to a server only another
        local profile has a login for.

        None rather than the id when nothing names it: a raw ServerId on a
        tile is worse than no qualifier at all, and the caller is better
        placed to decide that.

        Unlocked for the reason in :meth:`server_id_for`.
        """
        if not server_id:
            return None
        for u in list(self.users):
            for c in (u.get("credentials") or ()):
                if c.get("Id") == server_id:
                    return c.get("Name") or c.get("address") or None
        return None

    def actor_on(self, server_id):
        """The **active** local profile's Jellyfin account on one server, or
        None.

        The offline counterpart to :meth:`actor_for`. With no live client
        there is no login object to ask, but the person at the keyboard is
        still whoever this profile is, so their credential for the server
        the media came from names them.

        Deliberately the active profile only, unlike every other lookup
        here: the others answer "who does this row belong to", which spans
        profiles because the catalog does, and this one answers "who is
        watching", which does not. Falling back to another profile's account
        would file one person's viewing against another.
        """
        if not server_id:
            return None
        user = self.active_user
        for c in ((user or {}).get("credentials") or ()):
            if c.get("Id") == server_id and c.get("UserId"):
                return c["UserId"]
        return None

    def public_state(self):
        return {
            "active": self.active_id,
            "users": self.public_users(),
            "startup_locked": self.startup_needs_unlock(),
            "known_servers": self.known_servers(),
        }

    # -- credential syncing (called by ClientManager) ----------------------

    def credentials_of(self, user_id):
        """A copy of one user's saved credentials, or ``[]`` if there is no
        such user.

        By id rather than only for the active user, because a login is slow
        enough to outlive a user switch: the credential it is replacing then
        belongs to whoever *started* it, and reading the active list instead
        answers about the wrong person. Copied for the same reason as
        :meth:`credentials_for_active`.
        """
        with self._lock:
            u = self.get(user_id)
            if u is None:
                return []
            return [dict(c) for c in u["credentials"]]

    def credentials_for_active(self):
        """A copy of the active user's saved credentials for ClientManager to
        connect. Copied so ClientManager's live mutations don't touch the store
        until it explicitly saves back."""
        with self._lock:
            return self.credentials_of(self.active_id)

    def set_active_credentials(self, credentials):
        """Replace the active user's stored credentials (already cleaned of
        volatile runtime keys by the caller) and persist."""
        with self._lock:
            u = self.active_user
            if u is None:
                return
            u["credentials"] = [dict(c) for c in credentials]
        self.save()

    # -- last browsed server ----------------------------------------------

    def get_last_server(self):
        """The uuid of the server the active user was last browsing, or None.

        The caller must treat this as a hint: the server may since have been
        removed or failed to connect, so it is only usable if it is still in
        the live server list.
        """
        with self._lock:
            u = self.active_user
            return u.get("last_server") if u else None

    def set_last_server(self, server_uuid):
        """Remember which server the active user is browsing.

        Called on every server switch, so it no-ops when unchanged rather than
        rewriting users.json (which holds every user's tokens) on each nav.
        """
        with self._lock:
            u = self.active_user
            if u is None or u.get("last_server") == server_uuid:
                return
            u["last_server"] = server_uuid
        self.save()

    # -- unattended downloading -------------------------------------------

    @staticmethod
    def _auto_download_pairs(u):
        """One profile's auto-download accounts, as ``(ServerId, UserId)``.

        Tolerant of a hand-edited file: an entry that is not a pair of two
        non-empty strings is skipped, because this is read from the download
        scheduler's thread where raising would stop the whole pass.
        """
        out = []
        for entry in (u.get("auto_download") or ()):
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            server_id, user_id = entry
            if server_id and user_id:
                out.append((server_id, user_id))
        return out

    def auto_download_accounts(self):
        """Every account on this machine with unattended downloading on.

        **Across profiles on purpose.** R14 first asked for a picker gate --
        *"auto downloads only run when the account that turned it on is
        selected in the picker"* -- and then withdrew it once R11's own
        reason was shown to cover it: the constraint is resource usage, not
        liveness. So the account is the unit and who is at the keyboard does
        not enter into it. Restoring a gate here is a behaviour change that
        needs its own ruling, not a tidy-up.
        """
        with self._lock:
            return {pair
                    for u in list(self.users)
                    for pair in self._auto_download_pairs(u)}

    def auto_download_on(self, uuid):
        """Is unattended downloading on for the account behind this login?

        Asked with a *login* uuid because that is what every caller holds --
        the Servers tab draws one row per credential and the scheduler
        iterates live clients -- while the answer is keyed on the account.
        That is the whole point of R14: one server answering at two
        addresses is two login uuids and one account, and a list of uuids
        left unattended fetching configured, enabled, and silently doing
        nothing for the whole of a trip.
        """
        account = self.actor_for(uuid)
        return bool(account) and account in self.auto_download_accounts()

    def auto_download_logins(self):
        """Every saved login whose **account** has unattended downloading on.

        `auto_download_on`'s answer for every uuid at once, and it exists
        because of what the lock costs. `_lock` is held by `save()` across two
        durable file writes and two directory fsyncs -- which is why
        `server_id_for` and `actor_for` are documented as deliberately never
        taking it -- and the Servers tab asked once per credential per
        repaint, on the render path. Ticking the checkbox calls `save()`, so
        the next frame blocked for the whole of that write, once per row.

        **Still keyed on the account** (R14): one server answering at two
        addresses is two uuids and one permission, so both of its logins come
        back here or neither does. A set of uuids is the shape the caller
        wants; the account is still what decides membership.

        `_lock` is an `RLock`, so calling `auto_download_accounts` inside it
        is free and keeps one definition of which pairs are on rather than a
        second copy of the comprehension.
        """
        with self._lock:
            on = self.auto_download_accounts()
            return {c["uuid"] for u in list(self.users)
                    for c in (u.get("credentials") or ())
                    if c.get("uuid") and (c.get("Id"), c.get("UserId")) in on}

    def set_auto_download(self, uuid, enabled):
        """Turn unattended downloading on or off for a login's account.

        Returns whether anything changed; False also covers a login this
        registry cannot resolve to an account, which has nothing to store.

        On is written into the profile holding the credential -- derivable
        rather than guessed, since a uuid appears in exactly one profile's
        list, which is the fact R21 leans on. **Off is removed from every
        profile**, so that what the checkbox says is true: the pair is one
        account's setting, and two profiles holding the same account hold
        the same fact about it, not two.
        """
        with self._lock:
            account, owner = None, None
            for u in self.users:
                for c in (u.get("credentials") or ()):
                    if c.get("uuid") == uuid and c.get("Id") and c.get("UserId"):
                        account, owner = (c["Id"], c["UserId"]), u
                        break
                if account is not None:
                    break
            if account is None:
                return False
            changed = False
            for u in self.users:
                current = self._auto_download_pairs(u)
                want = list(current)
                if enabled:
                    if u is owner and account not in want:
                        want.append(account)
                else:
                    want = [p for p in want if p != account]
                # Only on a real change: writing [] into a profile that has
                # never been migrated would mark it adopted and lose its
                # share of the legacy list.
                if want != current:
                    u["auto_download"] = [list(p) for p in want]
                    changed = True
        if changed:
            self.save()
        return changed

    def _adopt_legacy_auto_download(self):
        """Move the global auto-download allow-list in here, as accounts.

        R14's real migration. `conf.Settings` has no per-profile dimension
        and `object_types` would not take a map, so the list belongs in the
        file that already holds one credential list per profile -- and it is
        derivable rather than lossy, because a ticked login uuid appears in
        exactly one profile's credentials.

        **`None` per profile means "not adopted", `[]` means "adopted,
        nothing ticked".** That marker, rather than the config key being
        empty, is what makes this idempotent: clearing the key is best
        effort (a session that could not *read* conf.json refuses to write
        it), and without the marker a failed clear would undo an untick on
        the next launch.

        Called with `_lock` held, from `load` only.
        """
        raw = (settings.auto_download_servers or "").strip()
        if not raw:
            return False
        wanted = {s.strip() for s in raw.split(",") if s.strip()}
        changed = False
        for u in self.users:
            if u.get("auto_download") is not None:
                continue
            picked = []
            for c in (u.get("credentials") or ()):
                if c.get("uuid") not in wanted:
                    continue
                if not (c.get("Id") and c.get("UserId")):
                    continue
                pair = [c["Id"], c["UserId"]]
                if pair not in picked:
                    picked.append(pair)
            u["auto_download"] = picked
            changed = True
        if not changed:
            return False
        # Both of these lose a ticked server, so neither is silent: the
        # symptom otherwise is auto-download enabled and doing nothing,
        # which is the bug R14 is about.
        #
        # Counted over **every** profile rather than only the ones adopted
        # just now: with a profile added after an earlier adoption, a uuid
        # already filed elsewhere would otherwise be reported as lost.
        creds = {c["uuid"]: c for u in self.users
                 for c in (u.get("credentials") or ()) if c.get("uuid")}
        for uuid in sorted(wanted - set(creds)):
            log.warning("Auto-download was enabled for server %s, which no "
                        "saved login matches any more; it is off now.", uuid)
        for uuid in sorted(u for u in wanted & set(creds)
                           if not (creds[u].get("Id")
                                   and creds[u].get("UserId"))):
            log.warning("Auto-download was enabled for login %s, whose saved "
                        "credential names no account; re-tick it in Settings "
                        "-> Servers.", uuid)
        try:
            settings.auto_download_servers = None
            settings.save()
        except Exception:
            # In memory it is cleared either way, so this session will not
            # adopt twice; the per-profile lists are the authority now.
            log.warning("Could not clear the legacy auto-download list from "
                        "the config.", exc_info=True)
        return True

    def append_credentials_for(self, user_id, credential):
        """File one (cleaned) credential under a specific user, **replacing
        any entry that already carries the same uuid**.

        Used when a login finishes after the initiating user was switched
        away: the server must land under the user who added it, not whoever
        is active by then. Returns whether the user still exists.

        Replacing rather than appending because two credentials under one
        uuid is never a valid state, however it is reached -- the switcher
        keys by uuid so the duplicate is invisible, every save writes both,
        and every connect races them. A re-authentication that finished
        after a user switch reached exactly that, because the in-place
        replacement on the other branch of `_finalize_login` does not cover
        this path. Doing it here fixes every caller rather than that one.
        """
        with self._lock:
            u = self.get(user_id)
            if u is None:
                return False
            uuid_ = credential.get("uuid")
            for index, existing in enumerate(u["credentials"]):
                if uuid_ and existing.get("uuid") == uuid_:
                    u["credentials"][index] = dict(credential)
                    break
            else:
                u["credentials"].append(dict(credential))
        self.save()
        return True

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
