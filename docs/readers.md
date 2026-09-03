# Books, and the two readers

Everything about how this app handles `CollectionType.books`: the two entity
types it holds, the progress wire format they share and disagree about, how a
books library browses and downloads, and the two in-window readers — the epub
reader that draws its own pages and the comic reader that hands them to mpv.

The code states each rule at the line that depends on it. This file is where the
reasoning, the measurements and the bugs that established the rules live, so the
modules do not have to carry them. Source comments cite it by section number.

Primary sources, if you are going deeper: `books.py`'s module docstring is the
statement of the data model, `epub/__init__.py` names the reader's layers, and
`comic.py`'s and `gateway/picture.py`'s docstrings argue the play-don't-draw
decision.

## 1. What a book is

`CollectionType.books` holds **two unrelated entity types**, and almost every
rule in this file exists because of that split. They need opposite things.

**`AudioBook : Audio` is an ordinary audio item.** Real `MediaSources`, real
duration, real transcode negotiation, the whole ffmpeg pipeline and the whole
existing progress-reporting path. It needed no playback work at all — only to be
recognised.

**`Book : BaseItem` is not `IHasMediaSources`.** Its DTO carries no
`MediaSources`, no `Container` and **no size under any `Fields` value**
(measured, not assumed). `GET /Items/{id}/Download` is the only endpoint that
yields its bytes, and there is nothing that serves a page, an archive entry or a
spine document — which is why inline reading from the server was rejected rather
than deferred, and why everything drawn in the window is drawn from a local copy.

Consequences that fall straight out of that:

- **Format comes from `Path`**, because a `Book` DTO states it nowhere else.
  That is what jellyfin-web reads too (`bookPlayer` gates on
  `item.Path?.endsWith('epub')`), and `Path` *is* served to non-admins under
  `Fields=Path` — verified against a non-admin account, which is what makes it
  viable rather than an admin-only trick. `books.book_format` returns `None`
  rather than guessing for an unreadable path: inventing `.epub` puts the wrong
  extension on a downloaded file and hands the wrong application a PDF. The
  download's `Content-Disposition` then confirms it, because for a book the
  extension is not cosmetic — it is what routes the file to an application.
- **The download-size estimate reports books as `unsized_count`**, not as 0 B.
- `BOOK_EXTENSIONS` is what `BookResolver` accepts. `.cba` is deliberately
  absent: it exists only as a MIME mapping on the server and is invisible to the
  library, so a file with that extension is not a `Book` at all.

