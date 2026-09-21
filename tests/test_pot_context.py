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


def _carried_context(blocks):
    """msgids whose `#.` block begins with the whole of the one before it.

    Separate from the case that uses it so the rule can be pointed at
    something other than the live template -- see
    `TheCarriedContextRuleFiresTest`. A rule whose only input is a file that
    is currently clean reports "clean" just as loudly when it has stopped
    matching at all.
    """
    previous, offenders = None, []
    for block in blocks:
        if block.is_header:
            continue
        comments = list(block.comments)
        if (previous and comments[:len(previous)] == previous
                and len(comments) > len(previous)):
            offenders.append(block.msgid)
        # Only an entry that HAS a comment can be carried into the next one,
        # and one with none breaks the chain rather than matching everything.
        previous = comments or None
    return offenders


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

    def test_no_entry_carries_the_previous_entry_s_context(self):
        """A comment that starts with the whole of the one above it.

        The other half of this file's job, and it took a merge to find:
        `test_every_entry_has_context` fails on an ABSENT comment and cannot
        see a WRONG one. A merge dropped the `#.` blocks, an ad-hoc script
        put them back, and one of the seven it inserted carried the previous
        entry's block *and then* its own -- so a translator was told the
        string was about volume and mpv.conf as well as about language
        codes. Plausible enough to read past; one in seven wrong.

        This shape specifically, rather than a general "is the context
        right", because it is what an insertion that fails to advance
        produces: a writer that emits the accumulated buffer instead of the
        current entry's. Measured over both trees -- one hit where the
        defect is, none at the base, and no false positives across 1,119
        contexted entries -- so it costs nothing and catches the one thing
        `regen_pot.sh` and the rules above cannot.
        """
        offenders = _carried_context(parse(POT))

        self.assertEqual(
            offenders, [],
            "these entries begin with the complete translator context of the "
            "entry above them, so they describe the wrong string first: %r. "
            "That is what a script restoring '#.' blocks produces when it "
            "fails to advance -- check each against the source site, do not "
            "just delete the duplicated half." % offenders)

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


class TheCarriedContextRuleFiresTest(unittest.TestCase):
    """The rule above, on input that is not the live template.

    `base.pot` is clean today, so the case that reads it passes whether the
    rule works or has quietly stopped matching -- a renamed `comments`
    attribute, a parser that stopped collecting `#.` lines. This is the half
    that can tell those apart, and it is cheap because the rule takes blocks
    rather than a path.

    The planted pair is the real one, shortened: the second entry carries the
    first's whole comment and then its own.
    """

    class _Block:
        is_header = False

        def __init__(self, msgid, comments):
            self.msgid, self.comments = msgid, comments

    VOLUME = ["Explanation under the Remember Playback Volume checkbox.",
              "mpv.conf is a filename and is not translated."]
    LANGUAGE = ["Explanation under the Language Filter list of codes."]

    def test_it_finds_a_carried_block(self):
        blocks = [self._Block("volume", list(self.VOLUME)),
                  self._Block("language", self.VOLUME + self.LANGUAGE)]

        self.assertEqual(["language"], _carried_context(blocks))

    def test_it_leaves_two_ordinary_entries_alone(self):
        blocks = [self._Block("volume", list(self.VOLUME)),
                  self._Block("language", list(self.LANGUAGE))]

        self.assertEqual([], _carried_context(blocks))

    def test_two_entries_sharing_one_context_exactly_are_not_flagged(self):
        """Deliberate, and the reason the rule asks for MORE than the one
        above rather than for a prefix.

        A msgid genuinely shared by two settings would legitimately carry the
        same sentence twice, and `base.pot` has such a pair already -- out of
        this round's range, and not clearly a defect at all. Flagging it
        would have made the rule's first act a false positive.
        """
        blocks = [self._Block("a", list(self.VOLUME)),
                  self._Block("b", list(self.VOLUME))]

        self.assertEqual([], _carried_context(blocks))

    def test_an_entry_with_no_context_breaks_the_chain(self):
        """Or an uncommented entry would carry the one before it forward and
        flag whatever came next. `test_every_entry_has_context` is what makes
        this state rare, not impossible -- the two rules run on the same file
        and the other one failing must not make this one lie."""
        blocks = [self._Block("volume", list(self.VOLUME)),
                  self._Block("bare", []),
                  self._Block("language", self.VOLUME + self.LANGUAGE)]

        self.assertEqual([], _carried_context(blocks))


if __name__ == "__main__":
    unittest.main()
