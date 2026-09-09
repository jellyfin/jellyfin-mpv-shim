"""CONFIG_VERSION 6: the platform gets to answer two settings.

`notify_updates` becomes a tri-state so a Flatpak install can stop announcing
updates its desktop already announces, and SteamOS gets the gamepad turned
on. One probe (`hostinfo`) behind both.

**Why a migration at all, since both are about defaults:**
`SettingsBase.dict()` writes every field on every save, so every existing
conf.json already carries `notify_updates: true` and `input_gamepad: false`
explicitly. A changed default alone reaches nobody who has ever run the app.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import unittest
from unittest import mock

sys.argv = ["test"]

from jellyfin_mpv_shim import hostinfo  # noqa: E402
from jellyfin_mpv_shim.conf import CONFIG_VERSION, Settings  # noqa: E402


def _at(version, **kw):
    """A Settings as an older config would have loaded it."""
    s = Settings()
    s.config_version = version
    for key, value in kw.items():
        setattr(s, key, value)
    return s


class NotifyUpdates(unittest.TestCase):
    def setUp(self):
        # Every case here is about the stored value, so the machine must not
        # be able to change the answer.
        patch = mock.patch.object(hostinfo, "is_steamos", lambda: False)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_stored_true_becomes_default(self):
        """The case that cannot be read: "never touched it" and "asked for
        it" are the same byte on disk, and this resolves them to the
        platform. The few who had explicitly enabled an already-on setting
        lose that once."""
        s = _at(5, notify_updates="True")
        self.assertTrue(s._migrate({"notify_updates": True}))
        self.assertEqual(s.notify_updates, "default")

    def test_a_stored_false_becomes_disabled(self):
        """Unambiguous -- somebody turned it off -- so it is carried across
        rather than handed to the platform."""
        s = _at(5, notify_updates="False")
        s._migrate({"notify_updates": False})
        self.assertEqual(s.notify_updates, "disabled")

    def test_a_quoted_false_is_read_as_false(self):
        """`adv_bool`, not Python truthiness: the field was declared `bool`,
        which in this schema means the STRINGS "false"/"no"/"0"/"off" are
        False. A templated conf.json that quotes its booleans would
        otherwise have its "off" migrated to the platform default."""
        s = _at(5)
        s._migrate({"notify_updates": "false"})
        self.assertEqual(s.notify_updates, "disabled")

    def test_a_config_with_no_entry_resolves_by_platform(self):
        s = _at(5)
        s._migrate({})
        self.assertEqual(s.notify_updates, "default")

    def test_an_already_migrated_value_is_left_alone(self):
        """A config restored from a newer backup, or hand-edited. Running
        the step again must not overwrite a real answer."""
        s = _at(5, notify_updates="enabled")
        s._migrate({"notify_updates": "enabled"})
        self.assertEqual(s.notify_updates, "enabled")

    def test_the_version_is_stamped(self):
        s = _at(5)
        s._migrate({})
        self.assertEqual(s.config_version, CONFIG_VERSION)


class Resolution(unittest.TestCase):
    """What `default` means, which is the whole point of the tri-state."""

    def _wanted(self, stored, managed):
        s = Settings()
        s.notify_updates = stored
        with mock.patch.object(hostinfo, "flatpak_managed", lambda: managed):
            return s.notify_updates_wanted()

    def test_default_is_off_in_a_flatpak_and_on_elsewhere(self):
        self.assertFalse(self._wanted("default", True))
        self.assertTrue(self._wanted("default", False))

    def test_an_explicit_answer_is_never_overridden(self):
        for managed in (True, False):
            self.assertTrue(self._wanted("enabled", managed),
                            "the platform overruled a user who asked for it")
            self.assertFalse(self._wanted("disabled", managed))

    def test_an_unreadable_value_resolves_by_platform(self):
        """A hand-edited conf.json, or one written by 3.0.0 and loaded
        before the migration runs: degrade to the default rather than to
        silence."""
        self.assertTrue(self._wanted("True", False))
        self.assertFalse(self._wanted("", True))


class SteamosGamepad(unittest.TestCase):
    def _migrated(self, steamos, **kw):
        s = _at(5, **kw)
        with mock.patch.object(hostinfo, "is_steamos", lambda: steamos):
            s._migrate({})
        return s

    def test_steamos_turns_the_gamepad_on(self):
        """A Steam Deck is a machine where the pad is the only pointing
        device most users have, so off is the wrong answer there."""
        self.assertTrue(self._migrated(True).input_gamepad)

    def test_and_nothing_else_does(self):
        self.assertFalse(self._migrated(False).input_gamepad)

    def test_it_is_a_plain_bool_and_stays_one(self):
        """Deliberately not the tri-state next door: gamepad input is
        additive, so there is no preference for a platform default to
        overrule -- and a second tri-state would be one copied for being
        nearby rather than for being right."""
        self.assertIsInstance(self._migrated(True).input_gamepad, bool)

    def test_turning_it_back_off_survives_a_later_migration(self):
        """The step runs once, at version 6. Someone on a Deck who does not
        want it must not have it forced back on at every start."""
        s = _at(CONFIG_VERSION, input_gamepad=False)
        with mock.patch.object(hostinfo, "is_steamos", lambda: True):
            s._migrate({})
        self.assertFalse(s.input_gamepad)


if __name__ == "__main__":
    unittest.main()
