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

-- ========================================================== teardown

scene({})
eq(fake.scroll_prop()["logs"], nil, "state for a vanished node is dropped")

print(string.format("1..%d", n))
if failed > 0 then
    io.stderr:write(string.format("%d of %d failed\n", failed, n))
    os.exit(1)
end
os.exit(0)
