-- A stand-in for the mpv scripting API, enough to load renderer.lua.
--
-- Only mpv's I/O is faked. Every decision renderer.lua makes -- scroll
-- clamping, follow-the-tail, textbox commit, focus -- runs for real.
--
-- The JSON codec is faked too, and deliberately: parse_json hands back the
-- table the test registered under a token, and format_json returns the table
-- untouched. So a test builds a scene as a Lua table and reads events back as
-- Lua tables, with no JSON round trip to get wrong on either side. The
-- renderer never inspects the encoded form, so nothing under test notices.

local M = {}

M.log = {
    commands = {},      -- every commandv, in order
    events = {},        -- decoded mpvtk-event payloads
    props = {},         -- set_property_native by name
    timers = {},        -- live timers, so a test can fire them
    keybinds = {},
    -- The RELEASE half of a set_key_bindings pair (entry[2]), for the few
    -- gestures whose whole subject is what the press left behind -- a click
    -- resolves the node under the pointer twice, once on each half.
    keybinds_up = {},
    forced = {},       -- keybind name -> was it add_FORCED_key_binding?
    keyopts = {},      -- keybind name -> the flags table it was bound with
    -- keybind name -> the mpv KEY it was bound for. Everything else here
    -- is keyed by name, so without this nothing can assert which key
    -- carries which meaning -- and for the controller that is the whole
    -- payload: the confirm/back swap changes nothing but that.
    keykeys = {},
    sections = {},      -- set_key_bindings group name -> {key = true}
    enabled = {},       -- section name -> enable_key_bindings state
    section_flags = {}, -- section name -> the flags it was enabled with
    -- osd updates, ie frames actually painted. The renderer paces itself to
    -- what a frame costs, so how many of them a gesture got is an
    -- observable and not just a detail.
    draws = 0,
}

-- Property names mpv answers "property unavailable" for, in both
-- directions. mpv 0.40 does this with clipboard/text on an X11 session --
-- it ships no x11 clipboard backend -- and a *silent* failure is exactly
-- what made that hard to spot, so the fake reproduces the real return
-- convention: set_property yields nil + err rather than raising.
M.unavailable = {}

-- Handler for the `subprocess` command: function(t) -> result table.
-- Left unset, every subprocess fails as if the binary were not installed,
-- which is the state a fallback has to cope with.
M.subprocess = nil

-- ------------------------------------------------------------ json stub

local tokens = {}
local next_token = 0

--- Register `tbl` and return the opaque string a script message takes.
function M.token(tbl)
    next_token = next_token + 1
    local key = "\0tok" .. next_token
    tokens[key] = tbl
    return key
end

local utils = {}

function utils.parse_json(s)
    if type(s) == "table" then return s end
    local hit = tokens[s]
    if hit ~= nil then return hit end
    return nil, "unregistered token"
end

function utils.format_json(tbl)
    return tbl
end

function utils.to_string(v)
    return tostring(v)
end

-- --------------------------------------------------------------- timers

-- The clock. Read by mp.get_time, jumped by M.advance, and what every
-- timer's deadline is measured against; declared up here because the Timer
-- methods below need it.
local now = 0

local Timer = {}
Timer.__index = Timer

function Timer:is_enabled() return self.enabled end
function Timer:stop() self.enabled = false end
function Timer:kill() self.enabled = false; self.dead = true end

--- Re-arms from *now*, as mpv's does: resuming a stopped timer restarts its
--- countdown rather than resuming a partly-elapsed one. The renderer leans
--- on this — request_render sets `timeout` and resumes to schedule the next
--- frame, so the deadline has to follow the new timeout.
function Timer:resume()
    self.enabled = true
    self.deadline = now + (self.timeout or 0)
end

--- Run every armed timer that is DUE, oldest arming first. Timers are how
--- the renderer defers drawing; a test that never calls this exercises the
--- logic without ever painting.
---
--- Deadlines are honoured, so a test drives the clock and this fires
--- whatever that reached. Ignoring them (as this used to) collapses every
--- pending timer onto the same instant, which makes anything the renderer
--- decides from timing untestable: a 300ms gesture-release and the 30ms
--- frame it is supposed to outlive both fire, in arming order, and the
--- gesture is over before its first frame is drawn.
---
--- A periodic timer stays armed afterwards, as the real one does — an
--- animation driven by one has to be able to tick more than once.
function M.fire_timers()
    for _, t in ipairs(M.log.timers) do
        if t.enabled and not t.dead and (t.deadline or 0) <= now then
            if t.periodic then
                t.deadline = now + (t.timeout or 0)
            else
                t.enabled = false
            end
            t.fn()
        end
    end
