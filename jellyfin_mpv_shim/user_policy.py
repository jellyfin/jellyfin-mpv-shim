"""What this user is allowed to do on this server.

Jellyfin grants SyncPlay and Live TV *recording* independently of everything
else, and the shim offered both to everyone. A user who lacks them does not
conclude they lack permission when the button fails — they conclude the
client is broken, and they are half right. See ``docs/PERMISSION_GAPS.md``.

Two things make this its own module rather than a method somewhere:

**It is per client, not per app.** The answer differs between two servers
signed in at once, and every consumer already has a client or a server uuid
in hand — the browser's ``LibrarySource`` connections, the player's
``clientManager`` ones. Caching on the client object means both reach the
same answer without either owning it.

**It fails open.** A policy that could not be fetched must not hide features
that work: the current behaviour is to offer everything, and a transient
error is not a reason to take a working button away. Only an answer the
server actually gave can close a gate. This mirrors ``ItemActions.can_edit``
and ``can_record``, which take the same line about capability probes and for
the same reason.

The fetch is ``GET /Users/Me``: the policy also arrives on the login
response, but credentials are restored from ``cred.json`` on every run after
the first, so there is no login response to read most of the time. One
request per server per session, taken lazily and cached, so a user who never
opens Live TV never pays for it.
"""

import logging

log = logging.getLogger(__name__)

#: Where the answer is parked on a client object. A private attribute on
#: someone else's class, which is the price of not making every caller own a
#: cache; the name is distinctive enough not to collide.
_CACHE_ATTR = "_jms_user_policy"

#: SyncPlayAccess is three-valued, not a boolean. `JoinGroups` may join an
#: existing group but not create one, so a client that treats this as
#: on/off either hides a feature that works or offers one that 403s.
CREATE_AND_JOIN = "CreateAndJoinGroups"
JOIN_ONLY = "JoinGroups"
NO_SYNCPLAY = "None"


def policy_for(client, refresh=False):
    """This client's `UserPolicy`, cached on the client. ``{}`` if unknown.

    An empty dict is the fail-open answer: every accessor below reads it as
    "permitted", because that is what the app did before any of this existed.
    """
    if client is None:
        return {}
    if not refresh:
        cached = getattr(client, _CACHE_ATTR, None)
        if cached is not None:
            return cached
    policy = {}
    try:
        user = client.jellyfin.get_user() or {}
        policy = user.get("Policy") or {}
    except Exception:
        # Not an error worth a user-facing message: the consequence is that
        # a button stays visible, which is the status quo.
        log.debug("could not read the user policy", exc_info=True)
        return {}
    try:
        setattr(client, _CACHE_ATTR, policy)
    except Exception:
        pass
    return policy


def syncplay_access(client):
    """``CreateAndJoinGroups`` / ``JoinGroups`` / ``None``.

    Absent from the policy means an older server that has no such setting,
    which is the fail-open case and must read as full access.
    """
    return (policy_for(client) or {}).get("SyncPlayAccess") or CREATE_AND_JOIN


def may_use_syncplay(client):
    return syncplay_access(client) != NO_SYNCPLAY


def may_create_syncplay_group(client):
    return syncplay_access(client) == CREATE_AND_JOIN


def may_manage_live_tv(client):
    """`EnableLiveTvManagement` — a *third* Live TV permission.

    Separate from `EnableLiveTvAccess`: watching Live TV and managing
    recordings are granted independently, and the browse gate
    (``LibrarySource.has_live_tv``) answers only the first. Absent means an
    answer we did not get, so: permitted.
    """
    policy = policy_for(client) or {}
    if "EnableLiveTvManagement" not in policy:
        return True
    return bool(policy.get("EnableLiveTvManagement"))
