"""Picking the interface language, and finding the system's.

Two halves of one feature. The **selector** exposes `lang`, which has been a
real setting reachable only by hand-editing conf.json. The **detection** it
falls back to was wrong on Linux in three separate ways, which is why the
translations were largely inert: a user whose desktop is German got an
English UI with nothing to explain it, so nobody was moved to finish the
German translation either.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import os
import sys
import unittest
from unittest import mock

sys.argv = ["test"]

from jellyfin_mpv_shim import i18n  # noqa: E402
from jellyfin_mpv_shim import locale_index  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import config as cfg  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.settings.general import (  # noqa: E402
    _language_choices)


class Detection(unittest.TestCase):
    """What `configure()` asks for, which is all this layer decides.

    Asserted on the `languages=` list handed to gettext rather than on the
    catalog that comes back: whether a translation exists is a property of
    the build, and a test that needs one would skip on a source checkout --
    where these are the only tests of this at all.
    """

    def _asked(self, lang=None, platform="linux", env=None):
        seen = {}

        def fake(domain, localedir, languages=None, fallback=False):
            seen["languages"] = languages
            return mock.MagicMock()

        with mock.patch.object(i18n.settings, "lang", lang), \
                mock.patch.object(i18n.sys, "platform", platform), \
                mock.patch.object(i18n.gettext, "translation", fake), \
                mock.patch.dict(os.environ, env or {}, clear=False):
            i18n.configure()
        return seen["languages"]

    def test_an_explicit_language_wins(self):
        self.assertEqual(self._asked(lang="pt_BR"), ["pt_BR"])

    def test_no_setting_defers_to_gettext_on_linux(self):
        """None means "you work it out": gettext reads LANGUAGE, LC_ALL,
        LC_MESSAGES and LANG in that order, honours the first of them (which
        `locale.getdefaultlocale()` ignores, and which is where GNOME and
        KDE put the display language), and needs no generated locale."""
        self.assertIsNone(self._asked())

    def test_and_it_is_not_asked_for_the_locale_module_answer(self):
        """The regression this replaces: `getdefaultlocale()` answers
        ('C', 'UTF-8') for LANG=de_DE.UTF-8 on a box with only en_US
        generated -- the Debian, container and Flatpak default -- so the
        request went out as ["C"] and the German catalog was never looked
        for."""
        asked = self._asked(env={"LANG": "de_DE.UTF-8", "LANGUAGE": "de"})
        self.assertNotEqual(asked, ["C"])
        self.assertIsNone(asked)

    def test_windows_still_asks_the_system(self):
        """The one thing `getdefaultlocale` was here for: Windows carries
        its UI language in no environment variable, so gettext's lookup
        finds nothing there."""
        with mock.patch.object(i18n, "_system_languages", lambda: ["fr_FR"]):
            self.assertEqual(self._asked(platform="win32"), ["fr_FR"])

    def test_an_empty_string_is_not_a_language(self):
        """A cleared or hand-edited conf.json. As an explicit language it
        matches no catalog, so it would silently mean English forever."""
        self.assertIsNone(self._asked(lang=""))

    def test_a_system_with_no_answer_does_not_raise(self):
        """`locale.getdefaultlocale()` is removed in Python 3.15. When it
        goes, this must degrade to gettext's lookup rather than traceback at
        startup, before anything is on screen."""
        with mock.patch.object(i18n.sys, "platform", "win32"), \
                mock.patch.dict(sys.modules, {"locale": None}):
            self.assertIsNone(i18n._system_languages())


class Picker(unittest.TestCase):
    def test_every_shipped_locale_is_offered(self):
        codes = {code for code, _label, _pct in locale_index.LOCALES}
        shipped = {
            name for name in os.listdir(
                os.path.join(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))),
                    "jellyfin_mpv_shim", "messages"))
            if os.path.isdir(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "jellyfin_mpv_shim", "messages", name))}
        missing = sorted(shipped - codes)
        self.assertEqual(
            missing, [],
            "shipped with no entry in the picker, so nobody can choose "
            "them: %s. Run tools/gen_locale_index.py, and add an endonym "
            "to its table for anything it reports." % missing)

    def test_the_first_choice_is_the_system_and_carries_no_code(self):
        """None, not "" -- that is what `lang` already means, and
        `set_setting` writes None only for a nullable key."""
        choices = _language_choices()
        self.assertIsNotNone(choices)
        self.assertIsNone(choices[0][1])
        self.assertEqual([c for _l, c in choices[1:]],
                         [code for code, _n, _p in locale_index.LOCALES])

    def test_a_language_is_named_in_itself(self):
        """Endonyms: the person opening this control cannot read the
        language the app is currently in."""
        labels = dict((code, label) for label, code in _language_choices()[1:])
        self.assertTrue(labels["de"].startswith("Deutsch"))
        self.assertTrue(labels["zh_Hans"].startswith("简体中文"))
        self.assertTrue(labels["ru"].startswith("Русский"))

    def test_and_says_how_much_of_it_there_is(self):
        """41 locales sit between 25% and 50%, so a threshold either hides
        most of the list or means nothing. The number is what makes listing
        everything honest."""
        labels = dict((code, label) for label, code in _language_choices()[1:])
        self.assertRegex(labels["de"], r"\d+%$")

    def test_the_row_is_first_on_the_landing_page(self):
        """Somebody who needs it cannot read anything else on the screen to
        find it."""
        _title, keys = cfg.TAB_SECTIONS["general"][0]
        self.assertEqual(keys[0], "lang")

    def test_and_asks_for_a_restart(self):
        """22 module-scope `_()` calls evaluate at import, so a live
        reconfigure leaves those in the old language while the rest
        repaints into the new one."""
        self.assertIn("lang", cfg.RESTART_REQUIRED)


if __name__ == "__main__":
    unittest.main()