end

-- ------------------------------------------------------------- mp table

local mp = {}
local msg_handlers = {}
local prop_observers = {}
local event_handlers = {}

--- Seconds an osd update "takes". The renderer times its own frames and
--- decides from that how fast to schedule them and whether scrolling has to
--- quantize, so how expensive drawing is has to be something a test can
--- state. Charged on the clock inside `update`, which is where the real
--- cost lands: it is the last thing render() does, and the measurement is
--- taken around the whole of it.
M.draw_cost = 0

--- ...and what one `overlay-add` costs. Separate knob, because the two are
--- separate claims: the renderer times the whole of render(), and the reason
--- that is worth anything is that the overlay re-issues -- the part that
--- scales with resolution, and that an external mpv pays a file mmap for --
--- happen INSIDE the timed region. A test that only ever charges draw_cost
--- cannot tell that apart from a measurement that stops before them.
M.overlay_cost = 0

function M.set_draw_cost(seconds) M.draw_cost = seconds or 0 end

function M.set_overlay_cost(seconds) M.overlay_cost = seconds or 0 end

function mp.create_osd_overlay()
    return {
        data = "",
        update = function()
            M.log.draws = M.log.draws + 1
            M.advance(M.draw_cost)
        end,
        remove = function() end,
    }
end

function mp.add_timeout(timeout, fn)
    local t = setmetatable(
        { timeout = timeout, fn = fn, enabled = true,
          deadline = now + (timeout or 0) }, Timer)
    table.insert(M.log.timers, t)
    return t
end

function mp.add_periodic_timer(timeout, fn)
    local t = mp.add_timeout(timeout, fn)
    t.periodic = true
    return t
end

-- The clock creeps on every read, so throttles and debounces make progress
-- without a test having to drive time by hand. M.advance jumps it, for the
-- tests that DO care -- an animation asked to run for 0.2s would otherwise
-- need 200 reads to get there.
--
-- The creep is also a floor under anything the renderer times: it reads the
-- clock either side of a frame, so no frame ever measures as free. Tests
-- that care what a frame costs set M.draw_cost rather than counting on it.
function mp.get_time() now = now + 0.001; return now end

function M.advance(dt) now = now + dt end

function mp.commandv(...)
    local args = { ... }
    table.insert(M.log.commands, args)
    if args[1] == "overlay-add" and M.overlay_cost > 0 then
        M.advance(M.overlay_cost)
    end
    if args[1] == "script-message" and args[2] == "mpvtk-event" then
        table.insert(M.log.events, args[3])
    end
end

function mp.command_native(t)
    table.insert(M.log.commands, t)
    if type(t) == "table" and t.name == "subprocess" then
        if M.subprocess then return M.subprocess(t) end
        return { status = -1, stdout = "" }
    end
end
function mp.command(s) table.insert(M.log.commands, { s }) end

function mp.set_property_native(name, value)
    -- Copy: the renderer hands us its live state.scroll table, and holding
    -- the reference would make every assertion see the latest value rather
    -- than the one published at the time.
    if type(value) == "table" then
        local copy = {}
        for k, v in pairs(value) do copy[k] = v end
        value = copy
    end
    M.log.props[name] = value
end

-- Real mpv returns true, or nil + an error string; it does not raise. Code
-- that only pcall'd the call therefore saw every failure as a success.
function mp.set_property(name, value)
    if M.unavailable[name] then return nil, "property unavailable" end
    M.log.props[name] = value
    return true
end
function mp.set_property_bool(name, value) M.log.props[name] = value end

-- The comic reader's pan is set this way, sixty times a second, entirely in
-- the renderer -- so it never reached mpv through any of the paths above and
-- had no fake at all until the end-of-page interlock needed testing.
function mp.set_property_number(name, value) M.log.props[name] = value end
function mp.get_property_native(name, def) return M.log.props[name] or def end
function mp.get_property(name, def)
    if M.unavailable[name] then return nil, "property unavailable" end
    return M.log.props[name] or def
end
function mp.get_property_number(name, def) return M.log.props[name] or def end
function mp.get_property_bool(name, def) return M.log.props[name] or def end

function mp.observe_property(name, _kind, fn)
    prop_observers[name] = prop_observers[name] or {}
    table.insert(prop_observers[name], fn)
end

function mp.register_script_message(name, fn) msg_handlers[name] = fn end
function mp.unregister_script_message(name) msg_handlers[name] = nil end

