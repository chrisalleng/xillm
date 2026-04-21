--[[
* nav - planning/planner.lua
*
* High-level planning policy.
*
* Decision tree each tick:
*   1. If a known path exists (already planned) and collision validates the
*      next segment, consume waypoints from it.
*   2. If the goal is set but no known path, run A* over the discovered graph.
*      If reachable, start following the path.
*   3. If A* can't reach the goal (graph incomplete), select the best frontier
*      node toward the goal and navigate there to grow the graph.
*   4. If no frontiers: graph is exhausted, report failure.
*
* planner.tick() returns a {x, y} waypoint for the adapter, or nil.
* The adapter translates this into a movement_bridge navigate call.
--]]

local S          = require('core.state')
local astar_mod  = require('planning.astar')
local frontier   = require('graph.frontier')
local raycast    = require('geometry.raycast')

local planner = {}

-- How often (frames) to replan if the path becomes stale.
local REPLAN_INTERVAL = 180  -- ~3 seconds at 60 fps

local _last_replan_frame = 0
local _frontier_target   = nil  -- node ID currently navigating toward (frontier mode)

-------------------------------------------------------------------------------
-- Set a new goal.  Clears any active path so a fresh plan is computed.
-------------------------------------------------------------------------------
function planner.set_goal(x, y)
    S.goal        = { x = x, y = y }
    S.active_path = nil
    _frontier_target = nil
end

function planner.clear_goal()
    S.goal           = nil
    S.active_path    = nil
    _frontier_target = nil
end

-------------------------------------------------------------------------------
-- Validate the next segment of the current active path using collision.
-- Returns true if the segment is passable (or if no collision data exists).
-------------------------------------------------------------------------------
local function validate_next_segment(zone_graph, path_idx)
    if S.spatial_index == nil or S.collision == nil then return true end
    if S.active_path == nil or path_idx > #S.active_path - 1 then return true end

    local a = zone_graph.nodes[S.active_path[path_idx]]
    local b = zone_graph.nodes[S.active_path[path_idx + 1]]
    if a == nil or b == nil then return true end

    if raycast.segment_blocked(
        S.spatial_index, S.collision.triangles,
        a.x, a.y, a.z, b.x, b.y, b.z) == true then
        return false
    end
    return true
end

-------------------------------------------------------------------------------
-- Attempt to plan a path from current position to S.goal.
-- Populates S.active_path on success.
-- Returns true if a path was found.
-------------------------------------------------------------------------------
local function try_plan(zone_graph, px, py)
    if S.goal == nil then return false end
    if zone_graph.node_count() == 0 then return false end

    local start_id = zone_graph.nearest_node(px, py)
    local goal_id  = zone_graph.nearest_node(S.goal.x, S.goal.y)
    if start_id == nil or goal_id == nil then return false end

    local path = astar_mod.search(zone_graph, start_id, goal_id)
    if path == nil then return false end

    if S.spatial_index ~= nil and S.collision ~= nil then
        local player = GetPlayerEntity()
        local z_hint = (player and player.ActorPointer ~= 0)
            and player.Movement.LocalPosition.Z or 0.0

        -- Validate the raw A* path edges against collision (wall check only).
        -- Graph edges were created with wall validation; skip segment_walkable
        -- here to avoid rejecting edges with minor height changes that the
        -- character can traverse.
        for i = 1, #path - 1 do
            local a = zone_graph.nodes[path[i]]
            local b = zone_graph.nodes[path[i + 1]]
            if a and b then
                local blocked = raycast.segment_blocked(
                    S.spatial_index, S.collision.triangles,
                    a.x, a.y, a.z, b.x, b.y, b.z) == true
                if blocked then
                    zone_graph.record_failure(path[i], path[i + 1])
                    zone_graph.record_failure(path[i], path[i + 1])
                    if i <= 2 then return false end
                    local trimmed = {}
                    for j = 1, i do trimmed[j] = path[j] end
                    path = trimmed
                    break
                end
            end
        end

        S.active_path = path
    else
        S.active_path = path
    end
    return true
end

-------------------------------------------------------------------------------
-- planner.tick(zone_graph, px, py, frame) → { next_waypoint_node_id } or nil
--
-- Returns nil when:
--   - No goal is set
--   - Graph is empty and no frontiers exist
--   - Navigation system should handle this frame itself (path in progress)
--
-- Returns { path, graph_ref, mode } where:
--   mode = 'path'     → follow the returned path array
--   mode = 'frontier' → navigate to frontier node (node_id in path[1])
-------------------------------------------------------------------------------
function planner.tick(zone_graph, px, py, frame)
    if S.goal == nil then return nil end

    -- Periodic replan: invalidate stale path.
    if frame - _last_replan_frame >= REPLAN_INTERVAL then
        _last_replan_frame = frame
        S.active_path = nil
    end

    -- If we have an active path, validate and return it.
    if S.active_path ~= nil and #S.active_path > 0 then
        -- Quick collision check on the first pending segment.
        local ok = validate_next_segment(zone_graph, 1)
        if ok then
            return { path = S.active_path, graph_ref = zone_graph, mode = 'path' }
        else
            -- Segment blocked: replan.
            S.active_path = nil
        end
    end

    -- Try A* to goal.
    if try_plan(zone_graph, px, py) then
        return { path = S.active_path, graph_ref = zone_graph, mode = 'path' }
    end

    -- Goal unreachable from known graph: navigate toward best frontier.
    local fid = frontier.best(zone_graph, px, py, S.goal.x, S.goal.y)
    if fid == nil then
        -- No frontier nodes: graph is exhausted and goal still unreachable.
        return nil
    end

    -- Always recompute the frontier path (don't cache: the player moved since
    -- the last plan, and caching caused direct-drive to fire after reaching a
    -- frontier because the same frontier id was still the best candidate).
    _frontier_target = fid
    local start_id = zone_graph.nearest_node(px, py)
    local path = start_id and astar_mod.search(zone_graph, start_id, fid) or nil
    if path then
        return { path = path, graph_ref = zone_graph, mode = 'frontier' }
    end

    return nil
end

return planner
