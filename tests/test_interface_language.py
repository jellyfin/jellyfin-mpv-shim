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


_MESSAGES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "jellyfin_mpv_shim", "messages")
_SHIPPED = frozenset(
    name for name in os.listdir(_MESSAGES)
    if os.path.isdir(os.path.join(_MESSAGES, name)))


class Detection(unittest.TestCase):
    """What `configure()` asks for, which is all this layer decides.

    Asserted on the `languages=` list handed to gettext rather than on the
    catalog that comes back: whether a translation exists is a property of
    the build, and a test that needs one would skip on a source checkout --
    where these are the only tests of this at all.
    """

    def _asked(self, lang=None, platform="linux", env=None):
        """Ask `configure()` what it would request, and **put the catalogue
        back**.

        The patch covers `gettext.translation`; it does not cover what
        `configure()` does with the answer, which is to store it in the
        module global every `_()` in the app reads. So the MagicMock stood in
        as the app's translation for the rest of the process -- and in a
        whole-suite run that is every module imported after this one, where
        `_("Settings")` returns a mock and every assertion about a
        user-facing string fails. Around 340 of them, on both platforms, in
        modules with nothing to do with i18n.

        Invisible per-module (each runs alone and passes) and invisible under
        `tools/run_tests_parallel.py` (one process per module), which is the
        shape `docs/testing.md` warns about: the leg that catches
        cross-module interference is the whole-suite one, and this is what it
        was catching.
        """
        seen = {}

        def fake(domain, localedir, languages=None, fallback=False):
            seen["languages"] = languages
            return mock.MagicMock()

        before = i18n.get_translation()
        self.addCleanup(setattr, i18n, "translation", before)
        # The saved language has to be one the picker offers, or configure()
        # substitutes a fallback (see Fallback below). Detection is about
        # what is asked for, so the index says every language is complete.
        everything = [(c, n, True) for c, n, _ok in locale_index.LOCALES]
        with mock.patch.object(i18n.settings, "lang", lang), \
                mock.patch.object(i18n.sys, "platform", platform), \
                mock.patch.object(i18n.gettext, "translation", fake), \
                mock.patch.object(locale_index, "LOCALES", everything), \
                mock.patch.dict(os.environ, env or {}, clear=False):
            i18n.configure()
        i18n.translation = before
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
        """Windows carries its display language in no environment variable,
        so gettext's own lookup finds nothing there."""
        with mock.patch.object(i18n, "_system_languages", lambda: ["fr_FR"]):
            self.assertEqual(self._asked(platform="win32"), ["fr_FR"])

    def test_an_empty_string_is_not_a_language(self):
        """A cleared or hand-edited conf.json. As an explicit language it
        matches no catalog, so it would silently mean English forever."""
        self.assertIsNone(self._asked(lang=""))

    def test_a_system_with_no_answer_does_not_raise(self):
        """Detection runs before anything is on screen, so a platform that
        will not answer has to degrade to gettext's lookup rather than
        traceback at startup."""
        def boom():
            raise OSError("no MUI")

        with mock.patch.object(i18n.sys, "platform", "win32"), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(i18n, "_windows_ui_languages", boom):
            self.assertIsNone(i18n._system_languages())


class WindowsDetection(unittest.TestCase):
    """The display language, which Windows keeps apart from the format one.

    `locale.getdefaultlocale()` answered with the **regional format** --
    measured on a Windows 11 box, setting the format to German while the
    display language stayed English moved it to `de_DE` and left
    `GetUserDefaultUILanguage` on `en_US`. That is the same
    format-versus-display split that made this app ignore KDE's language
    picker, landing on the same wrong half. It is also removed in 3.15, and
    neither documented replacement answers this question: `setlocale` +
    `getlocale` gives `('English_United States', '1252')` on Windows, which
    matches no catalog, and this app never calls `setlocale` at all (see
    `mpvtk_browser/timefmt.py`).
    """

    def _detect(self, tags, env=None):
        with mock.patch.object(i18n.sys, "platform", "win32"), \
                mock.patch.dict(os.environ, env or {}, clear=True), \
                mock.patch.object(i18n, "_preferred_ui_tags", lambda: tags):
            return i18n._system_languages()

    def test_the_preferred_languages_become_catalog_names(self):
        self.assertEqual(self._detect(["pt-BR", "en-US"]),
                         ["pt_BR", "pt", "en_US", "en"])

    def test_preference_order_is_kept(self):
        """Windows returns the user's own fallback order, and gettext chains
        every catalog it finds in the order given -- so a string missing from
        the first language comes from the second, not from English."""
        self.assertEqual(self._detect(["de-DE", "fr-FR"])[0], "de_DE")

    def test_a_script_subtag_is_not_skipped(self):
        """The one gettext will not do for us: it splits a code on its first
        underscore, so it offers `zh_Hans_CN` and then `zh`, and `zh_Hans` --
        the catalog we ship -- is never looked for."""
        self.assertEqual(self._detect(["zh-Hans-CN"]),
                         ["zh_Hans_CN", "zh_Hans", "zh"])

    def test_the_trailing_nulls_are_not_languages(self):
        """The buffer is null-separated *and* null-terminated, so the split
        hands us two empty strings after the last tag."""
        self.assertEqual(self._detect(["fr-FR", "", ""]), ["fr_FR", "fr"])

    def test_every_answer_reaches_a_shipped_catalog(self):
        """The assertion the conversion exists for. Each of these is a real
        `GetUserPreferredUILanguages` tag for a language we ship, and none of
        them is spelled the way the directory is."""
        for tag, catalog in (("de-DE", "de"), ("pt-BR", "pt_BR"),
                             ("zh-Hans-CN", "zh_Hans"),
                             ("zh-Hant-HK", "zh_Hant_HK"),
                             ("en-GB", "en_GB"), ("nb-NO", "nb_NO")):
            with self.subTest(tag=tag):
                self.assertIn(catalog, _SHIPPED)
                self.assertIn(catalog, self._detect([tag]))

    def test_an_environment_that_says_something_wins(self):
        """Somebody launching from a configured shell, or a wrapper script,
        means it. None hands the question back to gettext, which reads all
        four of these; the API call is only for when nothing has an opinion.
        """
        for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            with self.subTest(envvar=name):
                self.assertIsNone(self._detect(["de-DE"], env={name: "fr_FR"}))

    def test_but_an_empty_one_does_not(self):
        """`LANG=` is not an answer, and on Windows it is a common one to
        find lying around."""
        self.assertEqual(self._detect(["de-DE"], env={"LANG": ""}),
                         ["de_DE", "de"])

    def test_a_platform_that_will_not_answer_is_not_an_error(self):
        self.assertIsNone(self._detect(None))


