--[[
* nav - movement/movement_bridge.lua
*
* PRESERVED MOVEMENT LAYER — do not modify without a clear reason.
* This is the in-game character movement execution layer adapted from the
* original navigator.lua.  It handles all physical movement: AutoFollow
* delta writes, stuck detection, waypoint following, and probe-based
* autonomous exploration.
*
* Coordinate system (Ashita v4 / FFXI):
*   LocalPosition.X = east/west  (east is positive)
*   LocalPosition.Y = north/south (horizontal depth)
*   LocalPosition.Z = elevation   (vertical, mostly irrelevant for navigation)
*
* IAutoFollow axes:
*   SetFollowDeltaX = east/west
*   SetFollowDeltaY = north/south
*   SetFollowDeltaZ = elevation (leave near 0, let terrain handle it)
*
* Two modes:
*   NAVIGATE  (/mapper goto)  - follow A* path waypoint by waypoint
*   EXPLORE   (/mapper explore) - autonomous frontier exploration
*     1. Commit a FIXED probe point PROBE_DIST yalms ahead.
*     2. Drive toward it; record nodes while moving.
*     3. Probe reached -> continue straight if area ahead is unexplored;
*        otherwise redirect toward the least-explored heading.
*     4. Stuck -> build a priority queue of untried headings sorted by how
*        unexplored they are; try them one at a time (most promising first).
*     5. All headings exhausted -> A* to best frontier node and resume.
*     6. No reachable frontier -> exploration complete.
*
* IAutoFollow deltas must be written EVERY frame or the game stops the character.
*
* Public interface consumed by movement_adapter.lua:
*   bridge.is_active()           -> bool
*   bridge.mode()                -> 'idle' | 'navigate' | 'explore'
*   bridge.get_probe()           -> probe_x, probe_y
*   bridge.get_heading()         -> heading (radians)
*   bridge.get_probe_count()     -> integer
*   bridge.get_stuck_frames()    -> integer
*   bridge.stop()
*   bridge.start_navigate(path, graph_ref, on_done, extra_x, extra_y)
*   bridge.start_explore(heading, graph_ref, on_node, on_done, on_debug, sx, sy)
*   bridge.tick(px, py, pz, frame_counter)
--]]

local S       = require('core.state')
local raycast = require('geometry.raycast')
local D       = require('core.debug')

local bridge = {}

-- -------------------------------------------------------------------------
-- Constants (60 fps)
-- -------------------------------------------------------------------------
local WAYPOINT_RADIUS      = 1.5   -- yalms
local PROBE_DIST           = 10.0  -- yalms - distance of each probe target
local PROBE_COOLDOWN       = 90    -- frames of no-stuck-check after committing probe (~1.5s)
local STUCK_FRAMES_NAV     = 150   -- frames before skipping a blocked waypoint (~2.5s)
local MAX_WAYPOINT_SKIPS   = 5     -- consecutive skips before aborting
local STUCK_FRAMES_EXP     = 120   -- frames before trying next rotation (~2s)
local STUCK_CHECK_INTERVAL = 30    -- frames between position-progress snapshots
local MIN_POS_PROGRESS     = 0.4   -- yalms moved in STUCK_CHECK_INTERVAL frames to not be stuck
local HEADING_CANDIDATES   = 6     -- evenly-spaced directions to evaluate
local GRID_STEP            = 4.0   -- skip collision avoidance for waypoints within this distance
local DENSITY_REDIRECT     = 4     -- node count near next probe above which we redirect on success
local MIN_FRONTIER_NB      = 4     -- node neighbor count below which it is a frontier node

-- -------------------------------------------------------------------------
-- State
-- -------------------------------------------------------------------------
local MODE_IDLE     = 'idle'
local MODE_NAVIGATE = 'navigate'
local MODE_NAV_LOCAL = 'navigate_local'
local MODE_EXPLORE  = 'explore'

