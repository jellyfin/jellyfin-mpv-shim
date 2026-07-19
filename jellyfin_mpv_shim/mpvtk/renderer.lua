-- mpvtk renderer: draws a declarative scene pushed from Python and owns
-- all per-frame interaction (hover, scrolling, text editing, dropdowns)
-- so no Python round-trip happens during drawing.
--
-- Protocol (script-messages):
--   mpvtk-scene  (py -> lua): JSON scene, see layout.py for node shapes.
--   mpvtk-event  (lua -> py): JSON events:
--       {t=ready|resize, w, h}
--       {t=click, id} {t=change|submit, id, value}
--       {t=select, id, index, value}
--       {t=debug_state, ...} (reply to mpvtk-debug state)
--   mpvtk-debug  (py -> lua): test hooks, JSON:
--       {cmd=hover|click, id=...} {cmd=wheel, id=..., dir=1|-1, steps=n}
--       {cmd=text, s="..."} {cmd=key, name="BS"} {cmd=state}
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

local TICK = 0.03
local WHEEL_STEP = 80
local MAX_OVERLAYS = 63

local state = {
    scene = nil,
    nodes = {},
    byid = {},
    w = 0, h = 0,
    ready_sent = false,
    mouse = { x = -1, y = -1, hover = false },
    hover_id = nil,
    pressed = nil,          -- node id armed by mbtn down
    drag = nil,             -- {sc=id, axis, start_m, start_off} scrollbar drag
    scroll = {},            -- id -> offset (px)
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

-- Heuristic fallback — keep in sync with layout.py. Replaced at
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

local function text_w(s, size, bold)
    local w = 0
    local prev = nil
    for c in s:gmatch(U8PAT) do
        if prev then w = w + kern_w(prev, c) end
        w = w + char_w(c)
        prev = c
    end
    w = w * size
    if bold then w = w * 1.04 end
    return w
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

local function request_render()
    if state.tick_timer == nil then
        state.tick_timer = mp.add_timeout(0, function()
            state.tick_last = mp.get_time()
            render()
        end)
    end
    if not state.tick_timer:is_enabled() then
        local to = TICK - (mp.get_time() - state.tick_last)
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
            local off = state.scroll[node.id] or 0
            state.geo[node.id] = {
                dx = dx + (node.axis == 'x' and off or 0),
                dy = dy + (node.axis == 'y' and off or 0),
                x1 = vx1, y1 = vy1, x2 = vx2, y2 = vy2,
            }
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

local function clip_tag(clip)
    if not clip then return '' end
    return string.format('\\clip(%.1f,%.1f,%.1f,%.1f)',
        clip.x1, clip.y1, clip.x2, clip.y2)
end

local function hover_style(node)
    if node.hover and state.hover_id == node.id then
        return node.hover
    end
    return nil
end

local function draw_rect(ass, x, y, w, h, o)
    -- o: {fill, a, radius, bc, bw, clip}
    ass:new_event()
    ass:append('{\\pos(0,0)\\an7\\bord0\\shad0')
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
    ass:append(string.format(
        '{\\an%d\\pos(%.1f,%.1f)\\fs%d\\bord0\\shad0\\1c%s\\1a&H00&%s%s%s%s}',
        an, px, ey + node.h / 2, node.size,
        ass_color(color), node.bold and '\\b1' or '',
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
        '{\\pos(%.1f,%.1f)\\an7\\bord0\\shad0\\1c%s\\1a&H00&' ..
        '\\fscx%.2f\\fscy%.2f%s\\p1}',
        x, y, ass_color(color), scale, scale, clip_tag(clip)))
    ass:append(path)
    ass:append('{\\p0}')
end

local overlay_list  -- rebuilt per render: {slot -> args}
local occluders     -- rects (popup) that must appear above images

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

local function draw_image(node, ex, ey, clip)
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
            local offset = sy * stride + sx * 4
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
    local next_free = 1
    for _, ov in ipairs(overlay_list) do
        local slot = state.ov_slots[ov.key]
        if slot == nil then
            for s = 0, MAX_OVERLAYS - 1 do
                if state.ov_keys[s] == nil then
                    slot = s
                    break
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
        if slot ~= nil then
            -- v busts the cache when a file was rewritten in place
            local argstr = table.concat(ov.args, '\0') ..
                '\0' .. (ov.v or 0)
            if state.ov_last[slot] ~= argstr then
                state.ov_last[slot] = argstr
                mp.commandv('overlay-add', tostring(slot),
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

local function tb_menu_geometry(items)
    local n = #items
    local ih = 34
    local w = 40
    for _, item in ipairs(items) do
        w = math.max(w, text_w(item, 18) + 32)
    end
    local x = clamp(state.tb_menu.x, 0, math.max(0, state.w - w - 4))
    local y = state.tb_menu.y
    if y + n * ih > state.h and y - n * ih >= 0 then
        y = y - n * ih
    end
    y = clamp(y, 0, math.max(0, state.h - n * ih))
    return { x = x, y = y, w = w, ih = ih, n = n }
end

local function draw_textbox(ass, node, ex, ey, clip)
    local tb = state.tb[node.id]
    local focused = state.focus == node.id
    draw_rect(ass, ex, ey, node.w, node.h, {
        fill = '2a2a2a', radius = 6,
        bc = focused and '7aa2f7' or '444444',
        bw = focused and 2 or 1, clip = clip,
    })
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
        draw_text(ass, tnode, ex + pad, ey, inner, node.ph, '777777')
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
            { fill = '3d59a1', a = 200, clip = inner })
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
            'eeeeee', nil, true)
    else
        draw_text(ass, tnode, x0, ey, inner, disp, 'eeeeee')
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

