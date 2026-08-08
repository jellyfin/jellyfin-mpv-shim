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


def _row(series="s1", status=STATUS_COMPLETE, server="srv"):
    return {"series_id": series, "status": status, "server_uuid": server}


class UpcomingHeldTest(unittest.TestCase):
    """What counts as "already have it"."""

    def test_queued_and_in_progress_count(self):
        """The issue names this: without it every pass re-queues the same
        episodes for as long as the first batch takes, which is exactly the
        stampede hysteresis replaces."""
        d = _downloader([_row(status=STATUS_COMPLETE),
                         _row(status=STATUS_PENDING),
                         _row(status=STATUS_DOWNLOADING)])
        self.assertEqual(d._upcoming_held("srv", "s1"), 3)

    def test_errors_do_not(self):
        # Episodes we tried and failed to get. Treating a failure as stock
        # is how a series quietly stops being topped up.
        d = _downloader([_row(status=STATUS_COMPLETE),
                         _row(status=STATUS_ERROR)])
        self.assertEqual(d._upcoming_held("srv", "s1"), 1)

    def test_another_server_does_not(self):
        d = _downloader([_row(server="other"), _row(server="srv")])
        self.assertEqual(d._upcoming_held("srv", "s1"), 1)

    def test_a_catalog_failure_looks_stocked_rather_than_empty(self):
        """The safe direction: answering "none" would top the series up on
        every pass, which is the behaviour being removed."""
        d = _downloader([])
        d.manager.db.list = mock.Mock(side_effect=RuntimeError("locked"))
        self.assertGreater(d._upcoming_held("srv", "s1"), 1000)


class LookaheadBatchingTest(SettingsCase):
    """The property the issue is actually about, over several passes."""

    def _planner(self, held, episodes=12):
        settings.auto_download_lookahead = 2
        settings.auto_download_lookahead_min = 2
        settings.auto_download_lookahead_max = 8
        rows = [_row() for _ in range(held)]
        d = _downloader(rows)
        d._followed_series = lambda _s: {"s1"}
        d._watch_position = staticmethod(lambda _api, _sid: "anchor")
        api = mock.Mock()
        api.get_episodes.return_value = {
            "Items": [{"Id": "e%d" % i} for i in range(episodes)]}
        return d, api

    def test_a_stocked_series_costs_nothing(self):
        d, api = self._planner(held=4)
        self.assertEqual(d._lookahead(api, "srv"), [])
        api.get_episodes.assert_not_called()

    def test_exactly_at_the_low_mark_is_still_stocked(self):
        d, api = self._planner(held=2)
        self.assertEqual(d._lookahead(api, "srv"), [])

    def test_below_it_tops_up_to_the_maximum_in_one_go(self):
        """"With minimum 2 and maximum 8, dropping to one episode queues up
        to seven more" — one batch, not one episode."""
        d, api = self._planner(held=1)
        out = d._lookahead(api, "srv")
        self.assertEqual(len(out), 8)
        self.assertEqual(api.get_episodes.call_args.kwargs["limit"], 8)

    def test_with_no_hysteresis_it_is_the_flat_window(self):
        settings.auto_download_lookahead_min = None
        settings.auto_download_lookahead_max = None
        settings.auto_download_lookahead = 2
        d = _downloader([_row() for _ in range(4)])
        d._followed_series = lambda _s: {"s1"}
        d._watch_position = staticmethod(lambda _api, _sid: "anchor")
        api = mock.Mock()
        api.get_episodes.return_value = {
            "Items": [{"Id": "e%d" % i} for i in range(12)]}
        # Stocked or not, the old behaviour asks every pass.
        self.assertEqual(len(d._lookahead(api, "srv")), 2)

    def test_it_settles_rather_than_walking(self):
        """Three passes with the catalog growing as the first pass's
        downloads land. The second and third must ask for nothing — the old
        flat window's failure was re-queueing every pass."""
        asked = []
        held = [1]

        def run():
            d, api = self._planner(held=held[0])
            out = d._lookahead(api, "srv")
            asked.append(len(out))
            held[0] += len(out)

        for _ in range(3):
            run()
        self.assertEqual(asked, [8, 0, 0])


if __name__ == "__main__":
    unittest.main()