**Which formats open in the window** is `books.IN_WINDOW_FORMATS` / `reader_route`
— the single place that answers "can we draw this ourselves", so a book's own
page and the tile context menu cannot drift apart about it. `epub` opens the
reader (§4), `cbz`/`cbt` open the comic reader (§5). Everything else answers
`None` and is handed to the desktop (`system_open.py`), for reasons that differ
per format: a PDF needs a page rasterizer that costs a heavy dependency (Jellyfin
12's PDFtoImage probe is for page *counts*); mobi and azw are formats nothing in
the Jellyfin ecosystem opens; cbr and cb7 are RAR and 7-Zip, which Python does
not ship a reader for. All of them still Read — the button means "open this
book", and off the machine is where it opens.

`system_open.py` launches the handler **detached and never waits on it**. A
reader is a long-lived GUI application; `subprocess.run` would block the caller —
on Linux, the browser's worker pool — for as long as the user reads the book.
`xdg-open` exits immediately but the handlers underneath it do not always, and
one that inherits our pipes and never exits is indistinguishable from a hang.
Nothing there raises; callers get `(ok, method)` and decide what to say, and
"opened it" means "handed it over", which is as much as any launcher can promise.

## 2. Progress, and the `RunTimeTicks` wire format

`RunTimeTicks` is **overloaded as a fake progress unit and the encoding depends
on the format**, so one number means three different things:

| format | `RunTimeTicks` | `PlaybackPositionTicks` |
|---|---|---|
| comic / pdf | `page_count * 10000` | `page_index * 10000` (zero-based) |
| epub | `10_000_000` (one second) | `location / locations` as a fraction |
| mobi / azw | absent | meaningless |

Both spellings are pinned against the two implementations that define them:
`ProbeProvider.FetchAsync(Book)` writes the durations, and jellyfin-web's players
write the positions through `PositionTicks = 10000 * player.currentTime()`, where
`comicsPlayer` and `pdfPlayer` report a page index and `bookPlayer` reports
`fraction * 1000`. The server's own comments call this a placeholder for
"multiple progress types". Treat it as a wire format to interoperate with, never
as a design to build on.

**All of `books.py` exists to keep that in one place, and the off-by-one is the
whole risk**: the stored paged value is a zero-based *index*, so page 1 is 0
ticks, and getting it wrong puts the shim exactly one page behind every other
client — the kind of wrong that reads as a rounding quirk. `progress_of` returns
`(mode, value, total)` in the mode's own units (1-based pages, whole percent);
`ticks_for_page`, `ticks_for_percent` and `ticks_for_fraction` are the inverses.

`progress_mode` is decided by **format, not by the value**: a book that has never
been opened has a position of 0 under every encoding, and a mobi with no runtime
is not "at 0%" — it has no notion of progress to be at. `page_count` returns
`None` both for a format with no pages and for a paged format the server could
not count (page counts need the PDFtoImage probe that landed in Jellyfin 12.0, so
a 10.11 server returns no runtime for PDFs or comics at all); both mean the same
thing to a caller — there is no denominator to show.

### 2.1 Why an epub's number cannot be asked for

An epub's stored number is `location / total` over **epub.js's locations index**:
the book's text cut into ~1024-character runs, counted per spine section with a
partial tail at each boundary (§4.3). Two consequences, both fatal to a "type
where you are" control:

- the denominator is a property of how the book was *typeset into sections*, not
  of its length, so the same fraction means different amounts of reading in
  different books — and it is not `chars_read / chars_total` either;
- nothing on screen in any reader shows it. A page number is something a PDF
  viewer puts in front of you; a location index is an implementation detail of
  one JavaScript library, so the user has no number to read off and no way to
  check the one we would show them.

So the fraction is **read, shown, and never asked for**. That is what
`progress_settable` answers — and it is a question about the *manual* Pull/Push
dialog only, which is therefore offered for paged formats alone. It does not gate
the built-in reader, which computes the number rather than guessing at it (§4.3).

### 2.2 A page turn is not a typed value

**A page turn goes through `record_reading_position`, not `set_position`**
(`gateway/userdata.py`). `set_position` is the manual Progress… dialog: a value
the user typed, which may legitimately go backwards, and is deliberately *not*
queued. A page turn is the equivalent of a playback progress report and needs
what those get, because **a downloaded book is the one thing that can be read
with the server away** — before this existed, an offline page turn was written
nowhere at all, reopening the book offline started it from page one, and nothing
was ever sent on reconnect.

Three places, in a fixed order, and the order is the point:

1. **The local catalog, verbatim** (`sync.db.set_reading_position`, kept apart
   from `update_userdata` because every caller of that one is playback), so
   re-opening offline lands where the reading stopped.
2. **The server**, which is the whole story while online.
3. **The replay queue, only if the server refused.** Queueing unconditionally
   would be worse than not queueing: the queue is advance-only, so an entry left
   behind after a successful write would later be replayed and undo a page turn
   that went backwards.

The two semantics are deliberate and opposite — locally a cursor, on the wire a
high-water mark — so a client that has been offline cannot rewind where another
device reached. An `AudioBook` needs none of this: it is ordinary playback, and
`offline_media` already records it.

### 2.3 The server's audiobook resume rule is stated in MINUTES

`UserDataManager.UpdatePlayState` has an `AudioBook` arm, and it is not the video
rule: `MinAudiobookResume` (**5 minutes**) discards a position less than five
minutes in, and `MaxAudiobookResume` (**5 minutes**) discards one with less than
five minutes left *and marks the book finished*.

So **an audiobook under ten minutes can hold no resume position at all**, and one
under five cannot even be finished by playing it — only by marking it. This is
why stdjflib grew `The Overnight Vigil` (24 min) and `The Slow Crossing`
(3 × 12 min): the original 4-minute and 20-second fixtures make every resume test
fail in a way that reads as a client bug. `tests/e2e/test_audiobooks.py` pins the
rule, both fixtures, and the round trip through a real mpv.

A `Book` is excluded from **both** arms and stores its position verbatim.

## 3. Browsing and downloading a books library

**A books library browses by folder.** That is jellyfin-web's own default tab
(`constants/views/books.ts`: slot 0 is Folders) and, more to the point, the only
structure these libraries have: `SeriesName` is populated for books and **null
for audiobooks**, `Album` is the reverse and is tag-derived, so an untagged rip
has *nothing* joining its files but the directory they sit in. The folder is
therefore also the download unit, and the only container `sync.manager._expand`
expands by listing.

The two screens are not one screen (`mpvtk_browser/pages/books.py`):

- **`BooksPage`** is a `GridPage` with one extra question asked at render time —
  are all the loaded children AudioBooks, *and is all of it loaded?* — and draws
  an album (header, action bar, tabular track list) if so. The "all of it loaded"
  half matters: a windowed list is full of holes, and playing "from track 4" out
  of one queues blanks.
- **`BookPage`** is a download-and-open screen, and admits it. A book cannot be
  played, streamed, paged or partially fetched.

**Resume is the whole point on a book, so there is no bare Play once one is
started.** The position is hours of listening spread over weeks, and playing from
chapter one overwrites it as it goes, so the second button says *Play from
Beginning*. For the same reason a books library gets neither Play All nor Shuffle
(`pages/grid.py:NO_LIBRARY_PLAY`): half of one cannot be queued at all and the
other half would be every chapter of every book, from the start. A folder's own
progress comes from `PlayedPercentage` (a container has no position of its own),
and `is_watched` counts a `Folder` finished at `UnplayedItemCount == 0` — nothing
sets `Played` on one when you simply listen through it.

**An audiobook is told from a song by `is_audiobook` on the playstate snapshot**,
because `is_audio` cannot: an AudioBook *is* an Audio item. Chapters ride that
same snapshot rather than being read per frame — the video HUD asks the player
while it draws, which is fine because it is only up during playback, but the
audio bar is on screen for as long as the browser is, and every jsonipc property
read is an IPC round trip.

**The now-playing bar sheds by measured cost and priority, not by width
thresholds** (`MusicMixin._np_plan`). It is a fixed-height flex row, so every
control added to it comes out of the scrubber — and the scrubber is the one thing
on it that must be *draggable*. A per-control threshold table looked right and
was wrong: the bar satisfied its own table at every width and still laid out a
zero-pixel slider, because nothing checked the total. The plan keeps the longest
**prefix** of a priority list that fits (a prefix, so narrowing only ever removes
and widening only ever restores, in the same order), and the priority differs by
what is playing — chapter navigation outranks the favourite heart while a book is
on and does not exist while a song is. `tests/test_shell_books.py` asserts the
laid-out scene at ten widths rather than the table.

### 3.1 Offline, the shelf is rebuilt from leaves

`sync.manager._expand` lists a folder and downloads its children, so the catalog
holds **no folders** — and the two halves of a books library disagree about which
field would put them back together. Measured against a real server under the
`Fields` the downloader asks for: a `Book` carries `SeriesName` (the folder, or a
real series when tagged) and no `Album`; an `AudioBook` carries
`Album`/`AlbumArtist` and no `SeriesName`; **neither carries `ParentId`**.

So `OfflineLibrarySource._book_shelf` rebuilds from what is actually there, and
only where it changes what can be done:

- a **`Book`** stands alone — one file, one thing to read, exactly as it does in
  a folder online;
- **`AudioBook`s sharing an album** become a synthesized `Folder`, which is the
  case that needs it: otherwise a twelve-part recording is twelve tiles and
  starting it means picking a chapter;
- a **lone or untagged recording** is left loose, because a container around one
  chapter is a click that leads to the same thing.

Grouping is on `AlbumId` where the server gave one and on the album name
otherwise: the id is stable across a retag, and the name is all an untagged rip
has. Members sort on `IndexNumber` first, because that is the chapter order and
the names of a rip are frequently "Track 01" … "Track 10", which sort wrong as
text.

The container's `CollectionType: "books"` on the library is load-bearing rather
than decorative: the shell routes on it, and books is the one collection type it
*inherits* down the tree, which is what carries the container to the page that
draws an album.

## 4. The epub reader

`jellyfin_mpv_shim/epub/` is a layered pure-Python package, and
`mpvtk_browser/pages/reader.py` is the screen: **one bitmap and two bars**. The
bitmap is the page, rasterized at the exact size it will be drawn (mpv never
resamples an overlay — mpvtk GUIDE §5) and handed to the scene as a single
`Image`, the same transport the tile strips use, with the same cache and LRU.
Nothing about the book's typography is expressed as toolkit nodes.

The layers, bottom up, each knowing only about the ones below it and none of them
importing the UI: `xmlish` (tolerant markup), `archive` (zip, package document,
spine, TOC), `css` (the declarations that decide whether a line is a chapter
title), `content` (XHTML → blocks and styled runs, carrying character offsets),
`locations` (epub.js's index), `fonts`, `layout` (blocks → pages), `paint` (a
page → one bitmap), `book` (an open book that knows where it is). Per the
optional-dependency rule in CONTRIBUTING.md the package needs **Pillow**, which
the browser already requires, and nothing else outside the standard library.

Everything expensive happens on the worker pool — opening the archive, building
the locations index, paginating a chapter, rasterizing a page. The loop thread
only ever reads what those left behind.

### 4.1 Nothing parses XML with expat

`xml.etree` is expat underneath, and expat expands internal entities: **measured
on CPython 3.13, a fourteen-line billion-laughs document parses to a
3000-character string at three levels of nesting** and to gigabytes at nine. The
file comes off a media server that got it from whatever the user put in their
library, so it is not ours to trust. `epub/xmlish.py` is `html.parser`, which
never processes a DTD at all — a `<!DOCTYPE …>` internal subset arrives as one
opaque string through `handle_decl` and is dropped — so the whole class of attack
is absent rather than mitigated, and it cannot fetch an external DTD either.

The second reason is as strong: **real epubs are not well-formed.** Unclosed
`<p>`, bare `&`, stray `<br>`, mismatched nesting and the occasional Windows-1252
byte are routine in shipped books, and an XML parser is *required* to stop dead
on each. A reader that refuses a quarter of a library is not a reader.
`html.parser` recovers, which is the bet every browser makes and the one
jellyfin-web makes by handing the file to one. What it costs: no namespace
resolution (handled by matching on the *local* name — `package`, `item`,
`itemref`), no CDATA, no validation.

**Every read is bounded by what it delivers, never by the size the header
claims.** A zip entry declares its own uncompressed size in a header nobody
verifies, and a few kilobytes of zeroes expands to gigabytes, so `epub/archive.py`
caps by actually reading a byte past the cap rather than trusting
`ZipInfo.file_size`. Caps differ by what the entry is for: a spine document that
needs 32 MB is not a chapter, and an image that needs 24 MB is not going to be
drawn on a screen. Entry names are resolved and re-checked to be inside the
archive; nothing here writes one out, but the check is one line.

### 4.2 Published epubs contain no `<h1>`

They are `<p class="chaptertitle">` plus a stylesheet. The first real book tested
against this reader — a Wiley technical title, 122 entries, 29 spine documents —
contains not one `<h1>`; its chapter openers are `<p class="chaptertitle">` and
its section headings are `<p class="h1">`. That is not unusual, it is how almost
every professionally produced epub is built, because the production chain is a
word processor and a conversion script. Without a stylesheet reader, "support
headings" delivers headings for hand-written epubs and a wall of identical
paragraphs for the published ones.

So `epub/css.py` reads the handful of declarations that decide whether a line is
a chapter title (`USED_PROPERTIES`: size, weight, style, family, alignment,
decoration, indent, vertical margins). It implements **specificity and source
order and nothing else** — no inheritance engine, no computed-value pass, no
layout properties. Supported: type/class/id selectors, compounds, descendant and
child combinators, selector lists, `<style>` blocks and linked stylesheets.
Ignored: pseudo-classes and elements, attribute selectors, `!important`,
`@media` and every other at-rule, sibling combinators. **A selector it cannot
parse is dropped, not guessed**, because applying a heading's size to a page of
body text is far worse than not applying it.

The document model (`epub/content.py`) is deliberately shallow — a spine document
becomes a flat list of `Block`s, and a text block a list of `Span`s. `<script>`
and `<style>` content is not drawn but **is counted** (§4.3); `<table>` is
flattened to one paragraph per row, because a wrong-looking table beats a missing
one and beats a layout engine nobody will maintain.

### 4.3 The stored position is epub.js's locations index, reimplemented

`epub/locations.py` is a port of *behaviour*, written against
`node_modules/epubjs/src/locations.js` — not inferred. epub.js cuts each linear
spine document into runs of ~1024 characters of text and counts the runs; the
position is the number of complete runs before you. **Four details make the
numbers agree and none of them is what you would write:**

1. the counter **resets at every section boundary**, and each section closes its
   partial tail as a whole location **unconditionally** — so a section ending
   exactly on a boundary contributes a degenerate extra location. This is the big
   divergence from `chars_read / chars_total`: a book of many short sections
   inflates its total, one long novel barely at all;
2. a run that **starts inside a text node consumes 1025 characters, not 1024** —
   `dist` is computed before the `counter === 0` branch does `pos += 1`. A run
   continued from the previous node consumes exactly `dist`;
3. a text node that is **entirely whitespace counts zero**; any other counts its
   full length, whitespace included;
4. **`total` is `len(locations) - 1`**, which puts the denominator one short. It
   only matters for a short book, where it matters a lot.

Non-linear spine items (`linear="no"` — covers, footnote documents) are excluded
entirely, as they are in epub.js. Granularity is **absolute, not relative**:
~1024 characters per step in every book, so a 600 KB novel gets 0.17% steps and a
20 KB short story gets 5%.

Getting any of this wrong fails invisibly — the reader still works, it just
reports a percentage no other client agrees with. That count cannot be recovered
from the normalized text in a `Span` (normalization is lossy), which is why
`content.py` records it during the walk, when both are in hand.

**This is what changed "epub progress is not settable."** That rule was about the
*manual* control, and its argument was that no reader shows the user a number to
type; a reader that observes its own progress is not subject to it. So the reader
writes position back on every turn via `books.ticks_for_fraction` — exact, not
whole percent, because 1% of a novel is several pages. `progress_settable` still
answers no, and still means "do not ask the user".

### 4.4 Position is a character offset; the page number is derived

Position is `(spine_index, char_offset)` and **everything else is derived**: the
page number, the fraction reported to the server, the chapter title. That is what
makes a resize a non-event — re-measure, re-paginate, find the page containing
the offset, carry on. A reader that stored "page 74" would have to answer what
page 74 means at a different window size, and there is no answer.
`tests/test_epub_layout.py` asserts this over **repeated** re-layouts, not once:
the one-step version passes while each resize nudges the position.

Pagination is per spine document and **a document always starts a page** — what
every reader does, what a `page-break-before` on a chapter would force anyway,
and what makes opening a book cheap: only the current document is parsed,
measured and paginated, so a 1.2 MB book opens in the time its first chapter
takes. The cost is that "page 4 of 312" cannot be answered without paginating
everything, which is why the reader shows a fraction of the book (from
`locations.py`) and a page number within the chapter.

Caches key on content, exactly: a parsed section on the spine index, its
pagination additionally on the layout (font, size, spacing, column width), so
nothing survives a change that would move a line. One lock guards the mutable
state, because the UI calls in from the loop thread (what page am I on) and from
a worker (open, index, paginate).

### 4.5 The archive holds no open file handle

Every read in `epub/archive.py` opens the zip, takes what it came for and closes
it; only the name table is kept. That is not frugality about descriptors, it is
what **deletes a lifecycle**: a reader route can sit in the browser's forward
history indefinitely, and a handle held that long has to be closed on leaving the
page, on the history being dropped, on the window closing, on the download being
deleted — miss one and it leaks, and miss one on **Windows** and the handle is a
*lock*, so the downloads screen cannot delete a book that has been read.

Measured: reopening costs **0.15 ms on a 122-entry book**, a page turn does no
reads at all, and a chapter change does one. `EpubDocument.close()` survives, but
it is now only about memory.

### 4.6 The page is drawn with Pillow, not libass

Four reasons compound (`epub/paint.py`), and a page turn is one `Image`:

1. **overlay bitmaps composite above all script ASS** (mpvtk GUIDE §6), so every
   illustration would draw over the paragraph it sits beside — and the escape
   hatch, an occluder rect, works per node rather than per line of a reflowing
   page;
2. **one face per scene**: the toolkit measures and renders one UI font, while
   bold, italic, bold-italic and monospace in one paragraph is the ordinary case
   in a book;
3. **line breaking would have to agree with libass exactly** — layout decides
   where lines break, libass decides where glyphs land, and any drift shows up as
   a justified line overflowing its column;
4. **a page is one bitmap either way**, against several hundred text nodes pushed
   as JSON on every turn.

Text on the page cannot be selected or hit-tested, which for a reader is not a
cost (see §4.9 for what stands in). Page turns come from a **claimed key**
(`MpvtkApp.claim_keys`, GUIDE §3) plus click zones: LEFT/RIGHT on a page whose
content is one bitmap mean "turn", and spatial navigation has nothing to move
between.

`epub/fonts.py` resolves faces by *trying to load files*, most-preferred first,
and every failure degrades — no italic face means italic draws regular, no face
at all means Pillow's built-in bitmap font. Nothing there raises: a missing font
on someone's system must not be the difference between a book opening and an
error screen. **The body face is a serif** — not a house-style preference, but
what a page of continuous prose was cut for and what every published book in the
library was typeset in; the chrome around it stays in the toolkit's sans.
Non-Latin scripts fall back to `mpvtk.pilfont`, which offers regular and bold
only, so a Japanese book renders italic as regular — which is what Japanese
typography does anyway. Its `"symbol"` script is the one answer `face()` does
**not** take: that is what pilfont says about a string merely *containing* a
star or an arrow, and a book whose title has one would otherwise be set in a
symbol face from cover to cover.

### 4.7 Type size, colour and the measure

**Type size and page colour are settings, not page state** (`conf.reader_font_size`
= 21, `reader_theme`, `reader_justify`). The reader's own A−/A+ and colour
buttons write them through `config.set_setting`, so the control on screen and the
row in Settings → Browse → Reading are one value seen twice, and a size chosen
once is never asked for again. The stored size is a **number, not an index into
`FONT_STEPS`** — a typed 22 stays 22 and A+ steps to 24 — and both keys are
re-read every frame (`_sync_style`), because Settings is reachable from the tray
while a book is open.

**The measure is capped in ems and the column is centred** (`ReaderStyle.max_measure`,
default 34em ≈ 68 characters). Ems rather than pixels because what is being capped
is a count of *characters*: a pixel cap is the right column at one type size and
wrong at every other, which is exactly the setting the reader offers. Anything
converting a pointer position back into the book must take the offset from
`ReaderStyle.column()` rather than from `margin_x`, or it is right only in a
window narrow enough for the cap not to bite.

**An image is drawn the size the book asked for, not the size the file is**
(`content._image_box`, `layout._image_size`). The natural pixel size is only
right when the book said nothing, and books say something more often than they
look like they do — the case that prompted this was a cookbook whose step arrows
are 256 px PNGs styled to about a line tall, every one of them drawn a quarter of
a page tall. Both spellings are read (the presentation attribute an older book
carries, the stylesheet a new one does, CSS winning), plus `max-width` /
`max-height`, which is the commoner spelling for exactly these little marks.
Sizes are kept in **ems of the body size**, not pixels, for the same reason the
measure is: an icon the author sized against the text should stay sized against
it when the type size changes. Naming one side keeps the aspect ratio; naming
both is taken at its word.

### 4.8 Typography, and the two bugs mutation testing found

**The typography is the set a published novel actually uses**, and each piece
fails *quietly* without it, which is why they are pinned rather than eyeballed:
bulleted and numbered lists with hanging markers (`list-style-type`,
`<ol type>`/`start`, roman and alpha ordinals, bullets cycling by depth, `none`),
block quotes inset on **both** sides, `margin-left`/`margin-right` for verse and
quoted letters, bold definition terms, raised superscripts and lowered subscripts
(in ems of the *body*, so two markers at different sizes sit level), synthetic
small caps, and drop capitals.

A **drop cap is recognised by shape** — one or two characters at ≥2em opening a
paragraph — because the alternative reading is not "no drop cap" but a 3.4em
letter left inline, inflating the first line to three times its height. The lines
it spans are placed as one group, so a page break cannot land inside it.

Two bugs that mutation-testing the new tests turned up, both invisible on an
unindented page of plain prose:

- justification measured its right edge from the block's *measure* rather than
  from its absolute right, so every indented block (list item, quotation, verse)
  set ragged one indent short of the margin;
- a `\r` from a Windows-converted book reached the page as a tofu box at the end
  of every line of a code listing, because `html.parser` does not do XML's
  required line-end normalization and no face has a glyph for it.

### 4.9 Copy Paragraph / Copy Page, and the page-turn zones

**"Copy Paragraph" / "Copy Page" is what stands in for selecting text.** The page
is one bitmap, so there is nothing to drag a selection across; what a pointer
*can* name is the paragraph it landed in, and that is resolved **at the click**,
not when the menu is drawn. The hit test is by height alone — every line spans
the measure, so an x would be a parameter that is quietly ignored.

Both copies come from the **blocks, not the laid-out lines**, and a paragraph the
page ends inside is copied whole. That is partly taste (half a paragraph is a
fragment starting mid-sentence) and mostly the only version that can be right: a
space is not a run — the breaker drops space tokens and justification turns them
into a gap between two pieces' x — so `Line.text()` yields its words joined
together, and text rebuilt from the lines silently loses every space. Small
capitals are undone on the way out too (`Block.plain_text`), because the
uppercasing is a substitute for a face we do not have, not something the author
typed. The menu is out of flow and lives in the *page's* tree, so it dies with
the route.

**The page-turn halves are `zone` regions** — an ImageMap region flag that takes
no hover ring and leaves the spatial-nav order (`nnav` in the renderer). Both
rings exist to say "this one", and half a page of prose is not a "this": on a
strip of tiles they are the affordance, here they would be an accent box over the
sentence being read.

## 5. The comic reader

**A CBZ is *played*, not drawn** (`comic.py`, `mpvtk_browser/pages/comic.py`,
`gateway/picture.py`, `player_window.show_picture`) — the opposite decision from
the epub reader, for a measured reason. A comic page is 1600×2400 or larger and
the zoom goes well past fit-width, so rasterizing it the epub way costs a full
Pillow decode per page and a viewport-sized BGRA buffer per pan frame, in a
bitmap cache sized for a library's worth of artwork (`strips.StripStore.MAX_BYTES`
is 96–128 MB, and **32 MB on a machine short of RAM**, which one zoomed page
would not fit in). mpv already decodes pictures, keeps them on the GPU, and has
`video-zoom` / `video-pan-x` / `video-pan-y`. Measured: mpv holds an image
indefinitely with `keep-open` and `image-display-duration=inf`, and both
properties take effect on it.

