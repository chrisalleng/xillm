--[[
* mapper - graph.lua
* Node/edge graph data structure and A* pathfinding.
--]]

local graph = {}

-- Node list: array of { id, x, y, z }
graph.nodes = {}
-- Adjacency list: graph.adjacency[node_id] = { neighbor_id, ... }
graph.adjacency = {}
-- ID of the most recently added node (for edge-chaining during exploration)
graph.last_node_id = nil
-- True only when exploration has added new nodes since the last load/reset.
-- Navmesh-loaded graphs start clean; we only write back if the player explored.
graph.dirty = false

-------------------------------------------------------------------------------
-- Reset all graph state (called on zone change)
-------------------------------------------------------------------------------
function graph.reset()
    graph.nodes = {}
    graph.adjacency = {}
    graph.last_node_id = nil
    graph.dirty = false
end

-------------------------------------------------------------------------------
-- Add a node at (x, y, z).
-- x = LocalPosition.X (east/west)
-- y = LocalPosition.Y (north/south)
-- z = LocalPosition.Z (elevation)
-- Horizontal distance calculations use x and y only.
-- Returns the new node's id.
-------------------------------------------------------------------------------
function graph.add_node(x, y, z)
    local id = #graph.nodes + 1
    graph.nodes[id] = { id = id, x = x, y = y, z = z }
    graph.adjacency[id] = {}
    return id
end

-------------------------------------------------------------------------------
-- Add a bidirectional edge between two node IDs (deduplicates).
-------------------------------------------------------------------------------
function graph.add_edge(a, b)
    if a == b then return end

    local function has_edge(from, to)
        for _, nb in ipairs(graph.adjacency[from] or {}) do
            if nb == to then return true end
        end
        return false
    end

    if not has_edge(a, b) then
        graph.adjacency[a] = graph.adjacency[a] or {}
        table.insert(graph.adjacency[a], b)
    end
    if not has_edge(b, a) then
        graph.adjacency[b] = graph.adjacency[b] or {}
        table.insert(graph.adjacency[b], a)
    end
end

-------------------------------------------------------------------------------
-- Sample current position; add a node if far enough from the last one.
-- Connects the new node to graph.last_node_id automatically.
-- Returns the new or existing last node id.
-------------------------------------------------------------------------------
local NODE_DIST2 = 4.0  -- 2 yalm threshold squared (horizontal: x and y)

function graph.try_add_node(x, y, z)
    if graph.last_node_id == nil then
        graph.last_node_id = graph.add_node(x, y, z)
        graph.dirty = true
        return graph.last_node_id
    end

    local last = graph.nodes[graph.last_node_id]
    local dx = x - last.x
    local dy = y - last.y   -- north/south axis
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
-- x = LocalPosition.X (east/west), y = LocalPosition.Y (north/south)
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
-- A* pathfinding. Returns an array of node IDs [start..goal] or nil.
-------------------------------------------------------------------------------
function graph.astar(start_id, goal_id)
    if start_id == nil or goal_id == nil then return nil end
    if start_id == goal_id then return { start_id } end
    if graph.nodes[goal_id] == nil then return nil end

    local goal = graph.nodes[goal_id]

    local function heuristic(node_id)
        local n = graph.nodes[node_id]
        local dx = n.x - goal.x
        local dy = n.y - goal.y   -- north/south axis
        return math.sqrt(dx * dx + dy * dy)
    end

    local open = {}         -- array of { id, g, f }
    local came_from = {}
    local g_score = {}
    local closed = {}

    g_score[start_id] = 0
    table.insert(open, { id = start_id, g = 0, f = heuristic(start_id) })

    while #open > 0 do
        -- Pop the entry with the lowest f score
        table.sort(open, function(a, b) return a.f < b.f end)
        local current = table.remove(open, 1)
        local cur_id = current.id

        if cur_id == goal_id then
            -- Reconstruct path
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
                local edge_cost = math.sqrt(dx * dx + dy * dy)
                local tentative_g = current.g + edge_cost

                if g_score[nb_id] == nil or tentative_g < g_score[nb_id] then
                    came_from[nb_id] = cur_id
                    g_score[nb_id]   = tentative_g
                    local f = tentative_g + heuristic(nb_id)

                    -- Update or insert in open set
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

    return nil  -- No path found
end

-------------------------------------------------------------------------------
-- Path simplification via Douglas-Peucker.
-- Removes intermediate waypoints that lie within `tolerance` yalms of the
-- straight line between their neighbours.  Call this on the raw A* result
-- before handing it to the navigator to eliminate redundant micro-waypoints
-- on straight or near-straight segments.
-- Returns a new (shorter) array of node IDs with the same endpoints.
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
        if keep[i] then
            table.insert(result, path[i])
        end
    end
    return result
end

-------------------------------------------------------------------------------
-- Serialize nodes array for JSON export
-------------------------------------------------------------------------------
function graph.serialize_nodes()
    local out = {}
    for _, n in ipairs(graph.nodes) do
        table.insert(out, { id = n.id, x = n.x, y = n.y, z = n.z })
    end
    return out
end

-------------------------------------------------------------------------------
-- Serialize edges as a flat list { a, b } for JSON export (each pair once)
-------------------------------------------------------------------------------
function graph.serialize_edges()
    local out = {}
    local seen = {}
    for a, neighbors in pairs(graph.adjacency) do
        for _, b in ipairs(neighbors) do
            local key = math.min(a, b) .. '_' .. math.max(a, b)
            if not seen[key] then
                seen[key] = true
                table.insert(out, { a = a, b = b })
            end
        end
    end
    return out
end

-------------------------------------------------------------------------------
-- Load graph data from a deserialized JSON table
-------------------------------------------------------------------------------
function graph.load(data)
    graph.reset()
    if data.nodes then
        for _, n in ipairs(data.nodes) do
            graph.nodes[n.id] = { id = n.id, x = n.x, y = n.y, z = n.z }
            graph.adjacency[n.id] = graph.adjacency[n.id] or {}
        end
    end
    if data.edges then
        for _, e in ipairs(data.edges) do
            graph.add_edge(e.a, e.b)
        end
    end
    -- last_node_id stays nil; explorer will attach to nearest node on resume
end

-------------------------------------------------------------------------------
-- Count helpers
-------------------------------------------------------------------------------
function graph.node_count()
    return #graph.nodes
end

function graph.edge_count()
    local edges = graph.serialize_edges()
    return #edges
end

return graph
