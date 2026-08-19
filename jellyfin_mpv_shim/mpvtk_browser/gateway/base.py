"""What every gateway domain shares, and the coupling that survived the split.

Two things live here. **``_act``**, the guarded "do something to the player"
primitive -- transport, HUD and queue all reach for it, so it is the
gateway's own vocabulary rather than a transport concern. And **the
cross-domain declarations below**: the three calls that cross a domain
boundary, declared under ``TYPE_CHECKING`` rather than baselined, so the
coupling has a length. If that list grows, the split is drifting back toward
one class and the next person can see it happening. The count is pinned by
tests/test_gateway_coupling.py, because a stale one makes the guard useless --
it said four from the day of the split and there were never more than three.
Why each is legitimate: see docs/browser-shell.md section 1.
"""

import logging
from typing import TYPE_CHECKING, Any, Callable

log = logging.getLogger("mpvtk_browser.gateway")


class GatewayCore:
    """Base of every gateway mixin."""

    @staticmethod
    def _act(fn: Callable[[Any], Any]) -> None:
        """Every transport action the browser performs goes through here.

        run_action, not a direct call: these run on the browser's loop
        thread, and the player's lock is held for the whole of a playback
        start. Calling through would freeze the window until the load
        finished or timed out — see PlayerManager.run_action.
        """
        from ...player import playerManager
        try:
            playerManager.run_action(fn)
        except Exception:
            log.error("mpvtk player action failed", exc_info=True)

    if TYPE_CHECKING:
        # Provided by a sibling mixin on the composed PlayerGateway. Declared,
        # not implemented — calling one of these on a bare mixin is a bug, and
        # the composition is what makes it valid.
        def play_list(self, item_ids, server_uuid, start_index,
                      offset_ticks=None, srcid=None, aid=None,
                      sid=None, pause_stills=True) -> None: ...

        def rebuild_source(self) -> Any: ...

        def offline_source(self) -> Any: ...
