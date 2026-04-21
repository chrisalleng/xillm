--[[
* nav - exploration/explorer.lua
*
* Grow the per-zone discovered graph around the player.
*
* On each tick the explorer:
*   1. Generates candidate positions near the player using sampler.lua.
*   2. Validates each candidate (floor + walkability + wall check).
*   3. Adds valid candidates as graph nodes connected to ALL reachable
*      nearby nodes (not just the nearest), building a mesh rather than
*      a chain.
*   4. Cross-links existing nearby nodes that lack edges between them
*      when the player walks through already-explored territory.
--]]

local sampler  = require('exploration.sampler')
local raycast  = require('geometry.raycast')
local S        = require('core.state')

local explorer = {}

explorer.BUDGET_PER_TICK = 6
explorer.MIN_NODE_DIST = 2.0

local SAMPLE_RADII = { 2.5, 5.0, 8.0 }
local SAMPLE_COUNT = 8

-- Max distance for connecting a new node to existing neighbors.
local CONNECT_RADIUS = 6.0
-- Max distance for cross-linking existing nearby nodes.
local CROSSLINK_RADIUS = 5.0
-- Budget for cross-link checks per tick.
local CROSSLINK_BUDGET = 4

-------------------------------------------------------------------------------
-- Return true if (cx, cy) is far enough from all existing nodes.
-------------------------------------------------------------------------------
local function is_distinct(zone_graph, cx, cy, min_dist)
    local md2 = min_dist * min_dist
    for _, n in ipairs(zone_graph.nodes) do
        local dx = n.x - cx
        local dy = n.y - cy
        if dx*dx + dy*dy < md2 then return false end
    end
    return true
end

-------------------------------------------------------------------------------
-- Find all nodes within radius of (cx, cy). Returns array of node IDs.
-------------------------------------------------------------------------------
local function nodes_within(zone_graph, cx, cy, radius)
    local r2 = radius * radius
    local result = {}
    for id, n in ipairs(zone_graph.nodes) do
        local dx = n.x - cx
        local dy = n.y - cy
        if dx*dx + dy*dy <= r2 then
            result[#result + 1] = id
        end
    end
    return result
end

-------------------------------------------------------------------------------
-- Check if the segment from node a to point (bx, by) is traversable.
-------------------------------------------------------------------------------
local function segment_clear(ax, ay, bx, by, z_hint)
    if S.spatial_index == nil or S.collision == nil then return true end
    local ok = raycast.segment_walkable(
        S.spatial_index, S.collision.triangles,
        ax, ay, bx, by, z_hint)
    if ok == false then return false end
    local wall = raycast.segment_blocked(
        S.spatial_index, S.collision.triangles,
        ax, ay, z_hint, bx, by, z_hint)
    if wall == true then return false end
    return true
end

-------------------------------------------------------------------------------
-- Validate a candidate position using collision data.
-------------------------------------------------------------------------------
local function validate_candidate(zone_graph, cx, cy, pz)
    if S.spatial_index == nil or S.collision == nil then return true, pz end

    local fz = raycast.floor_height(
        S.spatial_index, S.collision.triangles,
        cx, cy, pz + raycast.FLOOR_SEARCH_RANGE)
    if fz == nil then return false, nil end

    local nearest = zone_graph.nearest_node(cx, cy)
    if nearest == nil then return true, fz end
    local n = zone_graph.nodes[nearest]

    if not segment_clear(n.x, n.y, cx, cy, pz) then return false, nil end
    return true, fz
end

-------------------------------------------------------------------------------
-- Check if two node IDs already share an edge.
-------------------------------------------------------------------------------
local function has_edge(zone_graph, a, b)
    for _, nb in ipairs(zone_graph.adjacency[a] or {}) do
        if nb == b then return true end
    end
    return false
end

-------------------------------------------------------------------------------
-- explorer.tick(zone_graph, px, py, pz)
-- Called every N frames. Grows the graph near the player position.
-------------------------------------------------------------------------------
function explorer.tick(zone_graph, px, py, pz)
    local candidates = sampler.multi_ring(px, py, SAMPLE_RADII, SAMPLE_COUNT)
    local min_dist   = explorer.MIN_NODE_DIST
    local budget     = explorer.BUDGET_PER_TICK
    local added      = 0

    -- Shuffle to avoid directional bias.
    for i = #candidates, 2, -1 do
        local j = math.random(1, i)
        candidates[i], candidates[j] = candidates[j], candidates[i]
    end

    for _, c in ipairs(candidates) do
        if added >= budget then break end

        if is_distinct(zone_graph, c.x, c.y, min_dist) then
            local valid, cz = validate_candidate(zone_graph, c.x, c.y, pz)
            if valid then
                if cz == nil then cz = pz end
                local new_id = zone_graph.add_node(c.x, c.y, cz)

                -- Connect to ALL reachable nearby nodes, not just the nearest.
                local nearby = nodes_within(zone_graph, c.x, c.y, CONNECT_RADIUS)
                for _, nid in ipairs(nearby) do
                    if nid ~= new_id and not has_edge(zone_graph, new_id, nid) then
                        local n = zone_graph.nodes[nid]
                        if segment_clear(n.x, n.y, c.x, c.y, pz) then
                            zone_graph.add_edge(new_id, nid)
                        end
                    end
                end

                -- Ensure at least one edge (fallback to nearest).
                if #(zone_graph.adjacency[new_id] or {}) == 0 then
                    local nearest = zone_graph.nearest_node(c.x, c.y)
                    if nearest and nearest ~= new_id then
                        zone_graph.add_edge(new_id, nearest)
                    end
                end

                zone_graph.dirty = true
                added = added + 1
            end
        end
    end

    -- Cross-link: connect existing nearby node pairs that lack edges.
    -- This stitches the mesh when the player revisits explored territory.
    local player_nearby = nodes_within(zone_graph, px, py, CROSSLINK_RADIUS)
    local links = 0
    for i = 1, #player_nearby do
        if links >= CROSSLINK_BUDGET then break end
        for j = i + 1, #player_nearby do
            if links >= CROSSLINK_BUDGET then break end
            local a, b = player_nearby[i], player_nearby[j]
            if not has_edge(zone_graph, a, b) then
                local na, nb = zone_graph.nodes[a], zone_graph.nodes[b]
                local dx = na.x - nb.x
                local dy = na.y - nb.y
                if dx*dx + dy*dy <= CROSSLINK_RADIUS * CROSSLINK_RADIUS then
                    if segment_clear(na.x, na.y, nb.x, nb.y, pz) then
                        zone_graph.add_edge(a, b)
                        zone_graph.dirty = true
                        links = links + 1
                    end
                end
            end
        end
    end
end

return explorer
