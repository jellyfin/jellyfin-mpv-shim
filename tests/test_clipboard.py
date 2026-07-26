"""Copying to the system clipboard without a clipboard dependency.

Every backend is probed and the first one that *verifiably* worked wins —
"verifiably" because mpv only has a clipboard/text where it has a backend for
the session (no x11 backend before 0.41) and a failed write does not always
raise, so a naive set-and-assume would report success while copying nothing.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.argv = ["test"]

from jellyfin_mpv_shim import clipboard  # noqa: E402


class _Mpv:
    """An mpv handle whose clipboard property may or may not stick.

    ``clipboard_text`` is a real property so that ``no_property=True`` models
    a build without it: python-mpv raises on both the command and the
    attribute set, which is what the code has to survive.
    """

    def __init__(self, writable=True, no_property=False):
        self._writable = writable
        self._no_property = no_property
        self._value = ""

    @property
    def clipboard_text(self):
        if self._no_property:
            raise AttributeError("clipboard/text")
        return self._value

    @clipboard_text.setter
    def clipboard_text(self, value):
        if self._no_property:
            raise AttributeError("clipboard/text")
        if self._writable:
            self._value = value

    def command(self, *args):
        if self._no_property:
            raise RuntimeError("no such property")
        if args[:2] == ("set", "clipboard/text") and self._writable:
            self._value = args[2]


class TestMpvBackend(unittest.TestCase):
    def test_a_writable_mpv_is_used_first(self):
        player = _Mpv()
        with mock.patch.object(clipboard, "_via_command") as cmd:
            ok, method = clipboard.copy_text("hello", player=player)
        self.assertTrue(ok)
        self.assertEqual(method, "mpv")
        self.assertEqual(player.clipboard_text, "hello")
        cmd.assert_not_called()

    def test_a_read_only_mpv_falls_through_instead_of_lying(self):
        """The set does not raise; the value simply does not stick. Trusting
        it would report a successful copy of nothing."""
        player = _Mpv(writable=False)
        with mock.patch.object(clipboard, "_via_command", return_value="xclip"):
            ok, method = clipboard.copy_text("hello", player=player)
        self.assertTrue(ok)
        self.assertEqual(method, "xclip")

    def test_an_mpv_without_the_property_falls_through(self):
        player = _Mpv(no_property=True)
        with mock.patch.object(clipboard, "_via_command", return_value="xsel"):
            ok, method = clipboard.copy_text("hello", player=player)
        self.assertEqual(method, "xsel")

    def test_no_player_is_fine(self):
        with mock.patch.object(clipboard, "_via_command", return_value="clip"):
            ok, method = clipboard.copy_text("hello")
        self.assertTrue(ok)
        self.assertEqual(method, "clip")


class TestCommandBackend(unittest.TestCase):
    def _run(self, available, returncode=0, recorder=None):
        def which(name):
            return "/usr/bin/" + name if name in available else None

        def run(argv, **kw):
            if recorder is not None:
                recorder.append((argv[0], kw.get("input")))
            return mock.Mock(returncode=returncode)

        return mock.patch.object(clipboard.shutil, "which", which), \
            mock.patch.object(clipboard.subprocess, "run", run)

    def test_wayland_wins_over_x11(self):
        """A Wayland session usually answers to xclip through XWayland too,
        and that lands in the wrong clipboard."""
        seen = []
        w, r = self._run({"wl-copy", "xclip", "xsel"}, recorder=seen)
        with w, r, mock.patch.object(clipboard, "_commands",
                                     return_value=clipboard._LINUX):
            self.assertEqual(clipboard._via_command("x"), "wl-copy")
        self.assertEqual([c for c, _i in seen], ["wl-copy"])

    def test_it_moves_on_when_a_tool_fails(self):
        w, r = self._run({"xclip", "xsel"}, returncode=1)
        with w, r, mock.patch.object(clipboard, "_commands",
                                     return_value=clipboard._LINUX):
            self.assertIsNone(clipboard._via_command("x"))

    def test_the_text_is_passed_on_stdin_as_utf8(self):
        seen = []
        w, r = self._run({"xclip"}, recorder=seen)
        with w, r, mock.patch.object(clipboard, "_commands",
                                     return_value=clipboard._LINUX):
            clipboard._via_command("héllo")
        self.assertEqual(seen[0][1], "héllo".encode("utf-8"))

    def test_the_output_pipes_are_detached(self):
        """xclip, xsel and wl-copy all fork a child that goes on owning the
        selection and inherits our pipes. Capturing output therefore waits
        for the *clipboard* to be replaced rather than for the command to
        finish — measured as a full 10s timeout on a copy that had in fact
        already succeeded."""
        seen = []

        def run(argv, **kw):
            seen.append(kw)
            return mock.Mock(returncode=0)

        with mock.patch.object(clipboard.shutil, "which",
                               lambda n: "/usr/bin/" + n), \
                mock.patch.object(clipboard.subprocess, "run", run), \
                mock.patch.object(clipboard, "_commands",
                                  return_value=clipboard._LINUX):
            clipboard._via_command("x")
        self.assertNotIn("capture_output", seen[0])
        self.assertEqual(seen[0].get("stdout"), clipboard.subprocess.DEVNULL)
        self.assertEqual(seen[0].get("stderr"), clipboard.subprocess.DEVNULL)

    def test_nothing_installed_is_a_clean_failure(self):
        w, r = self._run(set())
        with w, r:
            ok, method = clipboard.copy_text("hello")
        self.assertFalse(ok)
        self.assertIsNone(method)


class TestFileFallback(unittest.TestCase):
    def test_it_writes_a_file_when_there_is_no_clipboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "copied-logs.txt")
            with mock.patch.object(clipboard, "copy_text",
                                   return_value=(False, None)):
                ok, method, got = clipboard.copy_or_save("some log", path)
            self.assertTrue(ok)
            self.assertEqual(method, "file")
            self.assertEqual(got, path)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "some log")

    def test_a_successful_copy_writes_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "copied-logs.txt")
            with mock.patch.object(clipboard, "copy_text",
                                   return_value=(True, "xclip")):
                ok, method, got = clipboard.copy_or_save("x", path)
            self.assertIsNone(got)
            self.assertFalse(os.path.exists(path))

    def test_an_unwritable_path_fails_cleanly(self):
        with mock.patch.object(clipboard, "copy_text",
                               return_value=(False, None)):
            ok, method, got = clipboard.copy_or_save(
                "x", "/definitely/not/a/directory/out.txt")
        self.assertFalse(ok)
        self.assertIsNone(got)

    def test_empty_text_is_not_copied(self):
        self.assertEqual(clipboard.copy_text(""), (False, None))


if __name__ == "__main__":
    unittest.main()