local s = {
    mode  = MODE_IDLE,

    -- Navigate
    path            = nil,
    waypoint_idx    = 1,
    waypoint_skips  = 0,
    nav_check_x     = 0.0,
    nav_check_y     = 0.0,
    extra_x         = nil,   -- optional raw target after last waypoint (e.g. zone line)
    extra_y         = nil,

    -- Navigate local (coordinate-array paths from hires mesh)
    local_path      = nil,   -- array of {x, y, z}
    local_idx       = 1,

    -- Explore: fixed probe target (X and Y are horizontal axes)
    heading        = 0.0,
    probe_x        = nil,   -- east/west component of probe
    probe_y        = nil,   -- north/south component of probe (LocalPosition.Y)
    probe_count    = 0,
    rotations      = 0,     -- how many directions tried in current stuck episode
    rotation_queue = {},    -- headings to try when stuck, sorted by density ascending
    frontier       = {},

    -- Stuck detection
    stuck_frames   = 0,
    stuck_cooldown = 0,
    check_x        = 0.0,   -- explore: position at last snapshot
    check_y        = 0.0,
    steering       = false,  -- true when actively detouring around obstacle

    -- Callbacks
    on_node   = nil,
    on_done   = nil,
    on_debug  = nil,
    graph_ref = nil,
}

-- -------------------------------------------------------------------------
-- Helpers
-- -------------------------------------------------------------------------

local function stop_autofollow()
    local follow = AshitaCore:GetMemoryManager():GetAutoFollow()
    if follow then follow:SetIsAutoRunning(0) end
end

-- Drive character toward (tx, ty) on the horizontal plane.
local function drive_toward(px, py, tx, ty)
    local follow = AshitaCore:GetMemoryManager():GetAutoFollow()
    if follow == nil then return end
    follow:SetFollowDeltaX(tx - px)
    follow:SetFollowDeltaY(ty - py)
    follow:SetFollowDeltaZ(0)
    follow:SetIsAutoRunning(1)
end

-- 2D horizontal distance using X and Y axes.
local function dist2d(ax, ay, bx, by)
    local dx = ax - bx
    local dy = ay - by
    return math.sqrt(dx * dx + dy * dy)
end

local function normalize_heading(h)
    local pi2 = 2 * math.pi
    while h >= pi2 do h = h - pi2 end
    while h < 0    do h = h + pi2 end
    return h
end

local function dbg(msg)
    if s.on_debug then s.on_debug(msg) end
end

-- -------------------------------------------------------------------------
-- Local collision avoidance.
-- In navigate mode (following a planned path), only checks segment_blocked
-- (walls) since the path edges are already validated.
-- In explore mode, also checks segment_walkable to avoid cliffs/slopes.
-- -------------------------------------------------------------------------
local STEER_LOOKAHEAD = 2.5
local STEER_OFFSETS   = {
    0,
    0.35, -0.35,   -- ±20°
    0.7,  -0.7,    -- ±40°
    1.05, -1.05,   -- ±60°
    1.4,  -1.4,    -- ±80°
    1.75, -1.75,   -- ±100°
    2.1,  -2.1,    -- ±120°
    2.44, -2.44,   -- ±140°
}

local _steer_log_counter = 0