So the page is a file handed to `loadfile`, and the reader's two bars draw over
it the way the playback HUD draws over video. `comic.py` never decodes an image
and never scales one; the cost is one temporary file per page, because mpv cannot
read inside an archive, and extraction is a copy of already-compressed bytes
rather than a decode. `PAGE_CACHE` (8 extracted pages) is deliberately **above
the browser's pool width**, which is what makes it a cache and not a hazard:
extractions run on that pool, so with a narrower cache a burst of page turns has
one worker trimming a file another worker has just written and handed to mpv.
`MAX_PAGE_BYTES` is applied to what an entry *delivers*, the same rule as
`epub.archive`.

**The page size is read from the file's header, not from mpv.** Pillow parses a
JPEG header in well under a millisecond without decoding a pixel, and knowing the
size *before* the load is what lets the zoom be set in the same breath as the
picture. Asking mpv means polling until it has decoded, in the render path, for a
number that was sitting in the first two hundred bytes of the file.

### 5.1 Zip and tar only, in a natural sort

`.cbz` is a zip and `.cbt` is a tar, both of which Python ships. `.cbr` is RAR
and `.cb7` is 7-Zip, and neither has a stdlib reader — adding one means a
dependency for a format the desktop already opens, which is not a trade this
project makes (CONTRIBUTING.md). Those keep handing off to whatever the user has,
and `UNREADABLE_COMIC_EXTENSIONS` exists so the book screens can say *why*.

