--[[
* nav - geometry/hires_mesh.lua
*
* High-resolution heightfield mesh — sole navigation system.
* Cell size 0.4m matching FFXI-NavMesh-Builder Recast parameters.
*
* Only probes hires cells that overlap occupied spatial_index cells
* (cells that contain collision triangles). This skips empty space entirely.
*
* Recast parameters matched:
*   cellSize=0.40, cellHeight=0.20, agentHeight=1.8, agentRadius=0.7,
*   agentMaxClimb=0.5, agentMaxSlope=46.0
--]]

local raycast = require('geometry.raycast')
local S       = require('core.state')
local D       = require('core.debug')

local hires = {}

local CELL_SIZE      = 0.4
local AGENT_RADIUS   = 0.7
local MAX_CLIMB      = 0.5
local MAX_SLOPE      = 1.04   -- tan(46°)
local EROSION_CELLS  = math.ceil(AGENT_RADIUS / CELL_SIZE)  -- 2 cells
local SI_CELL_SIZE   = 4.0   -- spatial_index cell size (must match spatial_index.lua)

local PROBE_BUDGET    = 500
local VALIDATE_BUDGET = 300
local EROSION_BUDGET  = 500

local CELL_OPEN       = 1
local CELL_BLOCKED    = 2
local CELL_ERODED     = 3

-- Module state
local cells = {}
local cell_count = 0
local open_count = 0
local blocked_count = 0
local eroded_count = 0

-- Probe work list: array of {si_cx, si_cy} spatial-index cells to process.
-- Each SI cell maps to a 10x10 block of hires cells (4.0/0.4).
local si_work = {}
local si_work_head = 1
local si_work_total = 0
local probe_done = false
local z_top_cached = 100.0

-- Within-SI-cell cursor: tracks progress inside current SI cell.
local cur_si_key = nil
local cur_hx, cur_hy = nil, nil   -- hires cell cursor within SI cell
local cur_hx_max, cur_hy_max = nil, nil

-- Validate + erosion queues
local validate_queue = {}
local vq_head = 1
local erosion_queue = {}
local eq_head = 1

local function cell_key(cx, cy)
    return cx .. ',' .. cy
end

local function world_to_hires(x, y)
    return math.floor(x / CELL_SIZE), math.floor(y / CELL_SIZE)
end

local function hires_to_world(cx, cy)
    return (cx + 0.5) * CELL_SIZE, (cy + 0.5) * CELL_SIZE
end

