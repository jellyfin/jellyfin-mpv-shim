"""The startup PIN lock.

Split out of the single 1,154-line ``PlayerGateway``; see
``gateway/__init__.py`` for why the facade is composed rather than nested.
"""

import logging
from .base import GatewayCore

log = logging.getLogger("mpvtk_browser.gateway.lock")


class LockMixin(GatewayCore):
    def needs_unlock(self):
        from ...users import userManager
        try:
            return bool(userManager.startup_needs_unlock())
        except Exception:
            return False

    def unlock_user(self, user_id, pin):
        """Verify a specific user's PIN (the PIN-setup dialog's current-PIN
        check), as opposed to unlock() which gates the active user."""
        from ...users import userManager
        try:
            return bool(userManager.verify_pin(user_id, pin))
        except Exception:
            return False

    def unlock(self, pin):
        from ...users import userManager
        try:
            return bool(userManager.verify_pin(userManager.active_id, pin))
        except Exception:
            log.error("mpvtk unlock failed", exc_info=True)
            return False
