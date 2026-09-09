"""Every string in the template must carry translator context.

A Weblate translator for this project sees the English string and a
filename. Nothing else: `--add-location=file` removed line numbers on
purpose (docs/i18n.md section 2), only eleven entries carry a `msgctxt`, and
there are no screenshots attached. So "Off" arrives as three letters that
have to serve five different settings dropdowns, and `%s` arrives holding
nothing in particular.

The `#.` comments in `base.pot` are the only channel that reaches them, and
this is what keeps the channel from going quiet one string at a time. A new
`_()` call fails here until someone writes down what it means -- which is
the only moment anyone is thinking about that string.

`tools/pot_context.py` is what preserves those comments across
`regen_pot.sh`; this is the other half, because preservation alone cannot
tell a comment that was never written from one that was lost.
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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools.pot_context import parse, uncommented    # noqa: E402

POT = os.path.join(REPO, "jellyfin_mpv_shim", "messages", "base.pot")

#: Long enough to say what the string labels and where it sits. Anything
#: shorter is a restatement of the msgid, which is what the translator
#: already has.
MIN_CONTEXT = 20


class PotContextTest(unittest.TestCase):
    def test_every_entry_has_context(self):
        missing = uncommented(POT)
        # assertFalse on the count, not assertEqual on the list: an empty
        # template would otherwise dump all 1,083 strings into the failure.
        self.assertFalse(
            missing,
            "%d string(s) in base.pot have no '#.' translator context. "
            "Add one above the entry in base.pot saying what the string "
            "labels and which screen it is on; regen_pot.sh preserves it. "
            "First few: %r" % (len(missing), missing[:5]))

    def test_context_says_something(self):
        """A comment that only echoes the msgid is not context."""
        thin = []
        for block in parse(POT):
            if block.is_header or not block.comments:
                continue
            text = " ".join(block.comments).strip()
            if len(text) < MIN_CONTEXT or text.lower() == block.msgid.lower():
                thin.append((block.msgid, text))
        self.assertEqual(thin, [],
                         "translator context that restates the string or "
                         "says too little: %r" % (thin[:5],))

    def test_no_translators_prefix(self):
        """The `TRANSLATORS:` marker belongs in source, and there is none.

        xgettext writes that prefix when it lifts a comment out of Python
        with `--add-comments`. This project does not pass that flag (see
        tools/pot_context.py for why), so the prefix here would mean
        somebody wrote the context in a place the next regeneration reads
        from -- two authorities for one comment, and the .pot one loses.
        """
        offenders = [block.msgid for block in parse(POT)
                     if any(c.startswith("TRANSLATORS:")
                            for c in block.comments)]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