local function steer_toward(px, py, pz, tx, ty, walls_only, tz)
    if S.spatial_index == nil or S.collision == nil then
        s.steering = false
        drive_toward(px, py, tx, ty)
        return
    end

    local dx = tx - px
    local dy = ty - py
    local dist = math.sqrt(dx*dx + dy*dy)
    if dist < 0.5 then
        s.steering = false
        drive_toward(px, py, tx, ty)
        return
    end

    local base_hdg = math.atan2(dx, dy)
    local look = math.min(dist, STEER_LOOKAHEAD)
    local tris = S.collision.triangles
    local t_frac = (dist > 0.01) and (look / dist) or 0
    local end_z = tz and (pz + (tz - pz) * t_frac) or pz

    for _, offset in ipairs(STEER_OFFSETS) do
        local hdg = base_hdg + offset
        local cx = px + math.sin(hdg) * look
        local cy = py + math.cos(hdg) * look

        local wall_hit = raycast.segment_blocked(
            S.spatial_index, tris, px, py, pz, cx, cy, end_z)
        local walkable = true
        if not wall_hit and not walls_only then
            walkable = raycast.segment_walkable(
                S.spatial_index, tris, px, py, cx, cy, pz)
        end
        if not wall_hit and walkable ~= false then
            if offset == 0 then
                s.steering = false
                drive_toward(px, py, tx, ty)
            else
                s.steering = true
                _steer_log_counter = _steer_log_counter + 1
                if _steer_log_counter % 60 == 1 then
                    D.dbg(string.format('steer: offset=%.1f° toward (%.0f,%.0f)',
                        math.deg(offset), px + math.sin(hdg) * 10, py + math.cos(hdg) * 10))
                end
                drive_toward(px, py, px + math.sin(hdg) * dist, py + math.cos(hdg) * dist)
            end
            return
        end
    end

    _steer_log_counter = _steer_log_counter + 1
    if _steer_log_counter % 60 == 1 then
        D.dbg(string.format('steer: ALL BLOCKED at (%.0f,%.0f) toward (%.0f,%.0f)',
            px, py, tx, ty))
    end
    s.steering = false
    stop_autofollow()
end

-- Count graph nodes within PROBE_DIST * 0.75 yalms of (tx, ty).
-- High density = area already explored; low/zero = new territory.
local function count_density(tx, ty, g)
    if g == nil then return 0 end
    local count = 0
    local radius = PROBE_DIST * 0.75
    for _, n in ipairs(g.nodes) do
        if dist2d(n.x, n.y, tx, ty) < radius then
            count = count + 1
        end
    end
    return count
end

-- Return all HEADING_CANDIDATES headings sorted by density ascending (least
-- explored first), optionally excluding the heading most similar to `skip_hdg`.
local function sorted_headings(px, py, g, skip_hdg)
    local step = 2 * math.pi / HEADING_CANDIDATES
    local candidates = {}
    for i = 0, HEADING_CANDIDATES - 1 do
        local hdg = normalize_heading(i * step)
        local tx  = px + PROBE_DIST * math.sin(hdg)
        local ty  = py + PROBE_DIST * math.cos(hdg)
        table.insert(candidates, { hdg = hdg, density = count_density(tx, ty, g) })
    end

    -- Remove the candidate whose heading is closest to skip_hdg (already tried).
    if skip_hdg ~= nil then
        local min_diff, min_i = math.huge, 1
        for i, c in ipairs(candidates) do
            local d = math.abs(normalize_heading(c.hdg - skip_hdg))
            if d > math.pi then d = 2 * math.pi - d end
            if d < min_diff then min_diff, min_i = d, i end
        end
        table.remove(candidates, min_i)
    end

    table.sort(candidates, function(a, b) return a.density < b.density end)
    local result = {}
    for _, c in ipairs(candidates) do table.insert(result, c.hdg) end
    return result
end

-- Commit a new fixed probe point from current horizontal position.
local function commit_probe(px, py)
    s.probe_x        = px + PROBE_DIST * math.sin(s.heading)
    s.probe_y        = py + PROBE_DIST * math.cos(s.heading)
    s.stuck_frames   = 0
    s.stuck_cooldown = PROBE_COOLDOWN
    s.check_x        = px
    s.check_y        = py
    dbg(string.format('Probe -> (%.1f, %.1f) hdg=%.2f', s.probe_x, s.probe_y, s.heading))
end

-- -------------------------------------------------------------------------
-- Public API
-- -------------------------------------------------------------------------

function bridge.is_active()
    return s.mode ~= MODE_IDLE
end

function bridge.mode()
    return s.mode
end

function bridge.get_probe()
    return s.probe_x, s.probe_y
end

