--[[
* mapper - mapper.lua
*
* Ashita v4 addon: thin movement client for Python navigation server.
*
* Commands:
*   /mapper goto <x> <y>              - navigate to coordinates
*   /mapper goto <x> <y> <zone name>  - cross-zone navigation
*   /mapper goto <zone name>           - navigate to zone
*   /mapper goto "entity name"         - navigate to known entity
*   /mapper find "entity name"         - search zone for entity
*   /mapper stop                       - stop movement/search
*   /mapper pos                        - print current position
*   /mapper status                     - show state + entity count
--]]

addon.name    = 'mapper'
addon.author  = 'xillm'
addon.version = '.9'
addon.desc    = 'Navigation client for FFXI (Python navserver backend)'
addon.link    = ''

require('common')

local json     = require('json')
local struct   = require('struct')
local d3d8     = require('d3d8')
local imgui    = require('imgui')
local entities = require('entities')

-------------------------------------------------------------------------------
-- Config
-------------------------------------------------------------------------------
local WAYPOINT_RADIUS    = 1.0
local STUCK_CHECK_FRAMES = 120
local STUCK_MIN_PROGRESS = 0.5
local STATUS_INTERVAL    = 30

local BACKTRACK_FRAMES   = 25
local SIDESTEP_FRAMES    = 40
local RESUME_FRAMES      = 25
local ENTITY_SCAN_INTERVAL = 90
local ENTITY_SAVE_INTERVAL = 1800

-------------------------------------------------------------------------------
-- State
-------------------------------------------------------------------------------
local state = {
    frame       = 0,
    zone_id     = nil,
    data_path   = '',
    -- Movement
    waypoints   = nil,
    wp_idx      = 1,
    moving      = false,
    goal        = nil,
    -- Stuck detection
    check_x     = 0,
    check_y     = 0,
    check_frame = 0,
    stuck_count = 0,
    -- Avoidance maneuver
    avoidance   = nil,
    -- Request/response handshake
    pending_seq = nil,   -- sequence number of request awaiting response
    last_seq    = 0,     -- monotonic counter
    -- Weather
    weather_id  = 0,
    -- Search mode
    search      = nil,   -- { target_name, waypoints, wp_idx }
    -- Cross-zone route
    route       = nil,   -- { segments, seg_idx, target_zone, target_name }
}

-------------------------------------------------------------------------------
-- Helpers
-------------------------------------------------------------------------------

local function get_data_path()
    local ok, path = pcall(function()
        return AshitaCore:GetInstallPath()
    end)
    if ok and path then
        if path:sub(-1) ~= '/' and path:sub(-1) ~= '\\' then
            path = path .. '/'
        end
        return path .. 'config/addons/mapper/'
    end
    return ''
end

local function msg(text)
    print('\30\06[mapper]\30\01 ' .. text)
end

local function ipc_path(filename)
    return state.data_path .. filename
end

local function drive_toward(px, py, tx, ty)
    local follow = AshitaCore:GetMemoryManager():GetAutoFollow()
    if follow == nil then return end
    local dx = tx - px
    local dy = ty - py
    local len = math.sqrt(dx * dx + dy * dy)
    if len > 0.01 then
        dx = dx / len
        dy = dy / len
    end
    follow:SetFollowDeltaX(dx)
    follow:SetFollowDeltaY(dy)
    follow:SetFollowDeltaZ(0)
    follow:SetIsAutoRunning(1)
end

local function stop_autofollow()
    local follow = AshitaCore:GetMemoryManager():GetAutoFollow()
    if follow then follow:SetIsAutoRunning(0) end
end

local function stop_movement()
    stop_autofollow()
    state.moving = false
    state.waypoints = nil
    state.wp_idx = 1
end

local function cancel_all()
    stop_movement()
    state.goal = nil
    state.pending_seq = nil
    state.stuck_count = 0
    state.avoidance = nil
    state.search = nil
    state.route = nil
end

local function dist2d(ax, ay, bx, by)
    local dx = ax - bx
    local dy = ay - by
    return math.sqrt(dx * dx + dy * dy)
end

