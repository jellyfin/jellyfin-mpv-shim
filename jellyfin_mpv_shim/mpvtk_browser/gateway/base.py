"""What every gateway domain shares, and the coupling that survived the split.

Two things live here.

**``_act``** — the guarded "do something to the player" primitive. Transport,
HUD and queue all reach for it, so it is not a transport concern; it is the
gateway's own vocabulary.

**The cross-domain declarations below.** Splitting the gateway made something
visible that a single 1,154-line class had hidden: four calls cross a domain
boundary. Inside one class they were invisible; as separate mixins, mypy
names them. They are legitimate — a queue "add these and play" genuinely
needs playback, and a user switch genuinely needs the source rebuilt — so the
answer is to *declare* them rather than baseline the finding or pretend the
domains are independent.

Keeping them in one place means the coupling has a length. It is four:

    QueueMixin  -> play_list        (PlaybackMixin)
    UsersMixin  -> rebuild_source   (ServersMixin)
    UsersMixin  -> offline_source   (ServersMixin)
    ServersMixin-> offline_source   (its own; listed for symmetry)

If that list grows, the split is drifting back toward one class and the next
person can see it happening.
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
