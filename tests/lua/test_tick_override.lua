-- The JMS_TICK_MS diagnostic override of the renderer's frame-cadence floor.
--
-- The floor is read once at load, so each case is its own process: the Python
-- wrapper (tests/test_renderer_lua.py) sets JMS_FRAME_DIAG / JMS_TICK_MS and
-- the cadence it expects in JMS_TEST_EXPECT_TICK_MS (milliseconds).
--
-- Prints "ok N" / "not ok N - why", then a "1..N" plan.

local here = arg[0]:match("^(.*)/[^/]*$") or "."
package.path = here .. "/?.lua;" .. package.path

local fake = require("fake_mp")
fake.install()

local RENDERER = assert(arg[1], "usage: test_tick_override.lua <renderer.lua>")
local want = assert(tonumber(os.getenv("JMS_TEST_EXPECT_TICK_MS")),
                    "JMS_TEST_EXPECT_TICK_MS is required")

fake.log.props["input-builtin-dragging"] = true
assert(loadfile(RENDERER))()

local n, failed = 0, 0
local function ok(cond, name, detail)
    n = n + 1
    if not cond then failed = failed + 1 end
    print(string.format("%s %d - %s%s", cond and "ok" or "not ok", n, name,
        (not cond and detail) and ("\n    # " .. tostring(detail)) or ""))
end

-- First frame: the window size arrives and the renderer asks for a render.
local before = #fake.log.timers
fake.observe("osd-dimensions", { w = 1280, h = 720 })
local tick
for i = before + 1, #fake.log.timers do
    if not fake.log.timers[i].periodic then tick = fake.log.timers[i]; break end
end
ok(tick ~= nil, "a first frame was scheduled")
fake.advance(0.05)
fake.fire_timers()

-- The frame after it is scheduled one cadence floor after the last one: a cheap
-- frame (the fake clock creeps ~1 ms a read) leaves the floor as the pace.
fake.observe("osd-dimensions", { w = 1281, h = 721 })
local got = tick and tick.timeout and tick.timeout * 1000
ok(got ~= nil and got <= want and got > want - 8,
   string.format("the next frame waits about %g ms", want),
   string.format("got %s ms", tostring(got)))

local diag = fake.log.props["user-data/mpvtk/diag"]
if os.getenv("JMS_FRAME_DIAG") then
    ok(diag ~= nil and diag.tick_ms ~= nil, "the diagnostics publish tick_ms")
    ok(diag and math.abs(diag.tick_ms - want) < 1e-6,
       string.format("tick_ms is %g", want),
       diag and tostring(diag.tick_ms))
else
    ok(diag == nil, "no diagnostics are published when they are off")
end

print("1.." .. n)
os.exit(failed == 0 and 0 or 1)
