-- The opt-in scroll recorder (JMS_SCROLL_DIAG) in renderer.lua.
--
-- It is read once at load, so each case is its own process: the Python wrapper
-- (tests/test_renderer_lua.py) sets JMS_FRAME_DIAG / JMS_SCROLL_DIAG and says in
-- JMS_TEST_SDIAG_EXPECT whether a file must come out ("on") or not ("off"). The
-- same scripted gesture runs every time and prints a TRACE line per step -- the
-- stored and the PAINTED offset -- so the wrapper can compare the traces of the
-- recorder-on and recorder-off runs: the recorder must not move a pixel.
--
-- Prints "ok N" / "not ok N - why", the TRACE lines, then a "1..N" plan.

local here = arg[0]:match("^(.*)/[^/]*$") or "."
package.path = here .. "/?.lua;" .. package.path

local fake = require("fake_mp")
fake.install()
local mp = require("mp")
-- A clock that moves only when the test moves it: the fake's per-read creep
-- would make a run that reads the clock more often (any diagnostic) differ
-- from one that does not, and that is not what is under test.
mp.get_time = fake.clock

local RENDERER = assert(arg[1], "usage: test_scroll_diag.lua <renderer.lua>")
local expect = assert(os.getenv("JMS_TEST_SDIAG_EXPECT"), "JMS_TEST_SDIAG_EXPECT is required")
local path = os.getenv("JMS_SCROLL_DIAG")

fake.log.props["input-builtin-dragging"] = true
assert(loadfile(RENDERER))()
fake.observe("osd-dimensions", { w = 1280, h = 720 })

local n, failed = 0, 0
local function ok(cond, name, detail)
    n = n + 1
    if not cond then failed = failed + 1 end
    print(string.format("%s %d - %s%s", cond and "ok" or "not ok", n, name,
        (not cond and detail) and ("\n    # " .. tostring(detail)) or ""))
end

local MARK_W, MARK_H = 40, 400000
local function scene()
    fake.send("mpvtk-scene", fake.token({ nodes = {
        { id = "g", t = "scroll", axis = "y", x = 0, y = 0, w = 400, h = 300,
          cw = 400, ch = 1500, snap = 250, snap_off = 0 },
        { id = "g-mark", t = "img", sc = "g", src = "/g", x = 0, y = 0,
          w = MARK_W, h = MARK_H, iw = MARK_W, ih = MARK_H } } }))
end
local function stored() return fake.scroll_prop()["g"] or 0 end
local function drawn()
    local found
    for _, c in ipairs(fake.log.commands) do
        if c[1] == "overlay-add" and c[5] == "/g" then
            found = tonumber(c[6]) / tonumber(c[10])
        end
    end
    return found
end

scene()
fake.mouse(100, 100)
fake.advance(1)
fake.fire_timers()

local step_no = 0
local function frame(dt)
    fake.advance(dt or 0.03)
    fake.fire_timers()
end
local function trace(tag)
    step_no = step_no + 1
    print(string.format("TRACE %d %s stored=%.3f drawn=%s", step_no, tag, stored(),
        tostring(drawn())))
end
-- A scripted wheel event: `scale` nil is a mouse-wheel notch.
local function wheel(name, scale)
    if scale then fake.key(name, { scale = scale }) else fake.key(name) end
end

-- 1. slow notches, 150 ms apart, a frame every 30 ms
for i = 1, 5 do
    wheel("wheel_down"); trace("notch" .. i)
    for _ = 1, 5 do frame(); trace("frame") end
end
-- 2. a reversal in the middle of an ease
wheel("wheel_down"); frame(); wheel("wheel_up"); trace("reverse")
for _ = 1, 8 do frame(); trace("frame") end
-- 3. a slow touchpad drag, ~140 events a second, a frame every ~4 events
for i = 1, 40 do
    fake.advance(0.007); wheel("wheel_down", 0.05)
    if i % 4 == 0 then frame(0); trace("pad") end
end
for _ = 1, 4 do frame(); trace("frame") end
-- 4. frames of a hard touchpad flick: just under and at the notch threshold
fake.advance(0.05); wheel("wheel_down", 0.98); trace("pad0.98")
fake.advance(0.007); wheel("wheel_down", 0.99); trace("pad0.99")
fake.advance(0.007); wheel("wheel_down", 1.0); trace("pad1.0")
for _ = 1, 8 do frame(); trace("frame") end
-- 5. down to the end (clamp at the top of the range), then up to the start
for _ = 1, 24 do wheel("wheel_down"); frame() end
trace("at-end")
for _ = 1, 3 do frame(); end
for _ = 1, 30 do wheel("wheel_up"); frame() end
for _ = 1, 10 do frame() end
trace("at-start")
wheel("wheel_up"); frame(); trace("past-start")

-- ----------------------------------------------------------------- checks
local function readfile()
    local f = io.open(path, "r")
    if not f then return nil end
    local t = f:read("*a")
    f:close()
    return t
end

if expect == "off" then
    ok(path == nil or readfile() == nil, "no file is written when the recorder is off")
