--[[
* nav - planning/astar.lua
*
* Standalone A* search over a zone_graph.
* Used by planner.lua as its pathfinding primitive.
*
* Differs from zone_graph.astar() in that it:
*   - Takes the graph as an explicit argument (does not use the singleton)
*   - Supports edge suppression via confidence thresholds
*   - Skips suppressed edges so the planner avoids known-bad routes
--]]

local astar = {}

-- Minimum confidence for an edge to be traversable.
-- Edges below this threshold are treated as blocked during planning.
astar.SUPPRESSION_THRESHOLD = 0.1

-------------------------------------------------------------------------------
-- Search from start_id to goal_id over zone_graph.
-- opts table (all optional):
--   use_confidence    : bool  — weight edge cost by confidence (default true)
--   skip_suppressed   : bool  — ignore edges below threshold (default true)
--   suppression_threshold : float (default astar.SUPPRESSION_THRESHOLD)
--
-- Returns an array of node IDs [start..goal] or nil if unreachable.
-------------------------------------------------------------------------------
function astar.search(zone_graph, start_id, goal_id, opts)
    opts = opts or {}
    local use_conf  = opts.use_confidence  ~= false   -- default true
    local skip_supp = opts.skip_suppressed ~= false   -- default true
    local threshold = opts.suppression_threshold or astar.SUPPRESSION_THRESHOLD

    if start_id == nil or goal_id == nil then return nil end
    if start_id == goal_id then return { start_id } end
    local goal_node = zone_graph.nodes[goal_id]
    if goal_node == nil then return nil end

    local function heuristic(nid)
        local n  = zone_graph.nodes[nid]
        local dx = n.x - goal_node.x
        local dy = n.y - goal_node.y
        return math.sqrt(dx * dx + dy * dy)
    end

    local open      = {}
    local came_from = {}
    local g_score   = {}
    local closed    = {}

    g_score[start_id] = 0
    table.insert(open, { id = start_id, g = 0, f = heuristic(start_id) })

    while #open > 0 do
        -- Pop lowest-f entry.
        table.sort(open, function(a, b) return a.f < b.f end)
        local current = table.remove(open, 1)
        local cur_id  = current.id

        if cur_id == goal_id then
            local path = {}
            local c = goal_id
            while c ~= nil do
                table.insert(path, 1, c)
                c = came_from[c]
            end
            return path
        end

        closed[cur_id] = true

        for _, nb_id in ipairs(zone_graph.adjacency[cur_id] or {}) do
            if not closed[nb_id] then
                -- Skip suppressed edges when requested.
                if skip_supp and zone_graph.confidence[cur_id] then
                    local conf = zone_graph.confidence[cur_id][nb_id] or 0.5
                    if conf < threshold then goto next_neighbor end
                end

                local cur_node = zone_graph.nodes[cur_id]
                local nb_node  = zone_graph.nodes[nb_id]
                local dx = nb_node.x - cur_node.x
                local dy = nb_node.y - cur_node.y
                local base_cost = math.sqrt(dx * dx + dy * dy)

                local edge_cost = base_cost
                if use_conf and zone_graph.confidence[cur_id] then
                    local conf = zone_graph.confidence[cur_id][nb_id] or 0.5
                    edge_cost  = base_cost * (2.0 - conf)
                end

                local tg = current.g + edge_cost

                if g_score[nb_id] == nil or tg < g_score[nb_id] then
                    came_from[nb_id] = cur_id
                    g_score[nb_id]   = tg
                    local f = tg + heuristic(nb_id)

                    local found = false
                    for _, entry in ipairs(open) do
                        if entry.id == nb_id then
                            entry.g = tg
                            entry.f = f
                            found = true
                            break
                        end
                    end
                    if not found then
                        table.insert(open, { id = nb_id, g = tg, f = f })
                    end
                end

                ::next_neighbor::
            end
        end
    end

    return nil
end

return astar
