"""Auto-download: keep upcoming episodes on disk without being asked.

Runs as a scheduled job on the sync worker's idle loop, and only while
nothing is playing — downloading the next episode is worthless if it costs
the one you are watching its bandwidth.

Two sources, independently switchable:

* **Next Up** — the server's own Next Up list, i.e. the next episode of every
  series you have started. Broad, and scales with how many shows you have
  going.
* **Lookahead** — for series you already have downloads for, the next N
  episodes after the furthest one you hold. Narrow, follows a binge.

Everything it fetches is marked ``origin='auto'`` (see db.ORIGIN_*), which is
the whole safety story: the reaper only ever considers auto rows, so nothing
the user asked for is deleted to make room, however tight the cap. Asking for
an auto-downloaded item by hand promotes it to user-owned and takes it out of
the reaper's reach for good.

The reaper runs *before* the planner so a run that is over budget can free
space and then use it, rather than skipping for a whole interval.
"""

import json
import logging
import time

from ..conf import settings
from .db import STATUS_COMPLETE, ORIGIN_AUTO

log = logging.getLogger("sync.auto")

#: Fields needed to enqueue and to judge watched-ness. Matches what the
#: manager's own expansion asks for; MediaSources is what size estimates and
#: the container extension come from.
_FIELDS = "MediaSources,UserData,ParentId"

_GB = 1 << 30


class AutoDownloader:
    """Policy and scheduling for automatic downloads.

    Deliberately owns no thread of its own: :meth:`tick` is called from the
    sync worker's existing idle loop, so auto-downloads queue behind whatever
    the user asked for instead of racing it, and shutdown needs no extra
    coordination.
    """

    def __init__(self, manager, get_clients=None, is_busy=None,
                 now=time.time):
        self.manager = manager
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

    def tick(self):
        """Run the job if it is due. Never raises — this is called from the
        worker loop, where an exception would take the download thread with
        it and stop *user* downloads too."""
        if not self.due():
            return None
        self.last_run = self._now()
        try:
            return self.run()
        except Exception:
            log.error("Auto-download pass failed", exc_info=True)
            return None

    # -- the pass ----------------------------------------------------------

    def run(self):
        """One full pass: reap, then fill the freed space. Returns a summary
        dict (used by tests and the log line)."""
        reaped = self.reap()
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
        return max(0, int(settings.auto_download_max_gb or 0)) * _GB

    def free_budget(self):
        """Bytes of headroom under the cap. A cap of 0 means unlimited, which
        is deliberately reachable but not the default."""
        cap = self.cap_bytes()
        if cap <= 0:
            return float("inf")
        return cap - self.manager.db.auto_size()

    # -- reaping -----------------------------------------------------------

    def reap(self):
        """Delete auto-downloads that retention or the cap says should go.

        Order matters: watched first (they have served their purpose), then
        aged-out, then oldest-first purely to fit the cap. Only ever touches
        ``origin='auto'`` rows.
        """
        rows = self.manager.db.list_auto(status=STATUS_COMPLETE)
        if not rows:
            return 0
        removed = 0
        keep = []
        for row in rows:
            reason = self._retire_reason(row)
            if reason:
                self._delete(row, reason)
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
                if size <= cap:
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
        client = self.manager.get_client(row["server_uuid"])
        if client is not None:
            try:
                data = client.jellyfin.get_userdata_for_item(item_id)
                if data is not None:
                    return bool(data.get("Played"))
            except Exception:
                log.debug("Could not refresh userdata for %s", item_id,
                          exc_info=True)
        try:
            return bool(json.loads(row["userdata_json"] or "{}").get("Played"))
        except Exception:
            return False

    def _delete(self, row, reason):
        log.info("Auto-download: removing %s (%s).",
                 row["name"] or row["item_id"], reason)
        self.manager.delete(item_id=row["item_id"])

    # -- planning ----------------------------------------------------------

    def fill(self, budget):
        """Queue upcoming episodes until the budget is spent.

        Returns the number of items actually enqueued. Items whose size is
        unknown (the server omits MediaSources[0].Size for some containers)
        are counted as zero against the budget rather than skipped — the cap
        is then enforced on the next pass by the reaper, which works from real
        on-disk bytes.
        """
        queued = 0
        for server_uuid, item in self._candidates():
            if budget <= 0:
                break
            item_id = item.get("Id")
            if not item_id or self.manager.db.get(item_id):
                continue        # downloaded, queued, or errored — leave it
            added = self.manager.enqueue(server_uuid, item_id,
                                         item.get("Type") or "Episode",
                                         origin=ORIGIN_AUTO)
            if added:
                queued += added
                budget -= self._size_of(item)
        return queued

    @staticmethod
    def _size_of(item):
        sources = item.get("MediaSources") or [{}]
        return sources[0].get("Size") or 0

    def _candidates(self):
        """(server_uuid, item DTO) for everything worth downloading, best
        first. A generator so an exhausted budget stops the API calls too."""
        for server_uuid, client in (self.get_clients() or {}).items():
            if client is None:
                continue
            api = getattr(client, "jellyfin", None)
            if api is None:
                continue
            if settings.auto_download_next_up:
                for item in self._next_up(api):
                    yield server_uuid, item
            if int(settings.auto_download_lookahead or 0) > 0:
                for item in self._lookahead(api, server_uuid):
                    yield server_uuid, item

    def _next_up(self, api):
        """The next episode of every series in progress."""
        try:
            result = api.get_next(limit=50) or {}
        except Exception:
            log.debug("Next Up fetch failed", exc_info=True)
            return []
        return result.get("Items", []) or []

    def _lookahead(self, api, server_uuid):
        """The next N episodes after the furthest one already downloaded, per
        series we hold something for.

        Keyed off what is on disk rather than off server progress on purpose:
        the point is to stay a few episodes ahead of where the download set
        currently ends, so a binge never catches up with it.
        """
        count = int(settings.auto_download_lookahead or 0)
        out = []
        for series_id, last_id in self._series_frontier(server_uuid).items():
            try:
                result = api.shows("/%s/Episodes" % series_id, {
                    "UserId": "{UserId}",
                    "StartItemId": last_id,
                    "Limit": count + 1,
                    "Fields": _FIELDS,
                }) or {}
            except Exception:
                log.debug("Lookahead fetch failed for %s", series_id,
                          exc_info=True)
                continue
            # StartItemId is inclusive, so the first entry is the episode we
            # already have; the rest are what comes next.
            out.extend((result.get("Items", []) or [])[1:count + 1])
        return out

    def _series_frontier(self, server_uuid):
        """{series_id: id of the furthest episode we hold} for one server.

        "Furthest" is by (season, episode) number, which is the order the
        lookahead walks; a missing number sorts first so a special cannot
        become the frontier and push the window past real episodes.
        """
        frontier = {}
        for row in self.manager.db.list(status=STATUS_COMPLETE):
            if row["server_uuid"] != server_uuid or not row["series_id"]:
                continue
            key = (row["parent_index"] or 0, row["index_number"] or 0)
            best = frontier.get(row["series_id"])
            if best is None or key > best[0]:
                frontier[row["series_id"]] = (key, row["item_id"])
        return {sid: item for sid, (_key, item) in frontier.items()}
