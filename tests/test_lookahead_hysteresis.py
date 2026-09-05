"""Lookahead hysteresis and the configurable per-pass cap (#661).

> Lookahead downloads can cause frequent small transfers, repeatedly
> spinning up HDDs.

So the property is *batching*: a series that is stocked must cost **no
transfers at all**, and one that has run down must be topped up in one go.
That is a multi-pass property by nature, which is also this repo's standing
rule — the bug shape here is state feeding back into the input that
produced it, and the old flat window had exactly that history.

All three settings default to None, meaning "behave exactly as before".
An install that never opens the settings screen must not change behaviour,
because the failure mode this is about is unwanted disk activity.
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

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.conf import settings              # noqa: E402
from jellyfin_mpv_shim.sync import auto                  # noqa: E402
from jellyfin_mpv_shim.sync.db import (STATUS_COMPLETE,  # noqa: E402
                                       STATUS_DOWNLOADING,
                                       STATUS_ERROR,
                                       STATUS_PENDING)


class SettingsCase(unittest.TestCase):
    KEYS = ("auto_download_lookahead", "auto_download_lookahead_min",
            "auto_download_lookahead_max", "auto_download_max_per_pass")

    def setUp(self):
        for key in self.KEYS:
            self.addCleanup(setattr, settings, key, getattr(settings, key))


class HysteresisResolutionTest(SettingsCase):

    def test_unset_is_the_old_behaviour(self):
        settings.auto_download_lookahead_min = None
        settings.auto_download_lookahead_max = None
        self.assertIsNone(auto.hysteresis())

    def test_both_set_is_a_window(self):
        settings.auto_download_lookahead_min = 2
        settings.auto_download_lookahead_max = 8
        self.assertEqual(auto.hysteresis(), (2, 8))

    def test_half_configured_declines_loudly(self):
        """Something a person can type into the JSON. Guessing the other
        half is worse than declining: "min 5" with no max could mean top up
        to 5 or to the old flat window, and those differ by however large
        the series is."""
        settings.auto_download_lookahead_min = 5
        settings.auto_download_lookahead_max = None
        with self.assertLogs("sync.auto", level="WARNING") as caught:
            self.assertIsNone(auto.hysteresis())
        # The *message* is asserted, not just that it declined: falling
        # through to int(None) also declines and also warns, but tells the
        # user their numbers are not numbers rather than that the pair has
        # to be set together — which is the one thing they need to know.
        self.assertIn("set together", "\n".join(caught.output))

    def test_max_below_min_is_the_same_class_of_typo(self):
        settings.auto_download_lookahead_min = 8
        settings.auto_download_lookahead_max = 2
        with self.assertLogs("sync.auto", level="WARNING"):
            self.assertIsNone(auto.hysteresis())

    def test_nonsense_values_decline(self):
        settings.auto_download_lookahead_min = "two"
        settings.auto_download_lookahead_max = 8
        with self.assertLogs("sync.auto", level="WARNING"):
            self.assertIsNone(auto.hysteresis())


class PerPassCapTest(SettingsCase):

    def test_the_default_is_the_number_the_issue_asked_to_keep(self):
        settings.auto_download_max_per_pass = None
        self.assertEqual(auto._per_pass(), 20)

    def test_it_is_configurable(self):
        settings.auto_download_max_per_pass = 5
        self.assertEqual(auto._per_pass(), 5)

    def test_zero_and_nonsense_fall_back_rather_than_stampeding(self):
        for value in (0, -3, "lots", None):
            settings.auto_download_max_per_pass = value
            self.assertEqual(auto._per_pass(), 20, repr(value))


class _Db:
    def __init__(self, rows):
        self.rows = rows

    def list(self, status=None, series_id=None):
        return [r for r in self.rows
                if (series_id is None or r.get("series_id") == series_id)
                and (status is None or r.get("status") == status)]


def _downloader(rows):
    d = auto.AutoDownloader.__new__(auto.AutoDownloader)
    d.manager = mock.Mock(db=_Db(rows))
    return d


def _row(series="s1", status=STATUS_COMPLETE, server="srv", item_id=None):
    return {"series_id": series, "status": status, "server_uuid": server,
            "item_id": item_id or "e0"}


class HeldIdsTest(unittest.TestCase):
    """What counts as "already have it" — and *which* episodes.

    The first version counted every held episode of the series rather than
    the ones in the window, so somebody holding twenty old episodes was
    above any minimum for ever and the series was never topped up again.
    Silently: no downloads, no error. The issue says "at least the minimum
    number of **upcoming** episodes", and upcoming is the word that does
    the work.
    """

    def test_queued_and_in_progress_count(self):
        """The issue names this: without it every pass re-queues the same
        episodes for as long as the first batch takes."""
        d = _downloader([_row(status=STATUS_COMPLETE, item_id="a"),
                         _row(status=STATUS_PENDING, item_id="b"),
                         _row(status=STATUS_DOWNLOADING, item_id="c")])
        self.assertEqual(d._held_ids("srv", "s1"), {"a", "b", "c"})

    def test_errors_do_not(self):
        d = _downloader([_row(status=STATUS_COMPLETE, item_id="a"),
                         _row(status=STATUS_ERROR, item_id="b")])
        self.assertEqual(d._held_ids("srv", "s1"), {"a"})

    def test_another_server_does_not(self):
        d = _downloader([_row(server="other", item_id="a"),
                         _row(server="srv", item_id="b")])
        self.assertEqual(d._held_ids("srv", "s1"), {"b"})

    def test_a_catalog_failure_is_unknown_rather_than_empty(self):
        """None, not an empty set: empty reads as "hold nothing", which
        tops the series up on every pass — the behaviour being removed."""
        d = _downloader([])
        d.manager.db.list = mock.Mock(side_effect=RuntimeError("locked"))
        self.assertIsNone(d._held_ids("srv", "s1"))


class LookaheadBatchingTest(SettingsCase):
    """The property the issue is actually about, over several passes."""

    def _planner(self, held_ids=(), episodes=12, old_ids=()):
        """A planner whose catalog holds `held_ids` from the window, plus
        `old_ids` which are episodes of the same series that are NOT in it.
        """
        settings.auto_download_lookahead = 2
        settings.auto_download_lookahead_min = 2
        settings.auto_download_lookahead_max = 8
        rows = [_row(item_id=i) for i in tuple(held_ids) + tuple(old_ids)]
        d = _downloader(rows)
        d._followed_series = lambda _s: {"s1"}
        d._watch_position = staticmethod(lambda _api, _sid: "anchor")
        api = mock.Mock()
        api.get_episodes.return_value = {
            "Items": [{"Id": "e%d" % i} for i in range(episodes)]}
        return d, api

    def test_a_stocked_series_queues_nothing(self):
        d, api = self._planner(held_ids=["e0", "e1", "e2", "e3"])
        self.assertEqual(d._lookahead(api, "srv"), [])

    def test_exactly_at_the_low_mark_is_still_stocked(self):
        d, api = self._planner(held_ids=["e0", "e1"])
        self.assertEqual(d._lookahead(api, "srv"), [])

    def test_old_episodes_outside_the_window_do_not_count(self):
        """The bug: twenty held episodes of a series with none of them
        upcoming used to read as "stocked", and the series was never topped
        up again — with nothing said about it."""
        d, api = self._planner(held_ids=[],
                               old_ids=["z%d" % i for i in range(20)])
        self.assertEqual(len(d._lookahead(api, "srv")), 8)

    def test_below_it_tops_up_to_the_maximum_in_one_go(self):
        """"With minimum 2 and maximum 8, dropping to one episode queues up
        to seven more" — one batch, not one episode. `fill` skips the ones
        already held, so the whole window is handed over."""
        d, api = self._planner(held_ids=["e0"])
        out = d._lookahead(api, "srv")
        self.assertEqual(len(out), 8)
        self.assertEqual(api.get_episodes.call_args.kwargs["limit"], 8)

    def test_with_no_hysteresis_it_is_the_flat_window(self):
        settings.auto_download_lookahead_min = None
        settings.auto_download_lookahead_max = None
        settings.auto_download_lookahead = 2
        d = _downloader([_row(item_id="e%d" % i) for i in range(4)])
        d._followed_series = lambda _s: {"s1"}
        d._watch_position = staticmethod(lambda _api, _sid: "anchor")
        api = mock.Mock()
        api.get_episodes.return_value = {
            "Items": [{"Id": "e%d" % i} for i in range(12)]}
        # Stocked or not, the old behaviour asks every pass.
        self.assertEqual(len(d._lookahead(api, "srv")), 2)

    def test_it_settles_rather_than_walking(self):
        """Three passes with the catalog growing as the first pass's
        downloads land. The second and third must queue nothing — the old
        flat window's failure was re-queueing every pass."""
        asked = []
        held = ["e0"]

        for _ in range(3):
            d, api = self._planner(held_ids=list(held))
            out = d._lookahead(api, "srv")
            asked.append(len(out))
            held = [i["Id"] for i in out] or held

        self.assertEqual(asked, [8, 0, 0])


