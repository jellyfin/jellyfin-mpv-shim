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


class TokenValuesAreRedacted(unittest.TestCase):
    """`Token="..."` reaches the log from strings the shim did not build:
    mpv echoes our stream URL back through its own log handler, and the
    Authorization header spells the token this way. The four original
    patterns all assume a hex value we generated, so a token that is not
    hex used to print in full."""

    def test_the_authorization_header_spelling(self):
        out = render('auth %s', 'MediaBrowser Token="abc123XYZ-not_hex"')
        self.assertNotIn("abc123XYZ-not_hex", out)
        self.assertIn('Token="REDACTED"', out)

    def test_the_escaped_spelling_mpv_logs(self):
        """A real backslash, not a repr: this is one of mpv's own quoted log
        lines carrying our URL, which is where it was found."""
        out = render('%s', r'loadfile url="http://h/s?Token=\"abc123\""')
        self.assertNotIn("abc123", out)
        self.assertIn(r'Token=\"REDACTED\"', out)

    def test_a_non_hex_token_is_still_caught(self):
        """The point of not reusing [a-f0-9]: this is a value a server hands
        us, so the charset is not ours to assume."""
        out = render('%s', 'AccessToken="ZZZ-not-hex-at-all"')
        self.assertNotIn("ZZZ-not-hex-at-all", out)

    def test_ordinary_text_is_untouched(self):
        self.assertIn("nothing to see", render("%s", "nothing to see"))


class MpvNoiseFilter(unittest.TestCase):
    """`debug` is the level someone turns on to read one specific thing, and
    the renderer's per-frame traffic buries it thousands of lines deep. The
    filter drops that; `noise` is how you ask for it back."""

    def setUp(self):
        from jellyfin_mpv_shim import player
        from jellyfin_mpv_shim.conf import settings
        self.player = player
        self.settings = settings
        self.addCleanup(setattr, settings, "mpv_log_level",
                        settings.mpv_log_level)

    SCENE = ('Run command: script-message, flags=64, '
             'args=[args="mpvtk-scene", args="{...}"]')
    METRICS = ('Run command: script-message, flags=64, '
               'args=[args="mpvtk-metrics", args=""]')

    def test_the_three_noisy_shapes_are_noise(self):
        self.assertTrue(self.player._is_mpv_noise("cplayer", self.SCENE))
        self.assertTrue(self.player._is_mpv_noise("cplayer", self.METRICS))
        self.assertTrue(self.player._is_mpv_noise("vo/gpu-next", "anything"))

    def test_other_script_messages_are_not(self):
        """Only our own per-frame chatter. A trickplay or skip message is
        rare and worth seeing."""
        other = ('Run command: script-message, flags=64, '
                 'args=[args="mpvtk-hud-skip", args=""]')
        self.assertFalse(self.player._is_mpv_noise("cplayer", other))
        self.assertFalse(self.player._is_mpv_noise("cplayer", "seek done"))
        self.assertFalse(self.player._is_mpv_noise("mkv", "EOF reached."))

    def _emit(self, level, prefix, text):
        seen = []
        real = self.player.mpv_log.debug
        self.player.mpv_log.debug = lambda m: seen.append(m)
        self.addCleanup(setattr, self.player.mpv_log, "debug", real)
        self.player.mpv_log_handler(level, prefix, text)
        return seen

    def test_debug_drops_it(self):
        self.settings.mpv_log_level = "debug"
        self.assertEqual(self._emit("debug", "cplayer", self.SCENE), [])

    def test_noise_keeps_it(self):
        self.settings.mpv_log_level = "noise"
        self.assertEqual(len(self._emit("debug", "cplayer", self.SCENE)), 1)

    def test_a_gpu_next_error_survives_the_filter(self):
        """Never applied above info: a real gpu-next failure has to reach
        the user, and _recent_mpv_errors is what a failed load reports."""
        self.settings.mpv_log_level = "debug"
        self.player.clear_mpv_errors()
        self.player.mpv_log_handler("error", "vo/gpu-next", "context init failed")
        self.assertIn("context init failed", self.player.last_mpv_error() or "")

    def test_noise_is_not_handed_to_mpv(self):
        """mpv has no such level; it would refuse to start."""
        self.assertEqual(self.player.mpv_loglevel_for("noise"), "debug")
        self.assertEqual(self.player.mpv_loglevel_for("debug"), "debug")
        self.assertEqual(self.player.mpv_loglevel_for("info"), "info")
