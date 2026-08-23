-- Unit tests for renderer.lua, run against a faked mpv (see fake_mp.lua).
--
-- The renderer holds every piece of state the Python side cannot see --
-- scroll offsets, textbox edits, focus -- and it is reached only through
-- script messages. Both are exercised here through that real boundary:
-- push a scene, drive input, read back what the renderer published.
--
-- Prints "ok N" / "not ok N - why" (TAP-ish); the Python wrapper asserts on
-- the exit status and shows this output on failure.

local here = arg[0]:match("^(.*)/[^/]*$") or "."
package.path = here .. "/?.lua;" .. package.path

local fake = require("fake_mp")
fake.install()

local RENDERER = arg[1]
assert(RENDERER, "usage: test_renderer.lua <path to renderer.lua>")

-- Present on mpv 0.39+, and the renderer reads it at load time to decide
-- whether it may hand VO dragging back (see state.vodrag). Set BEFORE the
-- chunk runs, because that answer is taken once and never revisited.
fake.log.props["input-builtin-dragging"] = true

local chunk = assert(loadfile(RENDERER))
chunk()

-- The renderer only builds a scene once it knows the window size.
fake.observe("osd-dimensions", { w = 1280, h = 720 })

-- ------------------------------------------------------------ harness

local passed, failed = 0, 0
local n = 0

local function ok(cond, name, detail)
    n = n + 1
    if cond then
        passed = passed + 1
        print(string.format("ok %d - %s", n, name))
    else
        failed = failed + 1
        print(string.format("not ok %d - %s%s", n, name,
                            detail and ("\n    # " .. tostring(detail)) or ""))
    end
end

local function eq(got, want, name)
    ok(got == want, name,
       string.format("got %s, want %s", tostring(got), tostring(want)))
end

--- Push a scene of nodes and let the renderer reconcile it.
local function scene(nodes)
    fake.send("mpvtk-scene", fake.token({ nodes = nodes }))
end

--- A vertical scroll container: `h` tall viewport over `ch` of content.
local function vscroll(id, h, ch, extra)
    local node = { id = id, t = "scroll", axis = "y",
                   x = 0, y = 0, w = 400, h = h, cw = 400, ch = ch }
    for k, v in pairs(extra or {}) do node[k] = v end
    return node
end

-- An untouched container has no published entry at all, which means the
-- same thing as being at the top.
local function offset(id) return fake.scroll_prop()[id] or 0 end

--- Page down until the offset stops moving. A page is ~90% of the
--- viewport, so "how many" is geometry the test should not encode.
local function page_to_end(id)
    local prev
    while offset(id) ~= prev do
        prev = offset(id)
        fake.send("mpvtk-scroll", fake.token({ id = id, dir = 1 }))
    end
end

-- =========================================================== follow

-- A follow container is the log viewer: content is appended to the bottom
-- and the user wants to stay there, unless they have scrolled up to read.

scene({ vscroll("logs", 100, 500, { follow = true }) })
eq(offset("logs"), 400, "a follow container opens at the end")

scene({ vscroll("logs", 100, 700, { follow = true }) })
eq(offset("logs"), 600, "it rides the end as content grows")

-- Scroll up: away from the tail, so following must stop.
fake.send("mpvtk-scroll", fake.token({ id = "logs", dir = -1 }))
local parked = offset("logs")
ok(parked < 600, "scrolling up moves off the end",
   "offset " .. tostring(parked))

scene({ vscroll("logs", 100, 900, { follow = true }) })
eq(offset("logs"), parked, "a reader who scrolled up is not yanked down")

-- Back to the bottom: following resumes.
page_to_end("logs")
eq(offset("logs"), 800, "paging down reaches the end")
scene({ vscroll("logs", 100, 1100, { follow = true }) })
eq(offset("logs"), 1000, "returning to the end resumes following")

-- Slack: a fractional content height must not unstick the tail.
scene({ vscroll("logs", 100, 1103, { follow = true }) })
eq(offset("logs"), 1003, "a few px short of the end still counts as the end")

-- Content that fits needs no scrolling at all.
scene({ vscroll("logs", 100, 60, { follow = true }) })
eq(offset("logs"), 0, "content shorter than the viewport sits at zero")

-- A plain container is untouched by any of this.
scene({ vscroll("plain", 100, 500) })
eq(offset("plain"), 0, "a non-follow container still opens at the top")
scene({ vscroll("plain", 100, 900) })
eq(offset("plain"), 0, "and does not follow growth")

-- ------------------------------------------------ follow tells Python

-- The renderer moving the offset by itself is only half the job. Python
-- windowed its virtualized rows against the offset it knew when it BUILT
-- the scene, so a snap performed here invalidates that window. Publishing
-- the property is not enough -- it wakes nobody. Without the event the logs
-- panel opened BLANK: rows 0-57 materialized for offset 0, renderer drawn
-- at the bottom, tail spacer with nothing in it.
--
-- The original tests here asserted the offset was right and passed happily
-- while this was broken: right behaviour, wrong layer.

