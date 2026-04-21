--[[
* nav - graph/zone_graph.lua
*
* Per-zone discovered traversal graph.
* Enhanced from the original graph.lua; interface is backward-compatible with
* movement_bridge.lua which expects:
*   g.nodes[id].{x, y, z}
*   g.adjacency[id] = {neighbor_id, ...}
*   g.nearest_node(x, y)
*   g.get_frontier(min_neighbors)
*   g.astar(start_id, goal_id)
*
* Additions over graph.lua:
*   - Per-edge confidence scores (float 0.0–1.0)
*   - Confidence-weighted A* cost (lower confidence = higher cost)
*   - Frontier scoring helpers used by graph/frontier.lua
--]]

local graph = {}

-- Node list: array of { id, x, y, z }
graph.nodes     = {}
-- Adjacency list: graph.adjacency[node_id] = { neighbor_id, ... }
graph.adjacency = {}
-- Per-edge confidence: graph.confidence[a][b] = 0.0..1.0
-- Initialized to 0.5 (neutral); increases with successful traversal.
graph.confidence = {}
-- ID of the most recently added node (for edge-chaining during exploration)
graph.last_node_id = nil
-- True when exploration has added new nodes/edges since the last save.
graph.dirty = false
graph._edge_count = 0

-------------------------------------------------------------------------------
-- Reset all graph state (called on zone change)
-------------------------------------------------------------------------------
function graph.reset()
    graph.nodes      = {}
    graph.adjacency  = {}
    graph.confidence = {}
    graph.last_node_id = nil
    graph.dirty = false
    graph._edge_count = 0
end

-------------------------------------------------------------------------------
-- Add a node at (x, y, z).
-- x = LocalPosition.X (east/west)
-- y = LocalPosition.Y (north/south)
-- z = LocalPosition.Z (elevation)
-- Returns the new node's id.
-------------------------------------------------------------------------------
function graph.add_node(x, y, z)
    local id = #graph.nodes + 1
    graph.nodes[id]      = { id = id, x = x, y = y, z = z }
    graph.adjacency[id]  = {}
    graph.confidence[id] = {}
    return id
end

-------------------------------------------------------------------------------
-- Add a bidirectional edge between two node IDs (deduplicates).
-- Initial confidence = 0.5 (neutral / unconfirmed).
-------------------------------------------------------------------------------
function graph.add_edge(a, b)
    if a == b then return end

    local function has_edge(from, to)
        for _, nb in ipairs(graph.adjacency[from] or {}) do
            if nb == to then return true end
        end
        return false
    end

    local added = false
    if not has_edge(a, b) then
        graph.adjacency[a]  = graph.adjacency[a]  or {}
        graph.confidence[a] = graph.confidence[a] or {}
        table.insert(graph.adjacency[a], b)
        graph.confidence[a][b] = graph.confidence[a][b] or 0.5
        added = true
    end
    if not has_edge(b, a) then
        graph.adjacency[b]  = graph.adjacency[b]  or {}
        graph.confidence[b] = graph.confidence[b] or {}
        table.insert(graph.adjacency[b], a)
        graph.confidence[b][a] = graph.confidence[b][a] or 0.5
    end
    if added then graph._edge_count = graph._edge_count + 1 end
end

-------------------------------------------------------------------------------
-- Sample current position; add a node if far enough from the last one.
-- Returns the new or existing last node id.
-------------------------------------------------------------------------------
local NODE_DIST2 = 4.0  -- 2 yalm threshold squared

function graph.try_add_node(x, y, z)
    if graph.last_node_id == nil then
        graph.last_node_id = graph.add_node(x, y, z)
        graph.dirty = true
        return graph.last_node_id
    end

    local last = graph.nodes[graph.last_node_id]
    local dx = x - last.x
    local dy = y - last.y
    if (dx * dx + dy * dy) >= NODE_DIST2 then
        local new_id = graph.add_node(x, y, z)
        graph.add_edge(graph.last_node_id, new_id)
        graph.last_node_id = new_id
        graph.dirty = true
        return new_id
    end

    return graph.last_node_id
end