class Picker(unittest.TestCase):
    def test_every_shipped_locale_has_an_entry(self):
        codes = {code for code, _label, _ok in locale_index.LOCALES}
        missing = sorted(_SHIPPED - codes)
        self.assertEqual(
            missing, [],
            "shipped with no entry in the index, so the picker cannot decide "
            "about them: %s. Run tools/gen_locale_index.py, and add an "
            "endonym to its table for anything it reports." % missing)

    def test_the_index_matches_the_source_not_a_stored_number(self):
        """Completeness is recomputed from the strings in the code and the
        catalogs. A checked-in index that disagrees is stale -- a string was
        added or a catalog changed after it was generated -- and a stale
        "complete" is how an unfinished language gets offered."""
        from tools import gen_locale_index as gen

        total, rows = gen.build()
        with open(gen.OUT, encoding="utf-8") as fh:
            stored = fh.read()
        self.assertEqual(
            gen.render(total, rows), stored,
            "locale_index.py is stale: run tools/gen_locale_index.py")

    def test_the_first_choice_is_the_system_and_carries_no_code(self):
        """None, not "" -- that is what `lang` already means, and
        `set_setting` writes None only for a nullable key."""
        choices = _language_choices()
        self.assertIsNotNone(choices)
        self.assertIsNone(choices[0][1])

    def test_english_is_always_offered(self):
        self.assertIn(("English", "en"), _language_choices())

    def test_only_complete_languages_are_offered(self):
        offered = {c for _l, c in _language_choices()[1:]}
        complete = {code for code, _n, ok in locale_index.LOCALES if ok}
        self.assertEqual(offered, complete | {"en"})

    def test_an_incomplete_language_is_not_offered(self):
        fake = [("de", "Deutsch", True), ("fr", "Français", False)]
        with mock.patch.object(locale_index, "LOCALES", fake):
            labels = dict((c, l) for l, c in _language_choices())
        self.assertEqual(labels["de"], "Deutsch")      # name only
        self.assertNotIn("fr", labels)

    def test_no_label_carries_a_percentage(self):
        fake = [("de", "Deutsch", True)]
        with mock.patch.object(locale_index, "LOCALES", fake):
            for label, _code in _language_choices():
                self.assertNotIn("%", label)

    def test_the_percentage_help_text_is_gone(self):
        self.assertNotIn("lang", cfg.NOTES)


class Fallback(unittest.TestCase):
    """A saved language that is no longer offered."""

    def _asked(self, saved, env, index):
        seen = {}

        def fake(domain, localedir, languages=None, fallback=False):
            seen["languages"] = languages
            return mock.MagicMock()

        before = i18n.get_translation()
        self.addCleanup(setattr, i18n, "translation", before)
        with mock.patch.object(i18n.settings, "lang", saved), \
                mock.patch.object(i18n.sys, "platform", "linux"), \
                mock.patch.object(i18n.gettext, "translation", fake), \
                mock.patch.object(locale_index, "LOCALES", index), \
                mock.patch.dict(os.environ, env, clear=True):
            i18n.configure()
        i18n.translation = before
        return seen["languages"]

    def test_an_offered_language_is_used(self):
        self.assertEqual(
            self._asked("de", {}, [("de", "Deutsch", True)]), ["de"])

    def test_an_unoffered_one_falls_back_to_english(self):
        self.assertEqual(
            self._asked("fr", {"LANG": "C"}, [("fr", "Français", False)]),
            ["en"])

    def test_or_to_the_system_language_when_that_is_offered(self):
        index = [("fr", "Français", False), ("de", "Deutsch", True)]
        self.assertEqual(
            self._asked("fr", {"LANG": "de_DE.UTF-8"}, index), ["de"])

    def test_the_saved_setting_is_not_rewritten(self):
        with mock.patch.object(i18n.settings, "lang", "fr"):
            self._asked("fr", {}, [("fr", "Français", False)])
            self.assertEqual(i18n.settings.lang, "fr")

    def test_english_is_never_filtered(self):
        self.assertTrue(i18n.language_is_offered("en"))


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
