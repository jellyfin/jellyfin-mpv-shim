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

It is a replacement, not an alias [iw]: pointing it elsewhere hands ENTER
back to mpv, which is the whole request. `renderer.lua` resolves the name
when it installs the bindings and rebuilds `keyclaim.nav_names` with it;
the half of that no source-reading test can see -- the rebinding itself --
is driven through the real script-message boundary in
`tests/lua/test_renderer.lua`.
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
    """The default `renderer.lua` activates on, from its `NAV_KEYS` row.

    A DEFAULT and no longer a hardcode: the row carries a third field
    marking it as following `state.select_key`, which `mpvtk-select-key`
    moves. That the renderer really does resolve it -- and yields ENTER
    when it does -- is driven through the real binding in
    `tests/lua/test_renderer.lua`; what belongs here is only the fallback
    the three producers have to agree on. The marker is asserted so a
    row that quietly went back to a literal fails as a disagreement
    rather than passing as one.
    """
    src = ROOT.joinpath("jellyfin_mpv_shim", "mpvtk",
                        "renderer.lua").read_text(encoding="utf-8")
    block = src[src.index("local NAV_KEYS = {"):]
    block = block[:block.index("\n}")]
    marker = re.search(r"end,\s*true\s*\},", block)
    assert marker is not None, (
        "no NAV_KEYS row is marked as following state.select_key")
    # Back to the start of that row, not forward from the block: every row
    # opens the same way, so a forward match spans from the first one.
    row = block[block.rindex("{ '", 0, marker.start()):marker.end()]
    assert "nav_activate()" in row, (
        "the row marked configurable is not the activation one")
    return re.match(r"\{\s*'([^']+)'", row).group(1)


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
        src = ROOT.joinpath("jellyfin_mpv_shim", "player.py").read_text(
            encoding="utf-8")
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


class OfferedTest(unittest.TestCase):
    """The renderer half landed, so the setting is reachable -- and the
    keyboard is now the third thing that has to keep agreeing.

    These replace the tripwires this file shipped with. What they cannot
    see is the renderer actually rebinding: that is driven through the
    real script-message boundary in `tests/lua/test_renderer.lua`, which
    presses the moved key and checks ENTER was handed back.
    """

    def test_it_is_reachable_from_the_settings_screen(self):
        """The requirement is that a setting EXISTS to point somebody at
        [iw] -- an editable row, not a curated one.

        `sections()` is the "reachable at all" question and Advanced is
        one of its groups, so this passes for an uncurated key and fails
        for one the form hides. `SECTIONS` would be the wrong ask: it is
        the curated tabs only, and asserting membership there would pin
        the placement decision rather than the requirement.
        """
        reachable = {key for _title, keys in cfg.sections() for key in keys}
        self.assertIn("ui_select_key", reachable)

    def test_but_it_is_not_advertised_in_a_curated_group(self):
        """Key remapping is an answer to give somebody who asks, not a
        control to put in front of everyone [iw]. It falls through to
        Advanced, behind the disclosure.

        Not a tripwire like the case this replaces: that one meant "the
        renderer cannot honour this yet". This one is a placement the
        settings screen is expected to keep.
        """
        curated = {key for _title, keys in cfg.SECTIONS for key in keys}
        self.assertNotIn("ui_select_key", curated)

    def test_and_the_words_someone_looking_for_it_types(self):
        """Advanced is behind a disclosure, so search is how somebody sent
        looking for "the Enter setting" arrives. Measured misses, not
        guesses -- and asserted for `hud_wake_key` too, because it is the
        OTHER Enter during playback and answering only for this one is how
        the pair drifts."""
        for query in ("enter", "remap", "rebind", "keyboard", "shortcut"):
            with self.subTest(query=query):
                found = {key for _tab, _title, keys in cfg.search(query)
                         for key in keys}
                self.assertIn("ui_select_key", found)
                self.assertIn("hud_wake_key", found)

    def test_the_keyboard_is_pushed_the_value(self):
        """The keyboard's third of the agreement, at its one source.

        Read from source rather than executed, for the reason
        `remote_ok_key` above gives: building an `MpvtkApp` opens a real
        window. Scoped to the METHOD, so it says something -- `select_key`
        already appears in `push_gamepad` a few lines up, and a whole-file
        search for it would pass whatever this method did.

        That the settings screen re-pushes it, and re-pushes the gamepad
        table with it because a pad's Confirm carries the key as a
        literal, is executed instead:
        `tests/test_shell_settings.py:TestSelectKeyAppliesLive`.
        """
        src = ROOT.joinpath("jellyfin_mpv_shim", "mpvtk",
                            "app.py").read_text(encoding="utf-8")
        body = src[src.index("    def push_select_key"):]
        body = body[:body.index("\n    def ", 10)]
        self.assertIn("mpvtk-select-key", body)
        self.assertIn("select_key()", body,
                      "push_select_key reads the setting itself instead "
                      "of going through the one reader, so a cleared key "
                      "unbinds the keyboard while the pad falls back")


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
        doc = ROOT.joinpath("docs", "configuration.md").read_text(
            encoding="utf-8")
        entry = doc[doc.index("- `kb_menu_ok`"):]
        entry = entry[:entry.index("\n- `")]
        self.assertIn("legacy OSD menu only", entry)
        self.assertIn("ui_select_key", entry)


if __name__ == "__main__":
    unittest.main()
