"""Auto-download: keep upcoming episodes on disk without being asked.

Runs on the sync worker's idle loop, and only while nothing is playing --
downloading the next episode is worthless if it costs the one you are watching
its bandwidth.

Two independently switchable sources: **Next Up** (the server's own list, broad)
and **Lookahead** (the next N from where you are watching, per series we already
hold something for).

Everything fetched is marked with an ``auto:`` origin (db.ORIGIN_*), and **that
is the whole safety story**: the reaper only ever considers auto rows, so nothing
the user asked for is deleted to make room. Asking for an auto item by hand
promotes it out of the reaper's reach, one-way.

The reaper runs *before* the planner, so a run that is over budget can free space
and then use it. See docs/offline-sync.md section 4.
"""

import json
import logging
import time

from ..conf import settings
from .db import (STATUS_COMPLETE, STATUS_DOWNLOADING, STATUS_PENDING,
                 ORIGIN_AUTO_NEXT_UP, ORIGIN_AUTO_LOOKAHEAD)

log = logging.getLogger("sync.auto")

#: Fields needed to enqueue and to judge watched-ness. Matches what the
#: manager's own expansion asks for; MediaSources is what size estimates and
#: the container extension come from.
#:
#: UserData is deliberately absent even though watched-ness is judged from it:
#: it is not an ItemFields value, it rides on the separate EnableUserData query
#: parameter (which defaults to true). Asking for it here only worked because
#: the server's comma binder discards names it cannot parse.
_FIELDS = "MediaSources,ParentId"

_GB = 1 << 30

#: Charged against the budget for an item whose size the server does not
#: report. Counting those as free let an unbounded number through: the cap is
#: checked against *anticipated* bytes, and nothing corrects an overshoot
#: afterwards now that the reaper only evicts watched items. A rough guess
#: that is sometimes wrong beats a zero that is always wrong in the same
#: direction.
_UNKNOWN_SIZE = 2 * _GB

#: Hard ceiling on items queued in one pass, whatever the budget says. A
#: backstop for a pathological library (every size unreported, a cap of 0
#: meaning unlimited): auto-download should trickle, not stampede.
#:
#: The default for ``auto_download_max_per_pass``, which #661 asked to make
#: configurable and which the issue explicitly wanted left at 20.
_MAX_PER_PASS = 20


def _per_pass():
    """How many items one pass may queue.

    Anything below 1 falls back to the default rather than being clamped to
    it: a hand-typed -3 clamped to 1 is a one-item-per-pass trickle that
    looks like the feature working, and this setting exists to make passes
    *bigger*.
    """
    value = settings.auto_download_max_per_pass
    try:
        value = int(value) if value is not None else 0
    except (TypeError, ValueError):
        return _MAX_PER_PASS
    return value if value >= 1 else _MAX_PER_PASS


def hysteresis():
    """``(min, max)`` for the lookahead, or None when it is not configured.

    **Both or neither.** A half-configured pair is something a person can type
    into the JSON, and guessing the other half is worse than declining -- "min 5"
    with no max could mean top up to 5 or to the old flat window, and those
    differ by however large the series is. Declined **loudly**, because silently
    doing nothing is indistinguishable from a bug. Same for max < min.
    """
    lo = settings.auto_download_lookahead_min
    hi = settings.auto_download_lookahead_max
    if lo is None and hi is None:
        return None
    if lo is None or hi is None:
        log.warning("auto_download_lookahead_min and _max must be set "
                    "together; ignoring the one that is set and using the "
                    "flat lookahead.")
        return None
    try:
        lo, hi = int(lo), int(hi)
    except (TypeError, ValueError):
        log.warning("auto_download_lookahead_min/_max are not numbers; "
                    "using the flat lookahead.")
        return None
    if lo < 0 or hi < lo:
        log.warning("auto_download_lookahead_max (%s) is below the minimum "
                    "(%s); using the flat lookahead.", hi, lo)
        return None
    return lo, hi