if __name__ == "__main__":
    unittest.main()


class NoExtraWorkWhenUnconfiguredTest(SettingsCase):
    """The advanced settings must cost nothing when they are not set.

    The lookahead has always made one `get_episodes` per followed series per
    pass; hysteresis must not add a second, and the catalog read it needs
    must not happen at all when the window is unconfigured.
    """

    def _run(self):
        d = _downloader([_row(item_id="e0")])
        d._followed_series = lambda _s: {"s1"}
        d._watch_position = staticmethod(lambda _api, _sid: "anchor")
        d.manager.db.list = mock.Mock(return_value=[])
        api = mock.Mock()
        api.get_episodes.return_value = {
            "Items": [{"Id": "e%d" % i} for i in range(12)]}
        d._lookahead(api, "srv")
        return api, d.manager.db.list

    def test_unconfigured_reads_no_catalog_rows(self):
        settings.auto_download_lookahead = 2
        settings.auto_download_lookahead_min = None
        settings.auto_download_lookahead_max = None
        api, db_list = self._run()
        self.assertEqual(api.get_episodes.call_count, 1)
        db_list.assert_not_called()

    def test_configured_asks_the_server_exactly_as_often(self):
        settings.auto_download_lookahead = 2
        settings.auto_download_lookahead_min = 2
        settings.auto_download_lookahead_max = 8
        api, db_list = self._run()
        self.assertEqual(api.get_episodes.call_count, 1)
        db_list.assert_called_once()
