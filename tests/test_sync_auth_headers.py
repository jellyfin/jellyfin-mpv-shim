"""Every outbound request the sync manager makes carries the auth header.

This file exists because of *how* the header work went wrong, not because of
which request was wrong. The subtitle sidecar was converted to
``Authorization`` and the other six ``requests.get`` calls in the same module
were left building urls with ``?ApiKey=``. Against a stock Jellyfin that is
invisible -- it accepts either -- so the gap survived a review, a test suite
and a hand test, and only surfaced against a proxy that requires the header.

Six of those seven calls swallow their own exceptions
(``except Exception: log.debug(...)``), so the failure was not even loud:
the download completed and was marked done, with its artwork and trickplay
tiles silently absent. That is the case the enumeration below is really for.

So the important test here is not any single site -- it is
``TestEveryCallSiteIsCovered``, which reads the module's AST and fails on a
``requests.*`` call that does not pass ``headers=``. A new download helper
gets this wrong by default; this makes "by default" fail.
"""

import ast
import inspect
import os
import unittest
from unittest import mock

from jellyfin_mpv_shim.sync import manager as sync_manager
from jellyfin_mpv_shim.sync.manager import SyncManager, _same_origin

SERVER = "https://srv.example:8920"
TOKEN_HEADER = 'MediaBrowser Client="Shim", DeviceId="d", Token="REALTOKEN"'


class _Http:
    def _get_authenication_header(self):
        return TOKEN_HEADER


class _Config:
    def __init__(self, server=SERVER):
        self.data = {"auth.server": server, "auth.token": "REALTOKEN"}


class _Client:
    """Just enough client for the header helper."""

    def __init__(self, server=SERVER):
        self.config = _Config(server)
        self.http = _Http()


class TestHeadersFor(unittest.TestCase):
    """The one way this module authenticates an outbound request."""

    def setUp(self):
        self.mgr = SyncManager.__new__(SyncManager)
        self.client = _Client()

    def test_our_own_server_gets_the_header(self):
        h = self.mgr._headers_for(self.client, SERVER + "/Items/x/Download")
        self.assertEqual(h, {"Authorization": TOKEN_HEADER})

    def test_another_host_gets_nothing(self):
        h = self.mgr._headers_for(self.client, "https://elsewhere.example/x.srt")
        self.assertEqual(h, {})

    def test_a_downgrade_to_http_is_not_the_same_origin(self):
        """Sending the token over http to the host we reached over https
        hands it to anyone on the path."""
        h = self.mgr._headers_for(self.client, "http://srv.example:8920/x")
        self.assertEqual(h, {})

    def test_a_different_port_is_not_the_same_origin(self):
        h = self.mgr._headers_for(self.client, "https://srv.example:9999/x")
        self.assertEqual(h, {})

    def test_a_broken_client_falls_back_rather_than_raising(self):
        class _Boom:
            def _get_authenication_header(self):
                raise RuntimeError("no")

        self.client.http = _Boom()
        h = self.mgr._headers_for(self.client, SERVER + "/Items/x/Download")
        self.assertEqual(h, {})

    def test_a_junk_url_is_not_an_exception(self):
        self.assertEqual(self.mgr._headers_for(self.client, "::::"), {})


