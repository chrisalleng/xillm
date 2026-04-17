--[[
* mapper - mapper.lua
*
* Ashita v4 addon that autonomously explores a zone, builds a navigable node
* graph, records NPCs and interactive objects, and can navigate the player to
* arbitrary world coordinates using A* over the recorded graph.
*
* Commands:
*   /mapper explore                   -- Start autonomous exploration of current zone
*   /mapper stop                      -- Stop exploration or navigation
*   /mapper goto <x> <z>             -- Navigate to world coordinates in current zone
*   /mapper goto |Zone Name| [x z]   -- Navigate to another zone (cross-zone A*)
*   /mapper save                      -- Manually save zone data to disk
*   /mapper status                    -- Print current stats
*   /mapper pos                       -- Print current position coordinates
*
* Data is persisted per zone in:
*   <AshitaPath>/config/addons/mapper/zone_<id>.json
--]]

addon.name    = 'mapper'
addon.author  = 'xillm'
addon.version = '0.38'
addon.desc    = 'Autonomous zone mapper and navigator for FFXI'
addon.link    = ''

require('common')
local json             = require('json')
local graph            = require('graph')
local entities         = require('entities')
local navigator        = require('navigator')
local zone_transitions = require('zone_transitions')

-------------------------------------------------------------------------------
-- Module state
-------------------------------------------------------------------------------
local state = {
    zone_id   = nil,
    data_path = nil,
    frame     = 0,

    -- Cross-zone navigation state (/mapper goto |Zone Name| x z)
    cross_zone = {
        active     = false,
        route      = nil,   -- array of {from_zone,fx,fy,to_zone,tx,ty} from zone_astar
        step       = 1,     -- index into route (1 = walking to route[1]'s zone line)
        final_zone = nil,   -- destination zone ID
        final_x    = nil,   -- destination coords in final zone (may be nil)
        final_y    = nil,
    },
}

-------------------------------------------------------------------------------
-- Helpers
-------------------------------------------------------------------------------

local function print_msg(msg)
    AshitaCore:GetChatManager():QueueCommand(1, '/echo [mapper] ' .. tostring(msg))
end

-------------------------------------------------------------------------------
-- Zone navmesh cache (for cross-zone route planning)
-- Stores loaded navmesh data for zones other than the current one so zone_astar
-- can use real path costs instead of Euclidean distances.
-- Values: {nodes={[id]={x,y}}, adj={[id]={{to,cost},...}}} or false (no data).
-------------------------------------------------------------------------------
local _zone_navmesh_cache = {}
-- Coroutine for async cross-zone route planning (nil when idle).
local _planning_coroutine = nil
-- Zone ID captured when planning started; used to detect stale results.
local _planning_zone_id   = nil

local function _load_navmesh(zone_id)
    if _zone_navmesh_cache[zone_id] ~= nil then
        return _zone_navmesh_cache[zone_id]
    end
    if state.data_path == nil or state.data_path == '' then
        _zone_navmesh_cache[zone_id] = false
        return false
    end
    local path = string.format('%sconfig/addons/mapper/zone_%d.json', state.data_path, zone_id)
    local f = io.open(path, 'r')
    if not f then _zone_navmesh_cache[zone_id] = false; return false end
    local content = f:read('*all'); f:close()
    local ok, data = pcall(json.decode, content)
    if not ok or not data or not data.nodes then
        _zone_navmesh_cache[zone_id] = false; return false
    end
    local nodes = {}
    for _, n in ipairs(data.nodes) do nodes[n.id] = { x = n.x, y = n.y } end
    local adj = {}
    for _, n in ipairs(data.nodes) do adj[n.id] = {} end
    if data.edges then
        for _, e in ipairs(data.edges) do
            local na, nb = nodes[e.a], nodes[e.b]
            if na and nb then
                local dx, dy = na.x - nb.x, na.y - nb.y
                local c = math.sqrt(dx*dx + dy*dy)
                table.insert(adj[e.a], { to = e.b, cost = c })
                table.insert(adj[e.b], { to = e.a, cost = c })
            end
        end
    end
    local nm = { nodes = nodes, adj = adj }
    _zone_navmesh_cache[zone_id] = nm
    -- Yield after the expensive file-load so the renderer stays responsive.
    if coroutine.running() then
        print_msg(string.format('  navmesh zone %d (%d nodes)', zone_id, #data.nodes))
        coroutine.yield()
    end
    return nm
end

-- Dijkstra on a loaded navmesh; returns path cost or nil (unreachable).
-- Runs synchronously — the coroutine only yields between file loads, not
-- during individual Dijkstra iterations.
local function _navmesh_path_cost(nm, from_x, from_y, to_x, to_y)
    local sid, gid, bsd, bgd = nil, nil, math.huge, math.huge
    for id, n in pairs(nm.nodes) do
        local ds = (n.x-from_x)^2 + (n.y-from_y)^2
        local dg = (n.x-to_x)^2   + (n.y-to_y)^2
        if ds < bsd then bsd = ds; sid = id end
        if dg < bgd then bgd = dg; gid = id end
    end
    if sid == nil or gid == nil then return nil end
    if sid == gid then return 0 end
    local dist, vis, open = {[sid]=0}, {}, {{id=sid, cost=0}}
    while #open > 0 do
        table.sort(open, function(a,b) return a.cost < b.cost end)
        local cur = table.remove(open, 1)
        if vis[cur.id] then goto nm_cont end
        vis[cur.id] = true
        if cur.id == gid then return cur.cost end
        for _, e in ipairs(nm.adj[cur.id] or {}) do
            local nc = cur.cost + e.cost
            if not vis[e.to] and (dist[e.to] == nil or nc < dist[e.to]) then
                dist[e.to] = nc
                local found = false
                for _, oe in ipairs(open) do
                    if oe.id == e.to then oe.cost = nc; found = true; break end
                end
                if not found then table.insert(open, {id=e.to, cost=nc}) end
            end
        end
        ::nm_cont::
    end
    return nil
end

-- Returns a step_cost_fn for zone_transitions.zone_astar.
-- For the current in-memory zone: uses graph.astar (most accurate).
-- For other zones: loads saved navmesh from disk.
-- Returns nil for a step when the navmesh proves it is unreachable.
-- Falls back to Euclidean when no navmesh data is available.
local function make_step_cost_fn()
    return function(zone_id, from_x, from_y, exit)
        if zone_id == state.zone_id and graph.node_count() > 0 then
            local s = graph.nearest_node(from_x, from_y)
            local g = graph.nearest_node(exit.fx, exit.fy)
            if s ~= nil and g ~= nil then
                local path = graph.astar(s, g)
                if path == nil then return nil end
                local cost = 0
                for i = 2, #path do
                    local na, nb = graph.nodes[path[i-1]], graph.nodes[path[i]]
                    local dx, dy = na.x - nb.x, na.y - nb.y
                    cost = cost + math.sqrt(dx*dx + dy*dy)
                end
                return cost
            end
        else
            local nm = _load_navmesh(zone_id)
            if nm then return _navmesh_path_cost(nm, from_x, from_y, exit.fx, exit.fy) end
        end
        -- No navmesh available: Euclidean fallback (cannot detect walls)
        local dx, dy = exit.fx - from_x, exit.fy - from_y
        return math.sqrt(dx*dx + dy*dy)
    end
end

-------------------------------------------------------------------------------
-- Helpers (continued)
-------------------------------------------------------------------------------

local function get_install_path()
    -- Try the documented v4 method first; fall back if needed.
    local ok, path = pcall(function()
        return AshitaCore:GetInstallPath()
    end)
    if ok and path then return path end
    -- Fallback: ask Ashita for the addon path and strip two levels
    return ''
end

local function zone_file_path(zone_id)
    return string.format('%sconfig/addons/mapper/zone_%d.json', state.data_path, zone_id)
end

local function ensure_data_dir()
    local dir = string.format('%sconfig/addons/mapper/', state.data_path)
    -- Ashita v4 provides ashita.fs; use it if available, otherwise rely on
    -- io.open creating the file (parent directory must exist beforehand).
    local ok = pcall(function()
        if ashita and ashita.fs and ashita.fs.create_directory then
            ashita.fs.create_directory(dir)
        end
    end)
end

-------------------------------------------------------------------------------
-- Persistence
-------------------------------------------------------------------------------

local function save_zone(zone_id, force)
    if zone_id == nil or zone_id == 0 then return end
    -- Skip automatic saves when the graph is unchanged (e.g. pure navmesh navigation
    -- with no exploration).  Explicit /mapper save passes force=true.
    if not force and not graph.dirty then return end
    -- Invalidate planning cache so the next route query uses fresh data.
    _zone_navmesh_cache[zone_id] = nil
    ensure_data_dir()
    local data = {
        zone_id  = zone_id,
        nodes    = graph.serialize_nodes(),
        edges    = graph.serialize_edges(),
        entities = entities.serialize(zone_id),
    }
    local path = zone_file_path(zone_id)
    local f = io.open(path, 'w')
    if f then
        f:write(json.encode(data))
        f:close()
    else
        print_msg('Warning: could not write ' .. path)
    end
end

local function load_zone(zone_id)
    if zone_id == nil or zone_id == 0 then return end
    local path = zone_file_path(zone_id)
    local f = io.open(path, 'r')
    if f then
        local content = f:read('*all')
        f:close()
        local ok, data = pcall(json.decode, content)
        if ok and data then
            graph.load(data)
            entities.load(data, zone_id)
            print_msg(string.format('Loaded zone %d: %d nodes, %d entities',
                zone_id, graph.node_count(), entities.count(zone_id)))
        else
            print_msg('Warning: could not parse zone file for zone ' .. zone_id)
        end
    end
end

-------------------------------------------------------------------------------
-- Zone lifecycle
-------------------------------------------------------------------------------

local function zone_init(zone_id)
    if zone_id == nil or zone_id == 0 then return end
    state.zone_id = zone_id
    graph.reset()
    entities.reset(zone_id)
    load_zone(zone_id)
end

-------------------------------------------------------------------------------
-- Cross-zone navigation helpers
-------------------------------------------------------------------------------

-- When the planned zone exit is unreachable from the player's current navmesh
-- position, scan all exits from the current zone for one that IS reachable and
-- still connects to the final destination.  Rebuilds cross_zone route and
-- starts navigation if a viable exit is found.  Returns true on success.
local function try_reroute(start_id, px, py)
    local czs       = state.cross_zone
    local final     = czs.final_zone
    local exits     = zone_transitions.get_exits(state.zone_id)
    local best_path = nil
    local best_exit = nil
    local best_route= nil
    local best_hops = math.huge
    local best_nav  = math.huge

    for _, exit in ipairs(exits) do
        local exit_node = graph.nearest_node(exit.fx, exit.fy)
        if exit_node ~= nil then
            local nav_path = graph.astar(start_id, exit_node)
            if nav_path ~= nil then
                -- Can reach this exit.  Plan rest of journey from its landing spot.
                local zone_route = zone_transitions.zone_astar(
                    exit.to_zone, final, exit.tx, exit.ty, make_step_cost_fn())
                if zone_route ~= nil then
                    local total_hops = 1 + #zone_route
                    if total_hops < best_hops or
                       (total_hops == best_hops and #nav_path < best_nav) then
                        best_hops = total_hops
                        best_nav  = #nav_path
                        best_path = nav_path
                        best_exit = exit
                        best_route = zone_route
                    end
                end
            end
        end
    end

    if best_path == nil then return false end

    -- Rebuild the route: the newly chosen first hop + rest from zone_astar.
    local new_route = { {
        from_zone = state.zone_id,
        fx = best_exit.fx, fy = best_exit.fy,
        to_zone = best_exit.to_zone,
        tx = best_exit.tx, ty = best_exit.ty,
    } }
    for _, t in ipairs(best_route) do
        table.insert(new_route, t)
    end
    czs.route = new_route
    czs.step  = 1

    local via_name  = zone_transitions.ZONE_NAMES[best_exit.to_zone] or tostring(best_exit.to_zone)
    local dest_name = zone_transitions.ZONE_NAMES[final] or tostring(final)
    print_msg(string.format('Rerouting via %s -> %s (%d zone%s, %d waypoints)',
        via_name, dest_name, best_hops, best_hops == 1 and '' or 's', #best_path))

    navigator.start_navigate(best_path, graph, function(reason)
        if reason == 'stuck' then
            print_msg('Cross-zone navigation stuck at zone line.')
            czs.active = false
        end
    end, best_exit.fx, best_exit.fy)
    return true
end

-- Navigate to a zone line exit within the current zone.
-- transition = { from_zone, fx, fy, to_zone, tx, ty }
local function navigate_to_zoneline(transition, step_label)
    if graph.node_count() == 0 then
        print_msg('No map data for ' .. (zone_transitions.ZONE_NAMES[state.zone_id] or tostring(state.zone_id)) ..
            '. Run /mapper explore first.')
        state.cross_zone.active = false
        return
    end
    local player = GetPlayerEntity()
    if player == nil or player.ActorPointer == 0 then
        print_msg('Player entity not available.')
        state.cross_zone.active = false
        return
    end
    local px = player.Movement.LocalPosition.X
    local py = player.Movement.LocalPosition.Y
    local start_id = graph.nearest_node(px, py)
    local line_id  = graph.nearest_node(transition.fx, transition.fy)
    local path = (start_id ~= nil and line_id ~= nil) and graph.astar(start_id, line_id) or nil
    if path == nil then
        -- Planned exit unreachable from the current position; try any other
        -- reachable exit and replan the remainder of the route from there.
        if start_id ~= nil and try_reroute(start_id, px, py) then return end
        -- Last resort: navmesh is sparse near the zone entry (common immediately
        -- after zoning in).  Drive directly toward the trigger with no graph
        -- guidance; stuck detection will abort if the path is truly blocked.
        local to_name = zone_transitions.ZONE_NAMES[transition.to_zone] or tostring(transition.to_zone)
        print_msg(string.format('%s: no graph path to zone line -> %s; approaching directly.',
            step_label, to_name))
        navigator.start_navigate({}, graph, function(reason)
            if reason == 'stuck' then
                print_msg('Cross-zone navigation stuck (direct zone line approach).')
                state.cross_zone.active = false
            end
        end, transition.fx, transition.fy)
        return
    end
    local to_name = zone_transitions.ZONE_NAMES[transition.to_zone] or tostring(transition.to_zone)
    print_msg(string.format('%s: navigating to zone line -> %s (%d waypoints)',
        step_label, to_name, #path))
    -- extra_x/y = exact zone line position, so we keep walking after the last
    -- graph node until the game zones us or we reach the trigger.
    navigator.start_navigate(path, graph, function(reason)
        if reason == 'stuck' then
            print_msg('Cross-zone navigation stuck at zone line.')
            state.cross_zone.active = false
        end
    end, transition.fx, transition.fy)
end

-- Called by the zone change handler after zone_init has loaded the new zone.
local function cross_zone_resume(new_zone_id)
    local czs = state.cross_zone
    if not czs.active then return end

    -- Find where we are in the route.  The player may have skipped a step (e.g.
    -- walked through a different zone line than the one we aimed at) so scan
    -- forward from the current step to find the first matching to_zone.
    local matched_step = nil
    for i = czs.step, #czs.route do
        if czs.route[i].to_zone == new_zone_id then
            matched_step = i
            break
        end
    end

    if matched_step == nil then
        -- Not on the route at all — abort cleanly.
        czs.active = false
        print_msg('Cross-zone navigation cancelled (unexpected zone change).')
        return
    end

    czs.step = matched_step + 1

    if czs.step > #czs.route then
        -- Arrived in the final zone.
        czs.active = false
        local zone_name = zone_transitions.ZONE_NAMES[new_zone_id] or tostring(new_zone_id)
        if czs.final_x ~= nil then
            -- Navigate to the requested coordinates within this zone.
            if graph.node_count() == 0 then
                print_msg('Arrived in ' .. zone_name .. ' (no map data for final navigation).')
                return
            end
            local player = GetPlayerEntity()
            if player == nil or player.ActorPointer == 0 then
                print_msg('Arrived in ' .. zone_name .. ' (player entity unavailable).')
                return
            end
            local px = player.Movement.LocalPosition.X
            local py = player.Movement.LocalPosition.Y
            local start_id = graph.nearest_node(px, py)
            local goal_id  = graph.nearest_node(czs.final_x, czs.final_y)
            if start_id == nil or goal_id == nil then
                print_msg('Arrived in ' .. zone_name .. ' (no graph nodes for final leg).')
                return
            end
            local path = graph.astar(start_id, goal_id)
            if path == nil then
                print_msg('Arrived in ' .. zone_name .. ' (no path to final coords).')
                return
            end
            print_msg(string.format('Arrived in %s, navigating to (%.1f, %.1f).',
                zone_name, czs.final_x, czs.final_y))
            navigator.start_navigate(path, graph, function(reason)
                if reason == 'reached' then
                    print_msg(string.format('Reached destination (%.1f, %.1f).', czs.final_x, czs.final_y))
                elseif reason == 'stuck' then
                    print_msg('Final leg stuck.')
                end
            end)
        else
            print_msg('Arrived in ' .. zone_name .. '.')
        end
        return
    end

    -- More legs to go: navigate to the next zone line.
    local transition = czs.route[czs.step]
    local step_label = string.format('Step %d/%d', czs.step, #czs.route)
    navigate_to_zoneline(transition, step_label)
end

-------------------------------------------------------------------------------
-- Explorer callbacks
-------------------------------------------------------------------------------

local function on_node(x, y, z)
    graph.try_add_node(x, y, z)
end

local function on_debug(msg)
    print_msg('[dbg] ' .. msg)
end

local function on_explore_done(reason)
    if reason == 'complete' then
        print_msg('Exploration complete.')
    elseif reason == 'stuck' then
        print_msg('Exploration stopped: stuck.')
    elseif reason == 'backtrack_failed' then
        print_msg('Exploration stopped: could not backtrack.')
    else
        print_msg('Exploration stopped: ' .. tostring(reason))
    end
    save_zone(state.zone_id)
end

-------------------------------------------------------------------------------
-- Events
-------------------------------------------------------------------------------

ashita.events.register('load', 'mapper_load', function()
    state.data_path = get_install_path()
    -- Zone ID may not be available immediately on load; zone_init will be
    -- triggered from d3d_present once a valid zone ID is observed.
    state.zone_id = nil
    print_msg('Loaded. Type /mapper for help.')
end)

ashita.events.register('unload', 'mapper_unload', function()
    navigator.stop()
    save_zone(state.zone_id)
end)

ashita.events.register('d3d_present', 'mapper_render', function()
    state.frame = state.frame + 1

    local player = GetPlayerEntity()
    if player == nil or player.ActorPointer == 0 then return end

    -- X = east/west, Y = north/south (horizontal), Z = elevation
    local px = player.Movement.LocalPosition.X
    local py = player.Movement.LocalPosition.Y
    local pz = player.Movement.LocalPosition.Z

    -- Zone change detection (check every 5 frames; frequent so autofollow is
    -- stopped before it carries the character far from the zone entry position)
    if state.frame % 5 == 0 then
        local zone_id = AshitaCore:GetMemoryManager():GetParty():GetMemberZone(0)
        if zone_id ~= nil and zone_id ~= 0 then
            if zone_id ~= state.zone_id then
                -- Zone changed: save old data, stop any active mode, load new zone
                if state.zone_id ~= nil then
                    save_zone(state.zone_id)
                end
                navigator.stop()
                local was_cross_zone = state.cross_zone.active
                zone_init(zone_id)
                if was_cross_zone then
                    cross_zone_resume(zone_id)
                end
            end
        end
    end

    if state.zone_id == nil or state.zone_id == 0 then return end

    -- Entity scanning (every 30 frames)
    if state.frame % 30 == 0 then
        entities.scan(state.zone_id, graph)
    end

    -- Periodic debug output while exploring (every 90 frames, ~1.5 sec)
    if navigator.mode() == 'explore' and state.frame % 90 == 0 then
        local probe_x, probe_y = navigator.get_probe()
        local heading = navigator.get_heading()
        local stuck   = navigator.get_stuck_frames()
        local probes  = navigator.get_probe_count()
        print_msg(string.format(
            'pos=(%.2f, %.2f) elev=%.2f probe=(%.1f, %.1f) hdg=%.2f stuck=%d/%d probes=%d nodes=%d',
            px, py, pz,
            probe_x or -1, probe_y or -1,
            heading, stuck, 180,
            probes, graph.node_count()))
    end

    -- Resume async route planning one step per frame.
    if _planning_coroutine then
        if state.zone_id ~= _planning_zone_id then
            -- Zone changed while planning was in progress; result is stale.
            _planning_coroutine = nil
            print_msg('Route planning cancelled (zone changed).')
        else
            local ok, err = coroutine.resume(_planning_coroutine)
            if not ok then
                print_msg('Route planning error: ' .. tostring(err))
                _planning_coroutine = nil
            elseif coroutine.status(_planning_coroutine) == 'dead' then
                _planning_coroutine = nil
            end
        end
    end

    -- Navigator/explorer tick (every frame while active)
    navigator.tick(px, py, pz, state.frame)
end)

ashita.events.register('command', 'mapper_cmd', function(e)
    local args = e.command:args()
    if #args == 0 or args[1]:lower() ~= '/mapper' then return end

    e.blocked = true

    local sub = args[2] and args[2]:lower() or ''

    -- /mapper explore
    if sub == 'explore' then
        if state.zone_id == nil or state.zone_id == 0 then
            print_msg('Not in a valid zone.')
            return
        end
        local player = GetPlayerEntity()
        if player == nil or player.ActorPointer == 0 then
            print_msg('Player entity not available.')
            return
        end
        -- Use the player's current facing as initial heading.
        -- X = east/west, Y = north/south (horizontal), Z = elevation.
        local heading = player.Movement.Rotation or 0.0
        local px = player.Movement.LocalPosition.X
        local py = player.Movement.LocalPosition.Y
        navigator.start_explore(heading, graph, on_node, on_explore_done, on_debug, px, py)
        print_msg('Exploration started.')

    -- /mapper stop
    elseif sub == 'stop' then
        if navigator.is_active() or state.cross_zone.active or _planning_coroutine then
            navigator.stop()
            state.cross_zone.active = false
            _planning_coroutine = nil
            print_msg('Stopped.')
        else
            print_msg('Nothing active to stop.')
        end

    -- /mapper goto <x> <z>              (same-zone coordinate navigation)
    -- /mapper goto |Zone Name| [x z]   (cross-zone navigation; coords optional)
    elseif sub == 'goto' then
        if state.zone_id == nil or state.zone_id == 0 then
            print_msg('Not in a valid zone.')
            return
        end
        -- Always stop whatever is currently running before starting a new goto.
        navigator.stop()
        state.cross_zone.active = false
        _planning_coroutine = nil

        -- Detect whether the argument is a zone name or bare coordinates.
        -- Three forms accepted:
        --   1. Raw autotranslate bytes: args[3] starts with \xFD
        --   2. Pipe-wrapped display text: /mapper goto |Zone Name| [x z]
        --   3. Plain text zone name (no spaces): /mapper goto PortBastok [x z]
        local raw_cmd = e.command
        local zone_arg, rest_args

        if args[3] ~= nil and args[3]:byte(1) == 0xFD then
            -- Raw autotranslate sequence from in-game autotranslate UI
            zone_arg  = args[3]
            rest_args = table.concat(args, ' ', 4)
        else
            -- Pipe-wrapped: |Zone Name| may contain spaces, so match raw cmd
            zone_arg, rest_args = raw_cmd:match('/mapper%s+goto%s+|([^|]+)|%s*(.*)')
            if zone_arg == nil then
                -- Plain single-word zone name (only if not a number)
                if args[3] ~= nil and tonumber(args[3]) == nil then
                    zone_arg  = args[3]
                    rest_args = table.concat(args, ' ', 4)
                end
            end
        end

        if zone_arg ~= nil then
            -- ---- Cross-zone goto ----
            local target_zone = zone_transitions.resolve_zone(zone_arg)
            if target_zone == nil then
                print_msg('Unknown zone: ' .. zone_arg)
                return
            end
            local tx, tz
            local coords = { rest_args:match('(%-?%d+%.?%d*)%s+(%-?%d+%.?%d*)') }
            if #coords == 2 then
                tx = tonumber(coords[1])
                tz = tonumber(coords[2])
            end

            if target_zone == state.zone_id then
                -- Same zone: normal A* navigation to coords (if provided).
                if tx == nil then
                    print_msg('Already in ' .. (zone_transitions.ZONE_NAMES[target_zone] or tostring(target_zone)) .. '.')
                    return
                end
                if graph.node_count() == 0 then
                    print_msg('No map data for this zone. Run /mapper explore first.')
                    return
                end
                local player = GetPlayerEntity()
                if player == nil or player.ActorPointer == 0 then
                    print_msg('Player entity not available.')
                    return
                end
                local px = player.Movement.LocalPosition.X
                local py = player.Movement.LocalPosition.Y
                local start_id = graph.nearest_node(px, py)
                local goal_id  = graph.nearest_node(tx, tz)
                if start_id == nil or goal_id == nil then
                    print_msg('No graph nodes available.')
                    return
                end
                local path = graph.astar(start_id, goal_id)
                if path == nil then
                    print_msg('No path found to that location.')
                    return
                end
                navigator.start_navigate(path, graph, function(reason)
                    if reason == 'reached' then
                        print_msg(string.format('Reached (%.1f, %.1f).', tx, tz))
                    elseif reason == 'stuck' then
                        print_msg('Navigation aborted: stuck.')
                    end
                end)
                print_msg(string.format('Navigating to (%.1f, %.1f) via %d waypoints.', tx, tz, #path))
                return
            end

            -- Different zone: plan route asynchronously so the client doesn't
            -- freeze while navmesh files are loaded and Dijkstra runs.
            local player = GetPlayerEntity()
            local px_start, py_start = 0, 0
            if player ~= nil and player.ActorPointer ~= 0 then
                px_start = player.Movement.LocalPosition.X
                py_start = player.Movement.LocalPosition.Y
            end

            local dest_name    = zone_transitions.ZONE_NAMES[target_zone] or tostring(target_zone)
            local start_zone   = state.zone_id
            local cost_fn      = make_step_cost_fn()
            -- Capture tx/tz for the coroutine closure (may be nil).
            local final_x, final_y = tx, tz

            print_msg('Planning route to ' .. dest_name .. '...')
            _planning_zone_id   = start_zone
            _planning_coroutine = coroutine.create(function()
                local route = zone_transitions.zone_astar(
                    start_zone, target_zone, px_start, py_start, cost_fn)

                -- Discard stale results if the player zoned during planning.
                if state.zone_id ~= start_zone then return end

                if route == nil or #route == 0 then
                    print_msg('No zone transition path found to ' .. dest_name .. '.')
                    return
                end

                state.cross_zone.active     = true
                state.cross_zone.route      = route
                state.cross_zone.step       = 1
                state.cross_zone.final_zone = target_zone
                state.cross_zone.final_x    = final_x
                state.cross_zone.final_y    = final_y

                print_msg(string.format('Cross-zone route to %s (id=%d): %d zone%s.',
                    dest_name, target_zone, #route, #route == 1 and '' or 's'))
                navigate_to_zoneline(route[1], string.format('Step 1/%d', #route))
            end)
            return
        end

        -- ---- Same-zone coordinate goto (original behavior) ----
        local tx = tonumber(args[3])
        local tz = tonumber(args[4])
        if tx == nil or tz == nil then
            print_msg('Usage: /mapper goto <x> <z>  OR  /mapper goto |Zone Name| [x z]')
            return
        end
        if graph.node_count() == 0 then
            print_msg('No map data for this zone. Run /mapper explore first.')
            return
        end
        local player = GetPlayerEntity()
        if player == nil or player.ActorPointer == 0 then
            print_msg('Player entity not available.')
            return
        end
        local px = player.Movement.LocalPosition.X
        local py = player.Movement.LocalPosition.Y
        local start_id = graph.nearest_node(px, py)
        local goal_id  = graph.nearest_node(tx, tz)
        if start_id == nil or goal_id == nil then
            print_msg('No graph nodes available.')
            return
        end
        local path = graph.astar(start_id, goal_id)
        if path == nil then
            print_msg('No path found to that location.')
            return
        end
        navigator.start_navigate(path, graph, function(reason)
            if reason == 'reached' then
                print_msg(string.format('Reached destination (%.1f, %.1f).', tx, tz))
            elseif reason == 'stuck' then
                print_msg('Navigation aborted: stuck.')
            else
                print_msg('Navigation ended: ' .. tostring(reason))
            end
        end)
        print_msg(string.format('Navigating to (%.1f, %.1f) via %d waypoints.',
            tx, tz, #path))

    -- /mapper save
    elseif sub == 'save' then
        if state.zone_id == nil or state.zone_id == 0 then
            print_msg('Not in a valid zone.')
            return
        end
        save_zone(state.zone_id, true)
        print_msg(string.format('Saved zone %d (%d nodes, %d entities).',
            state.zone_id, graph.node_count(), entities.count(state.zone_id)))

    -- /mapper status
    elseif sub == 'status' then
        local mode_str = navigator.mode()
        local nodes    = graph.node_count()
        local edges    = graph.edge_count()
        local ents     = entities.count(state.zone_id)
        local zone_name = zone_transitions.ZONE_NAMES[state.zone_id] or tostring(state.zone_id)
        print_msg(string.format(
            'Zone: %s (%s) | Mode: %s | Nodes: %d | Edges: %d | Entities: %d',
            zone_name, tostring(state.zone_id), mode_str, nodes, edges, ents))
        if state.cross_zone.active then
            local czs = state.cross_zone
            local dest = zone_transitions.ZONE_NAMES[czs.final_zone] or tostring(czs.final_zone)
            print_msg(string.format('Cross-zone: step %d/%d -> %s', czs.step, #czs.route, dest))
        end

    -- /mapper pos
    elseif sub == 'pos' then
        local player = GetPlayerEntity()
        if player == nil or player.ActorPointer == 0 then
            print_msg('Player entity not available.')
            return
        end
        local px = player.Movement.LocalPosition.X
        local py = player.Movement.LocalPosition.Y
        local pz = player.Movement.LocalPosition.Z
        print_msg(string.format('pos=(%.3f, %.3f) elev=%.3f', px, py, pz))

    -- /mapper exits  (debug: list zone exits for current zone with expected positions)
    elseif sub == 'exits' then
        local exits = zone_transitions.get_exits(state.zone_id)
        if #exits == 0 then
            print_msg('No exits recorded for zone ' .. tostring(state.zone_id))
        else
            local zone_name = zone_transitions.ZONE_NAMES[state.zone_id] or tostring(state.zone_id)
            print_msg(string.format('Exits from %s (%d):', zone_name, state.zone_id))
            for i, ex in ipairs(exits) do
                local dest = zone_transitions.ZONE_NAMES[ex.to_zone] or tostring(ex.to_zone)
                local nn = graph.nearest_node(ex.fx, ex.fy)
                local nn_dist = 'no graph'
                if nn then
                    local n = graph.nodes[nn]
                    local dx = n.x - ex.fx
                    local dy = n.y - ex.fy
                    nn_dist = string.format('%.1f yalms', math.sqrt(dx*dx + dy*dy))
                end
                print_msg(string.format('  %d) -> %s(%d) at (%.1f, %.1f)  nearest node: %s',
                    i, dest, ex.to_zone, ex.fx, ex.fy, nn_dist))
            end
        end

    -- /mapper rawcmd (debug: print hex bytes of remainder)
    elseif sub == 'rawcmd' then
        local raw = e.command
        local hex = {}
        for i = 1, #raw do
            table.insert(hex, string.format('%02X', raw:byte(i)))
        end
        print_msg('raw: ' .. table.concat(hex, ' '))
        print_msg('str: ' .. raw)

    -- /mapper (no subcommand) → help
    else
        print_msg('Commands: explore | stop | goto <x> <z> | goto |Zone| [x z] | save | status | pos')
    end
end)