-- Forced-ness is recorded, not just the handler. It is the whole
-- difference between a binding the user's own input.conf can override by
-- naming the key and one they cannot touch without disabling it first --
-- and with both functions writing the same row, "these are remappable" was
-- a claim no test could see, let alone fail on.
function mp.add_key_binding(key, name, fn, opts)
    M.log.keybinds[name or key] = fn
    M.log.forced[name or key] = false
    M.log.keyopts[name or key] = opts or {}
    M.log.keykeys[name or key] = key
end

function mp.add_forced_key_binding(key, name, fn, opts)
    M.log.keybinds[name or key] = fn
    M.log.forced[name or key] = true
    M.log.keyopts[name or key] = opts or {}
    M.log.keykeys[name or key] = key
end

function mp.remove_key_binding(name)
    M.log.keybinds[name] = nil
    M.log.forced[name] = nil
    M.log.keyopts[name] = nil
    M.log.keykeys[name] = nil
end
-- Whether a section is enabled cannot model mpv's section STACK, but it can
-- model the flag, which is what a "this group was never turned on" bug looks
-- like. M.log.enabled[name] is nil until someone enables or disables it.
-- The FLAGS are recorded separately rather than in place of the boolean:
-- whether a section is on and what it was enabled WITH are different
-- questions, and only the second one decides whether mpv will drag or
-- resize the window while the pointer is over our UI (see the renderer's
-- state.vodrag).
function mp.enable_key_bindings(name, flags)
    M.log.enabled[name] = true
    M.log.section_flags[name] = flags or ""
end
function mp.disable_key_bindings(name) M.log.enabled[name] = false end

