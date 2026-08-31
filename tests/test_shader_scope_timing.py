"""A library-scope shader profile must be resolved before the decoder starts.

`hwdec` is read when the decoder is initialised, which `player.py` says in so
many words at the write site: "Setting it afterwards would apply to the file
after this one." `_forced_hwdec()` reads `profiles.forced_hwdec`, which is set
by `load_profile`.

`_warm_library_later` deferred the library lookup to the action thread, and
justified it on the grounds that "shader profiles are applied to a *running*
mpv and switching one mid-playback is what the menu does". That is true of
`glsl-shaders` and false of `hwdec`: the deferred profile landed after
`self._player.play(url)`, so the first item from a library played
software-decoded with the profile's filter disabled, and only the *second* was
right -- which reads as warm-up rather than a bug.

The lookup itself was deferred for a real reason: `apply_for_item` runs inside
`_play_media`, which holds the player lock for the whole of a start, and a
request there would block `run_action`'s non-blocking fast path. `play()` does
not hold that lock -- it is where PlaybackInfo is already fetched -- so that is
where the lookup belongs.
"""

import sys
import unittest
from types import SimpleNamespace as NS

sys.argv = [sys.argv[0]]


class _Profiles:
    """Stands in for ProfileManager, modelling the one thing that matters:
    `forced_hwdec` is unknown until the library id has been resolved."""

    def __init__(self, has_library_override=True):
        self._has = has_library_override
        self.resolved = False
        self.warmed = 0

    @property
    def forced_hwdec(self):
        # A library-scope profile naming a decoder -- knowable only once the
        # library is known, which is the whole point.
        return "d3d11va" if self.resolved else None

    wants_copy_hwdec = False

    def warm_library_scope(self, item, client=None):
        self.warmed += 1
        if self._has:
            self.resolved = True

    def apply_for_item(self, item, client=None):
        return True

    def suspend_for_still(self):
        pass


def _pm(profiles):
    from jellyfin_mpv_shim.player import PlayerManager

    pm = PlayerManager.__new__(PlayerManager)
    pm._player = NS(http_header_fields=[])
    pm._mpv_alive = True
    pm.should_send_timeline = False
    pm.start_time = 0.0
    pm._load_cancelled = False
    pm._start_in_progress = False
    pm._track_memory = None
    pm.menu = NS(profile_manager=profiles)
    pm.seen = []
    pm._play_media = lambda video, url, *a, **kw: pm.seen.append(
        pm._forced_hwdec())
    return pm


def _video():
    return NS(
        item={"Id": "i1", "SeriesId": "s1"},
        client=NS(config=NS(data={"auth.server": "https://s.invalid",
                                  "auth.token": "t"}),
                  http=NS(_get_authenication_header=lambda: 'Token="t"')),
        auth_via_header=False,
        explicit_tracks=True,
        aid=None, sid=None,
        get_playback_url=lambda: "https://s.invalid/Videos/1/stream",
        resolve_tracks_for_negotiation=lambda: None,
    )


class LibraryScopeIsKnownBeforeTheDecoderTest(unittest.TestCase):

    def test_the_profile_s_decoder_is_known_before_play_media(self):
        profiles = _Profiles()
        pm = _pm(profiles)
        pm.play(_video())
        # _forced_hwdec answers "has a profile named a decoder", not which one.
        self.assertEqual(
            pm.seen, [True],
            "_play_media ran without knowing the library's profile, so hwdec "
            "was written from the setting and the deferred profile landed "
            "after the decoder had already started")

    def test_it_asks_once_per_start(self):
        profiles = _Profiles()
        pm = _pm(profiles)
        pm.play(_video())
        self.assertEqual(profiles.warmed, 1)

    def test_a_manager_without_the_hook_does_not_break_playback(self):
        """Optional dependencies and older/stub managers: the play path must
        not require the method to exist."""
        profiles = NS(forced_hwdec=None, wants_copy_hwdec=False,
                      apply_for_item=lambda *a, **k: True)
        pm = _pm(profiles)
        pm.play(_video())
        self.assertEqual(pm.seen, [False])

    def test_no_menu_at_all_is_fine(self):
        """CLI mode has no menu, so no profile manager."""
        pm = _pm(None)
        pm.menu = None
        pm.play(_video())
        self.assertEqual(pm.seen, [False])


if __name__ == "__main__":
    unittest.main()