local function resolve_zone_name(name)
    name = name:gsub('\xEF.', '')
    local rm = AshitaCore:GetResourceManager()
    local name_lower = name:lower()
    for id = 0, 300 do
        local zname = rm:GetString('zones.names', id)
        if zname and zname:lower() == name_lower then
            return id, zname
        end
    end
    for id = 0, 300 do
        local zname = rm:GetString('zones.names', id)
        if zname and zname:lower():find(name_lower, 1, true) then
            return id, zname
        end
    end
    return nil
end

local function get_zone_name(zone_id)
    if not zone_id then return '?' end
    local rm = AshitaCore:GetResourceManager()
    return rm:GetString('zones.names', zone_id) or tostring(zone_id)
end

-------------------------------------------------------------------------------
-- File I/O
-------------------------------------------------------------------------------

local function read_json(path)
    local f = io.open(path, 'r')
    if not f then return nil end
    local text = f:read('*a')
    f:close()
    if not text or text == '' then return nil end
    local ok, data = pcall(json.decode, text)
    if not ok then return nil end
    return data
end

local function write_json(path, data)
    local f = io.open(path, 'w')
    if not f then return false end
    f:write(json.encode(data))
    f:close()
    return true
end

-------------------------------------------------------------------------------
-- Entity persistence
-------------------------------------------------------------------------------

local function save_entities(zone_id)
    if not zone_id or zone_id == 0 then return end
    local data = entities.serialize(zone_id)
    if data then
        write_json(ipc_path('entities_' .. zone_id .. '.json'), data)
    end
end

local function load_entities(zone_id)
    if not zone_id or zone_id == 0 then return end
    local data = read_json(ipc_path('entities_' .. zone_id .. '.json'))
    entities.load(data, zone_id)
    local n = entities.count(zone_id)
    if n > 0 then
        msg(string.format('Loaded %d entity records for zone %d.', n, zone_id))
    end
end

-------------------------------------------------------------------------------
-- Navigation requests
-------------------------------------------------------------------------------

local function request_path(px, py, pz, tx, ty, obstacle)
    state.last_seq = state.last_seq + 1
    local seq = state.last_seq
    local data = {
        action  = 'goto',
        zone_id = state.zone_id,
        player  = { px, py, pz },
        target  = { tx, ty, pz },
        seq     = seq,
    }
    if obstacle then
        data.new_obstacle = obstacle
    end
    write_json(ipc_path('nav_request.json'), data)
    state.pending_seq = seq
    state.goal = { x = tx, y = ty }
    msg(string.format('Requesting path to (%.0f, %.0f)... [#%d]', tx, ty, seq))
end

