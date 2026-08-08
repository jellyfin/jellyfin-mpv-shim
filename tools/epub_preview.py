#!/usr/bin/env python3
"""Render pages of an epub to PNGs, without the app or a window.

The reader's whole layout engine is pure Python that measures through
Pillow, so it can be driven from a shell — which is how it gets looked at.
A screenshot of the running app tells you the plumbing works; this tells you
whether the *typography* does, at whatever page size you care about, in a
second rather than a session.

    tools/epub_preview.py BOOK.epub --pages 1-6 --out /tmp/preview
    tools/epub_preview.py BOOK.epub --at 0.42 --size 900x1200 --theme sepia
    tools/epub_preview.py BOOK.epub --stats

``--stats`` prints the locations index and per-chapter pagination instead of
drawing anything, which is the quick way to see whether a book parsed at
all.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jellyfin_mpv_shim.epub import layout, paint          # noqa: E402
from jellyfin_mpv_shim.epub.book import EpubDocument      # noqa: E402


def parse_range(text, total):
    if not text:
        return list(range(min(4, total)))
    out = []
    for part in text.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            out += list(range(int(lo) - 1, int(hi)))
        else:
            out.append(int(part) - 1)
    return [i for i in out if 0 <= i < total]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("book")
    ap.add_argument("--out", default="/tmp/epub-preview")
    ap.add_argument("--size", default="820x1100",
                    help="page bitmap size, WxH (default 820x1100)")
    ap.add_argument("--pages", default="", help="e.g. 1-6 or 1,4,9")
    ap.add_argument("--spine", type=int, default=None,
                    help="spine document to page through (default: the "
                         "first with text)")
    ap.add_argument("--at", type=float, default=None,
                    help="start at a stored fraction (0..1), as a resume "
                         "would")
    ap.add_argument("--font", type=int, default=21)
    ap.add_argument("--kind", default="serif", choices=["serif", "sans"])
    ap.add_argument("--spacing", type=float, default=1.5)
    ap.add_argument("--theme", default="light", choices=sorted(paint.PALETTES))
    ap.add_argument("--no-justify", action="store_true")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    width, height = (int(v) for v in args.size.lower().split("x"))
    style = layout.ReaderStyle(font_px=args.font, font_kind=args.kind,
                              line_spacing=args.spacing,
                              justify=not args.no_justify)
    started = time.time()
    doc = EpubDocument(args.book, style)
    column = (width - 2 * style.margin_x, height - 2 * style.margin_y)
    doc.set_viewport(*column)
    print("%s — %s (%d spine documents), opened in %.0f ms"
          % (doc.title or "(untitled)", doc.author or "(unknown author)",
             doc.spine_count, (time.time() - started) * 1000))

    started = time.time()
    index = doc.build_index()
    print("locations index: %d locations, total=%d, %.0f ms"
          % (index.count, index.total, (time.time() - started) * 1000))

    if args.stats:
        for chapter in doc.chapters()[:40]:
            print("  %s%-52s spine %s"
                  % ("  " * chapter.level, chapter.title[:52],
                     chapter.spine_index))
        for section in index.sections[:40]:
            doc.goto(section.spine_index, 0)
            print("  spine %-3d %8d chars %5d locations %4d pages"
                  % (section.spine_index, section.chars, section.count,
                     doc.page_count()))
        return 0

    if args.at is not None:
        doc.goto_fraction(args.at)
        print("resumed at fraction %.4f -> spine %d offset %d"
              % (args.at, doc.spine_index, doc.char_offset))
    elif args.spine is not None:
        doc.goto(args.spine, 0)
    else:
        for section in index.sections:
            if section.chars > 2000:
                doc.goto(section.spine_index, 0)
                break

    colors = paint.palette(args.theme)
    os.makedirs(args.out, exist_ok=True)
    wanted = parse_range(args.pages, 10_000) or [0]
    count = max(wanted) + 1
    started = time.time()
    pages = doc.pages(doc.spine_index)
    paginate_ms = (time.time() - started) * 1000
    print("spine %d: %d pages in %.0f ms (%s)"
          % (doc.spine_index, len(pages), paginate_ms,
             doc.chapter_title() or "no chapter title"))

    written = []
    for offset in range(count):
        if offset:
            if not doc.next_page():
                break
        if offset not in wanted:
            continue
        started = time.time()
        image = doc.render((width, height), colors)
        path = os.path.join(args.out, "page%02d.png" % (offset + 1))
        image.save(path)
        written.append(path)
        fraction = doc.fraction()
        print("  %s  spine %d page %d/%d  offset %d  %s  %.0f ms"
              % (os.path.basename(path), doc.spine_index,
                 doc.page_number + 1, doc.page_count(), doc.char_offset,
                 ("%.2f%%" % (fraction * 100)) if fraction is not None
                 else "-", (time.time() - started) * 1000))
    print("wrote %d page(s) to %s" % (len(written), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
