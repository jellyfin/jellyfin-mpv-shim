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

-- =================================================== HUD auto-hide

-- The controls' auto-hide is a policy (hud_autohide), and the pointer
-- resting ON them holds it off in every mode but 'always'. Reaching for a
-- button must not be a race against the timer.

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
pv_paint()
ok(preview() ~= nil and math.abs(preview().secs - dragged) < 20,
   "releasing the drag snapped the bubble back to where it started",
   string.format("released at %s, was dragged to %s",
                 tostring(preview() and preview().secs), tostring(dragged)))

-- A bar that does not ask for a preview never gets one: this is opt-in
-- (the volume slider is the same widget).
scene({ { id = "hud-bar", t = "rect", x = 0, y = 640, w = 1280, h = 80 },
        { id = "hud-vol", t = "slider", x = 100, y = 660, w = 1080,
          h = 26, min = 0, max = 100, value = 50 } })
hud_pointer(642, 670)
pv_paint()
ok(preview() == nil, "a slider without pv draws no bubble")

-- ========================================================== teardown

scene({})
eq(fake.scroll_prop()["logs"], nil, "state for a vanished node is dropped")

print(string.format("1..%d", n))
if failed > 0 then
    io.stderr:write(string.format("%d of %d failed\n", failed, n))
    os.exit(1)
end
os.exit(0)
