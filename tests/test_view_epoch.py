"""Tests for BaseView.run_async's stale-result guards.

These exercise the guard logic without a real Tk app by faking the app's
run_async (which normally hops threads via a UI queue) to invoke callbacks
synchronously, and standing in a lightweight object for current_view.
"""
import unittest

from jellyfin_mpv_shim.library_browser.views import BaseView


class FakeApp:
    def __init__(self):
        self.current_view = None

    def run_async(self, work, done, on_error=None):
        # Synchronous stand-in: run work now, deliver via the guarded cbs.
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - mirror real error path
            if on_error:
                on_error(exc)
            return
        done(result)


class ViewEpochTest(unittest.TestCase):
    def _view(self, app):
        v = BaseView.__new__(BaseView)
        v.app = app
        v.route = {}
        v.frame = None
        v._req_epoch = 0
        return v

    def test_done_runs_when_view_current(self):
        app = FakeApp()
        v = self._view(app)
        app.current_view = v
        got = []
        v.run_async(lambda: 42, got.append)
        self.assertEqual(got, [42])

    def test_done_dropped_when_navigated_away(self):
        app = FakeApp()
        v = self._view(app)
        app.current_view = object()  # user moved to a different view
        got = []
        v.run_async(lambda: 42, got.append)
        self.assertEqual(got, [])

    def test_stale_epoch_dropped(self):
        app = FakeApp()
        v = self._view(app)
        app.current_view = v
        epoch = v.new_request()
        v.new_request()  # a newer request superseded this one
        got = []
        v.run_async(lambda: 1, got.append, epoch=epoch)
        self.assertEqual(got, [])

    def test_matching_epoch_runs(self):
        app = FakeApp()
        v = self._view(app)
        app.current_view = v
        epoch = v.new_request()
        got = []
        v.run_async(lambda: 1, got.append, epoch=epoch)
        self.assertEqual(got, [1])

    def test_error_dropped_when_navigated_away(self):
        app = FakeApp()
        v = self._view(app)
        app.current_view = object()
        errs = []

        def work():
            raise RuntimeError("boom")

        v.run_async(work, lambda _r: None, errs.append)
        self.assertEqual(errs, [])

    def test_error_delivered_when_current(self):
        app = FakeApp()
        v = self._view(app)
        app.current_view = v
        errs = []

        def work():
            raise RuntimeError("boom")

        v.run_async(work, lambda _r: None, errs.append)
        self.assertEqual(len(errs), 1)


if __name__ == "__main__":
    unittest.main()
