"""Does the fake `LibrarySource` still describe the real one?

~2900 unit tests run against `tests/_shell_harness.FakeSource`. That fake is
the only description of the server those tests have, and nothing checks it
against a server — so every one of them is sound exactly as far as the fake is
honest. Two ways it can stop being:

**A method the real source no longer has, or no longer takes.** A test calling
it stays green while production raises. This is the shape of `b97dd523`:
`enable_images=False` passed to a client method with no such argument, which
took down the Live TV channel page and which nothing found until someone
opened that screen. `tests/test_source_invariants.py` closed that at the
apiclient boundary by walking the AST; this closes the same gap one layer up,
at the boundary the browser actually uses.

**A DTO field the fake invents.** Production code grows a dependency on a key
the server does not send. Every historic instance ran this way round — the
offline Series DTO with no `PrimaryImageAspectRatio` (squares instead of
posters, `8a946e39`), the offline Season with no `SeriesId` (empty seasons,
`5847cd20`), the `.strm` whose `RunTimeTicks` is on the MediaSource and not
the Item (`8589cc4c`). The fake being *poorer* than the server is not
symmetrical: it makes tests stricter than reality, which is safe, so only the
one direction is asserted.

This suite currently finds nothing, which is the point of writing it down now:
it is a ratchet, not a diagnosis.
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Methods on the fake with no counterpart on LibrarySource, and why that is
# allowed. Keep this list short and argued — every entry is a screen whose
# tests are describing something the browser cannot actually ask for.
SURFACE_ALLOWLIST = {
    # Test-only seam, not part of the source contract.
    "_program",
}


def _public_methods(obj):
    return {name for name in dir(obj)
            if not name.startswith("__")
            and callable(getattr(obj, name, None))}


def _params(func):
    """Parameter names, minus the receiver.

    `self`/`cls` are dropped because one side being a `@staticmethod` is not a
    divergence a caller can see: `backdrop_spec` is a staticmethod on
    `LibrarySource` and a plain method on the fake, and `source.backdrop_spec(
    item)` is correct against either.
    """
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return None, False
    names, star = set(), False
    for param in sig.parameters.values():
        if param.kind in (param.VAR_KEYWORD, param.VAR_POSITIONAL):
            star = True
        elif param.name not in ("self", "cls"):
            names.add(param.name)
    return names, star


@_e2e.require_server
class SourceSurfaceTest(unittest.TestCase):
    """The fake's methods must exist on the real source, and take what the
    fake lets a test pass. No server data needed — only the two classes."""

    @classmethod
    def setUpClass(cls):
        from tests._shell_harness import FakeSource
        from jellyfin_mpv_shim.mpvtk_browser.repository import LibrarySource
        cls.fake_cls = FakeSource
        cls.real_cls = LibrarySource

    def test_every_fake_method_exists_on_the_real_source(self):
        fake = _public_methods(self.fake_cls) - SURFACE_ALLOWLIST
        real = _public_methods(self.real_cls)
        missing = sorted(m for m in fake - real if not m.startswith("_"))
        self.assertEqual(
            missing, [],
            "the fake source answers methods LibrarySource does not have, so "
            "tests exercising them are describing a server that cannot be "
            "asked: %s" % missing)

    def test_the_real_source_accepts_what_the_fake_accepts(self):
        """A parameter a test can pass to the fake must be one the real
        source takes. The reverse is fine — the real source may have grown
        options no test uses yet."""
        problems = []
        for name in sorted(_public_methods(self.fake_cls) - SURFACE_ALLOWLIST):
            if name.startswith("_"):
                continue
            real_attr = getattr(self.real_cls, name, None)
            if real_attr is None:
                continue                        # the test above owns this
            fake_params, _ = _params(getattr(self.fake_cls, name))
            real_params, real_star = _params(real_attr)
            if fake_params is None or real_params is None or real_star:
                continue
            extra = sorted(fake_params - real_params)
            if extra:
                problems.append("%s(%s)" % (name, ", ".join(extra)))
        self.assertEqual(
            problems, [],
            "the fake accepts arguments LibrarySource does not, so a test can "
            "make a call production would raise on: %s" % problems)


@_e2e.require_server
class DtoConformanceTest(unittest.TestCase):
    """The fake must not invent DTO fields the server does not send."""

    @classmethod
    def setUpClass(cls):
        from tests._shell_harness import FakeSource
        cls.session = _e2e.Session()
        cls.source = cls.session.library_source()
        cls.fake = FakeSource()
        U = _e2e.SOURCE_UUID
        cls.libs = cls.source.get_libraries(U)
        cls.by_name = {lib["Name"]: lib for lib in cls.libs}
        cls.fake_libs = cls.fake.get_libraries("srv1")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.source.stop()
        finally:
            cls.session.stop()

    def _keys(self, items):
        keys = set()
        for item in items or []:
            if isinstance(item, dict):
                keys |= set(item)
        return keys

    def assert_no_invented_keys(self, label, real_items, fake_items):
        # Both sides must have produced something, or the comparison is
        # vacuously true and would stay green through any amount of drift.
        self.assertTrue(real_items, "%s: the server returned nothing, so this "
                                    "comparison proves nothing" % label)
        self.assertTrue(fake_items, "%s: the fake returned nothing, so this "
                                    "comparison proves nothing" % label)
        invented = sorted(self._keys(fake_items) - self._keys(real_items))
        self.assertEqual(
            invented, [],
            "%s: the fake promises fields the server does not send, so code "
            "may come to depend on them: %s" % (label, invented))

    def test_libraries(self):
        self.assert_no_invented_keys("get_libraries", self.libs,
                                     self.fake_libs)

    def test_library_items(self):
        real = self.source.get_library_items(
            _e2e.SOURCE_UUID, self.by_name["Movies"]["Id"],
            start_index=0, limit=8)[0]
        fake = self.fake.get_library_items(
            "srv1", self.fake_libs[0]["Id"], start_index=0)[0]
        self.assert_no_invented_keys("get_library_items", real, fake)

    def test_seasons_and_episodes(self):
        series = self.source.get_library_items(
            _e2e.SOURCE_UUID, self.by_name["Shows"]["Id"],
            start_index=0, limit=1)[0][0]
        seasons = self.source.get_seasons(_e2e.SOURCE_UUID, series["Id"])
        self.assert_no_invented_keys(
            "get_seasons", seasons, self.fake.get_seasons("srv1", "series1"))

        episodes = self.source.get_episodes(
            _e2e.SOURCE_UUID, series["Id"], seasons[0]["Id"])
        self.assert_no_invented_keys(
            "get_episodes", episodes,
            self.fake.get_episodes("srv1", "series1", "season1"))

    def test_music(self):
        albums = self.source.get_music_albums(
            _e2e.SOURCE_UUID, self.by_name["Music"]["Id"],
            start_index=0, limit=4)[0]
        self.assert_no_invented_keys(
            "get_music_albums", albums,
            self.fake.get_music_albums("srv1", "lib-music", start_index=0)[0])

        tracks = self.source.get_album_tracks(_e2e.SOURCE_UUID,
                                              albums[0]["Id"])
        self.assert_no_invented_keys(
            "get_album_tracks", tracks,
            self.fake.get_album_tracks("srv1", "album1"))

    def test_home_rows(self):
        def flatten(rows):
            return [item for row in rows or [] for item in row.get("items") or []]

        self.assert_no_invented_keys(
            "get_home_rows",
            flatten(self.source.get_home_rows(_e2e.SOURCE_UUID)),
            flatten(self.fake.get_home_rows("srv1")))

    def test_live_tv_channels(self):
        if not self.source.has_live_tv(_e2e.SOURCE_UUID):
            self.skipTest("this server has no Live TV configured")
        self.assert_no_invented_keys(
            "get_channels", self.source.get_channels(_e2e.SOURCE_UUID),
            self.fake.get_channels("srv1"))


if __name__ == "__main__":
    unittest.main()
