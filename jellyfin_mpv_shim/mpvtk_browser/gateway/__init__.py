"""``PlayerGateway`` — the browser's one way to reach the rest of the app.

Everything the library UI needs from outside its own package comes through
here: playback and transport, the window handoff between browse and video,
servers and users, the offline catalog, SyncPlay. Nothing else under
``mpvtk_browser`` imports ``playerManager``, ``clientManager``,
``userManager`` or ``syncManager`` — ``tests/test_source_invariants.py``
enforces that.

This was ``ui.py``'s private ``_PlayerController``, which is where 61 of the
browser's 68 cross-package imports lived. It was already the boundary in
practice; extracting it made that a fact about the module graph rather than a
convention, which is what lets the page objects be constructed (and tested)
without dragging ``player.py`` in. See ``docs/ARCHITECTURE_TARGET.md`` §1.2.

**Why this is a package now.** One class had grown to 102 methods and 1,154
lines. It was never one responsibility — its own section banners named ten
domains, and one of them ("tile actions") had silently accumulated the whole
server-management surface underneath it. Splitting by those seams is the
cheapest structural win available: the facade below composes thirteen mixins,
so every existing ``gateway.X()`` call is unchanged, and each domain is now a
file you can read in one sitting.

The facade is **composition by inheritance, deliberately**. A nested-namespace
gateway (``gw.users.add``) would have been a wider change at every call site
for no gain here — these are a flat vocabulary of operations, not a tree. The
cost of flattening is that two mixins could define the same name and one
would silently win; ``tests/test_gateway_mixins.py`` refuses that, the same
way ``tests/test_mpvtk_browser_mixins.py`` does for the browser.

**Imports stay lazy, deliberately.** Every method imports its collaborator
inside the call rather than at module scope. That keeps ``mpvtk_browser``
importable without ``player.py`` — which selects an mpv backend at import
time and wires interdependent singletons — and it is also the seam that lets
``tests/test_player_controller.py`` substitute a broken collaborator and
sweep the whole class. ``deps.py`` holds the single exception and says why.

**The failure contract.** Almost every method catches ``Exception`` and
returns a documented fallback, because callers are the render loop (where an
escape kills the UI) or a pool worker (where it kills the worker with nobody
watching). Three deliberately do not:

* ``add_user`` / ``rename_user`` let the failure through — catching made the
  field clear and nothing happen, so the caller shows the message;
* ``_sync`` catches only to log, then re-raises, because the SyncPlay
  actions built on it need the caller to see the failure.

``tests/test_player_controller.py`` pins all three categories.
"""

from .diagnostics import DiagnosticsMixin
from .downloads import DownloadsMixin
from .editing import EditingMixin
from .hud import HudMixin
from .livetv import LiveTvMixin
from .lock import LockMixin
from .picture import PictureMixin
from .playback import PlaybackMixin
from .queue import QueueMixin
from .servers import ServersMixin, _collect_servers, _saved_servers_exist
from .syncplay import SyncPlayMixin
from .transport import TransportMixin
from .userdata import UserDataMixin
from .users import UsersMixin


class PlayerGateway(
    PlaybackMixin,
    PictureMixin,
    TransportMixin,
    HudMixin,
    UserDataMixin,
    ServersMixin,
    UsersMixin,
    LockMixin,
    QueueMixin,
    SyncPlayMixin,
    EditingMixin,
    LiveTvMixin,
    DownloadsMixin,
    DiagnosticsMixin,
):
    """Bridges the browser to the player: playback + browse/play window
    state. Imports player/event_handler lazily so the browser package stays
    independent of them for unit tests.

    Holds no state of its own — every method reaches a singleton and
    returns. That is what makes the flat composition above safe to read:
    there is no initialisation order between the mixins because there is
    nothing to initialise.
    """


__all__ = ["PlayerGateway", "_collect_servers", "_saved_servers_exist"]
