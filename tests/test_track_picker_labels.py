"""What a track picker is allowed to say, and how wide it may say it (#26).

Both halves are the same problem seen twice: **the text in these lists is
not ours**. A subtitle's DisplayTitle is whatever the person who made the
file called it, and a version name is the user's own directory or edition
label. Compose the label ourselves and two different tracks come out
identical; draw it at the control's width and every row ellipsizes to the
same prefix. Either way the picker offers a choice it cannot express.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.utils import get_sub_display_title           # noqa: E402


class SubtitleLabelTest(unittest.TestCase):
    def test_the_servers_own_title_wins(self):
        """The whole distinction between "Signs & Songs" and "Full" on a
        release that ships both — composed from Language/Forced/Codec they
        are the same string."""
        signs = {"DisplayTitle": "Signs & Songs - English - SUBRIP",
                 "Language": "eng", "Codec": "subrip"}
        full = {"DisplayTitle": "Full - English - SUBRIP",
                "Language": "eng", "Codec": "subrip"}
        self.assertNotEqual(get_sub_display_title(signs),
                            get_sub_display_title(full))
        self.assertEqual(get_sub_display_title(signs),
                         "Signs & Songs - English - SUBRIP")

    def test_the_composed_form_is_still_the_fallback(self):
        # An offline item rebuilt from the local catalog, or an older
        # server: no DisplayTitle, and the label must not come out empty.
        self.assertEqual(
            get_sub_display_title({"Language": "eng", "Codec": "subrip"}),
            "Eng (subrip)")

    def test_forced_is_not_doubled(self):
        """The server already puts "Forced" in DisplayTitle, so the
        composed suffix must not be added on top of it."""
        label = get_sub_display_title(
            {"DisplayTitle": "English - Forced - SUBRIP", "IsForced": True,
             "Language": "eng", "Codec": "subrip"})
        self.assertEqual(label.lower().count("forced"), 1)

    def test_a_blank_display_title_falls_back(self):
        self.assertEqual(
            get_sub_display_title({"DisplayTitle": "   ", "Language": "jpn",
                                   "Codec": "ass"}),
            "Jpn (ass)")

    def test_a_null_language_does_not_crash(self):
        """`stream.get("Language", ...)` returned None for a stream that
        HAS the key set to null, and .capitalize() then raised — a latent
        crash on any file whose subtitle track is untagged."""
        self.assertEqual(
            get_sub_display_title({"Language": None, "Codec": "subrip"}),
            "Unkn (subrip)")


class PickerWidthTest(unittest.TestCase):
    """The open list may be wider than the control; the control may not."""

    def _row(self):
        from jellyfin_mpv_shim.mpvtk_browser.pages.detail import DetailPage

        return DetailPage._picker_row(
            "Subtitle", "dt-sub",
            ["Signs & Songs - English - SUBRIP",
             "Full - English - SUBRIP"], 0, lambda i, v: None)

    def test_the_popup_is_allowed_to_be_wider(self):
        dd = self._row().children[1]
        self.assertGreater(dd.popup_w, dd.w,
                           "the open list must be able to outgrow the "
                           "control, or long titles all ellipsize alike")

    def test_the_control_itself_is_unchanged(self):
        # Widening it would put these rows out of line with the rest of the
        # page, and it is closed almost all of the time.
        self.assertEqual(self._row().children[1].w, 300)

    def test_every_picker_gets_it(self):
        """Version, Audio and Subtitle all carry text from outside, so the
        allowance belongs to the shared row builder rather than to one."""
        import inspect

        from jellyfin_mpv_shim.mpvtk_browser.pages import detail

        src = inspect.getsource(detail.DetailPage._track_pickers)
        self.assertEqual(src.count("self._picker_row("), 3)
        self.assertIn("popup_w",
                      inspect.getsource(detail.DetailPage._picker_row))


if __name__ == "__main__":
    unittest.main()
