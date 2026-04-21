--[[
* nav - core/commands.lua
*
* All /mapper command handlers.
*
* Commands:
*   /mapper explore          — autonomous probe-based zone exploration
*   /mapper stop             — halt navigation or exploration
*   /mapper goto <x> <z>     — navigate to world coordinates in current zone
*   /mapper save             — manually save zone graph to disk
*   /mapper status           — print current stats
*   /mapper pos              — print current position
*   /mapper debug [on|off]   — toggle verbose debug output
*   /mapper debug collision  — toggle collision overlay hint
*   /mapper debug graph      — toggle graph overlay hint
*   /mapper debug frontier   — toggle frontier overlay hint
--]]

local S                = require('core.state')
local D                = require('core.debug')
local zone_manager     = require('core.zone_manager')
local adapter          = require('movement.movement_adapter')
local zone_graph       = require('graph.zone_graph')
local planner          = require('planning.planner')
local entities         = require('entities')
local zone_transitions = require('core.zone_transitions')
local zld              = require('core.zone_line_discovery')
local vision           = require('exploration.vision')
local map_coords       = require('core.map_coords')

local commands = {}

-------------------------------------------------------------------------------
-- Internal helpers
-------------------------------------------------------------------------------

local function get_player()
    local p = GetPlayerEntity()
    if p == nil or p.ActorPointer == 0 then return nil end
    return p
end


-------------------------------------------------------------------------------
-- Command dispatch
-------------------------------------------------------------------------------

