"""Special Features through the real play path (#759): an extra plays and
the server hears about it, and the local trailers play as one queue.

Split from test_special_features, which is contract tier: run there, this
class built a real player outside xvfb and put a window on the developer's
desktop. tests/test_e2e_registry.py keeps real players out of that tier.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402
from test_special_features import FILM  # noqa: E402


@_e2e.require_server_and_mpv
class PlayingAnExtraTest(_e2e.E2ETestCase):
    def _film_extras(self):
        film = self.session.find(FILM, library="Movies", item_type="Movie")
        source = self.session.library_source()
        self.addCleanup(source.stop)
        return film, source

    def test_an_extra_plays_and_the_server_hears_about_it(self):
        film, source = self._film_extras()
        extra = next(e for e in source.get_special_features(
            _e2e.SOURCE_UUID, film["Id"]) if e["ExtraType"] == "Featurette")
        self.session.reset_played(extra["Id"])
        self.addCleanup(self.session.reset_played, extra["Id"])
        before = self.session.user_data(extra["Id"]).get("LastPlayedDate")
        self.pm.play(_e2e.build_media(self.session, [extra["Id"]]).video,
                     is_initial_play=True)
        self.assertTrue(_e2e.wait_for(lambda: self.pm._player.duration),
                        "mpv never opened the extra")
        self.pump_until(lambda: (self.pm._player.playback_time or 0) > 1)
        self.pm.stop()
        self.assertTrue(
            _e2e.wait_for(lambda: self.session.user_data(extra["Id"]).get(
                "LastPlayedDate") not in (None, before), timeout=20),
            "the server recorded no playback of the extra")

    def test_the_local_trailers_play_as_one_queue(self):
        film, source = self._film_extras()
        ids = [t["Id"] for t in source.get_trailers(_e2e.SOURCE_UUID,
                                                    film["Id"])]
        self.assertEqual(len(ids), 2)
        self.addCleanup(self.session.reset_played, *ids)
        media = _e2e.build_media(self.session, ids)
        self.pm.play(media.video, is_initial_play=True)
        self.assertTrue(_e2e.wait_for(lambda: self.pm._player.duration),
                        "mpv never opened the first trailer")
        self.assertEqual(len(media.queue), 2)


if __name__ == "__main__":
    unittest.main()
