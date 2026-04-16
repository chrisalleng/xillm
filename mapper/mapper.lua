--[[
* mapper - mapper.lua
*
* Ashita v4 addon that autonomously explores a zone, builds a navigable node
* graph, records NPCs and interactive objects, and can navigate the player to
* arbitrary world coordinates using A* over the recorded graph.
*
* Commands:
*   /mapper explore        -- Start autonomous exploration of current zone
*   /mapper stop           -- Stop exploration or navigation
*   /mapper goto <x> <z>  -- Navigate to world coordinates
*   /mapper save           -- Manually save zone data to disk
*   /mapper status         -- Print current stats
 *   /mapper pos            -- Print current position coordinates
*
* Data is persisted per zone in:
*   <AshitaPath>/config/addons/mapper/zone_<id>.json
--]]

addon.name    = 'mapper'
addon.author  = 'xillm'
addon.version = '2.3'
addon.desc    = 'Autonomous zone mapper and navigator for FFXI'
addon.link    = ''

require('common')
local json      = require('json')
local graph     = require('graph')
local entities  = require('entities')
local navigator = require('navigator')

-------------------------------------------------------------------------------
-- Module state
-------------------------------------------------------------------------------
local state = {
    zone_id   = nil,
    data_path = nil,
    frame     = 0,
}

-------------------------------------------------------------------------------
-- Helpers
-------------------------------------------------------------------------------

local function print_msg(msg)
    AshitaCore:GetChatManager():QueueCommand(1, '/echo [mapper] ' .. tostring(msg))
end

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

    -- Zone change detection (check every 60 frames)
    if state.frame % 60 == 0 then
        local zone_id = AshitaCore:GetMemoryManager():GetParty():GetMemberZone(0)
        if zone_id ~= nil and zone_id ~= 0 then
            if zone_id ~= state.zone_id then
                -- Zone changed: save old data, stop any active mode, load new zone
                if state.zone_id ~= nil then
                    save_zone(state.zone_id)
                end
                navigator.stop()
                zone_init(zone_id)
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
        if navigator.is_active() then
            navigator.stop()
            print_msg('Stopped.')
        else
            print_msg('Nothing active to stop.')
        end

    -- /mapper goto <x> <z>
    elseif sub == 'goto' then
        local tx = tonumber(args[3])
        local tz = tonumber(args[4])
        if tx == nil or tz == nil then
            print_msg('Usage: /mapper goto <x> <z>')
            return
        end
        if state.zone_id == nil or state.zone_id == 0 then
            print_msg('Not in a valid zone.')
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
        print_msg(string.format(
            'Zone: %s | Mode: %s | Nodes: %d | Edges: %d | Entities: %d',
            tostring(state.zone_id), mode_str, nodes, edges, ents))

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

    -- /mapper (no subcommand) → help
    else
        print_msg('Commands: explore | stop | goto <x> <z> | save | status | pos')
    end
end)