**Reading order is a natural sort of the filenames**, which is the whole of what
a CBZ says about it: digits must compare as *numbers*, so `page2` comes before
`page10` — which a plain sort gets exactly backwards, and which is the single
most visible way to get a comic wrong. `PAGE_SUFFIXES` decides what counts as a
page; a `ComicInfo.xml`, a `Thumbs.db` or a readme is not one. Progress is
`books.progress_of`'s page index, the same number every other client shows, and a
page turn is written through `record_reading_position` (§2.2).

### 5.2 It is a third window state

The browser had two: **browsing** (it owns the window, drawing over mpv's painted
idle background) and **yielded** (playback owns the window, the browser pushes an
empty scene). A comic is both at once — a picture in the window with the
library's own chrome over it — so it goes through neither `_start` nor `_yield`.

That is safe only because `PlayerManager` keys its queue, reporting and idle-quit
off `self._video`, which **stays None** here: `_on_eof_reached` and
`_on_playback_abort` both return without it, so nothing advances a queue; the
timeline reports nothing because there is no session; and `idle_quit` already
refuses to fire while `mpvtk_active`. What is *not* free is tidiness at the edges,
so `keep_open` and `image_display_duration` are set in `show_picture` and put
back in `clear_picture` (an image otherwise shows for one second and then mpv
idles), and leaving the reader must clear the picture or the comic stays behind
the library grid. A real playback start replaces the picture and needs no
coordination: `loadfile` replaces whatever is loaded.