local function draw_dropdown(ass, node, ex, ey, clip)
    local d = dd_state(node)
    local open = state.dd_open == node.id
    draw_rect(ass, ex, ey, node.w, node.h, {
        fill = '2a2a2a', radius = 6,
        bc = open and '7aa2f7' or '444444', bw = 1, clip = clip,
    })
    local label = node.items[d.sel + 1] or ''
    local indent = 0
    local ipath = node.icons and node.icons[d.sel + 1]
    if ipath and ipath ~= '' then
        local isz = math.floor(node.size * 1.1)
        draw_icon_path(ass, ipath, ex + 8, ey + (node.h - isz) / 2,
            isz, 'cccccc', clip)
        indent = isz + 6
    end
    local tnode = {
        w = node.w - 40 - indent, h = node.h, size = node.size,
        align = 'left',
    }
    draw_text(ass, tnode, ex + 10 + indent, ey, clip, label, 'eeeeee')
    -- arrow
    local ax = ex + node.w - 22
    local ay = ey + node.h / 2 - 2
    ass:new_event()
    ass:append(string.format(
        '{\\pos(0,0)\\an7\\bord0\\shad0\\1c%s\\1a&H00&%s}',
        ass_color('aaaaaa'), clip_tag(clip)))
    ass:draw_start()
    ass:move_to(ax, ay)
    ass:line_to(ax + 12, ay)
    ass:line_to(ax + 6, ay + 7)
    ass:draw_stop()
end

local function popup_geometry(node)
    local ex, ey = eff(node)
    local ih = node.h
    local n = #node.items
    local total = n * ih
    local py = ey + node.h + 4
    if py + total > state.h and ey - 4 - total >= 0 then
        py = ey - 4 - total
    end
    return { x = ex, y = py, w = node.w, ih = ih, n = n }
end

-- Generic floating list (dropdown popups, context menus). sel may be
-- nil; icons is an optional parallel list of unit-canvas ASS paths
-- ('' = none).
local function draw_list(ass, g, items, sel, size, icons)
    draw_rect(ass, g.x, g.y, g.w, g.n * g.ih, {
        fill = '222222', radius = 6, bc = '555555', bw = 1,
    })
    local isz = math.floor(size * 1.1)
    local indent = icons and (isz + 10) or 0
    for i, item in ipairs(items) do
        local iy = g.y + (i - 1) * g.ih
        local hovered = state.mouse.x >= g.x and
            state.mouse.x <= g.x + g.w and
            state.mouse.y >= iy and state.mouse.y < iy + g.ih
        if hovered or (sel ~= nil and (i - 1) == sel) then
            draw_rect(ass, g.x + 2, iy + 1, g.w - 4, g.ih - 2, {
                fill = hovered and '3d59a1' or '333333', radius = 4,
            })
        end
        if icons and icons[i] and icons[i] ~= '' then
            draw_icon_path(ass, icons[i], g.x + 8,
                iy + (g.ih - isz) / 2, isz, 'cccccc', nil)
        end
        local tnode = { w = g.w - 20 - indent, h = g.ih, size = size,
                        align = 'left' }
        draw_text(ass, tnode, g.x + 10 + indent, iy, nil, item,
            'eeeeee')
    end