local function check_path_response()
    if state.pending_seq == nil then return end

    local data = read_json(ipc_path('nav_path.json'))
    if not data then return end

    -- Only accept responses matching our pending request
    if data.seq ~= state.pending_seq then return end

    -- Response received - clear pending
    state.pending_seq = nil

    -- Handle cross_zone_goto response
    if data.action == 'cross_zone_goto' then
        if data.status == 'ok' and data.route and #data.route > 0 then
            local last_seg = data.route[#data.route]
            local target_zone = last_seg.next_zone or last_seg.zone_id
            state.route = {
                segments = data.route,
                seg_idx = 1,
                needs_path = true,
                target_zone = target_zone,
                target_pos = (not last_seg.is_transition) and last_seg.target or nil,
                replans = state.route_replans or 0,
            }
            state.route_replans = nil
            local names = {}
            local seen = {}
            for _, seg in ipairs(data.route) do
                local n = get_zone_name(seg.zone_id)
                if not seen[n] then
                    names[#names + 1] = n
                    seen[n] = true
                end
            end
            if data.route[#data.route].next_zone then
                local final = get_zone_name(data.route[#data.route].next_zone)
                if not seen[final] then names[#names + 1] = final end
            end
            msg(string.format('Route: %s (%d segments)',
                table.concat(names, ' > '), #data.route))
        elseif data.status == 'already_there' then
            msg('Already in target zone.')
            state.route = nil
        elseif data.status == 'no_route' then
            msg('No route found to destination zone.')
            state.route = nil
        else
            msg('Cross-zone error: ' .. (data.message or data.status or 'unknown'))
            state.route = nil
        end
        return
    end

    -- Handle search_points response
    if data.action == 'search_points' then
        if state.search and data.status == 'ok' and data.waypoints then
            state.search.waypoints = data.waypoints
            state.search.wp_idx = 1
            msg(string.format('Search plan: %d points. Searching for "%s"...',
                #data.waypoints, state.search.target_name))
        elseif data.status == 'error' then
            msg('Search error: ' .. (data.message or 'unknown'))
            state.search = nil
        end
        return
    end

    if (data.status == 'ok' or data.status == 'partial') and data.waypoints and #data.waypoints > 0 then
        state.waypoints = data.waypoints
        state.wp_idx = 1
        state.moving = true
        state.stuck_count = 0
        state.check_frame = state.frame
        if data.status == 'partial' then
            msg(string.format('Partial path: %d waypoints (ends %.0f yalms from target).',
                #data.waypoints, data.end_dist or 0))
        else
            msg(string.format('Path received: %d waypoints.', #data.waypoints))
        end
    elseif data.status == 'partial' and state.search and (data.end_dist or 0) > 30 then
        msg(string.format('Search point unreachable (%.0fy short) - skipping.', data.end_dist or 0))
        stop_movement()
        state.pending_seq = nil
    elseif data.status == 'no_path' then
        if state.search then
            msg('Search point unreachable - skipping.')
        else
            msg('No path found - destination may be unreachable.')
            cancel_all()
        end
    elseif data.status == 'error' then
        msg('Server error: ' .. (data.message or 'unknown'))
        cancel_all()
    end
end

-------------------------------------------------------------------------------
-- Obstacle avoidance
-------------------------------------------------------------------------------

local function start_avoidance(px, py)
    local wp = state.waypoints and state.waypoints[state.wp_idx]
    if not wp then return false end

    local dx = wp[1] - px
    local dy = wp[2] - py
    local len = math.sqrt(dx * dx + dy * dy)
    if len < 0.01 then return false end
    dx, dy = dx / len, dy / len

    local side = (state.stuck_count % 2 == 0) and 1 or -1
    state.avoidance = {
        phase = 'backtrack',
        frame_start = state.frame,
        fwd_x = dx,
        fwd_y = dy,
        perp_x = -dy * side,
        perp_y = dx * side,
    }
    msg(string.format('Obstacle detected - maneuvering (attempt %d)...', state.stuck_count))
    return true
end

local function avoidance_tick(px, py)
    local av = state.avoidance
    if not av then return end

    local elapsed = state.frame - av.frame_start

    if av.phase == 'backtrack' then
        if elapsed < BACKTRACK_FRAMES then
            drive_toward(px, py, px - av.fwd_x * 5, py - av.fwd_y * 5)
        else
            av.phase = 'sidestep'
            av.frame_start = state.frame
        end
    elseif av.phase == 'sidestep' then
        if elapsed < SIDESTEP_FRAMES then
            drive_toward(px, py, px + av.perp_x * 5, py + av.perp_y * 5)
        else
            av.phase = 'resume'
            av.frame_start = state.frame
        end
    elseif av.phase == 'resume' then
        if elapsed < RESUME_FRAMES then
            drive_toward(px, py, px + av.fwd_x * 5, py + av.fwd_y * 5)
        else
            state.avoidance = nil
            state.check_frame = state.frame
            state.check_x = px
            state.check_y = py
            state.check_wp_idx = state.wp_idx
        end
    end
end

-------------------------------------------------------------------------------
-- Movement tick
-------------------------------------------------------------------------------

local function movement_tick(px, py, pz)
    if not state.moving or not state.waypoints then return end

    if state.avoidance then
        avoidance_tick(px, py)
        return
    end

    local wp = state.waypoints[state.wp_idx]
    if not wp then
        stop_movement()
        if not state.search and not state.route then
            msg('Reached destination.')
        end
        state.goal = nil
        return
    end

    local wx, wy = wp[1], wp[2]
    local d = dist2d(px, py, wx, wy)

    if d < WAYPOINT_RADIUS then
        state.wp_idx = state.wp_idx + 1
        state.stuck_count = 0
        state.check_x = px
        state.check_y = py
        state.check_frame = state.frame

        if state.wp_idx > #state.waypoints then
            stop_movement()
            if not state.search and not state.route then
                msg('Reached destination.')
            end
            state.goal = nil
            return
        end
        wp = state.waypoints[state.wp_idx]
        wx, wy = wp[1], wp[2]
    end

    drive_toward(px, py, wx, wy)

    -- Stuck detection: no waypoint advancement for STUCK_CHECK_FRAMES
    if state.frame - state.check_frame >= STUCK_CHECK_FRAMES then
        local wp_progress = state.wp_idx - (state.check_wp_idx or state.wp_idx)
        local pos_progress = dist2d(px, py, state.check_x, state.check_y)
        state.check_x = px
        state.check_y = py
        state.check_wp_idx = state.wp_idx
        state.check_frame = state.frame

        if wp_progress < 3 and pos_progress < 3.0 then
            state.stuck_count = state.stuck_count + 1

            if state.search and state.stuck_count >= 2 then
                msg('Search point stuck - skipping.')
                stop_movement()
                state.stuck_count = 0
                state.goal = nil
                return
            end

            if state.stuck_count >= 6 then
                msg('Cannot reach destination after multiple retries.')
                cancel_all()
                return
            end

            if state.stuck_count % 2 == 1 then
                msg(string.format('Stuck (attempt %d) at wp %d/%d - trying avoidance.',
                    state.stuck_count, state.wp_idx, #state.waypoints))
                start_avoidance(px, py)
            else
                msg(string.format('Stuck (attempt %d) at wp %d/%d - reporting obstacle, repathing.',
                    state.stuck_count, state.wp_idx, #state.waypoints))
                local obstacle = { px, py, pz }
                local goal = state.goal
                stop_movement()
                if goal then
                    request_path(px, py, pz, goal.x, goal.y, obstacle)
                end
            end
        end
    end
end

-------------------------------------------------------------------------------
-- Path overlay
-------------------------------------------------------------------------------

local draw_ok = true
local draw_err_msg = nil

local function draw_path()
    if not draw_ok then return end

    local ok, err = pcall(function()
        local fg = imgui.GetForegroundDrawList()
        if not fg then error('no draw list') end

        if not state.moving or not state.waypoints then return end

        local dev = d3d8.get_device()
        if not dev then error('no device') end

        local r1, view = dev:GetTransform(2)
        local r2, proj = dev:GetTransform(3)
        if r1 ~= 0 or r2 ~= 0 or not view or not proj then
            error(string.format('transform fail v=%d p=%d', r1 or -1, r2 or -1))
        end

        local r3, vp = dev:GetViewport()
        if r3 ~= 0 or not vp then error('viewport fail') end

        local black = 0xFF000000
        local green = 0xFF00FF00
        local red   = 0xFF0000FF

        local prev_sx, prev_sy = nil, nil
        local wps = state.waypoints
        local idx = state.wp_idx

        for i = 1, #wps do
            local wp = wps[i]
            local gx, gy, gz = wp[1], wp[2], wp[3]
            local wx, wy, wz = gx, gz, gy

            local vx = wx*view._11 + wy*view._21 + wz*view._31 + view._41
            local vy = wx*view._12 + wy*view._22 + wz*view._32 + view._42
            local vz = wx*view._13 + wy*view._23 + wz*view._33 + view._43
            local vw = wx*view._14 + wy*view._24 + wz*view._34 + view._44

            local cx = vx*proj._11 + vy*proj._21 + vz*proj._31 + vw*proj._41
            local cy = vx*proj._12 + vy*proj._22 + vz*proj._32 + vw*proj._42
            local cw = vx*proj._14 + vy*proj._24 + vz*proj._34 + vw*proj._44

            local sx, sy = nil, nil
            if cw > 0.001 then
                sx = (cx/cw * 0.5 + 0.5) * vp.Width + vp.X
                sy = (-cy/cw * 0.5 + 0.5) * vp.Height + vp.Y
            end

            if sx and prev_sx then
                local col = (i <= idx) and green or black
                fg:AddLine({ prev_sx, prev_sy }, { sx, sy }, col, 2.0)
            end
            if sx then
                if i == idx then
                    fg:AddCircleFilled({ sx, sy }, 5, red)
                end
                prev_sx, prev_sy = sx, sy
            else
                prev_sx, prev_sy = nil, nil
            end
        end
    end)

    if not ok then
        if not draw_err_msg or draw_err_msg ~= tostring(err) then
            draw_err_msg = tostring(err)
            msg('Path draw error: ' .. draw_err_msg)
        end
        draw_ok = false
    end
end

-------------------------------------------------------------------------------
-- Events
-------------------------------------------------------------------------------

ashita.events.register('load', 'mapper_load', function()
    state.data_path = get_data_path()
    local f = io.open(ipc_path('nav_path.json'), 'w')
    if f then f:write('{}') f:close() end
    local zone_id = AshitaCore:GetMemoryManager():GetParty():GetMemberZone(0)
    if zone_id and zone_id ~= 0 then
        state.zone_id = zone_id
        load_entities(zone_id)
    end
    msg('Loaded v' .. addon.version .. '. /mapper goto <x> <y> [zone] | goto <zone> | find "name"')
end)

ashita.events.register('unload', 'mapper_unload', function()
    save_entities(state.zone_id)
    cancel_all()
end)

ashita.events.register('packet_in', 'mapper_packet', function(e)
    if e.id == 0x057 then
        local ok, weather = pcall(struct.unpack, 'H', e.data, 0x04 + 1)
        if ok and weather then
            state.weather_id = weather
            entities.set_weather(weather)
        end
    end
end)

ashita.events.register('d3d_present', 'mapper_render', function()
    state.frame = state.frame + 1

    local ok, player = pcall(GetPlayerEntity)
    if not ok or player == nil then return end
    local ok2, ptr = pcall(function() return player.ActorPointer end)
    if not ok2 or ptr == nil or ptr == 0 then return end

    local ok3, px, py, pz = pcall(function()
        return player.Movement.LocalPosition.X,
               player.Movement.LocalPosition.Y,
               player.Movement.LocalPosition.Z
    end)
    if not ok3 then return end

    -- Zone change detection
    if state.frame % 5 == 0 then
        local zone_id = AshitaCore:GetMemoryManager():GetParty():GetMemberZone(0)
        if zone_id ~= nil and zone_id ~= 0 and zone_id ~= state.zone_id then
            save_entities(state.zone_id)

            if state.route then
                -- Cross-zone route active: advance to next segment
                stop_movement()
                state.pending_seq = nil
                state.stuck_count = 0
                state.avoidance = nil
                state.search = nil
                state.goal = nil

                local r = state.route
                local seg = r.segments[r.seg_idx]
                if seg and seg.next_zone and seg.next_zone == zone_id then
                    r.seg_idx = r.seg_idx + 1
                    msg(string.format('Entered %s. Segment %d/%d.',
                        get_zone_name(zone_id), r.seg_idx, #r.segments))
                    if r.seg_idx > #r.segments then
                        msg(string.format('Arrived in %s!', get_zone_name(zone_id)))
                        state.route = nil
                    else
                        r.needs_path = true
                        r.zone_entry_frame = state.frame
                    end
                elseif zone_id == r.target_zone then
                    msg(string.format('Arrived in %s!', get_zone_name(zone_id)))
                    state.route = nil
                else
                    r.replans = (r.replans or 0) + 1
                    if r.replans > 3 then
                        msg('Route failed after multiple re-plans - giving up.')
                        state.route = nil
                    else
                        local prev_zone = state.zone_id
                        msg(string.format('Entered %s (re-plan %d, avoiding %s)...',
                            get_zone_name(zone_id), r.replans, get_zone_name(prev_zone)))
                        local target_zone = r.target_zone
                        local target_pos = r.target_pos
                        local replans = r.replans
                        state.route = nil
                        state.last_seq = state.last_seq + 1
                        local seq = state.last_seq
                        local req = {
                            action = 'cross_zone_goto',
                            zone_id = zone_id,
                            player = { px, py, pz },
                            target_zone = target_zone,
                            avoid_zones = { prev_zone },
                            seq = seq,
                        }
                        if target_pos then
                            req.target = target_pos
                        end
                        write_json(ipc_path('nav_request.json'), req)
                        state.pending_seq = seq
                        state.route_replans = replans
                    end
                end
            elseif state.moving or state.search then
                cancel_all()
                msg('Zone changed - stopping.')
            end

            state.zone_id = zone_id
            load_entities(zone_id)
        end
    end

    if state.zone_id == nil or state.zone_id == 0 then return end

    -- Entity scanning
    if state.frame % ENTITY_SCAN_INTERVAL == 0 then
        entities.scan(state.zone_id)
    end

    -- Check for path responses from server
    if state.frame % 6 == 0 then
        check_path_response()
    end

    -- Route tick: request path for current cross-zone segment
    if state.route and state.route.needs_path and not state.pending_seq then
        local r = state.route
        local seg = r.segments[r.seg_idx]
        if seg and seg.target then
            -- After zoning, drive toward target for 90 frames before requesting navmesh path
            -- This moves the player away from the zone boundary to avoid bounce-back
            if r.zone_entry_frame then
                drive_toward(px, py, seg.target[1], seg.target[2])
                if state.frame - r.zone_entry_frame >= 90 then
                    r.zone_entry_frame = nil
                    r.needs_path = false
                    request_path(px, py, pz, seg.target[1], seg.target[2])
                end
            else
                r.needs_path = false
                request_path(px, py, pz, seg.target[1], seg.target[2])
            end
        end
    end

    -- Route completion: reached final segment destination
    if state.route and not state.moving and not state.pending_seq then
        local r = state.route
        if r.seg_idx > #r.segments then
            msg(string.format('Route complete - arrived in %s.',
                get_zone_name(state.zone_id)))
            state.route = nil
        end
    end

    -- Search tick
    if state.search and state.search.waypoints then
        local s = state.search
        local match = entities.find_by_name(state.zone_id, s.target_name, px, py)
        if match then
            cancel_all()
            msg(string.format('Found %s at (%.0f, %.0f)! Navigating...', match.name, match.x, match.y))
            request_path(px, py, pz, match.x, match.y)
        elseif not state.moving and not state.pending_seq then
            if s.wp_idx > #s.waypoints then
                local name = s.target_name
                state.search = nil
                msg(string.format('Search complete - "%s" not found in zone.', name))
            else
                local wp = s.waypoints[s.wp_idx]
                s.wp_idx = s.wp_idx + 1
                request_path(px, py, pz, wp[1], wp[2])
            end
        end
    end

    -- Movement execution
    if state.moving then
        movement_tick(px, py, pz)
    end

    -- Periodic entity save
    if state.frame % ENTITY_SAVE_INTERVAL == 0 then
        save_entities(state.zone_id)
    end

    -- Write status for monitoring
    if state.frame % STATUS_INTERVAL == 0 then
        write_json(ipc_path('nav_status.json'), {
            zone_id = state.zone_id,
            zone_name = get_zone_name(state.zone_id),
            x = px, y = py, z = pz,
            moving = state.moving,
            wp_idx = state.wp_idx,
            wp_total = state.waypoints and #state.waypoints or 0,
            pending = state.pending_seq,
            stuck = state.stuck_count,
            entities = entities.count(state.zone_id),
            searching = state.search and state.search.target_name or nil,
            route_seg = state.route and state.route.seg_idx or nil,
            route_total = state.route and #state.route.segments or nil,
        })
    end

    -- Draw path overlay
    draw_path()
end)

ashita.events.register('command', 'mapper_cmd', function(e)
    local args = e.command:args()
    if #args == 0 or args[1] ~= '/mapper' then return end
    e.blocked = true

    local cmd = args[2] and args[2]:lower() or 'help'

    local function get_player_pos()
        local ok, player = pcall(GetPlayerEntity)
        if not ok or not player then return nil end
        return player.Movement.LocalPosition.X,
               player.Movement.LocalPosition.Y,
               player.Movement.LocalPosition.Z
    end

    local function parse_name(args_tbl, start_idx)
        if #args_tbl < start_idx then return nil end
        local rest = table.concat(args_tbl, ' ', start_idx)
        return rest:match('^"(.-)"$') or rest:match("^'(.-)'$") or rest
    end

    if cmd == 'goto' then
        if #args < 3 then
            msg('Usage: /mapper goto <x> <y> [zone] | goto <zone> | goto "entity"')
            return
        end

        local tx = tonumber(args[3])
        local ty = tonumber(args[4])

        local px, py, pz = get_player_pos()
        if not px then
            msg('Cannot get player position.')
            return
        end

        if tx and ty then
            -- Check for zone name after coordinates
            if #args >= 5 then
                local zone_name = table.concat(args, ' ', 5)
                zone_name = zone_name:gsub('\xEF.', '')
                local zone_id, resolved_name = resolve_zone_name(zone_name)
                if not zone_id then
                    msg(string.format('Unknown zone: %s', zone_name))
                    return
                end
                if zone_id == state.zone_id then
                    cancel_all()
                    request_path(px, py, pz, tx, ty)
                else
                    cancel_all()
                    state.last_seq = state.last_seq + 1
                    local seq = state.last_seq
                    write_json(ipc_path('nav_request.json'), {
                        action = 'cross_zone_goto',
                        zone_id = state.zone_id,
                        player = { px, py, pz },
                        target = { tx, ty, 0 },
                        target_zone = zone_id,
                        seq = seq,
                    })
                    state.pending_seq = seq
                    msg(string.format('Planning route to (%.0f, %.0f) in %s...', tx, ty, resolved_name))
                end
            else
                cancel_all()
                request_path(px, py, pz, tx, ty)
            end
        else
            local name = parse_name(args, 3)
            if not name or name == '' then
                msg('Usage: /mapper goto <x> <y> [zone] | goto <zone> | goto "entity"')
                return
            end

            -- Try zone name first
            local zone_id, resolved_name = resolve_zone_name(name)
            if zone_id and zone_id ~= state.zone_id then
                cancel_all()
                state.last_seq = state.last_seq + 1
                local seq = state.last_seq
                write_json(ipc_path('nav_request.json'), {
                    action = 'cross_zone_goto',
                    zone_id = state.zone_id,
                    player = { px, py, pz },
                    target_zone = zone_id,
                    seq = seq,
                })
                state.pending_seq = seq
                msg(string.format('Planning route to %s...', resolved_name))
                return
            end

            -- Try entity name
            local match = entities.find_by_name(state.zone_id, name, px, py)
            if match then
                msg(string.format('Navigating to %s at (%.0f, %.0f).', match.name, match.x, match.y))
                cancel_all()
                request_path(px, py, pz, match.x, match.y)
            else
                msg(string.format('No zone or entity "%s" found.', name))
            end
        end

    elseif cmd == 'find' then
        local name = parse_name(args, 3)
        if not name or name == '' then
            msg('Usage: /mapper find "entity name"')
            return
        end

        local px, py, pz = get_player_pos()
        if not px then
            msg('Cannot get player position.')
            return
        end

        local match = entities.find_by_name(state.zone_id, name, px, py)
        if match then
            msg(string.format('Already know %s at (%.0f, %.0f). Navigating.', match.name, match.x, match.y))
            cancel_all()
            request_path(px, py, pz, match.x, match.y)
            return
        end

        cancel_all()
        local entity_pos = {}
        local records = entities.zone_records[state.zone_id]
        if records then
            for _, rec in pairs(records) do
                entity_pos[#entity_pos + 1] = { rec.center_x, rec.center_y }
            end
        end
        state.last_seq = state.last_seq + 1
        local seq = state.last_seq
        write_json(ipc_path('nav_request.json'), {
            action = 'search_points',
            zone_id = state.zone_id,
            player = { px, py, pz },
            entity_positions = entity_pos,
            seq = seq,
        })
        state.pending_seq = seq
        state.search = {
            target_name = name,
            waypoints = nil,
            wp_idx = 1,
        }
        msg(string.format('Searching zone %d for "%s"...', state.zone_id, name))

    elseif cmd == 'stop' then
        cancel_all()
        msg('Stopped.')

    elseif cmd == 'pos' then
        local px, py, pz = get_player_pos()
        if px then
            msg(string.format('pos=(%.3f, %.3f) elev=%.3f zone=%s',
                px, py, pz, tostring(state.zone_id)))
        end

    elseif cmd == 'status' then
        if state.route then
            local r = state.route
            msg(string.format('Cross-zone route: segment %d/%d (%s)',
                r.seg_idx, #r.segments, get_zone_name(state.zone_id)))
        end
        if state.search then
            local s = state.search
            local total = s.waypoints and #s.waypoints or 0
            msg(string.format('Searching for "%s": point %d/%d',
                s.target_name, s.wp_idx - 1, total))
        elseif state.pending_seq then
            msg(string.format('Waiting for path response [#%d]...', state.pending_seq))
        elseif state.moving then
            msg(string.format('Moving: waypoint %d/%d, stuck=%d',
                state.wp_idx, state.waypoints and #state.waypoints or 0,
                state.stuck_count))
        else
            if not state.route then msg('Idle.') end
        end
        msg(string.format('Zone: %s (%s) | Entities: %d',
            get_zone_name(state.zone_id), tostring(state.zone_id),
            entities.count(state.zone_id)))

    else
        msg('Commands: goto <x> <y> [zone] | goto <zone> | goto "name" | find "name" | stop | pos | status')
    end
end)
