"""Subtitle sidecars load without holding up the player.

The server answers an embedded sidecar only after extracting every subtitle
stream of the file -- one pass over the whole file, which on a large remote
remux takes minutes. A synchronous `sub_add` held a playback start paused on
its first frame, under the player lock, for all of that (libmpv), or raised
`TimeoutError` out of `play()` after 120 s (python-mpv-jsonipc). So the add is
`command_async`, and what happens when it lands is decided then, on the
action thread, against what the user wants *by then*.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import threading
import unittest

sys.argv = [sys.argv[0]]      # importing player reaches args.get_args()

from jellyfin_mpv_shim.media import Video  # noqa: E402
from jellyfin_mpv_shim.player import PlayerManager  # noqa: E402

URL_A = "http://srv/Videos/x/s/Subtitles/0/0/Stream.pgssub"
URL_B = "http://srv/Videos/x/s/Subtitles/1/0/Stream.srt"


class FakeMpv:
    """What the sidecar path touches: `command_async`, `track_list`, and the
    two selections. A reply is delivered by the test, when it chooses --
    that interval is the whole subject of this file."""

    def __init__(self):
        self.adds = []            # (url, flag, callback), in call order
        self.track_list = [{"type": "video", "id": 1}]
        self.sub = None
        self.secondary_sid = None
        self.writes = []          # ("sub", value) / ("secondary_sid", value)

    def __setattr__(self, name, value):
        if name in ("sub", "secondary_sid") and "writes" in self.__dict__:
            self.writes.append((name, value))
        object.__setattr__(self, name, value)

    def command_async(self, name, *args, callback=None):
        assert name == "sub-add", name
        self.adds.append((args[0], args[1] if len(args) > 1 else None,
                          callback))

    def sub_add(self, *args):
        raise AssertionError("a synchronous sub_add blocks the player "
                             "until the server answers")

    def answer(self, index=-1, error=None):
        """mpv finishes the add at `index` of `self.adds`."""
        url, _flag, callback = self.adds[index]
        if error is None:
            self.track_list.append({
                "type": "sub", "id": 2 + sum(
                    t["type"] == "sub" for t in self.track_list),
                "external": True, "external-filename": url})
        callback(error, None)


def _video(sid=None, secondary=None):
    v = Video.__new__(Video)
    v.aid, v.sid = None, sid
    v.secondary_sid = secondary
    v.is_transcode = False
    v.audio_seq = {}
    v.subtitle_seq = {5: 3}          # one embedded subtitle, index 5
    v.subtitle_url = {0: URL_A, 1: URL_B}
    return v


class _Base(unittest.TestCase):

    def setUp(self):
        pm = PlayerManager.__new__(PlayerManager)
        pm._lock = threading.RLock()
        pm._player = self.mpv = FakeMpv()
        pm.external_subtitles = {}
        pm._pending_subs = {}
        pm._play_epoch = 1
        self.queued = []
        # The action thread, held still: a task runs when the test drains.
        pm.put_task = lambda func, *args: self.queued.append((func, args))
        self.pm = pm

    def play(self, video):
        self.pm._video = video
        self.pm.configure_streams()

    def drain(self):
        while self.queued:
            func, args = self.queued.pop(0)
            func(*args)


class TheStartDoesNotWaitTest(_Base):

    def test_configure_streams_returns_before_the_sidecar_arrives(self):
        self.play(_video(sid=0))
        self.assertEqual([(u, f) for u, f, _ in self.mpv.adds],
                         [(URL_A, "auto")])
        # Nothing to show yet -- and not mpv's own default track either,
        # which would be a subtitle the user did not pick.
        self.assertEqual(self.mpv.sub, "no")

    def test_it_is_selected_and_remembered_when_it_lands(self):
        self.play(_video(sid=0))
        self.mpv.answer()
        self.drain()
        self.assertEqual(self.mpv.sub, 2)
        self.assertEqual(self.pm.external_subtitles, {0: 2})

    def test_the_reply_itself_does_no_player_work(self):
        """mpv delivers it on its own event thread; player work there races
        the player lock. It only queues a task for the action thread."""
        self.play(_video(sid=0))
        before = list(self.mpv.writes)
        self.mpv.answer()
        self.assertEqual(self.mpv.writes, before)
        self.assertEqual(len(self.queued), 1)


class WhatTheUserWantsByThenTest(_Base):

    def test_a_subtitle_picked_meanwhile_is_not_overridden(self):
        video = _video(sid=0)
        self.play(video)
        video.sid = 5                       # switched to the embedded one
        self.pm.configure_streams()
        self.mpv.answer(0)
        self.drain()
        self.assertEqual(self.mpv.sub, 3)
        self.assertEqual(self.pm.external_subtitles, {0: 2},
                         "loaded is loaded: going back must not refetch it")

    def test_subtitles_switched_off_meanwhile_stay_off(self):
        video = _video(sid=0)
        self.play(video)
        video.sid = -1
        self.pm.configure_streams()
        self.mpv.answer(0)
        self.drain()
        self.assertEqual(self.mpv.sub, "no")

    def test_a_reply_from_an_earlier_play_touches_nothing(self):
        self.play(_video(sid=0))
        self.pm._play_epoch += 1            # the next item, or a restart
        self.pm.external_subtitles = {}
        self.pm._pending_subs = {}
        self.mpv.sub = "no"
        self.mpv.answer(0)
        self.drain()
        self.assertEqual(self.mpv.sub, "no")
        self.assertEqual(self.pm.external_subtitles, {})

    def test_picking_it_again_while_it_loads_adds_it_once(self):
        video = _video(sid=0)
        self.play(video)
        self.pm.configure_streams()
        self.pm.load_external_sub(0)
        self.assertEqual(len(self.mpv.adds), 1)
        self.mpv.answer(0)
        self.drain()
        self.assertEqual(self.mpv.sub, 2)

    def test_returning_to_a_loaded_sidecar_does_not_fetch_it_again(self):
        video = _video(sid=0)
        self.play(video)
        self.mpv.answer(0)
        self.drain()
        video.sid = 5
        self.pm.configure_streams()
        video.sid = 0
        self.pm.configure_streams()
        self.assertEqual(len(self.mpv.adds), 1)
        self.assertEqual(self.mpv.sub, 2)


class AFailedLoadTest(_Base):

    def test_it_leaves_the_selection_alone_and_can_be_retried(self):
        video = _video(sid=0)
        self.play(video)
        with self.assertLogs("player", "WARNING"):
            self.mpv.answer(0, error=SystemError("loading failed"))
            self.drain()
        self.assertEqual(self.mpv.sub, "no")
        self.assertEqual(self.pm.external_subtitles, {})
        self.pm.configure_streams()
        self.assertEqual(len(self.mpv.adds), 2, "a failure is not a cache")

    def test_an_add_that_cannot_even_be_sent_does_not_abort_the_start(self):
        def refuse(*args, **kwargs):
            raise SystemError("mpv refused the command")

        self.mpv.command_async = refuse
        with self.assertLogs("player", "WARNING"):
            self.play(_video(sid=0))
        self.assertEqual(self.pm._pending_subs, {})


class TheSecondarySubtitleTest(_Base):

    def test_a_secondary_sidecar_is_applied_when_it_lands(self):
        self.play(_video(sid=5, secondary=1))
        self.assertEqual([(u, f) for u, f, _ in self.mpv.adds],
                         [(URL_B, "auto")])
        self.assertEqual(self.mpv.secondary_sid, "no")
        self.mpv.answer(0)
        self.drain()
        self.assertEqual(self.mpv.secondary_sid, 2)
        self.assertEqual(self.mpv.sub, 3, "the primary is untouched")

    def test_a_sidecar_loading_as_the_primary_is_not_added_again(self):
        """Picked as the primary, then moved to the secondary while it was
        still loading: one add, and it lands where it is wanted now."""
        video = _video(sid=0)
        self.play(video)
        video.sid, video.secondary_sid = 5, 0
        self.pm.configure_streams()
        self.assertEqual(len(self.mpv.adds), 1)
        self.mpv.answer(0)
        self.drain()
        self.assertEqual(self.mpv.secondary_sid, 2)
        self.assertEqual(self.mpv.sub, 3)


if __name__ == "__main__":
    unittest.main()
