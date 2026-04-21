--[[
* nav - exploration/vision.lua
*
* Fog-of-war terrain reveal: build a navigation graph from collision data
* visible within the player's render distance (250 yalms).
*
* Every tick processes three phases:
*   1. Enqueue newly-visible grid cells when the player moves.
*   2. Pop cells from work_queue, probe floor_height, create graph nodes.
*   3. Pop nodes from pending_edges, validate 8-connected neighbor edges.
*
* The "seen" set persists implicitly through graph nodes — on zone load,
* rebuild_seen reconstructs it from existing node positions.
--]]

local raycast = require('geometry.raycast')
local S       = require('core.state')
local D       = require('core.debug')

local vision = {}

local GRID_SPACING   = 2.0
local VISION_RADIUS  = 250.0
local CELL_BUDGET    = 200
local EDGE_BUDGET    = 200
local MAX_SLOPE      = 1.04  -- tan(46°); matches Recast NavMesh max walkable slope

local seen          = {}
local seen_n        = 0
local grid_nodes    = {}
local work_queue    = {}
local wq_head       = 1
local pending_edges = {}
local pe_head       = 1
local last_player_cx = nil
local last_player_cy = nil
local last_crosslink_cx = nil
local last_crosslink_cy = nil

local ENTITY_SCAN_RADIUS = 49.9
local find_scanned       = {}
local find_scanned_n     = 0
local last_find_cx       = nil
local last_find_cy       = nil

local function cell_key(cx, cy)
    return cx .. ',' .. cy
end

local function world_to_cell(x, y)
    return math.floor(x / GRID_SPACING), math.floor(y / GRID_SPACING)
end

local function cell_to_world(cx, cy)
    return cx * GRID_SPACING, cy * GRID_SPACING
end

