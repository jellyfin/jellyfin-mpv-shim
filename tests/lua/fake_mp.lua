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

local Timer = {}
Timer.__index = Timer

function Timer:is_enabled() return self.enabled end
function Timer:resume() self.enabled = true end
function Timer:stop() self.enabled = false end
function Timer:kill() self.enabled = false; self.dead = true end

--- Run every armed timer once, newest arming first. Timers are how the
--- renderer defers drawing; a test that never calls this exercises the
--- logic without ever painting.
function M.fire_timers()
    for _, t in ipairs(M.log.timers) do
        if t.enabled and not t.dead then
            t.enabled = false
            t.fn()
        end
    end
end

-- ------------------------------------------------------------- mp table

local mp = {}
local msg_handlers = {}
local prop_observers = {}
local now = 0

function mp.create_osd_overlay()
    return { data = "", update = function() end, remove = function() end }
end

function mp.add_timeout(timeout, fn)
    local t = setmetatable(
        { timeout = timeout, fn = fn, enabled = true }, Timer)
    table.insert(M.log.timers, t)
    return t
end

function mp.add_periodic_timer(timeout, fn)
    return mp.add_timeout(timeout, fn)
end

function mp.get_time() now = now + 0.001; return now end

function mp.commandv(...)
    local args = { ... }
    table.insert(M.log.commands, args)
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

function mp.add_key_binding(key, name, fn, opts)
    M.log.keybinds[name or key] = fn
end

function mp.add_forced_key_binding(key, name, fn, opts)
    M.log.keybinds[name or key] = fn
end

function mp.remove_key_binding(name) M.log.keybinds[name] = nil end
function mp.enable_key_bindings() end
function mp.disable_key_bindings() end
function mp.set_key_bindings() end
function mp.register_event() end
function mp.register_idle() end
function mp.get_script_name() return "mpvtk" end
function mp.osd_message() end

mp.msg = { error = function() end, warn = function() end,
           info = function() end, verbose = function() end,
           debug = function() end, log = function() end }
mp.utils = utils

-- assdraw: only ass_new() and the builder methods the renderer chains.
local Ass = {}
Ass.__index = function(_t, _k) return function(s) return s end end
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

--- Fire a property observer, as mpv would.
function M.observe(name, value)
    for _, fn in ipairs(prop_observers[name] or {}) do fn(name, value) end
end

function M.key(name)
    local fn = M.log.keybinds[name]
    if not fn then error("no key binding: " .. name) end
    return fn()
end

function M.scroll_prop() return M.log.props["user-data/mpvtk/scroll"] or {} end

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
