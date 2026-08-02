"""The gateway is a flat facade over twelve mixins. That has one failure
mode, and this file is it.

``PlayerGateway`` composes ``PlaybackMixin``, ``TransportMixin``, … so callers
keep writing ``gateway.play_list(...)`` regardless of which file the method
lives in. The cost of flattening is that **two mixins can define the same
name and one silently wins** — by MRO order, which nobody reading either file
can see. The loser's method is simply never called, and because both are
plausible implementations of a plausible name, the symptom is a feature that
quietly does the wrong thing rather than an error.

That is not hypothetical in this codebase: the same hazard is why
``tests/test_mpvtk_browser_mixins.py`` exists for the browser's MRO, and why
``tests/test_page_contract.py`` refuses a route kind claimed twice.

The other half — that the split did not *lose* anything — is checked here
too, because a method that fell out of every mixin during the extraction
would also be silent: the attribute just stops existing, and only the code
path that calls it finds out.
"""

import ast
import inspect
import os
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser import gateway as gw  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway  # noqa: E402

PKG = os.path.dirname(inspect.getfile(gw))

#: Every method the gateway had as one class, immediately before the split.
#: Pinned as data rather than derived, so the check is against what shipped
#: rather than against whatever the code now happens to say.
#:
#: A name leaves this set only when the method is *deliberately* retired --
#: so far ``trickplay``, whose caller went away when the scrub preview moved
#: into renderer.lua (#618), and ``hud_sub_margin``, which raised the
#: subtitles clear of the bar and did so only for text tracks (#620).
#: Anything else disappearing is the bug this is here to catch.
BEFORE_SPLIT = {
    "add_server", "add_user", "any_client", "apply_audio_settings",
    "cancel_load", "chapters", "check_updates", "client_for",
    "collection_add", "collection_new", "collection_remove", "config_dir",
    "connect_and_rebuild", "copy_text", "delete_download", "delete_user",
    "download_activity", "download_enqueue", "download_estimate",
    "download_status", "downloaded_ids", "edit_apis", "get_aspect",
    "get_last_server", "get_queue", "get_queue_ids", "get_speed",
    "get_sync_groups", "has_downloads", "hud_action", "hud_key_opts",
    "hud_menu_state", "known_servers", "list_downloads",
    "list_servers", "list_users", "needs_unlock", "next",
    "offline_source", "on_browse_enter", "on_browse_leave",
    "on_downloads_changed", "on_minimize", "open_config_folder", "open_url",
    "play", "play_list", "playlist_add", "playlist_delete",
    "playlist_move_many", "playlist_new", "playlist_remove",
    "playlist_update", "prev", "quick_connect", "raise_window",
    "rebuild_source", "recent_logs", "refresh_playstate", "remove_server",
    "rename_user", "retry_connect", "retry_playback", "queue_items",
    "queue_remove", "queue_reorder", "seek", "seek_relative", "set_aspect",
    "set_favorite", "set_last_server", "set_paused", "set_repeat",
    "set_speed", "set_user_pin", "set_volume", "set_watched", "skip_to",
    "stop", "stop_for_close", "switch_user", "sync_active", "sync_join",
    "sync_leave", "sync_new", "sync_state", "toggle_favorite",
    "toggle_fullscreen", "toggle_mute", "toggle_night_mode", "toggle_pause",
    "toggle_stats", "unlock", "unlock_user", "use_hud",
    # private, but part of the contract other tests pin
    "_act", "_edit", "_queue_offline_watched", "_sync", "_ui_seek",
}


def _mixin_members():
    """{name: [defining mixin, ...]} read from source.

    From source rather than ``vars()`` so a name defined twice *within* one
    mixin — the other way to lose a method — is visible too.
    """
    out = {}
    for fn in sorted(os.listdir(PKG)):
        if not fn.endswith(".py") or fn in ("__init__.py", "deps.py"):
            continue
        path = os.path.join(PKG, fn)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef):
                continue
            for node in cls.body:
                names = []
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names = [node.name]
                elif isinstance(node, ast.Assign):
                    names = [t.id for t in node.targets
                             if isinstance(t, ast.Name)]
                for name in names:
                    out.setdefault(name, []).append("%s.%s" % (fn, cls.name))
    return out


class TestTheMixinsDoNotCollide(unittest.TestCase):
    def test_there_are_mixins_to_check(self):
        """A scan that silently matched nothing would pass forever."""
        members = _mixin_members()
        self.assertGreater(len(members), 90,
                           "the gateway mixin scan found almost nothing")

    def test_no_name_is_defined_twice(self):
        """The whole reason this file exists. One name, one definition —
        otherwise the MRO picks a winner and the loser is dead code that
        still reads as live."""
        dupes = {n: w for n, w in _mixin_members().items() if len(w) > 1}
        self.assertEqual(
            dupes, {},
            "These names are defined by more than one gateway mixin, so the "
            "MRO silently picks one:\n  "
            + "\n  ".join("%s <- %s" % (n, ", ".join(w))
                          for n, w in sorted(dupes.items())))

    def test_every_mixin_member_reaches_the_facade(self):
        """A mixin left out of the PlayerGateway bases would take its whole
        domain with it, and every call site would raise AttributeError only
        when that feature was used."""
        missing = sorted(n for n in _mixin_members()
                         if not hasattr(PlayerGateway, n))
        self.assertEqual(
            missing, [],
            "defined on a mixin but absent from PlayerGateway — is the mixin "
            "in the bases?:\n  " + "\n  ".join(missing))

    def test_every_base_is_used(self):
        """A base contributing nothing means its methods went somewhere else
        and the file is a husk."""
        idle = []
        for base in PlayerGateway.__mro__[1:]:
            if base is object:
                continue
            own = [n for n in vars(base) if not n.startswith("__")]
            if not own:
                idle.append(base.__name__)
        self.assertEqual(idle, [], "gateway mixins that define nothing: %s"
                         % idle)


class TestTheSplitLostNothing(unittest.TestCase):
    def test_every_pre_split_method_survives(self):
        """The split moved 102 methods between twelve files. One dropped on
        the floor would be silent until someone used that feature."""
        missing = sorted(n for n in BEFORE_SPLIT
                         if not hasattr(PlayerGateway, n))
        self.assertEqual(
            missing, [],
            "lost in the gateway split:\n  " + "\n  ".join(missing))

    def test_the_pin_is_not_stale(self):
        """If the gateway grows a method, that is fine — but the pin above
        should not silently drift into meaninglessness either. It may only
        be a subset of what exists."""
        actual = {n for n in dir(PlayerGateway) if not n.startswith("__")}
        self.assertGreaterEqual(
            len(actual), len(BEFORE_SPLIT),
            "the gateway has fewer members than it did before the split")
        self.assertEqual(
            BEFORE_SPLIT - actual, set(),
            "BEFORE_SPLIT names something the gateway no longer has")


if __name__ == "__main__":
    unittest.main()
