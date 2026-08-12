"""The Media Info dialog (#11) — jellyfin-web's ``itemMediaInfo``.

Two things here are divergences from jellyfin-web rather than gaps, and
both are pinned so they do not get "fixed" into parity later:

* **the file path is shown to everyone.** Web gates it on
  ``IsAdministrator``; it is not a boundary (the path is in this app's own
  log and in web's devtools, and a ``static=true`` stream is reachable by
  anyone holding the item guid), so hiding it buys nothing and costs the
  owner of the machine a row.
* **the menu offers this on item TYPE, not on ``item.MediaSources``.** Web
  can test the field on a card because its list responses carry it; ours
  deliberately do not, so the streams are fetched when the dialog opens.
"""

import unittest

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

from tests._shell_harness import (
    FakeController,
    FakeSource,
    _SyncPool,
    build_scene,
    ids,
)

SOURCES = [
    {"Id": "src1", "Name": "1080p", "Container": "mkv", "Size": 8_400_000_000,
     "Path": "/media/Films/Film (2017)/film.mkv",
     "MediaStreams": [
         {"Type": "Video", "Index": 0, "Codec": "hevc", "Width": 1920,
          "Height": 1080, "Profile": "Main 10", "BitDepth": 10},
         {"Type": "Audio", "Index": 1, "Codec": "truehd", "Channels": 8,
          "ChannelLayout": "7.1", "Language": "eng", "IsDefault": True},
         {"Type": "Subtitle", "Index": 2, "Codec": "subrip",
          "Language": "eng", "IsExternal": True},
     ]},
    {"Id": "src2", "Name": "4K", "Container": "mp4", "Size": 44_000_000_000,
     "Path": "/media/Films/Film (2017)/film-4k.mp4",
     "MediaStreams": [
         {"Type": "Video", "Index": 0, "Codec": "av1", "Width": 3840,
          "Height": 2160},
     ]},
]


class MediaInfoSource(FakeSource):
    """A source whose get_item answers with real MediaSources.

    The stand-in that matters here: FakeSource's plain item has none, so a
    dialog built against it would be empty, and every assertion about what
    it draws would pass against nothing.
    """

    def __init__(self, sources=None, fail=False):
        super().__init__()
        self.media_sources = SOURCES if sources is None else sources
        self.fail = fail
        self.get_item_calls = []

    def get_item(self, server, item_id):
        self.get_item_calls.append((server, item_id))
        if self.fail:
            raise RuntimeError("server away")
        item = dict(super().get_item(server, item_id) or {})
        item["MediaSources"] = self.media_sources
        return item