local function wq_push(item)
    work_queue[#work_queue + 1] = item
end

local function wq_pop()
    if wq_head > #work_queue then return nil end
    local item = work_queue[wq_head]
    work_queue[wq_head] = nil
    wq_head = wq_head + 1
    if wq_head > 1000 and wq_head > #work_queue then
        work_queue = {}
        wq_head = 1
    end
    return item
end

local function pe_push(item)
    pending_edges[#pending_edges + 1] = item
end

local function pe_pop()
    if pe_head > #pending_edges then return nil end
    local item = pending_edges[pe_head]
    pending_edges[pe_head] = nil
    pe_head = pe_head + 1
    if pe_head > 1000 and pe_head > #pending_edges then
        pending_edges = {}
        pe_head = 1
    end
    return item
end

-------------------------------------------------------------------------------
-- Phase 1: enqueue newly-visible cells around the player.
-------------------------------------------------------------------------------
local function enqueue_visible(px, py)
    local pcx, pcy = world_to_cell(px, py)
    if pcx == last_player_cx and pcy == last_player_cy then return end
    last_player_cx = pcx
    last_player_cy = pcy

    local radius_cells = math.ceil(VISION_RADIUS / GRID_SPACING)
    local r2 = VISION_RADIUS * VISION_RADIUS

    for dx = -radius_cells, radius_cells do
        for dy = -radius_cells, radius_cells do
            local cx = pcx + dx
            local cy = pcy + dy
            local key = cell_key(cx, cy)
            if not seen[key] then
                local wx, wy = cell_to_world(cx, cy)
                local ddx = wx - px
                local ddy = wy - py
                if ddx * ddx + ddy * ddy <= r2 then
                    seen[key] = true
                    seen_n = seen_n + 1
                    wq_push({ cx = cx, cy = cy })
                end
            end
        end
    end
end

-------------------------------------------------------------------------------
-- Phase 2: process cells from work queue — probe floor, create nodes.
-------------------------------------------------------------------------------
local function process_nodes(zone_graph, pz)
    if S.spatial_index == nil or S.collision == nil then return end

    local budget = CELL_BUDGET
    local triangles = S.collision.triangles
    local z_top = 100.0
    if S.collision.bounds and S.collision.bounds.max then
        z_top = S.collision.bounds.max[3] + 10.0
    end

    for _ = 1, budget do
        local cell = wq_pop()
        if cell == nil then break end

        local wx, wy = cell_to_world(cell.cx, cell.cy)
        local fz = raycast.floor_height(S.spatial_index, triangles, wx, wy, z_top)
        if fz ~= nil then
            local key = cell_key(cell.cx, cell.cy)
            local nid = zone_graph.add_node(wx, wy, fz)
            grid_nodes[key] = grid_nodes[key] or {}
            grid_nodes[key][#grid_nodes[key] + 1] = nid
            pe_push({ cx = cell.cx, cy = cell.cy, nid = nid })
            zone_graph.dirty = true
        end
    end
end

-------------------------------------------------------------------------------
-- Phase 3: process pending edge connections (8-connected neighbors).
-------------------------------------------------------------------------------
local DIR8 = {
    { 1, 0}, { -1,  0}, { 0, 1}, {0, -1},
    { 1, 1}, { -1, -1}, { 1,-1}, {-1,  1},
}

local function has_edge(zone_graph, a, b)
    for _, nb in ipairs(zone_graph.adjacency[a] or {}) do
        if nb == b then return true end
    end
    return false
end

local function process_edges(zone_graph)
    if S.spatial_index == nil or S.collision == nil then return end

    local budget = EDGE_BUDGET
    local triangles = S.collision.triangles

    for _ = 1, budget do
        local item = pe_pop()
        if item == nil then break end

        local nid = item.nid
        local node = zone_graph.nodes[nid]
        if node == nil then goto continue end

        for _, d in ipairs(DIR8) do
            local ncx = item.cx + d[1]
            local ncy = item.cy + d[2]
            local nkey = cell_key(ncx, ncy)
            local neighbor_nodes = grid_nodes[nkey]
            if neighbor_nodes then
                for _, nnid in ipairs(neighbor_nodes) do
                    if nnid ~= nid and not has_edge(zone_graph, nid, nnid) then
                        local nn = zone_graph.nodes[nnid]
                        if nn then
                            local dx = nn.x - node.x
                            local dy = nn.y - node.y
                            local hdist = math.sqrt(dx*dx + dy*dy)
                            local dz = math.abs(node.z - nn.z)
                            if hdist > 0.01 and dz / hdist < MAX_SLOPE then
                                local blocked = raycast.segment_blocked(
                                    S.spatial_index, triangles,
                                    node.x, node.y, node.z,
                                    nn.x, nn.y, nn.z)
                                if blocked ~= true then
                                    local walkable = raycast.segment_walkable(
                                        S.spatial_index, triangles,
                                        node.x, node.y, nn.x, nn.y, node.z)
                                    if walkable ~= false then
                                        zone_graph.add_edge(nid, nnid)
                                        zone_graph.dirty = true
                                    end
                                end
                            end
                        end
                    end
                end
            end
        end

        ::continue::
    end
end

-------------------------------------------------------------------------------
-- Public API
-------------------------------------------------------------------------------

function vision.reset()
    seen          = {}
    seen_n        = 0
    grid_nodes    = {}
    work_queue    = {}
    wq_head       = 1
    pending_edges = {}
    pe_head       = 1
    last_player_cx = nil
    last_player_cy = nil
    last_crosslink_cx = nil
    last_crosslink_cy = nil
    find_scanned   = {}
    find_scanned_n = 0
    last_find_cx   = nil
    last_find_cy   = nil
end

-------------------------------------------------------------------------------
-- Cross-link: connect nearby nodes when the player walks between them.
-- The player's physical position proves walkability between components.
-------------------------------------------------------------------------------
local CROSSLINK_RADIUS = 10.0
local CROSSLINK_BUDGET = 20

local function crosslink_near_player(zone_graph, px, py, pz)
    if S.spatial_index == nil or S.collision == nil then return end

    local pcx, pcy = world_to_cell(px, py)
    if pcx == last_crosslink_cx and pcy == last_crosslink_cy then return end
    last_crosslink_cx = pcx
    last_crosslink_cy = pcy

    local triangles = S.collision.triangles
    local pkey = cell_key(pcx, pcy)
    local player_nodes = grid_nodes[pkey]
    local player_nid = nil
    if player_nodes and #player_nodes > 0 then
        player_nid = player_nodes[1]
    else
        local fz = raycast.floor_height(S.spatial_index, triangles, px, py, pz + 5.0)
        if fz then
            player_nid = zone_graph.add_node(px, py, fz)
            grid_nodes[pkey] = grid_nodes[pkey] or {}
            grid_nodes[pkey][#grid_nodes[pkey] + 1] = player_nid
            if not seen[pkey] then
                seen[pkey] = true
                seen_n = seen_n + 1
            end
            zone_graph.dirty = true
        end
    end
    if player_nid == nil then return end

    -- Connect player node to nearby nodes using grid lookup (not full scan).
    local radius_cells = math.ceil(CROSSLINK_RADIUS / GRID_SPACING)
    local r2 = CROSSLINK_RADIUS * CROSSLINK_RADIUS
    local links = 0
    for dx = -radius_cells, radius_cells do
        if links >= CROSSLINK_BUDGET then break end
        for dy = -radius_cells, radius_cells do
            if links >= CROSSLINK_BUDGET then break end
            local nkey = cell_key(pcx + dx, pcy + dy)
            local cell_nodes = grid_nodes[nkey]
            if cell_nodes then
                for _, nid in ipairs(cell_nodes) do
                    if links >= CROSSLINK_BUDGET then break end
                    if nid ~= player_nid and not has_edge(zone_graph, player_nid, nid) then
                        local n = zone_graph.nodes[nid]
                        if n then
                            local ddx = n.x - px
                            local ddy = n.y - py
                            if ddx*ddx + ddy*ddy <= r2 then
                                local blocked = raycast.segment_blocked(
                                    S.spatial_index, triangles,
                                    px, py, pz, n.x, n.y, n.z)
                                if blocked ~= true then
                                    zone_graph.add_edge(player_nid, nid)
                                    zone_graph.dirty = true
                                    links = links + 1
                                end
                            end
                        end
                    end
                end
            end
        end
    end
end

function vision.tick(zone_graph, px, py, pz)
    enqueue_visible(px, py)
    local wq_depth = #work_queue - wq_head + 1
    local pe_depth = #pending_edges - pe_head + 1
    if wq_depth > 0 then
        process_nodes(zone_graph, pz)
    end
    if pe_depth > 0 then
        process_edges(zone_graph)
    end
    crosslink_near_player(zone_graph, px, py, pz)
end

function vision.rebuild_seen(zone_graph)
    seen       = {}
    seen_n     = 0
    grid_nodes = {}
    for id, n in ipairs(zone_graph.nodes) do
        local cx, cy = world_to_cell(n.x, n.y)
        local key = cell_key(cx, cy)
        if not seen[key] then
            seen[key] = true
            seen_n = seen_n + 1
        end
        grid_nodes[key] = grid_nodes[key] or {}
        grid_nodes[key][#grid_nodes[key] + 1] = id
    end
    last_player_cx = nil
    last_player_cy = nil
    last_crosslink_cx = nil
    last_crosslink_cy = nil

    local pruned = 0
    for a, neighbors in pairs(zone_graph.adjacency) do
        local na = zone_graph.nodes[a]
        if na then
            local keep = {}
            for _, b in ipairs(neighbors) do
                local nb = zone_graph.nodes[b]
                if nb then
                    local dx = nb.x - na.x
                    local dy = nb.y - na.y
                    local hdist = math.sqrt(dx*dx + dy*dy)
                    local dz = math.abs(na.z - nb.z)
                    if hdist < 0.01 or dz / hdist >= MAX_SLOPE then
                        pruned = pruned + 1
                    else
                        keep[#keep + 1] = b
                    end
                else
                    keep[#keep + 1] = b
                end
            end
            zone_graph.adjacency[a] = keep
        end
    end
    if pruned > 0 then
        zone_graph._edge_count = zone_graph._edge_count - pruned
        zone_graph.dirty = true
        D.print_msg(string.format('vision: pruned %d over-steep edges', pruned))
    end

    D.dbg(string.format('vision: rebuilt seen set, %d cells from %d nodes',
        vision.seen_count(), zone_graph.node_count()))
end

local repair_queue = {}
local repair_head = 1
local repair_added = 0
local REPAIR_BUDGET = 200

function vision.start_repair(zone_graph)
    repair_queue = {}
    repair_head = 1
    repair_added = 0
    for id, _ in ipairs(zone_graph.nodes) do
        repair_queue[#repair_queue + 1] = id
    end
    D.print_msg(string.format('vision: queued %d nodes for edge repair', #repair_queue))
end

function vision.repair_tick(zone_graph)
    if repair_head > #repair_queue then return false end
    if S.spatial_index == nil or S.collision == nil then return false end
    local triangles = S.collision.triangles
    local budget = REPAIR_BUDGET

    for _ = 1, budget do
        if repair_head > #repair_queue then
            D.print_msg(string.format('vision: edge repair complete, %d edges added', repair_added))
            if repair_added > 0 then zone_graph.dirty = true end
            return false
        end
        local id = repair_queue[repair_head]
        repair_head = repair_head + 1
        local n = zone_graph.nodes[id]
        if n then
            local cx, cy = world_to_cell(n.x, n.y)
            for _, d in ipairs(DIR8) do
                local nkey = cell_key(cx + d[1], cy + d[2])
                local neighbor_nodes = grid_nodes[nkey]
                if neighbor_nodes then
                    for _, nnid in ipairs(neighbor_nodes) do
                        if nnid ~= id and not has_edge(zone_graph, id, nnid) then
                            local nn = zone_graph.nodes[nnid]
                            if nn then
                                local dx = nn.x - n.x
                                local dy = nn.y - n.y
                                local hdist = math.sqrt(dx*dx + dy*dy)
                                local dz = math.abs(n.z - nn.z)
                                if hdist > 0.01 and dz / hdist < MAX_SLOPE then
                                    local blocked = raycast.segment_blocked(
                                        S.spatial_index, triangles,
                                        n.x, n.y, n.z,
                                        nn.x, nn.y, nn.z)
                                    if blocked ~= true then
                                        local walkable = raycast.segment_walkable(
                                            S.spatial_index, triangles,
                                            n.x, n.y, nn.x, nn.y, n.z)
                                        if walkable ~= false then
                                            zone_graph.add_edge(id, nnid)
                                            repair_added = repair_added + 1
                                        end
                                    end
                                end
                            end
                        end
                    end
                end
            end
        end
    end
    return true
end

function vision.repairing()
    return repair_head <= #repair_queue
end

function vision.get_boundary_cells()
    local boundary = {}
    for key, _ in pairs(seen) do
        local cx, cy = key:match('([^,]+),([^,]+)')
        cx, cy = tonumber(cx), tonumber(cy)
        if cx and cy then
            local is_boundary = false
            local unseen_count = 0
            for _, d in ipairs(DIR8) do
                local nkey = cell_key(cx + d[1], cy + d[2])
                if not seen[nkey] then
                    is_boundary = true
                    unseen_count = unseen_count + 1
                end
            end
            if is_boundary then
                local nodes = grid_nodes[key]
                if nodes and #nodes > 0 then
                    local wx, wy = cell_to_world(cx, cy)
                    boundary[#boundary + 1] = {
                        cx = cx, cy = cy,
                        x = wx, y = wy,
                        node_id = nodes[1],
                        unseen = unseen_count,
                    }
                end
            end
        end
    end
    return boundary
end

-------------------------------------------------------------------------------
-- Find frontier nodes within a reachable set: reachable nodes at the
-- edge of their connected component. A node is a frontier if any of
-- its 8 grid neighbors either has no graph node or has nodes that are
-- NOT in the reachable set (different component = potential cross-link).
-------------------------------------------------------------------------------
function vision.get_reachable_frontiers(reachable_set, zone_graph)
    local result = {}

    -- Get collision bounds to distinguish "outside zone" from "unexplored".
    local bounds_min_x, bounds_min_y = -math.huge, -math.huge
    local bounds_max_x, bounds_max_y =  math.huge,  math.huge
    if S.collision and S.collision.bounds then
        local b = S.collision.bounds
        if b.min then bounds_min_x, bounds_min_y = b.min[1], b.min[2] end
        if b.max then bounds_max_x, bounds_max_y = b.max[1], b.max[2] end
    end

    for nid, _ in pairs(reachable_set) do
        local n = zone_graph.nodes[nid]
        if n then
            local cx, cy = world_to_cell(n.x, n.y)
            local frontier_score = 0
            for _, d in ipairs(DIR8) do
                local nkey = cell_key(cx + d[1], cy + d[2])
                local neighbor_nodes = grid_nodes[nkey]
                if neighbor_nodes == nil or #neighbor_nodes == 0 then
                    if not seen[nkey] then
                        -- Truly unseen — only count if within collision bounds.
                        local wx, wy = cell_to_world(cx + d[1], cy + d[2])
                        if wx >= bounds_min_x and wx <= bounds_max_x
                           and wy >= bounds_min_y and wy <= bounds_max_y then
                            frontier_score = frontier_score + 2
                        end
                    end
                    -- If seen but no node: terrain edge (no floor) — skip
                else
                    local all_reachable = true
                    for _, nnid in ipairs(neighbor_nodes) do
                        if not reachable_set[nnid] then
                            all_reachable = false
                            break
                        end
                    end
                    if not all_reachable then
                        frontier_score = frontier_score + 3
                    end
                end
            end
            if frontier_score > 0 then
                result[#result + 1] = {
                    x = n.x, y = n.y,
                    node_id = nid,
                    unseen = frontier_score,
                }
            end
        end
    end
    return result
end

function vision.seen_count()
    return seen_n
end

function vision.queue_depth()
    local wq = #work_queue - wq_head + 1
    if wq < 0 then wq = 0 end
    local pe = #pending_edges - pe_head + 1
    if pe < 0 then pe = 0 end
    return wq + pe
end

-------------------------------------------------------------------------------
-- Find-scan tracking: marks cells within entity visibility range as scanned.
-------------------------------------------------------------------------------

function vision.mark_find_scanned(px, py)
    local pcx, pcy = world_to_cell(px, py)
    if pcx == last_find_cx and pcy == last_find_cy then return end
    last_find_cx = pcx
    last_find_cy = pcy

    local r_cells = math.ceil(ENTITY_SCAN_RADIUS / GRID_SPACING)
    local r2 = ENTITY_SCAN_RADIUS * ENTITY_SCAN_RADIUS
    for dx = -r_cells, r_cells do
        for dy = -r_cells, r_cells do
            local wx = (pcx + dx) * GRID_SPACING
            local wy = (pcy + dy) * GRID_SPACING
            local ddx = wx - px
            local ddy = wy - py
            if ddx*ddx + ddy*ddy <= r2 then
                local key = cell_key(pcx + dx, pcy + dy)
                if not find_scanned[key] then
                    find_scanned[key] = true
                    find_scanned_n = find_scanned_n + 1
                end
            end
        end
    end
end

function vision.reset_find_scanned()
    find_scanned   = {}
    find_scanned_n = 0
    last_find_cx   = nil
    last_find_cy   = nil
end

function vision.is_find_scanned(x, y)
    local cx, cy = world_to_cell(x, y)
    return find_scanned[cell_key(cx, cy)] == true
end

function vision.find_scanned_count()
    return find_scanned_n
end

function vision.get_find_frontiers(reachable_set, zone_graph)
    local result = {}

    local bounds_min_x, bounds_min_y = -math.huge, -math.huge
    local bounds_max_x, bounds_max_y =  math.huge,  math.huge
    if S.collision and S.collision.bounds then
        local b = S.collision.bounds
        if b.min then bounds_min_x, bounds_min_y = b.min[1], b.min[2] end
        if b.max then bounds_max_x, bounds_max_y = b.max[1], b.max[2] end
    end

    for nid, _ in pairs(reachable_set) do
        local n = zone_graph.nodes[nid]
        if n then
            local cx, cy = world_to_cell(n.x, n.y)
            local unseen_score = 0
            local unscanned_score = 0

            for _, d in ipairs(DIR8) do
                local nkey = cell_key(cx + d[1], cy + d[2])
                local neighbor_nodes = grid_nodes[nkey]
                if neighbor_nodes == nil or #neighbor_nodes == 0 then
                    if not seen[nkey] then
                        local wx, wy = cell_to_world(cx + d[1], cy + d[2])
                        if wx >= bounds_min_x and wx <= bounds_max_x
                           and wy >= bounds_min_y and wy <= bounds_max_y then
                            unseen_score = unseen_score + 2
                            if not find_scanned[nkey] then
                                unscanned_score = unscanned_score + 2
                            end
                        end
                    end
                else
                    if not find_scanned[nkey] then
                        unscanned_score = unscanned_score + 1
                    end
                    local all_reachable = true
                    for _, nnid in ipairs(neighbor_nodes) do
                        if not reachable_set[nnid] then
                            all_reachable = false
                            break
                        end
                    end
                    if not all_reachable then
                        unseen_score = unseen_score + 3
                    end
                end
            end

            -- For find mode: frontier if there are unscanned OR unseen neighbors
            local total = unseen_score + unscanned_score
            if total > 0 then
                result[#result + 1] = {
                    x = n.x, y = n.y,
                    node_id = nid,
                    unseen = unseen_score,
                    unscanned = unscanned_score,
                }
            end
        end
    end
    return result
end

return vision
