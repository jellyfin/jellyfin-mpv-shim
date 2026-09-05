"""One key means "activate what is focused", and three things send it.

#717: `kb_menu_ok` was set to something else, ENTER kept working, and the
new key did nothing. `kb_menu_ok` reaches only the legacy OSD menu
(`player.py` binds it, and `_on_menu_ok` returns immediately unless that
menu is showing); the ENTER the reporter was pressing belongs to the
library and the playback HUD, where the renderer hardcoded it.

**The trap, and why this file exists: ENTER is not only a keyboard key
here -- it is the wire format two OTHER input sources use to mean
Select.** `gamepad.py` maps Confirm to it, and `player.py`'s
`_NAV_KEYPRESS` maps a Jellyfin remote's "ok" to it. Remap the keyboard
alone and a user keeps keyboard selection while silently losing gamepad A
and phone/web Select. From the plan's exit predicate:

    A test that only presses the keyboard cannot fail for this.

So the assertions below are about AGREEMENT between the three, not about
any one of them.

**Half-landed on purpose.** The two synthetic producers read
`ui_select_key`; the renderer still force-binds ENTER, so the setting is
not offered in the settings screen and the docs say so. It is a
replacement, not an alias -- yielding ENTER back is the whole request
[iw] -- and that cannot happen until `renderer.lua`'s `NAV_KEYS` binds the
configured name and rebuilds `keyclaim.nav_names` with it. The last two
cases here are the tripwires for that work.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import pathlib
import re
import sys
import unittest
from unittest import mock

sys.argv = ["test"]

from jellyfin_mpv_shim import gamepad                       # noqa: E402
from jellyfin_mpv_shim.conf import (                        # noqa: E402
    Settings, select_key, settings)
from jellyfin_mpv_shim.mpvtk_browser import config as cfg   # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


def confirm_key():
    """What the gamepad's Confirm button sends.

    Through `select_key()`, because that is what the real caller passes
    (`mpvtk/app.py:push_gamepad`) -- calling `bindings()` with the raw
    setting would be a stand-in that skips the fallback and would report
    a pass for a cleared key that in production works.
    """
    binds = gamepad.bindings(select_key=select_key())
    return next(b[2] for b in binds if b[0] == gamepad.CONFIRM_BUTTON)


def remote_ok_key():
    """What a Jellyfin remote's "ok" sends.

    The same accessor, asserted at the source to be what `player.py`
    actually calls -- importing that module builds its singleton and opens
    a real mpv window (CLAUDE.md), so the press path cannot be executed
    here. `test_the_remote_path_calls_it` is the half that keeps this
    honest.
    """
    return select_key()


def renderer_activate_key():
    """The key `renderer.lua` binds to `nav_activate` in `NAV_KEYS`."""
    src = ROOT.joinpath("jellyfin_mpv_shim", "mpvtk",
                        "renderer.lua").read_text()
    block = src[src.index("local NAV_KEYS = {"):]
    block = block[:block.index("\n}")]
    row = next(line for line in block.splitlines()
               if "nav_activate()" in line)
    return re.search(r"\{\s*'([^']+)'", row).group(1)


class AgreementTest(unittest.TestCase):
    def test_all_three_send_the_same_key_by_default(self):
        """The property, at the shipped value. The two below are what stop
        this one passing because everything is frozen at "ENTER"."""
        self.assertEqual(confirm_key(), "ENTER")
        self.assertEqual(remote_ok_key(), "ENTER")
        self.assertEqual(renderer_activate_key(), "ENTER")

    def test_the_two_synthetic_ones_follow_the_setting_together(self):
        """Neither may keep a literal. This fails for a repair that moves
        the keyboard and forgets the pad -- the exact failure the plan's
        exit predicate names."""
        for key in ("k", "SPACE", "KP_ENTER"):
            with self.subTest(key=key), \
                    mock.patch.object(settings, "ui_select_key", key):
                self.assertEqual(confirm_key(), key,
                                 "the gamepad's Confirm is still a literal")
                self.assertEqual(remote_ok_key(), key,
                                 "the remote's ok is still a literal")

    def test_the_remote_path_calls_it(self):
        """`remote_ok_key` above is `select_key()`, so on its own it would
        prove nothing about the remote. This is what ties the two: read
        from source, matching the CALL and the table it replaced -- never
        a bare identifier, which the surrounding comment also contains."""
        src = ROOT.joinpath("jellyfin_mpv_shim", "player.py").read_text()
        body = src[src.index("elif ((action in self._NAV_KEYPRESS"):]
        body = body[:body.index("elif action ==")]
        self.assertIn("conf.select_key()", body)
        self.assertNotIn('"ENTER"', body,
                         "the remote path still spells the key itself")

    def test_and_the_gamepad_table_no_longer_spells_it(self):
        """`gamepad.py` is pure -- no settings import (its own header) --
        so Confirm carries a placeholder that `bindings()` substitutes.
        A literal creeping back would make the pad disagree silently."""
        confirm = next(b for b in gamepad.DEFAULT_BINDS
                       if b[0] == gamepad.CONFIRM_BUTTON)
        self.assertIs(confirm[2], gamepad.SELECT)

    def test_an_empty_value_falls_back_rather_than_unbinding(self):
        """A cleared key is a config that cannot select anything, on a
        screen whose only other way out is the mouse. Both readers fall
        back for the same reason `hud_wake_key` does."""
        for bad in ("", None):
            with self.subTest(bad=repr(bad)), \
                    mock.patch.object(settings, "ui_select_key", bad):
                self.assertEqual(confirm_key(), "ENTER")
                self.assertEqual(remote_ok_key(), "ENTER")


class NotYetOfferedTest(unittest.TestCase):
    """The tripwires for the renderer half. Both are meant to fail when
    that work lands -- read this file's header, then delete them."""

    def test_the_renderer_still_hardcodes_it(self):
        """When `NAV_KEYS` stops naming ENTER literally, the setting has a
        keyboard behind it and the two cases here are obsolete.

        The rest of the repair is not visible from this line, and is the
        reason it is called out rather than just deleted: the bound name
        also has to reach `keyclaim.nav_names`, whose comment warns that a
        claimed key OUTSIDE that set needs its own binding *and* its own
        removal -- and the opts blob only reaches the renderer on
        `mpvtk-hud` engage, while browse-mode nav is installed by
        `mpvtk-active`, which carries no opts.
        """
        self.assertEqual(
            renderer_activate_key(), "ENTER",
            "the renderer now resolves the activation key -- if it reads "
            "ui_select_key, delete this class and offer the setting")

    def test_so_the_setting_is_not_offered_yet(self):
        """Offering it while the renderer ignores it would let a user move
        the gamepad and the remote onto a key nothing listens for, while
        the keyboard kept ENTER. Worse than the bug being fixed."""
        curated = {key for _title, keys in cfg.SECTIONS for key in keys}
        self.assertNotIn("ui_select_key", curated,
                         "ui_select_key is on the settings screen; the "
                         "renderer must read it first")

    def test_and_it_is_documented_as_such(self):
        """Someone will find it in conf.json. The docs entry is what stops
        them concluding the feature is broken."""
        doc = ROOT.joinpath("docs", "configuration.md").read_text()
        entry = doc[doc.index("- `ui_select_key`"):]
        entry = entry[:entry.index("\n- `")]
        self.assertIn("Not yet offered in the settings screen", entry)


class DistinctFromTheMenuKeyTest(unittest.TestCase):
    def test_kb_menu_ok_is_a_separate_setting(self):
        """Kept independent [iw]: the smaller change, and it keeps
        `kb_menu_ok` meaning what it always has."""
        schema = cfg.settings_schema()
        self.assertIn("kb_menu_ok", schema)
        self.assertIn("ui_select_key", schema)
        self.assertNotEqual(Settings().kb_menu_ok,
                            Settings().ui_select_key,
                            "if these ever hold the same spelling, the "
                            "docs distinction below stops being visible")

    def test_the_docs_say_which_one_reaches_the_hud(self):
        """The whole of the bug report: the name promised the HUD. A user
        reading the reference has to be able to tell them apart."""
        doc = ROOT.joinpath("docs", "configuration.md").read_text()
        entry = doc[doc.index("- `kb_menu_ok`"):]
        entry = entry[:entry.index("\n- `")]
        self.assertIn("legacy OSD menu only", entry)
        self.assertIn("ui_select_key", entry)


if __name__ == "__main__":
    unittest.main()
