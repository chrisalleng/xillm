--[[
* mapper - navigator.lua
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
--]]

local navigator = {}

-- -------------------------------------------------------------------------
-- Constants (60 fps)
-- -------------------------------------------------------------------------
local WAYPOINT_RADIUS      = 1.5   -- yalms
local PROBE_DIST           = 10.0  -- yalms - distance of each probe target
local PROBE_COOLDOWN       = 90    -- frames of no-stuck-check after committing probe (~1.5s)
local STUCK_FRAMES_NAV     = 120   -- frames before skipping a blocked waypoint (~2s)
local MAX_WAYPOINT_SKIPS   = 5     -- consecutive skips before aborting
local STUCK_FRAMES_EXP     = 180   -- frames before trying next rotation (~3s)
local STUCK_CHECK_INTERVAL = 30    -- frames between position-progress snapshots
local MIN_POS_PROGRESS     = 0.4   -- yalms moved in STUCK_CHECK_INTERVAL frames to not be stuck
local HEADING_CANDIDATES   = 6     -- evenly-spaced directions to evaluate
local DENSITY_REDIRECT     = 4     -- node count near next probe above which we redirect on success
local MIN_FRONTIER_NB      = 4     -- node neighbor count below which it is a frontier node

-- -------------------------------------------------------------------------
-- State
-- -------------------------------------------------------------------------
local MODE_IDLE     = 'idle'
local MODE_NAVIGATE = 'navigate'
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
-- This is the priority queue used both for stuck-rotation and for redirect
-- decisions when a probe is reached.
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
-- Resets stuck detection and starts cooldown so the game can begin
-- accelerating the character before we measure progress.
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

function navigator.is_active()
    return s.mode ~= MODE_IDLE
end

function navigator.mode()
    return s.mode
end

function navigator.get_probe()
    return s.probe_x, s.probe_y
end

function navigator.get_heading()
    return s.heading
end

function navigator.get_probe_count()
    return s.probe_count
end

function navigator.get_stuck_frames()
    return s.stuck_frames
end

function navigator.stop()
    s.mode           = MODE_IDLE
    s.path           = nil
    s.waypoint_idx   = 1
    s.extra_x        = nil
    s.extra_y        = nil
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
-- waypoint (e.g. a zone line trigger position). If set, on_done fires only when
-- within WAYPOINT_RADIUS of the extra target, not at the last graph node.
function navigator.start_navigate(path, graph_ref, on_done, extra_x, extra_y)
    navigator.stop()
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