### 5.3 `keepaspect` is the trap, twice

`set_browse_window` turns `keepaspect` **off** so the library window resizes
freely instead of snapping to the last video's shape — and with it off mpv
**stretches** whatever is loaded to fill the window, which distorts the page *and*
makes `video-zoom` invisible (a stretched picture already fills the window at
every zoom). `show_picture` turns it on. Both symptoms, one property.

But it is the **window's** property, owned by `set_browse_window` (off) and
`browse_yield` (on); `show_picture` only borrows it, so `reset_picture_view` must
**not** put it back while something is playing. That second trap is the more
expensive one and it fires with no comic anywhere in the session — see the
`keepaspect` / `reset_picture_view` bullet in `CLAUDE.md`, which is where it
lives, because it is a `run_action` ordering hazard about playback rather than
about books. `_video is None and not self._loading` is the guard;
`tests/test_window_geometry.py:PictureViewHandoffTest` asserts both interleavings
rather than the guard.

### 5.4 Pan is measured in the scaled picture

**`video-pan-x` / `video-pan-y` are fractions of the SCALED PICTURE, not of the
window** — measured, because it cannot be reasoned out and the two readings
differ by whatever the zoom is. A 1400×2100 page at `video-zoom` 1.415 in a
1280×720 window is displayed 1920 tall, and `video-pan-y` 0.4 put its top edge at
**y = 168 = -599.5 + 0.4 × 1919**. Against the window's 720 it would be y = -311,
which is where the first version of this put the page. The sign moves the
*picture*, so the top of a page is `max_y`. `tests/test_picture_view.py` pins the
measurement; `fit_scale`, `fit_zoom` and `pan_bounds` in `gateway/picture.py` are
the arithmetic.