function commands.handle(e)
    local args = e.command:args()
    if #args == 0 or args[1]:lower() ~= '/mapper' then return end

    e.blocked = true

    local sub = args[2] and args[2]:lower() or ''

    -- /mapper explore
    if sub == 'explore' then
        if S.zone_id == nil or S.zone_id == 0 then
            D.print_msg('Not in a valid zone.')
            return
        end
        adapter.stop()
        planner.clear_goal()
        S.find_target = nil
        S.exploring = true
        S._search_zone = S.zone_id
        D.print_msg('Exploration started.')

    -- /mapper find <name>
    elseif sub == 'find' then
        if S.zone_id == nil or S.zone_id == 0 then
            D.print_msg('Not in a valid zone.')
            return
        end
        local name_parts = {}
        for i = 3, #args do
            name_parts[#name_parts + 1] = args[i]
        end
        local name = table.concat(name_parts, ' ')
        if name == '' then
            D.print_msg('Usage: /mapper find <entity name>')
            return
        end
        adapter.stop()
        planner.clear_goal()
        S.exploring = false
        S.find_target = name
        S._find_reset = true
        S._search_zone = S.zone_id
        D.print_msg(string.format('Searching for %s...', name))

    -- /mapper stop
    elseif sub == 'stop' then
        adapter.stop()
        planner.clear_goal()
        S.exploring = false
        S.find_target = nil
        S._search_zone = nil
        D.print_msg('Stopped.')

    -- /mapper goto <x> <y>   (same-zone coordinate navigation)
    -- x = LocalPosition.X (east/west), y = LocalPosition.Y (north/south)
    -- Use /mapper pos to read your current coordinates.
    elseif sub == 'goto' then
        if S.zone_id == nil or S.zone_id == 0 then
            D.print_msg('Not in a valid zone.')
            return
        end

        local tx, ty
        local label = args[3]
        if label and label:match('^[A-Pa-p]%-?%d+$') then
            tx, ty = map_coords.grid_to_world(S.zone_id, label)
            if tx == nil then
                D.print_msg('No map data for zone ' .. tostring(S.zone_id) .. '.')
                return
            end
            local grid = map_coords.world_to_grid(S.zone_id, tx, ty) or label:upper()
            D.print_msg(string.format('Grid %s → (%.0f, %.0f)', grid, tx, ty))
        else
            tx = tonumber(args[3])
            ty = tonumber(args[4])
        end
        if tx == nil or ty == nil then
            D.print_msg('Usage: /mapper goto <H-10> or /mapper goto <x> <y>')
            return
        end

        adapter.stop()
        S.exploring = false
        S.find_target = nil
        planner.set_goal(tx, ty)
        D.print_msg(string.format('Going to (%.1f, %.1f).', tx, ty))

    -- /mapper save
    elseif sub == 'save' then
        if S.zone_id == nil or S.zone_id == 0 then
            D.print_msg('Not in a valid zone.')
            return
        end
        zone_manager.save(true)
        D.print_msg(string.format('Saved zone %d (%d nodes, %d edges, %d entities).',
            S.zone_id,
            zone_graph.node_count(),
            zone_graph.edge_count(),
            entities.count(S.zone_id)))

    -- /mapper status
    elseif sub == 'status' then
        local mode_str = adapter.mode()
        local col_str  = S.collision and
            string.format('%d tris', #S.collision.triangles) or 'none'
        local exp_str  = S.exploring and 'yes' or 'no'
        local zl_str   = string.format('%d known, %d discovered',
            #zone_transitions.get(S.zone_id or 0), zld.count(S.zone_id))
        D.print_msg(string.format(
            'Zone: %s | Mode: %s | Exploring: %s | Nodes: %d | Edges: %d | Entities: %d | Collision: %s | Zone lines: %s',
            tostring(S.zone_id), mode_str, exp_str,
            zone_graph.node_count(), zone_graph.edge_count(),
            entities.count(S.zone_id), col_str, zl_str))
        if S.goal then
            D.print_msg(string.format('Goal: (%.1f, %.1f)', S.goal.x, S.goal.y))
        end
        if S.find_target then
            D.print_msg(string.format('Searching for: %s', S.find_target))
        end
        if S.zone_return then
            D.print_msg(string.format('Returning to zone %d (phase: %s, attempt %d)',
                S.zone_return.to_zone, S.zone_return.phase,
                S.zone_return.attempts))
        end

    -- /mapper pos
    elseif sub == 'pos' then
        local player = get_player()
        if not player then D.print_msg('Player entity not available.'); return end
        local px = player.Movement.LocalPosition.X
        local py = player.Movement.LocalPosition.Y
        local pz = player.Movement.LocalPosition.Z
        local grid = map_coords.world_to_grid(S.zone_id, px, py) or '?'
        D.print_msg(string.format('pos=(%.3f, %.3f) elev=%.3f [%s]', px, py, pz, grid))

    -- /mapper debug [on|off|collision|graph|frontier]
    elseif sub == 'debug' then
        local flag = args[3] and args[3]:lower() or 'toggle'
        if flag == 'on' then
            S.debug.enabled = true
            D.print_msg('Debug output enabled.')
        elseif flag == 'off' then
            S.debug.enabled = false
            D.print_msg('Debug output disabled.')
        elseif flag == 'collision' then
            S.debug.show_collision = not S.debug.show_collision
            D.print_msg('Collision overlay: ' .. (S.debug.show_collision and 'on' or 'off'))
        elseif flag == 'graph' then
            S.debug.show_graph = not S.debug.show_graph
            D.print_msg('Graph overlay: ' .. (S.debug.show_graph and 'on' or 'off'))
        elseif flag == 'frontier' then
            S.debug.show_frontier = not S.debug.show_frontier
            D.print_msg('Frontier overlay: ' .. (S.debug.show_frontier and 'on' or 'off'))
        else
            S.debug.enabled = not S.debug.enabled
            D.print_msg('Debug output: ' .. (S.debug.enabled and 'on' or 'off'))
        end

    -- /mapper repair
    elseif sub == 'repair' then
        if S.zone_id == nil or S.zone_id == 0 then
            D.print_msg('Not in a valid zone.')
            return
        end
        vision.start_repair(S.graph)

    -- /mapper clear
    elseif sub == 'clear' then
        if S.zone_id == nil or S.zone_id == 0 then
            D.print_msg('Not in a valid zone.')
            return
        end
        adapter.stop()
        planner.clear_goal()
        S.exploring = false
        local old_nodes = zone_graph.node_count()
        zone_graph.reset()
        vision.reset()
        S.stuck_blacklist = {}
        zone_manager.save(true)
        D.print_msg(string.format('Cleared zone %d graph (%d nodes removed).', S.zone_id, old_nodes))

    -- /mapper (no subcommand) → help
    else
        D.print_msg('Commands: explore | find <name> | stop | goto <x> <y> | save | clear | status | pos | debug [on|off]')
    end
end

return commands