-------------------------------------------------------------------------------
-- Find the nearest node to horizontal position (x, y).
-- Returns node_id or nil if graph is empty.
-------------------------------------------------------------------------------
function graph.nearest_node(x, y)
    local best_id, best_dist2 = nil, math.huge
    for id, node in ipairs(graph.nodes) do
        local dx = node.x - x
        local dy = node.y - y
        local d2 = dx * dx + dy * dy
        if d2 < best_dist2 then
            best_dist2 = d2
            best_id = id
        end
    end
    return best_id
end

-------------------------------------------------------------------------------
-- Return the set of node IDs considered "frontier" (fewer than min_neighbors
-- edges, meaning unexplored directions likely exist nearby).
-------------------------------------------------------------------------------
function graph.get_frontier(min_neighbors)
    min_neighbors = min_neighbors or 4
    local frontier = {}
    for id, neighbors in pairs(graph.adjacency) do
        if #neighbors < min_neighbors then
            table.insert(frontier, id)
        end
    end
    return frontier
end

-------------------------------------------------------------------------------
-- A* pathfinding over the discovered graph.
-- When use_confidence is true the edge cost is scaled by (2 - confidence)
-- so low-confidence edges are treated as longer / less preferred.
-- Returns an array of node IDs [start..goal] or nil.
-------------------------------------------------------------------------------
function graph.astar(start_id, goal_id, use_confidence)
    if start_id == nil or goal_id == nil then return nil end
    if start_id == goal_id then return { start_id } end
    if graph.nodes[goal_id] == nil then return nil end

    local goal = graph.nodes[goal_id]

    local function heuristic(node_id)
        local n = graph.nodes[node_id]
        local dx = n.x - goal.x
        local dy = n.y - goal.y
        return math.sqrt(dx * dx + dy * dy)
    end

    local open      = {}
    local came_from = {}
    local g_score   = {}
    local closed    = {}

    g_score[start_id] = 0
    table.insert(open, { id = start_id, g = 0, f = heuristic(start_id) })

    while #open > 0 do
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

        for _, nb_id in ipairs(graph.adjacency[cur_id] or {}) do
            if not closed[nb_id] then
                local cur_node = graph.nodes[cur_id]
                local nb_node  = graph.nodes[nb_id]
                local dx = nb_node.x - cur_node.x
                local dy = nb_node.y - cur_node.y
                local base_cost = math.sqrt(dx * dx + dy * dy)

                -- Apply confidence penalty: low-confidence edges cost more.
                local conf = 0.5
                if use_confidence and graph.confidence[cur_id] then
                    conf = graph.confidence[cur_id][nb_id] or 0.5
                end
                local edge_cost = base_cost * (2.0 - conf)

                local tentative_g = current.g + edge_cost

                if g_score[nb_id] == nil or tentative_g < g_score[nb_id] then
                    came_from[nb_id] = cur_id
                    g_score[nb_id]   = tentative_g
                    local f = tentative_g + heuristic(nb_id)

                    local found = false
                    for _, entry in ipairs(open) do
                        if entry.id == nb_id then
                            entry.g = tentative_g
                            entry.f = f
                            found = true
                            break
                        end
                    end
                    if not found then
                        table.insert(open, { id = nb_id, g = tentative_g, f = f })
                    end
                end
            end
        end
    end

    return nil
end

-------------------------------------------------------------------------------
-- Path simplification via Douglas-Peucker.
-------------------------------------------------------------------------------
local function _pt_line_dist(px, py, ax, ay, bx, by)
    local dx, dy = bx - ax, by - ay
    local len2   = dx * dx + dy * dy
    if len2 == 0 then
        local ex, ey = px - ax, py - ay
        return math.sqrt(ex * ex + ey * ey)
    end
    local t  = math.max(0, math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2))
    local cx = ax + t * dx
    local cy = ay + t * dy
    local ex, ey = px - cx, py - cy
    return math.sqrt(ex * ex + ey * ey)
end