end

local function draw_popup(ass, node)
    local d = dd_state(node)
    local g = state.dd_geo or popup_geometry(node)
    draw_list(ass, g, node.items, d.sel, node.size, node.icons)
end

local function menu_geometry(node)
    local n = #node.items
    local ih = node.ih
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
        nil, node.size, node.icons)
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
    if s == nil or node.force then
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

local function draw_slider(ass, node, ex, ey, clip)
    local s = sl_state(node)
    local rng = (node.max or 100) - (node.min or 0)
    local frac = 0
    if rng > 0 then
        frac = clamp((s.value - (node.min or 0)) / rng, 0, 1)
    end
    local tx1 = ex + SLIDER_PAD
    local tw = node.w - 2 * SLIDER_PAD
    local ty = ey + node.h / 2
    draw_rect(ass, tx1, ty - 3, tw, 6,
        { fill = '3a3a3a', radius = 3, clip = clip })
    if frac > 0 then
        draw_rect(ass, tx1, ty - 3, tw * frac, 6,
            { fill = '7aa2f7', radius = 3, clip = clip })
    end
    draw_rect(ass, tx1 + tw * frac - 8, ty - 8, 16, 16,
        { fill = 'dddddd', radius = 8, clip = clip })
end

local function slider_set_from_x(node, x)
    local ex = select(1, eff(node))
    local frac = clamp(
        (x - ex - SLIDER_PAD) / (node.w - 2 * SLIDER_PAD), 0, 1)
    local s = sl_state(node)
    local v = (node.min or 0) +
        frac * ((node.max or 100) - (node.min or 0))
    if v ~= s.value then
        s.value = v
        notify_slider(node.id)
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
    local track_x = node.x + node.w - 8
    local p = node.sc and state.geo[node.sc]
    if p then track_x = track_x - p.dx end
    local ty = node.y - (p and p.dy or 0)
    local th = node.h
    local frac = node.h / node.ch
    local thumb_h = math.max(24, th * frac)
    local off = state.scroll[node.id] or 0
    local thumb_y = ty + (th - thumb_h) * (off / maxs)
    local clip = p and { x1 = x1, y1 = y1, x2 = x2, y2 = y2 } or nil
    draw_rect(ass, track_x, ty, 6, th,
        { fill = '2a2a2a', radius = 3, clip = clip })
    draw_rect(ass, track_x, thumb_y, 6, thumb_h,
        { fill = '666666', radius = 3, clip = clip })
    state.bars[node.id] = {
        x = track_x, y = ty, w = 6, h = th,
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
    busy_visible = false
    local function draw_node(ass, node)
        local ex, ey, clip = eff(node)
        if node.t == 'rect' then
            local hs = hover_style(node)
            if node.ring then
                -- hit-rect over a bitmap: only a hover ring, outside
                if hs and hs.bc then
                    draw_rect(ass, ex - 2, ey - 2,
                        node.w + 4, node.h + 4, {
                            bc = hs.bc, bw = hs.bw or 3,
                            radius = 3, clip = clip,
                        })
                end
            elseif node.fill or node.bc or hs then
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
            draw_image(node, ex, ey, clip)
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
        elseif node.t == 'icon' then
            local hs = hover_style(node)
            draw_icon_path(ass, node.path, ex, ey,
                math.min(node.w, node.h),
                (hs and hs.c) or node.c or 'eeeeee', clip)
        end
    end
    local ass = assdraw.ass_new()
    local skip = { scroll = true, menu = true, layer = true }
    for _, node in ipairs(state.nodes) do
        if not skip[node.t] and not node.top and visible(node) then
            draw_node(ass, node)
        end
    end
    for _, node in ipairs(state.nodes) do
        if node.t == 'scroll' and not node.top then
            draw_scrollbar(ass, node)
        end
    end
    -- top layer: dialog / toast content above everything in flow
    for _, node in ipairs(state.nodes) do
        if node.top and not skip[node.t] and
            not (node.mod and state.modal_hidden) and visible(node) then
            draw_node(ass, node)
        end
    end
    for _, node in ipairs(state.nodes) do
        if node.t == 'scroll' and node.top and
            not (node.mod and state.modal_hidden) then
            draw_scrollbar(ass, node)
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
            draw_list(ass, state.tb_menu_geo, tb_menu_items(tbn),
                nil, 18)
        end
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
    if state.hud then
        -- input-diagnostics overlay (toggle: F12)
        local lw = state.last_wheel or {}
        local tnode = { w = 560, h = 24, size = 16, align = 'right' }
        draw_text(ass, tnode, state.w - 570, 4, nil,
            string.format(
                'wheel:%d s:%.2f tgt:%s off:%s | mouse:%d,%d hover:%s',
                state.wheel_count or 0, lw.scale or 0,
                tostring(lw.target), tostring(lw.off),
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
            (node.click or node.ctx or node.t == 'textbox' or
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
    return math.floor((y - g.y) / g.ih)
end

-- ---------------------------------------------------------- text editing

local text_keys_bound = false

local function tb_state(node)
    local tb = state.tb[node.id]
    if tb == nil or node.force then
        tb = { text = node.text or '', cursor = #(node.text or ''),
               shift = 0 }
        state.tb[node.id] = tb
    end
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
            pcall(mp.set_property, 'clipboard/text',
                tb.text:sub(a + 1, b))
        end
    elseif name == 'CUT' then
        if tb.sel and tb.sel ~= tb.cursor then
            if not node.mask then
                local a = math.min(tb.sel, tb.cursor)
                local b = math.max(tb.sel, tb.cursor)
                pcall(mp.set_property, 'clipboard/text',
                    tb.text:sub(a + 1, b))
            end
            tb_del_selection(tb)
            tb_changed(node, tb)
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
    elseif name == 'ESC' then
        if state.tb_menu then
            state.tb_menu = nil
            request_render()
        else
            blur()  -- luacheck: ignore (fwd-declared below)
        end
    elseif name == 'PASTE' then
        local ok, clip = pcall(mp.get_property, 'clipboard/text')
        if ok and clip and clip ~= '' then
            tb_insert(clip:gsub('[\r\n]', ' '))
        end
    end
end

local text_key_names = {}

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
        function(e)
            if not e or e.event == 'up' then return end
            local t = e.key_text
            if not t or t == '' or t:byte(1) < 0x20 then return end
            tb_insert(t)
        end, { repeatable = true, complex = true })
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
            offset = state.scroll[id] or 0,
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

local function set_scroll(node, off)
    off = clamp(off, 0, scroll_max(node))
    if state.scroll[node.id] ~= off then
        state.scroll[node.id] = off
        if node.watch then notify_scroll(node.id) end
        request_render()
    end
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
    if state.drag then
        local node = state.byid[state.drag.sc]
        local b = state.bars[state.drag.sc]
        if node and b then
            local maxs = scroll_max(node)
            local range = b.h - b.thumb_h
            if range > 0 then
                local delta = (y - state.drag.start_m) / range * maxs
                set_scroll(node, state.drag.start_off + delta)
            end
        end
        return
    end
    local node = node_at(x, y)
    local id = node and node.id or nil
    if id ~= state.hover_id then
        state.hover_id = id
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

local function on_mouse_down()
    local x, y = state.mouse.x, state.mouse.y
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
    local bar_id, b = bar_at(x, y)
    if bar_id then
        local node = state.byid[bar_id]
        if y < b.thumb_y or y > b.thumb_y + b.thumb_h then
            -- track click: jump a page towards the pointer
            local dir = y < b.thumb_y and -1 or 1
            set_scroll(node, (state.scroll[bar_id] or 0) + dir * node.h)
        end
        state.drag = {
            sc = bar_id,
            start_m = y,
            start_off = state.scroll[bar_id] or 0,
        }
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
        end
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
        request_render()
        return
    end
    if node.click then
        state.pressed = node.id
    end
end

local function on_mouse_up()
    if state.tb_drag then
        state.tb_drag = nil  -- selection (if any) stays
        return
    end
    if state.slider_drag then
        fire_slider(state.slider_drag)  -- final value, unthrottled
        state.slider_drag = nil
        return
    end
    if state.drag then
        state.drag = nil
        return
    end
    if state.pressed then
        local node = node_at(state.mouse.x, state.mouse.y)
        if node and node.id == state.pressed then
            send({ t = 'click', id = node.id })
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
    state.wheel_count = (state.wheel_count or 0) + 1
    local scale = (e and e.scale) or 1
    if scale <= 0 then scale = 1 end
    if state.hud then request_render() end
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
    set_scroll(node,
        (state.scroll[node.id] or 0) + dir * WHEEL_STEP * scale)
end

-- Double-click: select the word under the pointer.
local function on_dbl()
    local x, y = state.mouse.x, state.mouse.y
    local node = node_at(x, y)
    if not node or node.t ~= 'textbox' then return end
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

mp.set_key_bindings({
    { 'mbtn_left', function() on_mouse_up() end,
      function() on_mouse_down() end },
    { 'mbtn_left_dbl', function() on_dbl() end },
    { 'mbtn_right', function() end, function() on_rclick() end },
}, 'mpvtk_mouse', 'force')
mp.set_key_bindings({
    { 'wheel_up', function(e) on_wheel(-1, 'y', e) end },
    { 'wheel_down', function(e) on_wheel(1, 'y', e) end },
    { 'wheel_left', function(e) on_wheel(-1, 'x', e) end },
    { 'wheel_right', function(e) on_wheel(1, 'x', e) end },
    { 'shift+wheel_up', function(e) on_wheel(-1, 'x', e) end },
    { 'shift+wheel_down', function(e) on_wheel(1, 'x', e) end },
}, 'mpvtk_wheel', 'force')
mp.enable_key_bindings('mpvtk_mouse')
mp.enable_key_bindings('mpvtk_wheel')

mp.add_forced_key_binding('F12', 'mpvtk_hud', function()
    state.hud = not state.hud
    request_render()
end)

mp.observe_property('mouse-pos', 'native', function(_, pos)
    if not pos then return end
    if pos.hover == false then
        state.mouse.hover = false
        if state.hover_id then
            state.hover_id = nil
            request_render()
        end
        return
    end
    state.mouse.hover = true
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

local function reconcile()
    state.byid = {}
    for _, node in ipairs(state.nodes) do
        state.byid[node.id] = node
    end
    -- clamp scroll offsets; drop state for vanished ids
    for id, off in pairs(state.scroll) do
        local node = state.byid[id]
        if node and node.t == 'scroll' then
            state.scroll[id] = clamp(off, 0, scroll_max(node))
        else
            state.scroll[id] = nil
        end
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

mp.register_script_message('mpvtk-scene', function(json)
    local scene, err = utils.parse_json(json)
    if not scene then
        msg.error('bad scene: ' .. tostring(err))
        return
    end
    state.scene = scene
    state.nodes = scene.nodes or {}
    reconcile()
    request_render()
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
    -- Page a scroll container (by id) by ~90% of its viewport along its
    -- axis — drives the on-screen carousel arrow buttons.
    local cmd = utils.parse_json(json)
    if not cmd or not cmd.id then return end
    local node = state.byid[cmd.id]
    if not node or node.t ~= 'scroll' then return end
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
            on_mouse_move(x, y)
            on_mouse_down()
            on_mouse_up()
        end
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
        -- double/triple-click at char cell `at` of a textbox
        local node = state.byid[cmd.id]
        if node and node.t == 'textbox' then
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
        -- click item #index (0-based) of the open dropdown popup
        local g = state.dd_geo
        if g then
            local x = g.x + g.w / 2
            local y = g.y + ((cmd.index or 0) + 0.5) * g.ih
            on_mouse_move(x, y)
            on_mouse_down()
            on_mouse_up()
        end
    elseif cmd.cmd == 'text' then
        for c in tostring(cmd.s):gmatch('.') do tb_insert(c) end
    elseif cmd.cmd == 'key' then
        tb_key(cmd.name)
    elseif cmd.cmd == 'state' then
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
            tb = state.tb,
        })
    end
end)
