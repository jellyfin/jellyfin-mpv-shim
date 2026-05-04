"""Single source of truth for command-line arguments.

Other modules read parsed values via get_args(); they should never scan sys.argv
directly. Adding a new flag means editing this file and nothing else.
"""

import argparse

from .constants import APP_NAME, CLIENT_VERSION

_args = None


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
        "--mpv-loglevel",
        dest="mpv_loglevel",
        choices=("fatal", "error", "warn", "info", "debug"),
        default=None,
        help="override mpv_log_level for this run",
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
        "command",
        nargs="*",
        choices=("add", "clear"),
        help="add: prompt to add a server; clear: remove all stored credentials",
    )
    return parser


def get_args() -> argparse.Namespace:
    """Parse argv on first call, cache the result thereafter."""
    global _args
    if _args is None:
        _args = _build_parser().parse_args()
    return _args
