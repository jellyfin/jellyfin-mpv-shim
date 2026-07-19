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
    ov_last = {},           -- overlay slot -> last command args string
    ov_used = 0,
    tick_timer = nil,
    tick_last = 0,
    blink_timer = nil,
}

-- ---------------------------------------------------------------- utils

local function send(tbl)
    mp.commandv('script-message', 'mpvtk-event', utils.format_json(tbl))
end

-- Keep in sync with layout.py.
local NARROW = {}
for c in ("iIljtfr.,:;!|'`()[]\""):gmatch('.') do NARROW[c] = true end
local WIDE = {}
for c in ('mwMW@%&'):gmatch('.') do WIDE[c] = true end

local function char_w(c)
    if c == ' ' then return 0.30 end
    if NARROW[c] then return 0.34 end
    if WIDE[c] then return 0.85 end
    return 0.54
end

local function text_w(s, size, bold)
    local w = 0
    for c in s:gmatch('.') do w = w + char_w(c) end
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

local function draw_text(ass, node, ex, ey, clip, text, color, extra)
    local an = ALIGN_AN[node.align or 'left'] or 4
    local px = ex
    if an == 5 then px = ex + node.w / 2 end
    if an == 6 then px = ex + node.w end
    ass:new_event()
    ass:append(string.format(
        '{\\an%d\\pos(%.1f,%.1f)\\fs%d\\bord0\\shad0\\1c%s\\1a&H00&%s%s%s}',
        an, px, ey + node.h / 2, node.size,
        ass_color(color), node.bold and '\\b1' or '',
        clip_tag(clip), extra or ''))
    ass:append(esc(text))
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
    clip = clip or { x1 = 0, y1 = 0, x2 = state.w, y2 = state.h }
    local x1 = math.max(ex, clip.x1)
    local y1 = math.max(ey, clip.y1)
    local x2 = math.min(ex + node.w, clip.x2)
    local y2 = math.min(ey + node.h, clip.y2)
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
            overlay_list[#overlay_list + 1] = {
                tostring(px1), tostring(py1), node.src,
                tostring(sy * stride + sx * 4), 'bgra',
                tostring(px2 - px1), tostring(py2 - py1),
                tostring(stride),
            }
        end
    end
end