class AutoDownloader:
    """Policy and scheduling for automatic downloads.

    Deliberately owns no thread of its own: :meth:`tick` is called from the
    sync worker's existing idle loop, so auto-downloads queue behind whatever
    the user asked for instead of racing it, and shutdown needs no extra
    coordination.
    """

    def __init__(self, manager, get_clients=None, is_busy=None,
                 should_stop=None, now=time.time):
        self.manager = manager
        #: () -> bool; True once the app is shutting down. A pass is dozens
        #: of blocking HTTP calls, and SyncManager.stop() joins the worker
        #: with a short timeout and then closes the catalog regardless — so
        #: a pass that ignores this gets its writes silently dropped and its
        #: file deletions applied to a catalog that can no longer record
        #: them. The download loop polls the same flag every chunk.
        self.should_stop = should_stop or (lambda: False)
        #: () -> {server_uuid: client}
        self.get_clients = get_clients or (lambda: {})
        #: () -> bool; True while media is playing. Injected rather than
        #: imported so this module stays free of the player (and testable).
        self.is_busy = is_busy or (lambda: False)
        self._now = now
        self.last_run = 0.0

    # -- scheduling --------------------------------------------------------

    def due(self):
        """Should the job run now?

        False while playing: the check is here rather than at the call site so
        a run that becomes due mid-episode simply waits for the next tick
        instead of being lost.
        """
        if not settings.auto_download_enable:
            return False
        if self.is_busy():
            return False
        interval = max(1, int(settings.auto_download_interval_mins or 60)) * 60
        return (self._now() - self.last_run) >= interval

    def tick(self, should_stop=None):
        """Run the job if it is due. Never raises — this is called from the
        worker loop, where an exception would take the download thread with
        it and stop *user* downloads too.

        ``should_stop`` overrides the constructor's for this pass. The
        worker passes its own, which also returns True once that worker has
        been superseded — a stale one must abandon its pass rather than
        read a flag the worker replacing it has since cleared.
        """
        if not self.due():
            return None
        self.last_run = self._now()
        self._pass_stop = should_stop or self.should_stop
        try:
            return self.run()
        except Exception:
            log.error("Auto-download pass failed", exc_info=True)
            return None
        finally:
            self._pass_stop = None

    # -- the pass ----------------------------------------------------------

    def _interrupted(self):
        """Give up mid-pass on shutdown, or as soon as playback starts.

        is_busy is re-checked here rather than only in due(): a pass is
        long, and queueing downloads that then compete with the stream the
        user just started is exactly what this module promises not to do.
        """
        stop = getattr(self, "_pass_stop", None) or self.should_stop
        return stop() or self.is_busy()

    def run(self):
        """One full pass: reap, then fill the freed space. Returns a summary
        dict (used by tests and the log line)."""
        self._watched_cache = {}
        reaped = self.reap()
        if self._interrupted():
            return {"queued": 0, "reaped": reaped}
        queued = 0
        budget = self.free_budget()
        if budget <= 0:
            log.info("Auto-download: at the %d GB cap, nothing queued.",
                     settings.auto_download_max_gb)
        else:
            queued = self.fill(budget)
        if reaped or queued:
            log.info("Auto-download: queued %d, removed %d.", queued, reaped)
        return {"queued": queued, "reaped": reaped}

    # -- budget ------------------------------------------------------------

    def cap_bytes(self):
        """Cap in bytes. 0 means unlimited; negative means nothing at all."""
        return int(settings.auto_download_max_gb or 0) * _GB

    def free_budget(self):
        """Bytes of headroom under the cap.

        A cap of 0 is unlimited (reachable, not the default). A *negative*
        cap allows nothing: someone hand-editing -1 means "off", and
        clamping that up to 0 would hand them the opposite.
        """
        cap = self.cap_bytes()
        if cap < 0:
            return 0
        if cap == 0:
            return float("inf")
        return cap - self.manager.db.auto_size()

    # -- reaping -----------------------------------------------------------

    def reap(self):
        """Delete auto-downloads that retention or the cap says should go.

        Order matters: watched first (they have served their purpose), then
        aged-out, then oldest-first purely to fit the cap. Only ever touches
        rows with an ``auto:`` origin.
        """
        removed = 0
        # ERROR rows keep their .part bytes, count against auto_size(), and
        # are never retried by the planner (db.get finds them), so nothing
        # else reclaims them.
        try:
            for row in self.manager.db.list_auto_incomplete():
                if self._interrupted():
                    return removed
                self._delete(row, "failed download")
                removed += 1
        except Exception:
            log.debug("Could not reclaim failed auto downloads",
                      exc_info=True)
        rows = self.manager.db.list_auto(status=STATUS_COMPLETE)
        if not rows:
            return removed
        keep = []
        for row in rows:
            if self._interrupted():
                return removed
            reason = self._retire_reason(row)
            if reason:
                # Only the age rule needs a tombstone. A watched item is
                # skipped by enqueue's own include_watched=False, and a
                # cap eviction is space pressure rather than a judgement
                # that the user does not want the episode.
                self._delete(row, reason, tombstone=reason.startswith("un"))
                removed += 1
            else:
                keep.append(row)
        # Whatever survived retention still has to fit the budget — but only
        # *watched* items may be evicted for space. Deleting an unwatched
        # episode to make room for another unwatched episode is churn: it
        # would trade the one you are about to watch for one further ahead.
        # If the watched items are not enough, we stay over and queue
        # nothing (run() logs it) rather than reclaiming space destructively.
        cap = self.cap_bytes()
        if cap > 0:
            size = sum((r["downloaded_bytes"] or 0) for r in keep)
            # keep is already oldest-first (list_auto orders by completed_at).
            for row in keep:
                if size <= cap or self._interrupted():
                    break
                if not self._is_watched(row):
                    continue
                self._delete(row, "over the cap")
                size -= row["downloaded_bytes"] or 0
                removed += 1
        return removed

    def _retire_reason(self, row):
        """Why this auto-download should be deleted, or None to keep it."""
        if settings.auto_download_delete_watched and self._is_watched(row):
            return "watched"
        days = int(settings.auto_download_keep_days or 0)
        if days > 0:
            stamp = row["completed_at"] or row["added_at"] or 0
            if stamp and (self._now() - stamp) > days * 86400:
                return "unwatched for %d days" % days
        return None

    def _is_watched(self, row):
        """Watched according to the freshest thing we have.

        The catalog's userdata is a download-time snapshot, so it says
        "unwatched" forever if we trust it alone. Ask the server when one is
        reachable and fall back to the snapshot when offline — an item simply
        does not get reaped until we can confirm it, which is the safe way to
        be wrong.
        """
        item_id = row["item_id"]
        cache = getattr(self, "_watched_cache", None)
        if cache is not None and item_id in cache:
            # The cap loop re-checks every survivor the retention loop
            # already asked about; without this that is a second HTTP round
            # trip per row, per pass.
            return cache[item_id]
        client = self.manager.get_client(row["server_uuid"])
        if client is not None:
            try:
                data = client.jellyfin.get_userdata_for_item(item_id)
                if data is not None:
                    played = bool(data.get("Played"))
                    if cache is not None:
                        cache[item_id] = played
                    return played
            except Exception:
                log.debug("Could not refresh userdata for %s", item_id,
                          exc_info=True)
        try:
            return bool(json.loads(row["userdata_json"] or "{}").get("Played"))
        except Exception:
            return False

    def _delete(self, row, reason, tombstone=False):
        log.info("Auto-download: removing %s (%s).",
                 row["name"] or row["item_id"], reason)
        self.manager.delete(item_id=row["item_id"])
        if tombstone:
            # Remember the decision. An unwatched episode dropped on age is
            # still the server's Next Up -- unwatched is why it is there --
            # so without this the next pass re-downloads it and the cycle
            # repeats every keep_days, forever.
            try:
                self.manager.db.mark_discarded(row["item_id"])
            except Exception:
                log.debug("Could not record the discard for %s",
                          row["item_id"], exc_info=True)

    # -- planning ----------------------------------------------------------

    def fill(self, budget):
        """Queue upcoming episodes until the budget is spent. Returns the number
        actually enqueued.

        The cap is enforced against *anticipated* sizes, which the server
        sometimes under-reports or omits, so it is a **soft ceiling**: a pass can
        overshoot by one item plus whatever the estimates got wrong. Real on-disk
        bytes are measured next pass, so an overshoot throttles the pass after it
        rather than compounding.
        """
        queued = 0
        cap = _per_pass()
        try:
            discarded = self.manager.db.discarded_ids()
        except Exception:
            log.debug("Could not read the discard list", exc_info=True)
            discarded = set()
        capped = False
        for server_uuid, item, origin in self._candidates():
            if queued >= cap:
                # Reached only when there IS another candidate: the loop
                # body does not run otherwise.
                capped = True
                break
            if budget <= 0:
                break
            if self._interrupted():
                break
            item_id = item.get("Id")
            if not item_id or self.manager.db.get(item_id):
                continue        # downloaded, queued, or errored — leave it
            if item_id in discarded:
                continue        # reaped on age; do not fetch it again
            added = self.manager.enqueue(server_uuid, item_id,
                                         item.get("Type") or "Episode",
                                         origin=origin)
            if added:
                queued += added
                budget -= self._size_of(item)
        if capped:
            # Only when the loop actually broke with a candidate in hand.
            # `queued >= cap` on its own is true whenever the pass filled
            # exactly to the limit with nothing left over, and then this
            # promised a "rest" that does not exist -- which matters
            # because this line is how the feature is verified by hand.
            log.info("Auto-download: stopped at the %d-item per-pass limit; "
                     "the rest follow next pass.", cap)
        return queued

    @staticmethod
    def _size_of(item):
        """Anticipated bytes, never zero — see _UNKNOWN_SIZE."""
        sources = item.get("MediaSources") or [{}]
        return sources[0].get("Size") or _UNKNOWN_SIZE

    @staticmethod
    def allowed_servers():
        """Server uuids the scheduler may pull from.

        Empty means none. A logged-in server is not necessarily *your*
        server, and unattended downloads are a rude thing to point at a
        friend's box, so this is an explicit allow-list rather than an
        opt-out. The settings screen seeds it with the server you were
        looking at when you switched auto-download on.
        """
        raw = (settings.auto_download_servers or "").strip()
        return {s.strip() for s in raw.split(",") if s.strip()}

    def _candidates(self):
        """(server_uuid, item DTO, origin) for everything worth downloading,
        best first. A generator so an exhausted budget stops the API calls
        too. The origin travels with the item so the downloads manager can
        show each source as its own subtree."""
        allowed = self.allowed_servers()
        if not allowed:
            # Reachable by hand-editing the config (the settings screen always
            # seeds a server when switching this on). Say so — enabled but
            # silently doing nothing is otherwise indistinguishable from a bug.
            log.warning("Auto-download is on but no servers are selected; "
                        "tick one in Settings -> Servers.")
            return
        for server_uuid, client in (self.get_clients() or {}).items():
            if client is None:
                continue
            if server_uuid not in allowed:
                continue
            api = getattr(client, "jellyfin", None)
            if api is None:
                continue
            if settings.auto_download_next_up:
                for item in self._next_up(api):
                    yield server_uuid, item, ORIGIN_AUTO_NEXT_UP
            if int(settings.auto_download_lookahead or 0) > 0:
                for item in self._lookahead(api, server_uuid):
                    yield server_uuid, item, ORIGIN_AUTO_LOOKAHEAD

    def _next_up(self, api):
        """The most recent N entries of Next Up.

        Bounded because Next Up is as long as your started-series count —
        50 on a real library, which is far more than anyone wants fetched
        unattended. The server returns it most-recent first, so a small
        limit is the shows you are actually working through.
        """
        limit = max(1, int(settings.auto_download_next_up_limit or 10))
        try:
            # _FIELDS is not optional here: /NextUp is a list query and omits
            # MediaSources unless asked. Without the sizes every candidate
            # falls back to _UNKNOWN_SIZE, so the cap would be spent against a
            # guess for 100% of Next Up items.
            result = api.get_next(limit=limit, fields=_FIELDS) or {}
        except Exception:
            log.debug("Next Up fetch failed", exc_info=True)
            return []
        return result.get("Items", []) or []

    def _lookahead(self, api, server_uuid):
        """The N episodes from where you are watching, per series we hold
        something for.

        **Anchored on watch progress, never on what is on disk.** Anchoring on
        the furthest episode held is the obvious reading of "keep N ahead" and is
        a ratchet -- the window walks the whole series whether or not anybody
        watches it. Anchored on Next Up, it only advances when the user does.

        Pinned by test_the_window_does_not_walk_the_series_on_its_own.
        See docs/offline-sync.md section 4.
        """
        flat = int(settings.auto_download_lookahead or 0)
        window = hysteresis()
        out = []
        for series_id in self._followed_series(server_uuid):
            anchor = self._watch_position(api, series_id)
            if anchor is None:
                # Nothing next: the series is finished, or the server will not
                # say. Either way, extending the window is a guess, and the
                # wrong guess here is the runaway this method exists to avoid.
                continue
            count = window[1] if window is not None else flat
            try:
                result = api.get_episodes(series_id, start_item_id=anchor,
                                          limit=count,
                                          fields=_FIELDS) or {}
            except Exception:
                log.debug("Lookahead fetch failed for %s", series_id,
                          exc_info=True)
                continue
            # StartItemId is inclusive, so the first entry is the anchor —
            # the next episode to watch, which is part of the window.
            items = (result.get("Items", []) or [])[:count]
            if window is not None:
                low = window[0]
                held = self._held_ids(server_uuid, series_id)
                if held is None:
                    continue    # see _held_ids: look stocked, do nothing
                have = sum(1 for item in items if item.get("Id") in held)
                if have >= low:
                    # Above the low mark: queue nothing. The whole point --
                    # the reporter's disks were spinning up for one episode
                    # at a time, and a stocked series should cost no
                    # transfers.
                    continue
            out.extend(items)
        return out

    def _held_ids(self, server_uuid, series_id):
        """Ids of this series we hold or have asked for, or None if unknown.

        **Ids rather than a count**, and the caller intersects them with the
        window -- counting every held episode meant somebody holding twenty *old*
        ones was above any minimum for ever and the series was never topped up
        again, silently. "Upcoming" is the word doing the work.

        Queued and in-progress count; **errored rows do not**, or a failure reads
        as stock and reaches the same silent stall by another route.
        See docs/offline-sync.md section 4.
        """
        held = {STATUS_COMPLETE, STATUS_PENDING, STATUS_DOWNLOADING}
        try:
            rows = self.manager.db.list(series_id=series_id)
        except Exception:
            log.debug("Could not read held episodes for %s", series_id,
                      exc_info=True)
            # None, not an empty set: an empty one reads as "hold nothing",
            # which tops the series up on every pass -- the behaviour being
            # removed. The caller skips the series this time instead.
            return None
        return {row.get("item_id") for row in rows
                if row.get("server_uuid") == server_uuid
                and row.get("status") in held}

    @staticmethod
    def _watch_position(api, series_id):
        """Id of the next episode to watch in one series, or None.

        The server's own answer (/Shows/NextUp for that series), because it is
        the only thing that knows what was watched on other clients. For a
        series with nothing watched yet it names the first episode, so a show
        we hold but have not started anchors at the beginning rather than
        going unanchored.
        """
        try:
            result = api.get_next(series_id=series_id, limit=1) or {}
        except Exception:
            log.debug("Next Up fetch failed for series %s", series_id,
                      exc_info=True)
            return None
        items = result.get("Items") or []
        return items[0].get("Id") if items else None

    def _followed_series(self, server_uuid):
        """Series ids we hold at least one completed download for, on one
        server. This is the scope of the lookahead: the shows the user has
        shown some interest in, as opposed to Next Up's whole library."""
        return {row["series_id"]
                for row in self.manager.db.list(status=STATUS_COMPLETE)
                if row["server_uuid"] == server_uuid and row["series_id"]}
