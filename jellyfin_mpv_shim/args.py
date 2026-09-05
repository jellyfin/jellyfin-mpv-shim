"""Single source of truth for command-line arguments.

Other modules read parsed values via get_args(); they should never scan sys.argv
directly. Adding a new flag means editing this file and nothing else.
"""

import argparse
from typing import NamedTuple, Optional

from .constants import APP_NAME, CLIENT_VERSION

_args = None

#: Accepted values for the positional command. Not passed to argparse as
#: choices= — see _build_parser.
COMMANDS = ("add", "clear", "stop")


class CliOverride(NamedTuple):
    """A flag that shadows a `conf.Settings` field for one run."""

    #: Attribute on the parsed args.
    dest: str
    #: The settings field it shadows.
    key: str
    #: How to spell it back on a command line.
    flag: str
    #: The negative spelling, for a `BooleanOptionalAction` flag.
    off: Optional[str] = None

    def spell(self, value):
        """This override, as command-line arguments."""
        if self.off is not None:
            return [self.flag if value else self.off]
        return [self.flag, str(value)]


#: The flags that OVERRIDE a config key, and the key each one shadows.
#:
#: One table because three places need the same pairing and they must not
#: drift: `mpv_shim.main` applies them after the config loads,
#: `settings_base` keeps them out of the file they would otherwise be
#: written to (#718), and `restart._durable_flags` re-passes them across a
#: self-restart -- except for the setting the restart is FOR, since
#: re-passing that one would overwrite what the user just saved.
#:
#: A flag added here is covered by all three by construction. A flag added
#: to only one of them was the bug.
CLI_OVERRIDES = (
    CliOverride("enable_gui", "enable_gui", "--gui", "--no-gui"),
    CliOverride("start_minimized", "start_minimized",
                "--minimized", "--no-minimized"),
    CliOverride("mpv_loglevel", "mpv_log_level", "--mpv-loglevel"),
    CliOverride("ui_scale", "ui_scale", "--scale"),
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Jellyfin MPV Shim - cast media from Jellyfin to MPV.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s " + CLIENT_VERSION,
    )
    parser.add_argument(
        "--config",
        metavar="DIR",
        help="use a custom configuration directory",
    )
    parser.add_argument(
        "--gui",
        dest="enable_gui",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable or disable the GUI (overrides config)",
    )
    parser.add_argument(
        "--minimized",
        dest="start_minimized",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="start with the library browser minimized to the system tray (overrides config)",
    )
    parser.add_argument(
        "--mpv-loglevel",
        dest="mpv_loglevel",
        choices=("fatal", "error", "warn", "info", "debug", "noise"),
        default=None,
        help="override mpv_log_level for this run",
    )
    parser.add_argument(
        "--scale",
        dest="ui_scale",
        metavar="FACTOR",
        type=float,
        default=None,
        help="scale the in-player UI by FACTOR for this run, e.g. 1.5 or 2 "
        "(overrides ui_scale; not saved to the config)",
    )
    parser.add_argument(
        "--reset-shaders",
        dest="reset_shaders",
        action="store_true",
        help="clear the remembered video playback profile and the graphics "
        "API override, then start normally (recovery for when a profile "
        "leaves MPV unable to show video)",
    )
    parser.add_argument(
        "--disable-hwdec",
        dest="disable_hwdec",
        action="store_true",
        help="force software decoding for this run, whatever the Hardware "
        "Decoding setting says (recovery for when hardware decoding stops "
        "the window opening at all); not saved to the config",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="surface debug-level application log messages (does not affect mpv verbosity)",
    )
    parser.add_argument(
        "--server",
        metavar="URL",
        help="server URL for non-interactive credential add (requires --username)",
    )
    parser.add_argument(
        "--username",
        metavar="NAME",
        help="username for --server",
    )
    parser.add_argument(
        "--password",
        metavar="PASS",
        default="",
        help="password for --server (NOTE: visible to other processes via ps)",
    )
    parser.add_argument(
        "--quick-connect",
        dest="quick_connect",
        action="store_true",
        help="log in via Jellyfin Quick Connect instead of a password "
        "(useful for SSO users); prompts for/uses only the server URL",
    )
    parser.add_argument(
        "command",
        nargs="*",
        # Deliberately no choices=: with nargs="*", argparse before 3.11
        # validates the *empty list* against choices when no positional is
        # given, so declaring them here makes running with no arguments at
        # all die with "invalid choice: []" on Python 3.9/3.10 — which is
        # every normal launch. metavar keeps the usage line unchanged;
        # get_args validates the values itself below.
        metavar="{%s}" % ",".join(COMMANDS),
        help="add: prompt to add a server; clear: remove all stored "
        "credentials; stop: shut down the copy already running against this "
        "configuration directory",
    )
    return parser


def get_args() -> argparse.Namespace:
    """Parse argv on first call, cache the result thereafter."""
    global _args
    if _args is None:
        parser = _build_parser()
        _args = parser.parse_args()
        # The choices= check argparse cannot do for us (see _build_parser).
        # parser.error exits 2 with a usage line, matching what argparse
        # would have printed for a bad positional.
        unknown = [c for c in (_args.command or []) if c not in COMMANDS]
        if unknown:
            parser.error("argument command: invalid choice: %s (choose from %s)"
                         % (", ".join(repr(c) for c in unknown),
                            ", ".join(repr(c) for c in COMMANDS)))
    return _args
