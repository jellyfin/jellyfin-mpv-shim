"""What password stdjflib's QA accounts currently have.

Not a constant any more, and the reason is a real hole rather than tidiness.
A Jellyfin administrator can install plugins, which is code execution on the
host; the server answers `Access-Control-Allow-Origin: *` and checks no
`Host` header. So a QA server on a developer's machine with a *known* admin
password was a box any page a local browser happened to load could take over.
stdjflib now randomises `qa-admin`'s password per server state, publishes it
in a mode-0600 file, and binds to 127.0.0.1 only.

The other accounts are unchanged: every one of them is still `stdjflib`,
except `qa-nopassword`, whose whole point is not having one.

Deliberately free of imports from `jellyfin_mpv_shim` and free of side
effects, so `tools/shoot_browser.py` can use the same resolution as the suite
without pulling in `_e2e`'s config-directory isolation. One resolution, two
callers -- the same argument `_e2e` makes for importing the integration
harness rather than copying it.
"""

import json
import os
import tempfile
import urllib.parse

#: What the accounts are called when there is no published file to ask --
#: a run on a machine that cannot see the server's `/tmp`, i.e. the Windows
#: VM. The file overrides both when it is readable.
ADMIN_ACCOUNT = "qa-admin"
NO_PASSWORD_ACCOUNTS = ("qa-nopassword",)

#: Everybody else, still.
DEFAULT_PASSWORD = "stdjflib"

#: Hands the admin password to a run that cannot read the file -- the Windows
#: VM reaches the server over the network and has no view of the host's
#: `/tmp`. Checked before the file so a deliberate override always wins.
ADMIN_PASSWORD_ENV = "JMS_E2E_ADMIN_PASSWORD"

_facts = {}


def _published_path(port):
    """Where stdjflib writes the account file for the server on ``port``.

    Keyed by port rather than by server because that is how stdjflib keys it,
    and because the filter matrix holds sessions to `JMS_E2E_SERVER` and
    `JMS_E2E_SERVER_ALT` at the same time -- two servers, two ports, two
    different admin passwords.
    """
    # `os.getuid` does not exist on Windows, and the file is never there
    # anyway -- the VM reaches the server over the network and has no view of
    # the host's /tmp. A path that simply does not open is the right answer
    # there; `ADMIN_PASSWORD_ENV` is how that run gets the password.
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return os.path.join(tempfile.gettempdir(), "stdjflib-%d" % uid,
                        "servers", "%s.json" % port)


def facts_for(address):
    """stdjflib's published account file for one server, or ``{}``.

    Empty is not an error: a run from another machine cannot see the file at
    all, and `password_for` falls back to the constants above rather than
    refusing to log in.
    """
    port = urllib.parse.urlsplit(address).port or 8096
    if port in _facts:
        return _facts[port]
    try:
        with open(_published_path(port), encoding="utf-8") as fh:
            facts = json.load(fh)
        if not isinstance(facts, dict):
            facts = {}
    except (OSError, ValueError):
        facts = {}
    _facts[port] = facts
    return facts


def password_for(account, address):
    """The password ``account`` has on the server at ``address``.

    Resolved per call rather than baked into a default argument, because the
    answer now depends on **which server** is being asked and a default
    argument is evaluated once, at import, before any address is known.
    """
    facts = facts_for(address)
    if account in tuple(facts.get("no_password") or NO_PASSWORD_ACCOUNTS):
        return ""
    if account == (facts.get("admin") or ADMIN_ACCOUNT):
        # The published file FIRST. It is per server, and the whole reason
        # `_published_path` is keyed by port is that this suite holds
        # sessions to two servers with two different admin passwords -- so
        # an environment variable that wins would hand the second server the
        # first one's password and fail the login. The variable stays as the
        # fallback for a machine that cannot see the file at all (the Windows
        # VM reaches the server over the network), where one password is all
        # there is and all that is needed.
        return (facts.get("admin_password")
                or os.environ.get(ADMIN_PASSWORD_ENV) or DEFAULT_PASSWORD)
    return facts.get("password") or DEFAULT_PASSWORD


def source_of(account, address):
    """Where `password_for` got its answer, for a failure message.

    A wrong password reads as "login failed" and points at nothing. The one
    that will actually happen is `qa-admin` on a machine that can neither
    read the published file nor was given the override, where the answer is
    a guess at the historical default -- so that case says so.
    """
    facts = facts_for(address)
    if account in tuple(facts.get("no_password") or NO_PASSWORD_ACCOUNTS):
        return "no password, which is what this account is for"
    if account == (facts.get("admin") or ADMIN_ACCOUNT):
        # Same order as `password_for`: the published file first, the
        # environment second. A diagnostic that names a source the resolver
        # did not use sends the reader to the wrong password.
        if facts.get("admin_password"):
            return _published_path(
                urllib.parse.urlsplit(address).port or 8096)
        if os.environ.get(ADMIN_PASSWORD_ENV):
            return "$" + ADMIN_PASSWORD_ENV
        return ("a GUESS at the historical default: this is the admin, whose "
                "password is random per server state now, and there is no $%s "
                "and no readable %s" % (
                    ADMIN_PASSWORD_ENV,
                    _published_path(
                        urllib.parse.urlsplit(address).port or 8096)))
    return "the shared account password"
