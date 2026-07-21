"""CLI argument parsing, and the argparse wart that made a bare launch fail.

`command` is a `nargs="*"` positional. Declaring `choices=` on it looks
right and works on Python 3.11+, but on 3.9/3.10 argparse validates the
*empty list* against those choices when no positional is supplied — so
`jellyfin-mpv-shim` with no arguments at all exited 2 with "argument
command: invalid choice: []". Every test module that imports the shim hit
it too, since they set `sys.argv = [sys.argv[0]]` on purpose.

The choices are enforced in get_args() instead. These pin both halves: a
bare launch works, and a bad command is still rejected.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim import args as args_mod  # noqa: E402


class ParserTest(unittest.TestCase):

    def _parse(self, argv):
        return args_mod._build_parser().parse_args(argv)

    def test_no_arguments_parses(self):
        """The regression: this exited 2 on Python 3.9/3.10."""
        self.assertEqual(self._parse([]).command, [])

    def test_a_command_still_parses(self):
        self.assertEqual(self._parse(["add"]).command, ["add"])
        self.assertEqual(self._parse(["clear"]).command, ["clear"])

    def test_the_parser_does_not_declare_choices(self):
        """Guards the fix itself: re-adding choices= here would restore the
        bare-launch crash on 3.9/3.10, where it is the parser that runs."""
        action = next(a for a in args_mod._build_parser()._actions
                      if a.dest == "command")
        self.assertIsNone(action.choices)

    def test_usage_still_advertises_the_commands(self):
        """Dropping choices= must not drop them from --help."""
        self.assertIn("add", args_mod._build_parser().format_usage())
        self.assertIn("clear", args_mod._build_parser().format_usage())


class GetArgsValidationTest(unittest.TestCase):
    """get_args() is where the choices are actually enforced now."""

    def setUp(self):
        self._saved_argv = sys.argv
        self._saved_cache = args_mod._args
        args_mod._args = None       # get_args caches; force a real parse

    def tearDown(self):
        sys.argv = self._saved_argv
        args_mod._args = self._saved_cache

    def _get(self, argv):
        sys.argv = ["jellyfin-mpv-shim"] + argv
        return args_mod.get_args()

    def test_no_arguments_is_accepted(self):
        self.assertEqual(self._get([]).command, [])

    def test_known_commands_are_accepted(self):
        self.assertEqual(self._get(["add"]).command, ["add"])

    def test_unknown_commands_are_rejected(self):
        with self.assertRaises(SystemExit) as caught:
            self._get(["bogus"])
        self.assertEqual(caught.exception.code, 2)

    def test_an_unknown_command_beside_a_known_one_is_rejected(self):
        with self.assertRaises(SystemExit):
            self._get(["add", "bogus"])

    def test_the_result_is_cached(self):
        first = self._get([])
        sys.argv = ["jellyfin-mpv-shim", "clear"]
        self.assertIs(args_mod.get_args(), first,
                      "get_args re-parsed instead of using its cache")


if __name__ == "__main__":
    unittest.main()
