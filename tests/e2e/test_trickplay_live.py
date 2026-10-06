"""Trickplay against a real server's tiles, read off the relay's request log.

The unit suite models the tile source; this is the one place the shim meets
tiles a Jellyfin made, through the real HUD: the pointer hovers the seek bar,
the renderer asks for the frames it cannot draw, and the worker fetches a
window. The property is the backlog report's: **each tile is fetched once per
window**, however long the pointer stays inside a window it already has, and a
move to a new region fetches that window's tiles once each.

A control, not a regression of a fix: the QA server's tiles are exact, so this
passed before the bad-tile handling too. It guards the window logic against a
real manifest (docs/artwork-pipeline.md section 11). Needs the server built
with `--trickplay "Test Media"`; skipped when the item has no trickplay.
"""

import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _app  # noqa: E402
import _e2e  # noqa: E402
import _flows  # noqa: E402
from test_playback_lifecycle import _PlaybackCase  # noqa: E402

#: Three hours at a 10 s interval is ~1080 frames, several 25 MB windows.
ITEM = "Three hours"
TILE = re.compile(r"/Videos/([0-9a-f]+)/Trickplay/(\d+)/(\d+)\.jpg")


class TrickplayTilesAreFetchedOncePerWindowTest(_PlaybackCase):

    def tiles(self):
        out = []
        for _method, path in list(self.relay.requests):
            m = TILE.search(path)
            if m and m.group(1) == self.item_id:
                out.append(int(m.group(3)))
        return out

    def settled_tiles(self, quiet=3.0, timeout=60):
        """The tile requests once none has arrived for ``quiet`` seconds."""
        deadline = time.monotonic() + timeout
        seen, since = self.tiles(), time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.5)
            now = self.tiles()
            if now != seen:
                seen, since = now, time.monotonic()
            elif time.monotonic() - since >= quiet:
                return seen
        return seen

    def hover(self, fraction):
        """Point at ``fraction`` of the seek bar, as a hand does."""
        bar = _app.node(self.app.frame(), "hud-seek")
        if bar is None or not bar.get("vis"):
            # The HUD hides when the pointer rests; a hand moves it back.
            self.app.summon_hud()
            bar = _app.node(self.app.frame(), "hud-seek")
        self.assertIsNotNone(bar, "no seek bar on screen")
        self.app.point(bar["x"] + bar["w"] * fraction,
                       bar["y"] + bar["h"] / 2)

    def test_each_tile_once_per_window(self):
        found = [i for i in self.session.find_all(item_type="Movie")
                 if i.get("Name") == ITEM]
        self.assertEqual(1, len(found), ITEM)
        self.item_id = found[0]["Id"]
        detail = self.session._request(
            "/Items/%s?Fields=Trickplay" % self.item_id)
        manifests = list((detail.get("Trickplay") or {}).values())
        if not manifests:
            self.skipTest("%r has no trickplay on this server" % ITEM)
        self.fresh(self.item_id)

        _flows.open_by_search(self.app, ITEM, self.item_id)
        self.app.key("ENTER")
        self.app.playing_path()
        first = self.settled_tiles()
        self.assertTrue(first, "the opening window fetched no tiles")
        self.assertEqual(len(first), len(set(first)),
                         "a tile was fetched twice for one window: %r" % first)

        # Inside the window the file already holds: nothing more, however
        # often the pointer moves. 0.5-1.5% of three hours is 54-162 s.
        self.app.summon_hud()
        for fraction in (0.005, 0.01, 0.015, 0.005):
            self.hover(fraction)
            time.sleep(0.5)
        self.assertEqual(self.settled_tiles(), first,
                         "scrubbing inside a loaded window fetched tiles")

        # A new region: its window's tiles, once each, across three moves.
        for fraction in (0.75, 0.752, 0.754):
            self.hover(fraction)
            time.sleep(0.5)
        after = self.settled_tiles()[len(first):]
        self.assertTrue(after, "the far window was never fetched")
        self.assertEqual(len(after), len(set(after)),
                         "a tile was fetched twice: %r" % after)


if __name__ == "__main__":
    unittest.main()
