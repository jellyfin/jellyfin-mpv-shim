"""Every setting must appear in the configuration reference.

The docs drifted before anyone noticed: at the time this was written,
twenty-two settings had no entry at all, including the whole of offline sync
-- a feature named in the README's opening paragraph. Nothing catches that
kind of gap by review, because the omission is invisible from the diff that
causes it.

So the check is mechanical. Adding a setting to conf.py without documenting
it fails here, which is the only moment anyone is thinking about that
setting.

If a new key genuinely has no business in user documentation (runtime state
the app rewrites, migration bookkeeping), add it to INTERNAL below with a
reason rather than writing a hollow entry for it.
"""

import re
import os
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.conf import Settings  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(REPO, "docs", "configuration.md")
README = os.path.join(REPO, "README.md")

# Not user-facing settings: state the app owns and rewrites, or bookkeeping.
# Documenting these would invite people to edit values that get overwritten.
INTERNAL = {
    "config_version",        # migration bookkeeping; editing re-runs upgrades
    "client_uuid",           # device identity; editing orphans server sessions
    "window_width",          # window state, rewritten on exit
    "window_height",
    "window_maximized",
    "library_last_server",   # which server was last selected
    "music_volume",          # persisted playback volume, per media kind
    "video_volume",
    "language_config",       # structured; has its own prose section
    "auto_download_servers", # managed from the Downloads settings UI
}

# Settings documented as a group rather than one entry each, because the
# entries would be identical but for the codec name.
GROUPED = {
    "audio_passthrough_ac3",
    "audio_passthrough_dts",
    "audio_passthrough_eac3",
    "audio_passthrough_dts_hd",
    "audio_passthrough_truehd",
}

# The shape every entry uses:  - `key` - description. Default: `value`
ENTRY = re.compile(r"^\s*-\s+`([a-z][a-z0-9_]*)`", re.M)


def documented_keys(text):
    return set(ENTRY.findall(text))


class DocsCoverageTest(unittest.TestCase):
    def setUp(self):
        with open(DOCS, encoding="utf-8") as fh:
            self.docs = fh.read()
        self.documented = documented_keys(self.docs)
        self.settings = {k for k in Settings.__annotations__
                         if not k.startswith("_")}

    def test_every_setting_is_documented(self):
        missing = sorted(self.settings - self.documented - INTERNAL - GROUPED)
        self.assertEqual(
            missing, [],
            "These settings have no entry in docs/configuration.md:\n  "
            + "\n  ".join(missing)
            + "\n\nAdd one in the shape:  - `key` - What it does. Default: "
              "`value`\nIf it is not user-facing, add it to INTERNAL in "
              "tests/test_docs_coverage.py with a reason.")

    def test_grouped_settings_are_at_least_mentioned(self):
        # They get no entry of their own, but must not vanish entirely.
        for key in sorted(GROUPED):
            self.assertIn("`%s`" % key, self.docs,
                          "%s is not mentioned in the configuration reference"
                          % key)

    def test_no_documented_setting_has_been_removed(self):
        """Catches the reverse drift: a key deleted from conf.py but left in
        the docs, which sends people to edit something that does nothing."""
        # Enum values and language_config sub-keys use the same list shape, so
        # only flag names that look like settings and are not known vocabulary.
        vocabulary = {
            # audio_mode / osc_style / ui_scale / theme values
            "auto", "stereo", "optical", "hdmi", "mpvtk", "mpv", "default",
            "null", "jellyfin", "nebula",
            # language_config rule keys
            "type", "subtype", "alang", "slang", "amatch", "smatch",
            "aprefer", "sprefer", "aexclude", "sexclude",
        }
        ghosts = sorted(self.documented - self.settings - vocabulary)
        self.assertEqual(
            ghosts, [],
            "Documented in docs/configuration.md but absent from conf.py:\n  "
            + "\n  ".join(ghosts)
            + "\n\nEither the setting was removed (delete the entry) or this "
              "is a value/sub-key (add it to `vocabulary` here).")

    def test_internal_lists_have_not_gone_stale(self):
        """A key in INTERNAL or GROUPED that no longer exists means the
        exemption is silently covering nothing."""
        for name, group in (("INTERNAL", INTERNAL), ("GROUPED", GROUPED)):
            stale = sorted(group - self.settings)
            self.assertEqual(
                stale, [],
                "%s in tests/test_docs_coverage.py lists settings that no "
                "longer exist: %s" % (name, ", ".join(stale)))


class DocsStructureTest(unittest.TestCase):
    """The split into docs/ only helps if the pointers survive."""

    def setUp(self):
        with open(README, encoding="utf-8") as fh:
            self.readme = fh.read()
        with open(DOCS, encoding="utf-8") as fh:
            self.docs = fh.read()

    def test_readme_points_at_the_configuration_reference(self):
        self.assertIn("docs/configuration.md", self.readme)

    def test_reference_points_back_at_the_readme(self):
        self.assertIn("README.md", self.docs)

    def test_settings_reference_did_not_move_back_into_the_readme(self):
        # A handful of keys are named in the README's prose, which is fine.
        # A long list of entries means the reference has leaked back.
        self.assertLess(
            len(documented_keys(self.readme)), 25,
            "The README has grown a settings list again; the reference "
            "belongs in docs/configuration.md.")


if __name__ == "__main__":
    unittest.main()