class MediaInfoDialogTest(unittest.TestCase):

    def _browser(self, **kw):
        src = MediaInfoSource(**kw)
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b.nav_stack = [{"kind": "grid", "server": "srv"}]
        # Inline, so the fetch this dialog does on open completes inside
        # the test rather than on a thread nobody waits for.
        b._pool = _SyncPool()
        return b, src

    @staticmethod
    def _texts(nodes):
        return [n.get("text") or "" for n in nodes]

    @staticmethod
    def _flat(nodes):
        """Every drawn string joined, with whitespace removed.

        Whitespace removed because a wrapped Text is several nodes and the
        breaker **drops the space it broke at** — the same trap the epub
        reader documents (CLAUDE.md: "a space is not a run"). On screen
        that is an ordinary line break and reads correctly; it is only text
        rebuilt from the nodes that loses it. So a value that wraps can be
        asserted on its characters but not on its spacing.
        """
        return "".join(n.get("text") or "" for n in nodes).replace(" ", "")

    def _open(self, b, item=None):
        b._open_media_info(item or {"Id": "i1", "Type": "Movie",
                                    "Name": "The Film"}, "srv")
        return build_scene(b, (1280, 720))

    def test_it_fetches_the_item_rather_than_trusting_the_tile(self):
        b, src = self._browser()
        nodes, _h = self._open(b)
        self.assertEqual(src.get_item_calls, [("srv", "i1")])
        self.assertIn("minfo", ids(nodes))

    def test_it_shows_the_path_to_everyone(self):
        # Deliberate divergence: web gates this on IsAdministrator.
        b, _s = self._browser()
        nodes, _h = self._open(b)
        # Joined, because the value column wraps -- which is the point of
        # wrapping it rather than ellipsizing: a path is longer than the
        # dialog and cutting it drops the filename, the half anyone reads.
        self.assertIn("/media/Films/Film(2017)/film.mkv", self._flat(nodes))

    def test_a_path_with_no_spaces_still_wraps_rather_than_overflowing(self):
        """The commoner real shape, and the one a word-wrapper gets wrong:
        `/media/Films/Blade_Runner_2049/...mkv` has nothing to break on."""
        path = ("/media/Films/Blade_Runner_2049_2017_UHD_Remux/"
                "blade_runner_2049.2017.2160p.remux.mkv")
        sources = [dict(SOURCES[0], Path=path)]
        b, _s = self._browser(sources=sources)
        nodes, _h = self._open(b)
        self.assertIn(path, self._flat(nodes))
        scroll = next(n for n in nodes if n.get("id") == "minfo-scroll")
        for node in nodes:
            if node.get("text") and node["text"] in path:
                self.assertLessEqual(node.get("w") or 0,
                                     scroll.get("w") or 0,
                                     "a path segment ran past the dialog")

    def test_it_shows_every_stream_typed_and_in_order(self):
        b, _s = self._browser()
        nodes, _h = self._open(b)
        text = self._texts(nodes)
        for heading in ("Video", "Audio", "Subtitle"):
            self.assertIn(heading, text)
        joined = " | ".join(text)
        for attr in ("HEVC", "1920x1080", "Main 10", "TRUEHD", "7.1",
                     "SUBRIP"):
            self.assertIn(attr, joined)

    def test_a_single_version_item_offers_no_version_picker(self):
        b, _s = self._browser(sources=[SOURCES[0]])
        nodes, _h = self._open(b)
        self.assertNotIn("minfo-src", ids(nodes))

    def test_several_versions_get_a_picker(self):
        b, _s = self._browser()
        nodes, _h = self._open(b)
        self.assertIn("minfo-src", ids(nodes))

    def test_switching_version_asks_for_a_repaint(self):
        """The renderer flips a Dropdown's own selection optimistically, so
        without a repaint the control would look right while every row
        below it still described the first file.

        Asserted **separately from the redraw below**, because
        ``build_scene`` renders when asked whether or not the app would
        ever have redrawn — so the test that follows this one passes
        against a handler that never invalidates. That is the browser's
        standing footgun in its quietest form (CLAUDE.md), and a mutation
        run is what caught this test not covering it."""
        b, _s = self._browser()
        _nodes, handlers = self._open(b)
        seen = []
        real = b.invalidate
        b.invalidate = lambda: (seen.append(1), real())
        handlers["minfo-src"]["select"](1, "4K")
        self.assertTrue(seen, "switching version did not ask for a repaint")

    def test_switching_version_redescribes_the_other_file(self):
        b, _s = self._browser()
        _nodes, handlers = self._open(b)
        handlers["minfo-src"]["select"](1, "4K")
        nodes, _h = build_scene(b, (1280, 720))
        joined = " | ".join(self._texts(nodes))
        self.assertIn("AV1", joined)
        self.assertIn("3840x2160", joined)
        self.assertNotIn("TRUEHD", joined)
        self.assertIn("mp4", joined)

    def test_an_item_with_no_sources_says_so_rather_than_drawing_nothing(self):
        b, _s = self._browser(sources=[])
        nodes, _h = self._open(b)
        self.assertIn("minfo", ids(nodes))
        self.assertIn("No media information is available.",
                      self._texts(nodes))

    def test_a_failed_fetch_falls_back_to_what_the_tile_had(self):
        """From a menu press, a dialog that never appears is
        indistinguishable from a broken entry — so it opens, named, and
        says it has nothing rather than opening blank.

        The name is the assertion that has teeth: the dialog opens either
        way, so "it opened" does not distinguish falling back to the tile's
        own DTO from throwing it away."""
        b, _s = self._browser(fail=True)
        nodes, _h = self._open(b)
        self.assertIn("minfo", ids(nodes))
        text = self._texts(nodes)
        self.assertIn("The Film", text)
        self.assertIn("No media information is available.", text)

    def test_closing_it_puts_it_away(self):
        b, _s = self._browser()
        _nodes, handlers = self._open(b)
        handlers["minfo-ok"]["click"]()
        nodes, _h = build_scene(b, (1280, 720))
        self.assertNotIn("minfo", ids(nodes))


class MediaInfoMenuEntryTest(unittest.TestCase):
    """Which tiles offer it. Web asks ``item.MediaSources``; a grid DTO of
    ours has none, so the question becomes the item's type."""

    def _entries(self, item):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b.nav_stack = [{"kind": "grid", "server": "srv"}]
        return [e[2] for e in b._tile_menu_entries(item)]

    def test_video_and_audio_offer_it(self):
        for t in ("Movie", "Episode", "Video", "Audio", "AudioBook"):
            self.assertIn("mediainfo", self._entries({"Id": "x", "Type": t}),
                          "%s should offer Media Info" % t)

    def test_things_with_no_media_sources_do_not(self):
        # A Book is not IHasMediaSources at all (books.py), and neither a
        # Photo nor a Program has sources -- the dialog would always be
        # empty, which is worse than no entry.
        for t in ("Book", "Photo", "Program", "TvChannel", "Series",
                  "Season", "MusicAlbum"):
            self.assertNotIn("mediainfo",
                             self._entries({"Id": "x", "Type": t}),
                             "%s should not offer Media Info" % t)

    def test_it_survives_offline(self):
        """One of the few entries that answers as well with the server
        away: the catalog holds the source the file was downloaded with."""
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b.nav_stack = [{"kind": "grid", "server": "srv"}]
        b._offline = True
        entries = [e[2] for e in b._tile_menu_entries({"Id": "x",
                                                       "Type": "Movie"})]
        self.assertIn("mediainfo", entries)
        self.assertNotIn("download", entries)


if __name__ == "__main__":
    unittest.main()