-- Mouse and wheel bindings arrive as a whole *group*, and an entry may
-- carry two handlers: {key, on_up, on_down}. Recorded under the key name
-- so M.key('mbtn_back') reaches them; this was a no-op, which left every
-- binding in those groups invisible to the tests. The down half is the
-- one that acts (the renderer's up handlers finish a press it started),
-- so that is what M.key drives.
--
-- enable/disable_key_bindings stay no-ops: a fake cannot model mpv's
-- section stack, so a test can call a binding that is currently
-- suspended. WHETHER a group is enabled at a given moment is therefore a
-- claim only real mpv can settle -- but which group a binding is DECLARED
-- in is not, so the section names are recorded (M.log.sections) and a test
-- can assert on them.
function mp.set_key_bindings(list, name, _flags)
    local section = M.log.sections[name or "?"] or {}
    M.log.sections[name or "?"] = section
    for _, entry in ipairs(list or {}) do
        M.log.keybinds[entry[1]] = entry[3] or entry[2]
        if entry[3] then M.log.keybinds_up[entry[1]] = entry[2] end
        section[entry[1]] = true
    end
end
-- Recorded, not swallowed. `renderer.lua` reaches Python through
-- `register_script_message`, but `thumbfast.lua` -- the shim's compatibility
-- layer for thumbfast-style lua OSCs -- reads the raw `client-message`
-- EVENT instead, so a no-op here left that whole script undrivable and
-- therefore untested.
function mp.register_event(name, fn)
    event_handlers[name] = event_handlers[name] or {}
    table.insert(event_handlers[name], fn)
end
function mp.register_idle() end
function mp.get_script_name() return "mpvtk" end
function mp.osd_message() end

-- `mp.log(level, ...)` is the real primitive (player/lua.c:script_log);
-- mpv's own defaults.lua builds every mp.msg.* entry on top of it, and
-- thumbfast.lua calls it directly. Missing here, that was a nil-call the
-- moment anything logged.
function mp.log() end
mp.msg = { error = function() end, warn = function() end,
           info = function() end, verbose = function() end,
           debug = function() end, log = mp.log }
mp.utils = utils

-- assdraw: only ass_new() and the builder methods the renderer chains.
--
-- The methods used to be swallowed outright. They are recorded now, because
-- a class of bug lives entirely in the drawing and nowhere in the state the
-- rest of this file exposes: a selection highlight drawn in a colour nobody
-- can tell from the background, a bar whose thickness ignores the UI scale.
-- Recording is cheap (a table append per call) and the accessors below
-- reduce it to the two things worth asserting on -- rectangles and their
-- fills.
local shapes = {}
local pending = {}      -- \-tags appended since the last new_event

--- Rectangles drawn since the last :reset_draw(), as
--- {x, y, w, h, radius, fill = "rrggbb" or nil, alpha = 0-255,
---  blur = number or nil, clip = {x1, y1, x2, y2} or nil}.
--- `fill` is decoded back out of the ASS \1c tag, so a test names the
--- colour the way the theme does rather than in ASS's reversed hex.
---
--- `blur` and `clip` are here for the themed glow, whose whole bug surface
--- is the pair: it is blurred, so it must reach outside the box it decorates,
--- and it is clipped, so it must not reach outside the page. Neither is
--- observable anywhere else -- an unclipped halo is a correct rectangle
--- drawn over the header.
function M.shapes() return shapes end

function M.reset_draw()
    shapes = {}
    pending = {}
end

local function un_ass_color(tag)
    -- "&Hbbggrr&" -> "rrggbb"
    local bb, gg, rr = tag:match("&H(%x%x)(%x%x)(%x%x)&")
    if not rr then return nil end
    return (rr .. gg .. bb):lower()
end

local function tags()
    local blob = table.concat(pending)
    local fill = blob:match("\\1c(&H%x+&)")
    local alpha = blob:match("\\1a&H(%x%x)&")
    local clip
    local cx1, cy1, cx2, cy2 = blob:match(
        "\\clip%(([%d%.%-]+),([%d%.%-]+),([%d%.%-]+),([%d%.%-]+)%)")
    if cx1 then
        clip = { x1 = tonumber(cx1), y1 = tonumber(cy1),
                 x2 = tonumber(cx2), y2 = tonumber(cy2) }
    end
    return un_ass_color(fill or ""),
           alpha and (255 - tonumber(alpha, 16)) or 255,
           tonumber(blob:match("\\blur([%d%.]+)") or ""),
           clip
end

local function rect(_s, x1, y1, x2, y2, radius)
    local fill, alpha, blur, clip = tags()
    shapes[#shapes + 1] = { x = x1, y = y1, w = x2 - x1, h = y2 - y1,
                            radius = radius, fill = fill, alpha = alpha,
                            blur = blur, clip = clip }
end

local Ass = {}
local ASS_METHODS = {
    new_event = function(s) pending = {}; return s end,
    append = function(s, t) pending[#pending + 1] = tostring(t or ""); return s end,
    rect_cw = function(s, ...) rect(s, ...); return s end,
    round_rect_cw = function(s, ...) rect(s, ...); return s end,
}
Ass.__index = function(_t, k)
    return ASS_METHODS[k] or function(s) return s end
end
mp.assdraw = { ass_new = function()
    return setmetatable({ text = "" }, Ass)
end }

-- ------------------------------------------------------------- drivers

--- Deliver a script message, as mpv would.
function M.send(name, ...)
    local fn = msg_handlers[name]
    if not fn then error("no handler for script message: " .. name) end
    return fn(...)
end

function M.has_handler(name) return msg_handlers[name] ~= nil end

--- Deliver an mpv EVENT, as mpv would. See mp.register_event.
function M.emit(name, ev)
    local fired = false
    for _, fn in ipairs(event_handlers[name] or {}) do
        fired = true
        fn(ev)
    end
    if not fired then error("no handler for event: " .. name) end
end

--- The `client-message` an mpv `script-message` command arrives as: the
--- command name is args[1], exactly as the real event carries it. Scripts
--- that take this route see every script-message sent to anyone, which is
--- why they all switch on args[1] rather than registering by name.
function M.client_message(...)
    M.emit("client-message", { event = "client-message", args = { ... } })
end

--- Fire a property observer, as mpv would.
function M.observe(name, value)
    for _, fn in ipairs(prop_observers[name] or {}) do fn(name, value) end
end

function M.key(name, ...)
    local fn = M.log.keybinds[name]
    if not fn then error("no key binding: " .. name) end
    return fn(...)
end

--- The release of a down/up pair. M.key fires the DOWN half, which is the
--- one almost every test wants; this is its other half.
function M.key_up(name, ...)
    local fn = M.log.keybinds_up[name]
    if not fn then error("no key release binding: " .. name) end
    return fn(...)
end

--- Park the pointer, so hit-tested input (the wheel) has a target.
function M.mouse(x, y)
    M.observe("mouse-pos", { x = x, y = y, hover = true })
end

function M.scroll_prop() return M.log.props["user-data/mpvtk/scroll"] or {} end

--- The clock, without the creep mp.get_time adds. For tests that measure a
--- span of it (how many frames a gesture of a given length was painted in),
--- where reading the time must not itself move it along.
function M.clock() return now end

function M.reset_events() M.log.events = {} end

--- Install into package.preload so `require 'mp.utils'` &c. resolve.
function M.install()
    package.preload["mp"] = function() return mp end
    package.preload["mp.utils"] = function() return utils end
    package.preload["mp.msg"] = function() return mp.msg end
    package.preload["mp.assdraw"] = function() return mp.assdraw end
    _G.mp = mp
    return mp
end

M.mp = mp
return M
