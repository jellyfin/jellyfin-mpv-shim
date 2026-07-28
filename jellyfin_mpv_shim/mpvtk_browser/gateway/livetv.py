"""Scheduling and cancelling recordings.

The mutating half of Live TV; the reads live in ``repository.LibrarySource``.
The split is the same one playlist editing follows and for the same reason:
the repository is the seam an offline source stands in for, and an offline
source cannot schedule a recording.

**Every method here RAISES on failure.** These are all button presses, and a
swallowed error looks exactly like a recording that was scheduled — the
worst possible failure mode for this feature, because you find out when the
programme did not record. ``ItemActions.edit`` is what the callers wrap them
in, which rolls the optimistic state back and says so.
"""

from . import deps
from .base import GatewayCore


class LiveTvMixin(GatewayCore):
    def _live_tv(self, server_uuid, fn):
        """Run a Live TV mutation against one server's client. RAISES."""
        client = deps.clientManager.clients.get(server_uuid)
        if client is None:
            raise RuntimeError("no server connection")
        return fn(client.jellyfin)

    def create_timer(self, server_uuid, program_id):
        """Schedule a single recording of ``program_id``.

        Two calls, not one: the server's ``Timers/Defaults`` fills in the
        padding, keep-until and priority from *its* configuration as well as
        the programme's channel and times. Hand-building the DTO instead
        would silently ignore the user's DVR settings — which is how a
        recording ends up with no pre-roll on a server configured for two
        minutes of it.
        """
        return self._live_tv(
            server_uuid,
            lambda jf: jf.create_live_tv_timer(
                jf.get_new_timer_defaults(program_id=program_id)))

    def create_series_timer(self, server_uuid, program_id):
        """Record every showing of ``program_id``'s series."""
        return self._live_tv(
            server_uuid,
            lambda jf: jf.create_live_tv_series_timer(
                jf.get_new_timer_defaults(program_id=program_id)))

    def cancel_timer(self, server_uuid, timer_id):
        """Cancel a scheduled recording, or stop one in progress."""
        return self._live_tv(server_uuid,
                             lambda jf: jf.cancel_live_tv_timer(timer_id))

    def cancel_series_timer(self, server_uuid, timer_id):
        return self._live_tv(
            server_uuid, lambda jf: jf.cancel_live_tv_series_timer(timer_id))

    def update_timer(self, server_uuid, timer_id, changes):
        """Apply ``changes`` to one timer.

        Read-modify-write, because the endpoint replaces the whole DTO: a
        payload carrying only the two padding fields would blank the
        channel and the times along with everything else.
        """
        def apply(jf):
            timer = jf.get_live_tv_timer(timer_id) or {}
            timer.update(changes)
            return jf.update_live_tv_timer(timer_id, timer)

        return self._live_tv(server_uuid, apply)

    def update_series_timer(self, server_uuid, timer_id, changes):
        """Apply ``changes`` to one series rule. Read-modify-write, as
        above."""
        def apply(jf):
            timer = jf.get_live_tv_series_timer(timer_id) or {}
            timer.update(changes)
            return jf.update_live_tv_series_timer(timer_id, timer)

        return self._live_tv(server_uuid, apply)

    @staticmethod
    def live_tv_apis():
        """Whether the apiclient can schedule recordings at all.

        Same shape as ``edit_apis``: the Record affordances hide entirely on
        an apiclient too old to have these, rather than rendering and
        failing. Browsing the guide still works — only the buttons go.
        """
        try:
            from jellyfin_apiclient_python.api import API
        except Exception:
            return False
        return all(hasattr(API, name) for name in (
            "get_new_timer_defaults", "create_live_tv_timer",
            "create_live_tv_series_timer", "cancel_live_tv_timer",
            "cancel_live_tv_series_timer", "get_live_tv_series_timer",
            "update_live_tv_series_timer"))