local function _dp_simplify(path, lo, hi, tolerance, nodes, keep)
    if hi <= lo + 1 then return end
    local na = nodes[path[lo]]
    local nb = nodes[path[hi]]
    local max_dist, max_i = 0, lo
    for i = lo + 1, hi - 1 do
        local n = nodes[path[i]]
        local d = _pt_line_dist(n.x, n.y, na.x, na.y, nb.x, nb.y)
        if d > max_dist then
            max_dist, max_i = d, i
        end
    end
    if max_dist > tolerance then
        _dp_simplify(path, lo, max_i, tolerance, nodes, keep)
        keep[max_i] = true
        _dp_simplify(path, max_i, hi, tolerance, nodes, keep)
    end
end

function graph.simplify_path(path, tolerance)
    tolerance = tolerance or 2.0
    if #path <= 2 then return path end
    local keep = {}
    keep[1]     = true
    keep[#path] = true
    _dp_simplify(path, 1, #path, tolerance, graph.nodes, keep)
    local result = {}
    for i = 1, #path do
        if keep[i] then table.insert(result, path[i]) end
    end
    return result
end

-------------------------------------------------------------------------------
-- Confidence management
-------------------------------------------------------------------------------

-- Record a successful traversal of edge (a→b); raises confidence toward 1.0.
function graph.record_success(a, b)
    if not graph.confidence[a] then return end
    local c = graph.confidence[a][b] or 0.5
    graph.confidence[a][b] = math.min(1.0, c + 0.1)
    if graph.confidence[b] then
        local cr = graph.confidence[b][a] or 0.5
        graph.confidence[b][a] = math.min(1.0, cr + 0.1)
    end
    graph.dirty = true
end

-- Record a failed traversal; lowers confidence toward 0.0.
function graph.record_failure(a, b)
    if not graph.confidence[a] then return end
    local c = graph.confidence[a][b] or 0.5
    graph.confidence[a][b] = math.max(0.0, c - 0.2)
    if graph.confidence[b] then
        local cr = graph.confidence[b][a] or 0.5
        graph.confidence[b][a] = math.max(0.0, cr - 0.2)
    end
    graph.dirty = true
end

-- Returns true when confidence is so low the edge should be temporarily skipped.
function graph.is_edge_suppressed(a, b, threshold)
    threshold = threshold or 0.1
    if not graph.confidence[a] then return false end
    local c = graph.confidence[a][b] or 0.5
    return c < threshold
end

-------------------------------------------------------------------------------
-- Serialization
-------------------------------------------------------------------------------

function graph.serialize_nodes()
    local out = {}
    for _, n in ipairs(graph.nodes) do
        table.insert(out, { id = n.id, x = n.x, y = n.y, z = n.z })
    end
    return out
end

function graph.serialize_edges()
    local out  = {}
    local seen = {}
    for a, neighbors in pairs(graph.adjacency) do
        for _, b in ipairs(neighbors) do
            local key = math.min(a, b) .. '_' .. math.max(a, b)
            if not seen[key] then
                seen[key] = true
                local conf = (graph.confidence[a] and graph.confidence[a][b]) or 0.5
                table.insert(out, { a = a, b = b, conf = conf })
            end
        end
    end
    return out
end

function graph.load(data)
    graph.reset()
    if data.nodes then
        for _, n in ipairs(data.nodes) do
            graph.nodes[n.id]      = { id = n.id, x = n.x, y = n.y, z = n.z }
            graph.adjacency[n.id]  = graph.adjacency[n.id]  or {}
            graph.confidence[n.id] = graph.confidence[n.id] or {}
        end
    end
    if data.edges then
        for _, e in ipairs(data.edges) do
            graph.add_edge(e.a, e.b)
            -- Restore persisted confidence if available.
            if e.conf then
                graph.confidence[e.a] = graph.confidence[e.a] or {}
                graph.confidence[e.b] = graph.confidence[e.b] or {}
                graph.confidence[e.a][e.b] = e.conf
                graph.confidence[e.b][e.a] = e.conf
            end
        end
    end
    -- last_node_id stays nil; explorer will attach to nearest node on resume.
end

function graph.node_count()
    return #graph.nodes
end

function graph.edge_count()
    return graph._edge_count
end

return graph
