-- mpvtk renderer: draws a declarative scene pushed from Python and owns
-- all per-frame interaction (hover, scrolling, text editing, dropdowns)
-- so no Python round-trip happens during drawing.
--
-- Protocol (script-messages):
--   mpvtk-scene  (py -> lua): JSON scene, see layout.py for node shapes.
--   mpvtk-event  (lua -> py): JSON events:
--       {t=ready|resize, w, h}
--       {t=click, id, shift?, ctrl?} {t=change|submit, id, value}
--       {t=select, id, index, value}
--       {t=debug_state, ...} (reply to mpvtk-debug state)
--   mpvtk-debug  (py -> lua): test hooks, JSON:
--       {cmd=hover|click, id=...} {cmd=wheel, id=..., dir=1|-1, steps=n}
--       {cmd=text, s="..."} {cmd=key, name="BS"} {cmd=state}
--
-- Scroll offsets are additionally mirrored into the
-- 'user-data/mpvtk/scroll' property on every change, so the Python side
-- can read them synchronously (tight virtualization windows) instead of
-- waiting for the throttled scroll event.
--
-- Renderer-local state (scroll offsets, textbox edits, dropdown
-- selection, focus) survives scene pushes, keyed by node id; a node
-- with force=true resets it from the scene.
--
-- Known z-order constraint (verified empirically on mpv 0.41):
-- overlay-add bitmaps composite ABOVE all script ASS. ASS therefore
-- cannot draw on top of an image. Consequences handled here:
--   * images are clipped to their scroll viewports by cropping the
--     source (offset/stride math), so fixed chrome outside a viewport
--     is never overdrawn;
--   * chrome that must appear over images (dropdown popups) is treated
--     as an occluder: its rect is subtracted from every image's visible
--     region, splitting the image into up to 4 sub-overlays;
--   * hover rings are drawn just OUTSIDE the image bounds.
-- Text/captions must not be laid out on top of Image nodes.

local assdraw = require 'mp.assdraw'
local utils = require 'mp.utils'
local msg = require 'mp.msg'

local unpack = unpack or table.unpack  -- Lua 5.1 / 5.2+ compat

local osd = mp.create_osd_overlay('ass-events')