class TestTheMediaDownloadCarriesIt(unittest.TestCase):
    """The visible half: this one raises, so it 401'd out loud."""

    def setUp(self):
        # __new__, not __init__: the real one opens the catalog db and starts
        # a worker thread, and none of that is what this is about. The two
        # attributes the streaming loop reads are set by hand.
        self.mgr = SyncManager.__new__(SyncManager)
        self.mgr._cancelled = set()
        self.mgr._stop = False
        self.client = _Client()

    def _run(self, tmp, resume=0):
        seen = {}

        class _Resp:
            status_code = 200
            headers = {"Content-Length": "4"}

            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def raise_for_status(self_):
                pass

            def iter_content(self_, n):
                return [b"data"]

        def fake_get(url, **kw):
            seen["url"] = url
            seen["headers"] = kw.get("headers")
            return _Resp()

        with mock.patch.object(sync_manager.requests, "get", fake_get):
            self.mgr._stream_request(
                SERVER + "/Items/x/Download", tmp, "x", "name", 4, resume,
                lambda: False,
                self.mgr._headers_for(self.client, SERVER + "/Items/x/Download"))
        return seen

    def test_the_stream_request_sends_the_header(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            seen = self._run(os.path.join(d, "m.part"))
        self.assertEqual(seen["headers"].get("Authorization"), TOKEN_HEADER)

    def test_a_resume_keeps_both_range_and_authorization(self):
        """The bug's exact shape: the dict was rebuilt from scratch here, so
        the resume header arrived and the credentials did not."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            seen = self._run(os.path.join(d, "m.part"), resume=1024)
        self.assertEqual(seen["headers"].get("Authorization"), TOKEN_HEADER)
        self.assertEqual(seen["headers"].get("Range"), "bytes=1024-")


class TestEveryCallSiteIsCovered(unittest.TestCase):
    """The point of this file.

    A missed call site is what happened, so the guard is over call sites
    rather than over behaviour. Reading the AST rather than the text so a
    reformat, a wrapped line or a renamed variable cannot make it pass by
    accident.
    """

    def _calls(self):
        src = inspect.getsource(sync_manager)
        tree = ast.parse(src)
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            # requests.get / requests.post / ...
            if (isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "requests"):
                out.append((node.lineno, "requests." + fn.attr, node))
        return out

    def test_there_are_call_sites_to_check(self):
        """If this module stops using requests directly, this whole file is
        measuring nothing -- fail rather than pass vacuously."""
        self.assertGreaterEqual(len(self._calls()), 5)

    def test_every_request_passes_headers(self):
        missing = [(ln, name) for ln, name, node in self._calls()
                   if not any(kw.arg == "headers" for kw in node.keywords)]
        self.assertEqual(
            missing, [],
            "sync/manager.py issues a request without headers= at "
            + ", ".join("line %d (%s)" % m for m in missing)
            + ". Every outbound request must authenticate through "
              "SyncManager._headers_for; see its docstring.")

    def test_headers_come_from_the_helper(self):
        """Not just any headers= -- the one that same-origin gates. A literal
        dict here would pass the check above while sending the token to
        whatever host the url named."""
        offenders = []
        for ln, name, node in self._calls():
            kw = next((k for k in node.keywords if k.arg == "headers"), None)
            if kw is None:
                continue
            # allowed: self._headers_for(...), or a name bound from it
            ok = (isinstance(kw.value, ast.Call)
                  and isinstance(kw.value.func, ast.Attribute)
                  and kw.value.func.attr == "_headers_for")
            ok = ok or isinstance(kw.value, ast.Name)
            if not ok:
                offenders.append((ln, name))
        self.assertEqual(offenders, [],
                         "headers= not derived from _headers_for at "
                         + str(offenders))


class TestUrlsNoLongerCarryTheToken(unittest.TestCase):
    """The other half: the header is only a win if the url stops carrying it.

    Checked at the call site rather than by running each downloader, because
    the downloaders are wrapped in bare excepts -- a wrong url here fails
    silently, which is how this went unnoticed.
    """

    URL_BUILDERS = {"download_url", "artwork", "trickplay_tile_url",
                    "subtitle_url", "image_url", "audio_url", "video_url"}

    def test_no_url_builder_is_left_at_the_apikey_default(self):
        src = inspect.getsource(sync_manager)
        tree = ast.parse(src)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute)
                    and fn.attr in self.URL_BUILDERS):
                continue
            kw = next((k for k in node.keywords
                       if k.arg == "include_apikey"), None)
            if kw is None or not (isinstance(kw.value, ast.Constant)
                                  and kw.value.value is False):
                offenders.append((node.lineno, fn.attr))
        self.assertEqual(
            offenders, [],
            "url built with the apiclient's include_apikey=True default at "
            + ", ".join("line %d (%s)" % o for o in offenders)
            + ". The sync manager authenticates by header, so its urls must "
              "pass include_apikey=False.")


class TestSameOriginStillHolds(unittest.TestCase):
    """_headers_for leans on this entirely."""

    def test_matching(self):
        self.assertTrue(_same_origin(SERVER + "/a/b", SERVER))

    def test_scheme_port_and_host_all_count(self):
        for other in ("http://srv.example:8920", "https://srv.example:1",
                      "https://other.example:8920"):
            with self.subTest(other=other):
                self.assertFalse(_same_origin(other + "/a", SERVER))

    def test_a_relative_url_is_not_our_server(self):
        self.assertFalse(_same_origin("/Items/x/Download", SERVER))


if __name__ == "__main__":
    unittest.main()
