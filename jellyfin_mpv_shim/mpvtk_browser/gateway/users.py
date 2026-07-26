"""Local users on this machine.

Split out of the single 1,154-line ``PlayerGateway``; see
``gateway/__init__.py`` for why the facade is composed rather than nested.
"""

import logging

from . import deps
from .base import GatewayCore

log = logging.getLogger("mpvtk_browser.gateway.users")


class UsersMixin(GatewayCore):
    def list_users(self):
        """``[{id, name, locked, active}]`` for the chrome's user switcher."""
        from ...users import userManager
        try:
            active = userManager.active_id
            return [{"id": u["id"], "name": u.get("name", "?"),
                     "locked": bool(userManager.is_locked(u["id"])),
                     "require_startup": bool(u.get("require_startup")),
                     "active": u["id"] == active}
                    for u in userManager.public_users()]
        except Exception:
            log.error("mpvtk list_users failed", exc_info=True)
            return []

    def switch_user(self, user_id, pin=None):
        """Switch the active local user and rebuild the data source.

        Returns the new source; False if the user is PIN-locked and the PIN
        didn't match (the caller re-prompts); None if the switch worked but
        there is nothing to browse. Those last two are distinct — reporting
        an unreachable server as a bad PIN is what made a correct PIN look
        wrong. Runs on the browser's worker pool — deps.clientManager.switch_user
        reconnects and can block."""
        from ...users import userManager
        try:
            if userManager.get(user_id) is None:
                return False
            if userManager.is_locked(user_id) and not userManager.verify_pin(
                    user_id, pin or ""):
                return False
            deps.clientManager.switch_user(user_id)
        except Exception:
            log.error("mpvtk switch_user failed", exc_info=True)
            return False
        return self.rebuild_source() or self.offline_source()

    def add_user(self, name):
        """Raises on failure (a duplicate name, most often). Catching here
        made the field clear and nothing happen."""
        from ...users import userManager
        userManager.add_user(name)

    def rename_user(self, user_id, name):
        """Raises on failure — see add_user."""
        from ...users import userManager
        userManager.rename_user(user_id, name)

    def delete_user(self, user_id):
        """Returns (ok, error) — the active user and the last user can't go."""
        from ...users import userManager
        try:
            return userManager.delete_user(user_id)
        except Exception:
            log.error("mpvtk delete_user failed", exc_info=True)
            return False, None

    def set_user_pin(self, user_id, pin, require_startup=False):
        from ...users import userManager
        try:
            userManager.set_pin(user_id, pin or None,
                                require_startup=require_startup)
            return True
        except Exception:
            log.error("mpvtk set_user_pin failed", exc_info=True)
            return False
