"""Redacting secrets from the log must not break the log.

``CustomFormatter`` maps ``sanitize`` over ``record.args`` before Python
runs ``msg % args``. That makes ``sanitize`` part of the formatting
contract, not just a filter: an argument that comes back as a different
type can make the interpolation raise, and a record that raises during
formatting is *lost* — replaced by a "--- Logging error ---" traceback on
stderr. Which is worse than the message it swallowed.
"""

import logging
import sys
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim import log_utils  # noqa: E402


def render(msg, *args, sanitizing=True):
    """Format one record exactly as the app's handler would."""
    record = logging.LogRecord("t", logging.INFO, "f.py", 1, msg, args, None)
    return log_utils.CustomFormatter(force_sanitize=sanitizing).format(record)


class TypesSurviveSanitization(unittest.TestCase):
    def test_a_bool_still_formats_as_a_number(self):
        """The regression. bool is an int subclass and the natural thing to
        log through %d ("alive=%d"); the type check was `type(x) in (int,
        float)`, which bool fails, so it was stringified and every such
        record died in the formatter."""
        out = render("alive=%d video=%d", True, False)
        self.assertTrue(out.endswith("alive=1 video=0"), out)

    def test_ints_and_floats_still_format(self):
        out = render("%d items in %.1fs", 7, 1.25)
        self.assertTrue(out.endswith("7 items in 1.2s"), out)

    def test_an_object_arg_keeps_its_type_when_there_is_nothing_to_redact(self):
        class Thing:
            def __str__(self):
                return "thing"

        thing = Thing()
        record = logging.LogRecord("t", logging.INFO, "f.py", 1,
                                   "%s", (thing,), None)
        log_utils.CustomFormatter(force_sanitize=True).format(record)
        self.assertIs(record.args[0], thing,
                      "an argument with no secret in it was replaced anyway")


class SecretsAreStillRemoved(unittest.TestCase):
    """The reason any of this exists. These must not regress in the name of
    keeping types."""

    def test_a_token_in_the_message(self):
        out = render("GET /x?api_key=deadbeef0123")
        self.assertNotIn("deadbeef", out)
        self.assertIn("api_key=REDACTED", out)

    def test_a_token_in_a_string_argument(self):
        out = render("headers %s", "'AccessToken': 'abc123def'")
        self.assertNotIn("abc123def", out)

    def test_a_token_inside_a_dict_argument(self):
        """The case that makes non-strings worth stringifying at all: one of
        the patterns matches a header dict's repr. Keeping the argument's
        type must not be allowed to smuggle a token through."""
        out = render("headers %s", {"X-MediaBrowser-Token": "cafebabe1234"})
        self.assertNotIn("cafebabe1234", out)

    def test_sanitization_off_leaves_the_message_alone(self):
        out = render("GET /x?api_key=deadbeef0123", sanitizing=False)
        self.assertIn("deadbeef0123", out)


class TheWindowTraceSurvivesIt(unittest.TestCase):
    """The trace that found this: every window transition logs booleans
    through %d, so a sanitizing formatter turned the whole lifecycle log
    into logging-internal tracebacks."""

    def test_the_browse_transition_line_formats(self):
        out = render(
            "browse=%s (alive=%d video=%d loading=%d mpvtk=%d bg=%d) <- %s",
            "on", True, False, False, True, False, "mod.fn:12")
        self.assertIn("browse=on (alive=1 video=0 loading=0 mpvtk=1 bg=0)", out)


if __name__ == "__main__":
    unittest.main()