local function scroll_events()
    local out = {}
    for _, e in ipairs(fake.log.events) do
        if type(e) == "table" and e.t == "scroll" then out[#out + 1] = e end
    end
    return out
end

scene({})                      -- drop any prior state
fake.reset_events()
scene({ vscroll("logs", 100, 500, { follow = true }) })
local evs = scroll_events()
ok(#evs > 0, "opening at the end reported nothing to the app")
if #evs > 0 then
    eq(evs[#evs].id, "logs", "the scroll event names the container")
    eq(evs[#evs].offset, 400, "the event carries the snapped offset")
end

-- Riding the tail as content grows must report too: each snap re-windows.
fake.reset_events()
scene({ vscroll("logs", 100, 900, { follow = true }) })
eq(#scroll_events(), 1, "following growth did not report the new offset")

-- A snap that does not actually move must NOT spam the app: an unchanged
-- offset means Python's window is still valid.
fake.reset_events()
scene({ vscroll("logs", 100, 900, { follow = true }) })
eq(#scroll_events(), 0, "a no-op snap reported anyway")

-- A container that fits needs no scrolling, so nothing to report.
fake.reset_events()
scene({})
scene({ vscroll("small", 100, 60, { follow = true }) })
eq(#scroll_events(), 0, "content shorter than the viewport reported a snap")

-- And a reader who scrolled away is neither moved nor notified.
scene({})
scene({ vscroll("logs", 100, 900, { follow = true }) })
fake.send("mpvtk-scroll", fake.token({ id = "logs", dir = -1 }))
fake.reset_events()
scene({ vscroll("logs", 100, 1300, { follow = true }) })
eq(#scroll_events(), 0, "a parked reader was sent a snap event")

-- ============================================== off0 (restored offset)

-- The browser parks a screen's scroll offsets on its route when you
-- navigate away, and passes them back as `off0` when you return. The
-- renderer applies it, because it is the only side that knows the content
-- height in the frame the offset lands in.

scene({})                      -- drop any prior state
eq(offset("grid"), 0, "state cleared")
scene({ vscroll("grid", 100, 2000, { off0 = 800 }) })
eq(offset("grid"), 800, "a returning container did not open where it was left")

-- Once only. It restores a position; it does not drive one, or the wheel
-- would fight the scene on every frame.
fake.send("mpvtk-scroll", fake.token({ id = "grid", dir = 1 }))
local moved = offset("grid")
ok(moved > 800, "scrolling moved off the restored offset",
   "offset " .. tostring(moved))
scene({ vscroll("grid", 100, 2000, { off0 = 800 }) })
eq(offset("grid"), moved, "off0 yanked the user back on a later frame")

-- Clamped to the content actually present. A library that came back
-- shorter (a filter, a deletion) must not be scrolled past its end, which
-- is the state that renders as a screenful of blank spacers.
scene({})
scene({ vscroll("grid", 100, 300, { off0 = 5000 }) })
eq(offset("grid"), 200, "a restored offset past the end was not clamped")

scene({})
scene({ vscroll("grid", 100, 60, { off0 = 400 }) })
eq(offset("grid"), 0, "content shorter than the viewport is not scrollable")

-- And it has to tell Python, for the same reason `follow` does: the rows
-- were virtualized against offset 0 when the scene was built.
scene({})
fake.reset_events()
scene({ vscroll("grid", 100, 2000, { off0 = 800 }) })
local evs2 = scroll_events()
ok(#evs2 > 0, "restoring an offset reported nothing to the app")
if #evs2 > 0 then
    eq(evs2[#evs2].offset, 800, "the event carries the restored offset")
end

-- A restore to the top is a no-op and must not be announced.
scene({})
fake.reset_events()
scene({ vscroll("grid", 100, 2000, { off0 = 0 }) })
eq(#scroll_events(), 0, "restoring to the top reported a snap")

-- ==================================================== textbox commit

-- The settings screen has 65 rows that were losing the edit unless the
-- user pressed ENTER. blur() now reports the pending text.

-- Stacked, not overlapping: clicking away has to land on a *different*
-- node or the renderer rightly treats it as a click on the focused box.
local function textbox(id, value, row)
    return { id = id, t = "textbox", x = 0, y = (row or 0) * 40,
             w = 200, h = 30, size = 18, text = value or "" }
end

local function type_text(s)
    fake.send("mpvtk-debug", fake.token({ cmd = "text", s = s }))
end

local function click(id)
    fake.send("mpvtk-debug", fake.token({ cmd = "click", id = id }))
end

local function last_event(t)
    local evs = fake.log.events
    for i = #evs, 1, -1 do
        if type(evs[i]) == "table" and evs[i].t == t then return evs[i] end
    end
    return nil
end

scene({ textbox("box", "before", 0), textbox("other", "", 1) })
fake.reset_events()
click("box")
type_text("!")
click("other")           -- blur by clicking away

local commit = last_event("commit")
ok(commit ~= nil, "clicking away from an edited box commits it")
if commit then
    eq(commit.id, "box", "the commit names the box that lost focus")
    eq(commit.value, "before!", "the commit carries the typed value")
end

-- An untouched box must stay silent, or every click across a settings
-- screen would re-submit 65 unchanged values.
scene({ textbox("a", "x", 0), textbox("b", "y", 1) })
fake.reset_events()
click("a")
click("b")
ok(last_event("commit") == nil, "blurring an unedited box commits nothing")

-- ESC reverts, so it must not commit either.
scene({ textbox("c", "keep") })
fake.reset_events()
click("c")
type_text("Z")
fake.key("mpvtk_k_ESC")
ok(last_event("commit") == nil, "ESC reverts rather than committing")

-- ========================================================= clipboard

-- mpv's clipboard/text is not universal: --clipboard-backends defaults to
-- win32,mac,wayland,vo and the x11 backend only arrived in 0.41, so an
-- X11 session under mpv 0.40 answers "property unavailable" both ways.
-- Copy and paste were pcall'd, and mp.set_property signals failure by
-- RETURNING nil rather than raising -- so both silently did nothing.

local function subprocess_calls()
    local out = {}
    for _, c in ipairs(fake.log.commands) do
        if type(c) == "table" and c.name == "subprocess" then
            out[#out + 1] = c
        end
    end
    return out
end

-- The fallback follows the session, because a Wayland session usually
-- also answers xclip through XWayland -- a different clipboard.
local WANT_SET, WANT_GET, WANT_PKG
if os.getenv("WAYLAND_DISPLAY") then
    WANT_SET, WANT_GET, WANT_PKG = "wl-copy", "wl-paste", "wl-clipboard"
else
    WANT_SET, WANT_GET, WANT_PKG = "xclip", "xclip", "xclip"
end

local function select_all(id)
    click(id)
    fake.key("mpvtk_k_ctrl_a")
end

-- Working mpv property: nothing external is spawned.
fake.unavailable = {}
fake.log.commands = {}
scene({ textbox("clip1", "copy me") })
select_all("clip1")
fake.key("mpvtk_k_ctrl_c")
eq(fake.log.props["clipboard/text"], "copy me", "ctrl+c uses mpv's clipboard")
eq(#subprocess_calls(), 0, "a working mpv clipboard spawns nothing")

-- Property unavailable: fall back to the desktop's own tool.
fake.unavailable = { ["clipboard/text"] = true }
fake.log.commands = {}
fake.subprocess = function() return { status = 0, stdout = "" } end
scene({ textbox("clip2", "fallback") })
select_all("clip2")
fake.key("mpvtk_k_ctrl_c")
local calls = subprocess_calls()
ok(#calls > 0, "an unavailable clipboard property falls back to a helper")
if #calls > 0 then
    local argv = calls[1].args
    eq(argv[1], "sh", "the copy goes through a shell")
    ok(argv[3] and argv[3]:find(WANT_SET, 1, true) ~= nil,
       "the fallback matches the session", argv[3])
    -- xclip/xsel/wl-copy fork a child that keeps owning the selection, and
    -- mpv makes pipes for the child whether or not we capture them -- the
    -- forked copy inherits those and holds them until the clipboard is
    -- replaced, so an unredirected copy never returns. Measured on 0.40.
    ok(argv[3] and argv[3]:find(">/dev/null", 1, true) ~= nil,
       "the copy's pipes are closed before the tool forks", argv[3])
    eq(calls[1].stdin_data, "fallback", "the text is piped to it")
    eq(calls[1].capture_stdout, false, "copy does not capture stdout")
end

-- Paste, same fallback, reading back.
fake.log.commands = {}
fake.subprocess = function(t)
    if t.args[1] == WANT_GET then return { status = 0, stdout = "pasted" } end
    return { status = -1, stdout = "" }
end
scene({ textbox("clip3", "") })
click("clip3")
fake.key("mpvtk_k_ctrl_v")
local ch = last_event("change")
ok(ch ~= nil and ch.value == "pasted", "ctrl+v falls back to a helper",
   ch and ch.value or "no change event")

-- Nothing at all: the user gets told which package to install, rather
-- than a text field that silently ignores ctrl+v.
fake.subprocess = nil       -- every helper fails, as if not installed
fake.reset_events()
scene({ textbox("clip4", "") })
click("clip4")
fake.key("mpvtk_k_ctrl_v")
local warn = last_event("clipboard")
ok(warn ~= nil, "no clipboard at all reports it")
if warn then
    eq(warn.op, "paste", "the report says which operation failed")
    eq(warn.need, WANT_PKG, "it names the package to install")
end

-- Once per session: a nag on every failed paste is worse than silence.
fake.reset_events()
fake.key("mpvtk_k_ctrl_v")
ok(last_event("clipboard") == nil, "the clipboard notice does not repeat")

-- A cut whose copy failed would just destroy the text.
scene({ textbox("clip5", "precious") })
fake.reset_events()
select_all("clip5")
fake.key("mpvtk_k_ctrl_x")
local cut = last_event("change")
ok(cut == nil, "cut keeps the text when the copy could not happen",
   cut and cut.value or "")

fake.unavailable = {}
fake.subprocess = nil

-- =========================================================== wheel

-- The wheel drives on_wheel with scale 1 -- a discrete notch, the trackball
-- case. Default (continuous) scrolling moves the stored offset by a flat
-- pixel step and lets the DISPLAY snap; snapped_scrolling steps whole detents.

-- ------------------------------------------------- a page claiming the wheel

-- The epub reader has no scroll container -- its whole content is one
-- bitmap -- so a notch there means "turn the page", which it asks for by
-- claiming WHEEL_UP/WHEEL_DOWN like any other key. Two things to pin: that
-- the claim is answered at all (nothing else would deliver it), and that a
-- hi-res device does not fly through the book, since a trackpad sends
-- fractions of a notch several times per gesture.

local function key_events()
    local out = {}
    for _, e in ipairs(fake.log.events) do
        if type(e) == "table" and e.t == "key" then out[#out + 1] = e end
    end
    return out
end

scene({})
fake.mouse(400, 300)
fake.send("mpvtk-keys", fake.token({ keys = { "WHEEL_UP", "WHEEL_DOWN" } }))
fake.reset_events()
fake.key("wheel_down", { scale = 1 })
eq(#key_events(), 1, "a claimed wheel notch was not delivered")
eq((key_events()[1] or {}).key, "WHEEL_DOWN", "the wrong key was sent")

fake.reset_events()
fake.key("wheel_down", { scale = 0.3 })
fake.key("wheel_down", { scale = 0.3 })
eq(#key_events(), 0, "a fraction of a notch turned a page")
fake.key("wheel_down", { scale = 0.5 })
eq(#key_events(), 1, "accumulated fractions never made a notch")

fake.reset_events()
fake.key("wheel_up", { scale = 1 })
eq((key_events()[1] or {}).key, "WHEEL_UP", "scrolling up did not go back")

-- With no claim it must fall through to the ordinary scroll path, or every
-- other screen loses its wheel.
fake.send("mpvtk-keys", fake.token({ keys = {} }))
fake.reset_events()
fake.key("wheel_down", { scale = 1 })
eq(#key_events(), 0, "the wheel was still claimed after the claim was dropped")

-- --------------------------------------------- the comic page's wheel

-- A picture takes the wheel next: it is not a container, so scroll_at finds
-- nothing over it. What is pinned here is the interlock at the end of a
-- page, which has to hold off a fling (a dozen notches arrive before the
-- next page and its clamp do) without latching.

local function vpan_events()
    local out = {}
    for _, e in ipairs(fake.log.events) do
        if type(e) == "table" and e.t == "vpan" then out[#out + 1] = e end
    end
    return out
end

-- A page that FITS the window, which is Fit Page: no pan range at all, so
-- every notch is at the end of the page.
local function fitted_page()
    fake.log.props["video-pan-x"] = 0
    fake.log.props["video-pan-y"] = 0
    fake.send("mpvtk-vpan", fake.token({
        on = true, unitx = 1280, unity = 720, step = 120,
        minx = 0, maxx = 0, miny = 0, maxy = 0,
    }))
end

scene({})
fake.mouse(400, 300)
fitted_page()
fake.reset_events()
fake.key("wheel_down", { scale = 1 })
eq(#vpan_events(), 1, "a notch at the bottom did not ask for the next page")
fake.key("wheel_down", { scale = 1 })
fake.key("wheel_down", { scale = 1 })
eq(#vpan_events(), 1, "a fling past the bottom turned several pages")

-- A fresh clamp is what a page arriving looks like, and it is what releases
-- the interlock. In Fit Page the clamp is identical page to page, which is
-- why the app puts the page number in it -- without that there is no
-- message here at all and the wheel turns exactly one page, ever.
fitted_page()
fake.reset_events()
fake.key("wheel_down", { scale = 1 })
eq(#vpan_events(), 1, "a new clamp did not release the end-of-page interlock")

-- Reversing must re-ask even with no new clamp: the LAST page never gets
-- one (there is nothing to turn to), and in Fit Page it has no pan range
-- either, so an interlock that ignored the direction left the wheel dead in
-- both directions at the end of a comic.
fake.reset_events()
fake.key("wheel_down", { scale = 1 })
eq(#vpan_events(), 0, "the interlock did not hold for a repeated notch")
fake.key("wheel_up", { scale = 1 })
eq(#vpan_events(), 1, "scrolling back off the end of a comic was dead")
eq((vpan_events()[1] or {}).edge, "top", "the wrong edge was reported")

-- A page with somewhere to go scrolls instead of turning, and any movement
-- clears the interlock.
fake.log.props["video-pan-x"] = 0
fake.log.props["video-pan-y"] = 0
fake.send("mpvtk-vpan", fake.token({
    on = true, unitx = 1280, unity = 2000, step = 120,
    minx = 0, maxx = 0, miny = -0.4, maxy = 0.4,
}))
fake.reset_events()
fake.key("wheel_down", { scale = 1 })
eq(#vpan_events(), 0, "a page with room to scroll asked for a turn")
ok(fake.log.props["video-pan-y"] < 0, "the page did not scroll")

fake.send("mpvtk-vpan", fake.token({ on = false }))

local function wheel(id, steps, dir)
    fake.send("mpvtk-debug", fake.token({
        cmd = "wheel", id = id, dir = dir or 1, steps = steps or 1,
        axis = "y" }))
end

-- Continuous mode: an equal-row grid scrolls by a SUB-row step, not a whole
-- row per notch -- this is what stops a trackball overshooting.
scene({ vscroll("grid", 100, 1000, { snap = 240, bar = true }) })
wheel("grid", 1)
eq(offset("grid"), 80, "one notch scrolls a sub-row pixel step, not a row")

-- Least-common-denominator: the step is rounded so a whole number of notches
-- spans exactly one row (240 / round(240/80) = 80), consistently.
scene({ vscroll("grid2", 100, 1000, { snap = 240, bar = true }) })
wheel("grid2", 3)
eq(offset("grid2"), 240, "three notches land exactly one row down")
wheel("grid2", 3)
eq(offset("grid2"), 480, "and the next three land the next row -- same cadence")

-- A row the raw step does not divide is made consistent by rounding the detent
-- count DOWN (floor): 200 / floor(200/80)=2 -> a 100px step, 2 notches/row --
-- not the 3 tiny notches round() would have given. WHEEL_STEP is a floor on
-- granularity, so the step only grows.
scene({ vscroll("grid3", 100, 1000, { snap = 200, bar = true }) })
wheel("grid3", 1)
eq(offset("grid3"), 100, "the step grows to divide the row, never shrinks")
wheel("grid3", 1)
eq(offset("grid3"), 200, "two notches still span exactly one row")

-- A plain (non-snapping) container keeps the flat pixel step.
scene({ vscroll("plainw", 100, 1000) })
wheel("plainw", 1)
eq(offset("plainw"), 80, "a non-snapping container scrolls the flat step")

-- The pixel step is configurable (scroll_wheel_pixels): 120px over a 240 row
-- is 2 notches/row.
fake.send("mpvtk-wheel", fake.token({ px = 120 }))
scene({ vscroll("grid4", 100, 1000, { snap = 240, bar = true }) })
wheel("grid4", 1)
eq(offset("grid4"), 120, "the wheel step follows scroll_wheel_pixels")
fake.send("mpvtk-wheel", fake.token({ px = 80 }))

-- snapped_scrolling: each notch jumps a whole row, the old stepped behavior.
fake.send("mpvtk-wheel", fake.token({ snapped = true }))
scene({ vscroll("grid5", 100, 1000, { snap = 240, bar = true }) })
wheel("grid5", 1)
eq(offset("grid5"), 240, "snapped_scrolling steps a whole row per notch")

-- ...and on the home page's uneven breakpoints, one notch = one section.
scene({ vscroll("home", 100, 1000, { snaps = { 0, 130, 400, 640 } }) })
wheel("home", 1)
eq(offset("home"), 130, "snapped_scrolling steps one breakpoint on the home page")
fake.send("mpvtk-wheel", fake.token({ snapped = false }))

-- ================================================ absolute scroll + slide

-- The carousel page buttons aim at an exact offset (Python owns the tile
-- pitch, so it owns the alignment), optionally easing into it.

local function hscroll(id, w, cw, extra)
    local node = { id = id, t = "scroll", axis = "x",
                   x = 0, y = 0, w = w, h = 200, cw = cw, ch = 200 }
    for k, v in pairs(extra or {}) do node[k] = v end
    return node
end

scene({ hscroll("row", 500, 3000) })
fake.send("mpvtk-scroll", fake.token({ id = "row", to = 880 }))
eq(offset("row"), 880, "an absolute scroll lands exactly on the target")

fake.send("mpvtk-scroll", fake.token({ id = "row", to = 99999 }))
eq(offset("row"), 2500, "an absolute scroll past the end clamps")

-- A slide reports its DESTINATION straight away, before it has moved a
-- pixel. Everything downstream wants where the row is going: virtualization
-- builds the arriving window, and a page button held down chains whole pages
-- instead of re-deriving one from a half-finished slide.
scene({ hscroll("row2", 500, 3000) })
fake.send("mpvtk-scroll", fake.token({ id = "row2", to = 1000, ms = 200 }))
eq(offset("row2"), 1000, "a slide publishes its destination immediately")

-- ...and it converges there once the clock runs out.
fake.advance(1.0)
fake.fire_timers()
eq(offset("row2"), 1000, "a slide settles on its target")

-- User input beats an animation in flight: the row stops where the wheel
-- put it, and the published offset stops claiming the old destination.
scene({ hscroll("row3", 500, 3000) })
fake.send("mpvtk-scroll", fake.token({ id = "row3", to = 2000, ms = 200 }))
eq(offset("row3"), 2000, "slide armed")
fake.send("mpvtk-scroll", fake.token({ id = "row3", dir = -1 }))
ok(offset("row3") < 2000, "a direct scroll cancels the slide",
   string.format("offset stayed at %s", tostring(offset("row3"))))
local after_cancel = offset("row3")
fake.advance(1.0)
fake.fire_timers()
eq(offset("row3"), after_cancel, "the cancelled slide does not resume")

-- ========================================== mouse back button

-- The thumb button is Back, and it must stay Back by *being* ESC rather
-- than by reimplementing it: ESC's ladder (scrub -> popup -> menu ->
-- modal -> HUD, then Python for "one page off the nav stack") is spread
-- across bindings that come and go with what is on screen, and a second
-- copy of it would go stale on the first layer anyone adds.
--
-- What this cannot test is the scoping, because the fake cannot model
-- mpv's section stack: mbtn_back lives in the mpvtk_mouse group, which
-- is disabled while video plays, so mpv's own MBTN_BACK (playlist-prev)
-- survives mid-playback.
fake.log.commands = {}
fake.key("mbtn_back")
local sent_esc = false
for _, c in ipairs(fake.log.commands) do
    if type(c) == "table" and c[1] == "keypress" and c[2] == "ESC" then
        sent_esc = true
    end
end
ok(sent_esc, "the mouse back button presses ESC")

-- Its pair has no key to press: nothing in mpv or the app means
-- "forward", so it is an event and the app decides what history it has.
-- Windowless like `nav`, not addressed to whatever the pointer is over.
fake.reset_events()
fake.key("mbtn_forward")
local fwd = 0
for _, e in ipairs(fake.log.events) do
    if type(e) == "table" and e.t == "forward" then fwd = fwd + 1 end
end
eq(fwd, 1, "the mouse forward button sends one forward event")

-- ========================================== client-side title bar

-- `wdrag` marks a node that stands in for a title bar, on a window the
-- desktop drew none for. Both gestures are handled HERE rather than sent
-- to Python as events: `begin-vo-dragging` means "the button the user is
-- holding is now moving the window", so it has to be issued during the
-- press, and a round trip is not a gesture mpv will still accept.
local function titlebar(extra)
    local node = { id = "csd-bar", t = "rect", x = 0, y = 0, w = 400, h = 60 }
    for k, v in pairs(extra or {}) do node[k] = v end
    return node
end

local function commanded(name)
    for _, c in ipairs(fake.log.commands) do
        if type(c) == "table" and c[1] == name then return true end
    end
    return false
end

scene({ titlebar({ wdrag = true }) })
fake.mouse(200, 30)
fake.log.commands = {}
fake.key("mbtn_left")
ok(commanded("begin-vo-dragging"), "pressing the title bar drags the window")

-- Double-clicking a title bar maximizes it, everywhere.
fake.log.props["window-maximized"] = false
fake.key("mbtn_left_dbl")
eq(fake.log.props["window-maximized"], true,
   "double-clicking the title bar maximizes the window")
fake.key("mbtn_left_dbl")
eq(fake.log.props["window-maximized"], false,
   "and again restores it")

-- A bar that is also a button stays a button. The window controls sitting
-- ON the bar are separate, higher nodes, and node_at prefers them -- so
-- without this, every button in the top bar would drag the window instead
-- of doing its job.
scene({ titlebar({ wdrag = true, click = true }) })
fake.mouse(200, 30)
fake.log.commands = {}
fake.reset_events()
fake.key("mbtn_left")
ok(not commanded("begin-vo-dragging"),
   "a clickable node on the title bar does not drag the window")

-- An ordinary bar is not a title bar. `wdrag` is set only while the UI is
-- standing in for one, so a window WITH a title bar must not get a second,
-- worse one that ignores snapping and edge tiling.
scene({ titlebar({}) })
fake.mouse(200, 30)
fake.log.commands = {}
fake.key("mbtn_left")
ok(not commanded("begin-vo-dragging"),
   "a plain bar does not drag the window")
fake.log.props["window-maximized"] = false
fake.key("mbtn_left_dbl")
eq(fake.log.props["window-maximized"], false,
   "and double-clicking it does not maximize")

-- mpv refuses EVERY VO drag while the pointer is inside an input section
-- that was enabled without `allow-vo-dragging`, and a section's mouse area
-- covers the whole screen unless one is set. Our three sections set none,
-- so without the flag begin-vo-dragging above succeeds and moves nothing --
-- which is exactly how it shipped, and is invisible from inside the
-- renderer. These pin the flag onto every section that can be on screen.
for _, section in ipairs({ "mpvtk_mouse", "mpvtk_wheel", "mpvtk_thumb" }) do
    eq(fake.log.section_flags[section], "allow-vo-dragging",
       section .. " is enabled with allow-vo-dragging")
end

-- ...and the other half of that trade: the flag also re-arms mpv's OWN
-- dragging, which starts a window move from a press-and-move anywhere and
-- swallows the click. Over a UI that means dragging a scrollbar moves the
-- window, so it is off for as long as the UI owns the pointer.
eq(fake.log.props["input-builtin-dragging"], false,
   "mpv's built-in dragging is off while the UI is up")
fake.send("mpvtk-active", "no")
eq(fake.log.props["input-builtin-dragging"], true,
   "and is given back for playback, where it is mpv's own behaviour")
fake.send("mpvtk-active", "yes")
eq(fake.log.props["input-builtin-dragging"], false,
   "and taken away again when the UI comes back")

-- ========================================== client-side resize grip

-- `wsize` marks the corner. mpv has no "begin resizing" command outside
-- Wayland's own edge zone, so the renderer resizes the window the one way a
-- client always can: by writing `geometry`, which every VO honours as a
-- resize command.
local function grip(extra)
    local node = { id = "csd-grip", t = "rect", x = 378, y = 578,
                   w = 22, h = 22, wsize = "se" }
    for k, v in pairs(extra or {}) do node[k] = v end
    return node
end

local function geometry()
    return fake.log.props["geometry"]
end

fake.log.props["osd-width"] = 400
fake.log.props["osd-height"] = 600
fake.log.props["window-maximized"] = false
fake.log.props["fullscreen"] = false
fake.log.props["geometry"] = nil

scene({ grip() })
fake.mouse(390, 590)
fake.send("mpvtk-debug", fake.token({ cmd = "down", x = 390, y = 590 }))
fake.advance(1)
fake.mouse(490, 640)
eq(geometry(), "500x650",
   "dragging the grip resizes the window to the pointer plus the grab")

-- The pointer is OUTSIDE the window for most of a grow -- the corner only
-- catches up once the window has grown -- so hover goes false on the first
-- pixel of the drag. Those events still carry live coordinates, because the
-- held button keeps the pointer grabbed, and ignoring them stalls the whole
-- gesture: no resize, so the pointer never comes back inside, so no resize.
-- (Measured against a real mpv: this is what made the grip do nothing.)
fake.advance(1)
fake.observe("mouse-pos", { x = 600, y = 700, hover = false })
eq(geometry(), "610x710",
   "a resize keeps tracking the pointer once it leaves the window")

-- A floor, and the same one player_window uses -- a window dragged down to
-- nothing is one the app would then refuse to reopen at that size.
fake.advance(1)
fake.mouse(10, 10)
eq(geometry(), "320x240", "the window cannot be dragged below 320x240")

-- The release writes the final size unthrottled: the pacing above is there
-- so a fast drag does not queue dozens of resize commands, and it can drop
-- the last few pixels -- which are the ones the user actually chose.
fake.mouse(500, 500)
fake.send("mpvtk-debug", fake.token({ cmd = "up", x = 500, y = 500 }))
eq(geometry(), "510x510", "releasing writes the size the drag ended on")
fake.advance(1)
fake.mouse(700, 700)
eq(geometry(), "510x510", "and moving after the release resizes nothing")

-- The grip outranks a scrollbar gutter it shares a corner with. bar_at has
-- no z-order and a page scroller's gutter runs the full height of the window
-- at its right edge, straight through the drawn dots -- so without this the
-- one visible resize affordance page-scrolls the list instead.
scene({
    { id = "page", t = "scroll", axis = "y", x = 0, y = 0, w = 400, h = 600,
      cw = 400, ch = 4000, bar = true },
    grip(),
})
fake.log.props["geometry"] = nil
fake.mouse(390, 590)
fake.send("mpvtk-debug", fake.token({ cmd = "down", x = 390, y = 590 }))
fake.advance(1)
fake.mouse(440, 640)
eq(geometry(), "450x650", "the scrollbar gutter swallowed the resize grip")
fake.send("mpvtk-debug", fake.token({ cmd = "up", x = 440, y = 640 }))

-- Maximized, the geometry write would silently un-maximize the window
-- instead of resizing it, which is not what dragging a corner asks for.
-- (The grip is not drawn there either; this is the second lock.)
fake.log.props["window-maximized"] = true
local geo_before = geometry()
fake.mouse(390, 590)
fake.send("mpvtk-debug", fake.token({ cmd = "down", x = 390, y = 590 }))
fake.advance(1)
fake.mouse(600, 600)
eq(geometry(), geo_before, "a maximized window is not resized by the grip")
fake.log.props["window-maximized"] = false
fake.send("mpvtk-debug", fake.token({ cmd = "up", x = 600, y = 600 }))

-- ============================== the MENU key and mpvtk-focus

-- Two gestures that name a destination rather than a direction, and the
-- only two ways the 10ft user reaches things the pointer gets for free:
-- a tile's context menu (right-click) and the search box (a click).

local function navkey(k) fake.key("mpvtk_nav_" .. k) end

local function tile(id, x, y, extra)
    local node = { id = id, t = "rect", x = x, y = y, w = 100, h = 80,
                   click = true }
    for k, v in pairs(extra or {}) do node[k] = v end
    return node
end

-- MENU opens the context menu of the FOCUSED node. Anchored below the
-- node rather than over it: the menu is about that tile.
scene({ tile("t1", 0, 0, { ctx = true }), tile("t2", 200, 0, { ctx = true }) })
navkey("DOWN")                 -- engage nav mode, focus lands on a tile
navkey("RIGHT")                -- ...move it somewhere deterministic
fake.reset_events()
navkey("MENU")
local ctx = last_event("context")
ok(ctx ~= nil, "MENU opens a context menu")
if ctx then
    ok(ctx.y >= 80, "the menu opens below the node, not over it",
       "y = " .. tostring(ctx.y))
end

-- A node with no context menu is a no-op, exactly as right-clicking one
-- is -- not a menu belonging to some other node.
scene({ tile("plain", 0, 0) })
navkey("DOWN")
fake.reset_events()
navkey("MENU")
ok(last_event("context") == nil, "MENU on a node with no menu does nothing")

-- Focus by id: what a remote's search button asks for. A textbox takes
-- the keyboard too, or "focus the search box" would leave the user
-- unable to type in it.
scene({ tile("t1", 0, 0), textbox("nav-search", "") })
fake.send("mpvtk-focus", fake.token({ id = "nav-search" }))
type_text("hi")
local ch = last_event("change")
ok(ch ~= nil and ch.id == "nav-search",
   "focusing the search box lets the user type into it")

-- No id: "whatever the next scene nominates". Parked, because the page a
-- navigation lands on is a spinner first and the button it is asking for
-- does not exist yet.
scene({ tile("t1", 0, 0) })
navkey("DOWN")                          -- nav mode on
fake.send("mpvtk-focus", fake.token({}))
scene({ tile("spinner", 0, 0) })        -- still loading: nothing to focus
fake.reset_events()
scene({ tile("other", 0, 0), tile("play", 0, 100, { af = true, ctx = true }) })
navkey("MENU")
local af = last_event("context")
ok(af ~= nil and af.id == "play",
   "a parked autofocus lands on the page's own default when it arrives",
   af and af.id or "nothing focused")

-- The page being LEFT must not answer it. navigate() sends the request
-- while the outgoing scene is still up, and that scene often nominates a
-- node of its own (a detail page's Play button) -- which would swallow
-- the request and leave the arriving page with nothing focused.
scene({ tile("leaving", 0, 0, { af = true, ctx = true }),
        tile("other", 0, 100, { ctx = true }) })
-- Focus by id rather than by arrow keys: where a spatial move lands
-- depends on the nav_rect the previous test left behind, and this test
-- is about the `af` node, not about direction picking.
fake.send("mpvtk-focus", fake.token({ id = "other" }))
fake.send("mpvtk-focus", fake.token({}))
fake.reset_events()
navkey("MENU")
local stayed = last_event("context")
ok(stayed ~= nil and stayed.id ~= "leaving",
   "the page being left does not answer an autofocus request",
   stayed and stayed.id or "nothing focused")
-- ...and the request is still live for the page that arrives.
scene({ tile("arrived", 0, 0, { ctx = true }),
        tile("play", 0, 100, { af = true, ctx = true }) })
fake.reset_events()
navkey("MENU")
local landed = last_event("context")
ok(landed ~= nil and landed.id == "play",
   "it lands on the arriving page instead",
   landed and landed.id or "nothing focused")

-- ...but the user steering outranks it: an arrow key before the page
-- lands cancels the request, or focus would be yanked away mid-move.
-- The nodes the user is moving between survive the push, or this would
-- prove nothing but "a vanished focus is dropped".
local moving = { tile("top", 0, 0, { ctx = true }),
                 tile("below", 0, 100, { ctx = true }) }
scene(moving)
navkey("DOWN")
fake.send("mpvtk-focus", fake.token({}))
navkey("DOWN")                          -- the user moves focus themselves
scene({ moving[1], moving[2],
        tile("play", 0, 200, { af = true, ctx = true }) })
fake.reset_events()
navkey("MENU")
local kept = last_event("context")
ok(kept ~= nil and kept.id ~= "play",
   "an arrow key cancels a parked autofocus",
   kept and kept.id or "nothing focused")

-- ================================ hover enter/leave (hev)

-- The play chip on a tile is drawn by PYTHON, so the renderer has to say
-- which tile the pointer is on. Only nodes carrying `hev` are reported --
-- a row of cast members should not cost a scene rebuild per face crossed.

local function point(x, y)
    fake.observe("mouse-pos", { x = x, y = y, hover = true })
end

local function hover_events()
    local out = {}
    for _, e in ipairs(fake.log.events) do
        if type(e) == "table" and (e.t == "hover" or e.t == "hover_end") then
            out[#out + 1] = e.t .. ":" .. tostring(e.id)
        end
    end
    return table.concat(out, " ")
end

scene({ tile("tile-a", 0, 0, { hev = true }),
        tile("tile-b", 200, 0, { hev = true }),
        tile("plain", 400, 0) })
point(1000, 600)                       -- start clear of everything
fake.reset_events()
point(50, 40)
eq(hover_events(), "hover:tile-a", "entering a tile reports it")

fake.reset_events()
point(250, 40)
-- ENTER BEFORE LEAVE. The app draws from the id it is entering, and a leave
-- that arrived first would describe a state already over: it would blank the
-- control that had just been asked for.
eq(hover_events(), "hover:tile-b hover_end:tile-a",
   "crossing tiles reports the arrival before the departure")

fake.reset_events()
point(450, 40)
eq(hover_events(), "hover_end:tile-b",
   "a tile that does not opt in reports nothing but the departure")

-- The chip sits INSIDE its tile and is its own node, so reaching it is
-- leaving the tile. Both are reported; the app resolves them to one tile.
scene({ tile("tile-a", 0, 0, { hev = true }),
        { id = "tile-a-play", t = "rect", x = 30, y = 20, w = 40, h = 40,
          click = true, hev = true } })
point(5, 5)
fake.reset_events()
point(50, 40)
eq(hover_events(), "hover:tile-a-play hover_end:tile-a",
   "moving onto the control over a tile reports it before the tile's leave")

fake.reset_events()
point(1000, 600)
eq(hover_events(), "hover_end:tile-a-play", "leaving the window reports it")

-- A node that goes away mid-hover has to be reported too: the app is
-- holding a control open for it and will never hear otherwise.
point(50, 40)
fake.reset_events()
scene({})
eq(hover_events(), "hover_end:tile-a-play",
   "a hovered node leaving the scene reported no departure")

-- ================= a hover mpv lost, and one it never had (#700)
--
-- mpv sets `mouse-pos.hover` only from MOUSE_ENTER / MOUSE_LEAVE and ignores
-- every X11 crossing whose mode is not NotifyNormal, so a WM that grabs the
-- pointer, maximizes the window under it and ungrabs -- Cinnamon's title-bar
-- double click -- strands the flag at false with the pointer in the middle of
-- the window. That cost the whole UI its mouse: no hover ring, and every
-- click hit-tested at -1,-1.
--
-- An in-window position with hover=false is AMBIGUOUS, and the renderer
-- resolves it by what follows rather than by guessing (see the observer).
-- These are the three rules that has to satisfy.

local function mouse_state()
    fake.reset_events()
    fake.send("mpvtk-debug", fake.token({ cmd = "state" }))
    return (last_event("debug_state") or {}).mouse or {}
end

--- Let the leave grace expire with nothing else arriving.
local function idle_out()
    fake.advance(0.3)
    fake.fire_timers()
end

-- **The pointer is believed, with no leave needed first.** An enter can be
-- lost on its own -- a renderer that loads while the pointer is outside, a
-- crossing eaten by a grab -- and then there is no leave to have seen.
scene({ tile("tile-a", 0, 0, { hev = true }) })
point(300, 300)                      -- somewhere else, and clear of the tile
fake.reset_events()
fake.observe("mouse-pos", { x = 50, y = 40, hover = false })
eq(hover_events(), "hover:tile-a",
   "an in-window pointer was ignored because mpv said hover=false")
eq(mouse_state().hover, true, "the stranded hover flag was not corrected")

-- ...and it keeps being believed. The flag stays stranded for the rest of the
-- session, so EVERY event after it is hover=false too; correcting one must
-- not look like a crossing, or the UI alternates between hovering and
-- leaving one motion apart.
for _, xy in ipairs({ { 55, 42 }, { 60, 44 }, { 65, 46 } }) do
    fake.observe("mouse-pos", { x = xy[1], y = xy[2], hover = false })
end
eq(mouse_state().hover, true, "a stranded hover flag was only corrected once")

-- The click that used to go nowhere: it hit-tests state.mouse, the same as
-- the ring, so it is asserted separately.
fake.reset_events()
fake.key("mbtn_left")        -- press...
fake.key_up("mbtn_left")     -- ...and release
ok(last_event("click") ~= nil and last_event("click").id == "tile-a",
   "a click at a stranded-hover position hit nothing")

-- **A real leave still lands** -- which is what the playback HUD's auto-hide
-- is built on. It usually arrives carrying a MOVED position: mpv clears the
-- flag when the LeaveNotify is fed but commits a motion's position when the
-- command is dequeued, drains the queue per iteration and reports the
-- property once per drain, so an ordinary flick out of the window is ONE
-- event holding the last in-window position and hover=false. Measured at 27
-- of 30 crossings on a real mpv. Nothing follows it, and that is the tell.
point(50, 40)
fake.reset_events()
fake.observe("mouse-pos", { x = 60, y = 45, hover = false })
idle_out()
eq(hover_events(), "hover_end:tile-a", "a real leave never landed")
local m = mouse_state()
ok(m.hover == false and m.x == -1,
   "a leave that stopped reporting was not committed",
   string.format("hover=%s x=%s", tostring(m.hover), tostring(m.x)))

-- The same from the middle of the window, which is what a fast exit reports.
point(50, 40)
fake.observe("mouse-pos", { x = 640, y = 360, hover = false })
idle_out()
eq(mouse_state().hover, false,
   "a leave reporting a position mid-window was not committed")

-- An unchanged position is not motion at all: nothing to be ambiguous about,
-- so that leave is taken at once rather than after the grace.
point(50, 40)
fake.observe("mouse-pos", { x = 50, y = 40, hover = false })
eq(mouse_state().hover, false, "an unmoved leave waited for the grace")

-- Out of the window, likewise. The position only moves at all out there
-- while a button is held (X keeps delivering to the grab), and believing it
-- would light up controls under a pointer somewhere else entirely.
point(50, 40)
fake.observe("mouse-pos", { x = 1400, y = 900, hover = false })
eq(mouse_state().hover, false, "an out-of-window position was taken as a hover")

-- **A gesture is never interrupted by a leave.** A spurious one mid-drag is
-- where believing it costs the most: the scroll stops following the pointer
-- and does not resume, with the button still held.
scene({ vscroll("body", 400, 4000, { bar = true }) })
fake.advance(0.1)
fake.fire_timers()
local bar = ((function()
    fake.reset_events()
    fake.send("mpvtk-debug", fake.token({ cmd = "state" }))
    return (last_event("debug_state") or {}).bars or {}
end)())["body"]
ok(bar ~= nil, "fixture: the scroll container drew no scrollbar")
if bar then
    fake.mouse(bar.x + 2, bar.thumb_y + 2)
    fake.send("mpvtk-debug",
              fake.token({ cmd = "down", x = bar.x + 2, y = bar.thumb_y + 2 }))
    local before = offset("body")
    -- BELOW the window, which is where this actually happens: drag a
    -- scrollbar past the bottom edge and the pointer is genuinely outside,
    -- so hover goes false with the button still held. The gesture owns the
    -- pointer and must keep being fed -- the in-window half needs no rule of
    -- its own, since a moved in-window position is believed anyway.
    fake.observe("mouse-pos", { x = bar.x + 2, y = 900, hover = false })
    ok(offset("body") > before,
       "a leave mid-drag stopped the scrollbar following the pointer")
    fake.send("mpvtk-debug", fake.token({ cmd = "up", x = bar.x + 2,
                                          y = 900 }))
end
point(50, 40)


-- ===================================================== scroll snapping
--
-- Scrolling glides. It quantizes to row boundaries only when the frames a
-- gesture asks for cost more than the time between them -- or when the user
-- (or an external mpv) forces it on. Neither half is sufficient alone, and
-- both of those are tested below, because each is a rule this replaced: a
-- rate threshold snapped a brisk flick on a machine that draws in 3ms, and
-- snapping unconditionally (what came before that) charged everyone for a
-- mitigation almost nobody needed.
--
-- fake.set_draw_cost is what makes any of it testable. The renderer times
-- its own frames, so "this machine is slow" is a number a test can state
-- rather than a symptom it has to imitate.
--
-- Two observables, deliberately different things:
--   * where the offset SETTLES. During a gesture the stored offset stays
--     continuous and only the drawing is quantized, so the visible
--     consequence is at the end: a gesture that outran the renderer is
--     released onto the detent it was already showing, and one that did not
--     is left exactly where the pixels put it.
--   * how many frames were PAINTED. The cadence comes from the same
--     measurement, so an expensive scene draws the same gesture in fewer.

-- Row height. The wheel step is quantized to divide a row evenly (see
-- on_wheel: n = floor(250/80) = 3, step = 83.33), so EVERY THIRD NOTCH lands
-- on a boundary with nothing snapping at all. There is no row height that
-- avoids this -- the quantization is the point of it -- so the assertions
-- below do not lean on where the offset happens to land. They read what was
-- painted.
local SNAP = 250

--- A snapping container with a marker image inside it.
---
--- The marker is what makes the DRAWN offset observable, and observing it is
--- the whole business here: during a gesture the stored offset stays
--- continuous by design and only the drawing is quantized, so a suite that
--- watches `user-data/mpvtk/scroll` is watching the one number this feature
--- does not change. (Measured: with the offset as the only observable, both
--- "quantize always" and "quantize never" passed the entire suite.)
---
--- It is taller than any offset under test, so it is always on screen, and
--- `overlay-add` reports the source pixel it started copying from -- which is
--- exactly the scroll offset the renderer drew. See drawn_offset.
local MARK_W, MARK_H = 40, 400000

local function snapper(id, ch)
    return { { id = id, t = "scroll", axis = "y", x = 0, y = 0,
               w = 400, h = 300, cw = 400, ch = ch or 3000,
               snap = SNAP, snap_off = 0 },
             { id = id .. "-mark", t = "img", sc = id, src = "/" .. id,
               x = 0, y = 0, w = MARK_W, h = MARK_H,
               iw = MARK_W, ih = MARK_H } }
end

--- The offset the renderer last PAINTED for a container, in pixels.
---
--- overlay-add is (slot, x, y, src, byte-offset, fmt, w, h, stride). The
--- marker starts at content y=0 and the viewport at screen y=0, so the piece
--- painted always begins `drawn offset` rows down the source bitmap, and the
--- byte offset is that times the stride.
local function drawn_offset(id)
    local found
    for _, c in ipairs(fake.log.commands) do
        if c[1] == "overlay-add" and c[5] == "/" .. id then
            found = tonumber(c[6]) / tonumber(c[10])
        end
    end
    return found
end

--- Did the renderer paint this container at `want`?
---
--- Within a pixel, because a bitmap is copied from whole source pixels:
--- draw_image floors, so a stored offset of 833.33 is painted at 834. That
--- rounding is the difference between "glides" and "steps by 250" by a
--- factor of 250, so a pixel of slack costs the assertion nothing.
local function painted_at(id, want)
    local got = drawn_offset(id)
    return got ~= nil and math.abs(got - want) < 1.5
end

--- Was the last painted frame aligned to a row boundary?
local function drawn_on_row(id)
    local off = drawn_offset(id)
    if off == nil then return nil end
    return math.abs(off - math.floor(off / SNAP + 0.5) * SNAP) < 0.5
end

--- Is the stored offset sitting on a row boundary? Used only where the
--- question really is about the stored offset: where a gesture RELEASED.
local function on_row(id)
    local off = offset(id)
    return math.abs(off - math.floor(off / SNAP + 0.5) * SNAP) < 0.5
end

--- Let every pending timer come due: the gesture release, and the frame it
--- is meant to outlive.
local function settle()
    fake.advance(1.0)
    fake.fire_timers()
end

--- `n` wheel notches `gap` apart, painting after every second one.
---
--- Painting DURING the gesture is the point: the renderer has no idea what
--- a frame costs until it has drawn one, and a still frame is cheaper than
--- a scrolling one (at rest every overlay re-issue is skipped), so a
--- gesture is judged on its own first frames rather than on an estimate
--- from before it began.
---
--- Every second notch rather than every one because that is the shape mpv
--- delivers: the script cannot take an event while it is inside a frame, so
--- what arrives during one is handed over together afterwards. Painting on
--- every notch would charge the whole draw to the gap between two events
--- and make the gesture look slower than the wheel actually is.
local function fling(n, gap)
    for i = 1, n do
        fake.advance(gap)
        fake.key("wheel_down")
        if i % 2 == 0 then fake.fire_timers() end
    end
end

--- A fresh container over a settled renderer: no inherited offset, no
--- latched gesture, and -- because the cost estimate is smoothed rather
--- than reset -- no memory of how expensive the last scenario was.
local function fresh(id, cost, ch)
    fake.set_draw_cost(0)
    settle()
    for _ = 1, 8 do
        scene(snapper(id, ch))
        fake.advance(0.2)
        fake.fire_timers()
    end
    fake.mouse(10, 10)
    fake.advance(5.0)
    fake.set_draw_cost(cost or 0)
end

-- A fling on a renderer that keeps up glides. This is the case the rate
-- rule got wrong: the wheel here is going as fast as any fling, and that on
-- its own is not a reason to make anyone's scrolling step.
--
-- Asserted on the PAINTED frame, mid-gesture, before any release: what the
-- user sees is the claim, and it is the only form of it that can fail for
-- the right reason.
fresh("quick", 0)
fling(10, 0.02)
ok(drawn_offset("quick") > 0, "a fling on a fast renderer scrolls")
ok(painted_at("quick", offset("quick")),
   "a fling paints where the wheel put it when frames keep up",
   string.format("painted %s, wheel left it at %s",
                 tostring(drawn_offset("quick")), tostring(offset("quick"))))
settle()
ok(not on_row("quick"), "and it is left there when the gesture ends",
   string.format("offset %s was pulled to a row boundary",
                 tostring(offset("quick"))))

-- The same fling where a frame costs 50ms cannot have one per notch, and
-- quantizing is what buys the frames back.
fresh("dear", 0.05)
fling(10, 0.02)
ok(drawn_offset("dear") > 0, "a fling on a slow renderer still scrolls")
ok(drawn_on_row("dear"),
   "a slow renderer paints on a row boundary while the fling is live",
   string.format("painted %s, which is not on one",
                 tostring(drawn_offset("dear"))))
ok(drawn_offset("dear") ~= offset("dear"),
   "quantizing moved the stored offset instead of just the picture",
   "the scroller itself must stay continuous; only the drawing snaps")
settle()
ok(on_row("dear"), "and the gesture releases onto the detent it was showing",
   string.format("offset %s is not on one", tostring(offset("dear"))))

-- ...and cost alone is not a reason either. The same 50ms frames, asked for
-- one a second, are latency rather than stutter: each notch gets a whole
-- second of budget and there is nothing to save.
fresh("dear-slow", 0.05)
fling(4, 1.0)
settle()
ok(offset("dear-slow") > 0, "a slow scroll on a slow renderer scrolls")
ok(not on_row("dear-slow"),
   "an expensive frame asked for once a second still glides",
   string.format("offset %s snapped", tostring(offset("dear-slow"))))

-- The test is per gesture, so a fling that ends hands scrolling back: the
-- next unhurried notch must not be dragged to a boundary by the last one.
fresh("again", 0.05)
fling(10, 0.02)
settle()
ok(on_row("again"), "the fling landed aligned")
fake.advance(1.0)
fake.key("wheel_down")
settle()
ok(not on_row("again"), "scrolling is free again once the fling ends",
   string.format("offset %s snapped", tostring(offset("again"))))

-- And the estimate lets go as readily as it latches: the same fast fling
-- over the same container, once the frames are cheap again, glides.
fake.set_draw_cost(0)
for _ = 1, 8 do
    scene(snapper("again"))
    fake.advance(0.2)
    fake.fire_timers()
end
fake.advance(5.0)
local before_cheap = offset("again")
fling(10, 0.02)
settle()
ok(offset("again") > before_cheap, "the cheap fling scrolled")
ok(not on_row("again"), "a scene that got cheap again stops snapping",
   string.format("offset %s snapped", tostring(offset("again"))))

-- A container with no snap declared has nothing to quantize to, and a
-- fling over it must not wedge anything.
fake.set_draw_cost(0)
settle()
scene({ vscroll("plainfling", 300, 3000) })
fake.mouse(10, 10)
fake.advance(5.0)
fake.set_draw_cost(0.05)
fling(10, 0.02)
settle()
ok(offset("plainfling") > 0, "a fling over an unsnapped container scrolls",
   string.format("offset stayed at %s", tostring(offset("plainfling"))))

-- force_scroll_snapping (and external mpv) quantize the DRAWING at any
-- cost and any rate. The stored offset still glides -- the setting changes
-- what is painted, not where the scroller is -- so a slow scroll under it
-- is left off the boundary exactly as it is without.
fake.send("mpvtk-wheel", fake.token({ force_snap = true }))
fresh("forced", 0)
fling(4, 1.0)
ok(offset("forced") > 0, "forced snapping still scrolls")
ok(drawn_on_row("forced"), "forced snapping paints on a row boundary",
   string.format("painted %s, which is not on one",
                 tostring(drawn_offset("forced"))))
ok(not on_row("forced"), "forced snapping does not move the scroller",
   string.format("offset %s was pulled to a boundary",
                 tostring(offset("forced"))))
fake.send("mpvtk-wheel", fake.token({ force_snap = false }))
-- ...and turning it back off paints freely again on the same container.
fresh("unforced", 0)
fling(4, 1.0)
ok(painted_at("unforced", offset("unforced")),
   "clearing force_snap left the drawing quantized",
   string.format("painted %s, wheel left it at %s",
                 tostring(drawn_offset("unforced")),
                 tostring(offset("unforced"))))

-- ================================================= duration-aware cadence
--
-- The other half of the same measurement. Frames are scheduled at what one
-- costs, not at a rate picked for the worst machine: a gesture over an
-- expensive scene is painted fewer times, which is what leaves the script
-- room to take the events still arriving (mpv drops them -- "too many
-- queued events" -- when it does not).

--- Frames painted while scrolling for `span` seconds of clock.
---
--- The pump is finer than any cadence under test, so what is counted is
--- how often the renderer chose to draw and not how often it was asked.
local SPAN, DEAR = 1.0, 0.05

local function frames_for(id, cost)
    -- Content far taller than the gesture can exhaust: a scroller that
    -- reaches the bottom stops asking to be drawn, and the count would
    -- become "how long the container is" instead of "how often it drew".
    fresh(id, cost, 200000)
    local t0, drawn = fake.clock(), fake.log.draws
    while fake.clock() - t0 < SPAN do
        fake.advance(0.005)
        fake.key("wheel_down")
        fake.fire_timers()
    end
    return fake.log.draws - drawn
end

local quick_frames = frames_for("pace-quick", 0)
local dear_frames = frames_for("pace-dear", DEAR)
ok(quick_frames > 0, "a cheap scene paints while scrolling")
ok(dear_frames > 0, "an expensive scene still paints while scrolling",
   "a fling over it painted nothing at all")

-- There used to be a `dear_frames < quick_frames` assertion here. It could
-- not fail: the fake's osd:update advances its own clock by draw_cost, so a
-- 50ms frame arithmetically cannot fit as many draws into a fixed span as a
-- 0ms one, whatever the renderer decides. It passed with pacing removed
-- entirely. What follows is the invariant that actually distinguishes them:
-- drawing never takes more than its declared share of the clock. Scheduling
-- frames one rcost apart would satisfy "fewer than a cheap scene" too, and
-- leave the script no room to take the wheel events still coming.
ok(dear_frames * DEAR <= SPAN * 0.7,
   "drawing leaves the clock room for the events still arriving",
   string.format("%d frames x %.0fms fills %.0f%% of a %.0fms gesture",
                 dear_frames, DEAR * 1000,
                 dear_frames * DEAR / SPAN * 100, SPAN * 1000))

-- The cost that removing the external-mpv override rests on. Out of process
-- an image is a file mpv opens and mmaps rather than an address in this
-- process, so the expensive part of a scrolling frame is the overlay
-- re-issues -- and the claim is that those happen INSIDE the timed region,
-- so an external mpv is measured to be slow rather than assumed to be.
--
-- Here the osd update is free and only overlay-add costs anything. If the
-- measurement stopped before flush_overlays, this scene would read as cheap
-- and glide.
fresh("mmap", 0)
fake.set_overlay_cost(0.05)
fling(10, 0.02)
ok(drawn_on_row("mmap"),
   "an expensive overlay-add is not counted as part of a frame",
   string.format("painted %s, which is not on a row boundary",
                 tostring(drawn_offset("mmap"))))
fake.set_overlay_cost(0)

-- ================================================== overlay slot order
--
-- mpv composites overlay bitmaps in ascending slot order, so slot order IS
-- stacking order for anything that overlaps. Two things have to hold at
-- once, and they pull against each other:
--
--   * correctness -- a bitmap drawn over another (the hover play chip over
--     its row's artwork) must hold a HIGHER slot than the one it covers;
--   * cheapness -- getting there must not re-issue every other overlay on
--     the page. renumber_overlays is the fallback that does, and it is
--     visible as the whole page flickering.
--
-- The trap is that the two are only in tension once something has left a
-- LOW slot behind, which on a library page is constant: every row that
-- scrolls out of view frees one. A chip taking the lowest free slot then
-- lands under the very row it is drawn on.

--- Full-width artwork rows, `n` of them, optionally with a chip drawn on
--- one and optionally missing the first (scrolled out of view).
local function strip_page(opts)
    opts = opts or {}
    local nodes = {}
    for r = (opts.drop_first and 2 or 1), 5 do
        nodes[#nodes + 1] = { id = "strip" .. r, t = "img",
                              src = "/strip" .. r, x = 0, y = (r - 1) * 140,
                              w = 1200, h = 130, iw = 1200, ih = 130 }
    end
    if opts.chip then
        nodes[#nodes + 1] = { id = "chip", t = "img", src = "/chip",
                              x = 60, y = (opts.chip - 1) * 140 + 40,
                              w = 64, h = 64, iw = 64, ih = 64 }
    end
    return nodes
end

--- Paint a scene and report the overlay traffic it cost.
local function paint(nodes)
    local before = #fake.log.commands
    scene(nodes)
    fake.advance(1.0)
    fake.fire_timers()
    local adds, removes = 0, 0
    for i = before + 1, #fake.log.commands do
        local c = fake.log.commands[i]
        if c[1] == "overlay-add" then adds = adds + 1
        elseif c[1] == "overlay-remove" then removes = removes + 1 end
    end
    return adds, removes
end

--- The slot an overlay src was last issued to. Slot is argument 2 and the
--- source is argument 5 of overlay-add (x, y, src, offset, ...).
local function slot_of(src)
    local found
    for _, c in ipairs(fake.log.commands) do
        if c[1] == "overlay-add" and c[5] == src then
            found = tonumber(c[2])
        end
    end
    return found
end

scene({})                       -- drop every prior overlay
paint(strip_page())
local freed_adds = paint(strip_page({ drop_first = true }))
eq(freed_adds, 0, "a row leaving the page re-issued the others")

local chip_adds = paint(strip_page({ drop_first = true, chip = 4 }))
eq(chip_adds, 1, "showing the hover chip re-issued more than the chip")
ok(slot_of("/chip") > slot_of("/strip4"),
   "the chip is composited above the row it is drawn on",
   string.format("chip is slot %s, its row is slot %s",
                 tostring(slot_of("/chip")), tostring(slot_of("/strip4"))))

-- Moving the pointer between rows is one departure and one arrival, and
-- must stay that cheap however many times it happens.
paint(strip_page({ drop_first = true }))
local moved_adds = paint(strip_page({ drop_first = true, chip = 2 }))
eq(moved_adds, 1, "hovering a second row re-issued more than the chip")
ok(slot_of("/chip") > slot_of("/strip2"),
   "the chip stays above the row after moving to another",
   string.format("chip is slot %s, its row is slot %s",
                 tostring(slot_of("/chip")), tostring(slot_of("/strip2"))))

-- ========================================================= disabled

-- A disabled control is on screen and inert: no click, no spatial-nav
-- focus. It still ABSORBS the pointer -- the press must stop there rather
-- than reaching whatever it happens to sit over, which is why node_at
-- keeps returning it and each consumer drops it instead.

local function btn(id, row, extra)
    local node = { id = id, t = "rect", x = 0, y = (row or 0) * 40,
                   w = 200, h = 30, click = true }
    for k, v in pairs(extra or {}) do node[k] = v end
    return node
end

scene({ btn("live", 0), btn("dead", 1, { dis = true }) })
fake.reset_events()
click("dead")
ok(last_event("click") == nil, "a disabled node does not fire its click")
click("live")
local fired = last_event("click")
ok(fired ~= nil and fired.id == "live",
   "an enabled node beside it still fires")

-- Underneath, not beside: the disabled node covers the live one exactly.
scene({ btn("under", 0), btn("over", 0, { dis = true }) })
fake.reset_events()
click("over")
ok(last_event("click") == nil,
   "a disabled node absorbs the click instead of passing it down")

-- Spatial nav must skip it, or a remote lands focus on something that
-- cannot be activated and the ring appears to get stuck.
scene({ btn("first", 0), btn("skipme", 1, { dis = true }), btn("last", 2) })
fake.send("mpvtk-debug", fake.token({ cmd = "nav", id = "first" }))
fake.send("mpvtk-debug", fake.token({ cmd = "nav", dir = "down" }))
fake.reset_events()
fake.send("mpvtk-debug", fake.token({ cmd = "nav", action = "enter" }))
local navd = last_event("click")
ok(navd ~= nil and navd.id == "last",
   "arrowing down steps over the disabled node",
   navd and navd.id or "nothing activated")

-- A disabled textbox takes no focus either, so typing cannot reach it.
scene({ textbox("ro", "keep"), btn("elsewhere", 2) })
scene({ { id = "ro", t = "textbox", x = 0, y = 0, w = 200, h = 30,
          size = 18, text = "keep", dis = true }, btn("elsewhere", 2) })
fake.reset_events()
click("ro")
type_text("Z")
click("elsewhere")
ok(last_event("commit") == nil, "a disabled textbox cannot be edited")

-- A disabled control that mutes itself in Python carries ONLY `dis` -- no
-- click, no hover, no tip. node_at has to keep returning it anyway, or the
-- press it is supposed to swallow reaches whatever it sits over. Over the
-- playback HUD that is bare video, where a click toggles pause.
scene({ { id = "card", t = "rect", x = 0, y = 0, w = 400, h = 200,
          click = true },
        { id = "muted", t = "rect", x = 50, y = 50, w = 120, h = 40,
          dis = true } })
fake.reset_events()
fake.send("mpvtk-debug", fake.token({ cmd = "click", x = 100, y = 70 }))
eq(last_event("click"), nil,
   "a press on a self-muted disabled control fell through to the node "
   .. "underneath")

-- ================================================ thumb-button sections

-- The thumb buttons are the browser's Back/Forward, but over a FILM they
-- are whatever the user has under them -- mpv's playlist-prev/next, or the
-- shim's own chapter nav. So they are declared in their own key-binding
-- section, which a summoned playback HUD leaves disabled: an accidental
-- thumb press must not take the player away, and there was no reason for
-- the HUD to own a second way to dismiss itself when ESC already does.
ok(fake.log.sections["mpvtk_thumb"] ~= nil,
   "the thumb buttons have no section of their own to decline")
ok((fake.log.sections["mpvtk_thumb"] or {})["mbtn_back"]
   and (fake.log.sections["mpvtk_thumb"] or {})["mbtn_forward"],
   "the thumb buttons are not in mpvtk_thumb")
ok(not (fake.log.sections["mpvtk_mouse"] or {})["mbtn_back"],
   "mbtn_back is still in the group the HUD keeps enabled")
ok(not (fake.log.sections["mpvtk_mouse"] or {})["mbtn_forward"],
   "mbtn_forward is still in the group the HUD keeps enabled")

-- set_key_bindings DEFINES a section; it does not enable it. Browse owns
-- these from the moment the renderer loads.
eq(fake.log.enabled["mpvtk_thumb"], true,
   "the thumb section was never enabled at load")

-- ...and it has to come back afterwards. Two paths reach browse WITHOUT a
-- mpvtk-active transition -- startup (state.active begins true) and browse
-- resuming from a summoned HUD (phud_summon set active itself) -- so a
-- section the HUD disabled would otherwise stay disabled for the session,
-- and mouse Back would do nothing in the library.
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-active", "yes")            -- the no-op transition
eq(fake.log.enabled["mpvtk_thumb"], true,
   "an already-active mpvtk-active left the thumb section disabled")

fake.send("mpvtk-hud", "yes", fake.token({ hide = 4, mode = "hover" }))
-- (hud_pointer is defined further down; this block runs before it)
fake.observe("mouse-pos", { x = 600, y = 300, hover = true })
fake.observe("mouse-pos", { x = 600, y = 310, hover = true })  -- summons
eq(fake.log.enabled["mpvtk_thumb"], false,
   "a summoned HUD did not decline the thumb buttons")
fake.send("mpvtk-active", "yes")            -- browse resumes from the HUD
eq(fake.log.enabled["mpvtk_thumb"], true,
   "browse came back from a summoned HUD with mouse Back still dead")

-- The nav keys take the SAME no-op transition, and were the other half of
-- it. ui_suspend drops them for playback (the arrows are mpv's seek keys
-- there) and only ui_resume puts them back -- which is below the early
-- return. A summoned HUD calls ui_resume(no_nav) itself, correctly leaving
-- them off, and then browse resumed without ever binding them again: no
-- arrow, ENTER or TAB navigation in the library for the rest of the
-- session, from the first video played.
ok(fake.log.keybinds["mpvtk_nav_UP"] ~= nil,
   "browse came back from a summoned HUD with arrow navigation dead")
ok(fake.log.keybinds["mpvtk_nav_ENTER"] ~= nil,
   "browse came back from a summoned HUD with ENTER dead")
fake.send("mpvtk-hud", "no")

-- ============================================== scrollbar gutter is painted

-- layout.py reserves the gutter whenever a view HAS a bar, not only when its
-- content currently overflows -- it has no choice, because a full-bleed
-- header sizes itself during build, before anything has been measured, and
-- can only assume. So the reserved strip has to be painted here whether or
-- not there is anything to scroll, or an edge-to-edge backdrop stops 10px
-- short of an edge with nothing drawn at it [iw: "just a grey void on the
-- right side"].

local SUNKEN = "2a2a2a"                 -- state.tok.control_sunken
local THUMB = "666666"                  -- state.tok.scrollbar_thumb

local function filled(colour)
    local n = 0
    for _, sh in ipairs(fake.shapes()) do
        if sh.fill == colour then n = n + 1 end
    end
    return n
end

--- Push a scene and return with only ITS rectangles recorded. The reset
--- goes before the scene, not after the frame: the renderer paints when
--- something changed, so resetting afterwards and waiting for another frame
--- records nothing at all.
local function paint(nodes)
    fake.reset_draw()
    scene(nodes)
    fake.advance(0.1)
    fake.fire_timers()
end

paint({ vscroll("fits", 600, 400, { bar = true }) })
ok(filled(SUNKEN) == 1,
   "nothing was drawn in the gutter of a view with nothing to scroll",
   string.format("%d sunken rects", filled(SUNKEN)))
ok(filled(THUMB) == 0,
   "a thumb was drawn in a scrollbar that cannot be moved")

fake.reset_events()
fake.send("mpvtk-debug", fake.token({ cmd = "state" }))
ok(((last_event("debug_state") or {}).bars or {})["fits"] == nil,
   "an unscrollable bar left a drag target behind")

-- ...and the scrollable case still draws both, so the assertions above are
-- about the empty channel rather than about the scrollbar having gone away.
paint({ vscroll("over", 600, 6000, { bar = true }) })
ok(filled(SUNKEN) == 1 and filled(THUMB) == 1,
   "a scrollable view lost its track or its thumb",
   string.format("%d track, %d thumb", filled(SUNKEN), filled(THUMB)))

-- A view with no bar at all reserves nothing and must paint nothing.
paint({ vscroll("bare", 600, 6000) })
ok(filled(SUNKEN) == 0 and filled(THUMB) == 0,
   "a view with no scrollbar drew one anyway")

-- ============================================== scrollbar drag anchoring

-- The thumb is grabbed at a point, and that point stays under the pointer.
-- It used to be a delta from a parked offset multiplied by a LIVE
-- scroll_max, so a scroller that grew mid-drag mapped the same pointer
-- movement onto a bigger jump while the thumb got shorter -- and slid out
-- from under the cursor. That is #617 as the reporter met it: pages landing
-- while they dragged.

local function bars(id)
    fake.reset_events()
    fake.send("mpvtk-debug", fake.token({ cmd = "state" }))
    return ((last_event("debug_state") or {}).bars or {})[id]
end

local function repaint()
    fake.advance(0.1)
    fake.fire_timers()
end

scene({ vscroll("lib", 600, 6000, { bar = true }) })
repaint()
local bar = bars("lib")
ok(bar ~= nil, "the scroll container drew no scrollbar")

local GRAB = 5                          -- where on the thumb it is held
local py = bar.thumb_y + GRAB
fake.mouse(bar.x + 3, py)
fake.key("mbtn_left")                   -- press: the drag starts
py = py + 120
fake.mouse(bar.x + 3, py)
repaint()
ok(math.abs(bars("lib").thumb_y - (py - GRAB)) <= 1,
   "the thumb did not follow the pointer",
   string.format("thumb at %s, pointer holding %s",
                 tostring(bars("lib").thumb_y), tostring(py - GRAB)))

-- A page lands mid-drag: the content doubles under the thumb. The frame
-- that draws it is what re-measures the thumb, so the next pointer movement
-- is the one that has to land right -- and it does, because the grab is a
-- point on the thumb rather than a distance from an offset.
scene({ vscroll("lib", 600, 12000, { bar = true }) })
repaint()
py = py + 40
fake.mouse(bar.x + 3, py)
repaint()
ok(math.abs(bars("lib").thumb_y - (py - GRAB)) <= 1,
   "the thumb slid out from under the pointer when the content grew",
   string.format("thumb at %s, pointer holding %s",
                 tostring(bars("lib").thumb_y), tostring(py - GRAB)))

-- Release, so the drag does not eat the pointer for the tests below.
fake.send("mpvtk-debug", fake.token({ cmd = "click", x = 5, y = 5 }))

-- =================================================== HUD auto-hide

-- The controls' auto-hide is a policy (hud_autohide), and the pointer
-- resting ON them holds it off in every mode but 'always'. Reaching for a
-- button must not be a race against the timer.

-- Summoning the HUD is the one place the provisional guess is VISIBLE: the
-- flick that takes the pointer off the window is an in-window position with
-- hover=false, and believing it there raises the controls as you move away
-- from them. So the summon waits for a second such event -- the pointer
-- demonstrably still moving inside -- while everything else stays live on
-- the first, which is the whole point of being provisional.
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes", fake.token({ hide = 4, mode = "hover" }))
fake.observe("mouse-pos", { x = 600, y = 300, hover = true })   -- records only
fake.reset_events()
fake.observe("mouse-pos", { x = 600, y = 360, hover = false })  -- the flick out
fake.send("mpvtk-debug", fake.token({ cmd = "state" }))
ok((last_event("debug_state") or {}).phud_shown ~= true,
   "moving the pointer OFF the window summoned the playback HUD")
fake.observe("mouse-pos", { x = 600, y = 420, hover = false })  -- still moving
fake.reset_events()
fake.send("mpvtk-debug", fake.token({ cmd = "state" }))
ok((last_event("debug_state") or {}).phud_shown == true,
   "a pointer still moving inside never summoned the HUD")
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-active", "yes")

local function hud_engage(opts)
    fake.send("mpvtk-hud", "no")
    fake.send("mpvtk-hud", "yes", fake.token(opts or {}))
    -- Pointer movement is what summons an idle HUD, and the first event
    -- after engaging only records the position.
    fake.observe("mouse-pos", { x = 600, y = 300, hover = true })
    fake.observe("mouse-pos", { x = 600, y = 360, hover = true })
    -- What hud.py pushes once summoned: two bars carrying the ids
    -- phud_busy tests the pointer against.
    scene({ { id = "hud-topbar", t = "rect", x = 0, y = 0, w = 1280, h = 60 },
            { id = "hud-bar", t = "rect", x = 0, y = 640, w = 1280, h = 80 } })
end

local function hud_pointer(x, y)
    fake.observe("mouse-pos", { x = x, y = y, hover = true })
end

local function hud_wait()
    fake.advance(30)
    fake.fire_timers()
end

local function hud_hidden()
    local ev = last_event("hud")
    return ev ~= nil and ev.active == false
end

hud_engage({ hide = 4, mode = "hover" })
hud_pointer(600, 680)          -- onto the bar
fake.reset_events()
hud_wait()
ok(not hud_hidden(), "the controls stay up while the pointer is on them")

hud_pointer(600, 300)          -- back over the picture
fake.reset_events()
hud_wait()
ok(hud_hidden(), "they hide once the pointer is off them")

-- 'always' does not care where the pointer is.
hud_engage({ hide = 4, mode = "always" })
hud_pointer(600, 680)
fake.reset_events()
hud_wait()
ok(hud_hidden(), "'always' hides them even under the pointer")

-- A zero delay is only meaningful as "gone when not hovered", so it forces
-- that mode however the mode was set.
hud_engage({ hide = 0, mode = "always" })
hud_pointer(600, 680)
fake.reset_events()
hud_wait()
ok(not hud_hidden(),
   "a zero delay still holds while the pointer is on the controls")

-- Paused playback stopped being a rule and became a mode.
fake.log.props["pause"] = true
hud_engage({ hide = 4, mode = "paused" })
hud_pointer(600, 300)
fake.reset_events()
hud_wait()
ok(not hud_hidden(), "'paused' keeps them up on a paused film")

hud_engage({ hide = 4, mode = "hover" })
hud_pointer(600, 300)
fake.reset_events()
hud_wait()
ok(hud_hidden(), "the default hides them on a paused film")
fake.log.props["pause"] = nil

-- "The pointer is on the controls" has to mean the pointer was PUT there.
-- A mouse that has sat untouched wherever it was left is not reaching for
-- anything, and treating it as a hover left a keyboard-summoned HUD up for
-- ever. (It is also how a bare X server reads: the pointer parks at 0,0 and
-- the top bar is drawn under it.)
--- The two bars, as hud.py pushes them once the HUD is up. A hide clears
--- the renderer's node table (ui_suspend), so this has to be re-sent after
--- every summon -- which is exactly what the browser does.
local function hud_bars()
    scene({ { id = "hud-topbar", t = "rect", x = 0, y = 0, w = 1280, h = 60 },
            { id = "hud-bar", t = "rect", x = 0, y = 640, w = 1280, h = 80 } })
end

-- A caller that sends no opts at all gets the toolkit's own default, not
-- the 0.5s floor: `or 0` folded "absent" into "zero", and zero is
-- meaningful here (it forces hover mode), so PHUD_HIDE.def was unreachable.
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes")
hud_pointer(600, 300)
hud_pointer(600, 310)
hud_bars()
fake.reset_events()
fake.advance(1.0)
fake.fire_timers()
ok(not hud_hidden(), "no opts hid the controls after under a second")
hud_wait()
ok(hud_hidden(), "...and then never hid them at all")

fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes", fake.token({ hide = 4, mode = "hover" }))
-- The first pointer event after engaging only records the position, so this
-- parks the mouse on the bar without summoning anything.
hud_pointer(600, 680)
fake.send("mpvtk-hud-summon", "nav")
hud_bars()
fake.reset_events()
hud_wait()
ok(hud_hidden(), "a key summon under an unmoved pointer still auto-hides")

-- ...and moving it there does hold them, which is the case that gate must
-- not have broken. (Two events again: the first after a hide only records
-- the position, the second is movement and summons.)
hud_pointer(600, 680)
hud_pointer(600, 690)
hud_bars()
fake.reset_events()
hud_wait()
ok(not hud_hidden(), "moving the pointer onto them holds them up again")

-- A pointer that leaves the window entirely takes its position with it.
-- mpv keeps reporting the last coordinates with hover=false, which used to
-- read as a mouse still resting on the bar.
fake.observe("mouse-pos", { x = 600, y = 690, hover = false })
fake.reset_events()
hud_wait()
ok(hud_hidden(), "a pointer that left the window stops holding them up")

-- ================================================== scrub preview bubble

-- The seek bar's trickplay/chapter/timestamp bubble is drawn HERE. It used
-- to be a scene node Python rebuilt from a throttled hover event, which is
-- why it trailed the pointer (#618) and why its width was a guess that only
-- came out right when a thumbnail was the widest thing in it (#612).

local function pv_paint()
    fake.advance(0.1)
    fake.fire_timers()
end

--- The bubble's rect, or nil when it is not up.
local function preview()
    fake.reset_events()
    fake.send("mpvtk-debug", fake.token({ cmd = "state" }))
    return (last_event("debug_state") or {}).preview
end

--- The summoned HUD's seek bar as hud.py lays it out: 10 minutes over a
--- 1080px node at x=100, so the track runs 108..1172 and its midpoint is
--- 5:00.
local function seek_scene()
    scene({ { id = "hud-bar", t = "rect", x = 0, y = 640, w = 1280, h = 80 },
            { id = "hud-seek", t = "slider", x = 100, y = 660, w = 1080,
              h = 26, min = 0, max = 600, value = 0, pv = true } })
end

hud_engage({ hide = 4, mode = "hover" })
seek_scene()
hud_pointer(640, 300)
pv_paint()
ok(preview() == nil, "no bubble with the pointer off the bar")

hud_pointer(640, 670)
pv_paint()
local pv = preview()
ok(pv ~= nil, "the bubble appears with the pointer on the bar")
ok(pv and math.abs(pv.secs - 300) < 1,
   "the bubble reads the position under the pointer",
   pv and ("secs=" .. tostring(pv.secs)))
ok(pv and pv.y + pv.h <= 660, "the bubble sits above the bar it describes")

-- #612: the box is centred on the position it describes, whatever is in it.
-- Python assumed a flat 136px and drew whatever the content came to, so a
-- short chapter title pulled the bubble left of the point it was labelling.
ok(pv and math.abs((pv.x + pv.w / 2) - 640) < 2,
   "the bubble is centred on the pointer",
   pv and ("centre=" .. tostring(pv.x + pv.w / 2)))

-- No trickplay data yet, so no frame: the bubble is text only.
eq(pv and pv.frame, nil, "no frame before any trickplay data arrives")

-- ...and no overlay was issued for it either.
local function preview_overlay()
    local found
    for _, c in ipairs(fake.log.commands) do
        if c[1] == "overlay-add" and c[5] == "/tiles.bin" then found = c end
    end
    return found
end

fake.log.commands = {}
hud_pointer(640, 671)
pv_paint()
ok(preview_overlay() == nil, "a bubble with no tiles issues no overlay")

-- BIF tiles: 10s apart, so 5:00 is frame 30 and its bytes start at
-- 30 * w * h * 4 -- the offset argument, which is what lets the renderer
-- read one frame out of a file of them without decoding anything.
fake.send("shim-trickplay-bif", "60", "10000", "32", "18", "/tiles.bin")
fake.log.commands = {}
hud_pointer(640, 672)
pv_paint()
local ov = preview_overlay()
ok(ov ~= nil, "the trickplay frame is composited into the bubble")
eq(ov and tonumber(ov[6]), 30 * 32 * 18 * 4,
   "the overlay reads the frame for the hovered position")
eq(preview().frame, 30, "the bubble reports the frame it drew")

-- Past the end of the tiles clamps rather than reading past EOF, which
-- mpv mmaps and would answer with SIGBUS.
fake.send("shim-trickplay-bif", "10", "10000", "32", "18", "/tiles.bin")
hud_pointer(1100, 672)
pv_paint()
eq(preview().frame, 9, "a position past the last tile clamps to it")

-- ...and so does a position the loaded WINDOW does not reach, which is the
-- same hazard from the other direction. The file holds frames
-- [first, first + count) of a video `total` frames long, so a position has
-- to be turned into a frame against the video and then rebased onto the
-- file. Indexing a 20-frame mapping with frame 50 is exactly the read past
-- EOF that mpv answers with SIGBUS.
local function trickplay_request()
    for _, c in ipairs(fake.log.commands) do
        if c[1] == "script-message" and c[2] == "shim-trickplay-need" then
            return tonumber(c[3])
        end
    end
end

local function trickplay_asks()
    local n = 0
    for _, c in ipairs(fake.log.commands) do
        if c[1] == "script-message" and c[2] == "shim-trickplay-need" then
            n = n + 1
        end
    end
    return n
end

-- 10s cadence, 100 frames of video (the seek bar's whole 10 minutes), file
-- holds frames 40..59 -- 6:40 to 9:50.
fake.send("shim-trickplay-bif", "20", "10000", "32", "18", "/tiles.bin",
          "40", "100")
fake.log.commands = {}
hud_pointer(640, 672)                   -- 5:00 -> frame 30, before the window
pv_paint()
ok(preview() ~= nil, "the bubble went away outside the window")
ok(preview_overlay() == nil,
   "a frame outside the window was composited anyway -- that offset is "
   .. "past the end of the mapping")
eq(trickplay_request(), 300, "no window was asked for at the gap")

-- The ask is once per FRAME INDEX, not once per drawn frame: it sits in the
-- draw path and runs for as long as the pointer stays outside the window.
--
-- The pointer has to actually move, and the repaints have to actually
-- happen. One frame is 10s, which is ~17.7px of this bar, so 645 and 650
-- are both still frame 30 -- two renders, same index, and the guard is the
-- only thing that can suppress the second ask. Two bare pv_paint()s in a
-- row render ZERO times (a one-shot timer self-disables and nothing
-- invalidated), so a test built on those passes with the guard deleted.
fake.log.commands = {}
hud_pointer(645, 672)
pv_paint()
hud_pointer(650, 672)
pv_paint()
eq(trickplay_asks(), 0, "the request repeats for every render of one frame")

-- ...but a different frame index is a different question, and must ask.
-- Without this the test above would also pass if nothing ever asked at all.
fake.log.commands = {}
hud_pointer(700, 672)                   -- ~5:33 -> frame 33, still outside
pv_paint()
eq(trickplay_asks(), 1, "moving to another missing frame asked nothing")

-- 7:30 is frame 45, which the file holds as its frame 5.
fake.log.commands = {}
hud_pointer(906, 672)
pv_paint()
local win_ov = preview_overlay()
ok(win_ov ~= nil, "a frame inside the window was not drawn")
eq(win_ov and tonumber(win_ov[6]), (45 - 40) * 32 * 18 * 4,
   "the overlay offset was not rebased onto the window")
eq(trickplay_request(), nil, "asked for a window it already had")

-- The chapter-image fallback indexes by chapter start instead of a cadence.
fake.send("shim-trickplay-chapters", "32", "18", "/tiles.bin", "0,120,480")
hud_pointer(640, 673)
pv_paint()
eq(preview().frame, 1, "chapter tiles index by start time (5:00 is in #2)")

-- The bytes are unlinked right after the clear, so the renderer must stop
-- pointing at them before that happens.
fake.send("shim-trickplay-clear")
fake.log.commands = {}
hud_pointer(640, 674)
pv_paint()
ok(preview() ~= nil, "the bubble survives the tiles going away")
ok(preview_overlay() == nil, "...but stops reading the cleared file")

-- The pointer leaving the bar takes it away.
hud_pointer(640, 300)
pv_paint()
ok(preview() == nil, "the bubble goes with the pointer")

-- Releasing a drag must not make the bubble jump. A drag returns out of
-- on_mouse_move before the hover tracking, so the position it falls back to
-- when the button comes up has to have been kept current on the way.
hud_pointer(400, 672)                   -- park the pointer left of centre
pv_paint()
local before = preview().secs
fake.mouse(900, 672)
fake.key("mbtn_left")                   -- press ON the bar: the drag starts
fake.mouse(1000, 672)                   -- ...and drags right
pv_paint()
local dragged = preview().secs
ok(dragged > before, "the bubble did not follow the drag",
   string.format("%s -> %s", tostring(before), tostring(dragged)))
fake.send("mpvtk-debug", fake.token({ cmd = "click", x = 1000, y = 672 }))
-- The release itself requests no render, so pv_rect still holds the rect
-- painted DURING the drag -- which is the right answer either way and made
-- this test pass on unfixed code. Push the scene Python pushes in response
-- to the commit, which is what actually repaints, then look.
seek_scene()
pv_paint()
ok(preview() ~= nil and math.abs(preview().secs - dragged) < 2,
   "releasing the drag snapped the bubble back to where it started",
   string.format("released at %s, was dragged to %s",
                 tostring(preview() and preview().secs), tostring(dragged)))

-- A chapter name comes from container metadata and can run to a sentence.
-- clamp() returns its LOW bound when hi < lo, so an over-wide bubble pinned
-- itself at x=8 and ran off the right edge -- and stopped being centred on
-- the position it labels, which is #612 all over again.
fake.log.props["chapter-list"] = {
    { title = string.rep("A very long chapter name ", 12), time = 0 },
}
fake.observe("chapter-list", fake.log.props["chapter-list"])
hud_pointer(640, 675)
pv_paint()
local wide = preview()
ok(wide ~= nil, "no bubble to measure")
ok(wide and wide.x >= 0 and wide.x + wide.w <= 1280,
   "the bubble runs off the window",
   wide and string.format("x=%d w=%d right=%d", wide.x, wide.w,
                          wide.x + wide.w))
ok(wide and math.abs((wide.x + wide.w / 2) - 640) < 2,
   "a long chapter name knocked the bubble off centre",
   wide and ("centre=" .. tostring(wide.x + wide.w / 2)))
fake.observe("chapter-list", {})

-- A bar that does not ask for a preview never gets one: this is opt-in
-- (the volume slider is the same widget).
scene({ { id = "hud-bar", t = "rect", x = 0, y = 640, w = 1280, h = 80 },
        { id = "hud-vol", t = "slider", x = 100, y = 660, w = 1080,
          h = 26, min = 0, max = 100, value = 50 } })
hud_pointer(642, 670)
pv_paint()
ok(preview() == nil, "a slider without pv draws no bubble")

-- =========================================== keyboard scrolling (PGUP/etc)

-- The arrows are focus navigation and stay that way; these four are the
-- keys nothing was reaching. The target is the focused node's own scroller,
-- or the tallest one in the scene when nothing is focused.
local function keypress(name)
    local fn = fake.log.keybinds["mpvtk_nav_" .. name]
    ok(fn ~= nil, "no binding for " .. name)
    if fn then fn() end
end

fake.send("mpvtk-active", "no")
fake.send("mpvtk-active", "yes")     -- a clean browse state
scene({ vscroll("body", 400, 4000) })
eq(offset("body"), 0, "a fresh container should start at the top")

keypress("PGDWN")
eq(offset("body"), 400, "PGDWN did not page down by a viewport")
keypress("PGDWN")
eq(offset("body"), 800, "a second PGDWN did not page again")
keypress("PGUP")
eq(offset("body"), 400, "PGUP did not page back")

keypress("END")
eq(offset("body"), 3600, "END did not go to the bottom (ch - h)")
keypress("PGDWN")
eq(offset("body"), 3600, "PGDWN past the end should clamp, not overrun")
keypress("HOME")
eq(offset("body"), 0, "HOME did not go back to the top")

-- A scroller with nothing to scroll is not a target, and neither is a
-- horizontal one: PGUP/PGDWN are a vertical gesture.
scene({ vscroll("short", 400, 100),
        { id = "row", t = "scroll", axis = "x", x = 0, y = 0,
          w = 400, h = 100, cw = 4000, ch = 100 } })
keypress("PGDWN")
eq(offset("short"), 0, "paged a container with nothing to scroll")
eq(offset("row"), 0, "PGDWN scrolled a horizontal carousel")

-- An open popup floats over the page; scrolling what is behind it is never
-- what was meant. Same rule on_wheel applies to the wheel.
scene({ vscroll("body", 400, 4000),
        { id = "dd", t = "dropdown", x = 10, y = 10, w = 200, h = 30,
          items = { "a", "b", "c", "d", "e", "f", "g", "h" }, sel = 0 } })
keypress("PGDWN")
eq(offset("body"), 400, "sanity: the page pages with no popup open")
click("dd")                          -- open the dropdown
keypress("PGDWN")
eq(offset("body"), 400, "PGDWN scrolled the page behind an open popup")
scene({})                            -- drop the popup with the scene

-- ===================================== mpv's console owns the keyboard

-- Our ENTER/arrow bindings are FORCED, so they outrank the console's own
-- input: typing a command and pressing ENTER summoned the HUD and toggled
-- pause instead of running it.
-- force a real transition into browse: the tests above leave the renderer
-- in HUD mode, where the arrows are mpv's seek keys
fake.send("mpvtk-active", "no")
fake.send("mpvtk-active", "yes")
ok(fake.log.keybinds["mpvtk_nav_ENTER"] ~= nil,
   "browse should own ENTER before the console")

fake.observe("user-data/mpv/console/open", true)
ok(fake.log.keybinds["mpvtk_nav_ENTER"] == nil,
   "the console is up and ENTER is still ours")
ok(fake.log.keybinds["mpvtk_nav_UP"] == nil,
   "the console is up and the arrows are still ours")

fake.observe("user-data/mpv/console/open", false)
ok(fake.log.keybinds["mpvtk_nav_ENTER"] ~= nil,
   "ENTER was not taken back when the console closed")
ok(fake.log.keybinds["mpvtk_nav_UP"] ~= nil,
   "the arrows were not taken back when the console closed")

-- Restore puts back what was BOUND, not what some second copy of
-- ui_resume's rules thinks should be. During plain playback the arrows are
-- mpv's seek keys and nav is suspended, so closing the console there must
-- not hand them to us.
fake.send("mpvtk-active", "no")
ok(fake.log.keybinds["mpvtk_nav_ENTER"] == nil, "playback should not own ENTER")
fake.observe("user-data/mpv/console/open", true)
fake.observe("user-data/mpv/console/open", false)
ok(fake.log.keybinds["mpvtk_nav_ENTER"] == nil,
   "closing the console bound nav keys that were not bound before it opened")
fake.send("mpvtk-active", "yes")

-- ...but "what was bound" is a snapshot, and the LIFECYCLE moves underneath
-- it: the pointer can summon the HUD while the console is up. Restoring the
-- idle HUD's summon surface over a HUD that is now SHOWN takes mbtn_left back
-- for click-to-pause, so the bar's own buttons stop responding -- a mouse
-- that has gone dead with the controls in plain sight, and nothing on screen
-- to say why.
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes", fake.token({ hide = 4, mode = "hover" }))
ok(fake.log.keybinds["mpvtk_phud_click"] ~= nil,
   "sanity: an idle HUD takes the click for click-to-pause")
fake.observe("user-data/mpv/console/open", true)
ok(fake.log.keybinds["mpvtk_phud_click"] == nil,
   "the console is up and the summon surface is still ours")
fake.observe("mouse-pos", { x = 600, y = 300, hover = true })
fake.observe("mouse-pos", { x = 600, y = 360, hover = true })
fake.reset_events()
fake.send("mpvtk-debug", fake.token({ cmd = "state" }))
ok((last_event("debug_state") or {}).phud_shown == true,
   "sanity: pointer movement summons the HUD even with the console up")
fake.observe("user-data/mpv/console/open", false)
ok(fake.log.keybinds["mpvtk_phud_click"] == nil,
   "closing the console gave the shown HUD's clicks back to click-to-pause")

-- The same restore also replaces the wake key's upgrade-to-keyboard binding
-- with a cold summon, which is already a no-op on a HUD that is up: the bar
-- can then never be driven from the keyboard at all.
fake.key("mpvtk_wake")
fake.reset_events()
fake.send("mpvtk-debug", fake.token({ cmd = "state" }))
ok((last_event("debug_state") or {}).phud_kbd == true,
   "the wake key no longer upgrades a mouse-summoned HUD to keyboard driving")

fake.send("mpvtk-hud", "no")
fake.send("mpvtk-active", "yes")

-- **The state nothing described.** A HUD summoned by the POINTER keeps the
-- wake key bound to the upgrade-to-keyboard handler, while kb_summon was
-- cleared on the way in -- so all three tracked flags read false with ENTER
-- still ours, and the console was typed into a key that raised the HUD.
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes", fake.token({ hide = 4, mode = "hover" }))
fake.observe("mouse-pos", { x = 600, y = 300, hover = true })
fake.observe("mouse-pos", { x = 600, y = 360, hover = true })
fake.reset_events()
fake.send("mpvtk-debug", fake.token({ cmd = "state" }))
ok((last_event("debug_state") or {}).phud_shown == true,
   "fixture: the pointer never summoned the HUD")
ok(fake.log.keybinds["mpvtk_wake"] ~= nil,
   "fixture: a mouse-summoned HUD should keep the wake key")
fake.observe("user-data/mpv/console/open", true)
ok(fake.log.keybinds["mpvtk_wake"] == nil,
   "the console is up and ENTER still raises the HUD")
fake.observe("user-data/mpv/console/open", false)
ok(fake.log.keybinds["mpvtk_wake"] ~= nil,
   "closing the console left the HUD with no way to take the keyboard")
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-active", "yes")

-- **A lifecycle transition during the loan records an intent; it does not
-- take the keys back.** Playback starting while the console is open used to
-- re-bind the idle HUD's summon surface straight over it, so ENTER raised the
-- controls instead of running the command being typed.
fake.observe("user-data/mpv/console/open", true)
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes", fake.token({ hide = 4, mode = "hover" }))
ok(fake.log.keybinds["mpvtk_wake"] == nil,
   "a HUD engaged during the loan took the console's ENTER")
ok(fake.log.keybinds["mpvtk_phud_click"] == nil,
   "a HUD engaged during the loan took the console's mouse button")
fake.observe("user-data/mpv/console/open", false)
ok(fake.log.keybinds["mpvtk_wake"] ~= nil,
   "the summon surface was never handed back after the console closed")
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-active", "yes")

-- **The HUD must still let go while the console is up**, which is what makes
-- the ESC and F12 it holds a papercut rather than a trap: mpv's console keeps
-- `ctrl+[` (and a click) to close on, and both of ours come back the moment
-- the bar hides. The auto-hide is a renderer timer and the loan does not touch
-- it, so a pointer that stops moving -- and is not resting on the controls --
-- takes the bar down and hands ESC back.
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes", fake.token({ hide = 4, mode = "hover" }))
fake.observe("user-data/mpv/console/open", true)
fake.observe("mouse-pos", { x = 600, y = 300, hover = true })
fake.observe("mouse-pos", { x = 600, y = 360, hover = true })   -- summons
-- the bars hud.py draws, with the pointer in the gap between them
scene({ { id = "hud-topbar", t = "rect", x = 0, y = 0, w = 1280, h = 60 },
        { id = "hud-bar", t = "rect", x = 0, y = 640, w = 1280, h = 80 } })
ok(fake.log.keybinds["mpvtk_phud_esc"] ~= nil,
   "fixture: a shown HUD should hold ESC")
ok(fake.log.keybinds["mpvtk_hud"] ~= nil,
   "fixture: a shown HUD should hold F12")
fake.advance(5)
fake.fire_timers()
fake.reset_events()
fake.send("mpvtk-debug", fake.token({ cmd = "state" }))
ok((last_event("debug_state") or {}).phud_shown ~= true,
   "the HUD never auto-hid while the console was up")
ok(fake.log.keybinds["mpvtk_phud_esc"] == nil,
   "the HUD hid but kept ESC, so the console cannot close on it")
ok(fake.log.keybinds["mpvtk_hud"] == nil,
   "the HUD hid but kept F12")
ok(fake.log.keybinds["mpvtk_wake"] == nil,
   "hiding under the console took ENTER back for the summon surface")
fake.observe("user-data/mpv/console/open", false)
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-active", "yes")

-- The NAV half of the same rule, which the cases above cannot reach: the
-- console can be open across a transition in either direction.
--
-- Browse -> suspended, decided while the console holds the keys. Restoring
-- "nav was bound" here hands the arrows, ENTER and TAB to an invisible
-- renderer for the whole of playback -- and `phud.mode` does not stand in for
-- "the UI owns the keyboard", because it is only ever true under osc_style
-- mpvtk; with a lua OSC a suspended player has no HUD mode at all.
fake.observe("user-data/mpv/console/open", true)
fake.send("mpvtk-active", "no")
fake.observe("user-data/mpv/console/open", false)
ok(fake.log.keybinds["mpvtk_nav_ENTER"] == nil,
   "closing the console bound nav keys over suspended playback")
fake.send("mpvtk-active", "yes")

-- ...and the other way: suspended -> browse, which the `mpvtk-active`
-- handler records into the snapshot rather than binding behind the console.
fake.send("mpvtk-active", "no")
fake.observe("user-data/mpv/console/open", true)
fake.send("mpvtk-active", "yes")
fake.observe("user-data/mpv/console/open", false)
ok(fake.log.keybinds["mpvtk_nav_ENTER"] ~= nil,
   "browse came back from the console with no arrow, ENTER or TAB")

-- A keyboard-driven HUD owns them too, and it is not browse: `state.active`
-- alone would refuse them.
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes", fake.token({ grab = true, hide = 4,
                                           mode = "hover" }))
fake.observe("mouse-pos", { x = 600, y = 300, hover = true })
fake.observe("mouse-pos", { x = 600, y = 360, hover = true })
fake.observe("user-data/mpv/console/open", true)
fake.observe("user-data/mpv/console/open", false)
ok(fake.log.keybinds["mpvtk_nav_ENTER"] ~= nil,
   "a keyboard-driven HUD lost its arrows to the console")
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-active", "yes")

-- ==================================================== what gets drawn
--
-- Two things in this file live entirely in the drawing and are invisible to
-- every other assertion here: which colour says "this is the one you have
-- chosen", and whether a constant baked into a draw call follows the UI
-- scale. Both were reported as bugs in #620.

--- Push `nodes`, run `setup` against them, and return the rectangles the
--- next repaint drew. `setup` runs after the scene so it can click things
--- that only exist once the scene is up.
local function painted(nodes, setup)
    scene(nodes)
    if setup then setup() end
    fake.reset_draw()
    fake.advance(1.0)
    fake.fire_timers()
    return fake.shapes()
end

--- Rows drawn in `colour`, as a list of {opaque=bool} in draw order.
local function fills(shapes, colour)
    local out = {}
    for _, s in ipairs(shapes) do
        if s.fill == colour then
            out[#out + 1] = { opaque = (s.alpha or 255) >= 255, y = s.y }
        end
    end
    return out
end

local function count_solid(shapes, colour)
    local n = 0
    for _, s in ipairs(fills(shapes, colour)) do
        if s.opaque then n = n + 1 end
    end
    return n
end

local function any_fill(shapes, colour)
    return #fills(shapes, colour) > 0
end

-- The tokens are the stock ones (nothing has pushed mpvtk-theme), so the
-- accent is 7aa2f7 and control_bg -- the old selection fill -- is 333333.
local ACCENT, CONTROL_BG = "7aa2f7", "333333"

local DD = { id = "dd", t = "dropdown", x = 40, y = 40, w = 200, h = 30,
             size = 18, items = { "Auto", "1080p", "720p" }, sel = 1 }

local dd_shapes = painted({ DD }, function() click("dd") end)
ok(count_solid(dd_shapes, ACCENT) == 1,
   "the selected popup row is drawn in the accent",
   "selection used to take control_bg, one step off popup_bg -- #620")
ok(not any_fill(dd_shapes, CONTROL_BG),
   "the popup no longer draws the near-invisible old selection fill")

click("dd")     -- close it again, so later scenes are unaffected

-- The weak state is the ACCENT AT LOW ALPHA, not a grey token. control_hover
-- over popup_bg is BUTTON_ACTIVE over PANEL_BG, which in four of the six
-- shipped themes is a weaker contrast than the fill #620 was filed about --
-- and in jf-wmc is 1.00:1, the same relative luminance, ie. not drawn.
-- Derived from the accent it cannot collapse into the background unless the
-- accent already has.
local dd_hover = painted({ DD }, function()
    click("dd")
    fake.mouse(140, 78)      -- into the popup, on a row that is NOT selected
end)
local washes = fills(dd_hover, ACCENT)
ok(#washes >= 2, "a hovered popup row is highlighted at all",
   string.format("%d accent rows drawn", #washes))
ok(count_solid(dd_hover, ACCENT) == 1,
   "exactly one popup row is solid -- the selected one",
   "the hover must not be mistakable for the selection")
click("dd")

-- A context menu is not a ranked list: it is a list of actions, the gear
-- menu marks its current option with a check icon rather than a highlighted
-- row, and with no keyboard cursor up the pointer is the only cursor there
-- is. So hover keeps the solid accent it has always had -- demoting it along
-- with the dropdown's would have taken the feedback off every menu in the
-- app to fix a problem menus do not have.
local MENU = { id = "mnu", t = "menu", x = 40, y = 40, w = 200, rh = 30,
               size = 18, items = { "Screenshot", "Playback Data" } }
-- Parked by coordinate rather than by id: a menu carries `rh`, not `h`, so
-- the debug hover command has no box to find the centre of.
local menu_shapes = painted({ MENU }, function() fake.mouse(140, 55) end)
ok(count_solid(menu_shapes, ACCENT) == 1,
   "a hovered menu row keeps the solid accent")

-- ...but once a keyboard cursor is up, the two are the same kind of thing
-- and only ONE may be strong. Both took the accent, so a right-click
-- followed by Down painted two rows identically -- and ENTER and a click
-- would then act on different ones.
local menu_nav = painted({ MENU }, function()
    fake.mouse(140, 55)                                  -- pointer on row 0
    fake.send("mpvtk-debug", fake.token({ cmd = "nav", dir = "down" }))
end)
ok(count_solid(menu_nav, ACCENT) == 1,
   "the keyboard cursor and the pointer drew two identical rows",
   string.format("%d solid accent rows", count_solid(menu_nav, ACCENT)))
ok(#fills(menu_nav, ACCENT) >= 2,
   "the pointer stopped being drawn at all once the cursor moved")

-- Scale. A seek bar's track, chapter slits and thumb are constants of the
-- draw rather than fields of the node, so scale_scene never reaches them --
-- they were left at 1x inside a HUD that had otherwise doubled.
local SEEK = { id = "seek", t = "slider", x = 0, y = 100, w = 400, h = 26,
               min = 0, max = 100, value = 50, ov = true,
               marks = { 0.25, 0.75 } }

--- The chapter slit: the narrowest rectangle the slider drew.
local function slit_w(shapes)
    local narrowest
    for _, s in ipairs(shapes) do
        if s.h > 0 and s.w > 0 and (not narrowest or s.w < narrowest) then
            narrowest = s.w
        end
    end
    return narrowest
end

fake.send("mpvtk-scale", fake.token({ s = 1 }))
local at1x = slit_w(painted({ SEEK }))
fake.send("mpvtk-scale", fake.token({ s = 2 }))
local at2x = slit_w(painted({ SEEK }))
ok(at1x and at2x and at2x > at1x * 1.5,
   "chapter marks follow the UI scale",
   string.format("1x drew %s wide, 2x drew %s", tostring(at1x),
                 tostring(at2x)))
fake.send("mpvtk-scale", fake.token({ s = 1 }))

-- ============================================= the themed glow's clip
--
-- The glow is the one decoration that is meant to draw OUTSIDE the box it
-- decorates: a card fills its row's viewport, so a halo clipped to that
-- viewport is chopped back into the hard rectangle the glow exists to
-- replace. It used to buy that by passing no clip at all -- and chrome is
-- drawn BEFORE content (app.build stacks header, then the page), so a card
-- near the top of the page had its halo painted over the header. ASS has no
-- z-order beyond scene order, so nothing downstream could put it back.
--
-- The rule now: out of your own viewport, never out of the page.

local HDR_H = 60

--- A header, a page below it, a carousel inside that, and a tile in the
--- carousel -- the arrangement every library screen has.
local function glow_scene(tile)
    return {
        { id = "hdr", t = "rect", x = 0, y = 0, w = 1280, h = HDR_H,
          fill = "202020" },
        { id = "page", t = "scroll", axis = "y", x = 0, y = HDR_H,
          w = 1280, h = 720 - HDR_H, cw = 1280, ch = 2000 },
        { id = "row", t = "scroll", axis = "x", sc = "page",
          x = 0, y = 70, w = 1280, h = 220, cw = 4000, ch = 220 },
        tile,
    }
end

--- The blurred rectangle, ie. the glow. Nothing else in a scene is blurred.
local function blurred(shapes)
    for _, s in ipairs(shapes) do
        if s.blur then return s end
    end
    return nil
end

fake.send("mpvtk-theme", fake.token({ glow = true }))

local TILE = { id = "tile", t = "rect", sc = "row", x = 10, y = 70,
               w = 140, h = 210, ring = true,
               hover = { bc = "a855f7", bw = 3 } }

local ring = blurred(painted(glow_scene(TILE), function()
    fake.mouse(80, 150)      -- onto the tile
end))
ok(ring ~= nil, "a hovered tile draws a blurred halo under a glow theme")
ok(ring and ring.clip, "the halo is clipped at all",
   "an unclipped halo is what painted over the header")
ok(ring and ring.clip and ring.clip.y1 >= HDR_H,
   "the halo stops at the header",
   ring and ring.clip and ("clip starts at y=" .. tostring(ring.clip.y1)))
ok(ring and ring.clip and ring.clip.y1 < 70,
   "...but still escapes its own row, or it is the box it replaces",
   ring and ring.clip and ("clip starts at y=" .. tostring(ring.clip.y1)))

-- A box glow (themed chrome: hover={glow=true}) sitting DIRECTLY in the page
-- has no inner viewport to escape, so the page is the whole answer.
local BTN = { id = "btn", t = "rect", sc = "page", x = 40, y = 70,
              w = 200, h = 40, fill = "2a1656", radius = 6,
              hover = { fill = "3d2170", glow = true } }
local box = blurred(painted({
    { id = "hdr", t = "rect", x = 0, y = 0, w = 1280, h = HDR_H,
      fill = "202020" },
    { id = "page", t = "scroll", axis = "y", x = 0, y = HDR_H,
      w = 1280, h = 720 - HDR_H, cw = 1280, ch = 2000 },
    BTN,
}, function() fake.mouse(100, 90) end))
ok(box ~= nil, "a hovered themed box draws its halo")
ok(box and box.clip and box.clip.y1 >= HDR_H,
   "the box halo stops at the header too",
   box and box.clip and ("clip starts at y=" .. tostring(box.clip.y1)))

fake.send("mpvtk-theme", fake.token({ glow = false }))

-- =============================== the left button, and who gets to have it
--
-- #669. Clicking the hidden HUD pauses, which is the lua OSC's
-- click-anywhere and this client's long-standing behaviour. A *forced*
-- binding on mbtn_left is also exactly what stops the VO dragging the
-- window with that button, so giving mpv's modality back means not taking
-- the button at all rather than taking it and behaving differently.
--
-- Measured against a real mpv under Xvfb, both ways: a double click
-- delivers mbtn_left, mbtn_left_dbl, mbtn_left. So the two pause toggles
-- cancel and mpv's own MBTN_LEFT_DBL still fullscreens *with* our binding
-- installed -- which is why double-click needs nothing from us in either
-- mode, and why only the single-click binding is conditional.

fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes", fake.token({ hide = 4, mode = "hover" }))
ok(fake.log.keybinds["mpvtk_phud_click"] ~= nil,
   "click-to-pause is the default: mbtn_left is taken")

fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes",
          fake.token({ hide = 4, mode = "hover", click = false }))
ok(fake.log.keybinds["mpvtk_phud_click"] == nil,
   "mpv modality: mbtn_left is left alone so the VO can drag")

-- The wake key still has to work in that mode, or the HUD becomes
-- unreachable from the keyboard as well as from the left button.
ok(fake.log.keybinds["mpvtk_wake"] ~= nil,
   "the wake key survives mpv modality")

fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes",
          fake.token({ hide = 4, mode = "hover", click = true }))
ok(fake.log.keybinds["mpvtk_phud_click"] ~= nil,
   "and it comes back when the setting is turned on again")

-- An older Python side sends no `click` at all. Absent must mean the
-- historical behaviour, not false -- otherwise a version skew silently
-- removes click-to-pause for everyone.
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes", fake.token({ hide = 4 }))
ok(fake.log.keybinds["mpvtk_phud_click"] ~= nil,
   "an absent click option keeps click-to-pause")

fake.send("mpvtk-hud", "no")

-- ----------------- ...and the same answer while the controls are ON SCREEN
--
-- The bug this pins: the setting was honoured only while the HUD was
-- hidden. Once summoned, `mpvtk_mouse` owns mbtn_left / mbtn_left_dbl /
-- mbtn_right, so every one of them reaches the scene handlers instead of
-- the phud binding or mpv's defaults -- and those handlers paused on any
-- bare-video click, swallowed the double click, and swallowed the right
-- click. Reported from hand-testing; nothing here had ever clicked the
-- *picture* with the controls up.

local function hud_up(opts)
    fake.send("mpvtk-hud", "no")
    fake.send("mpvtk-hud", "yes", fake.token(opts))
    fake.observe("mouse-pos", { x = 640, y = 300, hover = true })
    fake.observe("mouse-pos", { x = 640, y = 360, hover = true })
    -- Bars at the edges; the middle of the window is bare video.
    scene({ { id = "hud-topbar", t = "rect", x = 0, y = 0, w = 1280, h = 60 },
            { id = "hud-bar", t = "rect", x = 0, y = 640, w = 1280, h = 80 } })
    repaint()
    fake.mouse(640, 360)
    fake.log.commands = {}
end

local function did(cmd, arg)
    for _, c in ipairs(fake.log.commands) do
        if c[1] == cmd and (arg == nil or c[2] == arg) then return true end
    end
    return false
end

hud_up({ hide = 4, mode = "hover", click = true })
fake.key("mbtn_left")
ok(did("cycle", "pause"),
   "click-to-pause on: a click on bare video still pauses with the HUD up")

hud_up({ hide = 4, mode = "hover", click = false })
fake.key("mbtn_left")
ok(not did("cycle", "pause"),
   "mpv modality: a click on bare video must not pause with the HUD up")
ok(did("begin-vo-dragging"),
   "mpv modality: a click on bare video starts a window drag instead")

-- Double click is full screen in BOTH modes -- it is mpv's own default and
-- the only reason it stopped working is that our section took the key.
for _, mode in ipairs({ true, false }) do
    hud_up({ hide = 4, mode = "hover", click = mode })
    fake.key("mbtn_left_dbl")
    ok(did("cycle", "fullscreen"),
       "double click on bare video did not full-screen with the HUD up",
       "click_pauses=" .. tostring(mode))
end

hud_up({ hide = 4, mode = "hover", click = false })
fake.key("mbtn_right")
ok(did("cycle", "pause"),
   "mpv modality: right click on bare video pauses with the HUD up")

hud_up({ hide = 4, mode = "hover", click = true })
fake.key("mbtn_right")
ok(not did("cycle", "pause"),
   "click-to-pause on: right click is not a second way to pause")

-- ------------- ...but "no node" is not "bare video" while something floats
--
-- node_at() answers with clickable SCENE nodes, and a modal's body, a
-- dropdown's popup rows and a context menu are none of those -- so it is
-- nil ON them. on_mouse_down checks all four before its bare-video branch;
-- on_dbl and on_rclick did not, so a double click inside the Playback Info
-- panel full-screened the window under it and a right click paused. Found
-- by the adversarial review, missed by everything above: these tests had
-- never had anything open over the picture.

local MODAL = { id = "dlg", t = "layer", kind = "modal",
                x = 340, y = 200, w = 600, h = 320 }

-- **The real sequence, not one event.** mpv delivers a double click as
-- mbtn_left, mbtn_left_dbl, mbtn_left -- measured, and written down 140
-- lines above this. Firing mbtn_left_dbl alone is what let the first
-- version of these tests pass while three of the four guards were dead:
-- the leading mbtn_left dismisses the dropdown, the context menu and the
-- textbox menu, so on_dbl asking "is one open?" always answered no.
local function dbl()
    fake.key("mbtn_left")
    fake.key("mbtn_left_dbl")
    fake.key("mbtn_left")
end

for _, mode in ipairs({ true, false }) do
    hud_up({ hide = 4, mode = "hover", click = mode })
    scene({ { id = "hud-bar", t = "rect", x = 0, y = 640, w = 1280, h = 80 },
            MODAL })
    repaint()
    fake.log.commands = {}
    dbl()
    ok(not did("cycle", "fullscreen"),
       "double click inside an open modal must not full-screen the window",
       "click_pauses=" .. tostring(mode))
end

hud_up({ hide = 4, mode = "hover", click = false })
scene({ { id = "hud-bar", t = "rect", x = 0, y = 640, w = 1280, h = 80 },
        MODAL })
repaint()
fake.log.commands = {}
fake.key("mbtn_right")
ok(not did("cycle", "pause"),
   "right click inside an open modal must not pause under it")

-- The dropdown popup has the same shape: its rows are popup geometry, not
-- scene nodes, so a double click on the audio-track picker landed here too.
local function dd_open()
    hud_up({ hide = 4, mode = "hover", click = false })
    scene({ { id = "hud-bar", t = "rect", x = 0, y = 640, w = 1280, h = 80 },
            { id = "dd", t = "dropdown", x = 500, y = 300, w = 200, h = 30,
              size = 18, items = { "One", "Two", "Three" }, sel = 0 } })
    repaint()
    click("dd")                      -- open it
    fake.mouse(640, 360)             -- pointer over a popup row
    fake.log.commands = {}
end

dd_open()
dbl()
ok(not did("cycle", "fullscreen"),
   "double click on an open dropdown's row must not full-screen")

-- Re-opened, because dbl() ends in an mbtn_left that closes the popup --
-- the reason the first version of this pair passed for the wrong reason.
dd_open()
fake.key("mbtn_right")
ok(not did("cycle", "pause"),
   "right click on an open dropdown must not pause under it")

-- ------------------------ ...and in a SyncPlay group the pause is Python's
--
-- The renderer pauses locally by default, because `cycle pause` with no
-- round trip is what makes click-to-pause feel immediate. In a group that
-- is not a pause at all: mpv stops, the group never hears, and the next
-- tick drags this player back. [iw] found it by clicking the video in a
-- group -- the keys had been fixed, the click had not.

hud_up({ hide = 4, mode = "hover", click = true, syncplay = true })
fake.reset_events()
fake.key("mbtn_left")
ok(not did("cycle", "pause"),
   "in a group, a click on bare video must not pause mpv locally")
ok(last_event("pause") ~= nil,
   "...it hands the pause to Python, which knows about the group")

-- ...and out of a group nothing changed: still local, still no round trip.
hud_up({ hide = 4, mode = "hover", click = true })
fake.reset_events()
fake.key("mbtn_left")
ok(did("cycle", "pause"),
   "outside a group the click still pauses locally")
ok(last_event("pause") == nil,
   "...and does not pay for a round trip to Python")

-- The right-click path in mpv modality is the same decision.
hud_up({ hide = 4, mode = "hover", click = false, syncplay = true })
fake.reset_events()
fake.key("mbtn_right")
ok(not did("cycle", "pause"),
   "in a group, right-click in mpv modality must not pause locally")
ok(last_event("pause") ~= nil,
   "...it hands that one over too")

-- The context menu had no test at all, and is the case the dead guard was
-- most obviously written for.
hud_up({ hide = 4, mode = "hover", click = false })
scene({ { id = "hud-bar", t = "rect", x = 0, y = 640, w = 1280, h = 80 },
        { id = "cm", t = "menu", x = 500, y = 260, w = 220, rh = 30,
          size = 18, items = { "Play", "Queue", "Mark Watched" } } })
repaint()
fake.mouse(560, 300)
fake.log.commands = {}
dbl()
ok(not did("cycle", "fullscreen"),
   "double click on an open context menu must not full-screen")

-- ...and the guard must not have cost the thing it guards: with nothing
-- floating, both still do what mpv would.
hud_up({ hide = 4, mode = "hover", click = false })
dbl()
ok(did("cycle", "fullscreen"),
   "with nothing open, double click on bare video still full-screens")
fake.key("mbtn_right")
ok(did("cycle", "pause"),
   "with nothing open, right click on bare video still pauses")

-- A click on a CONTROL is still a click on a control, in either mode --
-- the fall-through must not have eaten the HUD's own buttons. The bar's
-- own background is deliberately NOT a control: pressing the gaps between
-- buttons drags the window, which is what pressing chrome does everywhere
-- and mirrors it pausing there under the other setting.
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes", fake.token({ hide = 4, mode = "hover",
                                           click = false }))
fake.observe("mouse-pos", { x = 640, y = 300, hover = true })
fake.observe("mouse-pos", { x = 640, y = 360, hover = true })
scene({ { id = "hud-bar", t = "rect", x = 0, y = 640, w = 1280, h = 80 },
        { id = "hud-pp", t = "rect", x = 600, y = 660, w = 40, h = 40,
          click = true } })
repaint()
fake.mouse(620, 680)                    -- squarely on the play button
fake.log.commands = {}
fake.key("mbtn_left")
ok(not did("begin-vo-dragging"),
   "a press on a HUD control started a window drag instead of clicking")
ok(not did("cycle", "pause"),
   "a press on a HUD control fell through to the bare-video handler")

-- ...and NOT while the library owns the window. `mpvtk_mouse` is enabled
-- in browse mode too, so an unguarded fall-through would make a
-- double-click on empty library background toggle full screen -- which is
-- what the phud test on the fullscreen branch is actually protecting.
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-active", "yes")
scene({ { id = "tile", t = "rect", x = 10, y = 10, w = 100, h = 100,
          click = true } })
repaint()
fake.mouse(640, 400)                    -- empty page background
fake.log.commands = {}
fake.key("mbtn_left_dbl")
ok(not did("cycle", "fullscreen"),
   "double-clicking empty library background toggled full screen")
fake.send("mpvtk-active", "no")

-- ================================================ HUD nav + seek bar

-- Leave HUD mode with a known syncplay answer: `pause_now` hands the
-- pause to Python inside a group and does it locally otherwise, and the
-- flag survives until the next engage.
fake.send("mpvtk-hud", "yes",
          fake.token({ hide = 4, mode = "hover", syncplay = false }))
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-active", "yes")

-- Coming DOWN off the full-width seek bar, spatial nav lands on whichever
-- control in the row is nearest the x it came from -- and which one that
-- is changes with the window width, because the chapter and seek buttons
-- appear and disappear with it. A row may name the control the arrow
-- actually meant.
-- The bar spans 100..1180, so the x an arrow leaves it with is 640.
-- `near` is centred on exactly that and `pp` is nowhere near it: without
-- the gravity, distance alone picks `near` -- which is the point, because
-- a version of this row where the play button happens to sit under the
-- middle proves nothing at all.
local function hudrow()
    scene({
        { id = "bar", t = "slider", x = 100, y = 600, w = 1080, h = 26,
          min = 0, max = 600, value = 0, aadj = true, ctx = true },
        { id = "far", t = "rect", x = 300, y = 660, w = 40, h = 40,
          click = true, ctx = true },
        { id = "near", t = "rect", x = 620, y = 660, w = 40, h = 40,
          click = true, ctx = true },
        { id = "pp", t = "rect", x = 900, y = 660, w = 40, h = 40,
          click = true, ctx = true, grav = true },
    })
end

--- Which node has spatial focus, read the way the rest of this file does:
--- MENU opens the focused node's context menu and the event names it.
local function focused()
    fake.reset_events()
    navkey("MENU")
    local e = last_event("context")
    return e and e.id or nil
end

hudrow()
fake.send("mpvtk-focus", fake.token({ id = "bar" }))
navkey("DOWN")
eq(focused(), "pp", "DOWN off the bar ignored the row's gravity")

-- ...and gravity is VERTICAL only. Two presses, because the honest test
-- is the one that steps TOWARD the gravity node: leaving it can be
-- satisfied by ordinary distance, but arriving has to stop at `near`
-- rather than being pulled past it to `pp`. (The leaving direction is
-- kept as well -- it is the one a user notices.)
navkey("LEFT")
eq(focused(), "near", "gravity swallowed a sideways step away from it")
navkey("LEFT")
eq(focused(), "far", "...and the step after that")
navkey("RIGHT")
eq(focused(), "near", "a sideways step was pulled past its neighbour to "
                      .. "the gravity node")

-- Select on an always-adjust bar with no scrub pending. Adjust mode is
-- not a gesture -- the bar is live the moment it is focused -- so a
-- commit here seeks to where playback already is. It means play/pause.
hudrow()
fake.send("mpvtk-focus", fake.token({ id = "bar" }))
fake.reset_events()
fake.log.commands = {}
navkey("ENTER")
eq(last_event("commit"), nil, "Select on an untouched seek bar seeked")
local cycled = false
for _, c in ipairs(fake.log.commands) do
    if c[1] == "cycle" and c[2] == "pause" then cycled = true end
end
ok(cycled, "Select on an untouched seek bar did not play/pause")

-- ...but once something IS pending, Select accepts it. That half is the
-- whole point of the bar and must not regress.
navkey("RIGHT")                        -- scrub: a gesture is now in flight
fake.reset_events()
navkey("ENTER")
local done = last_event("commit")
eq(done and done.id, "bar", "Select did not accept a pending seek")


-- ========================================================== game controller

-- The whole point of the mpvtk-gamepad message: these bindings are NOT nav
-- keys and must not share their lifecycle. NAV_KEYS is torn down by
-- ui_suspend the moment a video starts, because playback wants the arrows
-- back -- which is where the first version of this lived, and why every
-- button on the pad went dead as soon as anything played.
fake.send("mpvtk-active", "yes")
fake.send("mpvtk-gamepad", fake.token({
    { "GAMEPAD_DPAD_UP", "key", "UP", 0.15 },
    { "GAMEPAD_ACTION_DOWN", "key", "ENTER", 0 },
    { "GAMEPAD_ACTION_RIGHT", "key", "ESC", 0 },
    { "GAMEPAD_START", "nav", "menu", 0 },
    { "GAMEPAD_RIGHT_STICK_LEFT", "seek", "left", 0.4 },
}))
ok(fake.log.keybinds["mpvtk_gp_GAMEPAD_DPAD_UP"] ~= nil,
   "the pushed table did not bind the d-pad")

-- Remappable by the user's own input.conf, which is the whole answer to
-- "how do I change what a button does" -- and is true only because these
-- are NON-forced. A forced binding would have to be disabled first.
eq(fake.log.forced["mpvtk_gp_GAMEPAD_DPAD_UP"], false,
   "gamepad bindings were forced, so input.conf could not override them")

-- Browse: the button is a synthetic keypress, so whatever owns the keyboard
-- right now answers it and the pad needs to know about none of them.
fake.log.commands = {}
fake.advance(1)
fake.key("mpvtk_gp_GAMEPAD_DPAD_UP")
local pressed = nil
for _, c in ipairs(fake.log.commands) do
    if c[1] == "keypress" then pressed = c[2] end
end
eq(pressed, "UP", "the d-pad did not press the keyboard key it stands for")

-- Now play something. The nav keys go, as they must...
fake.send("mpvtk-active", "no")
fake.send("mpvtk-hud", "yes", fake.token({ hide = 4, mode = "hover" }))
eq(fake.log.keybinds["mpvtk_nav_UP"], nil,
   "playback left the browser's arrow binding in place")
-- ...and the controller stays, because it has a second stick and does not
-- need to give the first one back.
ok(fake.log.keybinds["mpvtk_gp_GAMEPAD_DPAD_UP"] ~= nil,
   "playback tore down the gamepad bindings with the nav keys")

-- With the bar hidden the left stick WAKES the HUD rather than being
-- delivered: with hud_grab_keys off only the wake key is bound, so a
-- keypress would fall through to mpv's own arrows and the left stick would
-- seek. Seeking is the right stick's job.
fake.log.commands = {}
fake.advance(1)
fake.key("mpvtk_gp_GAMEPAD_DPAD_UP")
local fellthrough = false
for _, c in ipairs(fake.log.commands) do
    if c[1] == "keypress" then fellthrough = true end
end
ok(not fellthrough,
   "the left stick fell through to mpv's arrows over a hidden HUD")
-- ...and it WOKE the bar. Asserting only the absence of a keypress cannot
-- tell "drives the UI" from "swallowed the press", and swallowing it is
-- the likelier bug -- so the negative alone guards the headline behaviour
-- of the whole asymmetric-stick design with a message that would lie.
eq(fake.log.props["user-data/mpvtk/active"], true,
   "the left stick swallowed the press instead of waking the HUD")

-- A pointer summon with hud_grab_keys off leaves the arrows to mpv on
-- purpose, so the bar is SHOWN while the renderer holds no nav keys --
-- `shown` is not the same question as "the UI has the keyboard". Asking
-- only `shown` sent the stick to mpv's arrows over a visible HUD.
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes", fake.token({ hide = 4, mode = "hover" }))
fake.observe("mouse-pos", { x = 600, y = 300, hover = true })
fake.observe("mouse-pos", { x = 600, y = 312, hover = true })   -- summons
ok(fake.log.keybinds["mpvtk_nav_UP"] == nil,
   "the mouse summon took the arrows, so this case cannot be tested")
fake.log.commands = {}
fake.advance(1)
fake.key("mpvtk_gp_GAMEPAD_DPAD_UP")
local seeped = false
for _, c in ipairs(fake.log.commands) do
    if c[1] == "keypress" then seeped = true end
end
ok(not seeped,
   "over a MOUSE-summoned HUD the left stick reached mpv's own arrows")
ok(fake.log.keybinds["mpvtk_nav_UP"] ~= nil,
   "the pad did not take the keyboard from the pointer")
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes", fake.token({ hide = 4, mode = "hover" }))

-- The right stick asks Python, because the distance is the user's own
-- input.conf number and the seek has to be one a SyncPlay group hears
-- about -- neither of which a `seek 5` from here would be.
fake.reset_events()
fake.advance(1)
fake.key("mpvtk_gp_GAMEPAD_RIGHT_STICK_LEFT")
local sk = last_event("gpseek")
eq(sk and sk.dir, "left", "the right stick did not ask Python to seek")

-- START is a 'nav', not a keypress: the MENU *key* is a browser nav
-- binding, so over a playing video it is bound to nothing at all and a
-- keypress would go nowhere.
fake.reset_events()
fake.key("mpvtk_gp_GAMEPAD_START")
local nv = last_event("gpnav")
eq(nv and nv.a, "menu", "Start did not reach the remote-control ladder")

-- Over a showing Skip Intro button, confirm SKIPS -- it does not summon
-- the bar. The keyboard's ENTER and the remote's Select both did this
-- already; the pad had its own copy of the summon and only that, so A did
-- the one thing the button on screen said it would not.
-- Re-engage first: the direction press above SUMMONED the bar, and the
-- skip button is only an offer while it is down.
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes", fake.token({ hide = 4, mode = "hover" }))
fake.send("mpvtk-hud-skip", "Skip Intro")
fake.advance(1)
fake.reset_events()
fake.key("mpvtk_gp_GAMEPAD_ACTION_DOWN")
ok(last_event("hudskip") ~= nil, "A over the Skip button summoned instead")

-- ...and a DIRECTION over the same button still brings the bar up, which
-- is the way back to the rest of the controls.
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-hud", "yes", fake.token({ hide = 4, mode = "hover" }))
fake.send("mpvtk-hud-skip", "Skip Intro")
fake.advance(1)
fake.reset_events()
fake.key("mpvtk_gp_GAMEPAD_DPAD_UP")
eq(last_event("hudskip"), nil, "a direction accepted the skip")
eq(fake.log.props["user-data/mpvtk/active"], true,
   "a direction over the Skip button did not bring the bar up")
fake.send("mpvtk-hud-skip", "")

-- Auto-repeat. mpv repeats a held key at --input-ar-rate -- 40 a second by
-- default -- which on a stick is forty library rows a second and is not
-- something anybody can aim. Held controls are thinned to their own rate.
--
-- Each block counts from a RESET, not cumulatively: "3" as a running total
-- of a limit that let one through reads exactly like a limit that let three
-- through, and the failure message would be lying either way.
local function seeks_sent()
    local n = 0
    for _, e in ipairs(fake.log.events) do
        if type(e) == "table" and e.t == "gpseek" then n = n + 1 end
    end
    return n
end

fake.advance(1)
fake.reset_events()
fake.key("mpvtk_gp_GAMEPAD_RIGHT_STICK_LEFT")           -- lands
fake.key("mpvtk_gp_GAMEPAD_RIGHT_STICK_LEFT", { event = "repeat" })
fake.key("mpvtk_gp_GAMEPAD_RIGHT_STICK_LEFT", { event = "repeat" })
eq(seeks_sent(), 1, "a held stick fired every repeat mpv sent")

-- ...and it is a THINNING, not a lockout: past the interval it fires again.
fake.advance(1)
fake.reset_events()
fake.key("mpvtk_gp_GAMEPAD_RIGHT_STICK_LEFT", { event = "repeat" })
eq(seeks_sent(), 1, "the stick stayed muted after waiting out the interval")

-- An analog axis resting on its threshold chatters across it, and each
-- crossing arrives as a fresh PRESS rather than a repeat -- so limiting
-- only the events mpv marks as repeats would leave the sticks as bad as
-- they were.
fake.advance(1)
fake.reset_events()
fake.key("mpvtk_gp_GAMEPAD_RIGHT_STICK_LEFT")
fake.key("mpvtk_gp_GAMEPAD_RIGHT_STICK_LEFT")
fake.key("mpvtk_gp_GAMEPAD_RIGHT_STICK_LEFT")
eq(seeks_sent(), 1, "stick chatter was delivered press for press")

-- A release is not an action. With `complex` bindings mpv reports it, and
-- acting on it would double every press.
fake.advance(1)
fake.reset_events()
fake.key("mpvtk_gp_GAMEPAD_RIGHT_STICK_LEFT", { event = "up" })
eq(seeks_sent(), 0, "letting go of the stick counted as a seek")

-- Confirm does not repeat AT ALL, and that is mpv's flag rather than a
-- rate: holding a button is not a request to press it again, and an
-- auto-repeating Select activates whatever it lands on over and over.
eq(fake.log.keyopts["mpvtk_gp_GAMEPAD_ACTION_DOWN"].repeatable, false,
   "the confirm button was left auto-repeating")
eq(fake.log.keyopts["mpvtk_gp_GAMEPAD_DPAD_UP"].repeatable, true,
   "the d-pad cannot be held to scroll")
-- ...and a rate of 0 must not become a rate limit, or a double press of
-- Select would be swallowed.
fake.log.commands = {}
fake.key("mpvtk_gp_GAMEPAD_ACTION_DOWN")
fake.key("mpvtk_gp_GAMEPAD_ACTION_DOWN")
local enters = 0
for _, c in ipairs(fake.log.commands) do
    if c[1] == "keypress" and c[2] == "ENTER" then enters = enters + 1 end
end
eq(enters, 2, "a second press of Select was swallowed as a repeat")

-- Re-pushing rebinds, which is what makes the confirm/back swap apply
-- without a restart. The keys that LEAVE the table have to go with it, or
-- the old meaning survives underneath the new one.
fake.send("mpvtk-gamepad", fake.token({
    { "GAMEPAD_ACTION_RIGHT", "key", "ENTER", 0 },
}))
eq(fake.log.keybinds["mpvtk_gp_GAMEPAD_DPAD_UP"], nil,
   "a re-push left a binding the new table does not have")
fake.log.commands = {}
fake.advance(1)
fake.key("mpvtk_gp_GAMEPAD_ACTION_RIGHT")
local swapped = nil
for _, c in ipairs(fake.log.commands) do
    if c[1] == "keypress" then swapped = c[2] end
end
eq(swapped, "ENTER", "the re-pushed table kept the old meaning of the key")
-- ...and it is bound for the mpv key the table names. Every other log
-- here is keyed by BINDING NAME, which the renderer derives from the key
-- itself -- so a renderer that bound one fixed key under all the right
-- names would satisfy the whole suite, and the confirm/back swap is
-- nothing but a change of which key carries which meaning.
eq(fake.log.keykeys["mpvtk_gp_GAMEPAD_ACTION_RIGHT"], "GAMEPAD_ACTION_RIGHT",
   "the binding was made for a different key than the table named")

-- Malformed rows are skipped rather than binding something that errors on
-- press. This arrives from another process, so "cannot happen" is not a
-- property of this file.
fake.send("mpvtk-gamepad", fake.token({
    { "GAMEPAD_ACTION_UP" }, { 1, 2, 3 }, "nonsense",
    { "GAMEPAD_BACK", "key", "ESC", 0 },
}))
eq(fake.log.keybinds["mpvtk_gp_GAMEPAD_ACTION_UP"], nil,
   "a row with no kind was bound anyway")
ok(fake.log.keybinds["mpvtk_gp_GAMEPAD_BACK"] ~= nil,
   "a bad row stopped the good ones after it being bound")

-- Leave the renderer browsing for the teardown block below.
fake.send("mpvtk-gamepad", fake.token({}))
fake.send("mpvtk-hud", "no")
fake.send("mpvtk-active", "yes")

-- ------------------------------------------------- force on a dropdown

-- `force` means "the scene's value wins over the renderer's own", and the
-- renderer keeps a selection per dropdown across scene pushes. There was no
-- coverage of the pair at all, and the gap hid a real bug: dd_state ran the
-- force reset from the DRAW, so the frame after a click redrew the label as
-- the pre-click option -- every time, not in a race -- until the app pushed
-- a scene agreeing. The textbox and the slider both guard their force reset
-- against an in-flight gesture; the dropdown did not.

--- A dropdown's renderer-local selection, after actually painting.
---
--- The paint is the point: `dd_state` materializes from the draw, so a
--- scene push alone leaves nothing to read -- and the draw is also where
--- the bug lived, so a helper that skipped it could not see it.
local function dd_sel(id)
    fake.advance(0.1)
    fake.fire_timers()
    fake.reset_events()
    fake.send("mpvtk-debug", fake.token({ cmd = "state" }))
    local dd = (last_event("debug_state") or {}).dropdowns or {}
    return dd[id] and dd[id].sel
end

local function forced_dd(sel)
    scene({ { id = "fdd", t = "dropdown", x = 40, y = 40, w = 200, h = 30,
              size = 18, items = { "Any", "English", "German" },
              sel = sel, force = true } })
end

forced_dd(0)
eq(dd_sel("fdd"), 0, "the forced dropdown did not take the scene's value")

click("fdd")                            -- open the popup
fake.send("mpvtk-debug", fake.token({ cmd = "popup", index = 1 }))
-- Read the event BEFORE dd_sel, which resets the log to find its own reply.
eq((last_event("select") or {}).index, 1, "the app was not told")
eq(dd_sel("fdd"), 1, "the click did not move the selection")

-- The frame before the app has answered. This is the bug: it runs from the
-- draw, so an unguarded force put the label back to "Any" immediately.
fake.advance(0.1)
fake.fire_timers()
eq(dd_sel("fdd"), 1, "the selection snapped back before the app answered")

-- ...and so does a scene push that still carries the old value, which is
-- any unrelated repaint landing in the same window (a poller, the filter
-- panel's own match-count spinner).
forced_dd(0)
eq(dd_sel("fdd"), 1, "a stale repaint reverted the click")

-- The app agrees: the gesture ends and the scene is authoritative again.
forced_dd(1)
eq(dd_sel("fdd"), 1, "the agreed value was lost")

-- ...which is what makes THIS work -- the case force exists for. Clear All
-- empties the filters and re-queries; without force the picker went on
-- showing the language the user just cleared.
forced_dd(0)
eq(dd_sel("fdd"), 0, "the scene could not move the picker back to Any")

-- An answer that is neither the old value nor the picked one still ends the
-- gesture, or a rejected selection would be shown as accepted forever.
click("fdd")
fake.send("mpvtk-debug", fake.token({ cmd = "popup", index = 1 }))
eq(dd_sel("fdd"), 1, "sanity: the click moved it")
forced_dd(2)
eq(dd_sel("fdd"), 2, "a third answer did not end the gesture")

-- Without force the renderer keeps its own selection, which is the
-- behaviour every unforced dropdown relies on.
scene({ { id = "udd", t = "dropdown", x = 40, y = 40, w = 200, h = 30,
          size = 18, items = { "Any", "English" }, sel = 0 } })
click("udd")
fake.send("mpvtk-debug", fake.token({ cmd = "popup", index = 1 }))
scene({ { id = "udd", t = "dropdown", x = 40, y = 40, w = 200, h = 30,
          size = 18, items = { "Any", "English" }, sel = 0 } })
eq(dd_sel("udd"), 1, "an unforced dropdown was reset by a scene push")

-- ========================================================== teardown

scene({})
eq(fake.scroll_prop()["logs"], nil, "state for a vanished node is dropped")

print(string.format("1..%d", n))
if failed > 0 then
    io.stderr:write(string.format("%d of %d failed\n", failed, n))
    os.exit(1)
end
os.exit(0)
