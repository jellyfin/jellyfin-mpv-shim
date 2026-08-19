"""What this user is allowed to do on this server.

Jellyfin grants SyncPlay, Live TV recording, downloading and collection
management independently of everything else, and the shim offered all of them
to everyone. A user who lacks one does not conclude they lack permission when
the button fails -- they conclude the client is broken, and they are half
right. Per-permission write-ups are in ``docs/PERMISSION_GAPS.md``.

Its own module for two reasons. **It is per client, not per app**: the answer
differs between two servers signed in at once, and caching on the client
object means every consumer reaches the same answer without owning it. And
**it fails open** -- only an answer the server actually gave can close a
gate, because a transient error is not a reason to take a working button
away.

The fetch is ``GET /Users/Me``, once per server per session and taken lazily;
see docs/jellyfin-api-notes.md section 13 for why the login response is not
enough.
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


def may_download(client):
    """`EnableContentDownloading` — may this user use `/Items/{id}/Download`?

    A *fourth* independently-granted permission, and the one with the widest
    blast radius: that endpoint is the only path to a Photo's original bytes
    and the only path to a Book's bytes at all. For a photo the image
    endpoint is an unconditional fallback and jellyfin-web draws the same
    line (`slideshow.js:getImgUrl`); for a book there is no second road. See
    `docs/PERMISSION_GAPS.md` §4 and §4b.

    Absent means an answer we did not get, so: permitted. Closing this gate
    on a failed fetch would send every photo through the resizer.
    """
    policy = policy_for(client) or {}
    if "EnableContentDownloading" not in policy:
        return True
    return bool(policy.get("EnableContentDownloading"))


def may_manage_collections(client):
    """`EnableCollectionManagement` — may this user write to a collection?

    A *fifth* independently-granted permission, off by default on a modern
    server, and the whole of `CollectionController` sits behind it -- so
    creating a collection, adding to one and removing from one are one
    permission and one 403. **There is no administrator bypass**, whatever
    jellyfin-web's `IsAdministrator || EnableCollectionManagement` suggests:
    we ask what the endpoint asks. See `docs/PERMISSION_GAPS.md` §5.

    Absent means an answer we did not get, so: permitted.
    """
    policy = policy_for(client) or {}
    if "EnableCollectionManagement" not in policy:
        return True
    return bool(policy.get("EnableCollectionManagement"))
