"""Photograph how this build draws every script the UI has to draw.

    xvfb-run -a python3 -m tools.shoot_text_samples            # Linux
    "%USERPROFILE%\\jms\\venv\\Scripts\\python.exe" -m tools.shoot_text_samples

**The question no test here can answer.** `tests/test_mpvtk_pilfont.py` can
prove a glyph is not the face's own `.notdef`, which is the difference between
a character and a box -- and that is all it can prove. It cannot see text drawn
in the wrong *order*, an Arabic word whose letters failed to *join*, a line
sitting on the wrong baseline, kerning that is subtly absent, or a Traditional
Chinese title drawn with Japanese letterforms. Those are all correct-by-
assertion and wrong on screen, and short of OCR the only instrument is a person
looking. This makes the thing to look at.

Everything is drawn through the shipping path -- `pilfont.font_for` for the
face and `pilfont.draw_text` for the ink, the same two calls
`mpvtk_browser.strips` makes for a tile caption -- so the sheet is evidence
about the build it ran in and not about a private reimplementation.

Each row names the face(s) that actually drew it, because "this looks wrong" is
only actionable with that. The header names the platform, Pillow and whether
Raqm is live: **without Raqm every right-to-left row below is expected to be
wrong**, and a sheet that did not say so would look like a font bug.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Before PIL, always: `load_fribidi` runs once from `PyInit__imagingft`, so
# this is the last moment it can matter. See win_fribidi.
from jellyfin_mpv_shim.win_fribidi import preload, describe  # noqa: E402

preload()

from jellyfin_mpv_shim.mpvtk import pilfont  # noqa: E402

#: ``(group, label, text)`` or ``(group, label, text, expected)``.
#: ``expected`` explains rows whose boxes a reader should not chase, and it
#: must say which of two things it is: a **documented trade** (cite it, with
#: "GUIDE ..." or "BY DESIGN") or a **known gap** (start it "KNOWN GAP"). The
#: distinction is the whole value -- one means stop looking, the other means
#: this is a bug with a ticket's worth of work behind it. A test holds the
#: convention. Ordered so the plain cases come first and the ones that have
#: actually broken come last.
SAMPLES = [
    ("Latin and neighbours", "ASCII", "Blade Runner 2049"),
    ("Latin and neighbours", "accents", "Amélie · Señor · Křižík · Æther"),
    ("Latin and neighbours", "Cyrillic", "Ведьмак · Иван Васильевич"),
    ("Latin and neighbours", "Greek", "Ελλάδα · Οδύσσεια"),
    ("Latin and neighbours", "kerning (AV pairs)", "AVATAR WAVY To Ta Ty"),

    ("CJK -- one bucket, four languages", "zh-Hans (#736's title)",
     "莲花 电视剧 第一季"),
    ("CJK -- one bucket, four languages", "zh-Hans (more)",
     "流浪地球 · 三体 · 战狼"),
    ("CJK -- one bucket, four languages", "zh-Hant",
     "後宮甄嬛傳 · 臺灣 · 鬼滅之刃"),
    ("CJK -- one bucket, four languages", "ja (kanji+kana)",
     "進撃の巨人 · ハウルの動く城"),
    ("CJK -- one bucket, four languages", "ja (katakana)",
     "ドラえもん · エヴァンゲリオン"),
    ("CJK -- one bucket, four languages", "ko (Hangul)",
     "오징어 게임 · 기생충"),
    ("CJK -- one bucket, four languages", "fullwidth forms",
     "（２０１３）　ＡＢＣ　！？"),

    ("Right to left", "Arabic", "مسلسل الحلقة الأولى"),
    ("Right to left", "Arabic + digits", "مسلسل (2013) الجزء 2"),
    ("Right to left", "Hebrew", "שלום עולם"),
    ("Right to left", "Hebrew + punctuation", "הסרט הזה, משנת 2013."),
    ("Right to left", "Hebrew + niqqud", "בְּרֵאשִׁית"),

    ("Other scripts", "Thai", "ภาพยนตร์ไทย"),
    ("Other scripts", "Devanagari", "हिन्दी फ़िल्म"),

    # These had no bucket in `script_of_char` at all, so every codepoint
    # answered "latin", got the Latin face and drew as boxes -- on a box
    # with the full Noto set installed and on a stock Windows 10, both of
    # which ship a face for every one of them. Georgian and Armenian stay
    # in the group because DejaVu happens to cover them, which is what made
    # the rest look like a platform limit rather than a bug.
    #
    # Armenian still has no bucket and does not need one: nothing measured
    # draws it that the Latin chain does not already reach.
    ("Scripts that had no bucket until now",
     "Bengali", "বাংলা চলচ্চিত্র"),
    ("Scripts that had no bucket until now",
     "Tamil", "தமிழ் திரைப்படம்"),
    ("Scripts that had no bucket until now",
     "Telugu", "తెలుగు సినిమా"),
    ("Scripts that had no bucket until now",
     "Gurmukhi", "ਪੰਜਾਬੀ ਫ਼ਿਲਮ"),
    ("Scripts that had no bucket until now",
     "Kannada", "ಕನ್ನಡ ಚಲನಚಿತ್ರ"),
    ("Scripts that had no bucket until now",
     "Sinhala", "සිංහල චිත්‍රපටය"),
    ("Scripts that had no bucket until now",
     "Ethiopic", "አማርኛ ፊልም"),
    ("Scripts that had no bucket until now",
     "Khmer", "ខ្មែរ ភាពយន្ត"),
    ("Scripts that had no bucket until now",
     "Myanmar", "မြန်မာ ဇာတ်ကား"),
    ("Scripts that had no bucket until now",
     "Georgian (DejaVu has it)", "ქართული ფილმი"),
    ("Scripts that had no bucket until now",
     "Armenian (DejaVu has it)", "Հայերեն ֆիլմ"),

    ("Symbols and emoji", "symbol face", "★ 8.1 · ✓ · ▶ · ♪ · ⏸ · ⏭"),
    ("Symbols and emoji", "colour emoji", "🎬 🍿 ⭐ 🎵 📺"),
    ("Symbols and emoji", "emoji ZWJ (one glyph)", "👩‍💻 👨‍👩‍👧 🏳️‍🌈"),
    # Not marked expected: the keycap halves ARE boxes here and that is a
    # real gap, not a trade. U+20E3 is in `_JOINERS` so it rides with the
    # digit before it, which makes the run "latin" -- so the enclosing mark
    # never reaches the emoji face. Left for a reader to judge.
    ("Symbols and emoji", "keycap + variation sel.", "1️⃣ 2️⃣ ☂️ ❤️"),
    ("Symbols and emoji", "symbol orphans (12.4)", "⌒ ⌓ ⎰ 〈 〉"),

    ("Mixes -- where it breaks", "CJK + Latin year",
     "進撃の巨人 (2013)"),
    ("Mixes -- where it breaks", "CJK + Latin words",
     "君の名は。 your name."),
    ("Mixes -- where it breaks", "zh + ja in one line",
     "莲花电视剧 進撃の巨人"),
    ("Mixes -- where it breaks", "CJK + Arabic (RTL wins)",
     "進撃の巨人 مسلسل",
     "BY DESIGN: an RTL line gets ONE face (Pillow cannot reorder bidi "
     "across draw calls) and RTL outranks, so the CJK is what degrades. "
     "Reordered text is a wrong line where tofu is only an ugly one."),
    ("Mixes -- where it breaks", "CJK + Hebrew (RTL wins)",
     "進撃の巨人 שלום",
     "BY DESIGN, same as above -- though Liberation Hebrew happens to "
     "carry the Latin, so only the CJK boxes."),
    ("Mixes -- where it breaks", "Latin + emoji + star",
     "Dune ⭐ 8.1 🎬 (2021)"),
    ("Mixes -- where it breaks", "every script at once",
     "Aあ莲한مسلسلשלוםไทยहिन्दी★🎬",
     "BY DESIGN: it contains RTL, so the whole line is one face and "
     "almost everything else boxes. No face covers this and none can."),
    ("Mixes -- where it breaks", "Arabic + Latin word",
     "مسلسل Netflix الأصلي"),
    ("Mixes -- where it breaks", "CJK + symbol",
     "進撃の巨人 ★ 8.1"),

    # #740: the character the run's face does not have. `script_of_char`
    # calls all of these "latin" -- they are below the CJK catch-all and in
    # none of the symbol tables -- so they are drawn by the Latin face,
    # which on Windows is Arial: 7 of the 64 Number Forms and none of the
    # 160 enclosed alphanumerics, against every CJK face on the same box
    # having them all. The peel moves the one character and leaves the
    # words around it alone, so what a reader is checking here is that the
    # numeral appears AND that nothing beside it changed typeface.
    ("Characters the run's own face lacks", "#740's title",
     "机动战士Z高达Ⅱ：恋人们"),
    ("Characters the run's own face lacks", "Roman numerals, CJK context",
     "高达Ⅱ · 第Ⅲ部 · Ⅳ"),
    ("Characters the run's own face lacks", "Roman numerals, Latin context",
     "Rocky Ⅱ · Final Fantasy Ⅶ"),
    ("Characters the run's own face lacks", "enclosed alphanumerics",
     "① ② ③ ⑩ ⓐ"),
    ("Characters the run's own face lacks", "letterlike and units",
     "№ 5 · 25℃ · ℡ · ™"),
    ("Characters the run's own face lacks", "Hangul jamo (no bucket)",
     "ᄀ ᄁ ᄂ ᅡ ᆨ"),
    ("Characters the run's own face lacks", "orphan inside a Latin word",
     "Blade Runner Ⅱ 2049"),
    ("Characters the run's own face lacks", "orphan beside an emoji",
     "Ⅶ 🎬 ① ⭐"),
    ("Characters the run's own face lacks", "orphan in an RTL line",
     "مسلسل Ⅱ الجزء",
     "BY DESIGN: an RTL line is one draw call and nothing is peeled off "
     "its face -- Pillow reorders bidi within a call and cannot across "
     "several (GUIDE section 12.1)."),
    ("Characters the run's own face lacks", "controls and zero-width",
     "A\u0085B\u200bC\u007fD",
     "BY DESIGN: category C is never peeled, so these draw as whatever the "
     "line's own face does with them, which is nothing. NotoSansSymbols2 "
     "has a picture for every C0/C1 control and must not be handed them "
     "(GUIDE section 12.4)."),
]

#: A codepoint that can never be assigned, so every face draws its own
#: no-glyph mark for it. Printed once at the top as the reference: anything
#: below that looks like THIS is tofu.
TOFU = "\U000FFFFF"


def _faces_for(text, size, bold=False):
    """The face names `draw_text` will actually use, run by run."""
    fnt = pilfont.font_for(text, size, bold)
    pieces, whole = pilfont._split(text, fnt, None)
    if whole is not None:
        used = [whole]
    else:
        used = [face for _script, _chunk, face in pieces]
    names = []
    for f in used:
        name = os.path.basename(str(getattr(f, "path", "") or "builtin"))
        if name not in names:
            names.append(name)
    return fnt, names


def build(size=26, width=1500):
    from PIL import Image, ImageDraw

    label_size = 15
    note_size = 13
    row_h = int(size * 2.4)
    # Budget exactly what the row loop adds for an explained row, or the
    # last rows fall off the bottom of the canvas.
    explained = sum(12 + max(0, (len(r[3]) // 74)) * 16
                    for r in SAMPLES if len(r) > 3)
    head_h = 150
    groups = []
    for row in SAMPLES:
        if row[0] not in groups:
            groups.append(row[0])
    height = (head_h + len(SAMPLES) * row_h
              + len(groups) * int(size * 1.9) + explained + 80)

    img = Image.new("RGB", (width, height), (24, 24, 28))
    draw = ImageDraw.Draw(img)

    def put(x, y, text, px, fill, bold=False):
        fnt = pilfont.font_for(text, px, bold)
        pilfont.draw_text(draw, (x, y), text, fnt, fill=fill)

    y = 14
    put(20, y, "Text rendering sample sheet -- jellyfin-mpv-shim", 24,
        (255, 255, 255), bold=True)
    y += 34
    from PIL import features
    import PIL

    put(20, y, "platform %s   Pillow %s   %s" % (
        sys.platform, PIL.__version__, describe()), note_size, (170, 175, 185))
    y += 20
    put(20, y, "raqm=%s  fribidi=%s  harfbuzz=%s   <- with raqm=False every "
        "right-to-left row below is EXPECTED to be wrong (unjoined, "
        "unreordered) and unkerned" % (
            features.check("raqm"), features.version("fribidi"),
            features.version("harfbuzz")), note_size, (170, 175, 185))
    y += 22
    put(20, y, "reference: an unassigned codepoint, i.e. what tofu looks "
        "like here ->", note_size, (170, 175, 185))
    put(560, y - 4, TOFU * 4, size, (255, 120, 120))
    y += 28
    put(20, y, "each row: label / the text as the UI draws it / the face(s) "
        "that drew it", note_size, (140, 145, 155))
    y += 30

    current = None
    for row in SAMPLES:
        group, label, text = row[0], row[1], row[2]
        expected = row[3] if len(row) > 3 else None
        if group != current:
            current = group
            y += int(size * 0.7)
            put(20, y, group, 19, (120, 200, 255), bold=True)
            draw.line([(20, y + 26), (width - 20, y + 26)],
                      fill=(60, 62, 70), width=1)
            y += int(size * 1.2)
        put(24, y + 6, label, label_size, (150, 155, 165))
        try:
            fnt, names = _faces_for(text, size)
            pilfont.draw_text(draw, (330, y), text, fnt, fill=(240, 240, 245))
            note = " + ".join(names)
        except Exception as exc:                     # noqa: BLE001
            note = "%s: %s" % (type(exc).__name__, exc)
            put(330, y, "<raised>", size, (255, 90, 90))
        put(1080, y + 6, note[:58], note_size, (130, 170, 130))
        if not expected:
            y += row_h
            continue
        words, line, lines = expected.split(), "", []
        for w in words:
            if len(line) + len(w) + 1 > 74:
                lines.append(line)
                line = w
            else:
                line = (line + " " + w).strip()
        lines.append(line)
        gap = expected.startswith("KNOWN GAP")
        # Red for a gap, amber for a trade: a reader must be able to tell
        # "stop looking" from "this one is real" at a glance.
        colour = (235, 120, 120) if gap else (200, 170, 110)
        lead = "" if gap else "boxes here are expected -- "
        # **Below the sample's descender, not beside it.** `draw_text`
        # anchors "la", so the sample occupies roughly one full ascent plus
        # descent from `y`; at `size * 1.05` the note landed on top of the
        # Arabic it was explaining.
        note_y = y + int(size * 1.55)
        put(330, note_y, lead + lines[0], note_size - 1, colour)
        for i, extra in enumerate(lines[1:], start=1):
            put(330, note_y + i * 16, extra, note_size - 1, colour)
        y += row_h + 12 + max(0, len(lines) - 1) * 16

    return img


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--out", default=None,
                    help="output PNG (default: text-samples-<platform>.png "
                         "in the current directory)")
    ap.add_argument("--size", type=int, default=26,
                    help="pixel size for the sample text (default 26)")
    args = ap.parse_args(argv)

    out = args.out or ("text-samples-%s.png" % sys.platform)
    img = build(size=args.size)
    img.save(out)
    print("wrote %s (%dx%d)" % (out, img.width, img.height))
    print(describe())
    for (script, bold), name in sorted(
            pilfont._resolved.items(), key=lambda kv: str(kv[0])):
        print("  resolved %-11s bold=%-5s -> %s"
              % (script, bold, os.path.basename(str(name)) if name else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