local function vq_push(item) validate_queue[#validate_queue + 1] = item end
local function vq_pop()
    if vq_head > #validate_queue then return nil end
    local item = validate_queue[vq_head]
    validate_queue[vq_head] = nil
    vq_head = vq_head + 1
    if vq_head > 1000 and vq_head > #validate_queue then
        validate_queue = {}
        vq_head = 1
    end
    return item
end

local function eq_push(item) erosion_queue[#erosion_queue + 1] = item end
local function eq_pop()
    if eq_head > #erosion_queue then return nil end
    local item = erosion_queue[eq_head]
    erosion_queue[eq_head] = nil
    eq_head = eq_head + 1
    if eq_head > 1000 and eq_head > #erosion_queue then
        erosion_queue = {}
        eq_head = 1
    end
    return item
end

-------------------------------------------------------------------------------
-- Build work list from spatial_index occupied cells.
-- Only SI cells that contain triangles get probed.
-------------------------------------------------------------------------------
local function init_work_list()
    if #si_work > 0 or probe_done then return end
    if S.spatial_index == nil then return end

    local idx = S.spatial_index
    z_top_cached = 100.0
    if S.collision and S.collision.bounds and S.collision.bounds.max then
        z_top_cached = S.collision.bounds.max[3] + 10.0
    end

    for key, tris in pairs(idx.grid) do
        if #tris > 0 then
            local cx_str, cy_str = key:match('([^,]+),([^,]+)')
            local si_cx = tonumber(cx_str)
            local si_cy = tonumber(cy_str)
            if si_cx and si_cy then
                si_work[#si_work + 1] = { si_cx = si_cx, si_cy = si_cy }
            end
        end
    end
    si_work_total = #si_work

    D.dbg(string.format('hires: %d occupied SI cells to probe (~%dK hires cells)',
        si_work_total,
        math.floor(si_work_total * (SI_CELL_SIZE / CELL_SIZE)^2 / 1000)))
end

-------------------------------------------------------------------------------
-- Phase 1: Probe floors within occupied spatial_index cells.
-- Processes PROBE_BUDGET hires cells per tick.
-------------------------------------------------------------------------------
local function probe_floors()
    if S.spatial_index == nil or S.collision == nil then return end
    if probe_done then return end

    local triangles = S.collision.triangles
    local si = S.spatial_index
    local hires_per_si = math.floor(SI_CELL_SIZE / CELL_SIZE)  -- 10
    local budget = PROBE_BUDGET

    for _ = 1, budget do
        -- Need a new SI cell?
        if cur_hx == nil then
            if si_work_head > #si_work then
                probe_done = true
                D.dbg(string.format('hires: probing complete, %d cells (%d open)',
                    cell_count, open_count))
                return
            end
            local item = si_work[si_work_head]
            si_work[si_work_head] = nil
            si_work_head = si_work_head + 1

            -- Map SI cell to hires cell range (with 1-cell border for edge checking)
            cur_hx = item.si_cx * hires_per_si - 1
            cur_hy = item.si_cy * hires_per_si - 1
            cur_hx_max = (item.si_cx + 1) * hires_per_si
            cur_hy_max = (item.si_cy + 1) * hires_per_si
        end

        local key = cell_key(cur_hx, cur_hy)
        if not cells[key] then
            local wx, wy = hires_to_world(cur_hx, cur_hy)
            local fz = raycast.floor_height(si, triangles, wx, wy, z_top_cached)
            if fz ~= nil then
                cells[key] = { z = fz, state = CELL_OPEN }
                cell_count = cell_count + 1
                open_count = open_count + 1
                vq_push({ cx = cur_hx, cy = cur_hy, key = key })
            end
        end

        -- Advance within SI cell
        cur_hy = cur_hy + 1
        if cur_hy > cur_hy_max then
            cur_hy = cur_hy_max - hires_per_si
            cur_hx = cur_hx + 1
            if cur_hx > cur_hx_max then
                cur_hx = nil  -- done with this SI cell
            end
        end
    end
end

-------------------------------------------------------------------------------
-- Phase 2: Slope/step validation.
-------------------------------------------------------------------------------
local CARDINAL = { {1,0}, {-1,0}, {0,1}, {0,-1} }

local function validate_slopes()
    for _ = 1, VALIDATE_BUDGET do
        local item = vq_pop()
        if item == nil then return end

        local c = cells[item.key]
        if c == nil or c.state ~= CELL_OPEN then goto continue end

        for _, d in ipairs(CARDINAL) do
            local nkey = cell_key(item.cx + d[1], item.cy + d[2])
            local nc = cells[nkey]
            if nc ~= nil and nc.z ~= nil and nc.state == CELL_OPEN then
                local dz = math.abs(c.z - nc.z)
                if dz > MAX_CLIMB or dz / CELL_SIZE > MAX_SLOPE then
                    c.state = CELL_BLOCKED
                    open_count = open_count - 1
                    blocked_count = blocked_count + 1
                    eq_push({ cx = item.cx, cy = item.cy })
                    break
                end
            end
        end

        ::continue::
    end
end

-------------------------------------------------------------------------------
-- Phase 3: Agent radius erosion.
-------------------------------------------------------------------------------
local function erode_radius()
    for _ = 1, EROSION_BUDGET do
        local item = eq_pop()
        if item == nil then return end

        for edx = -EROSION_CELLS, EROSION_CELLS do
            for edy = -EROSION_CELLS, EROSION_CELLS do
                if edx == 0 and edy == 0 then goto skip end
                if edx * edx + edy * edy > EROSION_CELLS * EROSION_CELLS then goto skip end
                local nkey = cell_key(item.cx + edx, item.cy + edy)
                local nc = cells[nkey]
                if nc ~= nil and nc.state == CELL_OPEN then
                    nc.state = CELL_ERODED
                    open_count = open_count - 1
                    eroded_count = eroded_count + 1
                end
                ::skip::
            end
        end
    end
end

-------------------------------------------------------------------------------
-- Grid-based A* over the heightfield.
-------------------------------------------------------------------------------
local DIR8 = {
    { 1, 0, 1.0}, {-1, 0, 1.0}, {0, 1, 1.0}, {0,-1, 1.0},
    { 1, 1, 1.414}, {-1,-1, 1.414}, {1,-1, 1.414}, {-1, 1, 1.414},
}

function hires.find_path(sx, sy, gx, gy, max_nodes)
    max_nodes = max_nodes or 200000
    local scx, scy = world_to_hires(sx, sy)
    local gcx, gcy = world_to_hires(gx, gy)

    local skey = cell_key(scx, scy)
    local gkey = cell_key(gcx, gcy)

    local function find_walkable_near(cx, cy, radius)
        local best_key, best_cx, best_cy, best_d2 = nil, nil, nil, math.huge
        for dx = -radius, radius do
            for dy = -radius, radius do
                local k = cell_key(cx + dx, cy + dy)
                local c = cells[k]
                if c and c.state == CELL_OPEN then
                    local d2 = dx * dx + dy * dy
                    if d2 < best_d2 then
                        best_key = k
                        best_cx = cx + dx
                        best_cy = cy + dy
                        best_d2 = d2
                    end
                end
            end
        end
        return best_key, best_cx, best_cy
    end

    local sc = cells[skey]
    if sc == nil or sc.state ~= CELL_OPEN then
        skey, scx, scy = find_walkable_near(scx, scy, 8)
        if skey == nil then return nil end
    end

    local gc = cells[gkey]
    if gc == nil or gc.state ~= CELL_OPEN then
        gkey, gcx, gcy = find_walkable_near(gcx, gcy, 8)
        if gkey == nil then return nil end
    end

    if skey == gkey then
        local wx, wy = hires_to_world(scx, scy)
        return { { x = wx, y = wy, z = cells[skey].z } }
    end

    local open = {}
    local g_score = {}
    local came_from = {}
    local came_cell = {}
    local closed = {}

    local function heuristic(cx, cy)
        local dx = cx - gcx
        local dy = cy - gcy
        return math.sqrt(dx * dx + dy * dy) * CELL_SIZE
    end

    local function heap_push(f, g, cx, cy, key)
        local n = #open + 1
        open[n] = { f = f, g = g, cx = cx, cy = cy, key = key }
        while n > 1 do
            local p = math.floor(n / 2)
            if open[p].f > open[n].f then
                open[p], open[n] = open[n], open[p]
                n = p
            else
                break
            end
        end
    end

    local function heap_pop()
        if #open == 0 then return nil end
        local top = open[1]
        local last = #open
        open[1] = open[last]
        open[last] = nil
        local n = 1
        while true do
            local smallest = n
            local l = 2 * n
            local r = l + 1
            if l < last and open[l].f < open[smallest].f then smallest = l end
            if r < last and open[r].f < open[smallest].f then smallest = r end
            if smallest ~= n then
                open[n], open[smallest] = open[smallest], open[n]
                n = smallest
            else
                break
            end
        end
        return top
    end

    g_score[skey] = 0
    came_cell[skey] = { cx = scx, cy = scy }
    heap_push(heuristic(scx, scy), 0, scx, scy, skey)

    local expanded = 0
    while #open > 0 and expanded < max_nodes do
        local cur = heap_pop()
        if cur == nil then break end
        if cur.key == gkey then break end
        if closed[cur.key] then goto next_node end
        closed[cur.key] = true
        expanded = expanded + 1

        for _, d in ipairs(DIR8) do
            local ncx = cur.cx + d[1]
            local ncy = cur.cy + d[2]
            local nkey = cell_key(ncx, ncy)
            if not closed[nkey] then
                local nc = cells[nkey]
                if nc and nc.state == CELL_OPEN then
                    local move_cost = d[3] * CELL_SIZE
                    local ng = cur.g + move_cost
                    if g_score[nkey] == nil or ng < g_score[nkey] then
                        g_score[nkey] = ng
                        came_from[nkey] = cur.key
                        came_cell[nkey] = { cx = ncx, cy = ncy }
                        heap_push(ng + heuristic(ncx, ncy), ng, ncx, ncy, nkey)
                    end
                end
            end
        end
        ::next_node::
    end

    if came_from[gkey] == nil and skey ~= gkey then return nil end

    local path_keys = { gkey }
    local k = gkey
    while came_from[k] do
        k = came_from[k]
        path_keys[#path_keys + 1] = k
    end

    local result = {}
    for i = #path_keys, 1, -1 do
        local pk = path_keys[i]
        local cc = came_cell[pk]
        if cc then
            local wx, wy = hires_to_world(cc.cx, cc.cy)
            local c = cells[pk]
            result[#result + 1] = { x = wx, y = wy, z = c and c.z or 0 }
        end
    end

    result = hires._simplify_path(result, CELL_SIZE * 3.0)
    D.dbg(string.format('hires A*: expanded=%d, path=%d pts', expanded, #result))
    return result
end

-------------------------------------------------------------------------------
-- Douglas-Peucker path simplification.
-------------------------------------------------------------------------------
function hires._simplify_path(path, epsilon)
    if #path <= 2 then return path end
    local function pld(p, a, b)
        local abx, aby = b.x - a.x, b.y - a.y
        local len2 = abx * abx + aby * aby
        if len2 < 1e-10 then
            local dx, dy = p.x - a.x, p.y - a.y
            return math.sqrt(dx * dx + dy * dy)
        end
        local t = math.max(0, math.min(1, ((p.x-a.x)*abx + (p.y-a.y)*aby) / len2))
        local dx = p.x - (a.x + t * abx)
        local dy = p.y - (a.y + t * aby)
        return math.sqrt(dx * dx + dy * dy)
    end
    local function rdp(pts, i, j)
        if j <= i + 1 then return { pts[i] } end
        local max_d, max_k = 0, i
        for k = i + 1, j - 1 do
            local d = pld(pts[k], pts[i], pts[j])
            if d > max_d then max_d = d; max_k = k end
        end
        if max_d <= epsilon then return { pts[i] } end
        local left = rdp(pts, i, max_k)
        local right = rdp(pts, max_k, j)
        for k = 2, #right do left[#left + 1] = right[k] end
        return left
    end
    local s = rdp(path, 1, #path)
    s[#s + 1] = path[#path]
    return s
end

function hires.is_walkable(x, y)
    local c = cells[cell_key(world_to_hires(x, y))]
    return c ~= nil and c.state == CELL_OPEN
end

function hires.floor_at(x, y)
    local c = cells[cell_key(world_to_hires(x, y))]
    return c and c.z or nil
end

function hires.has_coverage(x, y, radius)
    radius = radius or 10.0
    local cx, cy = world_to_hires(x, y)
    local r = math.ceil(radius / CELL_SIZE)
    local total, known = 0, 0
    for dx = -r, r, 5 do
        for dy = -r, r, 5 do
            total = total + 1
            if cells[cell_key(cx + dx, cy + dy)] then known = known + 1 end
        end
    end
    return total > 0 and (known / total) > 0.5
end

function hires.is_ready()
    local vq = #validate_queue - vq_head + 1
    local eq = #erosion_queue - eq_head + 1
    return probe_done and vq <= 0 and eq <= 0
end

-------------------------------------------------------------------------------
-- Re-sort remaining SI work list so cells near the line from (ax,ay) to
-- (bx,by) are probed first. Call when a new goal is set.
-------------------------------------------------------------------------------
function hires.prioritize(ax, ay, bx, by)
    if probe_done then return end
    if si_work_head > #si_work then return end

    -- Finish current SI cell before re-sorting
    -- (cur_hx stays, we just reorder future work)

    local mid_x = (ax + bx) * 0.5
    local mid_y = (ay + by) * 0.5

    -- Build new array from remaining items
    local remaining = {}
    for i = si_work_head, #si_work do
        if si_work[i] then
            remaining[#remaining + 1] = si_work[i]
        end
    end

    -- Sort by distance to midpoint of start→goal line
    local si_to_world = SI_CELL_SIZE
    table.sort(remaining, function(a, b)
        local ax2 = a.si_cx * si_to_world - mid_x
        local ay2 = a.si_cy * si_to_world - mid_y
        local bx2 = b.si_cx * si_to_world - mid_x
        local by2 = b.si_cy * si_to_world - mid_y
        return (ax2*ax2 + ay2*ay2) < (bx2*bx2 + by2*by2)
    end)

    -- Rebuild work list (compact)
    si_work = remaining
    si_work_head = 1

    D.dbg(string.format('hires: prioritized %d SI cells toward (%.0f,%.0f)→(%.0f,%.0f)',
        #remaining, ax, ay, bx, by))
end

function hires.tick(px, py, pz)
    init_work_list()
    if not probe_done then probe_floors() end
    local vq = #validate_queue - vq_head + 1
    local eq = #erosion_queue - eq_head + 1
    if vq > 0 then validate_slopes() end
    if eq > 0 then erode_radius() end
end

function hires.reset()
    cells = {}
    cell_count = 0
    open_count = 0
    blocked_count = 0
    eroded_count = 0
    si_work = {}
    si_work_head = 1
    si_work_total = 0
    probe_done = false
    cur_si_key = nil
    cur_hx = nil
    cur_hy = nil
    cur_hx_max = nil
    cur_hy_max = nil
    validate_queue = {}
    vq_head = 1
    erosion_queue = {}
    eq_head = 1
    z_top_cached = 100.0
end

function hires.stats()
    local vq = #validate_queue - vq_head + 1
    if vq < 0 then vq = 0 end
    local eq = #erosion_queue - eq_head + 1
    if eq < 0 then eq = 0 end
    local si_remaining = #si_work - si_work_head + 1
    if si_remaining < 0 then si_remaining = 0 end
    return {
        total = cell_count, open = open_count,
        blocked = blocked_count, eroded = eroded_count,
        validate_queue = vq, erosion_queue = eq,
        si_remaining = si_remaining, si_total = si_work_total,
        probe_done = probe_done, ready = hires.is_ready(),
    }
end

return hires
