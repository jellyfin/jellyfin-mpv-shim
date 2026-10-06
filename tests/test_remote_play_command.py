"""The websocket "Play" command, before anything is built from it.

A cast arrives as the server's expansion of what was cast: a folder (an
album, a photo album) comes as its children. Two things follow, and both
were found in the 3.1.0 hand test [iw, 2026-10-04]:

- the list can be empty, or the start index past its end, and indexing it
  raised inside the websocket callback -- a cast that did nothing but log
  "list index out of range";
- several ids are a Play All, which in the browser runs a photo queue as a
  slideshow, so a cast photo album must not open paused on its first photo.
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
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # conffile reaches args.get_args() on import

from jellyfin_mpv_shim import event_handler  # noqa: E402


def _play(arguments):
    """Run the bound Play handler with nothing playing; return what reached
    start_playback, as (ids, kwargs) per call."""
    started = []
    with mock.patch.object(event_handler, "start_playback",
                           lambda client, ids, **kw: started.append(
                               (list(ids), kw))), \
            mock.patch.object(event_handler.playerManager, "has_video",
                              lambda: False):
        event_handler.EventHandler().play_media(
            object(), "Play", dict({"PlayCommand": "PlayNow"}, **arguments))
    return started


class DeclinedPlayTest(unittest.TestCase):
    def test_an_empty_list_is_declined_with_a_warning(self):
        for ids in ([], None):
            with self.subTest(ids=ids), self.assertLogs(
                    event_handler.log, "WARNING") as logs:
                self.assertEqual(_play({"ItemIds": ids}), [])
            self.assertIn("Declined a remote Play", logs.output[0])

    def test_a_start_index_past_the_end_is_declined(self):
        for index in (3, 9, -1):
            with self.subTest(index=index), self.assertLogs(
                    event_handler.log, "WARNING"):
                self.assertEqual(
                    _play({"ItemIds": ["a", "b", "c"], "StartIndex": index}),
                    [])

    def test_a_valid_index_still_plays_from_it(self):
        (call,) = _play({"ItemIds": ["a", "b", "c"], "StartIndex": 2})
        self.assertEqual(call[0], ["a", "b", "c"])
        self.assertEqual(call[1]["start_index"], 2)


class CastQueueIsAPlayAllTest(unittest.TestCase):
    def test_several_items_run_and_one_opens_paused(self):
        """Several: a photo album, run as the browser's Play All runs it.
        One: "show me this picture", as a click on a single photo is."""
        for ids, paused in ((["p1", "p2", "p3"], False), (["p1"], True)):
            with self.subTest(ids=ids):
                (call,) = _play({"ItemIds": ids})
                self.assertIs(call[1]["pause_stills"], paused)


if __name__ == "__main__":
    unittest.main()
