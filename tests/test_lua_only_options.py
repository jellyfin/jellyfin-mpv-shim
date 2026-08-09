"""An mpv built without lua must still start (#16 follow-up).

`--osc` is only registered when mpv was built with lua, and a build without
it does not ignore the option -- it refuses to start. That made the whole
lua fallback unreachable: `lua_works` needs a live mpv to probe, and the app
died constructing one.

Measured against a real `-Dlua=disabled` mpv 0.41 (built via
`~/Desktop/mpv-matrix`), on both backends:

  libmpv   AttributeError('mpv option does not exist', -5, (h, b'osc', b'no'))
  jsonipc  the binary prints "Error parsing option osc (option not found)"
           and exits, arriving as MPVError("MPV process retry limit reached.")
           only after every start retry has been spent on it

The two failures look nothing alike and **neither is parsed**, because
there is only one option this can be: `--osc` is the single lua-gated
option the shim sets. [iw]: "we don't even need the detector, osc not being
available means lua wasn't compiled in." So construction is retried once
without it, and the answer is recorded rather than rediscovered.

The direction of that inference is the thing to keep straight, and the last
test here is what holds it: no `--osc` proves lua is absent, but having it
proves only that lua was *compiled in*. Lua that loads and then errors is
exactly what the probe exists for, so the ordinary path must still ask.
"""

import logging
import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]


#: The live half -- that a real no-lua mpv rejects --osc, on both backends,
#: and that the fallback comes up -- is `tests/e2e/test_mpv_matrix.py`. It
#: needs a built mpv and is deliberately not in the discovered suite: this
#: process has a live libmpv (discovery imports the modules that import
#: `player`), and spawning an mpv binary out of it segfaults at teardown
#: often enough to be flaky. Nothing to do with what is asserted there.


def _libmpv_error(name=b"osc", value=b"no"):
    """The shape python-mpv raises. Recorded from a real failure."""
    return AttributeError("mpv option does not exist", -5,
                          (object(), name, value))


class ConstructRetryTest(unittest.TestCase):
    """`PlayerManager._construct_mpv`, without constructing a player.

    The method only needs `self` for `_lua_works`, so it is called unbound
    against a stand-in -- importing `player` for real opens a window.
    """

    def _construct(self, factory, options):
        from jellyfin_mpv_shim import player

        me = mock.Mock()
        me._lua_works = None
        with mock.patch.object(player, "mpv") as fake_mpv:
            fake_mpv.MPV.side_effect = factory
            got = player.PlayerManager._construct_mpv(me, options)
        return got, fake_mpv.MPV.call_args_list, me

    @staticmethod
    def _rejects_osc(exc):
        def factory(**kw):
            if "osc" in kw:
                raise exc
            return "player"
        return factory

    def test_libmpv_retries_without_osc(self):
        got, calls, _me = self._construct(
            self._rejects_osc(_libmpv_error()), {"osc": False, "vo": "gpu"})
        self.assertEqual(got, "player")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("osc", calls[1].kwargs)
        # Only --osc goes. Dropping the rest would start mpv configured
        # differently in ways nothing else would notice.
        self.assertEqual(calls[1].kwargs["vo"], "gpu")

    def test_the_external_failure_is_retried_the_same_way(self):
        # jsonipc reports a dead process and names no option; the retry is
        # driven by *what we sent*, so it does not care.
        class MPVError(Exception):
            pass

        got, calls, _me = self._construct(
            self._rejects_osc(MPVError("MPV process retry limit reached.")),
            {"osc": False})
        self.assertEqual(got, "player")
        self.assertEqual(len(calls), 2)

    def test_a_failure_with_no_osc_in_the_set_is_not_retried(self):
        # Nothing else we send is lua-gated, so a failure without --osc is
        # a real failure and must surface as itself.
        def factory(**kw):
            raise RuntimeError("vo does not exist")

        with self.assertRaises(RuntimeError):
            self._construct(factory, {"vo": "nonesuch"})

    def test_the_second_failure_surfaces(self):
        # Retrying must not swallow the real error when --osc was not the
        # problem after all -- and must not have claimed a cause on the way
        # there. The warning is logged only once the retry has worked.
        def factory(**kw):
            raise RuntimeError("something else entirely")

        with self.assertLogs("player", level="WARNING") as caught:
            logging.getLogger("player").warning("nothing to see")
            with self.assertRaises(RuntimeError):
                self._construct(factory, {"osc": False})
        self.assertNotIn("without lua", "\n".join(caught.output))

    def test_a_failed_retry_does_not_claim_lua_is_absent(self):
        def factory(**kw):
            raise RuntimeError("something else entirely")

        from jellyfin_mpv_shim import player

        me = mock.Mock()
        me._lua_works = None
        with mock.patch.object(player, "mpv") as fake_mpv:
            fake_mpv.MPV.side_effect = factory
            with self.assertRaises(RuntimeError):
                player.PlayerManager._construct_mpv(me, {"osc": False})
        self.assertIsNone(me._lua_works)

    def test_dropping_osc_records_that_lua_is_absent(self):
        # The point of the whole change: `lua_works` then answers without
        # loading a probe script and without spending its timeout.
        _got, _calls, me = self._construct(
            self._rejects_osc(_libmpv_error()), {"osc": False})
        self.assertIs(me._lua_works, False)

    def test_a_clean_start_leaves_the_lua_question_open(self):
        """mpv having --osc is not proof that lua *runs*.

        This is the half that is easy to get backwards, and getting it
        backwards would skip the probe on every normal machine -- so lua
        that loads and then errors would look fine and the app would draw
        nothing.
        """
        _got, calls, me = self._construct(lambda **kw: "player",
                                          {"osc": False})
        self.assertEqual(len(calls), 1)
        self.assertIsNone(me._lua_works)


if __name__ == "__main__":
    unittest.main()