### 5.5 The end-of-page interlock is per direction, and the clamp carries the page

A wheel notch that cannot move the page asks Python to turn it, and a fling
delivers a dozen notches before the new page lands — so the renderer asks once
and waits for a **fresh clamp**, which is what a page arriving looks like. Two
things break that in **Fit Page**, and both were invisible in Fit Width because a
taller-than-the-window page really does have a pan range:

- the clamp is byte-identical from page to page, and `MpvtkApp.set_picture_pan`
  skips an unchanged model — so no message, no release, and the wheel turned
  exactly one page and went dead. Hence `"page"` in the payload, which the
  renderer never reads and which exists only to make the clamp differ;
- the **last page never gets a new clamp at all**, so an interlock that ignored
  the direction latched and killed scrolling *back* off the end too.

### 5.6 Every continuous gesture is the renderer's

`mpvtk-vpan`, `state.vpan*`. Python sends a clamp and the pan unit once per
zoom / page turn / resize; a drag and a wheel notch then set mpv's properties in
Lua with no round trip, because a page turn is one message but a scroll is sixty
a second. Only two things come back: a wheel notch that ran off the end (turn the
page) and ctrl+wheel (zoom), both of which need the page size and the bar
heights. All of it hangs off `state` because `renderer.lua`'s main chunk is **at**
the 200-local ceiling.

