"""``PlayerGateway`` — the browser's one way to reach the rest of the app.

Everything the library UI needs from outside its own package comes through
here: playback and transport, the window handoff between browse and video,
servers and users, the offline catalog, SyncPlay. Nothing else under
``mpvtk_browser`` imports ``playerManager``, ``clientManager``,
``userManager`` or ``syncManager`` — ``tests/test_source_invariants.py``
enforces that.

**The failure contract: a gateway method does not raise, and the three that
do are named.** Almost every method catches ``Exception`` and returns a
documented fallback, because callers are the render loop (where an escape
kills the UI) or a pool worker (where it kills the worker with nobody
watching). Three deliberately do not:

* ``add_user`` / ``rename_user`` let the failure through — catching made the
  field clear and nothing happen, so the caller shows the message;
* ``_sync`` catches only to log, then re-raises, because the SyncPlay
  actions built on it need the caller to see the failure.

``tests/test_player_controller.py`` pins all three categories.

**Imports stay lazy, deliberately.** Every method imports its collaborator
inside the call rather than at module scope, which is what keeps
``mpvtk_browser`` importable without ``player.py`` and is also the seam the
tests substitute at. ``deps.py`` holds the single exception and says why.

Why this is a package, and why the facade composes by inheritance rather
than nesting namespaces: see docs/browser-shell.md section 1.
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
    returns, which is what makes the flat composition above safe to read.
    """


__all__ = ["PlayerGateway", "_collect_servers", "_saved_servers_exist"]
