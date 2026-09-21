"""Being asked to play an item with no media source must not raise.

A **virtual episode** is one the server lists out of the series metadata with
no file behind it, and PlaybackInfo answers for it with `MediaSources: []`.
`get_best_media_source` returns None for that, and the next statement but one
dereferenced it -- so what reached `play()`'s caller was a `TypeError`, and on
the default configuration: the dereference is the segment lookup, which runs
whenever any segment type is not "off", and `segment_intro` and `segment_outro`
both default to "ask".

**The guard for exactly this already existed and could not fire.** `play()`
answers an unplayable item at its "no URL found" branch -- log, revoke the auth
header, return -- and the dereference happened earlier, inside
`get_playback_url`. That makes this the second instance in one review round of
a guard written against a failure it sits downstream of (the other is
`set_osd_settings`, defeated because python-mpv shadowed instead of raising),
which is worth naming: **a guard placed after the thing it guards against is
indistinguishable, in review, from one placed before it.** Nothing in the diff
says which, and both read as defensive.

So the fix is upstream of the existing guard rather than a second guard: two
places deciding what "unplayable" means is how they come to disagree.

The filters that stop a virtual episode being offered or queued are elsewhere.
This is the backstop for what they cannot cover -- a cast from another client,
which arrives as an id this shim never filtered, and a server whose state
changed between the listing and the press.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import logging
import sys
import unittest
from types import SimpleNamespace as NS

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

SERVER = "https://jellyfin.example.invalid"
TOKEN = "SYNTHETIC_TEST_TOKEN"


def _client(item, sources):
    return NS(
        config=NS(data={"auth.server": SERVER, "auth.token": TOKEN,
                        "auth.server-id": "sid"}),
        http=NS(_get_authenication_header=lambda:
                'MediaBrowser Token="%s"' % TOKEN),
        jellyfin=NS(
            get_item=lambda _id, **kw: item,
            # What the server answers for an episode with no file: the
            # request succeeds and the list is empty. An error would be a
            # different path entirely, and this is the one that shipped.
            get_play_info=lambda *a, **kw: {"MediaSources": list(sources)},
        ),
    )


class _Parent:
    def __init__(self, item, sources):
        self.client = _client(item, sources)
        self.is_local = True
        self.item = item
        self.queue = []
        self.has_next = False


class _FakePlayer:
    def __init__(self):
        self.http_header_fields = ["stale: from the previous item"]


def _video(sources=()):
    from jellyfin_mpv_shim.media import Video

    item = {
        "Type": "Episode", "Name": "An episode nobody has",
        "Id": "item1", "SeriesName": "A Show",
        # What the server marks a virtual episode with. Carried so the
        # fixture is the real DTO shape rather than only the empty list.
        "LocationType": "Virtual",
        "MediaSources": list(sources),
    }
    video = Video("item1", _Parent(item, sources))
    video.item = item
    return video


def _pm():
    """A PlayerManager with only what `play()` touches."""
    from jellyfin_mpv_shim.player import PlayerManager

    pm = PlayerManager.__new__(PlayerManager)
    pm._player = _FakePlayer()
    pm._mpv_alive = True
    pm.should_send_timeline = False
    pm.start_time = 0.0
    pm._load_cancelled = False
    pm._start_in_progress = False
    pm._track_memory = None
    pm.menu = None
    pm.played = []
    pm._play_media = lambda video, url, *a, **kw: pm.played.append(url)
    return pm


class AnItemWithNoMediaSourceTest(unittest.TestCase):
    def setUp(self):
        from jellyfin_mpv_shim.conf import settings

        # **Left at the defaults, deliberately.** The dereference is the
        # segment lookup, so a test that turned segments off would exercise a
        # path where the bug cannot happen and pass against the old code.
        self.assertTrue(
            settings.segment_intro != "off" or settings.segment_outro != "off",
            "no segment type is on by default any more, so this test no "
            "longer reaches the statement it was written for")

    def test_play_returns_instead_of_raising(self):
        pm, video = _pm(), _video()

        with self.assertLogs("player", level=logging.ERROR) as logs:
            pm.play(video)      # used to be a TypeError out of play()

        self.assertEqual([], pm.played, "an unplayable item reached mpv")
        self.assertTrue(
            any("no URL found" in line for line in logs.output),
            "the existing unplayable-item guard did not run: %r" % logs.output)

    def test_the_auth_header_does_not_outlive_the_attempt(self):
        """The other half of that guard, and the reason it is the one to
        reach: `http-header-fields` is a GLOBAL mpv option, so a header
        installed for an item that never plays would sit there pointing at
        whatever plays next."""
        pm, video = _pm(), _video()

        with self.assertLogs("player", level=logging.ERROR):
            pm.play(video)

        self.assertFalse(
            any(TOKEN in header
                for header in (pm._player.http_header_fields or [])),
            "the access token was left on the player")

    def test_an_item_with_a_source_still_plays(self):
        """The control. A guard that declined everything would pass the two
        tests above and ship a client that plays nothing."""
        pm = _pm()
        video = _video([{
            "Id": "src", "MediaStreams": [],
            "SupportsDirectPlay": True, "SupportsDirectStream": True,
            "Path": SERVER + "/stream.mkv", "Container": "mkv",
            "RunTimeTicks": 100 * 10000000,
        }])

        pm.play(video)

        self.assertEqual(1, len(pm.played), "a playable item did not play")
        self.assertTrue(pm.played[0], "the url was empty")


if __name__ == "__main__":
    unittest.main()