else
    -- the buffer is flushed by its idle timer
    fake.advance(2); fake.fire_timers()
    local txt = readfile()
    ok(txt ~= nil, "the recorder wrote a file")
    local E, F, hdr = {}, {}, 0
    for line in (txt or ""):gmatch("[^\n]+") do
        local cols = {}
        for c in (line .. "\t"):gmatch("([^\t]*)\t") do cols[#cols + 1] = c end
        if line:sub(1, 1) == "#" then hdr = hdr + 1
        elseif cols[1] == "E" then E[#E + 1] = cols
        elseif cols[1] == "F" then F[#F + 1] = cols end
    end
    ok(hdr == 3, "three header lines")
    -- 5 + 2 + 40 + 3 + 24 + 30 + 1 wheel events
    ok(#E == 105, "one E line per wheel event", tostring(#E))
    ok(#F > 40, "frames were recorded while the gesture was live", tostring(#F))
    -- E columns: 1 E,2 t,3 interval,4 dir,5 axis,6 scale,7 class,8 c,9 max,10 before,
    -- 11 after,12 want,13 clamp,14 glide0,15 glide1,16 snap
    ok(E[1][3] == "-" and E[1][6] == "-" and E[1][7] == "W" and E[1][4] == "1",
       "the first event: no interval, no scale delivered, a notch, down")
    ok(math.abs(tonumber(E[2][3]) - 150) < 1,
       "an interval is the time between events (5 frames of 30 ms)", E[2][3])
    ok(E[1][8] == "c1" and E[1][9] == "1200", "container c1 and its scroll range (1500 - 300)")
    local want1 = tonumber(E[1][12])
    ok(math.abs(want1 - tonumber(E[1][10]) - 250 / 3) < 0.01, "want is before + one quantized notch (250/3)")
    ok(E[1][13] == "-" and E[1][14] == "0" and E[1][15] == "1", "a notch starts a glide")
    -- the touchpad drag: scale recorded, class T, no glide started, gain applied
    local pad
    for _, e in ipairs(E) do if e[6] == "0.0500" then pad = e; break end end
    ok(pad ~= nil and pad[7] == "T" and pad[15] ~= nil, "a 0.05 delta is recorded as class T")
    ok(pad and math.abs(tonumber(pad[11]) - tonumber(pad[10]) - 0.05 * 250 / 3 * 0.25) < 0.01,
       "and moved by the fine gain", pad and (pad[10] .. " -> " .. pad[11]))
    -- the threshold: 0.98 is T, 0.99 and 1.0 are W
    local cls = {}
    for _, e in ipairs(E) do cls[e[6]] = e[7] end
    ok(cls["0.9800"] == "T" and cls["0.9900"] == "W" and cls["1.0000"] == "W",
       "0.98 is a fine delta, 0.99 and 1.0 are notches", tostring(cls["0.9800"]) .. tostring(cls["0.9900"]) .. tostring(cls["1.0000"]))
    -- boundaries
    local hi, lo
    for _, e in ipairs(E) do
        if e[13] == "hi" then hi = hi or e end
        if e[13] == "lo" then lo = e end
    end
    ok(hi ~= nil and tonumber(hi[11]) == 1200, "the end is a clamp 'hi' that stopped at the range")
    ok(lo ~= nil and tonumber(lo[11]) == 0, "the start is a clamp 'lo' that stopped at 0")
    ok(E[#E][13] == "lo" and E[#E][11] == "0.00", "a notch past the start is clamped and moves nothing")
    -- F columns: 1 F,2 t,3 gap,4 c,5 stored,6 drawn,7 d_drawn,8 glide,9 glide_pos,10 snap
    local sum = 0
    for _, f in ipairs(F) do sum = sum + tonumber(f[7]) end
    ok(math.abs(sum - (tonumber(F[#F][6]) - (tonumber(F[1][6]) - tonumber(F[1][7])))) < 1e-6,
       "the per-frame displacements add up to the drawn travel")
    local dsum, dd = 0, true
    for i = 2, #F do
        if tonumber(F[i][7]) ~= tonumber(F[i][6]) - tonumber(F[i - 1][6]) then dd = false end
    end
    ok(dd, "d_drawn is the drawn offset minus the previous frame's")
    -- the last frame's drawn value matches what the test saw painted
    ok(tonumber(F[#F][6]) == drawn(), "the last recorded drawn offset is the painted one",
       tostring(F[#F][6]) .. " vs " .. tostring(drawn()))
    -- privacy: nothing but numbers, flags and numbered containers
    local leak = false
    for line in (txt or ""):gmatch("[^\n]+") do
        if line:sub(1, 1) ~= "#" and line:find("[A-Za-z]") then
            local body = line:gsub("^[EFT]\t", "")
            body = body:gsub("truncated", ""):gsub("c%d+", ""):gsub("%f[%a]hi%f[%A]", ""):gsub("%f[%a]lo%f[%A]", ""):gsub("[WTLFSxy]", ""):gsub("e%-?%d+", "")
            if body:find("[A-Za-z]") then leak = true end
        end
    end
    ok(not leak, "the file holds only numbers, flags and numbered containers")
    ok(not (txt or ""):find("/g") and not (txt or ""):find("g%-mark"), "no container or image names")
end

print("1.." .. n)
os.exit(failed == 0 and 0 or 1)