-- start_x/y are horizontal start coordinates (LocalPosition.X and .Y)
function navigator.start_explore(heading, graph_ref, on_node, on_done, on_debug, start_x, start_y)
    navigator.stop()
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
function navigator.tick(px, py, pz, frame_counter)
    if s.mode == MODE_IDLE then return end

    -- ---- NAVIGATE mode ----
    if s.mode == MODE_NAVIGATE then
        local g = s.graph_ref
        if g == nil or s.path == nil then
            navigator.stop()
            if s.on_done then s.on_done('error') end
            return
        end

        -- Extra-target phase: all graph waypoints have been visited; drive
        -- directly to the raw target (e.g. the zone line trigger position).
        -- This check MUST come before the g.nodes lookup below, because
        -- waypoint_idx > #path means path[waypoint_idx] is nil, which would
        -- cause the nil-node check to fire 'reached' prematurely every frame.
        if s.waypoint_idx > #s.path then
            if s.extra_x == nil then
                navigator.stop()
                if s.on_done then s.on_done('reached') end
                return
            end
            -- Stuck detection still applies so we abort if the trigger is
            -- genuinely unreachable (bad coordinates, terrain block, etc.).
            if frame_counter % STUCK_CHECK_INTERVAL == 0 then
                local moved = dist2d(px, py, s.nav_check_x, s.nav_check_y)
                s.nav_check_x = px
                s.nav_check_y = py
                if moved >= MIN_POS_PROGRESS then
                    s.stuck_frames = 0
                else
                    s.stuck_frames = s.stuck_frames + STUCK_CHECK_INTERVAL
                end
            end
            if s.stuck_frames >= STUCK_FRAMES_NAV then
                navigator.stop()
                if s.on_done then s.on_done('stuck') end
                return
            end
            drive_toward(px, py, s.extra_x, s.extra_y)
            return
        end

        -- Normal graph-following phase.
        local target = g.nodes[s.path[s.waypoint_idx]]
        if target == nil then
            navigator.stop()
            if s.on_done then s.on_done('reached') end
            return
        end

        -- Position-snapshot stuck detection: every STUCK_CHECK_INTERVAL frames
        -- measure total progress. Sliding against a lamp post still moves slightly
        -- per-frame but won't cover MIN_POS_PROGRESS over the snapshot window.
        if frame_counter % STUCK_CHECK_INTERVAL == 0 then
            local moved = dist2d(px, py, s.nav_check_x, s.nav_check_y)
            s.nav_check_x = px
            s.nav_check_y = py
            if moved >= MIN_POS_PROGRESS then
                s.stuck_frames = 0
            else
                s.stuck_frames = s.stuck_frames + STUCK_CHECK_INTERVAL
            end
        end

        if s.stuck_frames >= STUCK_FRAMES_NAV then
            s.waypoint_skips = s.waypoint_skips + 1
            if s.waypoint_skips > MAX_WAYPOINT_SKIPS then
                navigator.stop()
                if s.on_done then s.on_done('stuck') end
                return
            end
            -- Skip the blocked waypoint and aim for the next one.
            s.waypoint_idx = s.waypoint_idx + 1
            s.stuck_frames = 0
            if s.waypoint_idx > #s.path then
                if s.extra_x ~= nil then
                    -- Transition to extra-target phase this frame.
                    drive_toward(px, py, s.extra_x, s.extra_y)
                else
                    navigator.stop()
                    if s.on_done then s.on_done('reached') end
                end
                return
            end
            target = g.nodes[s.path[s.waypoint_idx]]
        end

        local d = dist2d(px, py, target.x, target.y)
        if d <= WAYPOINT_RADIUS then
            s.waypoint_idx   = s.waypoint_idx + 1
            s.stuck_frames   = 0
            s.waypoint_skips = 0   -- clean progress resets the skip counter
            s.nav_check_x    = px
            s.nav_check_y    = py
            if s.waypoint_idx > #s.path then
                if s.extra_x ~= nil then
                    -- Transition to extra-target phase: drive toward trigger this frame.
                    drive_toward(px, py, s.extra_x, s.extra_y)
                else
                    navigator.stop()
                    if s.on_done then s.on_done('reached') end
                end
                return
            else
                target = g.nodes[s.path[s.waypoint_idx]]
            end
        end

        drive_toward(px, py, target.x, target.y)
        return
    end

    -- ---- EXPLORE mode ----
    if s.mode == MODE_EXPLORE then

        -- First tick: initialise probe
        if s.probe_x == nil then
            commit_probe(px, py)
        end

        -- Record node candidate every 10 frames
        if frame_counter % 10 == 0 and s.on_node then
            s.on_node(px, py, pz)
        end

        local dist_to_probe = dist2d(px, py, s.probe_x, s.probe_y)

        -- Tick down post-commit cooldown
        if s.stuck_cooldown > 0 then
            s.stuck_cooldown = s.stuck_cooldown - 1
        end

        -- ---- Probe reached ----
        if dist_to_probe <= WAYPOINT_RADIUS then
            s.stuck_frames   = 0
            s.rotations      = 0
            s.rotation_queue = {}   -- made real progress; reset stuck-rotation state
            s.probe_count    = s.probe_count + 1
            dbg(string.format('Probe reached (#%d) at (%.1f, %.1f)',
                s.probe_count, px, py))

            if s.graph_ref then
                s.frontier = s.graph_ref.get_frontier(MIN_FRONTIER_NB)
            end

            -- Check whether the path ahead is already explored.
            -- If density is high, pivot toward the least-explored heading so
            -- we expand the frontier rather than retrace covered ground.
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

            -- Fall through to drive_toward — do NOT return early.
            -- IAutoFollow must receive a fresh delta every frame or the
            -- game stops the character, causing jitter.
        end

        -- ---- Stuck detection (position-snapshot, after cooldown expires) ----
        -- Every STUCK_CHECK_INTERVAL frames, measure how far the character moved
        -- from the last snapshot. Tiny wall-sliding doesn't reset the counter.
        if s.stuck_cooldown == 0 and frame_counter % STUCK_CHECK_INTERVAL == 0 then
            local moved = dist2d(px, py, s.check_x, s.check_y)
            s.check_x = px
            s.check_y = py
            if moved >= MIN_POS_PROGRESS then
                s.stuck_frames = 0
            else
                s.stuck_frames = s.stuck_frames + STUCK_CHECK_INTERVAL
            end
        end

        -- ---- Stuck: rotate toward next best unexplored direction ----
        if s.stuck_frames >= STUCK_FRAMES_EXP then

            -- Build the rotation queue once per stuck episode.
            -- Excludes the current heading (just failed) and sorts remaining
            -- directions by how few nodes are near each probe landing spot.
            if #s.rotation_queue == 0 and s.rotations == 0 then
                s.rotation_queue = sorted_headings(px, py, s.graph_ref, s.heading)
                dbg(string.format('Stuck at (%.1f, %.1f): built queue of %d directions',
                    px, py, #s.rotation_queue))
            end

            if #s.rotation_queue > 0 then
                -- Try the next best untried direction
                s.heading = table.remove(s.rotation_queue, 1)
                s.rotations = s.rotations + 1
                commit_probe(px, py)
                dbg(string.format('Rotate -> hdg=%.2f (attempt %d, %d left)',
                    s.heading, s.rotations, #s.rotation_queue))
            else
                -- All HEADING_CANDIDATES directions exhausted from this position.
                -- Backtrack via A* to the most promising frontier node.
                s.rotations      = 0
                s.probe_count    = 0
                s.rotation_queue = {}

                if s.graph_ref then
                    s.frontier = s.graph_ref.get_frontier(MIN_FRONTIER_NB)
                end

                if #s.frontier == 0 then
                    dbg('No frontier nodes remaining. Exploration complete.')
                    navigator.stop()
                    if s.on_done then s.on_done('complete') end
                    return
                end

                local g = s.graph_ref
                -- Score each frontier node: prefer fewest neighbors (most
                -- unexplored), break ties by preferring farther nodes so the
                -- character pushes the boundary outward.
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
                        navigator.start_navigate(path, g, function(reason)
                            if reason == 'reached' and fn then
                                dbg('At frontier node; resuming exploration.')
                                -- Pick the best unexplored heading from this
                                -- new position rather than reusing the old one.
                                local hdg_queue = sorted_headings(fn.x, fn.y, g, nil)
                                local hdg = hdg_queue[1] or 0.0
                                navigator.start_explore(
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
                navigator.stop()
                if s.on_done then s.on_done('complete') end
                return
            end
        end

        -- Always refresh IAutoFollow — skipping even one frame stops the character.
        drive_toward(px, py, s.probe_x, s.probe_y)
    end
end

return navigator
