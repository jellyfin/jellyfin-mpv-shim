#!/usr/bin/env python3
"""Every raw `self._video` truth-test has to say which question it is asking.

`self._video` answers two different questions and the code spelled both the
same way::

    is something playing?          -> self._video is not None
    is the library on screen?      -> self._video is not None   # WRONG

Music is what splits them: audio keeps `_video` set **and** keeps the browser
up -- that is what the now-playing bar is for. So the second question has its
own predicate, `_library_showing()` (and `_video_on_screen()` for the
complement), and a site that spells it `self._video` is either asking the
first question or is a bug.

**Three sites got it wrong, and they were found one at a time.** `show_picture`
refused a comic behind the now-playing bar; `set_fullscreen` persisted the
video preference during music; `_apply_browse_fullscreen` applied only the ON
direction. The audit that fixed the first two missed the third, which lives one
call *below* the method it fixed -- so the shape survived its own repair, and
two more (`refresh_browse_bg`, `toggle_settings_menu`) were still open after
it. That is the argument for a lint rather than a fourth careful reading: this
rule is right in prose, applied at N-1 of N sites, and re-read by someone who
already believes it.

So: no judgement about which meaning is correct, because a checker cannot know
that. It requires only that the site be **declared**, which puts the question
in front of whoever adds one -- and the declaration is where a reader learns
`_library_showing()` exists.

Run it directly to list undeclared sites; `tests/test_no_raw_video_predicate.py`
is the guard.
"""

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The player object and its mixins -- everywhere `self._video` is in scope.
FILES = (
    "jellyfin_mpv_shim/player.py",
    "jellyfin_mpv_shim/player_window.py",
    "jellyfin_mpv_shim/player_audio.py",
    "jellyfin_mpv_shim/player_reporting.py",
)

#: Declared sites, by (file, function), each saying which question it asks.
#:
#: "playback" -- is an item loaded/playing. Audio counts, and should.
#: "picture"  -- is a VIDEO PICTURE on screen. Pairs with `_current_is_audio`.
#: "window"   -- who owns the window. Deliberate, and each says why.
DECLARED = {
    # -- the definition itself
    ("player.py", "_library_showing"): "playback; this IS the predicate",

    # -- playback: is an item loaded. Audio counts.
    ("player.py", "_on_eof_reached"): "playback; the item that just ended",
    ("player.py", "_on_playback_abort"): "playback; the item being aborted",
    ("player.py", "_on_pause_change"): "playback; the item being paused",
    ("player.py", "update"): "playback; the item being polled",
    ("player.py", "watched_skip"): "playback; acts on the current item",
    ("player.py", "unwatched_quit"): "playback; acts on the current item",
    ("player.py", "get_video_attr"): "playback; reads the current item",
    ("player.py", "_maybe_save_volume"): (
        "playback; the per-type volume bucket needs the item, and music has "
        "its own bucket -- excluding audio here would stop it saving"),
    ("player.py", "idle_quit"): (
        "playback; do not quit mpv out from under a track. The library being "
        "on screen is not the question -- music keeps it up and still must "
        "not be quit"),
    ("player.py", "is_active"): "playback; public API, named for it",
    ("player.py", "is_playing"): "playback; public API, named for it",
    ("player.py", "is_not_paused"): "playback; public API, named for it",
    ("player.py", "has_video"): "playback; public API, named for it",
    ("player.py", "get_video"): "playback; returns the item",

    # -- picture: paired with _current_is_audio, which is the correct spelling
    ("player.py", "_stats_key"): (
        "picture; a video is on screen to annotate. Audio falls through to "
        "the swallow, deliberately"),
    ("player_window.py", "show_picture"): (
        "picture; a video owns the window and refuses, audio is stopped and "
        "gives it up"),

    # -- window: who owns it, and why _video is the right question here
    ("player_window.py", "reset_picture_view"): (
        "window; keepaspect must NOT be put back while ANYTHING plays. "
        "`_library_showing()` would let this run during music, and this "
        "method runs from _release_page_grabs through the deferring _act -- "
        "landing second is what made every film play stretched. See "
        "test_window_geometry.py:PictureViewHandoffTest"),
    ("player_window.py", "clear_picture"): (
        "window; a video owns the window, so the browse window is not ours "
        "to restore. A picture always stops audio first (show_picture), so "
        "there is no music state to reach here"),
    ("player_window.py", "set_browse_window"): (
        "playback, three times: the colorspace hint would cost a playing "
        "video its HDR passthrough; `stop` must not be fired at a track; and "
        "force_window must not be released while anything plays"),
    ("player_window.py", "force_window"): (
        "playback; same release rule as set_browse_window"),
}


def _is_self_video(node):
    return (isinstance(node, ast.Attribute) and node.attr == "_video"
            and isinstance(node.value, ast.Name) and node.value.id == "self")


class _Finder(ast.NodeVisitor):
    """Truth-tests only. `self._video.parent`, `self._video = x` and
    `self._video.item` are uses, not questions, and are none of this
    checker's business."""

    def __init__(self):
        self.fn = None
        self.hits = []

    def visit_FunctionDef(self, node):
        prev, self.fn = self.fn, node.name
        self.generic_visit(node)
        self.fn = prev

    visit_AsyncFunctionDef = visit_FunctionDef

    def _test(self, node):
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Compare) and _is_self_video(sub.left)
                    and any(isinstance(o, (ast.Is, ast.IsNot))
                            for o in sub.ops)):
                self.hits.append((self.fn, sub.lineno))
                return
        self._bare(node)

    def _bare(self, node):
        if _is_self_video(node):
            self.hits.append((self.fn, node.lineno))
        elif isinstance(node, ast.BoolOp):
            for v in node.values:
                self._bare(v)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            self._bare(node.operand)
        elif (isinstance(node, ast.Call)
              and getattr(node.func, "id", "") == "bool"):
            for a in node.args:
                self._bare(a)

    def visit_If(self, node):
        self._test(node.test)
        self.generic_visit(node)

    def visit_IfExp(self, node):
        self._test(node.test)
        self.generic_visit(node)

    def visit_While(self, node):
        self._test(node.test)
        self.generic_visit(node)

    def visit_Return(self, node):
        if node.value is not None:
            self._test(node.value)
        self.generic_visit(node)


def find(root=ROOT):
    """[(file, function, lineno)] for every raw truth-test found."""
    out = []
    for rel in FILES:
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=rel)
        finder = _Finder()
        finder.visit(tree)
        base = os.path.basename(rel)
        for fn, lineno in sorted(set(finder.hits), key=lambda h: h[1]):
            out.append((base, fn, lineno))
    return out


def undeclared(root=ROOT):
    return [h for h in find(root) if (h[0], h[1]) not in DECLARED]


def main():
    hits = find()
    bad = [h for h in hits if (h[0], h[1]) not in DECLARED]
    for base, fn, lineno in bad:
        print("%s:%d: %s() tests self._video without declaring which "
              "question it asks" % (base, lineno, fn))
    reached = {(b, f) for b, f, _ in hits}
    for key in sorted(set(DECLARED) - reached):
        print("stale declaration: %s %s() no longer tests self._video"
              % key)
    print("%d declared, %d undeclared" % (len(reached & set(DECLARED)),
                                          len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
