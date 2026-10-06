-- Property sweep: every overlay-add the renderer issues for a scene image
-- addresses only bytes inside that image's iw*ih*4 -- at fractional
-- positions, behind scroll offsets, layers and occluders, and scaled (the
-- dw/dh branch the scrub preview uses). On libmpv the src is a raw address,
-- so one byte past the end is a read of someone else's memory; #800 was a
-- float edge of 432.99999999999994 that integer-positioned tests never made.
--   lua test_image_bounds.lua ../../jellyfin_mpv_shim/mpvtk/renderer.lua
package.path = "./?.lua;" .. package.path
local fake = require("fake_mp"); fake.install()
fake.log.props["input-builtin-dragging"] = true
assert(loadfile(arg[1]))()
fake.observe("osd-dimensions", { w = 1280, h = 720 })

local function scene(nodes) fake.send("mpvtk-scene", fake.token({ nodes = nodes })) end
local function settle() fake.advance(1.0); fake.fire_timers() end

math.randomseed(800)
local function frac()
    local p = { 0, 0.5, 0.99999999999994, 1e-13, 0.25, 0.7 }
    return p[math.random(#p)]
end

local BASE = 1048576
local bad, adds, scaled_adds = 0, 0, 0
local first_bad
for trial = 1, 6000 do
    local iw, ih = math.random(1, 400), math.random(1, 400)
    local src = (trial % 2 == 0) and ("&" .. BASE) or "/s"
    local img = { id = "im", t = "img", src = src, sc = "sc",
        x = math.random(-300, 900) + frac(), y = math.random(-300, 900) + frac(),
        w = iw + math.random(-5, 50) + frac(), h = ih + math.random(-5, 50) + frac(),
        iw = iw, ih = ih }
    if trial % 3 == 0 then
        local k = ({ 0.5, 0.75, 1.25, 1.5, 2, 2.5 })[math.random(6)]
        img.dw = math.max(1, math.floor(iw * k + 0.5))
        img.dh = math.max(1, math.floor(ih * k + 0.5))
        img.w, img.h = img.dw + frac(), img.dh + frac()
    end
    local nodes = {
        { id = "sc", t = "scroll", axis = "y", x = math.random(0, 50) + frac(),
          y = math.random(0, 50) + frac(), w = 1000 + frac(), h = 600 + frac(),
          cw = 1000, ch = 5000 },
        img,
    }
    if math.random() < 0.5 then
        nodes[#nodes + 1] = { id = "ly", t = "layer",
            x = math.random(-50, 900) + frac(), y = math.random(-50, 600) + frac(),
            w = math.random(1, 300) + frac(), h = math.random(1, 300) + frac() }
    end
    if math.random() < 0.5 then
        nodes[#nodes + 1] = { id = "oc", t = "occ", sc = "sc",
            x = math.random(-50, 900) + frac(), y = math.random(-50, 900) + frac(),
            w = math.random(1, 300) + frac(), h = math.random(1, 300) + frac() }
    end
    local before = #fake.log.commands
    scene({}); scene(nodes); settle()
    fake.send("mpvtk-scroll", fake.token({ id = "sc", to = math.random(0, 400) + frac() }))
    settle()
    for i = before + 1, #fake.log.commands do
        local c = fake.log.commands[i]
        if c[1] == "overlay-add" and (c[5] == "/s" or c[5]:sub(1, 1) == "&") then
            adds = adds + 1
            if c[12] then scaled_adds = scaled_adds + 1 end
            local off = tonumber(c[6])
            if c[5]:sub(1, 1) == "&" then off = tonumber(c[5]:sub(2)) - BASE end
            local w, h, stride = tonumber(c[8]), tonumber(c[9]), tonumber(c[10])
            local last = off + (h - 1) * stride + w * 4
            if stride ~= iw * 4 or off < 0 or w < 1 or h < 1
                    or (off % stride) / 4 + w > iw or last > iw * ih * 4 then
                bad = bad + 1
                first_bad = first_bad or string.format(
                    "iw=%d ih=%d off=%s w=%s h=%s stride=%s x=%.17g y=%.17g",
                    iw, ih, tostring(off), c[8], c[9], c[10], img.x, img.y)
            end
        end
    end
end

print("1..3")
print((adds > 1000 and "ok" or "not ok") .. " 1 - the sweep issued overlays (" .. adds .. ")")
print((scaled_adds > 100 and "ok" or "not ok") .. " 2 - and scaled ones (" .. scaled_adds .. ")")
print((bad == 0 and "ok" or "not ok") .. " 3 - every one inside its image"
      .. (first_bad and (" -- first out of bounds: " .. first_bad) or ""))
os.exit(bad == 0 and adds > 1000 and scaled_adds > 100 and 0 or 1)