-- Finest cadence the renderer ever aims for. It only ever *slows* from
-- here, and only by measurement — see active_tick and state.render_duty.
local TICK = 0.03
-- ...and the slowest. Backing off is the response to a frame that costs
-- too much, but a single pathological measurement must not be able to turn
-- the UI into a slideshow, and past ~7fps the backoff is worse than the
-- stutter it is treating.
local RENDER_PACE_MAX = 0.15
-- How long after the last wheel event we still consider a gesture "live".
local GESTURE_WINDOW = 0.3
-- (What a frame costs and how much of its slot it may spend live on `state`
-- as rcost/render_duty, with the rest of the snap state — this chunk is at
-- Lua's 200-local ceiling, so a new file-scope local costs someone theirs.)
local WHEEL_STEP = 80
local MAX_OVERLAYS = 63

local state = {
    scene = nil,
    nodes = {},
    byid = {},
    w = 0, h = 0,
    -- Design tokens, replaced wholesale by the mpvtk-theme message (see
    -- theme.py, which owns the names and the stock values). Everything the
    -- renderer draws for itself -- text fields, dropdowns and their popups,
    -- scrollbars, tooltips, the focus ring -- reads from here, because
    -- Python never sends a colour for any of them and so no call site could
    -- ever theme one.
    --
    -- A TABLE rather than one local per colour, for two reasons. This chunk
    -- is at LuaJIT's ceiling of 200 locals per function and adding ~20 more
    -- would fail to load outright ("main function has more than 200 local
    -- variables"), silently, as a blank UI. And a table can be swapped in
    -- one assignment, which is what makes changing theme at runtime a
    -- re-render rather than a restart.
    --
    -- The tok.chip_*/tok.scrim entries are drawn over VIDEO, not over the
    -- app, and stay dark whatever the app's theme does.
    tok = {
        on_surface = 'eeeeee',
        on_surface_strong = 'ffffff',
        on_surface_muted = 'aaaaaa',
        on_surface_faint = '777777',
        control_bg = '333333',
        control_hover = '4a4a4a',
        control_sunken = '2a2a2a',
        outline = '444444',
        outline_strong = '555555',
        popup_bg = '222222',
        overlay_bg = '111111',
        scrollbar_thumb = '666666',
        scrollbar_thumb_active = 'bbbbbb',
        selection = '3d59a1',
        accent = '7aa2f7',
        accent_hover = '92b3f8',
        accent_soft = '223055',
        on_accent = '101010',
        accent_on_video = '7aa2f7',
        scrim = '000000',
        chip_bg = '202020',
        chip_bg_hover = '3a3a3a',
        chip_fg = 'ffffff',
    },
    -- Kept as their own fields because so much code reads them: aliases into
    -- tok, refreshed whenever it is replaced.
    accent = '7aa2f7',
    accent_soft = '223055',
    -- Themed glow: a blurred accent halo behind bold titles and around the
    -- selected card. Off unless an app theme asks for it (mpvtk-theme), so
    -- the stock look is unchanged.
    glow = false,
    active = true,          -- false while yielded to playback (see mpvtk-active)
    snapped = false,        -- snapped_scrolling: one notch = one detent (mpvtk-wheel)
    -- force_scroll_snapping (mpvtk-wheel). An external mpv used to turn
    -- this on unconditionally — an image is a file mpv opens and mmaps
    -- there rather than an address in this process — but that cost is
    -- inside what rcost measures, so it is left to be measured.
    force_snap = false,
    -- What one render actually costs, in seconds: a smoothed measurement
    -- taken around render() in request_render. Everything the renderer does
    -- about being slow is derived from this and nothing else — how fast it
    -- schedules frames (active_tick) and, from that, whether scrolling
    -- quantizes to row boundaries (on_wheel).
    --
    -- What it captures is what costs: the layout pass, building the ASS
    -- string, and every overlay-add. A changed scroll offset re-issues every
    -- visible bitmap, up to MAX_OVERLAYS of them, and out of process mpv
    -- opens and mmaps a file for each — that is the expense this is here to
    -- notice, and all of it happens inside the timed region.
    --
    -- It does NOT capture libass laying the finished string out, which
    -- happens on the VO thread after osd:update() returns. Reviewers keep
    -- deriving an alarm from that, so: libass is far quicker than the
    -- overlay work, which is why a measurement blind to it still tracks the
    -- real cost, and why an external mpv turned out to quantize on its own
    -- once it was measured rather than assumed (see app.push_scroll_config).
    --
    -- It is also measured from whatever the renderer last drew, and a
    -- *scrolling* frame is dearer than a still one: at rest the overlay
    -- re-issues are all skipped by the argstr check in flush_overlays. So a
    -- gesture is judged on its own first frames rather than on an estimate
    -- from before it started, and a fling takes a frame or two to be
    -- recognized as one. That latency is the reason the release below is
    -- time-based rather than symmetric with the latch.
    rcost = 0,
    -- The share of the wall clock the renderer may spend drawing, which is
    -- what sets the cadence: frames are scheduled rcost/render_duty apart,
    -- so the rest of each slot is left for the script to take the input
    -- still arriving. That margin is the whole point — mpv drops events
    -- ("too many queued events") when a script's callbacks run back to
    -- back, and a wheel gesture is exactly when both are happening at once.
    --
    -- 0.6 leaves two thirds again as long as the frame itself. Nearer 1
    -- would be pacing in name only: rendering already spaces itself by its
    -- own duration, so a duty of 0.9 asks for barely a tenth more room than
    -- doing nothing would give.
    render_duty = 0.6,
    -- The cadence below which a glide is worse than a step, and so the
    -- point at which a scroll quantizes to row boundaries. Quantizing makes
    -- consecutive frames *identical*, which is what lets the overlay
    -- re-issues be skipped and leaves libass nothing new to lay out; it is
    -- the cheap way out of a frame budget, and it costs the smooth glide of
    -- ordinary scrolling.
    --
    -- So it is asked for only when the sustainable cadence (active_tick, ie
    -- what rcost affords) is slower than BOTH this and the rate the gesture
    -- is asking for. A machine holding 30fps glides, because a glide at
    -- 30fps looks better than a step at 30fps. One that has fallen to 20
    -- steps, because by then the glide is a series of lurches and the step
    -- at least lands somewhere deliberate.
    --
    -- 0.045 is where the field report came from: this was a flat 22fps
    -- ceiling on every wheel gesture, everywhere, and the reason it existed
    -- was a 4K screen that could not hold it.
    snap_pace = 0.045,
    -- Display quantization for the gesture in progress: latched by the cost
    -- test in on_wheel, released GESTURE_WINDOW after the last event.
    snap_live = false,
    wheel_prev = nil,       -- time of the previous wheel event (rate measure)
    snap_timer = nil,
    -- playback-HUD lifecycle (see mpvtk-hud): attached-but-idle during
    -- video, summoned by nav keys / mouse motion, auto-hides
    phud = { mode = false, shown = false, timer = nil, mx = -1, my = -1,
             -- auto-hide policy and the no-scrim text halo, pushed
             -- with the engage (mpvtk-hud) so setting changes stick
             hide_s = 4, hide_mode = 'hover', shadow = false,
             -- has the pointer MOVED since this summon? The bar-hover
             -- hold below is meaningless without it: see phud_busy
             moved = false },
    -- Scrub preview: the seek bar's trickplay/chapter bubble, drawn HERE
    -- rather than asked for from Python. A hover that has to round-trip,
    -- rebuild the whole HUD tree and come back as a scene is a preview that
    -- visibly trails the pointer, and it made every other control in the
    -- bar feel slow while it did (#618). Everything the bubble needs is
    -- already local: the tile file is raw BGRA that overlay-add consumes
    -- directly, and mpv owns the chapter list.
    tp = nil,               -- tiles: {file, iw, ih, count, mult} or {..., times}
    chlist = nil,           -- mpv's chapter-list, for the bubble's caption
    pv_hover = nil,         -- id of the preview slider under the pointer
    pv_secs = 0,            -- the position it is pointing at, in seconds
    pv_rect = nil,          -- last drawn bubble rect (nil = not shown)
    ready_sent = false,
    mouse = { x = -1, y = -1, hover = false },
    hover_id = nil,
    hover_region = nil,     -- id of the hovered node that asked to be told
    pressed = nil,          -- node id armed by mbtn down
    mods = {},              -- {shift, ctrl} of the current mouse press
    rpt = nil,              -- {id, timer} while a hold-repeat is armed
    nav = nil,              -- spatial-nav focused node id (10ft keys)
    nav_pidx = nil,         -- keyboard index inside an open popup/menu
    nav_adjust = nil,       -- slider value-adjust mode (ENTER toggles)
    focus_req = nil,        -- parked mpvtk-focus: {id=…} or {} for `af`
    drag = nil,             -- {sc=id, axis, start_m, start_off} scrollbar drag
    scroll = {},            -- id -> offset (px)
    snap_accum = {},        -- snaps container id -> fractional notch carry
    tb = {},                -- id -> {text, cursor, shift}
    dd = {},                -- id -> {sel}
    focus = nil,            -- focused textbox id
    dd_open = nil,          -- open dropdown id
    cursor_on = true,
    geo = {},               -- scroll id -> {dx, dy, x1,y1,x2,y2 (clip)}
    bars = {},              -- scroll id -> thumb geometry (for hit test)
    dd_geo = nil,           -- open popup geometry
    sl = {},                -- slider id -> {value}
    slider_drag = nil,      -- slider id being dragged
    tb_drag = nil,          -- {id, anchor} during click-drag selection
    tb_menu = nil,          -- {id, x, y} textbox context menu
    scale = 1,              -- UI scale (mpvtk-scale); see ui_px()
    tb_menu_geo = nil,
    modal = nil,            -- 'layer' meta node of the open Dialog
    modal_hidden = false,   -- dismissed locally, awaiting scene push
    busy_phase = 0,
    busy_timer = nil,
    ov_slots = {},          -- overlay key (node id + piece) -> slot
    ov_keys = {},           -- slot -> overlay key
    ov_last = {},           -- slot -> last issued args string
    ov_used = 0,
    tick_timer = nil,
    tick_last = 0,
    blink_timer = nil,
}

-- ---------------------------------------------------------------- utils

local function send(tbl)
    mp.commandv('script-message', 'mpvtk-event', utils.format_json(tbl))
end

-- ------------------------------------------------------------ clipboard
--
-- mpv's `clipboard/text` is the fast path, and on Windows and macOS the
-- only one needed. It is NOT universal on Linux: --clipboard-backends
-- defaults to win32,mac,wayland,vo and mpv only gained an *x11* backend
-- in 0.41, so on an X11 session under mpv 0.40 the property reads and
-- writes as "property unavailable". Copy silently did nothing and paste
-- inserted nothing, with no way for the user to tell why.
--
-- Note mp.set_property does not raise on failure, it returns nil + err --
-- so the original `pcall(mp.set_property, ...)` reported success either
-- way. Both results are checked below.
local function clip_tools()
    if state.clip_tools then return state.clip_tools end
    local t = {}
    local plat = mp.get_property('platform') or ''
    -- `set` is a shell command (see clip_write), `get` a plain argv.
    if plat == 'darwin' then
        -- Only reachable if the mac backend is missing; pbcopy/pbpaste
        -- are part of the OS, so there is nothing to install.
        t = { { set = 'pbcopy', get = { 'pbpaste' } } }
    elseif os.getenv('WAYLAND_DISPLAY') then
        -- Wayland first when the session is Wayland: it usually also
        -- answers xclip through XWayland, which is a different clipboard.
        t = { { pkg = 'wl-clipboard',
                set = 'wl-copy',
                get = { 'wl-paste', '--no-newline' } } }
    elseif os.getenv('DISPLAY') then
        t = { { pkg = 'xclip',
                set = 'xclip -selection clipboard',
                get = { 'xclip', '-selection', 'clipboard', '-o' } },
              { pkg = 'xsel',
                set = 'xsel --clipboard --input',
                get = { 'xsel', '--clipboard', '--output' } } }
    end
    -- Windows is deliberately absent: every mpv that runs there has the
    -- win32 backend, we ship mpv with the installer, and the shell
    -- redirect clip_write relies on assumes a POSIX sh.
    state.clip_tools = t
    return t
end

local function clip_call(t)
    local ok, r = pcall(mp.command_native, t)
    if not ok or type(r) ~= 'table' or r.status ~= 0 then return nil end
    return r.stdout or ''
end

local function clip_read(argv)
    return clip_call({ name = 'subprocess', playback_only = false,
                       args = argv,
                       capture_stdout = true, capture_stderr = true })
end

local function clip_write(cmd, text)
    -- The redirect goes inside a shell rather than relying on
    -- capture_stdout=false. xclip, xsel and wl-copy all fork a child that
    -- goes on owning the selection, and mpv makes pipes for the child
    -- whether or not we asked to capture them -- the forked copy inherits
    -- those and holds them until the clipboard is replaced, so mpv waits
    -- forever. Measured against mpv 0.40: an unwrapped copy never
    -- returns; `exec ... >/dev/null 2>&1` closes the pipes before the
    -- fork and returns in 0s. `detach` is not the answer either -- it
    -- leaves the child holding mpv's OWN stdout instead.
    --
    -- Nothing user-supplied is interpolated into the command; the text
    -- travels on stdin.
    return clip_call({ name = 'subprocess', playback_only = false,
                       args = { 'sh', '-c',
                                'exec ' .. cmd .. ' >/dev/null 2>&1' },
                       stdin_data = text,
                       capture_stdout = false, capture_stderr = false })
end

-- Tell the app once per session that there is no clipboard to be had, so
-- it can name the package to install. Once, not per keypress: a nag on
-- every failed paste would be worse than the silence it replaces.
local function clip_notify(op)
    if state.clip_warned then return end
    state.clip_warned = true
    local tools = clip_tools()
    send({ t = 'clipboard', op = op,
           need = tools[1] and tools[1].pkg or nil })
end

local function clip_set(text)
    local ok, res = pcall(mp.set_property, 'clipboard/text', text)
    if ok and res then return true end
    for _, tool in ipairs(clip_tools()) do
        if clip_write(tool.set, text) then return true end
    end
    return false
end

local function clip_get()
    local ok, v = pcall(mp.get_property, 'clipboard/text')
    if ok and v and v ~= '' then return v end
    for _, tool in ipairs(clip_tools()) do
        local out = clip_read(tool.get)
        if out and out ~= '' then return out end
    end
    return nil
end

-- Playback-HUD lifecycle (implemented with the mpvtk-hud handler at
-- the bottom; fwd-declared because the input handlers above it feed
-- its auto-hide timer and summon path). All are assigned before the
-- event loop can dispatch anything.
local phud_touch, phud_summon, phud_hide, phud_clear

-- Heuristic fallback — must match layout.py's _NARROW/_WIDE/_*_W
-- (enforced by tests/test_python_lua_constants.py). Replaced at
-- runtime by measured advances via the mpvtk-metrics message.
local NARROW = {}
for c in ("iIljtfr.,:;!|'`()[]\""):gmatch('.') do NARROW[c] = true end
local WIDE = {}
for c in ('mwMW@%&'):gmatch('.') do WIDE[c] = true end

local measured_widths = nil
local kern_table = nil
local ui_font = nil

local function char_w(c)
    if measured_widths then
        local w = measured_widths[c]
        if w then return w end
    end
    if #c > 1 then
        -- unmeasured multibyte: CJK/fullwidth glyphs are ~1em
        if c:byte(1) >= 0xE3 then return 1.0 end
        return 0.6
    end
    if c == ' ' then return 0.30 end
    if NARROW[c] then return 0.34 end
    if WIDE[c] then return 0.85 end
    return 0.54
end

local function kern_w(a, b)
    if not kern_table then return 0 end
    return kern_table[a .. b] or 0
end

-- Iterate UTF-8 codepoints (Lua 5.1 has no utf8 lib).
local U8PAT = '[\1-\127\194-\244][\128-\191]*'

local function u8_prev(s, i)
    -- previous codepoint boundary before byte offset i
    local j = i
    repeat
        j = j - 1
    until j <= 0 or s:byte(j + 1) < 0x80 or s:byte(j + 1) >= 0xC0
    return math.max(0, j)
end

local function u8_next(s, i)
    -- next codepoint boundary after byte offset i
    local n = #s
    local j = i + 1
    while j < n and s:byte(j + 1) >= 0x80 and s:byte(j + 1) < 0xC0 do
        j = j + 1
    end
    return math.min(n, j)
end

local function u8_count(s)
    local n = 0
    for _ in s:gmatch(U8PAT) do n = n + 1 end
    return n
end

-- How much wider the bold face is than the regular one. Mirrors
-- layout.py's BOLD_FACTOR (measured, not guessed -- see the comment
-- there); tests/test_python_lua_constants.py pins the two together.
local BOLD_FACTOR = 1.12

local function text_w(s, size, bold)
    local w = 0
    local prev = nil
    for c in s:gmatch(U8PAT) do
        if prev then w = w + kern_w(prev, c) end
        w = w + char_w(c)
        prev = c
    end
    w = w * size
    if bold then w = w * BOLD_FACTOR end
    return w
end

-- Mirror of layout.py's ellipsize (same metrics, same kern handling)
-- for text the renderer owns: dropdown labels and popup/menu items,
-- whose available width only the widget knows exactly.
local function ellipsize(s, size, bold, max_w)
    max_w = max_w + 0.5  -- float slop, mirror of layout.py's
    if text_w(s, size, bold) <= max_w then return s end
    local ell = text_w('…', size, bold)
    local out = {}
    local w = 0
    local prev = nil
    local bf = bold and BOLD_FACTOR or 1
    for c in s:gmatch(U8PAT) do
        local cw = char_w(c) * size * bf
        if prev then cw = cw + kern_w(prev, c) * size * bf end
        if w + cw + ell > max_w then break end
        out[#out + 1] = c
        w = w + cw
        prev = c
    end
    return table.concat(out) .. '…'
end

local function ass_color(hex)
    -- "rrggbb" -> "&Hbbggrr&"
    return string.format('&H%s%s%s&',
        hex:sub(5, 6), hex:sub(3, 4), hex:sub(1, 2))
end

local function ass_alpha(a)  -- 0-255 opacity -> ASS alpha
    return string.format('&H%02X&', 255 - (a or 255))
end

local function esc(s)
    return (s:gsub('\\', '\\\239\187\191'):gsub('{', '\\{'):gsub('\n', ' '))
end

local function clamp(v, lo, hi)
    if v < lo then return lo end
    if hi < lo then return lo end
    if v > hi then return hi end
    return v
end

-- ------------------------------------------------------------ rendering

local render  -- fwd

-- The minimum spacing between renders right now, start to start.
--
-- Derived from what a frame measurably costs rather than from what is
-- happening: scrolling does not slow the cadence down, being slow does.
-- This used to drop to a flat 22fps for the duration of any wheel gesture,
-- which was the right treatment aimed at the wrong patient — a fling on a
-- machine that draws a frame in 3ms was never the problem, and it paid for
-- the mitigation anyway.
local function active_tick()
    local pace = state.rcost / state.render_duty
    if pace < TICK then return TICK end
    if pace > RENDER_PACE_MAX then return RENDER_PACE_MAX end
    return pace
end

local function request_render()
    if state.tick_timer == nil then
        state.tick_timer = mp.add_timeout(0, function()
            local t0 = mp.get_time()
            state.tick_last = t0
            render()
            -- Smoothed so one dear frame — the first after a scene push
            -- issues every overlay — does not pace the next second, and so
            -- the estimate still follows a page that genuinely got heavier
            -- within a few frames. Both directions matter: this decides
            -- when scrolling stops gliding, so it has to let go as readily
            -- as it latches.
            state.rcost = state.rcost * 0.6 + (mp.get_time() - t0) * 0.4
        end)
    end
    if not state.tick_timer:is_enabled() then
        local to = active_tick() - (mp.get_time() - state.tick_last)
        if to < 0 then to = 0 end
        state.tick_timer.timeout = to
        state.tick_timer:resume()
    end
end

local function scroll_max(node)
    if node.axis == 'x' then
        return math.max(0, node.cw - node.w)
    end
    return math.max(0, node.ch - node.h)
end

-- Nearest breakpoint index in an explicit snaps list (home sections).
local function snap_index(snaps, off)
    local idx, bestd = 1, math.huge
    for i, s in ipairs(snaps) do
        local d = math.abs(off - s)
        if d < bestd then bestd = d; idx = i end
    end
    return idx
end

-- Row-quantized DISPLAY offset for a snapping container (node.snap set by
-- the grid; see widgets.Scroll). The stored offset in state.scroll stays
-- continuous — this only maps it to the nearest row boundary for drawing and
-- hit-testing (compute_geo/draw_scrollbar), so the picture and clicks agree
-- while the accumulator underneath is free to move by fractional notches.
-- Snap stops are {0} ∪ {snap_off + k*snap} ∪ {max}: the top rests flush so
-- the header stays reachable, and the bottom rests flush so the last partial
-- row is never left with a gap beneath it.
--
-- node.snaps (an explicit list of breakpoints, e.g. home section tops) takes
-- precedence: the offset snaps to the nearest listed stop, clamped to the
-- scrollable range.
local function snap_round(node, off)
    -- Whether the display quantizes at all right now. Three ways in, and
    -- the middle one is the reason this test exists:
    --   * force_snap — the user's opt-in;
    --   * snap_live — this gesture is outrunning the renderer (see
    --     on_wheel);
    --   * snapped — snapped_scrolling, where the stored offset already sits
    --     on a detent so rounding is a no-op, but gating it off would be a
    --     lie about what that mode does.
    -- Everything else — which is most scrolling, on most setups — glides.
    if not (state.force_snap or state.snap_live or state.snapped) then
        return off
    end
    if node.snaps then
        local max = scroll_max(node)
        local best, bestd = clamp(off, 0, max), math.huge
        for _, s in ipairs(node.snaps) do
            local sv = clamp(s, 0, max)
            local d = math.abs(off - sv)
            if d < bestd then bestd = d; best = sv end
        end
        return best
    end
    local snap = node.snap
    if not snap or snap <= 0 then return off end
    local max = scroll_max(node)
    local base = node.snap_off or 0
    if off <= base * 0.5 then return 0 end
    if off >= max - snap * 0.5 then return max end
    local k = math.floor((off - base) / snap + 0.5)
    if k < 0 then k = 0 end
    local snapped = base + k * snap
    if snapped < 0 then return 0 end
    if snapped > max then return max end
    return snapped
end

-- Effective geometry of every scroll container. Scene order guarantees
-- ancestors appear before descendants.
local function compute_geo()
    state.geo = {}
    for _, node in ipairs(state.nodes) do
        if node.t == 'scroll' then
            local p = node.sc and state.geo[node.sc]
            local dx = p and p.dx or 0
            local dy = p and p.dy or 0
            local vx1, vy1 = node.x - dx, node.y - dy
            local vx2, vy2 = vx1 + node.w, vy1 + node.h
            if p then
                vx1 = math.max(vx1, p.x1); vy1 = math.max(vy1, p.y1)
                vx2 = math.min(vx2, p.x2); vy2 = math.min(vy2, p.y2)
            end
            local off = snap_round(node, state.scroll[node.id] or 0)
            local g = {
                dx = dx + (node.axis == 'x' and off or 0),
                dy = dy + (node.axis == 'y' and off or 0),
                x1 = vx1, y1 = vy1, x2 = vx2, y2 = vy2,
            }
            -- The OUTERMOST viewport this one sits in (itself, when there is
            -- no parent). Decorations that are allowed to spill out of their
            -- own container still may not leave the page -- see glow_clip.
            g.root = p and p.root or g
            state.geo[node.id] = g
        end
    end
end

-- Returns ex, ey, clip {x1,y1,x2,y2} (nil clip = whole osd)
local function eff(node)
    local g = node.sc and state.geo[node.sc]
    if not g then
        return node.x, node.y, nil
    end
    return node.x - g.dx, node.y - g.dy, g
end

local function visible(node)
    local ex, ey, clip = eff(node)
    if not clip then
        return ex < state.w and ey < state.h and
            ex + node.w > 0 and ey + node.h > 0
    end
    return ex < clip.x2 and ey < clip.y2 and
        ex + node.w > clip.x1 and ey + node.h > clip.y1
end

-- Clip for a decoration that is allowed to spill OUT of its own viewport,
-- but not out of the page: the container's rect grown by `spill`, clamped
-- back to the outermost scroll ancestor.
--
-- Both of the themed glows want this. Clipped to their own container the
-- blur is chopped into a hard edge, which is the box they exist to replace
-- (a card fills its row's viewport, so the halo is entirely outside it).
-- Clipped to nothing -- which is what they used to do -- the halo of a card
-- near the top of the page paints over the header, because chrome is drawn
-- BEFORE content and ASS has no z-order beyond scene order.
--
-- Growing the already-intersected rect and re-clamping gives the same
-- answer as growing the container's own rect would: where the parent had
-- already cut this viewport, the clamp puts the edge straight back.
local function spill_clip(clip, spill)
    if not clip then return nil end
    local r = clip.root or clip
    return {
        x1 = math.max(clip.x1 - spill, r.x1),
        y1 = math.max(clip.y1 - spill, r.y1),
        x2 = math.min(clip.x2 + spill, r.x2),
        y2 = math.min(clip.y2 + spill, r.y2),
    }
end

local function clip_tag(clip)
    if not clip then return '' end
    return string.format('\\clip(%.1f,%.1f,%.1f,%.1f)',
        clip.x1, clip.y1, clip.x2, clip.y2)
end

local function hover_style(node)
    if state.nav == node.id then
        -- key focus owns the node's visual state: the focus ring
        -- replaces any hover styling so the two never fight
        return nil
    end
    if node.hover and state.hover_id == node.id then
        return node.hover
    end
    return nil
end
local function draw_rect(ass, x, y, w, h, o)
    -- o: {fill, a, radius, bc, bw, clip, blur}
    ass:new_event()
    ass:append('{\\pos(0,0)\\an7\\bord0\\shad0')
    -- blur softens the border into a halo (the themed selection glow); pass
    -- such a caller's clip through spill_clip, or the container's viewport
    -- chops the blur back into a hard edge.
    if o.blur then ass:append(string.format('\\blur%.1f', o.blur)) end
    if o.fill then
        ass:append('\\1c' .. ass_color(o.fill))
        ass:append('\\1a' .. ass_alpha(o.a))
    else
        ass:append('\\1a&HFF&')
    end
    if o.bc then
        ass:append(string.format('\\bord%.1f', o.bw or 1))
        ass:append('\\3c' .. ass_color(o.bc) .. '\\3a&H00&')
    end
    if o.clip then ass:append(clip_tag(o.clip)) end
    ass:append('}')
    ass:draw_start()
    if o.radius and o.radius > 0 then
        ass:round_rect_cw(x, y, x + w, y + h, o.radius)
    else
        ass:rect_cw(x, y, x + w, y + h)
    end
    ass:draw_stop()
end

local ALIGN_AN = { left = 4, center = 5, right = 6 }

-- raw=true skips escaping: the caller pre-escaped segments and mixed
-- in override tags (inline caret drawing).
local function draw_text(ass, node, ex, ey, clip, text, color, extra,
                         raw)
    local an = ALIGN_AN[node.align or 'left'] or 4
    local px = ex
    if an == 5 then px = ex + node.w / 2 end
    if an == 6 then px = ex + node.w end
    ass:new_event()
    -- \q2: WrapStyle "no word wrapping, \N only". The layout engine has
    -- already broken this text into lines; without \q2 libass applies its
    -- own smart wrapping on top, re-flowing any line it considers too wide
    -- for the play area. Two wrappers disagreeing by a fraction of a pixel
    -- is what made long text jump to an extra line seemingly at random —
    -- our break was never authoritative. With \q2 a slightly-too-long line
    -- simply extends (and is clipped) instead of reflowing.
    -- A themed glow (\bord + \blur in the accent) behind headings only:
    -- bold and reasonably large, so body text and captions are untouched.
    -- Drawn as part of the same event, so it cannot drift from the glyphs.
    local glow = ''
    if state.glow and node.bold and (node.size or 0) >= 22 then
        glow = string.format('\\bord2\\blur6\\3c%s\\3a&H30&',
                             ass_color(state.accent))
    elseif state.phud.shown and state.phud.shadow then
        -- The scrim is what normally makes HUD text legible over any
        -- frame. With hud_scrim "none" there is nothing behind it, so the
        -- glyphs carry their own dark halo instead. Scoped to a summoned
        -- HUD, which is the only thing on screen while it is up -- no node
        -- needs to declare anything.
        glow = '\\bord1.4\\blur2\\3c&H000000&\\3a&H40&'
    end
    ass:append(string.format(
        -- %.2f, not %d: a font size arrives already scaled and is NOT rounded
        -- (mpvtk/scaling.py _EXACT_KEYS). Truncating it here would put the
        -- rounding error straight back, and it is the whole reason a wrapped
        -- paragraph used to overrun its column on a fractional UI scale.
        '{\\q2\\an%d\\pos(%.1f,%.1f)\\fs%.2f\\bord0\\shad0' ..
        '\\1c%s\\1a&H00&%s%s%s%s%s}',
        an, px, ey + node.h / 2, node.size,
        ass_color(color), glow, node.bold and '\\b1' or '',
        ui_font and ('\\fn' .. ui_font) or '',
        clip_tag(clip), extra or ''))
    ass:append(raw and text or esc(text))
end

-- Material icon as an ASS drawing: `path` is on the 24x24 unit canvas
-- with corner anchors (see mpvtk/vector.py), scaled via \fscx/\fscy —
-- the same convention as the jellyfin OSC.
local function draw_icon_path(ass, path, x, y, px, color, clip)
    local scale = px / 24 * 100
    ass:new_event()
    ass:append(string.format(
        '{\\pos(%.1f,%.1f)\\an7%s\\shad0\\1c%s\\1a&H00&' ..
        '\\fscx%.2f\\fscy%.2f%s\\p1}',
        x, y,
        -- see draw_text: with no scrim the glyph carries its own halo
        (state.phud.shown and state.phud.shadow)
            and '\\bord1.4\\blur2\\3c&H000000&\\3a&H40&' or '\\bord0',
        ass_color(color), scale, scale, clip_tag(clip)))
    ass:append(path)
    ass:append('{\\p0}')
end

local overlay_list  -- rebuilt per render: paint-ordered {key, args, rect}
local occluders     -- rects (popup/menu/layer) above ALL images
local flow_occs     -- Stack occlude markers: {i=scene idx, x1..y2};
                    -- subtracted only from images EARLIER in paint order

-- Subtract rect o from rect r; appends up to 4 remainder rects to out.
local function subtract_rect(r, o, out)
    local ox1 = math.max(r.x1, o.x1)
    local oy1 = math.max(r.y1, o.y1)
    local ox2 = math.min(r.x2, o.x2)
    local oy2 = math.min(r.y2, o.y2)
    if ox1 >= ox2 or oy1 >= oy2 then
        out[#out + 1] = r
        return
    end
    if r.y1 < oy1 then
        out[#out + 1] = { x1 = r.x1, y1 = r.y1, x2 = r.x2, y2 = oy1 }
    end
    if oy2 < r.y2 then
        out[#out + 1] = { x1 = r.x1, y1 = oy2, x2 = r.x2, y2 = r.y2 }
    end
    if r.x1 < ox1 then
        out[#out + 1] = { x1 = r.x1, y1 = oy1, x2 = ox1, y2 = oy2 }
    end
    if ox2 < r.x2 then
        out[#out + 1] = { x1 = ox2, y1 = oy1, x2 = r.x2, y2 = oy2 }
    end
end

local function draw_image(node, ex, ey, clip, idx)
    -- Crop the source so only the part inside the clip is shown.
    -- CRITICAL: never let the crop exceed the source pixel size (iw/ih)
    -- — mpv mmaps the file and reading past EOF is a SIGBUS crash.
    clip = clip or { x1 = 0, y1 = 0, x2 = state.w, y2 = state.h }
    local x1 = math.max(ex, clip.x1)
    local y1 = math.max(ey, clip.y1)
    local x2 = math.min(ex + math.min(node.w, node.iw), clip.x2)
    local y2 = math.min(ey + math.min(node.h, node.ih), clip.y2)
    if x2 - x1 < 1 or y2 - y1 < 1 then return end
    local pieces = { { x1 = x1, y1 = y1, x2 = x2, y2 = y2 } }
    for _, occ in ipairs(occluders) do
        local next_pieces = {}
        for _, p in ipairs(pieces) do
            subtract_rect(p, occ, next_pieces)
        end
        pieces = next_pieces
    end
    -- Stack occluders only punch images painted BELOW them; images
    -- later in paint order sit above the ASS anyway (higher slot).
    for _, occ in ipairs(flow_occs) do
        if idx and occ.i > idx then
            local next_pieces = {}
            for _, p in ipairs(pieces) do
                subtract_rect(p, occ, next_pieces)
            end
            pieces = next_pieces
        end
    end
    local stride = node.iw * 4
    local pidx = 0
    for _, p in ipairs(pieces) do
        local px1, py1 = math.floor(p.x1), math.floor(p.y1)
        local px2, py2 = math.floor(p.x2), math.floor(p.y2)
        if px2 - px1 >= 1 and py2 - py1 >= 1 then
            local sx = px1 - math.floor(ex)
            local sy = py1 - math.floor(ey)
            if #overlay_list >= MAX_OVERLAYS then
                msg.warn('overlay budget exceeded; image dropped: ' ..
                    node.id)
                return
            end
            pidx = pidx + 1
            local src = node.src
            -- node.base: a byte offset INTO the source, for a file that
            -- holds several frames back to back rather than one image.
            -- The trickplay tile file is the only such source (frame n at
            -- n * w * h * 4); the crop maths above is unchanged, it just
            -- starts from there instead of from zero.
            local offset = sy * stride + sx * 4 + (node.base or 0)
            if src:sub(1, 1) == '&' then
                -- same-process memory source (libmpv backend): fold
                -- the crop offset into the address so overlay-add's
                -- offset semantics for '&' never matter
                src = string.format('&%.0f',
                    tonumber(src:sub(2)) + offset)
                offset = 0
            end
            overlay_list[#overlay_list + 1] = {
                key = node.id .. '#' .. pidx,
                v = node.v,
                x1 = px1, y1 = py1, x2 = px2, y2 = py2,
                args = {
                    tostring(px1), tostring(py1), src,
                    tostring(offset), 'bgra',
                    tostring(px2 - px1), tostring(py2 - py1),
                    tostring(stride),
                },
            }
        end
    end
end

-- Slots are STICKY per overlay key: an unchanged image is never
-- re-issued and never changes slot, so scene pushes (e.g. infinite
-- scroll materializing new rows) only touch genuinely new or departed
-- overlays instead of churning every slot — that churn was visible as
-- flicker of already-displayed content.
--
-- Z-ORDER: mpv composites overlay slots in ascending id order, so slot
-- order IS bitmap stacking order. Sticky assignment ignores that, which
-- is fine while bitmaps never overlap (grids/strips). When two
-- OVERLAPPING overlays' slot order contradicts paint order (a Stack
-- floating a bitmap over a strip), the whole set is renumbered to paint
-- order once and stickiness resumes from there. Adds are still issued
-- before removes (remove-before-add showed as a one-frame hole).
local function rects_overlap(a, b)
    return a.x1 < b.x2 and b.x1 < a.x2 and a.y1 < b.y2 and b.y1 < a.y2
end

local function ov_argstr(ov)
    -- v busts the cache when a file was rewritten in place
    return table.concat(ov.args, '\0') .. '\0' .. (ov.v or 0)
end

local function renumber_overlays()
    -- Re-issue every overlay at slot = paint index. overlay-adds are
    -- idempotent per slot (skipped when args match) and happen before
    -- any remove, so already-correct content never blinks.
    local ns, nk, nl = {}, {}, {}
    for i, ov in ipairs(overlay_list) do
        local slot = i - 1
        local argstr = ov_argstr(ov)
        if state.ov_last[slot] ~= argstr then
            mp.commandv('overlay-add', tostring(slot), unpack(ov.args))
        end
        ns[ov.key] = slot
        nk[slot] = ov.key
        nl[slot] = argstr
    end
    for slot = 0, MAX_OVERLAYS - 1 do
        if state.ov_keys[slot] and not nk[slot] then
            mp.commandv('overlay-remove', tostring(slot))
        end
    end
    state.ov_slots, state.ov_keys, state.ov_last = ns, nk, nl
end

local function flush_overlays()
    local wanted = {}
    for _, ov in ipairs(overlay_list) do
        wanted[ov.key] = true
    end
    -- Departing slots are NOT removed up front: a new image can take
    -- one over with a direct overlay-add (an atomic per-slot swap),
    -- and removes happen last — remove-before-add left a one-frame
    -- hole that showed as tile flicker while scrolling.
    local freeable = {}
    for slot = 0, MAX_OVERLAYS - 1 do
        local k = state.ov_keys[slot]
        if k and not wanted[k] then
            freeable[#freeable + 1] = slot
        end
    end
    -- phase 1: assign slots (sticky; new keys take free slots).
    --
    -- Sticky slots go on first, so a new overlay can see where everything
    -- staying already sits and land on the right side of it. Slot order IS
    -- stacking order, so a bitmap floated over another — the hover play
    -- chip over its row's artwork — has to take a HIGHER slot than the one
    -- it covers, and the lowest free slot is very often a lower one: any
    -- row that scrolled off left its slot behind. Taking it contradicts
    -- paint order, which sends phase 2 to renumber_overlays, which re-issues
    -- every visible bitmap to a new slot. That is what a hover chip
    -- appearing used to cost, and it is visible as the whole page flickering
    -- for something the size of a coin.
    for _, ov in ipairs(overlay_list) do
        ov.slot = state.ov_slots[ov.key]
    end
    local next_free = 1
    for i, ov in ipairs(overlay_list) do
        local slot = ov.slot
        if slot == nil then
            -- The window paint order allows: above everything already
            -- placed that this covers and is painted under, below anything
            -- it covers and is painted over.
            local lo, hi = -1, MAX_OVERLAYS
            for j, b in ipairs(overlay_list) do
                if b.slot and j ~= i and rects_overlap(ov, b) then
                    if j < i then
                        if b.slot > lo then lo = b.slot end
                    elseif b.slot < hi then
                        hi = b.slot
                    end
                end
            end
            for s = lo + 1, hi - 1 do
                if state.ov_keys[s] == nil then
                    slot = s
                    break
                end
            end
            if slot == nil and (lo >= 0 or hi < MAX_OVERLAYS) then
                -- Nothing free where this belongs. Take any slot and let
                -- phase 2 sort the stacking out the expensive way; a
                -- correct picture beats a cheap one.
                for s = 0, MAX_OVERLAYS - 1 do
                    if state.ov_keys[s] == nil then
                        slot = s
                        break
                    end
                end
            end
            if slot == nil and next_free <= #freeable then
                slot = freeable[next_free]
                next_free = next_free + 1
                state.ov_slots[state.ov_keys[slot]] = nil
                state.ov_last[slot] = nil
            end
            if slot == nil then
                msg.warn('no free overlay slot for ' .. ov.key)
            else
                state.ov_slots[ov.key] = slot
                state.ov_keys[slot] = ov.key
            end
        end
        ov.slot = slot
    end
    -- phase 2: does any overlapping pair stack in the wrong order?
    local bad = false
    for i = 1, #overlay_list do
        local a = overlay_list[i]
        if a.slot then
            for j = i + 1, #overlay_list do
                local b = overlay_list[j]
                if b.slot and b.slot < a.slot and
                    rects_overlap(a, b) then
                    bad = true
                    break
                end
            end
        end
        if bad then break end
    end
    if bad then
        renumber_overlays()
        state.ov_used = #overlay_list
        return
    end
    -- phase 3: issue changed overlays, then free departed slots
    for _, ov in ipairs(overlay_list) do
        if ov.slot ~= nil then
            local argstr = ov_argstr(ov)
            if state.ov_last[ov.slot] ~= argstr then
                state.ov_last[ov.slot] = argstr
                mp.commandv('overlay-add', tostring(ov.slot),
                    unpack(ov.args))
            end
        end
    end
    for i = next_free, #freeable do
        local slot = freeable[i]
        local k = state.ov_keys[slot]
        if k then state.ov_slots[k] = nil end
        state.ov_keys[slot] = nil
        state.ov_last[slot] = nil
        mp.commandv('overlay-remove', tostring(slot))
    end
    state.ov_used = #overlay_list
end

-- Display width of the first `upto` chars as rendered (bullets when
-- masked — a fixed advance keeps cursor math trivial).
local MASK_W = 0.55

-- Caret/selection boundary after byte offset `upto` (a codepoint
-- boundary): the pen position where the NEXT glyph starts, i.e.
-- including the kern into it — where libass draws its origin.
local function tb_text_w(node, text, upto)
    if node.mask then
        return u8_count(text:sub(1, upto)) * MASK_W * node.size
    end
    local pen = 0
    local prev = nil
    local pos = 0
    for c in text:gmatch(U8PAT) do
        if pos >= upto then
            if prev then pen = pen + kern_w(prev, c) end
            break
        end
        if prev then pen = pen + kern_w(prev, c) end
        pen = pen + char_w(c)
        pos = pos + #c
        prev = c
    end
    return pen * node.size
end

-- Codepoint boundary (byte offset) nearest to screen x.
local function tb_index_at(node, tb, x)
    local pad = 10
    local ex = select(1, eff(node))
    local rel = x - ex - pad + tb.shift
    local pen = 0
    local prev = nil
    local pos = 0
    for c in tb.text:gmatch(U8PAT) do
        local cw
        if node.mask then
            cw = MASK_W * node.size
        else
            cw = ((prev and kern_w(prev, c) or 0) + char_w(c)) *
                node.size
        end
        if pen + cw / 2 > rel then return pos end
        pen = pen + cw
        pos = pos + #c
        prev = c
    end
    return pos
end

local function tb_menu_items(node)
    -- masked boxes must not leak their contents to the clipboard
    if node.mask then return { 'Paste', 'Select All' } end
    return { 'Cut', 'Copy', 'Paste', 'Select All' }
end

local function ui_px(v)
    -- Logical -> physical for chrome that is composed HERE and so never
    -- passes through Python's scale_scene: the textbox context menu and
    -- the hover tooltip. Same rounding as scaling.px().
    return math.floor(v * (state.scale or 1) + 0.5)
end


local function tb_menu_geometry(items)
    local n = #items
    local ih = ui_px(34)
    local w = ui_px(40)
    local fs = ui_px(18)
    for _, item in ipairs(items) do
        w = math.max(w, text_w(item, fs) + ui_px(32))
    end
    local x = clamp(state.tb_menu.x, 0, math.max(0, state.w - w - 4))
    local y = state.tb_menu.y
    if y + n * ih > state.h and y - n * ih >= 0 then
        y = y - n * ih
    end
    y = clamp(y, 0, math.max(0, state.h - n * ih))
    return { x = x, y = y, w = w, ih = ih, n = n, fs = fs }
end

local function draw_textbox(ass, node, ex, ey, clip)
    local tb = state.tb[node.id]
    local focused = state.focus == node.id
    draw_rect(ass, ex, ey, node.w, node.h, {
        fill = state.tok.control_sunken, radius = 6,
        bc = focused and state.accent or state.tok.outline,
        bw = focused and 2 or 1, clip = clip,
    })
    -- see draw_dropdown: shape kept, contrast dropped
    local ink = node.dis and state.tok.on_surface_faint
        or state.tok.on_surface
    local pad = 10
    local inner = {
        x1 = math.max(ex + pad, clip and clip.x1 or 0),
        y1 = clip and math.max(ey, clip.y1) or ey,
        x2 = math.min(ex + node.w - pad, clip and clip.x2 or state.w),
        y2 = clip and math.min(ey + node.h, clip.y2) or ey + node.h,
    }
    local tnode = {
        w = node.w - 2 * pad, h = node.h,
        size = node.size, align = 'left',
    }
    local text = tb and tb.text or node.text or ''
    if text == '' and not focused and (node.ph or '') ~= '' then
        draw_text(ass, tnode, ex + pad, ey, inner, node.ph, state.tok.on_surface_faint)
        return
    end
    local shift = tb and tb.shift or 0
    local x0 = ex + pad - shift
    if tb and tb.sel and tb.sel ~= tb.cursor then
        local a = math.min(tb.sel, tb.cursor)
        local b = math.max(tb.sel, tb.cursor)
        local sx1 = x0 + tb_text_w(node, text, a)
        local sx2 = x0 + tb_text_w(node, text, b)
        draw_rect(ass, sx1, ey + node.h * 0.14,
            sx2 - sx1, node.h * 0.72,
            { fill = state.tok.selection, a = 200, clip = inner })
    end
    local disp = node.mask and string.rep('•', u8_count(text)) or text
    if focused then
        -- The caret is an INLINE zero-width ASS drawing spliced into
        -- the text at the cursor: libass places it at the exact pen
        -- position between the surrounding glyphs (kerning, shaping
        -- and fallback fonts included) — our width math is only
        -- needed for click mapping, not caret display. Zero width
        -- (a vertical line path) means no advance; the visible bar is
        -- the \bord outline around it.
        local cur = tb and tb.cursor or #text
        local dsplit
        if node.mask then
            dsplit = u8_count(text:sub(1, cur)) * 3  -- '•' is 3 bytes
        else
            dsplit = cur
        end
        -- inline drawing y origin is the line's ASCENT TOP (positive
        -- down), not the baseline — negative coords render above the
        -- glyphs. The drawing is ALWAYS spliced while focused and the
        -- blink toggles only its border alpha: removing it changed
        -- the line's bounding box, and \an4 re-centering bobbed the
        -- text ~1px with every blink.
        local caret = string.format(
            '{\\p1\\bord0.8\\3c&HEEEEEE&\\3a%s\\1a&HFF&}' ..
            'm 0 %d l 0 %d' ..
            '{\\p0\\bord0\\1a&H00&\\3a&H00&\\fsp0}',
            state.cursor_on and '&H00&' or '&HFF&',
            math.floor(node.size * 0.06),
            math.floor(node.size * 0.92))
        local a = disp:sub(1, dsplit)
        local b = disp:sub(dsplit + 1)
        local pre = esc(a)
        if not node.mask and #a > 0 and #b > 0 then
            -- splitting the text at the caret breaks the shaping run,
            -- dropping the kern between the surrounding pair (the
            -- suffix shifted while the caret sat inside e.g. "Ta").
            -- Restore it as negative letter-spacing on the prefix's
            -- last character — same measured amount, so nothing moves.
            local pc = a:match(U8PAT .. '$')
            local nc = b:match('^' .. U8PAT)
            if pc and nc then
                local k = kern_w(pc, nc)
                if k ~= 0 then
                    pre = esc(a:sub(1, #a - #pc)) ..
                        string.format('{\\fsp%.2f}', k * node.size) ..
                        esc(pc)
                end
            end
        end
        draw_text(ass, tnode, x0, ey, inner,
            pre .. caret .. esc(b),
            ink, nil, true)
    else
        draw_text(ass, tnode, x0, ey, inner, disp, ink)
    end
end

local function dd_state(node)
    local d = state.dd[node.id]
    if d == nil or node.force then
        d = { sel = node.sel or 0 }
        state.dd[node.id] = d
    end
    return d
end

-- Vertical fade. Alpha-stepped bands show visible banding (the lua
-- OSC's jellyfin layout hit exactly this), so instead draw ONE solid
-- translucent box with a heavily blurred fading edge: the gaussian
-- edge is a continuous alpha ramp. The box extends well past the node
-- on the other three sides and is clipped to the node rect, so only
-- the fade edge shows. ASS (not a bitmap) so ordinary ASS content
-- still draws on top of it. Constants match the OSC's
-- add_jf_gradient, which is field-proven not to band.
-- Multi-stop COLOUR ramp. Each stop after the first is drawn as one
-- blurred-edge layer over everything before it: alpha-compositing a 0->255
-- ramp of B over solid A gives exactly A*(1-t) + B*t, which is a linear
-- interpolation. So an n-stop gradient is n ASS events, not n bands -- and
-- bands are what this file already avoids for the scrim, for the same reason
-- (the old lua OSC's banded gradient was visibly stepped).
--
-- The ramp's shape is a gaussian rather than a true line, so the midpoint of
-- each segment lands where it should and the quarter points bow slightly.
-- Over a themed background that reads as a soft ease, not as an error.
local function draw_gradient(ass, node, ex, ey, clip)
    local a1, a2 = node.a1 or 0, node.a2 or 0
    local w, h = node.w, node.h
    if h <= 0 or w <= 0 then return end
    -- confine everything to the node rect (∩ the scroll clip)
    local c = { x1 = ex, y1 = ey, x2 = ex + w, y2 = ey + h }
    if clip then
        c.x1 = math.max(c.x1, clip.x1)
        c.y1 = math.max(c.y1, clip.y1)
        c.x2 = math.min(c.x2, clip.x2)
        c.y2 = math.min(c.y2, clip.y2)
    end
    if c.x2 <= c.x1 or c.y2 <= c.y1 then return end
    if node.stops then
        -- Multi-stop COLOUR ramp (themed backgrounds and top bars), rather
        -- than an alpha fade of one colour. Each stop after the first is one
        -- blurred-edge layer over everything before it: compositing a 0->255
        -- ramp of B over solid A gives exactly A*(1-t) + B*t, a linear
        -- interpolation. So an n-stop gradient is n ASS events, not n bands
        -- -- and bands are what this file already avoids for the scrim,
        -- because the old lua OSC's banded gradient was visibly stepped.
        --
        -- Deliberately inline rather than its own `local function`: this
        -- chunk is at LuaJIT's 200-locals-per-function ceiling, and one more
        -- top-level local fails to compile at load with nothing but
        -- "main function has more than 200 local variables".
        local horiz = node.axis == 'x'
        local span = horiz and w or h
        if span <= 0 or #node.stops == 0 then return end
        draw_rect(ass, ex, ey, w, h, { fill = node.stops[1][2], clip = c })
        -- Exactly ONE layer per stop segment. Splitting a segment into
        -- several finer ramps hugs a true lerp more closely -- and looks
        -- worse. Every gaussian starts and ends at ~zero slope, so each
        -- extra layer adds a junction where the brightness briefly stops
        -- changing, and the eye reads those flat spots as bands: two layers
        -- put a visible plateau mid-gradient, six read as stacked strips.
        -- Measured in tools/gradient_fidelity.py, which reports the slope
        -- profile precisely so this stays hard to get wrong twice.
        --
        -- The cost of one layer is that the ramp eases rather than running
        -- straight, which over a background is the nicer of the two errors.
        for i = 2, #node.stops do
            local p0 = node.stops[i - 1][1] * span
            local p1 = node.stops[i][1] * span
            local seg = p1 - p0
            if seg > 0.5 then
                -- A gaussian edge reaches ~full about 2*blur out, so a blur
                -- of seg/4 spans exactly this segment with the box boundary
                -- at its midpoint.
                local blur = seg / 4
                local over = blur * 4 + span
                local mid = (p0 + p1) / 2
                ass:new_event()
                ass:append(string.format(
                    '{\\pos(0,0)\\an7\\bord0\\shad0\\blur%.1f\\1c%s' ..
                    '\\1a&H00&%s}',
                    blur, ass_color(node.stops[i][2]), clip_tag(c)))
                ass:draw_start()
                if horiz then
                    ass:rect_cw(ex + mid, ey - over,
                                ex + w + over, ey + h + over)
                else
                    ass:rect_cw(ex - over, ey + mid,
                                ex + w + over, ey + h + over)
                end
                ass:draw_stop()
            end
        end
        return
    end
    local lo = math.min(a1, a2)
    if lo > 0 then
        -- uniform base layer; the blurred box adds the delta on top
        -- (compositing two translucent layers slightly under-shoots
        -- the dense end's target opacity — fine for a scrim)
        draw_rect(ass, ex, ey, w, h,
            { fill = node.c or '000000', a = lo, clip = c })
    end
    local hi = math.max(a1, a2)
    if hi <= lo then return end
    -- gaussian edge reaches ~full/zero alpha about 2*blur from the
    -- box edge; place the edge so the ramp spans the node height,
    -- solid at the dense end
    local blur = h / 4
    local over = blur * 4
    local edge = h / 2.2
    ass:new_event()
    ass:append(string.format(
        '{\\pos(0,0)\\an7\\bord0\\shad0\\blur%.1f\\1c%s\\1a%s%s}',
        blur, ass_color(node.c or '000000'), ass_alpha(hi - lo),
        clip_tag(c)))
    ass:draw_start()
    if a2 >= a1 then
        -- dense at the bottom: the box's top edge is the fade
        ass:rect_cw(ex - over, ey + h - edge, ex + w + over,
            ey + h + over)
    else
        ass:rect_cw(ex - over, ey - over, ex + w + over, ey + edge)
    end
    ass:draw_stop()
end

local function draw_dropdown(ass, node, ex, ey, clip)
    local d = dd_state(node)
    local open = state.dd_open == node.id
    if node.ticon then
        local hovered = open or state.hover_id == node.id
        local isz = math.floor(node.size * 1.2)
        if node.tchip then
            -- Button trigger (the now-playing bar's chapter picker): a
            -- filled rounded square, matching the transport buttons it
            -- sits among. Colours come from the app theme, which the
            -- renderer has no token for -- see Dropdown.trigger_chip.
            draw_rect(ass, ex, ey, node.w, node.h, {
                fill = hovered and node.tchip[2] or node.tchip[1],
                radius = 6, clip = clip,
            })
            draw_icon_path(ass, node.ticon,
                ex + (node.w - isz) / 2, ey + (node.h - isz) / 2, isz,
                node.tchip[3], clip)
            return
        end
        -- chromeless icon trigger (playback HUD track pickers):
        -- round translucent accent wash when hovered/open, accent
        -- icon tint — same treatment as the HUD's flat buttons
        if hovered then
            local r = math.min(node.w, node.h) / 2
            draw_rect(ass, ex + node.w / 2 - r, ey + node.h / 2 - r,
                r * 2, r * 2, {
                    fill = state.accent, a = 70, radius = r,
                    clip = clip,
                })
        end
        draw_icon_path(ass, node.ticon,
            ex + (node.w - isz) / 2, ey + (node.h - isz) / 2, isz,
            hovered and state.accent or state.tok.on_surface_muted, clip)
        return
    end
    draw_rect(ass, ex, ey, node.w, node.h, {
        fill = state.tok.control_sunken, radius = 6,
        bc = open and state.accent or state.tok.outline, bw = 1, clip = clip,
    })
    -- Disabled: the control keeps its shape so the form does not reflow,
    -- and loses its contrast. on_surface_faint for everything that was
    -- ink, which is the token that already means "present but not the
    -- thing you are reading".
    local ink = node.dis and state.tok.on_surface_faint
        or state.tok.on_surface
    local dim = node.dis and state.tok.on_surface_faint
        or state.tok.on_surface_muted
    local label = node.items[d.sel + 1] or ''
    local indent = 0
    local ipath = node.icons and node.icons[d.sel + 1]
    if ipath and ipath ~= '' then
        local isz = math.floor(node.size * 1.1)
        draw_icon_path(ass, ipath, ex + 8, ey + (node.h - isz) / 2,
            isz, dim, clip)
        indent = isz + 6
    end
    local tnode = {
        w = node.w - 40 - indent, h = node.h, size = node.size,
        align = 'left',
    }
    -- the label must not spill under the arrow or past the control
    label = ellipsize(label, node.size, false, tnode.w)
    draw_text(ass, tnode, ex + 10 + indent, ey, clip, label, ink)
    -- arrow
    local ax = ex + node.w - 22
    local ay = ey + node.h / 2 - 2
    ass:new_event()
    ass:append(string.format(
        '{\\pos(0,0)\\an7\\bord0\\shad0\\1c%s\\1a&H00&%s}',
        ass_color(dim), clip_tag(clip)))
    ass:draw_start()
    ass:move_to(ax, ay)
    ass:line_to(ax + 12, ay)
    ass:line_to(ax + 6, ay + 7)
    ass:draw_stop()
end

-- Longest popup that fits on screen, in items. A year filter or a big
-- track picker has more entries than the window is tall; without this the
-- overflow simply drew past the bottom edge, unreachable.
local function popup_max_items(ih)
    return math.max(1, math.floor((state.h - 16) / ih))
end

local function popup_geometry(node)
    local ex, ey = eff(node)
    -- icon triggers size the popup to the items (pw), not the control
    local w = node.pw or node.w
    -- ...and pw is measured from the item text, so a long one (a subtitle
    -- track named after its release) can be wider than the window. Clamping
    -- x alone would leave the right-hand edge off screen; the items
    -- ellipsize to whatever width survives.
    w = math.min(w, state.w - 8)
    local ih = node.pw and math.floor(node.size * 1.9) or node.h
    local count = #node.items
    local n = math.min(count, popup_max_items(ih))
    local off = math.max(0, math.min(state.dd_scroll or 0, count - n))
    local total = n * ih
    local px = math.max(0, math.min(ex, state.w - w - 4))
    local py = ey + node.h + 4
    if py + total > state.h and ey - 4 - total >= 0 then
        py = ey - 4 - total
    end
    -- a clamped popup may still not fit below/above: keep it on screen
    py = math.max(8, math.min(py, state.h - total - 8))
    return { x = px, y = py, w = w, ih = ih, n = n,
             count = count, off = off }
end

-- Scrollbar thumb of a clipped popup, or nil when the whole list fits.
-- One definition for drawing and hit-testing, so the grab rect can't
-- drift from what is on screen.
function popup_thumb(g)
    local count = g.count or g.n
    if not g or count <= g.n then return nil end
    -- Scaled, like the rest of this file's drawing constants: a 5px thumb
    -- beside a row that has trebled in height is not a scrollbar (#620).
    -- One definition serves drawing and hit-testing, so they scale together.
    local track_y, track_h = g.y + ui_px(4), g.n * g.ih - 2 * ui_px(4)
    local th = math.max(ui_px(18), track_h * g.n / count)
    local ty = track_y + (track_h - th) * ((g.off or 0) / (count - g.n))
    return { x = g.x + g.w - ui_px(8), y = ty, w = ui_px(5), h = th,
             track_y = track_y, track_h = track_h }
end

-- Opacity of the subordinate hover wash in a list that already has a
-- stronger cursor. Same idiom (and roughly the same weight) as the icon
-- trigger's hover disc above: enough to read as a highlight on any theme's
-- popup surface, not enough to be mistaken for the solid accent.
local HOVER_WASH = 70

-- Generic floating list (dropdown popups, context menus). sel may be
-- nil; icons is an optional parallel list of unit-canvas ASS paths
-- ('' = none).
-- `ranked` says the list has a VALUE, one row of which is what the control
-- is currently set to (a dropdown popup). A context menu does not: it is a
-- list of actions, `sel` there is the keyboard cursor, and the pointer is
-- the only other cursor there is. That difference decides how loud hover is
-- allowed to be — see the fills below.
local function draw_list(ass, g, items, sel, size, icons, ranked)
    -- The paddings are scaled for the same reason the seek bar's are (see
    -- draw_slider): they are constants of the drawing rather than fields of
    -- a node, so scale_scene never reaches them, and at 200% they left a
    -- tall row with a 10px gutter. `size` and `g.ih` arrive scaled already.
    local pad = ui_px(10)
    local isz = math.floor(size * 1.1)
    local indent = icons and (isz + pad) or 0
    draw_rect(ass, g.x, g.y, g.w, g.n * g.ih, {
        fill = state.tok.popup_bg, radius = ui_px(6),
        bc = state.tok.outline_strong, bw = 1,
    })
    local off = g.off or 0
    local count = g.count or #items
    for vis = 1, g.n do
        local i = vis + off
        local item = items[i]
        if item == nil then break end
        local iy = g.y + (vis - 1) * g.ih
        local hovered = state.mouse.x >= g.x and
            state.mouse.x <= g.x + g.w and
            state.mouse.y >= iy and state.mouse.y < iy + g.ih
        local chosen = sel ~= nil and (i - 1) == sel
        -- Two states, and which is which is the whole of #620. The STRONG
        -- one is "what ENTER would act on / what this control is set to";
        -- the WEAK one is "where the pointer is". This used to draw them the
        -- other way round -- hover took the accent and the selected row took
        -- control_bg, one step off popup_bg (0x2e3138 on 0x26292f in the
        -- stock theme) -- so "which quality am I on" was a question about
        -- two greys nobody can tell apart.
        --
        -- The weak state is the accent at LOW ALPHA rather than a grey
        -- token, and that is load-bearing: control_hover over popup_bg is
        -- BUTTON_ACTIVE over PANEL_BG, which in four of the six shipped
        -- themes is a weaker contrast than the fill this commit is
        -- replacing, and in jf-wmc is 1.00:1 -- the same relative luminance,
        -- ie. not drawn at all. Derived from the accent it cannot collapse
        -- into the background unless the accent already has.
        --
        -- `sel` in an UNRANKED list (a context menu) is the keyboard cursor
        -- rather than a value, so there the pointer and the cursor are the
        -- same kind of thing. With no keyboard cursor up, hover IS the
        -- cursor and takes the strong state -- which is what menus have
        -- always drawn. With one up, hover steps down to the weak state
        -- instead: both used to take the accent, so a right-click followed
        -- by Down painted two rows identically, and ENTER and a click would
        -- then act on different ones.
        local strong_hover = hovered and not (ranked or sel ~= nil)
        local fill, alpha = nil, nil
        if chosen then
            fill = hovered and state.tok.accent_hover or state.accent
        elseif strong_hover then
            fill = state.accent
        elseif hovered then
            fill, alpha = state.accent, HOVER_WASH
        end
        if fill then
            draw_rect(ass, g.x + ui_px(2), iy + ui_px(1),
                      g.w - 2 * ui_px(2), g.ih - 2 * ui_px(1),
                      { fill = fill, a = alpha, radius = ui_px(4) })
        end
        -- ...and the ink follows the fill it lands on, text and icon alike.
        -- ON_ACCENT is the one token that exists precisely because a theme's
        -- body text is not guaranteed to read on its accent -- but only over
        -- a SOLID one. The wash is mostly popup_bg, so body text is both
        -- correct over it and far more legible than on_accent would be.
        local on_accent = chosen or strong_hover
        local ink = on_accent and state.tok.on_accent or state.tok.on_surface
        if icons and icons[i] and icons[i] ~= '' then
            draw_icon_path(ass, icons[i], g.x + ui_px(8),
                iy + (g.ih - isz) / 2, isz,
                on_accent and ink or state.tok.on_surface_muted, nil)
        end
        local tnode = { w = g.w - 2 * pad - indent, h = g.ih, size = size,
                        align = 'left' }
        draw_text(ass, tnode, g.x + pad + indent, iy, nil,
            ellipsize(item, size, false, tnode.w), ink)
    end
    if count > g.n then
        -- a thumb, so a clipped list doesn't look like the whole list
        local t = popup_thumb(g)
        if t then
            draw_rect(ass, t.x, t.y, t.w, t.h,
                      { fill = state.dd_bar_drag and state.tok.scrollbar_thumb_active
                               or state.tok.scrollbar_thumb,
                        radius = t.w / 2 })
        end
    end
end

local function draw_popup(ass, node)
    local d = dd_state(node)
    local g = state.dd_geo or popup_geometry(node)
    -- keyboard navigation highlights its own index while active
    draw_list(ass, g, node.items, state.nav_pidx or d.sel, node.size,
        node.icons, true)
end

local function menu_geometry(node)
    local n = #node.items
    local ih = node.rh
    local x = math.max(0, math.min(node.x, state.w - node.w - 4))
    local y = node.y
    if y + n * ih > state.h and y - n * ih >= 0 then
        y = y - n * ih  -- flip above the click point
    end
    y = math.max(0, math.min(y, state.h - n * ih))
    return { x = x, y = y, w = node.w, ih = ih, n = n }
end

local function draw_menu(ass, node)
    draw_list(ass, state.menu_geo or menu_geometry(node), node.items,
        state.nav_pidx, node.size, node.icons)
end

local function item_at(g, x, y)
    if not g then return nil end
    if x < g.x or x >= g.x + g.w or y < g.y or y >= g.y + g.n * g.ih then
        return nil
    end
    return math.floor((y - g.y) / g.ih)
end

local function active_menu()
    if state.menu_hidden then return nil end
    for _, node in ipairs(state.nodes) do
        if node.t == 'menu' then return node end
    end
    return nil
end

local function modal_active()
    return state.modal ~= nil and not state.modal_hidden
end

-- ------------------------------------------------------ slider & busy

local function sl_state(node)
    local s = state.sl[node.id]
    -- force=true tracks the scene value (seek bars follow playback) —
    -- but never while the user is mid-gesture, or the app's periodic
    -- pushes would stomp the in-flight drag/adjust value. A merely
    -- FOCUSED always-adjust bar is not a gesture: nav_scrubbed flips
    -- on the first actual LEFT/RIGHT, so the idle thumb keeps moving
    -- with playback.
    local busy = state.slider_drag == node.id
        or (state.nav_adjust and state.nav == node.id
            and state.nav_scrubbed)
    if s == nil or (node.force and not busy) then
        s = { value = node.value or 0 }
        state.sl[node.id] = s
    end
    return s
end

local slider_notify_timers = {}
local slider_last_notify = {}

local function fire_slider(id)
    slider_last_notify[id] = mp.get_time()
    local node = state.byid[id]
    local s = state.sl[id]
    if node and s then
        send({ t = 'change', id = id, value = s.value })
    end
end

local function notify_slider(id)
    if slider_notify_timers[id] then return end
    local elapsed = mp.get_time() - (slider_last_notify[id] or -1e9)
    if elapsed >= 0.15 then
        fire_slider(id)
    else
        slider_notify_timers[id] = mp.add_timeout(0.15 - elapsed,
            function()
                slider_notify_timers[id] = nil
                fire_slider(id)
            end)
    end
end

local SLIDER_PAD = 8

-- Geometry of the standalone Skip button. The scene draws the same
-- button (hud.py's _skip_float) while the HUD is up and this one takes
-- over when it hides, so every number here mirrors that widget or the
-- handoff reads as the button hopping / changing weight:
--   PHUD_SKIP_BOTTOM  hud.py _SKIP_BOTTOM, to the button's BOTTOM edge
--   PHUD_SKIP_FS      hud.py _SKIP_SIZE
--   PHUD_SKIP_PAD     hud.py _SKIP_PAD (Box padding around the label)
--   PHUD_SKIP_LINE_H  layout.py LINE_H, which sets the label's height
-- Its label is a plain Text node, so it is NOT bold.
-- (tests/test_python_lua_constants.py)
local PHUD_SKIP_BOTTOM = 106
local PHUD_SKIP_FS = 18
local PHUD_SKIP_PAD = 10
local PHUD_SKIP_RIGHT = 24
local PHUD_SKIP_LINE_H = 1.25
--   PHUD_SKIP_BG/_FG/_ALPHA  hud.py _SKIP_BG/_SKIP_FG/_SKIP_ALPHA
-- Colours, so the handoff is not a colour flash. Deliberately NOT in
-- _SCALE_BASE below: those are pixel geometry and get multiplied by the
-- UI scale, which would turn an opacity into nonsense.
local PHUD_SKIP_BG = '202020'
local PHUD_SKIP_FG = 'ffffff'
local PHUD_SKIP_ALPHA = 180

local function draw_slider(ass, node, ex, ey, clip)
    -- No hover outline: the highlight means "this is what the arrows
    -- drive", so only keyboard/remote focus draws it (the nav ring).
    -- Under the pointer the bar is already directly clickable, and
    -- lighting it up on passing the mouse over read as a mode change
    -- that hadn't happened. The hover *preview bubble* still fires.
    local s = sl_state(node)
    local rng = (node.max or 100) - (node.min or 0)
    local frac = 0
    if rng > 0 then
        frac = clamp((s.value - (node.min or 0)) / rng, 0, 1)
    end
    local tx1 = ex + SLIDER_PAD
    local tw = node.w - 2 * SLIDER_PAD
    local ty = ey + node.h / 2
    -- Two surfaces. The playback HUD's bars sit on the PICTURE and keep a
    -- pale track that reads over any frame; an ordinary slider is app chrome
    -- and follows the theme, or a light theme would give it a near-black
    -- track. Same widget, same draw, different backdrop.
    local ov = node.ov
    -- Over video the accent gets its own token, so a theme whose accent does
    -- not read on a picture can pin the seek bar without giving up its
    -- colour everywhere else. Defaults to the accent.
    local accent = (ov and state.tok.accent_on_video) or state.accent
    if node.dis then
        -- see draw_dropdown: a disabled bar keeps its geometry (so the
        -- value it is stuck at stays readable) and loses its colour
        accent = state.tok.on_surface_faint
    end
    -- The bar's own geometry is scaled here rather than by scale_scene:
    -- these are constants of the DRAWING, not fields of the node, so
    -- nothing on the Python side ever sees them. Left literal they made the
    -- seek bar a 6px hairline with 2px chapter slits inside a HUD that had
    -- otherwise doubled -- which is #620's "the timeline bar is pretty thin
    -- and it's hard to see where the chapters are". SLIDER_PAD is already
    -- scaled (on receipt of mpvtk-scale) because a click maps through it.
    local th = ui_px(6)               -- track thickness
    local tr = th / 2
    local mw, mh = ui_px(2), ui_px(11)   -- chapter slit
    local kw = ui_px(16)              -- thumb diameter
    draw_rect(ass, tx1, ty - tr, tw, th,
        { fill = ov and '3a3a3a' or state.tok.control_sunken,
          radius = tr, clip = clip })
    -- buffered/seekable ranges, shaded like the jellyfin OSC's
    if node.ranges then
        for _, r in ipairs(node.ranges) do
            local r1, r2 = clamp(r[1], 0, 1), clamp(r[2], 0, 1)
            if r2 > r1 then
                draw_rect(ass, tx1 + tw * r1, ty - tr,
                    tw * (r2 - r1), th,
                    { fill = ov and 'ffffff' or state.tok.on_surface_muted,
                      a = 100, clip = clip })
            end
        end
    end
    if frac > 0 then
        draw_rect(ass, tx1, ty - tr, tw * frac, th,
            { fill = accent, radius = tr, clip = clip })
    end
    -- chapter slits (2x11 logical): accent once passed, dim white ahead —
    -- same treatment as the jellyfin OSC's seekbar markers
    if node.marks then
        for _, m in ipairs(node.marks) do
            if m > 0 and m < 1 then
                local passed = m <= frac
                draw_rect(ass, tx1 + tw * m - mw / 2, ty - mh / 2, mw, mh, {
                    fill = passed and accent
                           or (ov and 'ffffff' or state.tok.on_surface),
                    a = passed and 255 or 77,
                    clip = clip,
                })
            end
        end
    end
    draw_rect(ass, tx1 + tw * frac - kw / 2, ty - kw / 2, kw, kw,
        { fill = node.dis and state.tok.on_surface_faint
                 or (ov and 'dddddd' or state.tok.on_surface),
          radius = kw / 2, clip = clip })
end

-- Scrub semantics for seek-style sliders: 'change' fires (throttled)
-- while the value is in flight — a scrubbing preview, not a command;
-- 'commit' fires once when the gesture ends (drag release / adjust
-- mode toggled off) with the final value; 'cancel' reverts to the
-- scene value without committing (ESC, or focus moving off the
-- slider mid-adjust). Sliders whose app handler only registers
-- on_change (volume) behave exactly as before.
local function slider_flush(id)
    if slider_notify_timers[id] then
        slider_notify_timers[id]:kill()
        slider_notify_timers[id] = nil
    end
end

local function slider_commit(node)
    slider_flush(node.id)
    slider_last_notify[node.id] = mp.get_time()
    local s = sl_state(node)
    send({ t = 'commit', id = node.id, value = s.value })
    state.nav_scrubbed = nil
end

local function slider_cancel(node)
    slider_flush(node.id)
    local s = sl_state(node)
    s.value = node.value or 0
    send({ t = 'cancel', id = node.id })
    state.nav_scrubbed = nil
    request_render()
end

local function slider_set_from_x(node, x)
    local ex = select(1, eff(node))
    local frac = clamp(
        (x - ex - SLIDER_PAD) / (node.w - 2 * SLIDER_PAD), 0, 1)
    local s = sl_state(node)
    local v = (node.min or 0) +
        frac * ((node.max or 100) - (node.min or 0))
    if node.pv then
        -- Carry the scrub preview's position along with the drag. A drag
        -- returns out of on_mouse_move before update_slider_hover, so
        -- pv_secs would otherwise still hold wherever the pointer was when
        -- the drag STARTED -- and releasing the button, which drops
        -- slider_drag and falls the preview back to the hover position,
        -- made the bubble jump back there for a frame.
        state.pv_hover, state.pv_secs = node.id, v
    end
    if v ~= s.value then
        s.value = v
        notify_slider(node.id)
        request_render()
    end
end

-- Pointer-enter/leave notification, plus the seek bar's own hover
-- tracking. ONE function for two features because this chunk sits on
-- LuaJIT's ceiling of 200 locals in a main function -- a new top-level
-- local here does not fail at the call, it fails to load the renderer at
-- all.
--
-- `hev` is enter/leave for a node that wants a control drawn over it: the
-- tile strips' hit regions, and the play chip that then appears on one. The
-- chip opts in too, or the pointer moving onto it would read as leaving the
-- tile, the app would take the chip away, and the two would alternate for as
-- long as the pointer sat there. One event per tile crossed is the same
-- order as the hover ring this already re-renders for.
--
-- `pv` is the seek bar's scrub preview, and it sends NOTHING. It used to:
-- a throttled {t=hover, value} that the app answered with a whole rebuilt
-- HUD scene, which is why the bubble trailed the pointer by a frame or
-- three (#618). The renderer draws it now (see render), so all that is
-- left here is remembering where on the bar the pointer is.
local function update_slider_hover(node)
    local hid = (node and node.hev) and node.id or nil
    if hid ~= state.hover_region then
        local prev = state.hover_region
        state.hover_region = hid
        -- ENTER BEFORE LEAVE, deliberately. The app decides what to draw
        -- from the id it is entering, and a leave that arrived first would
        -- describe a state that is already over: crossing from a tile to the
        -- control floated on it, or from one tile to the next, would blank
        -- what had just been asked for. Arriving in this order, a leave for
        -- something the app has already moved on from is simply ignored.
        if hid then send({ t = 'hover', id = hid }) end
        if prev then send({ t = 'hover_end', id = prev }) end
    end
    local id = (node and node.t == 'slider' and node.pv) and node.id or nil
    local prev = state.pv_hover
    state.pv_hover = id
    if id then
        local ex = select(1, eff(node))
        local frac = clamp(
            (state.mouse.x - ex - SLIDER_PAD) /
            (node.w - 2 * SLIDER_PAD), 0, 1)
        local v = (node.min or 0) +
            frac * ((node.max or 100) - (node.min or 0))
        if v ~= state.pv_secs or prev ~= id then
            state.pv_secs = v
            -- on_mouse_move only re-renders when the hovered NODE changes,
            -- and moving along the bar never does; without this the bubble
            -- would stick at the position it first appeared at.
            request_render()
        end
    elseif prev then
        request_render()
    end
end

local busy_visible = false

local function draw_busy(ass, node, ex, ey, clip)
    busy_visible = true
    local cx, cy = ex + node.w / 2, ey + node.h / 2
    local r = math.min(node.w, node.h) / 2 - 4
    for i = 0, 7 do
        local ang = (i / 8) * 2 * math.pi
        local fade = ((i - state.busy_phase) % 8) / 8
        draw_rect(ass,
            cx + r * math.cos(ang) - 2.5,
            cy + r * math.sin(ang) - 2.5, 5, 5,
            { fill = 'cccccc', a = math.floor(255 - 200 * fade),
              radius = 2.5, clip = clip })
    end
end

local function draw_scrollbar(ass, node)
    local g = state.geo[node.id]
    local maxs = scroll_max(node)
    state.bars[node.id] = nil
    if not node.bar or maxs <= 0 or not g then return end
    local x1, y1, x2, y2 = g.x1, g.y1, g.x2, g.y2
    -- geometry of viewport (unclipped by ancestors for simplicity)
    -- Scaled for the same reason the seek bar and the popup's own thumb are
    -- (see draw_slider): drawing constants never pass through scale_scene.
    -- Left at 1x this was the only scrollbar in the app that did not follow
    -- the UI scale, so at 200% a dropdown's 10px thumb sat beside a 6px page
    -- scrollbar. Drawing and hit-testing both come from these numbers via
    -- state.bars below, so they move together.
    local bw = ui_px(6)
    local track_x = node.x + node.w - ui_px(8)
    local p = node.sc and state.geo[node.sc]
    if p then track_x = track_x - p.dx end
    local ty = node.y - (p and p.dy or 0)
    local th = node.h
    local frac = node.h / node.ch
    local thumb_h = math.max(ui_px(24), th * frac)
    -- Default: the thumb rides the continuous offset, so it glides a notch at
    -- a time even while the content only steps on detent crossings -- the
    -- feedback that keeps sub-row scrolling from feeling dead. In
    -- snapped_scrolling the offset already sits on a detent, so track it
    -- row-aligned to match.
    local off = state.snapped
        and snap_round(node, state.scroll[node.id] or 0)
        or clamp(state.scroll[node.id] or 0, 0, maxs)
    local thumb_y = ty + (th - thumb_h) * (off / maxs)
    local clip = p and { x1 = x1, y1 = y1, x2 = x2, y2 = y2 } or nil
    draw_rect(ass, track_x, ty, bw, th,
        { fill = state.tok.control_sunken, radius = bw / 2, clip = clip })
    draw_rect(ass, track_x, thumb_y, bw, thumb_h,
        { fill = state.tok.scrollbar_thumb, radius = bw / 2, clip = clip })
    state.bars[node.id] = {
        x = track_x, y = ty, w = bw, h = th,
        thumb_y = thumb_y, thumb_h = thumb_h,
    }
end

render = function()
    if state.w < 1 or not state.scene then
        osd.data = ''
        osd:update()
        return
    end
    compute_geo()
    overlay_list = {}
    occluders = {}
    flow_occs = {}
    state.bars = {}
    state.dd_geo = nil
    state.menu_geo = nil
    if state.dd_open then
        -- popup rect must occlude images, so compute it up front
        local node = state.byid[state.dd_open]
        if node then
            local g = popup_geometry(node)
            state.dd_geo = g
            occluders[#occluders + 1] = {
                x1 = g.x - 2, y1 = g.y - 2,
                x2 = g.x + g.w + 2, y2 = g.y + g.n * g.ih + 2,
            }
        end
    end
    local menu_node = active_menu()
    if menu_node then
        local g = menu_geometry(menu_node)
        state.menu_geo = g
        occluders[#occluders + 1] = {
            x1 = g.x - 2, y1 = g.y - 2,
            x2 = g.x + g.w + 2, y2 = g.y + g.n * g.ih + 2,
        }
    end
    state.tb_menu_geo = nil
    if state.tb_menu then
        local tbn = state.byid[state.tb_menu.id]
        if tbn and tbn.t == 'textbox' then
            local g = tb_menu_geometry(tb_menu_items(tbn))
            state.tb_menu_geo = g
            occluders[#occluders + 1] = {
                x1 = g.x - 2, y1 = g.y - 2,
                x2 = g.x + g.w + 2, y2 = g.y + g.n * g.ih + 2,
            }
        else
            state.tb_menu = nil
        end
    end
    -- floating layers (dialogs, toasts) occlude images too
    for _, node in ipairs(state.nodes) do
        if node.t == 'layer' and
            not (node.mod and state.modal_hidden) then
            occluders[#occluders + 1] = {
                x1 = node.x - 2, y1 = node.y - 2,
                x2 = node.x + node.w + 2, y2 = node.y + node.h + 2,
            }
        end
    end
    -- tooltip: geometry computed up front so it can occlude images
    state.tip_geo = nil
    if state.tip then
        local tnode = state.byid[state.tip]
        if tnode and tnode.tip and state.hover_id == state.tip then
            local tfs = ui_px(15)
            local tw = text_w(tnode.tip, tfs) + ui_px(18)
            local th = ui_px(26)
            local tx = clamp(state.mouse.x + ui_px(12), 2,
                             state.w - tw - 2)
            local ty = state.mouse.y + ui_px(20)
            if ty + th > state.h - 2 then
                ty = state.mouse.y - th - ui_px(8)
            end
            state.tip_geo = {
                x = tx, y = ty, w = tw, h = th, fs = tfs,
                text = tnode.tip,
            }
            occluders[#occluders + 1] = {
                x1 = tx - 2, y1 = ty - 2,
                x2 = tx + tw + 2, y2 = ty + th + 2,
            }
        else
            state.tip = nil
        end
    end
    -- Stack occlude markers: punch ASS children through image siblings
    -- painted below them (clipped to their scroll viewport)
    for i, node in ipairs(state.nodes) do
        if node.t == 'occ' then
            local ex, ey, c = eff(node)
            local r = {
                i = i,
                x1 = ex, y1 = ey,
                x2 = ex + node.w, y2 = ey + node.h,
            }
            if c then
                r.x1 = math.max(r.x1, c.x1)
                r.y1 = math.max(r.y1, c.y1)
                r.x2 = math.min(r.x2, c.x2)
                r.y2 = math.min(r.y2, c.y2)
            end
            if r.x2 > r.x1 and r.y2 > r.y1 then
                flow_occs[#flow_occs + 1] = r
            end
        end
    end
    busy_visible = false
    local function draw_node(ass, node, idx)
        local ex, ey, clip = eff(node)
        if node.t == 'rect' then
            local hs = hover_style(node)
            if node.ring then
                -- hit-rect over a bitmap: only a hover ring, outside
                if hs and hs.bc then
                    if state.glow then
                        -- Themed selection: a soft accent halo instead of a
                        -- hard box. Clipped to the page rather than to the
                        -- row — the row viewport would chop the blur back
                        -- into the box this replaces (see spill_clip).
                        draw_rect(ass, ex - 3, ey - 3,
                            node.w + 6, node.h + 6, {
                                bc = hs.bc, bw = 3, radius = 14, blur = 16,
                                clip = spill_clip(clip, 3 + 3 + 16),
                            })
                    else
                        draw_rect(ass, ex - 2, ey - 2,
                            node.w + 4, node.h + 4, {
                                bc = hs.bc, bw = hs.bw or 3,
                                radius = 3, clip = clip,
                            })
                    end
                end
            elseif hs and hs.circle then
                -- round translucent wash centered on the button —
                -- the jellyfin OSC's hover treatment
                local r = math.min(node.w, node.h) / 2
                draw_rect(ass, ex + node.w / 2 - r, ey + node.h / 2 - r,
                    r * 2, r * 2, {
                        fill = hs.fill or state.accent,
                        a = hs.a or 70, radius = r, clip = clip,
                    })
            elseif node.fill or node.bc or hs then
                if state.glow and hs and hs.glow then
                    -- Soft accent halo behind a hovered box (themed chrome:
                    -- hover={"glow": true}). Drawn first so the box sits on
                    -- top of it, and clipped to the page rather than to its
                    -- own container — the latter chops the blur back into a
                    -- hard edge (see spill_clip).
                    draw_rect(ass, ex - 2, ey - 2,
                        node.w + 4, node.h + 4, {
                            bc = hs.bc or node.bc or state.accent, bw = 2,
                            radius = (node.radius or 0) + 2, blur = 10,
                            clip = spill_clip(clip, 2 + 2 + 10),
                        })
                end
                draw_rect(ass, ex, ey, node.w, node.h, {
                    fill = (hs and hs.fill) or node.fill,
                    a = node.a, radius = node.radius,
                    bc = (hs and hs.bc) or node.bc, bw = node.bw,
                    clip = clip,
                })
            end
        elseif node.t == 'text' then
            local hs = hover_style(node)
            draw_text(ass, node, ex, ey, clip, node.text,
                (hs and hs.c) or node.c)
        elseif node.t == 'img' then
            draw_image(node, ex, ey, clip, idx)
            local hs = hover_style(node)
            if hs and hs.bc then
                -- bitmaps sit above ASS: the ring must be fully outside
                draw_rect(ass, ex - 2, ey - 2, node.w + 4, node.h + 4, {
                    bc = hs.bc, bw = hs.bw or 3,
                    radius = 3, clip = clip,
                })
            end
        elseif node.t == 'textbox' then
            draw_textbox(ass, node, ex, ey, clip)
        elseif node.t == 'dropdown' then
            draw_dropdown(ass, node, ex, ey, clip)
        elseif node.t == 'slider' then
            draw_slider(ass, node, ex, ey, clip)
        elseif node.t == 'busy' then
            draw_busy(ass, node, ex, ey, clip)
        elseif node.t == 'grad' then
            draw_gradient(ass, node, ex, ey, clip)
        elseif node.t == 'icon' then
            local hs = hover_style(node)
            local c = (hs and hs.c) or node.c or state.tok.on_surface
            if node.hb and state.hover_id == node.hb and node.hc then
                c = node.hc  -- accent tint while the parent button hovers
            end
            draw_icon_path(ass, node.path, ex, ey,
                math.min(node.w, node.h), c, clip)
        end
    end
    local ass = assdraw.ass_new()
    local skip = { scroll = true, menu = true, layer = true, occ = true }
    for i, node in ipairs(state.nodes) do
        if not skip[node.t] and not node.top and visible(node) then
            draw_node(ass, node, i)
        end
    end
    for _, node in ipairs(state.nodes) do
        if node.t == 'scroll' and not node.top then
            draw_scrollbar(ass, node)
        end
    end
    -- top layer: dialog / toast content above everything in flow
    for i, node in ipairs(state.nodes) do
        if node.top and not skip[node.t] and
            not (node.mod and state.modal_hidden) and visible(node) then
            draw_node(ass, node, i)
        end
    end
    for _, node in ipairs(state.nodes) do
        if node.t == 'scroll' and node.top and
            not (node.mod and state.modal_hidden) then
            draw_scrollbar(ass, node)
        end
    end
    -- spatial-nav focus ring: outside the node bounds like hover rings
    -- (bitmaps would cover an inline ring). Theme accent; white while
    -- a slider is in adjust mode.
    if state.nav then
        local node = state.byid[state.nav]
        if node and visible(node) then
            local ex, ey, clip = eff(node)
            -- an always-adjust bar is permanently live, so it keeps
            -- the accent "active" outline; white stays the explicit
            -- adjust-mode signal for ordinary sliders
            local ring = state.accent
            if state.nav_adjust and not node.aadj then
                ring = state.tok.on_surface_strong
            end
            draw_rect(ass, ex - 3, ey - 3, node.w + 6, node.h + 6, {
                bc = ring, bw = 3, radius = 4, clip = clip,
            })
        end
    end
    if state.dd_open then
        local node = state.byid[state.dd_open]
        if node then draw_popup(ass, node) end
    end
    if menu_node then draw_menu(ass, menu_node) end
    if state.tb_menu_geo then
        local tbn = state.byid[state.tb_menu.id]
        if tbn then
            -- g.fs, not a literal: the box was measured at that size
            draw_list(ass, state.tb_menu_geo, tb_menu_items(tbn),
                nil, state.tb_menu_geo.fs)
        end
    end
    if state.tip_geo then
        local g = state.tip_geo
        draw_rect(ass, g.x, g.y, g.w, g.h, {
            fill = state.tok.overlay_bg, a = 245, radius = 5,
            bc = state.tok.control_hover, bw = 1,
        })
        draw_text(ass, { w = g.w - ui_px(18), h = g.h, size = g.fs,
                         align = 'left' },
            g.x + ui_px(9), g.y, nil, g.text, state.tok.on_surface_muted)
    end
    if busy_visible and not state.busy_timer then
        state.busy_timer = mp.add_periodic_timer(0.1, function()
            state.busy_phase = (state.busy_phase + 1) % 8
            request_render()
        end)
    elseif not busy_visible and state.busy_timer then
        state.busy_timer:kill()
        state.busy_timer = nil
    end
    if state.phud.mode and state.phud.intro
        and (state.phud.skip_show or state.phud.shown)
        and not state.byid['hud-skip'] then
        -- Standalone Skip Intro/Credits button (see the mpvtk-hud-skip
        -- handler). Renderer-drawn: the idle scene is blank by design,
        -- so this can't come from a scene push, and drawing it here
        -- also covers the summon round-trip during which the scene's
        -- own hud-skip node does not exist yet. Whenever that node IS
        -- present the scene owns the button and this yields, so the two
        -- never double up. Mirrors the scene button's style/placement.
        -- Box(pad) around a centred, non-bold Text — the same box the
        -- scene's Button lays out, rebuilt from the shared constants so
        -- the two copies occupy the same pixels and the handoff between
        -- them is invisible.
        local label = state.phud.intro
        local fs = PHUD_SKIP_FS
        local bw = math.floor(text_w(label, fs, false)
                              + 2 * PHUD_SKIP_PAD)
        local bh = math.floor(fs * PHUD_SKIP_LINE_H + 2 * PHUD_SKIP_PAD)
        local x1 = state.w - bw - PHUD_SKIP_RIGHT
        local y1 = state.h - PHUD_SKIP_BOTTOM - bh
        state.phud.skip_rect = { x1 = x1, y1 = y1,
                                 x2 = x1 + bw, y2 = y1 + bh }
        draw_rect(ass, x1, y1, bw, bh,
            { fill = PHUD_SKIP_BG, a = PHUD_SKIP_ALPHA, radius = 6 })
        ass:new_event()
        -- \1a&H00& keeps the label fully opaque over the translucent
        -- box: the background is what softens against the picture, and
        -- fading the text with it would cost legibility on bright frames.
        ass:append(string.format(
            '{\\q2\\an5\\pos(%.1f,%.1f)\\fs%.2f\\bord0\\shad0\\1c%s' ..
            '\\1a&H00&\\b0%s}',
            x1 + bw / 2, y1 + bh / 2, fs, ass_color(PHUD_SKIP_FG),
            ui_font and ('\\fn' .. ui_font) or ''))
        ass:append(esc(label))
    else
        state.phud.skip_rect = nil
    end
    -- Scrub preview bubble: the trickplay frame for the position under the
    -- pointer (or being dragged / arrow-adjusted), the chapter that position
    -- falls in, and the timestamp. Everything it needs is already here --
    -- the tile file is raw BGRA that overlay-add takes as-is, and mpv owns
    -- chapter-list -- so it tracks the pointer at render cadence instead of
    -- at the speed of a round trip through a rebuilt HUD scene (#618).
    --
    -- Measuring its own text is the other half: Python could only *assume* a
    -- width for a box it did not draw, and the width it assumed was right
    -- only when a thumbnail was the widest thing in it. Without trickplay
    -- the bubble drifted by half the difference, which is #612.
    state.pv_rect = nil
    local pv_id, pv_secs
    if state.slider_drag then
        pv_id = state.slider_drag
    elseif state.nav_adjust and state.nav_scrubbed then
        pv_id = state.nav
    end
    if pv_id then
        local n = state.byid[pv_id]
        if n and n.pv then pv_secs = sl_state(n).value else pv_id = nil end
    end
    if not pv_id and state.pv_hover then
        pv_id, pv_secs = state.pv_hover, state.pv_secs
    end
    local pv_node = pv_id and state.byid[pv_id]
    if pv_node and pv_secs and (pv_node.max or 0) > 0 then
        local pad, gap = ui_px(8), ui_px(4)
        local fs = ui_px(14)
        local lh = math.floor(fs * PHUD_SKIP_LINE_H)
        -- Which frame of the tile file, if there is one. BIF data is
        -- evenly spaced (multiplier ms apart); the chapter-image fallback
        -- carries one frame per chapter start.
        local tp = state.tp
        local fw, fh, base = 0, 0, 0
        local idx
        if tp and (tp.iw or 0) > 0 and (tp.ih or 0) > 0 then
            if tp.times and #tp.times > 0 then
                idx = 0
                for i = #tp.times, 1, -1 do
                    if tp.times[i] <= pv_secs then idx = i - 1 break end
                end
                if idx > #tp.times - 1 then idx = #tp.times - 1 end
            elseif (tp.count or 0) > 0 and (tp.mult or 0) > 0 then
                idx = math.floor(pv_secs * 1000 / tp.mult)
                if idx > tp.count - 1 then idx = tp.count - 1 end
                if idx < 0 then idx = 0 end
            end
        end
        if idx then
            fw, fh = tp.iw, tp.ih
            base = idx * tp.iw * tp.ih * 4
        end
        local cap
        for _, c in ipairs(state.chlist or {}) do
            if (c.time or 0) <= pv_secs and c.title and c.title ~= '' then
                cap = c.title
            end
        end
        local whole = math.floor(pv_secs)
        local ts
        if whole >= 3600 then
            ts = string.format('%d:%02d:%02d', math.floor(whole / 3600),
                               math.floor(whole % 3600 / 60), whole % 60)
        else
            ts = string.format('%d:%02d', math.floor(whole / 60), whole % 60)
        end
        -- Capped at the window, and the caption ellipsized to fit it.
        -- A chapter name comes from container metadata and can run to a
        -- sentence; clamp() returns its LOW bound when hi < lo, so an
        -- over-wide bubble pinned itself at x=8, ran off the right edge,
        -- and stopped being centred on the position it labels -- which is
        -- #612, the bug this block exists to fix.
        local bwmax = state.w - 2 * ui_px(8)
        local bw = math.min(bwmax,
                            math.max(fw, text_w(ts, fs, true),
                                     cap and text_w(cap, fs, false) or 0)
                            + 2 * pad)
        if cap then cap = ellipsize(cap, fs, false, bw - 2 * pad) end
        local bh = 2 * pad + lh + (fh > 0 and (fh + gap) or 0)
                   + (cap and (lh + gap) or 0)
        local ex, ey = eff(pv_node)
        local frac = clamp(pv_secs / pv_node.max, 0, 1)
        local px = math.floor(clamp(
            ex + SLIDER_PAD + (pv_node.w - 2 * SLIDER_PAD) * frac - bw / 2,
            ui_px(8), state.w - bw - ui_px(8)))
        -- Pinned by its BOTTOM edge, so a bubble that grows a thumbnail or
        -- a chapter line grows upwards and never over the bar it describes.
        local py = math.floor(math.max(0, ey - ui_px(10) - bh))
        state.pv_rect = { x = px, y = py, w = bw, h = bh,
                          secs = pv_secs, frame = idx }
        draw_rect(ass, px, py, bw, bh,
                  { fill = '282828', radius = ui_px(6) })
        local ty = py + pad
        if fh > 0 then
            -- Native size: overlay-add does not scale, and the worker
            -- already decoded these at thumbnail_preferred_size.
            draw_image({ id = 'hud-preview', src = tp.file, base = base,
                         iw = fw, ih = fh, w = fw, h = fh },
                       math.floor(px + (bw - fw) / 2), ty)
            ty = ty + fh + gap
        end
        if cap then
            draw_text(ass, { w = bw - 2 * pad, h = lh, size = fs,
                             align = 'center' },
                      px + pad, ty, nil, cap, 'dddddd')
            ty = ty + lh + gap
        end
        draw_text(ass, { w = bw - 2 * pad, h = lh, size = fs,
                         align = 'center', bold = true },
                  px + pad, ty, nil, ts, 'ffffff')
    end
    if state.hud then
        -- input-diagnostics overlay (toggle: F12)
        local lw = state.last_wheel or {}
        local tnode = { w = 640, h = 24, size = 16, align = 'right' }
        draw_text(ass, tnode, state.w - 650, 4, nil,
            string.format(
                'wheel:%d s:%.2f tgt:%s off:%s snap:%s ' ..
                'draw:%dms/%dms | mouse:%d,%d hover:%s',
                state.wheel_count or 0, lw.scale or 0,
                tostring(lw.target), tostring(lw.off),
                -- 'F' forced by the setting, 'g' this gesture,
                -- '-' free. The whole point of the feature is that most
                -- scrolling reads '-', so it is worth being able to see.
                state.force_snap and 'F'
                    or (state.snap_live and 'g' or '-'),
                -- What a frame costs and the cadence that buys it. These
                -- two decide everything above: a machine that reads
                -- draw:3ms/30ms will never quantize, and one that reads
                -- draw:50ms/62ms always will. Reading them is how you tell
                -- "the renderer is slow here" from "the wheel is not
                -- arriving", which look identical from the outside.
                math.floor(state.rcost * 1000 + 0.5),
                math.floor(active_tick() * 1000 + 0.5),
                state.mouse.x or -1, state.mouse.y or -1,
                tostring(state.mouse.hover)),
            'ffcc66')
    end
    osd.res_x = state.w
    osd.res_y = state.h
    osd.data = ass.text
    osd:update()
    flush_overlays()
end

-- ------------------------------------------------------------ hit tests

local function point_in(x, y, ex, ey, w, h, clip)
    if x < ex or y < ey or x >= ex + w or y >= ey + h then return false end
    if clip and (x < clip.x1 or y < clip.y1 or x >= clip.x2 or
                 y >= clip.y2) then
        return false
    end
    return true
end

-- Interactive node under point (topmost = latest in paint order).
-- While a modal dialog is open only its nodes are targetable.
local function node_at(x, y)
    local modal = modal_active()
    for i = #state.nodes, 1, -1 do
        local node = state.nodes[i]
        if node.t ~= 'scroll' and node.t ~= 'layer' and
            node.t ~= 'menu' and
            (not modal or node.mod) and
            (node.click or node.ctx or node.dbl or node.tip or
             node.hev or node.dis or node.wdrag or node.wsize or
             node.t == 'textbox' or
             node.t == 'dropdown' or node.t == 'slider' or
             node.hover) then
            local ex, ey, clip = eff(node)
            if point_in(x, y, ex, ey, node.w, node.h, clip) then
                return node
            end
        end
    end
    return nil
end

-- Deepest scroll container under point; with ``axis`` set, walks up the
-- container chain to the nearest scrollable with that axis (vertical
-- wheel over a tile row should scroll the page, not the row).
local function scroll_at(x, y, axis)
    local modal = modal_active()
    for i = #state.nodes, 1, -1 do
        local node = state.nodes[i]
        if node.t == 'scroll' and (not modal or node.mod) then
            local g = state.geo[node.id]
            if g and x >= g.x1 and x < g.x2 and y >= g.y1 and y < g.y2 then
                local cur = node
                while cur do
                    if (axis == nil or cur.axis == axis) and
                        scroll_max(cur) > 0 then
                        return cur
                    end
                    cur = cur.sc and state.byid[cur.sc] or nil
                end
                return nil
            end
        end
    end
    return nil
end

local function bar_at(x, y)
    local modal = modal_active()
    for id, b in pairs(state.bars) do
        local sn = state.byid[id]
        if (not modal or (sn and sn.mod)) and
            x >= b.x - 2 and x <= b.x + b.w + 2 and y >= b.y and
            y <= b.y + b.h then
            return id, b
        end
    end
    return nil
end

local function popup_item_at(x, y)
    local g = state.dd_geo
    if not g then return nil end
    if x < g.x or x >= g.x + g.w or y < g.y or y >= g.y + g.n * g.ih then
        return nil
    end
    -- + off: the popup shows a window into the item list, not all of it
    return math.floor((y - g.y) / g.ih) + (g.off or 0)
end

-- Put the thumb's centre at pointer ``y``: turns a track click or a drag
-- into a scroll offset.
function popup_scroll_to_y(y, t, g)
    local count = g.count or g.n
    local span = t.track_h - t.h
    if span <= 0 then return end
    local frac = ((y - t.h / 2) - t.track_y) / span
    frac = math.max(0, math.min(1, frac))
    state.dd_scroll = math.floor(frac * (count - g.n) + 0.5)
    request_render()
end

-- Scroll an open popup, keeping ``keep`` (an item index) visible when
-- given — keyboard navigation must not walk off the drawn window.
function popup_scroll(delta, keep)
    local g = state.dd_geo
    if not g or g.count <= g.n then return end
    local off = (state.dd_scroll or 0) + (delta or 0)
    if keep ~= nil then
        off = math.min(off, keep)
        off = math.max(off, keep - g.n + 1)
    end
    state.dd_scroll = math.max(0, math.min(off, g.count - g.n))
    request_render()
end

-- ---------------------------------------------------------- text editing

local text_keys_bound = false

local function tb_state(node)
    local tb = state.tb[node.id]
    -- force=true tracks the scene value (the page box follows Next/Prev/Last)
    -- — but never while the box is focused. tb_state is called on every draw
    -- AND from the key handlers, so an unconditional force would rebuild from
    -- node.text on each keystroke: typed characters would never accumulate and
    -- ENTER would submit the current page instead of the one just typed. Same
    -- shape as sl_state's mid-gesture guard.
    local editing = state.focus == node.id
    if tb == nil or (node.force and not editing) then
        tb = { text = node.text or '', cursor = #(node.text or ''),
               shift = 0 }
        state.tb[node.id] = tb
    end
    -- The value this box last agreed with the app on. blur() compares
    -- against it so leaving a field commits an edit instead of dropping it,
    -- and so an untouched field stays silent.
    if tb.committed == nil then tb.committed = tb.text end
    return tb
end

local function tb_fix_shift(node, tb)
    local pad = 10
    local avail = node.w - 2 * pad
    local cx = tb_text_w(node, tb.text, tb.cursor)
    if cx - tb.shift > avail then tb.shift = cx - avail end
    if cx - tb.shift < 0 then tb.shift = cx end
    if tb.shift < 0 then tb.shift = 0 end
end

-- Word boundaries for ctrl+arrow / ctrl+backspace (space-delimited).
local function word_left(text, pos)
    while pos > 0 and text:sub(pos, pos) == ' ' do pos = pos - 1 end
    while pos > 0 and text:sub(pos, pos) ~= ' ' do pos = pos - 1 end
    return pos
end

local function word_right(text, pos)
    local n = #text
    while pos < n and text:sub(pos + 1, pos + 1) == ' ' do
        pos = pos + 1
    end
    while pos < n and text:sub(pos + 1, pos + 1) ~= ' ' do
        pos = pos + 1
    end
    return pos
end

-- Deletes the selected range if any; returns true when it did.
local function tb_del_selection(tb)
    if tb.sel == nil or tb.sel == tb.cursor then
        tb.sel = nil
        return false
    end
    local a = math.min(tb.sel, tb.cursor)
    local b = math.max(tb.sel, tb.cursor)
    tb.text = tb.text:sub(1, a) .. tb.text:sub(b + 1)
    tb.cursor = a
    tb.sel = nil
    return true
end

local function focused_node()
    return state.focus and state.byid[state.focus]
end

local function tb_changed(node, tb)
    tb_fix_shift(node, tb)
    state.cursor_on = true
    send({ t = 'change', id = node.id, value = tb.text })
    request_render()
end

local function tb_insert(s)
    local node = focused_node()
    if not node then return end
    local tb = tb_state(node)
    tb_del_selection(tb)  -- typing replaces the selection
    tb.text = tb.text:sub(1, tb.cursor) .. s .. tb.text:sub(tb.cursor + 1)
    tb.cursor = tb.cursor + #s
    tb_changed(node, tb)
end

local function tb_key(name)
    local node = focused_node()
    if not node then return end
    local tb = tb_state(node)
    if name == 'BS' then
        if tb_del_selection(tb) then
            tb_changed(node, tb)
        elseif tb.cursor > 0 then
            local a = u8_prev(tb.text, tb.cursor)
            tb.text = tb.text:sub(1, a) ..
                tb.text:sub(tb.cursor + 1)
            tb.cursor = a
            tb_changed(node, tb)
        end
    elseif name == 'DEL' then
        if tb_del_selection(tb) then
            tb_changed(node, tb)
        elseif tb.cursor < #tb.text then
            local b = u8_next(tb.text, tb.cursor)
            tb.text = tb.text:sub(1, tb.cursor) ..
                tb.text:sub(b + 1)
            tb_changed(node, tb)
        end
    elseif name == 'LEFT' then
        tb.sel = nil
        tb.cursor = u8_prev(tb.text, tb.cursor)
        tb_fix_shift(node, tb); state.cursor_on = true; request_render()
    elseif name == 'RIGHT' then
        tb.sel = nil
        tb.cursor = u8_next(tb.text, tb.cursor)
        tb_fix_shift(node, tb); state.cursor_on = true; request_render()
    elseif name == 'SLEFT' or name == 'SRIGHT' or
        name == 'CSLEFT' or name == 'CSRIGHT' then
        if tb.sel == nil then tb.sel = tb.cursor end
        if name == 'SLEFT' then
            tb.cursor = u8_prev(tb.text, tb.cursor)
        elseif name == 'SRIGHT' then
            tb.cursor = u8_next(tb.text, tb.cursor)
        elseif name == 'CSLEFT' then
            tb.cursor = word_left(tb.text, tb.cursor)
        else
            tb.cursor = word_right(tb.text, tb.cursor)
        end
        if tb.sel == tb.cursor then tb.sel = nil end
        tb_fix_shift(node, tb); state.cursor_on = true; request_render()
    elseif name == 'CLEFT' or name == 'CRIGHT' then
        tb.sel = nil
        if name == 'CLEFT' then
            tb.cursor = word_left(tb.text, tb.cursor)
        else
            tb.cursor = word_right(tb.text, tb.cursor)
        end
        tb_fix_shift(node, tb); state.cursor_on = true; request_render()
    elseif name == 'CBS' then
        if tb_del_selection(tb) then
            tb_changed(node, tb)
        elseif tb.cursor > 0 then
            local a = word_left(tb.text, tb.cursor)
            tb.text = tb.text:sub(1, a) .. tb.text:sub(tb.cursor + 1)
            tb.cursor = a
            tb_changed(node, tb)
        end
    elseif name == 'CDEL' then
        if tb_del_selection(tb) then
            tb_changed(node, tb)
        elseif tb.cursor < #tb.text then
            local b = word_right(tb.text, tb.cursor)
            tb.text = tb.text:sub(1, tb.cursor) .. tb.text:sub(b + 1)
            tb_changed(node, tb)
        end
    elseif name == 'CTRLA' then
        tb.sel = 0
        tb.cursor = #tb.text
        tb_fix_shift(node, tb); request_render()
    elseif name == 'CTRLC' then
        if tb.sel and tb.sel ~= tb.cursor and not node.mask then
            local a = math.min(tb.sel, tb.cursor)
            local b = math.max(tb.sel, tb.cursor)
            if not clip_set(tb.text:sub(a + 1, b)) then
                clip_notify('copy')
            end
        end
    elseif name == 'CUT' then
        if tb.sel and tb.sel ~= tb.cursor then
            -- A cut whose copy failed would just destroy the text, so
            -- only masked fields (which never copy) cut unconditionally.
            local cut = true
            if not node.mask then
                local a = math.min(tb.sel, tb.cursor)
                local b = math.max(tb.sel, tb.cursor)
                cut = clip_set(tb.text:sub(a + 1, b))
                if not cut then clip_notify('copy') end
            end
            if cut then
                tb_del_selection(tb)
                tb_changed(node, tb)
            end
        end
    elseif name == 'HOME' then
        tb.sel = nil
        tb.cursor = 0
        tb_fix_shift(node, tb); request_render()
    elseif name == 'END' then
        tb.sel = nil
        tb.cursor = #tb.text
        tb_fix_shift(node, tb); request_render()
    elseif name == 'ENTER' then
        send({ t = 'submit', id = node.id, value = tb.text })
        tb.committed = tb.text
    elseif name == 'ESC' then
        if state.tb_menu then
            state.tb_menu = nil
            request_render()
        else
            -- ESC is a cancel: put back what the field held on focus, so
            -- the blur below has nothing to commit.
            tb.text = tb.committed
            tb.cursor = #tb.text
            tb_fix_shift(node, tb)
            blur()  -- luacheck: ignore (fwd-declared below)
        end
    elseif name == 'PASTE' then
        local clip = clip_get()
        if clip then
            tb_insert(clip:gsub('[\r\n]', ' '))
        else
            clip_notify('paste')
        end
    end
end

local text_key_names = {}

-- The any_unicode handler, hoisted out of the binding so the debug hook
-- (mpvtk-debug cmd=text) drives the same code a real keypress does. They
-- were separate, which made every "type into a box" test pass against a
-- renderer that had stopped delivering keystrokes entirely.
function tb_key_text(e)
    if not e or e.event == 'up' then return end
    local t = e.key_text
    if not t or t == '' or t:byte(1) < 0x20 then return end
    tb_insert(t)
end

local function bind_text_keys()
    if text_keys_bound then return end
    text_keys_bound = true
    text_key_names = {}
    local function bind(key, bname, fn)
        mp.add_forced_key_binding(key, bname, fn,
            { repeatable = true })
        text_key_names[#text_key_names + 1] = bname
    end
    -- ALL printable text arrives through any_unicode's key_text — the
    -- full unicode range, including IME-committed strings on backends
    -- where mpv receives them (Wayland text-input-v3, Windows IME).
    mp.add_forced_key_binding('any_unicode', 'mpvtk_text',
        function(e) tb_key_text(e) end,
        { repeatable = true, complex = true })
    text_key_names[#text_key_names + 1] = 'mpvtk_text'
    -- editing keys: mpv key -> tb_key op (binding names derive from
    -- the KEY so e.g. ctrl+HOME can share the HOME action)
    local ops = {
        ['BS'] = 'BS', ['DEL'] = 'DEL',
        ['LEFT'] = 'LEFT', ['RIGHT'] = 'RIGHT',
        ['HOME'] = 'HOME', ['END'] = 'END',
        ['ENTER'] = 'ENTER', ['KP_ENTER'] = 'ENTER', ['ESC'] = 'ESC',
        ['shift+LEFT'] = 'SLEFT', ['shift+RIGHT'] = 'SRIGHT',
        ['ctrl+LEFT'] = 'CLEFT', ['ctrl+RIGHT'] = 'CRIGHT',
        ['ctrl+shift+LEFT'] = 'CSLEFT',
        ['ctrl+shift+RIGHT'] = 'CSRIGHT',
        ['ctrl+BS'] = 'CBS', ['ctrl+DEL'] = 'CDEL',
        ['ctrl+HOME'] = 'HOME', ['ctrl+END'] = 'END',
        ['ctrl+a'] = 'CTRLA', ['ctrl+c'] = 'CTRLC',
        ['ctrl+x'] = 'CUT', ['ctrl+v'] = 'PASTE',
    }
    for key, op in pairs(ops) do
        bind(key, 'mpvtk_k_' .. key:gsub('%+', '_'), function()
            tb_key(op)
        end)
    end
    -- Numpad digits: with NumLock on, mpv delivers these as the named keys
    -- KP0..KP9 / KP_DEC, NOT as any_unicode, so they never reach tb_key_text
    -- and typing a port/PIN/URL from the numpad did nothing. Bind them to
    -- insert their literal character. NumLock-off numpad keys keep their
    -- KP_INS/KP_END/... nav meaning (not bound here) and are unaffected.
    local kp_chars = {
        ['KP0'] = '0', ['KP1'] = '1', ['KP2'] = '2', ['KP3'] = '3',
        ['KP4'] = '4', ['KP5'] = '5', ['KP6'] = '6', ['KP7'] = '7',
        ['KP8'] = '8', ['KP9'] = '9', ['KP_DEC'] = '.',
    }
    for key, ch in pairs(kp_chars) do
        bind(key, 'mpvtk_kp_' .. key, function() tb_insert(ch) end)
    end
end

local function unbind_text_keys()
    if not text_keys_bound then return end
    text_keys_bound = false
    for _, bname in ipairs(text_key_names) do
        mp.remove_key_binding(bname)
    end
    text_key_names = {}
end

function blur()
    if not state.focus then return end
    -- Leaving a text field commits it. Without this the only way to save was
    -- ENTER, and clicking away silently discarded what you typed — across 65
    -- settings rows, with nothing on screen saying so.
    local node = state.byid[state.focus]
    local tb = node and state.tb[state.focus]
    if tb and tb.committed ~= nil and tb.text ~= tb.committed then
        tb.committed = tb.text
        send({ t = 'commit', id = state.focus, value = tb.text })
    end
    state.focus = nil
    unbind_text_keys()
    if state.blink_timer then
        state.blink_timer:kill()
        state.blink_timer = nil
    end
    request_render()
end

local function focus_textbox(node)
    if state.focus == node.id then return end
    blur()
    state.focus = node.id
    tb_state(node)
    state.cursor_on = true
    bind_text_keys()
    state.blink_timer = mp.add_periodic_timer(0.55, function()
        state.cursor_on = not state.cursor_on
        request_render()
    end)
    request_render()
end

-- --------------------------------------------------------------- input

local scroll_notify_timers = {}
local scroll_last_notify = {}
local SCROLL_NOTIFY_INTERVAL = 0.15

-- Where a container is *settling* to, while slide_scroll eases it there.
-- Both the published snapshot and the scroll event report this rather than
-- the in-flight value, because every consumer wants the destination:
-- virtualization should build the window the row is arriving at, park()
-- should remember it, and the carousel page buttons page from it — which is
-- what lets hold-repeat chain whole pages instead of re-deriving one from a
-- half-finished slide (the slide is longer than the repeat interval).
local scroll_dest = {}

-- Scroll notification for watched containers (drives windowed/infinite
-- scrolling on the Python side). This is a leading-edge THROTTLE, not
-- a debounce: the first tick notifies immediately and sustained
-- scrolling notifies every interval, so the app can materialize ahead
-- DURING the scroll — a trailing debounce only fired after the wheel
-- stopped, which is exactly "infinite scroll falls behind". A trailing
-- timer still covers the final resting position.
local function fire_scroll(id)
    scroll_last_notify[id] = mp.get_time()
    local node = state.byid[id]
    if node and node.t == 'scroll' then
        send({
            t = 'scroll', id = id,
            offset = scroll_dest[id] or state.scroll[id] or 0,
            max = scroll_max(node),
        })
    end
end

local function notify_scroll(id)
    if scroll_notify_timers[id] then
        return  -- a trailing notification is already scheduled
    end
    local elapsed = mp.get_time() - (scroll_last_notify[id] or -1e9)
    if elapsed >= SCROLL_NOTIFY_INTERVAL then
        fire_scroll(id)
    else
        scroll_notify_timers[id] = mp.add_timeout(
            SCROLL_NOTIFY_INTERVAL - elapsed,
            function()
                scroll_notify_timers[id] = nil
                fire_scroll(id)
            end)
    end
end

-- Mirror scroll offsets into a property the Python side can read
-- SYNCHRONOUSLY at build() time (tight virtualization windows; the
-- throttled scroll event is the async path). user-data needs mpv >=
-- 0.36; a failed set is harmless on older builds.
local function publish_scroll()
    local out = state.scroll
    if next(scroll_dest) ~= nil then
        out = {}
        for k, v in pairs(state.scroll) do
            out[k] = v
        end
        -- Overlaid second, and not merged into the loop above: a container
        -- sliding away from rest has no state.scroll entry yet, so keying off
        -- that map alone published nothing for exactly the rows that are
        -- moving.
        for k, v in pairs(scroll_dest) do
            out[k] = v
        end
    end
    pcall(mp.set_property_native, 'user-data/mpvtk/scroll', out)
end

-- Running slide_scroll animations, by container id.
local scroll_anim = {}

local function stop_slide(id)
    local a = scroll_anim[id]
    if a then
        a.timer:kill()
        scroll_anim[id] = nil
        scroll_dest[id] = nil
    end
end

-- `quiet` moves the container without telling Python. Only slide_scroll's
-- intermediate frames use it: the destination was announced when the slide
-- started, so re-announcing every frame would publish the same value ~7 times
-- over and fire the watch event off a position nothing should act on.
local function set_scroll_now(node, off, quiet)
    off = clamp(off, 0, scroll_max(node))
    if state.scroll[node.id] ~= off then
        state.scroll[node.id] = off
        if not quiet then
            publish_scroll()
            if node.watch then notify_scroll(node.id) end
        end
        request_render()
    end
end

local function set_scroll(node, off)
    -- Any direct move cancels an animation in flight, so a wheel notch or a
    -- drag during a slide takes over instead of fighting it to the end.
    stop_slide(node.id)
    set_scroll_now(node, off)
end

-- Ease a container to `target` over `dur` seconds instead of jumping. Used by
-- the carousel page buttons: a page is a whole viewport of movement, and
-- landing on it instantly gives no sense of which way the row went.
--
-- This is only safe now that nothing is punched out of the strip. An
-- occlude=True button subtracts a STATIC rect from the image below it, so
-- sliding the artwork under one made the notch crawl across the posters —
-- which is why paging used to be a jump.
local function slide_scroll(node, target, dur)
    stop_slide(node.id)
    local id = node.id
    local from = state.scroll[id] or 0
    target = clamp(target, 0, scroll_max(node))
    if dur <= 0 or math.abs(target - from) < 1 then
        set_scroll_now(node, target)
        return
    end
    -- Announce the destination up front, once. Python acts on where the row
    -- is going, not on the frames it passes through.
    scroll_dest[id] = target
    publish_scroll()
    if node.watch then notify_scroll(id) end
    local t0 = mp.get_time()
    local a = {}
    a.timer = mp.add_periodic_timer(TICK, function()
        -- Re-look up by id: a rebuild between ticks replaces the node table,
        -- and clamping against the old one's content size would be wrong.
        local cur = state.byid[id]
        if not cur or cur.t ~= 'scroll' then
            stop_slide(id)
            return
        end
        local p = (mp.get_time() - t0) / dur
        if p >= 1 then
            stop_slide(id)   -- clears scroll_dest, so the final set publishes
            set_scroll_now(cur, target)
            publish_scroll()  -- ...even if rounding already landed us there
            return
        end
        -- Ease-out cubic: leaves quickly, settles gently. Rounded because
        -- overlay positions are whole pixels — a fractional offset just
        -- jitters the strip between two of them.
        local e = 1 - (1 - p) ^ 3
        set_scroll_now(cur, math.floor(from + (target - from) * e + 0.5),
                       true)
    end)
    scroll_anim[id] = a
end

local function tb_menu_action(node, label)
    if label == 'Cut' then
        tb_key('CUT')
    elseif label == 'Copy' then
        tb_key('CTRLC')
    elseif label == 'Paste' then
        tb_key('PASTE')
    elseif label == 'Select All' then
        tb_key('CTRLA')
    end
end

local function on_mouse_move(x, y)
    phud_touch()
    -- The observer only fires on a change, so reaching here IS movement.
    state.phud.moved = true
    state.mouse.x, state.mouse.y = x, y
    if state.tb_drag then
        local node = state.byid[state.tb_drag.id]
        if node and node.t == 'textbox' then
            local tb = tb_state(node)
            tb.cursor = tb_index_at(node, tb, x)
            tb.sel = (state.tb_drag.anchor ~= tb.cursor) and
                state.tb_drag.anchor or nil
            tb_fix_shift(node, tb)
            state.cursor_on = true
            request_render()
        end
        return
    end
    if state.slider_drag then
        local node = state.byid[state.slider_drag]
        if node and node.t == 'slider' then
            slider_set_from_x(node, x)
        end
        return
    end
    if state.dd_bar_drag then
        local g = state.dd_geo
        local t = g and popup_thumb(g)
        if t then
            popup_scroll_to_y(y - state.dd_bar_drag.grab + t.h / 2, t, g)
        end
        return
    end
    if state.drag then
        local node = state.byid[state.drag.sc]
        local b = state.bars[state.drag.sc]
        if node and b then
            local range = b.h - b.thumb_h
            if range > 0 then
                -- where the grabbed point of the thumb is now
                local top = clamp(y - state.drag.grab, b.y, b.y + range)
                set_scroll(node, (top - b.y) / range * scroll_max(node))
            end
        end
        return
    end
    if state.wsize then
        -- Dragging the resize grip (see on_mouse_down). The window's
        -- top-left does not move, so window coordinates are stable and the
        -- grabbed corner simply tracks `pointer + grab offset` -- no
        -- accumulation, so a size the WM refuses cannot make the corner
        -- drift away from the cursor.
        local g = state.wsize
        local wnow = mp.get_time()
        -- A geometry write is a resize COMMAND, not bookkeeping, so this is
        -- paced to roughly a frame rather than fired per motion event.
        if wnow - g.t >= 0.016 then
            g.t = wnow
            local gw, gh = g.w, g.h
            if g.edge:find('e') then gw = x + g.dx end
            if g.edge:find('s') then gh = y + g.dy end
            -- The same floor player_window uses, so a window dragged to
            -- nothing is not one the app then refuses to reopen.
            gw = math.max(320, math.floor(gw + 0.5))
            gh = math.max(240, math.floor(gh + 0.5))
            if gw ~= g.w or gh ~= g.h then
                g.w, g.h = gw, gh
                mp.set_property('geometry', string.format('%dx%d', gw, gh))
            end
        end
        return
    end
    local node = node_at(x, y)
    -- A popup floats above the page and eats the click, so the page must
    -- not light up under it either — tiles were hover-ringing through an
    -- open dropdown, which read as though they were still clickable.
    if state.dd_open or active_menu() or state.tb_menu then
        node = nil
    end
    -- A disabled control still ABSORBS the pointer (node_at returned it,
    -- so nothing underneath lights up either) but takes no hover of its
    -- own: no ring, no tooltip, no slider hover reporting.
    if node and node.dis then
        node = nil
    end
    update_slider_hover(node)
    local id = node and node.id or nil
    if id ~= state.hover_id then
        state.hover_id = id
        -- tooltip: re-arm on every hover change; show after a delay
        if state.tip then
            state.tip = nil
        end
        if state.tip_timer then
            state.tip_timer:kill()
            state.tip_timer = nil
        end
        if node and node.tip then
            state.tip_timer = mp.add_timeout(0.5, function()
                state.tip_timer = nil
                state.tip = id
                request_render()
            end)
        end
        request_render()
    elseif state.dd_open or active_menu() or state.tb_menu or
        state.hud then
        request_render()  -- popup/menu item hover, HUD refresh
    end
end

local function dismiss_menu(idx)
    local menu_node = active_menu()
    if not menu_node then return false end
    state.menu_hidden = true  -- hide instantly; scene push confirms
    if idx ~= nil then
        send({ t = 'select', id = menu_node.id, index = idx,
               value = menu_node.items[idx + 1] })
    else
        send({ t = 'dismiss', id = menu_node.id })
    end
    request_render()
    return true
end

-- Click payload carries the modifier state of the press (shift/ctrl
-- range and additive selection in tables). Omitted when unset.
local function send_click(node)
    local m = state.mods or {}
    send({
        t = 'click', id = node.id,
        shift = m.shift or nil, ctrl = m.ctrl or nil,
    })
end

-- Hold-repeat (node.rpt): fire on press, then refire while held and
-- still over the control (leaving pauses it; returning resumes).
local REPEAT_DELAY = 0.4
local REPEAT_IVL = 0.12

local function stop_repeat()
    if state.rpt then
        state.rpt.timer:kill()
        state.rpt = nil
    end
end

local function start_repeat(node)
    stop_repeat()
    local id = node.id
    local mods = state.mods
    send_click(node)
    local function tick()
        local n = state.byid[id]
        local under = node_at(state.mouse.x, state.mouse.y)
        if n and n.click and n.rpt and under and under.id == id then
            state.mods = mods
            send_click(n)
        end
        state.rpt = { id = id, timer = mp.add_timeout(REPEAT_IVL, tick) }
    end
    state.rpt = { id = id, timer = mp.add_timeout(REPEAT_DELAY, tick) }
end

local function on_mouse_down()
    phud_touch()
    local x, y = state.mouse.x, state.mouse.y
    if state.tip then
        state.tip = nil  -- clicking dismisses the tooltip
    end
    -- the pointer takes over from spatial navigation
    state.nav = nil
    state.nav_pidx = nil
    state.nav_adjust = nil
    state.nav_rect = nil
    state.nav_pending = nil
    -- ...including from a focus the app parked but never got to apply
    state.focus_req = nil
    if state.nav_mode then
        state.nav_mode = false
        send({ t = 'nav', active = false })
    end
    -- textbox copy/paste menu eats the click first
    if state.tb_menu then
        local idx = item_at(state.tb_menu_geo, x, y)
        local tbn = state.byid[state.tb_menu.id]
        state.tb_menu = nil
        if idx ~= nil and tbn then
            tb_menu_action(tbn, tb_menu_items(tbn)[idx + 1])
        end
        request_render()
        return
    end
    -- an open context menu eats the click next
    if active_menu() then
        dismiss_menu(item_at(state.menu_geo, x, y))
        return
    end
    -- open popup eats the click first
    if state.dd_open then
        -- the scrollbar takes precedence over the row under it
        local g = state.dd_geo
        local t = g and popup_thumb(g)
        if t and x >= t.x - 4 and x <= t.x + t.w + 4
            and y >= g.y and y < g.y + g.n * g.ih then
            if y < t.y or y >= t.y + t.h then
                -- clicking the track jumps the thumb to the pointer
                popup_scroll_to_y(y, t, g)
                t = popup_thumb(g)
            end
            state.dd_bar_drag = { grab = y - t.y }
            request_render()
            return
        end
        local idx = popup_item_at(x, y)
        local node = state.byid[state.dd_open]
        state.dd_open = nil
        if idx ~= nil and node then
            local d = dd_state(node)
            d.sel = idx
            send({ t = 'select', id = node.id, index = idx,
                   value = node.items[idx + 1] })
        end
        request_render()
        return
    end
    -- The resize grip outranks the scrollbar gutter it shares a corner
    -- with. bar_at has no z-order -- it takes any scrollbar whose rect
    -- contains the point -- and a page scroller's gutter runs the full
    -- height of the window at its right edge, straight through the drawn
    -- dots. Without this, pressing the one visible resize affordance
    -- page-scrolls the list and starts a thumb drag.
    --
    -- Below the popup/menu checks above, though: a dropdown over the corner
    -- is still what the press belongs to.
    local grip = node_at(x, y)
    if grip and grip.wsize and not grip.click then
        -- mpv has no "begin resizing" command -- only Wayland has an edge
        -- zone, and it implements that itself, in the compositor's own
        -- protocol, before the press ever reaches a script. Everywhere else
        -- the window is resized the one way a client can: by writing
        -- `geometry`, which every VO treats as a resize command (see
        -- player_window._sync_window_geometry).
        --
        -- The grab is the distance from the pointer to the corner it is
        -- pulling, so the corner stays under the cursor however far the
        -- drag runs. Refused while maximized or fullscreen: there the write
        -- would silently un-maximize the window instead of resizing it,
        -- which is not what dragging a corner asks for.
        local ww = mp.get_property_number('osd-width', 0)
        local wh = mp.get_property_number('osd-height', 0)
        if ww > 0 and wh > 0
            and not mp.get_property_bool('window-maximized', false)
            and not mp.get_property_bool('fullscreen', false) then
            state.wsize = { edge = grip.wsize, w = ww, h = wh,
                            dx = ww - x, dy = wh - y, t = 0 }
        end
        return
    end
    local bar_id, b = bar_at(x, y)
    if bar_id then
        local node = state.byid[bar_id]
        if y < b.thumb_y or y > b.thumb_y + b.thumb_h then
            -- track click: jump a page towards the pointer
            local dir = y < b.thumb_y and -1 or 1
            set_scroll(node, (state.scroll[bar_id] or 0) + dir * node.h)
        end
        -- Anchored INSIDE the thumb (the pattern the dropdown's own
        -- scrollbar uses), not as a delta from a parked offset. A delta has
        -- to be multiplied by a live scroll_max, so a scroller that grows
        -- mid-drag maps the same pointer movement onto a bigger jump while
        -- the thumb simultaneously gets shorter -- and slides out from
        -- under the cursor (#617). Grabbing a point on the thumb and
        -- deriving the offset from where that point now is cannot drift,
        -- whatever the content does.
        --
        -- A track click above has already jumped a page, so re-read the
        -- thumb rather than trusting the rect the hit test was made
        -- against; if the jump moved it under the pointer, grab it there.
        local top = b.thumb_y
        if y < b.thumb_y or y > b.thumb_y + b.thumb_h then
            top = y - b.thumb_h / 2
        end
        state.drag = { sc = bar_id, grab = clamp(y - top, 0, b.thumb_h) }
        return
    end
    local node = node_at(x, y)
    if state.focus and (not node or node.id ~= state.focus) then
        blur()
    end
    if not node then
        -- click-away from an open modal dialog dismisses it
        if modal_active() then
            local m = state.modal
            if x < m.x or x >= m.x + m.w or y < m.y or
                y >= m.y + m.h then
                state.modal_hidden = true
                send({ t = 'dismiss', id = m.id })
                request_render()
            end
        elseif state.phud.mode and state.phud.shown then
            -- summoned HUD: clicking bare video toggles pause, like
            -- the lua OSC's click-anywhere behavior
            mp.commandv('cycle', 'pause')
        end
        return
    end
    if node.dis then
        -- Absorbed, not passed through: the press lands on the disabled
        -- control and stops there rather than reaching whatever it sits
        -- over. Focus was already dropped above.
        return
    end
    if node.wdrag and not node.click then
        -- Client-side title bar: drag the window. Started HERE, on the
        -- press, because that is the only moment mpv will take it -- the
        -- command means "the button the user is holding is now moving the
        -- window", so it cannot be an event round-tripped through Python.
        --
        -- `not node.click` so a bar that is also a button keeps being a
        -- button; the controls sitting on the bar are separate, higher
        -- nodes and node_at has already preferred them.
        --
        -- Inline rather than a helper, and pcall rather than a version
        -- check: begin-vo-dragging is mpv 0.38+ and a VO that cannot drag
        -- raises instead of declining. A window that will not drag is a
        -- disappointment; an error out of the mouse handler would take the
        -- renderer down and with it every other click on screen. (The main
        -- chunk is at Lua's 200-local ceiling -- see
        -- tests/test_renderer_lua.py -- so this cannot be a file-scope
        -- function anyway.)
        pcall(mp.commandv, 'begin-vo-dragging')
        return
    end
    if node.t == 'textbox' then
        focus_textbox(node)
        local tb = tb_state(node)
        -- triple-click: a plain click right after a double-click on
        -- the same box selects everything (mpv has no triple event)
        local last = state.last_dbl
        if last and last.id == node.id and
            mp.get_time() - last.t < 0.4 then
            state.last_dbl = nil
            if #tb.text > 0 then
                tb.sel = 0
                tb.cursor = #tb.text
            end
            request_render()
            return
        end
        tb.sel = nil
        tb.cursor = tb_index_at(node, tb, x)
        -- arm click-drag selection from this anchor
        state.tb_drag = { id = node.id, anchor = tb.cursor }
        state.cursor_on = true
        request_render()
        return
    end
    if node.t == 'slider' then
        state.slider_drag = node.id
        slider_set_from_x(node, x)
        return
    end
    if node.t == 'dropdown' then
        state.dd_open = node.id
        -- start the window on the current selection: opening a 50-year
        -- picker at the top hides whatever is already chosen
        state.dd_scroll = dd_state(node).sel or 0
        request_render()
        return
    end
    if node.click then
        if node.rpt then
            -- repeat buttons fire on PRESS (and refire while held);
            -- the release is swallowed in on_mouse_up
            start_repeat(node)
        end
        state.pressed = node.id
    end
end

local function on_mouse_up()
    if state.dd_bar_drag then
        state.dd_bar_drag = nil
        request_render()
        return       -- releasing the thumb must not select a row
    end
    if state.tb_drag then
        state.tb_drag = nil  -- selection (if any) stays
        return
    end
    if state.slider_drag then
        fire_slider(state.slider_drag)  -- final value, unthrottled
        local node = state.byid[state.slider_drag]
        if node then slider_commit(node) end
        state.slider_drag = nil
        return
    end
    if state.drag then
        state.drag = nil
        return
    end
    if state.wsize then
        -- The final size, unthrottled: the pace above can drop the last few
        -- pixels of a drag, and where the window ends up is the one size
        -- the user actually chose.
        state.wsize.t = 0
        on_mouse_move(state.mouse.x, state.mouse.y)
        state.wsize = nil
        return
    end
    if state.rpt then
        stop_repeat()  -- press already fired; no click on release
        state.pressed = nil
        return
    end
    if state.pressed then
        local node = node_at(state.mouse.x, state.mouse.y)
        if node and node.id == state.pressed then
            send_click(node)
        end
        state.pressed = nil
    end
end

-- e.scale carries hi-res wheel deltas (trackpads, libinput
-- button-scrolling trackballs) — honor it instead of stepping whole
-- notches. state.wheel_count feeds the debug HUD: if the counter stops
-- advancing while the device scrolls, mpv isn't delivering events (a
-- compositor/mpv issue); if it advances but nothing moves, it's ours.
local WHEEL_LOCK_S = 2.0

local function on_wheel(dir, axis, e)
    phud_touch()
    state.wheel_count = (state.wheel_count or 0) + 1
    -- Timestamp every wheel event, including the ones that go on to scroll
    -- nothing: the gap between them is what the latch below measures, and a
    -- gesture does not stop being one because a notch of it landed on a
    -- popup.
    local wnow = mp.get_time()
    local wprev = state.wheel_prev
    state.wheel_prev = wnow
    local scale = (e and e.scale) or 1
    if scale <= 0 then scale = 1 end
    if state.hud then request_render() end
    -- An open popup takes the wheel: it floats over the page, so scrolling
    -- the page under it is never what was meant.
    if state.dd_open and axis == 'y' then
        popup_scroll(dir > 0 and 3 or -3)
        return
    end
    local node = scroll_at(state.mouse.x, state.mouse.y, axis)
    local locked = false
    if node then
        state.wheel_lock = {
            id = node.id, axis = axis, t = mp.get_time(),
        }
    else
        -- Hit-test came up empty mid-gesture (observed in the field:
        -- mouse-pos can go unreliable during trackball
        -- button-scrolling). A wheel gesture keeps scrolling its last
        -- target — which is also the correct gesture semantics: the
        -- target shouldn't change under a scroll in progress.
        local lock = state.wheel_lock
        if lock and lock.axis == axis and
            mp.get_time() - lock.t < WHEEL_LOCK_S then
            local cand = state.byid[lock.id]
            if cand and cand.t == 'scroll' then
                node = cand
                locked = true
                lock.t = mp.get_time()
            end
        end
    end
    -- HUD forensics: tgt '-' = no target even via gesture lock;
    -- 'id*' = gesture lock engaged (raw hit-test failed); frozen off
    -- with a target = clamp no-op.
    state.last_wheel = {
        scale = scale,
        target = node and (node.id .. (locked and '*' or '')) or '-',
        off = node and math.floor(state.scroll[node.id] or 0) or -1,
    }
    if not node then return end
    -- Latch display quantization when this gesture is asking for frames
    -- faster than they can be drawn.
    --
    -- active_tick() is the cadence the measured frame cost affords; the
    -- slot is how often this gesture wants one, floored at snap_pace
    -- because below that cadence a glide has stopped being smooth whatever
    -- the wheel is doing. Falling behind is one being larger than the
    -- other, and it takes both: a fling on a machine that draws in 3ms
    -- keeps up and glides, which is most scrolling on most setups, and a
    -- slow nudge on one that takes 80ms is given the whole gap and also
    -- glides, because a frame that lands late but alone is latency, not
    -- stutter.
    --
    -- Which frames are dear is measured across wheel events globally rather
    -- than per container: a fling is a property of the input device, and a
    -- gesture that crosses from one scroller to another is still one flick
    -- of one wheel.
    --
    -- **Below the early returns above, though.** Quantization is a property
    -- of the whole scene -- snap_round runs for every container in
    -- compute_geo -- so latching it while the wheel is over an open popup,
    -- or over dead space, jerks whatever is behind onto a row boundary for a
    -- gesture that was never scrolling it. It also left `state.wheel_lock`
    -- pointing at whatever was scrolled *last*, which the release below then
    -- moved. Here, there is a target, and it is that target.
    --
    -- A single overrun latches it, because the release below is
    -- time-based. Requiring a sustained overrun instead would spend the
    -- first half of every fling doing the expensive thing.
    if wprev then
        local slot = wnow - wprev
        if slot < state.snap_pace then slot = state.snap_pace end
        if active_tick() > slot then
            state.snap_live = true
        end
    end
    if state.snap_live then
        -- Drop back to free scrolling once the gesture is over, moving the
        -- STORED offset onto the detent the last frame was already
        -- *showing*. Both end the quantization; this way does it without
        -- moving anything on screen. Releasing to the continuous offset
        -- instead would shift the content by up to half a row 300ms after
        -- the user stopped touching anything — which reads as a glitch, and
        -- throws away the one thing gesture snapping is good for: a fling
        -- lands aligned rather than halfway through a row.
        --
        -- Only the container the gesture was locked to. That is the one
        -- that moved, and it is already tracked for the hit-test fallback.
        if state.snap_timer then
            state.snap_timer:kill()
        end
        state.snap_timer = mp.add_timeout(GESTURE_WINDOW, function()
            state.snap_timer = nil
            if not state.snap_live then return end
            local lock = state.wheel_lock
            local cur, landed = nil, nil
            -- Same staleness rule the hit-test fallback uses: this fires a
            -- third of a second after the fact, and a scene push in between
            -- can have replaced the container this id resolves to (every
            -- library page builds a scroller called "grid"). An expired lock
            -- means there is nothing this gesture still owns.
            if lock and mp.get_time() - lock.t < WHEEL_LOCK_S then
                local target = state.byid[lock.id]
                if target and target.t == 'scroll' then
                    cur = state.scroll[target.id] or 0
                    landed = snap_round(target, cur)
                end
                state.snap_live = false
                -- After clearing the flag, so this is the settled position
                -- and not something the next frame re-rounds.
                --
                -- Only when it actually moves. set_scroll cancels any
                -- animation in flight, so landing a no-op here -- which is
                -- every container without a `snap`, where snap_round returns
                -- the offset unchanged -- would kill a carousel slide that
                -- started during the gesture and park the row short of the
                -- destination Python was already told about.
                if landed and math.abs(landed - cur) > 0.5 then
                    set_scroll(state.byid[lock.id], landed)
                end
            else
                state.snap_live = false
            end
            request_render()
        end)
    end
    -- snapped_scrolling (accessibility): each notch steps exactly one detent
    -- and the display snaps with it -- one home-screen section, or one grid
    -- row. This is the old stepped behavior, kept as an opt-in.
    if state.snapped then
        if node.snaps and #node.snaps > 0 then
            -- Explicit breakpoints (home sections): a notch steps to the
            -- adjacent stop; a hi-res/coalesced delta carries the fraction so a
            -- fling lands several sections on one frame.
            local acc = (state.snap_accum[node.id] or 0) + dir * scale
            local steps = 0
            if acc >= 1 then
                steps = math.floor(acc)
            elseif acc <= -1 then
                steps = math.ceil(acc)
            end
            state.snap_accum[node.id] = acc - steps
            if steps ~= 0 then
                local idx = snap_index(node.snaps, state.scroll[node.id] or 0)
                local tgt = clamp(idx + steps, 1, #node.snaps)
                set_scroll(node, node.snaps[tgt])
            end
            return
        end
        local step = WHEEL_STEP
        if node.snap and node.snap > 0 then step = node.snap end
        set_scroll(node, (state.scroll[node.id] or 0) + dir * step * scale)
        return
    end
    -- Default: every container scrolls by a flat pixel step (the value the
    -- settings page has always used), and compute_geo/draw snap the CONTENT to
    -- the nearest breakpoint. The stored offset stays continuous so the
    -- scrollbar glides while the picture steps -- a trackpad/trackball can't
    -- overshoot a whole row. For an equal-row grid (node.snap) the step is
    -- quantized so a whole number of notches spans exactly one row: a row
    -- height WHEEL_STEP does not divide would otherwise advance 2-3-2-3.
    -- Home sections (node.snaps, unequal) just take the raw step and land on
    -- the nearest breakpoint.
    --
    -- floor, not round: round the detent count DOWN so a tall row never turns
    -- into four tiny notches. WHEEL_STEP is then a MINIMUM granularity -- the
    -- quantized step only ever grows to divide the row evenly, never shrinks.
    local step = WHEEL_STEP
    if node.snap and node.snap > 0 then
        local n = math.max(1, math.floor(node.snap / WHEEL_STEP))
        step = node.snap / n
    end
    set_scroll(node,
        (state.scroll[node.id] or 0) + dir * step * scale)
end

-- Double-click: word-select in a textbox; a 'dbl' event for nodes
-- that registered on_dbl (fires after the two normal clicks, like Tk).
local function on_dbl()
    local x, y = state.mouse.x, state.mouse.y
    local node = node_at(x, y)
    if not node then return end
    if node.t ~= 'textbox' then
        if node.dbl then
            send({ t = 'dbl', id = node.id })
        elseif node.wdrag and not node.click then
            -- Double-clicking a title bar maximizes it, everywhere. Kept
            -- beside the drag rather than sent as a 'dbl' event so the two
            -- halves of "this is a title bar" cannot be wired up separately
            -- and drift; a bar that declared on_dbl above keeps its own
            -- meaning.
            local ok, cur = pcall(mp.get_property_bool, 'window-maximized')
            if ok then
                pcall(mp.set_property_bool, 'window-maximized', not cur)
            end
        end
        return
    end
    focus_textbox(node)
    state.tb_drag = nil  -- don't let a jitter-drag clobber this
    local tb = tb_state(node)
    if #tb.text > 0 then
        local ci = math.min(#tb.text,
            math.max(1, tb_index_at(node, tb, x) + 1))
        tb.sel = word_left(tb.text, ci)
        tb.cursor = word_right(tb.text, ci - 1)
        if tb.sel == tb.cursor then tb.sel = nil end
    end
    state.last_dbl = { t = mp.get_time(), id = node.id }
    state.cursor_on = true
    request_render()
end

local function on_rclick()
    if state.tb_menu then
        state.tb_menu = nil
        request_render()
        return
    end
    if active_menu() then
        dismiss_menu(nil)
        return
    end
    local x, y = state.mouse.x, state.mouse.y
    local node = node_at(x, y)
    if node and node.t == 'textbox' then
        -- built-in copy/paste menu; keeps any current selection
        focus_textbox(node)
        state.tb_menu = { id = node.id, x = x, y = y }
        request_render()
        return
    end
    if node and node.ctx then
        send({ t = 'context', id = node.id, x = x, y = y })
    end
end

-- Modified presses are distinct mpv keys; each variant records its
-- modifier set before the shared handlers run. The _dbl variants are
-- bound (as no-ops for modified ones) so they can't fall through to
-- the player's defaults while browsing.
local function mouse_pair(shift, ctrl)
    local function set()
        state.mods = { shift = shift, ctrl = ctrl }
    end
    return function() set(); on_mouse_up() end,
        function() set(); on_mouse_down() end
end

-- ------------------------------------------- spatial navigation (10ft)
--
-- Arrow keys walk the focusable nodes (anything clickable, plus
-- textboxes/dropdowns/sliders — inferred from the scene, no extra
-- protocol); ENTER activates. All renderer-local: geometry, scrolling
-- and the focus ring live here, Python just receives the same
-- click/select events a mouse would produce. The pointer always wins:
-- any mouse press drops key focus.

local function nav_candidates()
    compute_geo()
    local modal = modal_active()
    local out = {}
    for _, node in ipairs(state.nodes) do
        if node.t ~= 'scroll' and node.t ~= 'layer' and
            node.t ~= 'menu' and node.t ~= 'occ' and
            (not modal or node.mod) and not node.dis and
            (node.click or node.dbl or node.t == 'textbox' or
             node.t == 'dropdown' or node.t == 'slider') and
            visible(node) then
            out[#out + 1] = node
        end
    end
    return out
end

local function nav_center(node)
    local ex, ey = eff(node)
    return ex + node.w / 2, ey + node.h / 2
end

-- Asymmetric margins: scrolling back toward the start reveals extra
-- context above/left of the target — a row's heading scrolls into
-- view with its carousel instead of being clipped at the viewport top.
local NAV_LEAD = 56
local NAV_TAIL = 12

local function nav_scroll_into_view(node)
    local sc = node.sc
    while sc do
        local cont = state.byid[sc]
        if not cont or cont.t ~= 'scroll' then break end
        local off = state.scroll[sc] or 0
        local rel, ext, view
        if cont.axis == 'x' then
            rel, ext, view = node.x - cont.x, node.w, cont.w
        else
            rel, ext, view = node.y - cont.y, node.h, cont.h
        end
        if rel - NAV_LEAD < off then
            set_scroll(cont, rel - NAV_LEAD)
        elseif rel + ext + NAV_TAIL > off + view then
            set_scroll(cont, rel + ext + NAV_TAIL - view)
        end
        sc = cont.sc
    end
end

-- The set of scroll containers the node lives in, and membership test:
-- navigation prefers neighbours inside the same containers (and
-- scrolling them) before letting focus escape to fixed chrome.
local function nav_chain(node)
    local set = {}
    local sc = node.sc
    while sc do
        set[sc] = true
        local cont = state.byid[sc]
        sc = cont and cont.sc or nil
    end
    return set
end

local function nav_in_chain(chain, c)
    local sc = c.sc
    while sc do
        if chain[sc] then return true end
        local cont = state.byid[sc]
        sc = cont and cont.sc or nil
    end
    return false
end

local function nav_set(node)
    state.nav = node.id
    -- always-adjust sliders (the HUD seek bar) are live the moment
    -- focus lands: LEFT/RIGHT scrub, ENTER commits, no arming step
    state.nav_adjust = (node.t == 'slider' and node.aadj) or nil
    state.nav_scrubbed = nil
    state.nav_pending = nil
    nav_scroll_into_view(node)
    -- remember where focus was (effective coords): if the node later
    -- leaves the scene (virtualized rows dematerialize), the next
    -- press re-anchors to the nearest focusable instead of resetting
    -- to the top-left of the screen
    compute_geo()
    local ex, ey = eff(node)
    state.nav_rect = { x = ex, y = ey, w = node.w, h = node.h }
    request_render()
end

-- Land spatial-nav focus on the scene's autofocus node (play/pause).
-- Used when the first HUD scene arrives after a key summon, and when
-- the wake key takes keyboard control of a HUD that is already up.
local function phud_focus_autofocus()
    for _, node in ipairs(state.nodes or {}) do
        if node.af then
            state.phud.want_focus = nil
            nav_set(node)  -- an aadj seek bar wakes live via nav_set
            return true
        end
    end
    return false
end

-- Modality flag for the app: keyboard/remote engaged (hide carousel
-- arrows, etc). Cleared by any mouse press (see on_mouse_down).
local function set_nav_mode(on)
    if state.nav_mode == on then return end
    state.nav_mode = on
    send({ t = 'nav', active = on })
end

-- A focus request from the app (mpvtk-focus): either a node id -- the
-- search box, when a remote's search button asks for it -- or, with no
-- id, "whatever this scene nominates", which is how a page opened from a
-- remote lands on its Play button.
--
-- PARKED, not applied on the spot. The page a navigation lands on is a
-- spinner first: the node being asked for usually does not exist until a
-- later push, so a request that insisted on the current scene would do
-- nothing at all on exactly the screens it is for.
--
-- What keeps a parked request from surfacing on some unrelated page later
-- is that every user input cancels it (arrows, Tab, any mouse press) and
-- an `af` request is dropped outright once the pointer is driving --
-- landing on a page is the app's suggestion, and the person holding the
-- mouse outranks it.
--
-- Global, like blur() and phud_touch(): this chunk is at LuaJIT's ceiling
-- of 200 locals and one more top-level `local function` fails to compile
-- at load, silently, as a blank UI. Still defined here rather than
-- earlier, because it calls set_nav_mode and nav_set, which are locals.
function apply_focus_req()
    local req = state.focus_req
    if not req then return end
    if not req.id and not state.nav_mode then
        state.focus_req = nil
        return
    end
    local node
    if req.id then
        node = state.byid[req.id]
    else
        for _, n in ipairs(state.nodes or {}) do
            if n.af then node = n; break end
        end
    end
    if not node then return end     -- not on screen yet; keep waiting
    state.focus_req = nil
    -- Same pair as nav_tab. A textbox wants the keyboard, not just the
    -- ring: asking for the search box means asking to type in it. And
    -- landing anywhere else has to LEAVE a field that had it, or the
    -- arrows keep moving a caret -- which is exactly the search case,
    -- where submitting from the box is what opened the page being
    -- focused.
    if node.t == 'textbox' then
        focus_textbox(node)
    else
        blur()
    end
    set_nav_mode(true)
    nav_set(node)
    request_render()
end

-- Does candidate ``c`` overlap ``cur`` on the axis orthogonal to the
-- move? Same row for horizontal moves, same column for vertical —
-- the tier-1 requirement that stops RIGHT at the end of a carousel
-- from hopping to a diagonal tile in some other row.
local function nav_overlap(cur, c, dx)
    local ax, ay = eff(cur)
    local bx, by = eff(c)
    if dx ~= 0 then
        return ay < by + c.h and by < ay + cur.h
    end
    return ax < bx + c.w and bx < ax + cur.w
end

local function nav_pick(cur, cands, dx, dy, need_overlap, filter)
    local cx, cy = nav_center(cur)
    local best, score
    for _, c in ipairs(cands) do
        if c.id ~= cur.id and
            (not need_overlap or nav_overlap(cur, c, dx)) and
            (filter == nil or filter(c)) then
            local px, py = nav_center(c)
            local ddx, ddy = px - cx, py - cy
            local fwd = dx * ddx + dy * ddy       -- along the direction
            local orth = math.abs(dx * ddy) + math.abs(dy * ddx)
            if fwd > 0.5 and fwd >= orth * 0.3 then
                local s = fwd + 2.5 * orth
                if score == nil or s < score then best, score = c, s end
            end
        end
    end
    return best
end

-- Vertical moves are ROW-focused: find the nearest row of candidates
-- beyond the current node's edge in the direction of travel, then the
-- horizontally nearest element within that row — UP from a right-hand
-- button must land in the row directly above even when everything
-- there sits to its left (no x-overlap required).
local function nav_pick_row(cur, cands, dy, filter)
    local cx = select(1, nav_center(cur))
    local ax, ay = eff(cur)
    local function beyond(c)
        local py = select(2, nav_center(c))
        if dy < 0 then
            return py < ay, ay - py
        end
        return py > ay + cur.h, py - (ay + cur.h)
    end
    local nearest, ndist
    for _, c in ipairs(cands) do
        if c.id ~= cur.id and (filter == nil or filter(c)) then
            local ok, dist = beyond(c)
            if ok and (ndist == nil or dist < ndist) then
                nearest, ndist = c, dist
            end
        end
    end
    if not nearest then return nil end
    local _, nyy = eff(nearest)
    local best, score
    for _, c in ipairs(cands) do
        if c.id ~= cur.id and (filter == nil or filter(c)) then
            local ok = beyond(c)
            local ex, ey = eff(c)
            -- same visual row = vertical overlap with the nearest hit
            if ok and ey < nyy + nearest.h and nyy < ey + c.h then
                local s = math.abs(ex + c.w / 2 - cx)
                if score == nil or s < score then best, score = c, s end
            end
        end
    end
    return best
end

-- One entry point: row-based for vertical, overlap-confined for
-- horizontal (RIGHT stays inside its carousel row).
local function nav_choose(cur, cands, dx, dy, filter)
    if dy ~= 0 then
        return nav_pick_row(cur, cands, dy, filter)
    end
    return nav_pick(cur, cands, dx, dy, true, filter)
end

-- Wraparound (vertical only): when nothing lies further in the
-- direction anywhere, jump to the FURTHEST row the other way — UP at
-- the very top reaches the bottom bar in two presses instead of a
-- hundred DOWNs through a long list.
local function nav_wrap(cur, cands, dy)
    local cx = select(1, nav_center(cur))
    local far, fd
    for _, c in ipairs(cands) do
        if c.id ~= cur.id then
            local py = select(2, nav_center(c))
            local d = (dy < 0) and py or -py  -- UP wraps to bottom-most
            if fd == nil or d > fd then far, fd = c, d end
        end
    end
    if not far then return nil end
    local _, fy = eff(far)
    local best, score
    for _, c in ipairs(cands) do
        if c.id ~= cur.id then
            local ex, ey = eff(c)
            if ey < fy + far.h and fy < ey + c.h then
                local s = math.abs(ex + c.w / 2 - cx)
                if score == nil or s < score then best, score = c, s end
            end
        end
    end
    return best
end

local function nav_move(dx, dy)
    phud_touch()
    set_nav_mode(true)
    -- steering by hand outranks a focus the app parked
    state.focus_req = nil
    -- a focused textbox owns the arrows (caret movement) whichever
    -- forced binding mpv happens to prefer — delegate, don't navigate
    if state.focus then
        if dy == 0 then
            tb_key(dx < 0 and 'LEFT' or 'RIGHT')
        end
        return
    end
    -- popups first: UP/DOWN walk the open dropdown/menu
    local menu_node = active_menu()
    if state.dd_open or menu_node then
        local node = state.dd_open and state.byid[state.dd_open]
        local n = node and #node.items or (menu_node and #menu_node.items)
        if n and n > 0 and dy ~= 0 then
            local cur = state.nav_pidx
            if cur == nil and node then cur = dd_state(node).sel end
            state.nav_pidx = clamp((cur or 0) + dy, 0, n - 1)
            -- follow the highlight: it must not walk off the drawn window
            popup_scroll(0, state.nav_pidx)
            request_render()
        end
        return
    end
    local cur = state.nav and state.byid[state.nav]
    -- slider adjust mode: LEFT/RIGHT change the value in 5% steps
    if cur and cur.t == 'slider' and state.nav_adjust and dx ~= 0 then
        local s = sl_state(cur)
        state.nav_scrubbed = true  -- gesture begins: value is pinned
        local rng = (cur.max or 100) - (cur.min or 0)
        s.value = clamp(s.value + dx * rng * 0.05,
                        cur.min or 0, cur.max or 100)
        notify_slider(cur.id)
        request_render()
        return
    end
    if state.nav_adjust and cur and cur.t == 'slider' then
        slider_cancel(cur)  -- focus is leaving mid-adjust: revert
    end
    state.nav_adjust = nil
    local cands = nav_candidates()
    if #cands == 0 then return end
    if not cur or not state.byid[state.nav] then
        -- no current focus: re-anchor near where focus last was (the
        -- node may have been dematerialized by virtualization); with
        -- no history, take the topmost-leftmost visible focusable
        local best, score
        local r = state.nav_rect
        for _, c in ipairs(cands) do
            local cx, cy = nav_center(c)
            local s
            if r then
                local rx, ry = r.x + r.w / 2, r.y + r.h / 2
                s = (cx - rx) ^ 2 + (cy - ry) ^ 2
            else
                s = cy * 10000 + cx
            end
            if score == nil or s < score then best, score = c, s end
        end
        if best then nav_set(best) end
        return
    end
    -- tier 1: aligned candidates INSIDE the focused node's own scroll
    -- containers — fixed chrome above/below must not win while the
    -- container can still scroll to reveal an aligned neighbour
    local chain = nav_chain(cur)
    local function inside(c)
        return nav_in_chain(chain, c)
    end
    local best = nav_choose(cur, cands, dx, dy, inside)
    if best then
        nav_set(best)
        return
    end
    -- Nothing aligned in-container on screen: the neighbour may be
    -- fully clipped. Page the scroll chain along the axis of travel
    -- and retry; if the content isn't materialized yet, remember the
    -- direction and finish when the next scene arrives (reconcile).
    local want_axis = (dx ~= 0) and 'x' or 'y'
    local dirn = (dx ~= 0) and dx or dy
    local sc = cur.sc
    while sc do
        local cont = state.byid[sc]
        if not cont or cont.t ~= 'scroll' then break end
        if cont.axis == want_axis and scroll_max(cont) > 0 then
            local off = state.scroll[sc] or 0
            local view = (want_axis == 'x') and cont.w or cont.h
            local target = clamp(off + dirn * view * 0.6, 0,
                                 scroll_max(cont))
            if target ~= off then
                set_scroll(cont, target)
                best = nav_choose(cur, nav_candidates(), dx, dy,
                                  inside)
                if best then
                    nav_set(best)
                else
                    state.nav_pending = { dx = dx, dy = dy }
                end
                return
            end
        end
        sc = cont.sc
    end
    -- tier 2: the containers are exhausted — candidates anywhere
    -- (chrome, the now-playing bar). Horizontal still stays in its
    -- row: RIGHT at the end of a fully scrolled carousel does nothing
    -- rather than hopping to an arbitrary other row.
    best = nav_choose(cur, cands, dx, dy)
    if best then
        nav_set(best)
        return
    end
    -- tier 3, vertical only: wrap around to the far end
    if dy ~= 0 then
        best = nav_wrap(cur, cands, dy)
        if best then nav_set(best) end
    end
end

local function nav_activate()
    phud_touch()
    set_nav_mode(true)
    if state.focus then
        tb_key('ENTER')  -- submit, same as the textbox's own binding
        return
    end
    local menu_node = active_menu()
    if state.dd_open then
        local node = state.byid[state.dd_open]
        local idx = state.nav_pidx
        state.dd_open = nil
        state.nav_pidx = nil
        if node and idx ~= nil then
            local d = dd_state(node)
            d.sel = idx
            send({ t = 'select', id = node.id, index = idx,
                   value = node.items[idx + 1] })
        end
        request_render()
        return
    end
    if menu_node then
        local idx = state.nav_pidx
        state.nav_pidx = nil
        dismiss_menu(idx)
        return
    end
    local node = state.nav and state.byid[state.nav]
    if not node then return end
    if node.t == 'textbox' then
        focus_textbox(node)
        request_render()
    elseif node.t == 'dropdown' then
        state.dd_open = node.id
        state.nav_pidx = dd_state(node).sel
        state.dd_scroll = dd_state(node).sel or 0
        request_render()
    elseif node.t == 'slider' then
        if state.nav_adjust then
            -- commit BEFORE dropping adjust mode: sl_state treats a
            -- scrubbing slider as busy, so clearing first would let
            -- force=true snap the value back to the scene position
            -- and commit the OLD spot (the "ENTER rejects my seek" bug)
            slider_commit(node)
            -- always-adjust bars stay live for the next gesture
            state.nav_adjust = node.aadj and true or nil
        else
            state.nav_adjust = true
        end
        request_render()
    elseif node.click then
        state.mods = {}
        send_click(node)
    elseif node.dbl then
        send({ t = 'dbl', id = node.id })
    end
end

-- The MENU key (and a remote's hamburger, which the player sends as a
-- keypress) opens the context menu of the FOCUSED node — the keyboard's
-- answer to a right-click, and the only way the actions on a tile's menu
-- are reachable from ten feet away.
--
-- Anchored below the node's leading edge rather than at its centre: the
-- menu is about that node, so covering it is the one place it must not
-- open. menu_geometry clamps and flips it back into the window from
-- there, exactly as it does for a pointer near an edge.
--
-- A node with no context menu is a no-op, as right-clicking one is. So is
-- an unfocused scene: with nothing selected there is nothing for the menu
-- to be *about*, and picking a node on the user's behalf would be a
-- different gesture than the one they made.
--
-- Global for the same reason as apply_focus_req: the 200-locals ceiling.
function nav_context()
    phud_touch()
    if state.focus or state.dd_open or active_menu() or state.tb_menu then
        return          -- a popup or an edited field owns the keyboard
    end
    local node = state.nav and state.byid[state.nav]
    if not node or not node.ctx then return end
    set_nav_mode(true)
    local ex, ey = eff(node)
    send({ t = 'context', id = node.id,
           x = ex + ui_px(12), y = ey + node.h })
end

-- Linear focus order for Tab: the scene is built in document order, and
-- nav_candidates() preserves state.nodes order, so "next candidate in the
-- list" is reading order — the same sequence a form's fields sit in. Unlike
-- the arrow keys (spatial, container-aware) this is a flat cycle that wraps,
-- so Tab always reaches every field/button regardless of layout.
local function nav_tab(dir)
    phud_touch()
    state.focus_req = nil       -- as nav_move: the user is steering
    -- A popup owns the keyboard while open; leave arrows/ENTER to drive it.
    if state.dd_open or active_menu() or state.tb_menu then return end
    local cands = nav_candidates()
    if #cands == 0 then return end
    set_nav_mode(true)
    -- Anchor on the edited field if any, else the spatial-nav highlight.
    local anchor = state.focus or state.nav
    local idx
    for i, c in ipairs(cands) do
        if c.id == anchor then idx = i; break end
    end
    local nxt
    if idx == nil then
        nxt = (dir > 0) and cands[1] or cands[#cands]
    else
        nxt = cands[((idx - 1 + dir) % #cands) + 1]
    end
    -- Landing on a textbox focuses it for immediate typing (form behaviour);
    -- focus_textbox commits and unbinds any field we are leaving. For
    -- everything else, blur() commits/leaves the old field first.
    if nxt.t == 'textbox' then
        focus_textbox(nxt)
    else
        blur()
    end
    nav_set(nxt)
end

-- Keyboard scrolling: PGUP/PGDWN by a viewport, HOME/END to the ends.
--
-- The arrows are NOT here. They move focus (nav_move), and nav_scroll_into_view
-- already scrolls the container to follow it -- which is the same gesture from
-- the user's side and the thing the d-pad and the remote both depend on.
--
-- Which container? The wheel asks what is under the pointer; a keypress has no
-- pointer, so it is the focused node's own scroller, falling back to the
-- tallest scrollable in the scene -- the page body on every route that has one.
--
-- Deliberately not routed through on_wheel. Its snap/quantization latch
-- (snap_live, wheel_lock, GESTURE_WINDOW) measures the cadence of a physical
-- fling to decide whether to quantize to row boundaries; a discrete keypress
-- has no cadence to measure and no business latching it for the whole scene.
-- set_scroll already animates and already clamps.
local function key_scroll(kind)
    phud_touch()
    -- A focused textbox owns these: HOME and END are caret keys there, which
    -- is why they cannot simply be taken. Same delegation nav_move does for
    -- LEFT/RIGHT. PGUP/PGDWN are not edit keys, so tb_key ignores them.
    if state.focus then
        tb_key(kind)
        return
    end
    -- An open popup floats over the page, so scrolling what is behind it is
    -- never what was meant -- the rule on_wheel already applies to the wheel.
    local menu_node = active_menu()
    if state.dd_open or menu_node then
        local node = state.dd_open and state.byid[state.dd_open]
        local n = node and #node.items or (menu_node and #menu_node.items) or 0
        if kind == 'HOME' or kind == 'END' then
            if n > 0 then
                state.nav_pidx = (kind == 'HOME') and 0 or n - 1
                popup_scroll(0, state.nav_pidx)
            end
        else
            popup_scroll(kind == 'PGUP' and -3 or 3)
        end
        request_render()
        return
    end
    local cont
    local cur = state.nav and state.byid[state.nav]
    local sc = cur and cur.sc
    while sc do
        local c = state.byid[sc]
        if not c or c.t ~= 'scroll' then break end
        if c.axis ~= 'x' and scroll_max(c) > 0 then
            cont = c
            break
        end
        sc = c.sc
    end
    if not cont then
        -- No focus, or focus sits outside any scroller: take the biggest one
        -- the scene will let us have. modal_active/node.mod is the same gate
        -- scroll_at applies, so a dialog's backdrop stays put underneath it.
        local modal = modal_active()
        for _, node in ipairs(state.nodes) do
            if node.t == 'scroll' and node.axis ~= 'x'
                and (not modal or node.mod) and scroll_max(node) > 0
                and (not cont or node.h > cont.h) then
                cont = node
            end
        end
    end
    if not cont then return end
    if kind == 'HOME' then
        set_scroll(cont, 0)
    elseif kind == 'END' then
        set_scroll(cont, scroll_max(cont))
    else
        set_scroll(cont, (state.scroll[cont.id] or 0)
            + (kind == 'PGUP' and -cont.h or cont.h))
    end
end

local NAV_KEYS = {
    { 'UP', function() nav_move(0, -1) end },
    { 'DOWN', function() nav_move(0, 1) end },
    { 'LEFT', function() nav_move(-1, 0) end },
    { 'RIGHT', function() nav_move(1, 0) end },
    { 'ENTER', function() nav_activate() end },
    { 'TAB', function() nav_tab(1) end },
    { 'shift+TAB', function() nav_tab(-1) end },
    { 'MENU', function() nav_context() end },
    { 'PGUP', function() key_scroll('PGUP') end },
    { 'PGDWN', function() key_scroll('PGDWN') end },
    { 'HOME', function() key_scroll('HOME') end },
    { 'END', function() key_scroll('END') end },
}

local function bind_nav_keys()
    for _, k in ipairs(NAV_KEYS) do
        mp.add_forced_key_binding(k[1], 'mpvtk_nav_' .. k[1], k[2],
            { repeatable = true })
    end
    state.kb_nav = true
end

local function unbind_nav_keys()
    for _, k in ipairs(NAV_KEYS) do
        mp.remove_key_binding('mpvtk_nav_' .. k[1])
    end
    state.kb_nav = false
end

bind_nav_keys()
pcall(mp.set_property_native, 'user-data/mpvtk/active', true)

mp.set_key_bindings({
    { 'mbtn_left', mouse_pair(false, false) },
    { 'shift+mbtn_left', mouse_pair(true, false) },
    { 'ctrl+mbtn_left', mouse_pair(false, true) },
    { 'ctrl+shift+mbtn_left', mouse_pair(true, true) },
    { 'mbtn_left_dbl', function() on_dbl() end },
    { 'shift+mbtn_left_dbl', function() end },
    { 'ctrl+mbtn_left_dbl', function() end },
    { 'mbtn_right', function() end, function() on_rclick() end },
}, 'mpvtk_mouse', 'force')
-- The thumb buttons, in a section of their OWN so the playback HUD can
-- decline them (see ui_resume).
--
-- Back is routed as a synthetic ESC rather than given a handler: ESC
-- already steps out exactly one layer (scrub -> popup -> menu/dialog -> the
-- HUD, or one page off the browser's nav stack, whose base case lives in
-- Python on the player's ESC binding), and a second implementation of that
-- ladder would drift from it. Forward has no key to ride on -- nothing in
-- mpv or the app means "forward" -- so it is an event and the app decides;
-- windowless like `nav`/`hud` rather than addressed to a node, because
-- history belongs to the app and not to whatever the pointer is over. An
-- app that registers no handler simply ignores it.
--
-- Separate from mpvtk_mouse because BROWSE wants them and a summoned
-- playback HUD must not: over a film these are the buttons people have
-- bound to something of their own (mpv's playlist-prev/next, or the shim's
-- own chapter nav -- mouse_chapter_nav), and an accidental thumb press
-- taking the player away is not a trade worth making for a second way to
-- dismiss the bar. ESC is still ESC.
mp.set_key_bindings({
    { 'mbtn_back', function() end,
      function() mp.commandv('keypress', 'ESC') end },
    { 'mbtn_forward', function() end, function() send({ t = 'forward' }) end },
}, 'mpvtk_thumb', 'force')
mp.set_key_bindings({
    { 'wheel_up', function(e) on_wheel(-1, 'y', e) end },
    { 'wheel_down', function(e) on_wheel(1, 'y', e) end },
    { 'wheel_left', function(e) on_wheel(-1, 'x', e) end },
    { 'wheel_right', function(e) on_wheel(1, 'x', e) end },
    { 'shift+wheel_up', function(e) on_wheel(-1, 'x', e) end },
    { 'shift+wheel_down', function(e) on_wheel(1, 'x', e) end },
}, 'mpvtk_wheel', 'force')
-- Whether these sections may be enabled with `allow-vo-dragging`, and what
-- the user's own `input-builtin-dragging` was before we touched it. Kept on
-- `state` rather than as file-scope locals purely for the local budget --
-- renderer.lua's main chunk is at Lua's 200 ceiling (see
-- tests/test_renderer_lua.py).
--
-- mpv refuses EVERY VO drag -- our begin-vo-dragging, its own built-in
-- dragging, and on Wayland its CSD edge-resize zones -- while the pointer is
-- inside an input section that was enabled without this flag. And a section's
-- mouse area defaults to the WHOLE SCREEN (input.c: get_bind_section sets
-- mouse_area_set on a rect of INT_MIN..INT_MAX), so these three, which set no
-- mouse area at all, were enough on their own: mp_input_test_dragging said
-- "the pointer is over a script's UI" wherever the pointer was, and
-- vo_x11_begin_dragging returned without sending _NET_WM_MOVERESIZE. Measured
-- exactly that way -- the command reported success and no message was sent.
--
-- The flag hands dragging back, and hands back mpv's BUILT-IN dragging with
-- it: that starts a window move from a press-and-move anywhere and swallows
-- the click it interrupted, which over a UI means dragging a slider or a
-- scrollbar moves the window instead. So the flag is only used when the
-- built-in one can also be turned off (`input-builtin-dragging`, mpv 0.39+),
-- leaving the title bar's own begin-vo-dragging as the one thing that moves
-- the window. An older mpv keeps both off, which is where this shipped.
state.builtin_drag = mp.get_property_native('input-builtin-dragging')
state.vodrag = state.builtin_drag ~= nil and 'allow-vo-dragging' or ''

-- Take the built-in dragging away for as long as the UI owns the pointer;
-- ui_suspend gives it back for playback, where dragging the video to move
-- the window is mpv's own behaviour and nothing of ours is on screen.
if state.vodrag ~= '' then
    mp.set_property_bool('input-builtin-dragging', false)
end
mp.enable_key_bindings('mpvtk_mouse', state.vodrag)
mp.enable_key_bindings('mpvtk_wheel', state.vodrag)
-- set_key_bindings DEFINES a section; it does not enable it. Browse owns
-- the thumb buttons from the moment the renderer loads, and the app does
-- not necessarily transition mpvtk-active to get there (see below).
mp.enable_key_bindings('mpvtk_thumb', state.vodrag)

mp.add_forced_key_binding('F12', 'mpvtk_hud', function()
    state.hud = not state.hud
    request_render()
end)

mp.observe_property('mouse-pos', 'native', function(_, pos)
    if not pos then return end
    if pos.hover == false then
        -- Mid-resize the pointer is OUTSIDE the window for most of the
        -- gesture -- it has to be, since the corner only reaches it once
        -- the window has grown -- and hover goes false the instant the
        -- drag starts. The coordinates are still live: the button is held,
        -- so the implicit pointer grab keeps motion coming to us (X11) and
        -- the compositor keeps delivering to the focused surface
        -- (Wayland). Taking the branch below would both drop the event and
        -- forget where the pointer was, which stalls the resize on its
        -- first pixel and then computes a 320x240 window from x,y = -1.
        if state.wsize then
            -- Report the truth -- the pointer really is outside -- but keep
            -- tracking it. Only the coordinates matter to the drag.
            state.mouse.hover = false
            on_mouse_move(pos.x, pos.y)
            return
        end
        state.mouse.hover = false
        -- and forget WHERE it was. The coordinates outlive the pointer
        -- otherwise, and phud_busy would go on reading a mouse that had
        -- left the window as resting on the controls.
        state.mouse.x, state.mouse.y = -1, -1
        state.tip = nil
        if state.tip_timer then
            state.tip_timer:kill()
            state.tip_timer = nil
        end
        update_slider_hover(nil)
        if state.hover_id then
            state.hover_id = nil
            request_render()
        end
        return
    end
    state.mouse.hover = true
    -- Record it for BOTH branches. The idle-HUD branch below returns
    -- without reaching on_mouse_move, so a mouse summon used to leave
    -- state.mouse holding whatever was there before HUD mode -- and
    -- phud_busy would then decide "is the pointer on the controls?"
    -- against a position the pointer had left long ago.
    state.mouse.x, state.mouse.y = pos.x, pos.y
    if state.phud.mode and not state.phud.shown then
        -- HUD idle: real pointer movement summons it. The first event
        -- after entering idle only records the position (mx = -1 means
        -- unknown) so a stale delta can't insta-summon.
        local known = state.phud.mx >= 0
        local moved = known and
            (math.abs(pos.x - state.phud.mx) +
             math.abs(pos.y - state.phud.my)) > 2
        state.phud.mx, state.phud.my = pos.x, pos.y
        if moved then
            -- Pointer movement summons the full HUD, skippable segment
            -- or not: the scene draws its own Skip button, so there is
            -- nothing to withhold, and a live segment lasting a minute
            -- must not leave the mouse unable to raise the controls.
            -- (phud_summon drops the standalone overlay.)
            phud_summon('mouse')
        end
        return
    end
    on_mouse_move(pos.x, pos.y)
end)

mp.observe_property('osd-dimensions', 'native', function(_, dim)
    if not dim or dim.w < 1 then return end
    if dim.w == state.w and dim.h == state.h then return end
    state.w, state.h = dim.w, dim.h
    if not state.ready_sent then
        state.ready_sent = true
        send({ t = 'ready', w = dim.w, h = dim.h })
    else
        send({ t = 'resize', w = dim.w, h = dim.h })
    end
    request_render()
end)

-- ---------------------------------------------------------------- scene

-- How close to the end still counts as "at the end" for a follow=true
-- container. Not zero: the offset is clamped to a fractional content
-- height, so an exact compare would unstick the tail on its own rounding.
local FOLLOW_SLACK = 6

local function reconcile()
    local prev = state.byid or {}
    state.byid = {}
    for _, node in ipairs(state.nodes) do
        state.byid[node.id] = node
    end
    -- Follow containers we move ourselves. Python windowed its virtualized
    -- rows against the offset it knew about when it BUILT this scene, so a
    -- snap performed here invalidates that window and it has to be told —
    -- publish_scroll() alone only updates the property, it wakes nobody.
    -- Without this the logs panel opened blank: Python materialized rows
    -- 0-57 for offset 0, we jumped to the bottom, and the renderer drew the
    -- tail spacer with nothing in it until an unrelated rebuild happened.
    local snapped = {}
    -- clamp scroll offsets; drop state for vanished ids
    for id, off in pairs(state.scroll) do
        local node = state.byid[id]
        if node and node.t == 'scroll' then
            local max = scroll_max(node)
            -- A follow container that was parked at the end before this
            -- scene rides the new end. Measured against the PREVIOUS
            -- node's max, because the content just grew and against the
            -- new one nothing would ever look parked.
            local was = prev[id]
            if node.follow and was and was.t == 'scroll'
                    and off >= scroll_max(was) - FOLLOW_SLACK then
                state.scroll[id] = max
                if max ~= off then snapped[#snapped + 1] = id end
            else
                state.scroll[id] = clamp(off, 0, max)
            end
        else
            state.scroll[id] = nil
        end
    end
    -- A container with no offset yet is being seen for the first time (or
    -- after its id vanished). `follow` opens it at the end, like a console;
    -- `off0` opens it at a remembered position (the browser parks one on the
    -- route when you navigate away, so going back lands where you left).
    --
    -- Both are applied HERE and not by the Python side, for the same reason:
    -- the offset has to be clamped against the content height of the frame
    -- it lands in, and this is the only place that has both. And both must
    -- fire_scroll, or Python's virtualized rows stay windowed for offset 0
    -- while the view sits somewhere else -- a screenful of blank spacers.
    for _, node in ipairs(state.nodes) do
        if node.t == 'scroll' and not state.scroll[node.id] then
            local want = nil
            if node.follow then
                want = scroll_max(node)
            elseif node.off0 then
                want = clamp(node.off0, 0, scroll_max(node))
            end
            if want then
                state.scroll[node.id] = want
                if want ~= 0 then
                    snapped[#snapped + 1] = node.id
                end
            end
        end
    end
    publish_scroll()
    for _, id in ipairs(snapped) do
        -- fire_scroll, not notify_scroll: the latter throttles to
        -- SCROLL_NOTIFY_INTERVAL, and a snap is a rare one-shot whose whole
        -- point is to re-window immediately. Waiting 150ms for it would show
        -- the blank frame we are trying to avoid.
        --
        -- This only has an effect if the app registered an on_scroll for
        -- this container (that is what routes the event back into a
        -- rebuild), so `follow` requires one. Enforced by
        -- tests/test_mpvtk_virtualized_scrolls.py.
        fire_scroll(id)
    end
    for id in pairs(state.tb) do
        local node = state.byid[id]
        if not node or node.t ~= 'textbox' then state.tb[id] = nil end
    end
    for id in pairs(state.dd) do
        local node = state.byid[id]
        if not node or node.t ~= 'dropdown' then state.dd[id] = nil end
    end
    for id in pairs(state.sl) do
        local node = state.byid[id]
        if not node or node.t ~= 'slider' then state.sl[id] = nil end
    end
    if state.pv_hover and not state.byid[state.pv_hover] then
        state.pv_hover = nil  -- seek bar left the scene mid-hover
    end
    if state.hover_region and not state.byid[state.hover_region] then
        -- Scrolled away, navigated away, or the app dropped the node.
        -- Told rather than forgotten: the app is holding a chip open for it.
        send({ t = 'hover_end', id = state.hover_region })
        state.hover_region = nil
    end
    if state.slider_drag and not state.byid[state.slider_drag] then
        state.slider_drag = nil
    end
    if state.tb_drag and not state.byid[state.tb_drag.id] then
        state.tb_drag = nil
    end
    if state.tb_menu and not state.byid[state.tb_menu.id] then
        state.tb_menu = nil
    end
    if state.focus and (not state.byid[state.focus]) then blur() end
    if state.dd_open and (not state.byid[state.dd_open]) then
        state.dd_open = nil
    end
    if state.nav and not state.byid[state.nav] then
        -- focused node left the scene (route change / virtualization);
        -- nav_rect is kept so the next press re-anchors nearby
        state.nav = nil
        state.nav_adjust = nil
    end
    -- a nav move that scrolled into unmaterialized content completes
    -- here once the pushed scene contains the aligned neighbour
    if state.nav_pending and state.active then
        local p = state.nav_pending
        state.nav_pending = nil
        local cur = state.nav and state.byid[state.nav]
        if cur then
            local chain = nav_chain(cur)
            local best = nav_choose(
                cur, nav_candidates(), p.dx, p.dy,
                function(c) return nav_in_chain(chain, c) end)
            if best then nav_set(best) end
        end
    end
    -- the scene is authoritative for menu and modal presence
    state.menu_hidden = false
    state.modal_hidden = false
    state.modal = nil
    local has_menu = false
    for _, node in ipairs(state.nodes) do
        if node.t == 'menu' then has_menu = true end
        if node.t == 'layer' and node.kind == 'modal' then
            state.modal = node
        end
    end
    local want_esc = has_menu or state.modal ~= nil
    if want_esc and not state.menu_esc_bound then
        state.menu_esc_bound = true
        mp.add_forced_key_binding('ESC', 'mpvtk_menu_esc', function()
            if active_menu() then
                dismiss_menu(nil)
            elseif modal_active() then
                state.modal_hidden = true
                send({ t = 'dismiss', id = state.modal.id })
                request_render()
            end
        end)
    elseif not want_esc and state.menu_esc_bound then
        state.menu_esc_bound = false
        mp.remove_key_binding('mpvtk_menu_esc')
    end
    -- force=true resets renderer-local widget state from the scene
    for _, node in ipairs(state.nodes) do
        if node.force then
            if node.t == 'textbox' then state.tb[node.id] = nil end
            if node.t == 'dropdown' then state.dd[node.id] = nil end
        end
    end
end

mp.register_script_message('mpvtk-metrics', function(json)
    local m = utils.parse_json(json)
    if not m then return end
    measured_widths = m.widths
    kern_table = m.kern
    ui_font = m.font
    if m.mask_w then MASK_W = m.mask_w end
    request_render()
end)

mp.register_script_message('mpvtk-theme', function(json)
    -- Replace the token table. Merged over what is already there rather than
    -- assigned outright, so an app that sends a subset keeps the stock value
    -- for everything else -- and so a message that arrives malformed cannot
    -- leave the renderer with no colours at all.
    --
    -- Safe to send at any time: nothing here is cached into a scene, so a
    -- theme swap is this message plus a re-render, with no restart.
    local t = utils.parse_json(json)
    if type(t) ~= 'table' then return end
    for key, value in pairs(t) do
        if type(value) == 'string' and state.tok[key] ~= nil then
            state.tok[key] = value
        end
    end
    -- Aliases the drawing code reads directly.
    state.accent = state.tok.accent
    state.accent_soft = state.tok.accent_soft
    state.glow = t.glow == true
    request_render()
end)

-- UI scale. Scene nodes arrive already in physical pixels (Python converts
-- in mpvtk.scaling.scale_scene), but these constants live only here, so
-- they are scaled on receipt instead.
--
-- Bases are snapshotted at load so repeated messages don't compound, and
-- the declarations above keep their literal form on purpose:
-- tests/test_python_lua_constants.py pins several of them to their Python
-- twins by matching the source text.
local _SCALE_BASE = {
    WHEEL_STEP = WHEEL_STEP,
    SLIDER_PAD = SLIDER_PAD,
    PHUD_SKIP_BOTTOM = PHUD_SKIP_BOTTOM,
    PHUD_SKIP_FS = PHUD_SKIP_FS,
    PHUD_SKIP_PAD = PHUD_SKIP_PAD,
    PHUD_SKIP_RIGHT = PHUD_SKIP_RIGHT,
    NAV_LEAD = NAV_LEAD,
    NAV_TAIL = NAV_TAIL,
    FOLLOW_SLACK = FOLLOW_SLACK,
}

mp.register_script_message('mpvtk-scale', function(json)
    local t = utils.parse_json(json)
    if not t or not tonumber(t.s) then return end
    local s = tonumber(t.s)
    if s <= 0 then s = 1 end
    state.scale = s
    -- Same rounding as Python's scaling.px(); SLIDER_PAD in particular must
    -- land on the identical value or clicks seek to the wrong time.
    local function sc(v) return math.floor(v * s + 0.5) end
    WHEEL_STEP = sc(_SCALE_BASE.WHEEL_STEP)
    SLIDER_PAD = sc(_SCALE_BASE.SLIDER_PAD)
    PHUD_SKIP_BOTTOM = sc(_SCALE_BASE.PHUD_SKIP_BOTTOM)
    PHUD_SKIP_FS = sc(_SCALE_BASE.PHUD_SKIP_FS)
    PHUD_SKIP_PAD = sc(_SCALE_BASE.PHUD_SKIP_PAD)
    PHUD_SKIP_RIGHT = sc(_SCALE_BASE.PHUD_SKIP_RIGHT)
    NAV_LEAD = sc(_SCALE_BASE.NAV_LEAD)
    NAV_TAIL = sc(_SCALE_BASE.NAV_TAIL)
    FOLLOW_SLACK = sc(_SCALE_BASE.FOLLOW_SLACK)
    request_render()
end)

mp.register_script_message('mpvtk-wheel', function(json)
    -- scroll_wheel_pixels + snapped_scrolling (see conf.py). px is the LOGICAL
    -- notch step; it becomes the base WHEEL_STEP scales from, so a later (or
    -- earlier) mpvtk-scale still lands the right physical value.
    local t = utils.parse_json(json)
    if not t then return end
    local px = tonumber(t.px)
    if px and px > 0 then
        _SCALE_BASE.WHEEL_STEP = px
        WHEEL_STEP = math.floor(px * state.scale + 0.5)
    end
    if t.snapped ~= nil then
        state.snapped = t.snapped and true or false
    end
    if t.force_snap ~= nil then
        state.force_snap = t.force_snap and true or false
    end
    request_render()
end)

mp.register_script_message('mpvtk-scene', function(json)
    local scene, err = utils.parse_json(json)
    if not scene then
        msg.error('bad scene: ' .. tostring(err))
        return
    end
    state.scene = scene
    -- A scene that lands while we're yielded to playback must not repaint
    -- over the video; the app re-pushes on resume.
    state.nodes = state.active and (scene.nodes or {}) or {}
    reconcile()
    if state.phud.shown and state.phud.want_focus then
        -- first HUD scene after a key/remote summon: focus lands on
        -- the autofocus node (play/pause)
        phud_focus_autofocus()
    else
        -- a parked mpvtk-focus, now that the page it was asking for may
        -- finally be on screen
        apply_focus_req()
    end
    request_render()
end)

mp.register_script_message('mpvtk-focus', function(json)
    local t = json and utils.parse_json(json) or nil
    local id = t and t.id or nil
    state.focus_req = { id = id }
    -- Only a request that NAMES a node may be answered by the scene
    -- already up (the search box is in the chrome, on screen now).
    -- "Whatever the next scene nominates" must NOT be: navigate() sends
    -- it while the page being LEFT is still the current scene, and that
    -- page may well nominate a node of its own -- a detail page's Play
    -- button -- which would swallow the request, move focus to a node
    -- about to be reconciled away, and leave the arriving page with
    -- nothing focused. Deterministic, and only in the keyboard/remote
    -- mode the whole feature is for.
    if id then apply_focus_req() end
end)

-- Full-UI input ownership, shared by browse (mpvtk-active) and a
-- summoned playback HUD (mpvtk-hud below).
local function ui_resume(no_nav)
    -- See the section definitions above: our sections are what decide
    -- whether mpv will drag its window at all, and the flag is only safe
    -- while its built-in dragging is off.
    if state.vodrag ~= '' then
        mp.set_property_bool('input-builtin-dragging', false)
    end
    mp.enable_key_bindings('mpvtk_mouse', state.vodrag)
    mp.enable_key_bindings('mpvtk_wheel', state.vodrag)
    -- Browse takes the thumb buttons; a summoned playback HUD leaves them
    -- to whatever the user has under them. See the mpvtk_thumb bindings.
    if state.phud.mode then
        mp.disable_key_bindings('mpvtk_thumb')
    else
        mp.enable_key_bindings('mpvtk_thumb', state.vodrag)
    end
    -- no_nav: the playback HUD came up under the pointer with
    -- hud_grab_keys off — the mouse drives it and the arrows stay
    -- mpv's seek keys. Browse always takes the arrows.
    if not no_nav then bind_nav_keys() end
    mp.add_forced_key_binding('F12', 'mpvtk_hud', function()
        state.hud = not state.hud
        request_render()
    end)
end

local function ui_suspend()
    blur()                    -- drops the text-edit bindings + caret timer
    stop_repeat()
    unbind_nav_keys()         -- playback needs the arrows (seek/OSC)
    state.nav = nil
    state.nav_pidx = nil
    state.nav_adjust = nil
    state.nav_rect = nil
    state.nav_pending = nil
    state.pressed = nil
    state.tip = nil
    if state.tip_timer then
        state.tip_timer:kill()
        state.tip_timer = nil
    end
    if state.nav_mode then
        state.nav_mode = false
        send({ t = 'nav', active = false })
    end
    state.dd_open = nil
    state.tb_menu = nil
    state.modal = nil
    state.wsize = nil
    mp.disable_key_bindings('mpvtk_mouse')
    mp.disable_key_bindings('mpvtk_wheel')
    mp.disable_key_bindings('mpvtk_thumb')
    -- Give the user's own built-in dragging back: nothing of ours is on
    -- screen now, and dragging the video to move the window is what mpv
    -- does everywhere else.
    if state.vodrag ~= '' then
        mp.set_property_bool('input-builtin-dragging', state.builtin_drag)
    end
    mp.remove_key_binding('mpvtk_hud')
    state.nodes = {}
    state.byid = {}
    reconcile()
end

-- When the UI shares the player's window (see mpvtk/app.py AdoptBackend) it
-- must get completely out of the way during playback: our forced mbtn/wheel
-- sections otherwise swallow the clicks and scrolls the mpv OSC needs, so the
-- OSC looks dead even though it is drawn. 'mpvtk-active no' unbinds everything
-- and blanks the scene; 'yes' restores it. Either direction also leaves
-- HUD mode entirely (browse resuming / yielding to the lua OSC).
mp.register_script_message('mpvtk-active', function(on)
    local want = (on == 'yes' or on == 'true' or on == '1')
    phud_clear()
    -- Re-assert browse's input BEFORE the early return. phud_clear may
    -- have just left HUD mode, and the return below fires whenever `active`
    -- did not change -- which is the case both at startup (state.active
    -- begins true, so the app's first 'yes' is a no-op) and whenever browse
    -- resumes from a SUMMONED HUD (phud_summon set active itself). In both,
    -- ui_resume never runs, so anything the HUD turned off stays off for the
    -- rest of the session.
    --
    -- Two things, and they fail the same way for the same reason. The thumb
    -- section: mouse Back does nothing in the library until a playback round
    -- trip happens to hide the bar first. And the NAV KEYS: ui_suspend drops
    -- them for playback (there the arrows are mpv's seek keys) and only
    -- ui_resume puts them back, while a summoned HUD calls
    -- ui_resume(no_nav) itself -- correctly, the bar is driven by the mouse
    -- unless hud_grab_keys is on. So browse resumed with no arrow, ENTER or
    -- TAB navigation at all, from the first video played onwards. Browse
    -- always takes the arrows, so there is no no_nav case to weigh here.
    if want and not state.phud.mode then
        mp.enable_key_bindings('mpvtk_thumb', state.vodrag)
        if state.kb_saved then
            -- mpv's console has them on loan; let it give them back.
            state.kb_saved.nav = true
        else
            bind_nav_keys()
        end
    end
    if want == state.active then return end
    state.active = want
    -- mirrored so the player can route remote navigation commands to
    -- the browser's nav keys only while the UI actually owns them
    pcall(mp.set_property_native, 'user-data/mpvtk/active', want)
    if want then
        ui_resume()
    else
        ui_suspend()
    end
    request_render()
end)

-- ------------------------------------------------ playback HUD (mpvtk-hud)
-- A third lifecycle state besides active/inactive (MIGRATION.md Phase
-- 9): during video playback the renderer stays ATTACHED but IDLE —
-- blank scene, no forced input sections, only a lightweight summon
-- surface (arrow/ENTER catchers + the mouse-move observer above).
-- Summoning binds the full sections and notifies Python ({t=hud,
-- active=true}), which pushes the HUD scene; an inactivity timer
-- tears it back down to idle. While idle, every other key keeps its
-- mpv default (space pauses, q quits, …).

-- Default auto-hide delay, and the floor an explicit one is clamped
-- to. Zero is a legal setting -- it means "gone the moment the
-- pointer is not on the controls" -- but a literal zero would also
-- blink them out in the same frame as the mouse motion that
-- summoned them, so the timer never runs shorter than the floor.
-- One table, not two locals: this chunk is at LuaJIT's 200-local ceiling
-- (tests/test_renderer_lua.py reports the headroom).
local PHUD_HIDE = { def = 4, min = 0.5 }
-- How long the standalone Skip button stays up on its own after a
-- segment starts. Independent of the HUD: it runs whether or not the
-- bar is summoned, so the offer is on screen for the same window
-- either way.
local PHUD_SKIP_S = 10
local PHUD_SUMMON_KEYS = { 'UP', 'DOWN', 'LEFT', 'RIGHT', 'ENTER' }

local function phud_summon_enter()
    -- Select/ENTER wakes the HUD AND toggles pause/play (the bar
    -- comes up focused + in adjust mode via the autofocus slider)
    phud_summon('key')
    mp.commandv('cycle', 'pause')
end

local function phud_wake_key()
    return state.phud.wake_key or 'ENTER'
end

local phud_bind_summon  -- fwd: the skip overlay rebinds on hide
local phud_skip_hide, phud_skip_unbind  -- fwd: summon/hide retune them

-- Standalone Skip Intro/Credits overlay: shown for PHUD_SKIP_S when a
-- skippable segment starts (mpvtk-hud-skip from Python), whether the
-- HUD is idle or summoned. The summoned HUD scene draws its own Skip
-- button; the renderer's copy simply yields once that node exists (see
-- the draw block), which is what keeps the button from blinking out
-- during the summon round-trip.
--
-- Input follows the same split. While idle, ENTER (and remote Select,
-- routed here as a keypress) skips instead of summoning, and a click
-- skips on the button / summons elsewhere. While the HUD is up, ENTER
-- and clicks belong to the scene, whose Skip button is a real node.
local function phud_skip_bind()
    -- ENTER skips while the button is up and nothing else owns it
    -- (deterministically: any ENTER summon/wake binding is removed,
    -- not merely shadowed)
    if not state.phud.skip_show or state.phud.shown then return end
    if phud_wake_key() == 'ENTER' then
        mp.remove_key_binding('mpvtk_wake')
    end
    mp.remove_key_binding('mpvtk_summon_ENTER')
    mp.add_forced_key_binding('ENTER', 'mpvtk_skip_enter', function()
        send({ t = 'hudskip' })
        phud_skip_hide()
    end)
    state.kb_skip = true
end

-- Give ENTER back to the scene without taking the button down.
function phud_skip_unbind()
    mp.remove_key_binding('mpvtk_skip_enter')
    state.kb_skip = false
end

function phud_skip_hide()
    if state.phud.skip_timer then
        state.phud.skip_timer:kill()
        state.phud.skip_timer = nil
    end
    if not state.phud.skip_show then return end
    state.phud.skip_show = false
    phud_skip_unbind()
    if state.phud.mode and not state.phud.shown then
        -- hand ENTER back to the summon surface (add_forced with an
        -- existing name replaces, so rebinding everything is safe)
        phud_bind_summon()
    end
    request_render()
end

local function phud_skip_show()
    if not state.phud.mode or not state.phud.intro then return end
    state.phud.skip_show = true
    phud_skip_bind()
    -- (re)arm the auto-hide
    if state.phud.skip_timer then state.phud.skip_timer:kill() end
    state.phud.skip_timer = mp.add_timeout(PHUD_SKIP_S, function()
        state.phud.skip_timer = nil
        phud_skip_hide()
    end)
    request_render()
end

local function phud_bind_wake()
    -- The one key taken over while idle: summons the HUD for keyboard
    -- driving ('ENTER' also toggles pause/play on wake). Everything
    -- else keeps its mpv default unless hud_grab_keys opted in.
    local wk = phud_wake_key()
    mp.add_forced_key_binding(wk, 'mpvtk_wake',
        wk == 'ENTER' and phud_summon_enter
        or function() phud_summon('key') end)
end

function phud_bind_summon()
    phud_bind_wake()
    if state.phud.grab then
        for _, key in ipairs(PHUD_SUMMON_KEYS) do
            if key ~= phud_wake_key() then
                mp.add_forced_key_binding(key, 'mpvtk_summon_' .. key,
                    key == 'ENTER' and phud_summon_enter
                    or function() phud_summon('key') end)
            end
        end
    end
    -- clicking the hidden-HUD video pauses (the lua OSC's
    -- click-anywhere), except on the standalone skip button
    mp.add_forced_key_binding('mbtn_left', 'mpvtk_phud_click', function()
        local r = state.phud.skip_rect
        local x, y = state.phud.mx, state.phud.my
        if state.phud.skip_show and r and x >= r.x1 and x <= r.x2
            and y >= r.y1 and y <= r.y2 then
            send({ t = 'hudskip' })
            phud_skip_hide()
        else
            mp.commandv('cycle', 'pause')
        end
    end)
    state.kb_summon = true
end

local function phud_unbind_summon()
    mp.remove_key_binding('mpvtk_wake')
    for _, key in ipairs(PHUD_SUMMON_KEYS) do
        mp.remove_key_binding('mpvtk_summon_' .. key)
    end
    mp.remove_key_binding('mpvtk_phud_click')
    state.kb_summon = false
end

local function phud_disarm()
    if state.phud.timer then
        state.phud.timer:kill()
        state.phud.timer = nil
    end
end

-- Interactions the auto-hide must not interrupt; checked at expiry
-- (the timer re-arms instead of hiding). nav_adjust is deliberately NOT
-- here: the bar wakes in adjust mode by default, and an actual scrub
-- gesture pauses playback, which already holds the HUD open.
--
-- Two of these are the hud_autohide setting rather than a rule:
--
--   * The POINTER RESTING ON THE CONTROLS holds them up, in every mode but
--     'always'. Reaching for a button and then reading its tooltip must not
--     be a race against the timer, and it is what makes a short delay --
--     down to none -- usable: the controls go when you are not using them
--     rather than when you stop moving.
--   * PAUSED playback held them up unconditionally, on the reasoning that
--     hiding the controls the moment someone pauses is the opposite of what
--     pausing means. It is also how you end up unable to look at the frame
--     you paused to look at (#620), so it is now the 'paused' mode.
local function phud_busy()
    if state.dd_open ~= nil or state.modal ~= nil
        or state.tb_menu ~= nil or state.slider_drag ~= nil
        or state.pressed ~= nil
        -- A resize drag holds the button down without moving the pointer
        -- for as long as it likes, and the grip press deliberately sets no
        -- `pressed` (it is not a click). Without this the HUD auto-hides
        -- mid-gesture, ui_suspend drops the drag state, and the window
        -- stops at whatever size it had reached.
        or state.wsize ~= nil
        or active_menu() ~= nil then
        return true
    end
    if state.phud.hide_mode == 'paused'
        and mp.get_property_native('pause', false) then
        return true
    end
    if state.phud.hide_mode ~= 'always' and state.phud.moved then
        -- Over either bar: the ids are the contract (hud.py gives the top
        -- row and the transport column one for exactly this). Falling back
        -- to "over any clickable node" would flicker in the gaps between
        -- buttons, which is most of the bar's area.
        --
        -- Only once the pointer has MOVED since this summon, though. A
        -- mouse that has sat untouched in the bottom strip of the screen
        -- all evening is not reaching for anything, and taking it as a
        -- hover left a keyboard-summoned HUD up forever -- which is also
        -- how it reads under a bare X server, where the pointer parks at
        -- 0,0 and the top bar is drawn under it.
        local mx, my = state.mouse.x, state.mouse.y
        for _, id in ipairs({ 'hud-bar', 'hud-topbar' }) do
            local bar = state.byid[id]
            if bar then
                local bx, by = eff(bar)
                if mx >= bx and mx <= bx + bar.w
                    and my >= by and my <= by + bar.h then
                    return true
                end
            end
        end
    end
    return false
end

local function phud_arm()
    phud_disarm()
    local delay = math.max(PHUD_HIDE.min,
                           state.phud.hide_s or PHUD_HIDE.def)
    state.phud.timer = mp.add_timeout(delay, function()
        state.phud.timer = nil
        if not (state.phud.mode and state.phud.shown) then return end
        if phud_busy() then
            phud_arm()
        else
            phud_hide()
        end
    end)
end

function phud_touch()
    if state.phud.mode and state.phud.shown then phud_arm() end
end

-- Take keyboard control of a HUD that came up under the pointer.
-- Without this the wake key would do nothing once the mouse had
-- already raised the HUD, and there'd be no way to reach the arrows
-- with hud_grab_keys off. Unlike a cold ENTER summon this does NOT
-- toggle pause: the HUD is already visible and the user is aiming at
-- it, not blindly waking it.
local function phud_kbd_take()
    if state.phud.kbd then return end
    state.phud.kbd = true
    mp.remove_key_binding('mpvtk_wake')
    bind_nav_keys()
    -- the scene is already up, so focus now instead of waiting for
    -- the next push to consume want_focus
    if not phud_focus_autofocus() then state.phud.want_focus = true end
    phud_touch()
    request_render()
end

function phud_summon(src)
    if not state.phud.mode or state.phud.shown then return end
    -- The scene's own Skip button takes over — but only once it is
    -- actually on screen, so the overlay keeps drawing across the
    -- round-trip and the button never blinks out. ENTER goes back to
    -- the scene immediately, though: it activates the focused node.
    phud_skip_unbind()
    state.phud.shown = true
    -- A mouse summon IS movement; a key summon starts with the pointer
    -- wherever it happened to be, which means nothing.
    state.phud.moved = src == 'mouse'
    -- a keyboard/remote summon lands spatial-nav focus on the scene's
    -- autofocus node (play/pause) once Python pushes the HUD; a mouse
    -- summon leaves the pointer in charge
    state.phud.want_focus = src ~= 'mouse'
    -- Who drives the HUD. A mouse summon with hud_grab_keys off is
    -- the one case that does NOT take the arrows — merely moving the
    -- pointer must not steal mpv's seek keys.
    state.phud.kbd = src ~= 'mouse' or state.phud.grab or false
    state.active = true
    phud_unbind_summon()
    ui_resume(not state.phud.kbd)
    if not state.phud.kbd then
        -- the wake key still upgrades to keyboard driving
        mp.add_forced_key_binding(phud_wake_key(), 'mpvtk_wake',
            phud_kbd_take)
    end
    -- ESC steps back out one layer at a time: popup -> menu/dialog ->
    -- the HUD itself. (A scene-driven dialog also binds
    -- mpvtk_menu_esc, added later so it wins while it exists.)
    mp.add_forced_key_binding('ESC', 'mpvtk_phud_esc', function()
        if state.nav_adjust and state.nav_scrubbed then
            -- scrub in flight: revert it, keep the HUD up (an
            -- always-adjust bar stays live for the next gesture)
            local node = state.nav and state.byid[state.nav]
            if node and node.t == 'slider' then
                slider_cancel(node)
                if not node.aadj then state.nav_adjust = nil end
            else
                state.nav_adjust = nil
            end
            request_render()
        elseif state.dd_open then
            state.dd_open = nil
            state.nav_pidx = nil
            request_render()
        elseif active_menu() then
            dismiss_menu(nil)
        elseif modal_active() then
            state.modal_hidden = true
            send({ t = 'dismiss', id = state.modal.id })
            request_render()
        else
            phud_hide()
            return
        end
        phud_touch()
    end)
    pcall(mp.set_property_native, 'user-data/mpvtk/active', true)
    send({ t = 'hud', active = true })
    phud_arm()
    request_render()
end

function phud_hide()
    if not state.phud.shown then return end
    if state.nav_adjust and state.nav_scrubbed then
        -- a scrubbed bar reverts cleanly so the app drops its pending
        -- state / resumes a scrub-pause
        local node = state.nav and state.byid[state.nav]
        if node and node.t == 'slider' then slider_cancel(node) end
        state.nav_adjust = nil
    end
    state.phud.shown = false
    state.phud.kbd = nil
    state.phud.mx, state.phud.my = -1, -1
    phud_disarm()
    state.active = false
    mp.remove_key_binding('mpvtk_phud_esc')
    ui_suspend()
    phud_bind_summon()
    -- the scene (and its Skip button) is gone; if the segment window
    -- is still running, the overlay draws again and reclaims ENTER
    phud_skip_bind()
    pcall(mp.set_property_native, 'user-data/mpvtk/active', false)
    send({ t = 'hud', active = false })
    request_render()
end

-- Leave HUD mode entirely (called by mpvtk-active on any transition,
-- and by 'mpvtk-hud no'). Deliberately does NOT touch the full input
-- sections: the caller decides whether they stay (browse resuming
-- from a summoned HUD) or go (plain suspend).
function phud_clear()
    if not state.phud.mode then return end
    phud_skip_hide()
    state.phud.intro = nil
    state.phud.mode = false
    state.phud.shown = false
    state.phud.kbd = nil
    state.phud.mx, state.phud.my = -1, -1
    phud_disarm()
    phud_unbind_summon()
    mp.remove_key_binding('mpvtk_phud_esc')
    pcall(mp.set_property_native, 'user-data/mpvtk/hud', false)
end

mp.register_script_message('mpvtk-hud', function(on, opts_json)
    local want = (on == 'yes' or on == 'true' or on == '1')
    if want then
        -- keyboard policy travels with the engage (re-applied even
        -- when already in HUD mode, so setting changes stick)
        local opts = opts_json and utils.parse_json(opts_json) or nil
        state.phud.grab = (opts and opts.grab) or false
        state.phud.wake_key = (opts and opts.key) or 'ENTER'
        -- `or` would fold an ABSENT hide into 0, and 0 is meaningful
        -- here (it forces hover mode below). Absent means "the toolkit's
        -- own default", which is what PHUD_HIDE.def is for -- it was
        -- unreachable, so a caller that sent no opts got the 0.5s floor
        -- instead of the ~4s every docstring promises.
        local hide = opts and opts.hide
        state.phud.hide_s = hide ~= nil and hide or PHUD_HIDE.def
        state.phud.hide_mode = (opts and opts.mode) or 'hover'
        state.phud.shadow = (opts and opts.shadow) or false
        if state.phud.hide_s <= 0 then
            -- A zero delay only means anything as "gone the moment the
            -- pointer leaves the controls"; with no pointer test it is a
            -- HUD the mouse cannot summon at all.
            state.phud.hide_mode = 'hover'
        end
    end
    if want == state.phud.mode then return end
    if want then
        -- enter attached-but-idle, tearing down whatever we owned
        if state.active then
            state.active = false
            ui_suspend()
            pcall(mp.set_property_native,
                'user-data/mpvtk/active', false)
        end
        state.phud.mode = true
        state.phud.shown = false
        state.phud.intro = nil    -- Python re-pushes the live label
        state.phud.mx, state.phud.my = -1, -1
        phud_bind_summon()
        -- mirrored so the player routes remote Move*/Select here (to
        -- summon) instead of treating them as seek keys
        pcall(mp.set_property_native, 'user-data/mpvtk/hud', true)
    else
        local shown = state.phud.shown
        phud_clear()
        if shown then
            state.active = false
            ui_suspend()
            pcall(mp.set_property_native,
                'user-data/mpvtk/active', false)
        end
    end
    request_render()
end)

-- Remote navigation while the HUD is idle: the player routes
-- Move*/Select here because keypresses would hit mpv defaults (only
-- the configured wake key is grabbed). 'select' = wake + pause/play,
-- anything else = plain wake.
mp.register_script_message('mpvtk-hud-summon', function(kind)
    if not state.phud.mode or state.phud.shown then return end
    if state.phud.skip_show and kind == 'select' then
        -- overlay showing: Select accepts the skip, like local ENTER
        send({ t = 'hudskip' })
        phud_skip_hide()
    elseif kind == 'select' then
        phud_summon_enter()
    else
        phud_summon('key')
    end
end)

-- Skippable-segment label ('' = no segment). Pushed by the browser
-- from every video playstate; the player also pushes a playstate the
-- moment a segment starts/ends, so this stays within ~a pump of
-- reality. A segment start opens the PHUD_SKIP_S window regardless of
-- HUD state; whichever of the two buttons is appropriate then draws.
mp.register_script_message('mpvtk-hud-skip', function(label)
    label = (label ~= nil and label ~= '') and label or nil
    local started = label ~= nil and state.phud.intro == nil
    state.phud.intro = label
    if not state.phud.mode then return end
    if label == nil then
        phud_skip_hide()
        request_render()
    elseif started then
        -- armed whether or not the bar is up: with the HUD summoned
        -- the scene draws the button, and the window still governs
        -- what happens once the bar auto-hides
        phud_skip_show()
    end
end)

-- Trickplay tiles for the scrub preview. These three messages are the
-- TrickPlay worker's, not mpvtk's: they predate this renderer and are what
-- the lua OSCs consume (see thumbfast.lua), so nothing extra is sent for us
-- and the two consumers cannot disagree about which file is live. The
-- worker guarantees the path is unique per generation and only unlinks the
-- previous one after pointing everybody at the new one.
mp.register_script_message('shim-trickplay-bif',
    function(count, mult, w, h, path)
        state.tp = { file = path, iw = tonumber(w), ih = tonumber(h),
                     count = tonumber(count), mult = tonumber(mult) }
        request_render()
    end)

-- The fallback when the server has no BIF data: one frame per chapter,
-- indexed by the chapter start times (seconds) rather than by a cadence.
mp.register_script_message('shim-trickplay-chapters',
    function(w, h, path, times)
        local t = {}
        for s in tostring(times or ''):gmatch('[^,]+') do
            t[#t + 1] = tonumber(s)
        end
        state.tp = { file = path, iw = tonumber(w), ih = tonumber(h),
                     times = t }
        request_render()
    end)

mp.register_script_message('shim-trickplay-clear', function()
    -- The bytes go away right after this, so forget them first.
    state.tp = nil
    request_render()
end)

-- The bubble's caption. mpv has the chapters already; asking Python for
-- them would be a round trip for something sitting in a property.
mp.observe_property('chapter-list', 'native', function(_, v)
    state.chlist = v
end)

-- ---------------------------------------------------------- test hooks

local function center_of(id)
    local node = state.byid[id]
    if not node then return nil end
    compute_geo()
    local ex, ey, clip = eff(node)
    local x, y = ex + node.w / 2, ey + node.h / 2
    if clip then
        x = clamp(x, clip.x1 + 1, clip.x2 - 1)
        y = clamp(y, clip.y1 + 1, clip.y2 - 1)
    end
    return x, y
end

mp.register_script_message('mpvtk-scroll', function(json)
    -- Move a scroll container (by id) along its axis. Two forms:
    --   {id=..., to=px}   absolute (clamped) — what the carousel page
    --                     buttons use, because aligning the target to item
    --                     boundaries needs the tile pitch, which only
    --                     Python knows (tile_renderer.page_target).
    --                     Optional `ms` eases into it instead of jumping.
    --   {id=..., dir=+-1} page by ~90% of the viewport, for callers with
    --                     no item grid to align to
    local cmd = utils.parse_json(json)
    if not cmd or not cmd.id then return end
    local node = state.byid[cmd.id]
    if not node or node.t ~= 'scroll' then return end
    if cmd.to ~= nil then
        slide_scroll(node, tonumber(cmd.to) or 0,
                     (tonumber(cmd.ms) or 0) / 1000)
        return
    end
    local page = ((node.axis == 'x') and node.w or node.h) * 0.9
    set_scroll(node, (state.scroll[cmd.id] or 0) + (cmd.dir or 1) * page)
end)

mp.register_script_message('mpvtk-debug', function(json)
    local cmd = utils.parse_json(json)
    if not cmd then return end
    if cmd.cmd == 'hover' then
        local x, y = center_of(cmd.id)
        if x then on_mouse_move(x, y) end
    elseif cmd.cmd == 'click' then
        local x, y
        if cmd.id then
            x, y = center_of(cmd.id)
        else
            x, y = cmd.x, cmd.y
        end
        if x then
            state.mods = {
                shift = cmd.shift or false, ctrl = cmd.ctrl or false,
            }
            on_mouse_move(x, y)
            on_mouse_down()
            on_mouse_up()
            state.mods = {}
        end
    elseif cmd.cmd == 'nav' then
        -- spatial navigation: {cmd=nav, dir=up|down|left|right} moves,
        -- {cmd=nav, action=enter} activates, {cmd=nav, id=...} focuses
        if cmd.id then
            local node = state.byid[cmd.id]
            if node then nav_set(node) end
        elseif cmd.action == 'enter' then
            nav_activate()
        elseif cmd.dir then
            local d = { up = { 0, -1 }, down = { 0, 1 },
                        left = { -1, 0 }, right = { 1, 0 } }
            local v = d[cmd.dir]
            if v then nav_move(v[1], v[2]) end
        end
    elseif cmd.cmd == 'down' or cmd.cmd == 'up' then
        -- separate press/release (hold-repeat tests). Raw x/y instead of
        -- an id, for things with no node of their own (a scrollbar thumb).
        local x, y = cmd.x, cmd.y
        if x == nil then x, y = center_of(cmd.id) end
        if x then
            on_mouse_move(x, y)
            if cmd.cmd == 'down' then on_mouse_down()
            else on_mouse_up() end
        end
    elseif cmd.cmd == 'moveto' then
        local x, y = cmd.x, cmd.y
        if x == nil and cmd.id then x, y = center_of(cmd.id) end
        if x then on_mouse_move(x, y) end
    elseif cmd.cmd == 'hud' then
        state.hud = not state.hud
        request_render()
    elseif cmd.cmd == 'wheel' then
        if cmd.id then
            local x, y = center_of(cmd.id)
            if x then on_mouse_move(x, y) end
        end
        for _ = 1, cmd.steps or 1 do
            on_wheel(cmd.dir or 1, cmd.axis)
        end
    elseif cmd.cmd == 'rclick' then
        local x, y = center_of(cmd.id)
        if x then
            on_mouse_move(x, y)
            on_rclick()
        end
    elseif cmd.cmd == 'menu' then
        -- click item #index (0-based) of the open context menu
        local g = state.menu_geo
        if g then
            local x = g.x + g.w / 2
            local y = g.y + ((cmd.index or 0) + 0.5) * g.ih
            on_mouse_move(x, y)
            on_mouse_down()
            on_mouse_up()
        end
    elseif cmd.cmd == 'tbdrag' then
        -- simulate a click-drag selection from char a to char b
        local node = state.byid[cmd.id]
        if node and node.t == 'textbox' then
            local tb = tb_state(node)
            local ex, ey = eff(node)
            local cy = ey + node.h / 2
            local pad = 10
            local xa = ex + pad - tb.shift +
                tb_text_w(node, tb.text, cmd.a or 0)
            local xb = ex + pad - tb.shift +
                tb_text_w(node, tb.text, cmd.b or 0)
            on_mouse_move(xa, cy)
            on_mouse_down()
            on_mouse_move(xb, cy)
            on_mouse_up()
        end
    elseif cmd.cmd == 'dbl' or cmd.cmd == 'triple' then
        -- double/triple-click at char cell `at` of a textbox, or a
        -- plain double-click on any other node (dbl event)
        local node = state.byid[cmd.id]
        if node and node.t ~= 'textbox' then
            local x, y = center_of(cmd.id)
            if x then
                on_mouse_move(x, y)
                on_dbl()
            end
        elseif node then
            local tb = tb_state(node)
            local ex, ey = eff(node)
            local at = cmd.at or 0
            local xa = ex + 10 - tb.shift +
                (tb_text_w(node, tb.text, at) +
                 tb_text_w(node, tb.text,
                     math.min(#tb.text, at + 1))) / 2
            on_mouse_move(xa, ey + node.h / 2)
            on_dbl()
            if cmd.cmd == 'triple' then
                on_mouse_down()
                on_mouse_up()
            end
        end
    elseif cmd.cmd == 'tbmenu' then
        -- click item #index of the open textbox copy/paste menu
        local g = state.tb_menu_geo
        if g then
            local x = g.x + g.w / 2
            local y = g.y + ((cmd.index or 0) + 0.5) * g.ih
            on_mouse_move(x, y)
            on_mouse_down()
            on_mouse_up()
        end
    elseif cmd.cmd == 'popup' then
        -- click item #index (0-based) of the open dropdown popup. The
        -- popup shows a window into the list, so scroll the target into
        -- view first and click its VISIBLE row.
        local idx = cmd.index or 0
        popup_scroll(0, idx)
        render()
        local g = state.dd_geo
        if g then
            local x = g.x + g.w / 2
            local y = g.y + (idx - (g.off or 0) + 0.5) * g.ih
            on_mouse_move(x, y)
            on_mouse_down()
            on_mouse_up()
        end
    elseif cmd.cmd == 'text' then
        -- Through the real any_unicode handler, not tb_insert: the point of
        -- a debug hook is to stand in for the input it simulates.
        for c in tostring(cmd.s):gmatch('.') do
            tb_key_text({ event = 'down', key_text = c })
        end
    elseif cmd.cmd == 'key' then
        tb_key(cmd.name)
    elseif cmd.cmd == 'state' then
        local ov = {}
        for s = 0, MAX_OVERLAYS - 1 do
            local k = state.ov_keys[s]
            if k then ov[k] = s end
        end
        send({
            t = 'debug_state',
            w = state.w, h = state.h,
            hover = state.hover_id,
            focus = state.focus,
            dd_open = state.dd_open,
            menu_open = active_menu() ~= nil,
            modal_open = modal_active(),
            tb_menu = state.tb_menu ~= nil,
            sliders = state.sl,
            has_metrics = measured_widths ~= nil,
            font = ui_font,
            wheel_count = state.wheel_count or 0,
            scroll = state.scroll,
            overlays = state.ov_used,
            ov = ov,
            tip = state.tip_geo and state.tip_geo.text or nil,
            nav = state.nav,
            nav_pidx = state.nav_pidx,
            dd_geo = state.dd_geo and {
                x = state.dd_geo.x, w = state.dd_geo.w,
                y = state.dd_geo.y, ih = state.dd_geo.ih,
                n = state.dd_geo.n, count = state.dd_geo.count,
                off = state.dd_geo.off,
            } or nil,
            tb = state.tb,
            -- scrollbar geometry per container ({x, y, w, h, thumb_y,
            -- thumb_h}), so a test can grab a thumb where it is drawn
            bars = state.bars,
            active = state.active,
            phud_mode = state.phud.mode,
            phud_shown = state.phud.shown,
            phud_intro = state.phud.intro,
            phud_skip = state.phud.skip_show or false,
            -- the scrub preview bubble ({x, y, w, h, secs, frame}), nil
            -- when it is not up. It is not a scene node, so this is the
            -- only way anything outside the renderer can see it.
            preview = state.pv_rect,
            -- is the HUD taking the arrow keys (keyboard-driven), or
            -- only the pointer (hud_grab_keys off, mouse summon)?
            phud_kbd = state.phud.kbd or false,
            mouse = { x = state.mouse.x, y = state.mouse.y,
                      hover = state.mouse.hover },
        })
    elseif cmd.cmd == 'phud' then
        -- drive the playback-HUD lifecycle from tests
        if cmd.action == 'summon' then
            phud_summon()
        elseif cmd.action == 'hide' then
            phud_hide()
        elseif cmd.action == 'mousemove' then
            -- synthetic idle-pointer movement (mouse-pos can't be
            -- injected reliably under headless X)
            state.phud.mx, state.phud.my = cmd.x or 10, cmd.y or 10
            if state.phud.mode and not state.phud.shown then
                phud_summon('mouse')
            end
        end
    end
end)

-- mpv's console (`) wants the keyboard, but our ENTER and arrow bindings
-- are FORCED and outrank it: typing a command and pressing ENTER summoned
-- the HUD (and toggled pause) instead of running the command, with no way
-- back but ESC. Hand the keys over for as long as the console is up, and
-- take them again when it closes.
--
-- Restored from what was actually bound rather than re-derived: which of
-- the three groups is live depends on browse-vs-HUD, hud_grab_keys and
-- whether the standalone Skip button is showing, and a second copy of that
-- decision would drift from ui_resume's. The mouse and wheel sections stay
-- put -- the console does not want them, and mpvtk_mouse is what dismisses
-- a stray click.
--
-- Everything hangs off `state` and the handler is anonymous deliberately:
-- this chunk is at 192 of LuaJIT's 200 top-level locals, and going over
-- does not fail at the call, it fails to load the renderer at all.
--
-- The property is mpv >= 0.38 and reads nil both before the console is
-- first opened and on builds without it. nil is falsy, which is the right
-- answer for "the console is not up" in either case.
mp.observe_property('user-data/mpv/console/open', 'bool', function(_, open)
    if open then
        if state.kb_saved then return end
        state.kb_saved = { nav = state.kb_nav, summon = state.kb_summon,
                           skip = state.kb_skip }
        if state.kb_nav then unbind_nav_keys() end
        if state.kb_summon then phud_unbind_summon() end
        if state.kb_skip then phud_skip_unbind() end
    elseif state.kb_saved then
        local was = state.kb_saved
        state.kb_saved = nil
        if was.nav then bind_nav_keys() end
        if was.summon then phud_bind_summon() end
        if was.skip then phud_skip_bind() end
    end
end)
