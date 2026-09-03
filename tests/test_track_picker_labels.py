"""What a track picker is allowed to say, and how wide it may say it (#26).

Both halves are the same problem seen twice: **the text in these lists is
not ours**. A subtitle's DisplayTitle is whatever the person who made the
file called it, and a version name is the user's own directory or edition
label. Compose the label ourselves and two different tracks come out
identical; draw it at the control's width and every row ellipsizes to the
same prefix. Either way the picker offers a choice it cannot express.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

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


class RealServerStringsTest(unittest.TestCase):
    """The strings a real Jellyfin actually sends, from the QA server's Test
    Media library. Measured, not invented — the first version of this file
    guessed at DisplayTitle's shape because the library I searched had no
    subtitle streams; **[iw]** pointed at the one that does."""

    #: (DisplayTitle, Title, Language, Codec) exactly as returned.
    REAL = [
        ("Styled - English - Default - ASS", "Styled", "eng", "ass"),
        ("English - SUBRIP", None, "eng", "subrip"),
        ("Spanish - SUBRIP", None, "spa", "subrip"),
        ("English - SUBRIP - External", None, "eng", "subrip"),
        ("English - DVBSUB", None, "eng", "DVBSUB"),
        ("Greek - SUBRIP - External", None, "Greek, Modern (1453-)",
         "subrip"),
    ]

    @staticmethod
    def _stream(display, title, lang, codec):
        return {"DisplayTitle": display, "Title": title, "Language": lang,
                "Codec": codec, "Type": "Subtitle"}

    def test_the_authors_own_title_survives(self):
        """The case this change exists for, in real data: an ASS track named
        "Styled". Composed from Language/Codec it is "Eng (ass)" — the same
        string every other ASS track in the file would get."""
        styled = self._stream(*self.REAL[0])
        self.assertEqual(get_sub_display_title(styled),
                         "Styled - English - Default - ASS")
        self.assertNotIn("Eng (ass)", get_sub_display_title(styled))

    def test_every_real_track_is_distinguishable(self):
        labels = [get_sub_display_title(self._stream(*r)) for r in self.REAL]
        self.assertEqual(len(labels), len(set(labels)))

    def test_a_full_language_name_does_not_break_the_fallback(self):
        """The server sends Language='Greek, Modern (1453-)' for one of
        these — a name, not a code. It only reaches the composed form when
        DisplayTitle is absent, but it must not raise there."""
        s = self._stream(*self.REAL[5])
        del s["DisplayTitle"]
        self.assertIn("Greek", get_sub_display_title(s))


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
