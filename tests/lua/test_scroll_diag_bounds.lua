-- The scroll recorder's output is bounded: a small in-memory buffer that is
-- written out in blocks, and a file that stops growing. Env: JMS_FRAME_DIAG=1,
-- JMS_SCROLL_DIAG=<file> (set by tests/test_renderer_lua.py).

local here = arg[0]:match("^(.*)/[^/]*$") or "."
package.path = here .. "/?.lua;" .. package.path
local fake = require("fake_mp")
fake.install()
local mp = require("mp")
mp.get_time = fake.clock
local path = assert(os.getenv("JMS_SCROLL_DIAG"))
fake.log.props["input-builtin-dragging"] = true
assert(loadfile(assert(arg[1])))()
fake.observe("osd-dimensions", { w = 1280, h = 720 })

local n, failed = 0, 0
local function ok(cond, name, detail)
    n = n + 1
    if not cond then failed = failed + 1 end
    print(string.format("%s %d - %s%s", cond and "ok" or "not ok", n, name,
        (not cond and detail) and ("\n    # " .. tostring(detail)) or ""))
end

fake.send("mpvtk-scene", fake.token({ nodes = {
    { id = "g", t = "scroll", axis = "y", x = 0, y = 0, w = 400, h = 300,
      cw = 400, ch = 1500, snap = 250, snap_off = 0 } } }))
fake.mouse(100, 100)
fake.advance(1)
fake.fire_timers()

local function lines()
    local f = io.open(path, "r")
    if not f then return 0 end
    local c = 0
    for _ in f:lines() do c = c + 1 end
    f:close()
    return c
end

-- Events are buffered: nothing but the header reaches the file until 128 lines
-- have collected (or the idle timer fires).
for _ = 1, 100 do fake.advance(0.001); fake.key("wheel_down", { scale = 0.05 }) end
ok(lines() == 3, "100 events are still in the buffer: only the header is on disk", tostring(lines()))
for _ = 1, 28 do fake.advance(0.001); fake.key("wheel_down", { scale = 0.05 }) end
ok(lines() == 3 + 128, "the 128th line writes the whole block out", tostring(lines()))

-- A long run: the file stops at its cap, with one closing marker, and the
-- memory the recorder holds does not keep growing.
local function mem() collectgarbage(); collectgarbage(); return collectgarbage("count") end
for _ = 1, 20000 do fake.advance(0.001); fake.key("wheel_down", { scale = 0.05 }) end
local m1 = mem()
for _ = 1, 100000 do fake.advance(0.001); fake.key("wheel_up", { scale = 0.05 }) end
local m2 = mem()
ok(m2 - m1 < 1024, "memory is flat across 100000 more events (KB grown: " .. string.format("%.0f", m2 - m1) .. ")")
fake.advance(2); fake.fire_timers()
local total = lines()
ok(total == 3 + 100000 + 1, "the file holds the header, 100000 lines and a marker", tostring(total))
local last
for l in io.lines(path) do last = l end
ok(last == "T\ttruncated", "the last line says the file was truncated", tostring(last))
for _ = 1, 1000 do fake.advance(0.001); fake.key("wheel_down", { scale = 0.05 }) end
fake.advance(2); fake.fire_timers()
ok(lines() == total, "nothing more is written once it is capped", tostring(lines()))

print("1.." .. n)
os.exit(failed == 0 and 0 or 1)