function bridge.get_heading()
    return s.heading
end

function bridge.get_probe_count()
    return s.probe_count
end

function bridge.get_stuck_frames()
    return s.stuck_frames
end

function bridge.stop()
    s.mode           = MODE_IDLE
    s.path           = nil
    s.waypoint_idx   = 1
    s.extra_x        = nil
    s.extra_y        = nil
    s.local_path     = nil
    s.local_idx      = 1
    s.probe_x        = nil
    s.probe_y        = nil
    s.probe_count    = 0
    s.stuck_frames   = 0
    s.stuck_cooldown = 0
    s.rotations      = 0
    s.rotation_queue = {}
    s.frontier       = {}
    s.check_x        = 0.0
    s.check_y        = 0.0
    stop_autofollow()
end

-- extra_x, extra_y: optional raw target to drive toward after the last graph
-- waypoint (e.g. a zone line trigger position).
function bridge.start_navigate(path, graph_ref, on_done, extra_x, extra_y)
    bridge.stop()
    if (path == nil or #path == 0) and extra_x == nil then
        if on_done then on_done('empty_path') end
        return
    end
    s.mode           = MODE_NAVIGATE
    s.path           = path or {}
    s.waypoint_idx   = 1
    s.waypoint_skips = 0
    s.extra_x        = extra_x
    s.extra_y        = extra_y
    s.graph_ref      = graph_ref
    s.on_done        = on_done
    s.stuck_frames   = 0
    s.nav_check_x    = 0.0
    s.nav_check_y    = 0.0
end

-- Navigate along a coordinate-array path from the hires mesh.
-- waypoints: array of { x, y, z } world positions.
function bridge.start_navigate_local(waypoints, on_done)
    bridge.stop()
    if waypoints == nil or #waypoints == 0 then
        if on_done then on_done('empty_path') end
        return
    end
    s.mode         = MODE_NAV_LOCAL
    s.local_path   = waypoints
    s.local_idx    = 1
    s.waypoint_skips = 0
    s.on_done      = on_done
    s.stuck_frames = 0
    s.nav_check_x  = 0.0
    s.nav_check_y  = 0.0
end

-- start_x/y are horizontal start coordinates (LocalPosition.X and .Y)
function bridge.start_explore(heading, graph_ref, on_node, on_done, on_debug, start_x, start_y)
    bridge.stop()
    s.mode      = MODE_EXPLORE
    s.heading   = normalize_heading(heading or 0.0)
    s.probe_count = 0
    s.rotations = 0
    s.graph_ref = graph_ref
    s.on_node   = on_node
    s.on_done   = on_done
    s.on_debug  = on_debug
    s.frontier  = {}
    if graph_ref then
        s.frontier = graph_ref.get_frontier(MIN_FRONTIER_NB)
    end
    if start_x ~= nil and start_y ~= nil then
        commit_probe(start_x, start_y)
    end
end

-- -------------------------------------------------------------------------
-- Per-frame tick
-- px, py = LocalPosition.X (east/west), LocalPosition.Y (north/south)
-- pz     = LocalPosition.Z (elevation, only used for node recording)
-- -------------------------------------------------------------------------
function bridge.tick(px, py, pz, frame_counter)
    if s.mode == MODE_IDLE then return end

    -- ---- NAVIGATE mode ----
    if s.mode == MODE_NAVIGATE then
        local g = s.graph_ref
        if g == nil or s.path == nil then
            bridge.stop()
            if s.on_done then s.on_done('error') end
            return
        end

        -- Extra-target phase: all graph waypoints visited; drive to raw target.
        if s.waypoint_idx > #s.path then
            if s.extra_x == nil then
                bridge.stop()
                if s.on_done then s.on_done('reached') end
                return
            end
            -- Arrival check: stop when within waypoint radius of the raw target.
            if dist2d(px, py, s.extra_x, s.extra_y) <= WAYPOINT_RADIUS then
                bridge.stop()
                if s.on_done then s.on_done('reached') end
                return
            end
            if frame_counter % STUCK_CHECK_INTERVAL == 0 then
                local dist_now  = dist2d(px, py, s.extra_x, s.extra_y)
                local dist_prev = dist2d(s.nav_check_x, s.nav_check_y, s.extra_x, s.extra_y)
                local progress  = dist_prev - dist_now
                s.nav_check_x = px
                s.nav_check_y = py
                if progress >= MIN_POS_PROGRESS then
                    s.stuck_frames = 0
                else
                    s.stuck_frames = s.stuck_frames + STUCK_CHECK_INTERVAL
                end
            end
            if s.stuck_frames >= STUCK_FRAMES_NAV then
                bridge.stop()
                if s.on_done then s.on_done('stuck') end
                return
            end
            steer_toward(px, py, pz, s.extra_x, s.extra_y, false)
            return
        end

        -- Normal graph-following phase.
        local target = g.nodes[s.path[s.waypoint_idx]]
        if target == nil then
            bridge.stop()
            if s.on_done then s.on_done('reached') end
            return
        end

        if frame_counter % STUCK_CHECK_INTERVAL == 0 then
            local progress
            if s.steering then
                -- When steering around an obstacle, measure raw movement
                -- instead of progress-toward-waypoint (sideways movement is
                -- fine as long as we're actually moving).
                progress = dist2d(px, py, s.nav_check_x, s.nav_check_y)
            else
                local dist_now  = dist2d(px, py, target.x, target.y)
                local dist_prev = dist2d(s.nav_check_x, s.nav_check_y, target.x, target.y)
                progress = dist_prev - dist_now
            end
            s.nav_check_x = px
            s.nav_check_y = py
            if progress >= MIN_POS_PROGRESS then
                s.stuck_frames = 0
            else
                s.stuck_frames = s.stuck_frames + STUCK_CHECK_INTERVAL
            end
        end

        if s.stuck_frames >= STUCK_FRAMES_NAV then
            -- Penalize the edge we got stuck on.
            if s.waypoint_idx >= 1 and s.waypoint_idx <= #s.path then
                local prev_idx = math.max(1, s.waypoint_idx - 1)
                local cur_idx  = s.waypoint_idx
                if s.path[prev_idx] and s.path[cur_idx] then
                    g.record_failure(s.path[prev_idx], s.path[cur_idx])
                end
            end
            s.waypoint_skips = s.waypoint_skips + 1
            if s.waypoint_skips > MAX_WAYPOINT_SKIPS then
                bridge.stop()
                if s.on_done then s.on_done('stuck') end
                return
            end
            s.waypoint_idx = s.waypoint_idx + 1
            s.stuck_frames = 0
            if s.waypoint_idx > #s.path then
                if s.extra_x ~= nil then
                    steer_toward(px, py, pz, s.extra_x, s.extra_y, false)
                else
                    bridge.stop()
                    if s.on_done then s.on_done('stuck') end
                end
                return
            end
            target = g.nodes[s.path[s.waypoint_idx]]
        end

        local d = dist2d(px, py, target.x, target.y)
        if d <= WAYPOINT_RADIUS then
            s.waypoint_idx   = s.waypoint_idx + 1
            s.stuck_frames   = 0
            s.waypoint_skips = 0
            s.nav_check_x    = px
            s.nav_check_y    = py
            if s.waypoint_idx > #s.path then
                if s.extra_x ~= nil then
                    steer_toward(px, py, pz, s.extra_x, s.extra_y, false)
                else
                    bridge.stop()
                    if s.on_done then s.on_done('reached') end
                end
                return
            else
                target = g.nodes[s.path[s.waypoint_idx]]
            end
        end

        local d = dist2d(px, py, target.x, target.y)
        if d <= GRID_STEP then
            s.steering = false
            drive_toward(px, py, target.x, target.y)
        else
            steer_toward(px, py, pz, target.x, target.y, false, target.z)
        end
        return
    end

    -- ---- NAVIGATE_LOCAL mode (coordinate-array paths from hires mesh) ----
    if s.mode == MODE_NAV_LOCAL then
        if s.local_path == nil or s.local_idx > #s.local_path then
            bridge.stop()
            if s.on_done then s.on_done('reached') end
            return
        end

        local target = s.local_path[s.local_idx]

        -- Stuck detection
        if frame_counter % STUCK_CHECK_INTERVAL == 0 then
            local progress
            if s.steering then
                progress = dist2d(px, py, s.nav_check_x, s.nav_check_y)
            else
                local dist_now  = dist2d(px, py, target.x, target.y)
                local dist_prev = dist2d(s.nav_check_x, s.nav_check_y, target.x, target.y)
                progress = dist_prev - dist_now
            end
            s.nav_check_x = px
            s.nav_check_y = py
            if progress >= MIN_POS_PROGRESS then
                s.stuck_frames = 0
            else
                s.stuck_frames = s.stuck_frames + STUCK_CHECK_INTERVAL
            end
        end

        if s.stuck_frames >= STUCK_FRAMES_NAV then
            s.waypoint_skips = s.waypoint_skips + 1
            if s.waypoint_skips > MAX_WAYPOINT_SKIPS then
                bridge.stop()
                if s.on_done then s.on_done('stuck') end
                return
            end
            s.local_idx = s.local_idx + 1
            s.stuck_frames = 0
            if s.local_idx > #s.local_path then
                bridge.stop()
                if s.on_done then s.on_done('stuck') end
                return
            end
            target = s.local_path[s.local_idx]
        end

        -- Arrival at waypoint
        local d = dist2d(px, py, target.x, target.y)
        if d <= WAYPOINT_RADIUS then
            s.local_idx    = s.local_idx + 1
            s.stuck_frames = 0
            s.waypoint_skips = 0
            s.nav_check_x  = px
            s.nav_check_y  = py
            if s.local_idx > #s.local_path then
                bridge.stop()
                if s.on_done then s.on_done('reached') end
                return
            end
            target = s.local_path[s.local_idx]
            d = dist2d(px, py, target.x, target.y)
        end

        -- Movement: walls-only steering since hires path is pre-validated
        if d <= GRID_STEP then
            s.steering = false
            drive_toward(px, py, target.x, target.y)
        else
            steer_toward(px, py, pz, target.x, target.y, true, target.z)
        end
        return
    end

    -- ---- EXPLORE mode ----
    if s.mode == MODE_EXPLORE then

        if s.probe_x == nil then
            commit_probe(px, py)
        end

        -- Record node candidate every 10 frames
        if frame_counter % 10 == 0 and s.on_node then
            s.on_node(px, py, pz)
        end

        local dist_to_probe = dist2d(px, py, s.probe_x, s.probe_y)

        if s.stuck_cooldown > 0 then
            s.stuck_cooldown = s.stuck_cooldown - 1
        end

        -- ---- Probe reached ----
        if dist_to_probe <= WAYPOINT_RADIUS then
            s.stuck_frames   = 0
            s.rotations      = 0
            s.rotation_queue = {}
            s.probe_count    = s.probe_count + 1
            dbg(string.format('Probe reached (#%d) at (%.1f, %.1f)',
                s.probe_count, px, py))

            if s.graph_ref then
                s.frontier = s.graph_ref.get_frontier(MIN_FRONTIER_NB)
            end

            local fwd_x   = px + PROBE_DIST * math.sin(s.heading)
            local fwd_y   = py + PROBE_DIST * math.cos(s.heading)
            local density = count_density(fwd_x, fwd_y, s.graph_ref)
            if density >= DENSITY_REDIRECT then
                local queue = sorted_headings(px, py, s.graph_ref, nil)
                if queue[1] then
                    s.heading = queue[1]
                    dbg(string.format('Redirect (fwd density %d) -> hdg=%.2f', density, s.heading))
                end
            end

            commit_probe(px, py)
        end

        -- ---- Stuck detection (progress toward probe) ----
        if s.stuck_cooldown == 0 and frame_counter % STUCK_CHECK_INTERVAL == 0 then
            local dist_now  = dist2d(px, py, s.probe_x, s.probe_y)
            local dist_prev = dist2d(s.check_x, s.check_y, s.probe_x, s.probe_y)
            local progress  = dist_prev - dist_now
            s.check_x = px
            s.check_y = py
            if progress >= MIN_POS_PROGRESS then
                s.stuck_frames = 0
            else
                s.stuck_frames = s.stuck_frames + STUCK_CHECK_INTERVAL
            end
        end

        -- ---- Stuck: rotate toward next best unexplored direction ----
        if s.stuck_frames >= STUCK_FRAMES_EXP then

            if #s.rotation_queue == 0 and s.rotations == 0 then
                s.rotation_queue = sorted_headings(px, py, s.graph_ref, s.heading)
                dbg(string.format('Stuck at (%.1f, %.1f): built queue of %d directions',
                    px, py, #s.rotation_queue))
            end

            if #s.rotation_queue > 0 then
                s.heading = table.remove(s.rotation_queue, 1)
                s.rotations = s.rotations + 1
                commit_probe(px, py)
                dbg(string.format('Rotate -> hdg=%.2f (attempt %d, %d left)',
                    s.heading, s.rotations, #s.rotation_queue))
            else
                -- All directions exhausted: A* to best frontier node.
                s.rotations      = 0
                s.probe_count    = 0
                s.rotation_queue = {}

                if s.graph_ref then
                    s.frontier = s.graph_ref.get_frontier(MIN_FRONTIER_NB)
                end

                if #s.frontier == 0 then
                    dbg('No frontier nodes remaining. Exploration complete.')
                    bridge.stop()
                    if s.on_done then s.on_done('complete') end
                    return
                end

                local g = s.graph_ref
                local best_id, best_score = nil, -math.huge
                for _, nid in ipairs(s.frontier) do
                    local n = g.nodes[nid]
                    if n then
                        local d      = dist2d(px, py, n.x, n.y)
                        local nb_cnt = #(g.adjacency[nid] or {})
                        local score  = -nb_cnt * 100 + math.min(d, 80)
                        if score > best_score then
                            best_score = score
                            best_id    = nid
                        end
                    end
                end

                if best_id then
                    local fn = g.nodes[best_id]
                    dbg(string.format('Backtracking to frontier node %d (%.1f yalms, %d nb)',
                        best_id, dist2d(px, py, fn.x, fn.y),
                        #(g.adjacency[best_id] or {})))
                    local start_id = g.nearest_node(px, py)
                    local path     = g.astar(start_id, best_id)
                    if path and #path > 0 then
                        local saved_on_node  = s.on_node
                        local saved_on_done  = s.on_done
                        local saved_on_debug = s.on_debug
                        bridge.start_navigate(path, g, function(reason)
                            if reason == 'reached' and fn then
                                dbg('At frontier node; resuming exploration.')
                                local hdg_queue = sorted_headings(fn.x, fn.y, g, nil)
                                local hdg = hdg_queue[1] or 0.0
                                bridge.start_explore(
                                    hdg, g,
                                    saved_on_node, saved_on_done, saved_on_debug,
                                    fn.x, fn.y)
                            else
                                dbg('Backtrack ended: ' .. tostring(reason))
                                if saved_on_done then
                                    saved_on_done('backtrack_' .. tostring(reason))
                                end
                            end
                        end)
                        return
                    end
                end

                dbg('No reachable frontier. Exploration complete.')
                bridge.stop()
                if s.on_done then s.on_done('complete') end
                return
            end
        end

        -- Always refresh IAutoFollow — skipping even one frame stops the character.
        steer_toward(px, py, pz, s.probe_x, s.probe_y)
    end
end

return bridge