local function flush_overlays()
    for i, args in ipairs(overlay_list) do
        local key = table.concat(args, '\0')
        if state.ov_last[i] ~= key then
            state.ov_last[i] = key
            mp.commandv('overlay-add', tostring(i - 1), unpack(args))
        end
    end
    for i = #overlay_list + 1, state.ov_used do
        state.ov_last[i] = nil
        mp.commandv('overlay-remove', tostring(i - 1))
    end
    state.ov_used = #overlay_list
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
    draw_text(ass, tnode, ex + pad - shift, ey, inner, text, 'eeeeee')
    if focused and state.cursor_on then
        local cx = ex + pad - shift +
            text_w(text:sub(1, tb and tb.cursor or #text), node.size)
        if cx >= inner.x1 - 1 and cx <= inner.x2 + 1 then
            draw_rect(ass, cx, ey + node.h * 0.18, 2, node.h * 0.64,
                { fill = 'eeeeee', clip = inner })
        end
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
    local tnode = {
        w = node.w - 40, h = node.h, size = node.size, align = 'left',
    }
    draw_text(ass, tnode, ex + 10, ey, clip, label, 'eeeeee')
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

local function draw_popup(ass, node)
    local d = dd_state(node)
    local g = popup_geometry(node)
    state.dd_geo = g
    draw_rect(ass, g.x, g.y, g.w, g.n * g.ih, {
        fill = '222222', radius = 6, bc = '555555', bw = 1,
    })
    for i, item in ipairs(node.items) do
        local iy = g.y + (i - 1) * g.ih
        local hovered = state.mouse.x >= g.x and
            state.mouse.x <= g.x + g.w and
            state.mouse.y >= iy and state.mouse.y < iy + g.ih
        if hovered or (i - 1) == d.sel then
            draw_rect(ass, g.x + 2, iy + 1, g.w - 4, g.ih - 2, {
                fill = hovered and '3d59a1' or '333333', radius = 4,
            })
        end
        local tnode = { w = g.w - 20, h = g.ih, size = node.size,
                        align = 'left' }
        draw_text(ass, tnode, g.x + 10, iy, nil, item, 'eeeeee')
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
    if state.dd_open then
        -- popup rect must occlude images, so compute it up front
        local node = state.byid[state.dd_open]
        if node then
            local g = popup_geometry(node)
            state.dd_geo = g
            occluders[1] = {
                x1 = g.x - 2, y1 = g.y - 2,
                x2 = g.x + g.w + 2, y2 = g.y + g.n * g.ih + 2,
            }
        end
    end
    local ass = assdraw.ass_new()
    for _, node in ipairs(state.nodes) do
        if node.t == 'scroll' or not visible(node) then
            -- scroll containers draw nothing themselves (bars come later)
        else
        local ex, ey, clip = eff(node)
        if node.t == 'rect' then
            local hs = hover_style(node)
            draw_rect(ass, ex, ey, node.w, node.h, {
                fill = (hs and hs.fill) or node.fill,
                a = node.a, radius = node.radius,
                bc = (hs and hs.bc) or node.bc, bw = node.bw,
                clip = clip,
            })
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
        end
        end
    end
    for _, node in ipairs(state.nodes) do
        if node.t == 'scroll' then draw_scrollbar(ass, node) end
    end
    if state.dd_open then
        local node = state.byid[state.dd_open]
        if node then draw_popup(ass, node) end
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
local function node_at(x, y)
    for i = #state.nodes, 1, -1 do
        local node = state.nodes[i]
        if node.t ~= 'scroll' and
            (node.click or node.t == 'textbox' or node.t == 'dropdown' or
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
    for i = #state.nodes, 1, -1 do
        local node = state.nodes[i]
        if node.t == 'scroll' then
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
    for id, b in pairs(state.bars) do
        if x >= b.x - 2 and x <= b.x + b.w + 2 and y >= b.y and
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
    local cx = text_w(tb.text:sub(1, tb.cursor), node.size)
    if cx - tb.shift > avail then tb.shift = cx - avail end
    if cx - tb.shift < 0 then tb.shift = cx end
    if tb.shift < 0 then tb.shift = 0 end
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
    tb.text = tb.text:sub(1, tb.cursor) .. s .. tb.text:sub(tb.cursor + 1)
    tb.cursor = tb.cursor + #s
    tb_changed(node, tb)
end

local function tb_key(name)
    local node = focused_node()
    if not node then return end
    local tb = tb_state(node)
    if name == 'BS' then
        if tb.cursor > 0 then
            tb.text = tb.text:sub(1, tb.cursor - 1) ..
                tb.text:sub(tb.cursor + 1)
            tb.cursor = tb.cursor - 1
            tb_changed(node, tb)
        end
    elseif name == 'DEL' then
        if tb.cursor < #tb.text then
            tb.text = tb.text:sub(1, tb.cursor) ..
                tb.text:sub(tb.cursor + 2)
            tb_changed(node, tb)
        end
    elseif name == 'LEFT' then
        tb.cursor = math.max(0, tb.cursor - 1)
        tb_fix_shift(node, tb); state.cursor_on = true; request_render()
    elseif name == 'RIGHT' then
        tb.cursor = math.min(#tb.text, tb.cursor + 1)
        tb_fix_shift(node, tb); state.cursor_on = true; request_render()
    elseif name == 'HOME' then
        tb.cursor = 0
        tb_fix_shift(node, tb); request_render()
    elseif name == 'END' then
        tb.cursor = #tb.text
        tb_fix_shift(node, tb); request_render()
    elseif name == 'ENTER' then
        send({ t = 'submit', id = node.id, value = tb.text })
    elseif name == 'ESC' then
        blur()  -- luacheck: ignore (fwd-declared below)
    elseif name == 'PASTE' then
        local ok, clip = pcall(mp.get_property, 'clipboard/text')
        if ok and clip and clip ~= '' then
            tb_insert(clip:gsub('[\r\n]', ' '))
        end
    end
end

local function bind_text_keys()
    if text_keys_bound then return end
    text_keys_bound = true
    for code = 33, 126 do
        local c = string.char(code)
        mp.add_forced_key_binding(c, 'mpvtk_ch_' .. code, function()
            tb_insert(c)
        end, { repeatable = true })
    end
    mp.add_forced_key_binding('SPACE', 'mpvtk_space', function()
        tb_insert(' ')
    end, { repeatable = true })
    local keys = { 'BS', 'DEL', 'LEFT', 'RIGHT', 'HOME', 'END', 'ENTER',
                   'ESC' }
    for _, k in ipairs(keys) do
        mp.add_forced_key_binding(k, 'mpvtk_k_' .. k, function()
            tb_key(k)
        end, { repeatable = true })
    end
    mp.add_forced_key_binding('KP_ENTER', 'mpvtk_k_KPE', function()
        tb_key('ENTER')
    end)
    mp.add_forced_key_binding('ctrl+v', 'mpvtk_paste', function()
        tb_key('PASTE')
    end)
end

local function unbind_text_keys()
    if not text_keys_bound then return end
    text_keys_bound = false
    for code = 33, 126 do
        mp.remove_key_binding('mpvtk_ch_' .. code)
    end
    mp.remove_key_binding('mpvtk_space')
    for _, k in ipairs({ 'BS', 'DEL', 'LEFT', 'RIGHT', 'HOME', 'END',
                         'ENTER', 'ESC' }) do
        mp.remove_key_binding('mpvtk_k_' .. k)
    end
    mp.remove_key_binding('mpvtk_k_KPE')
    mp.remove_key_binding('mpvtk_paste')
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

local function set_scroll(node, off)
    off = clamp(off, 0, scroll_max(node))
    if state.scroll[node.id] ~= off then
        state.scroll[node.id] = off
        request_render()
    end
end

local function on_mouse_move(x, y)
    state.mouse.x, state.mouse.y = x, y
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
    elseif state.dd_open then
        request_render()  -- popup item hover
    end
end

local function on_mouse_down()
    local x, y = state.mouse.x, state.mouse.y
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
    if not node then return end
    if node.t == 'textbox' then
        focus_textbox(node)
        local tb = tb_state(node)
        local pad = 10
        local ex = select(1, eff(node))
        local rel = x - ex - pad + tb.shift
        local cur = 0
        local acc = 0
        for i = 1, #tb.text do
            local cw = char_w(tb.text:sub(i, i)) * node.size
            if acc + cw / 2 > rel then break end
            acc = acc + cw
            cur = i
        end
        tb.cursor = cur
        state.cursor_on = true
        request_render()
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

local function on_wheel(dir, axis)
    local node = scroll_at(state.mouse.x, state.mouse.y, axis)
    if not node then return end
    set_scroll(node, (state.scroll[node.id] or 0) + dir * WHEEL_STEP)
end

mp.set_key_bindings({
    { 'mbtn_left', function() on_mouse_up() end,
      function() on_mouse_down() end },
    { 'mbtn_left_dbl', 'ignore' },
}, 'mpvtk_mouse', 'force')
mp.set_key_bindings({
    { 'wheel_up', function() on_wheel(-1, 'y') end },
    { 'wheel_down', function() on_wheel(1, 'y') end },
    { 'wheel_left', function() on_wheel(-1, 'x') end },
    { 'wheel_right', function() on_wheel(1, 'x') end },
    { 'shift+wheel_up', function() on_wheel(-1, 'x') end },
    { 'shift+wheel_down', function() on_wheel(1, 'x') end },
}, 'mpvtk_wheel', 'force')
mp.enable_key_bindings('mpvtk_mouse')
mp.enable_key_bindings('mpvtk_wheel')

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
    if state.focus and (not state.byid[state.focus]) then blur() end
    if state.dd_open and (not state.byid[state.dd_open]) then
        state.dd_open = nil
    end
    -- force=true resets renderer-local widget state from the scene
    for _, node in ipairs(state.nodes) do
        if node.force then
            if node.t == 'textbox' then state.tb[node.id] = nil end
            if node.t == 'dropdown' then state.dd[node.id] = nil end
        end
    end
end

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
    elseif cmd.cmd == 'wheel' then
        if cmd.id then
            local x, y = center_of(cmd.id)
            if x then on_mouse_move(x, y) end
        end
        for _ = 1, cmd.steps or 1 do
            on_wheel(cmd.dir or 1, cmd.axis)
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
            scroll = state.scroll,
            overlays = state.ov_used,
            tb = state.tb,
        })
    end
end)