**The reading mode is a setting, not page state** (`conf.comic_fit`, default
`"width"`). Fit Width / Fit Page on the reader's own bar write it through
`config.set_setting`, so the next comic opens the way the last one was being read
— a preference about how somebody reads comics, not about one comic. Read per
call rather than captured, because Settings is reachable from the tray while a
comic is up, and validated on the way out: it is a plain string in a JSON file
somebody can type into, and an unknown one would go straight to `fit_zoom`.

## 6. Permissions

**`EnableContentDownloading` is fatal for books, not merely inconvenient.**
Everywhere else in the app it costs offline viewing and there is a fallback; here
it is the only path to the content (§1), so `ItemActions.read_book` refuses with
a message that names the reason instead of enqueuing a fetch that 403s and
leaving a Read button that appears to do nothing. The button stays visible on
purpose — an administrator can grant the permission, and one that had silently
vanished would leave nothing to ask about.

The full write-up, including what is pinned and where, is
`docs/PERMISSION_GAPS.md` §4b. Do not restate it here.

---

Two related facts deliberately live in `CLAUDE.md` rather than in this file,
because both fire when no book is involved: the `keepaspect` / `reset_picture_view`
handoff ordering (§5.3), and `_library_showing()` never being `_video is None` —
which bit audiobook playback but is a property of the browser's back navigation
during *any* audio playback.
