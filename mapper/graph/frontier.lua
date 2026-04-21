--[[
* nav - graph/frontier.lua
*
* Frontier node identification and scoring.
*
* A "frontier" node is at the boundary of the explored area — it has at
* least one angular sector with no nearby graph nodes, indicating
* unexplored territory in that direction.
--]]

local frontier = {}

local SECTOR_COUNT = 6
local SECTOR_ANGLE = 2 * math.pi / SECTOR_COUNT
local SECTOR_RADIUS = 6.0

-------------------------------------------------------------------------------
-- Check if a node is a frontier by looking for empty angular sectors.
-- Uses adjacency + 2-hop neighbors for efficiency instead of all-nodes scan.
-------------------------------------------------------------------------------
local function is_frontier_node(zone_graph, id)
    local n = zone_graph.nodes[id]
    if n == nil then return false end

    local r2 = SECTOR_RADIUS * SECTOR_RADIUS
    local sectors = {}
    for s = 1, SECTOR_COUNT do sectors[s] = false end

    -- Collect 2-hop neighborhood for a bounded check.
    local to_check = {}
    local seen = { [id] = true }
    for _, nb in ipairs(zone_graph.adjacency[id] or {}) do
        if not seen[nb] then
            seen[nb] = true
            to_check[#to_check + 1] = nb
        end
        for _, nb2 in ipairs(zone_graph.adjacency[nb] or {}) do
            if not seen[nb2] then
                seen[nb2] = true
                to_check[#to_check + 1] = nb2
            end
        end
    end

    for _, oid in ipairs(to_check) do
        local other = zone_graph.nodes[oid]
        if other then
            local dx = other.x - n.x
            local dy = other.y - n.y
            if dx*dx + dy*dy <= r2 then
                local angle = math.atan2(dx, dy)
                if angle < 0 then angle = angle + 2 * math.pi end
                local sector = math.floor(angle / SECTOR_ANGLE) + 1
                if sector > SECTOR_COUNT then sector = SECTOR_COUNT end
                sectors[sector] = true
            end
        end
    end

    for s = 1, SECTOR_COUNT do
        if not sectors[s] then return true end
    end
    return false
end

-------------------------------------------------------------------------------
-- Return all frontier node IDs.
-------------------------------------------------------------------------------
function frontier.get_frontiers(zone_graph, _unused)
    local result = {}
    for id, _ in ipairs(zone_graph.nodes) do
        if is_frontier_node(zone_graph, id) then
            result[#result + 1] = id
        end
    end
    return result
end

-------------------------------------------------------------------------------
-- Score and sort frontier nodes.
-------------------------------------------------------------------------------
function frontier.score(zone_graph, frontier_ids, px, py, gx, gy)
    local scored = {}

    local has_goal = (gx ~= nil and gy ~= nil)
    local goal_dx, goal_dy = 0, 0
    if has_goal then
        local gdist = math.sqrt((gx - px)^2 + (gy - py)^2)
        if gdist > 0.001 then
            goal_dx = (gx - px) / gdist
            goal_dy = (gy - py) / gdist
        end
    end

    for _, id in ipairs(frontier_ids) do
        local n = zone_graph.nodes[id]
        if n then
            local dx    = n.x - px
            local dy    = n.y - py
            local dist  = math.sqrt(dx * dx + dy * dy)
            local nb    = #(zone_graph.adjacency[id] or {})

            local align = 0
            if has_goal and dist > 0.001 then
                align = (dx / dist) * goal_dx + (dy / dist) * goal_dy
            end

            local score = align * 2.0
                        + math.max(0, 4 - nb) * 0.5
                        - dist * 0.01

            scored[#scored + 1] = { id = id, score = score, node = n, dist = dist }
        end
    end

    table.sort(scored, function(a, b) return a.score > b.score end)
    return scored
end

-------------------------------------------------------------------------------
-- Return the best single frontier node ID.
-------------------------------------------------------------------------------
function frontier.best(zone_graph, px, py, gx, gy)
    local ids    = frontier.get_frontiers(zone_graph)
    if #ids == 0 then return nil end
    local scored = frontier.score(zone_graph, ids, px, py, gx, gy)
    return scored[1] and scored[1].id or nil
end

-------------------------------------------------------------------------------
-- Score boundary cells from the vision system for explore-mode targeting.
-- Returns sorted array of { x, y, score, node_id }.
-------------------------------------------------------------------------------
function frontier.get_vision_frontiers(vision_mod, zone_graph, px, py, last_dir)
    local boundary = vision_mod.get_boundary_cells()
    if #boundary == 0 then return {} end

    local scored = {}
    for _, cell in ipairs(boundary) do
        local dx = cell.x - px
        local dy = cell.y - py
        local dist = math.sqrt(dx * dx + dy * dy)

        -- Information gain: count unseen neighbors.
        -- (Already filtered to boundary cells, so all have at least 1 unseen neighbor.)

        -- Distance sweet spot: 30-100 yalms is ideal for exploration movement.
        local dist_score = 0
        if dist < 10 then
            dist_score = dist / 10
        elseif dist < 30 then
            dist_score = 1.0
        elseif dist < 100 then
            dist_score = 1.0 - (dist - 30) * 0.005
        else
            dist_score = 0.65 - (dist - 100) * 0.002
        end

        -- Directional momentum.
        local momentum = 0
        if last_dir and dist > 1.0 then
            momentum = (dx / dist) * last_dir.dx + (dy / dist) * last_dir.dy
        end

        local score = dist_score * 10.0 + momentum * 4.0

        scored[#scored + 1] = {
            x = cell.x, y = cell.y,
            score = score,
            node_id = cell.node_id,
            dist = dist,
        }
    end

    table.sort(scored, function(a, b) return a.score > b.score end)
    return scored
end

return frontier
