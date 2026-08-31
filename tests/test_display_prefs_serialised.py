"""The four DisplayPreferences writers share one document.

There is no partial-update path on this API, so every save GETs the whole
DisplayPreferencesDto, changes its own slice of CustomPrefs and POSTs it back.
Four methods in `repository.py` do that -- `save_home_layout`,
`save_user_prefs`, `save_live_tv_prefs`, `save_view_setting` -- and the file
holds no lock at all. They reach the server through the shared four-worker
AsyncRunner, so two can genuinely be in flight, and `settings/home.py` puts
two of them on one tab.

Read dto0, read dto0, change A, change B, post A, post B: B's document still
carries A's old value, so A is reverted. Both calls report success, nothing
rolls back, and the loss surfaces on the next full refresh, on restart, or in
jellyfin-web -- which reads the same document.

The window is a full round trip. ~10 ms against a server on localhost; the
real ones are remote HTTPS, where it is 50-200 ms and two checkboxes a fifth
of a second apart is an ordinary action.

The in-tree model is `gateway/editing.py:playlist_move_many`, which hit this
with per-move tasks fanned onto the same pool -- "landed a different order on
the server than the one shown" -- and fixed it by running the sequence in
order. `users.py:save` is the local-document equivalent: whole-document
read-modify-write under a lock.
"""

import sys
import threading
import unittest

sys.argv = [sys.argv[0]]

#: Long enough to force the overlap while the writes are unserialised, short
#: enough that a SERIALISED run -- where the second reader cannot arrive until
#: the first has posted, so the barrier can never be met -- does not spend the
#: suite's time waiting for it.
BARRIER_WAIT = 0.5

from jellyfin_mpv_shim.mpvtk_browser.repository import (  # noqa: E402
    LibrarySource)


class _Api:
    """One shared CustomPrefs document, with a barrier in the GET so the
    interleaving is expressible rather than hoped for."""

    def __init__(self, barrier=None):
        self.doc = {"Id": "dp", "CustomPrefs": {}}
        self._barrier = barrier
        self.posts = 0

    def get_user_settings(self, client=None):
        import copy
        got = copy.deepcopy(self.doc)
        if self._barrier is not None:
            # Both readers get the same snapshot -- the state the bug needs,
            # and the state serialising must make impossible.
            try:
                self._barrier.wait(timeout=BARRIER_WAIT)
            except threading.BrokenBarrierError:
                pass
        return got

    def update_user_settings(self, dto, client=None):
        self.posts += 1
        self.doc = dto


class _Conn:
    def __init__(self, api):
        self.api = api


def _source(api):
    src = LibrarySource.__new__(LibrarySource)
    src._home_prefs = {}
    src._user_prefs = {}
    src._live_tv_prefs = {}
    src._custom_prefs = {}
    src._conn = lambda server_uuid: _Conn(api)
    return src


class TwoWritersDoNotClobberEachOtherTest(unittest.TestCase):

    def _run_both(self, src, first, second):
        errors = []

        def run(fn):
            try:
                fn()
            except Exception as exc:      # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(f,))
                   for f in (first, second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertFalse(any(t.is_alive() for t in threads), "a writer hung")
        self.assertEqual(errors, [], repr(errors))

    def test_a_layout_save_and_a_prefs_save_both_survive(self):
        """The reachable pair: settings/home.py submits both from one tab."""
        api = _Api(barrier=threading.Barrier(2, timeout=BARRIER_WAIT))
        src = _source(api)

        self._run_both(
            src,
            lambda: src.save_home_layout("srv", ["resume", "nextup"]),
            lambda: src.save_user_prefs("srv", {"use_episode_images": True}))

        custom = api.doc.get("CustomPrefs") or {}
        home_keys = [k for k in custom if k.startswith("homesection")]
        self.assertTrue(
            home_keys,
            "the home layout was reverted by the preferences save: %r"
            % sorted(custom))
        self.assertTrue(
            [k for k in custom if "pisode" in k or "mage" in k],
            "the preferences save was reverted by the home layout save: %r"
            % sorted(custom))

    def test_both_posts_are_still_made(self):
        """Serialising must not collapse the two writes into one."""
        api = _Api(barrier=threading.Barrier(2, timeout=BARRIER_WAIT))
        src = _source(api)
        self._run_both(
            src,
            lambda: src.save_home_layout("srv", ["resume"]),
            lambda: src.save_user_prefs("srv", {"use_episode_images": True}))
        self.assertEqual(api.posts, 2)

    def test_two_library_sources_share_one_lock(self):
        """A reconnect builds a NEW LibrarySource while async work can still
        hold a writer bound to the old one (`ui.py` swaps the source on every
        reconnect). Per-instance locks are two locks for one document, which
        is the same lost update with an extra step."""
        api = _Api(barrier=threading.Barrier(2, timeout=BARRIER_WAIT))
        before, after = _source(api), _source(api)

        self._run_both(
            before,
            lambda: before.save_home_layout("srv", ["resume", "nextup"]),
            lambda: after.save_user_prefs("srv", {"use_episode_images": True}))

        custom = api.doc.get("CustomPrefs") or {}
        self.assertTrue([k for k in custom if k.startswith("homesection")],
                        "the layout write was lost across a source rebuild")
        self.assertTrue([k for k in custom if "pisode" in k or "mage" in k],
                        "the prefs write was lost across a source rebuild")

    def test_a_single_writer_is_unaffected(self):
        """The control: no barrier, one writer, nothing to serialise."""
        api = _Api()
        src = _source(api)
        src.save_home_layout("srv", ["resume", "nextup"])
        custom = api.doc.get("CustomPrefs") or {}
        self.assertTrue([k for k in custom if k.startswith("homesection")])


if __name__ == "__main__":
    unittest.main()
