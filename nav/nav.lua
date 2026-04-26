--[[
* nav - nav.lua
*
* Ashita v4 addon: thin movement client for Python navigation server.
*
* Commands:
*   /nav goto <x> <y>              - navigate to coordinates
*   /nav goto <x> <y> <zone name>  - cross-zone navigation
*   /nav goto <zone name>           - navigate to zone
*   /nav goto "entity name"         - navigate to known entity
*   /nav find "entity name"         - search zone for entity
*   /nav stop                       - stop movement/search
*   /nav pos                        - print current position
*   /nav status                     - show state + entity count
--]]

addon.name    = 'nav'
addon.author  = 'xillm'
addon.version = '.36'
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
    -- Preview mode: load waypoints without moving the character
    preview_mode = false,
    -- Stuck detection
    check_x     = 0,
    check_y     = 0,
    check_frame = 0,
    stuck_count = 0,
    -- Request/response handshake
    pending_seq = nil,   -- sequence number of request awaiting response
    last_seq    = 0,     -- monotonic counter
    -- Weather
    weather_id  = 0,
    -- Search mode
    search      = nil,   -- { target_name, waypoints, wp_idx }
    -- Cross-zone route
    route       = nil,   -- { segments, seg_idx, target_zone, target_name }
    -- Object instance overlay (debug): list of { name, collision, bbox_min, bbox_max }
    instances     = nil,
    draw_objects  = false,
    object_range  = 50.0,  -- yalms; only draw instances whose center is within this radius
    -- Drop-off connection overlay (debug): list of { start, end, radius, bidir, source }
    dropoffs      = nil,
    draw_dropoffs = false,
    dropoff_range = 80.0,  -- yalms; only draw connections whose midpoint is within this radius
    -- Recording mode: captures player positions for offline comparison vs navmesh
    record_active = false,
    record_path   = nil,   -- list of { x, y, z, frame }
    record_zone   = nil,
    record_last_x = nil,
    record_last_y = nil,
    -- Retry state machine for stuck/no-route fallback. Lifecycle:
    --   1. Fresh `/nav goto` arms `retry.target` and `phase='normal'`.
    --   2. On stuck or no_path, escalate phase: 'normal' -> 'wide' (re-request
    --      with wider_radius=true), then up to 3 random-walk attempts before
    --      giving up and appending the failure to nav_failures.json.
    --   3. Reaching the destination resets phase + walks to 0.
    retry = {
        phase   = 'normal',  -- 'normal' | 'wide' | 'walking' | 'failed'
        target  = nil,       -- { x, y, zone_id } — current retry goal (becomes detour resume point during wide-detour)
        walks   = 0,         -- number of random-walk attempts (0..3)
        is_walk = false,     -- true while the active path is a random walk, not the real goal
    },
    -- Detour state: when wide-radius retry fires, we save the in-progress
    -- default-radius waypoints + the index 5 ahead and only re-plan the
    -- short stretch around the stuck point at 1.0 radius. Once the detour
    -- arrives at that 5-ahead point, we splice the remainder of the
    -- saved waypoints back in and continue the original long path.
    detour = nil,  -- { saved_waypoints, resume_idx, original_target }
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
        return path .. 'config/addons/nav/'
    end
    return ''
end

local function msg(text)
    print('\30\06[nav]\30\01 ' .. text)
end

local function ipc_path(filename)
    return state.data_path .. filename
end

local function get_player_pos()
    local ok, player = pcall(GetPlayerEntity)
    if not ok or not player then return nil end
    return player.Movement.LocalPosition.X,
           player.Movement.LocalPosition.Y,
           player.Movement.LocalPosition.Z
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
    -- Don't clear state.waypoints here. The retry detour logic needs
    -- to read the in-progress waypoint chain when a stuck triggers
    -- retry_or_fail (which calls stop_movement first). Stale waypoints
    -- are inert with state.moving=false and get overwritten when the
    -- next path response arrives, so leaving them is safe.
end

local function cancel_all()
    stop_movement()
    state.goal = nil
    state.pending_seq = nil
    state.stuck_count = 0
    state.search = nil
    state.route = nil
    -- Clear retry state on user-initiated cancel. Internal escalations
    -- (retry_or_fail) call cancel_all only after exhausting retries, so
    -- losing the retry target here is also correct in that case.
    state.retry.phase = 'normal'
    state.retry.target = nil
    state.retry.walks = 0
    state.retry.is_walk = false
    state.detour = nil
end

local function dist2d(ax, ay, bx, by)
    local dx = ax - bx
    local dy = ay - by
    return math.sqrt(dx * dx + dy * dy)
end

-- Autotranslate (groupId, messageId) → zone_id. Extracted from LSB
-- scripts/commands/zone.lua. Key = (groupId<<8) | messageId.
-- When the user types {East Ronfaure} the Ashita command arg contains a
-- 0xFD-delimited byte sequence with groupId at +3 and messageId at +4 from
-- the 0xFD marker (matching LSB's !zone command parser).
local AUTOTRANS_ZONE_MAP = {
    [0x14A9] = 1, [0x14AA] = 2, [0x1484] = 3, [0x1485] = 4, [0x148A] = 5,
    [0x148B] = 6, [0x1486] = 7, [0x1487] = 8, [0x1488] = 9, [0x1489] = 10,
    [0x148C] = 11, [0x148D] = 12, [0x148E] = 13, [0x14DC] = 13, [0x14AB] = 14,
    [0x149B] = 16, [0x149A] = 16, [0x149C] = 17, [0x149E] = 18, [0x149D] = 18,
    [0x149F] = 19, [0x14A0] = 20, [0x14A1] = 20, [0x14A2] = 21, [0x14A3] = 22,
    [0x14A4] = 22, [0x14A5] = 22, [0x14A6] = 22, [0x14A7] = 23, [0x14A8] = 23,
    [0x1490] = 24, [0x1491] = 25, [0x148F] = 26, [0x1493] = 27, [0x1494] = 28,
    [0x1496] = 29, [0x1495] = 29, [0x1498] = 30, [0x1497] = 30, [0x1499] = 31,
    [0x1492] = 32, [0x14AC] = 33, [0x14AD] = 34, [0x14AE] = 35, [0x14B0] = 36,
    [0x14B1] = 37, [0x14B2] = 38, [0x14B4] = 39, [0x14B5] = 40, [0x14B6] = 41,
    [0x14B7] = 42, [0x14AF] = 43, [0x14B8] = 44, [0x14B9] = 46, [0x14BA] = 47,
    [0x14BB] = 48, [0x14DB] = 50, [0x14BC] = 50, [0x14BD] = 51, [0x14BE] = 52,
    [0x14BF] = 53, [0x14C0] = 54, [0x14C1] = 55, [0x14C2] = 56, [0x14C3] = 57,
    [0x14C4] = 58, [0x14C5] = 59, [0x14C6] = 60, [0x14C7] = 61, [0x14C8] = 62,
    [0x14C9] = 63, [0x14CA] = 64, [0x14CB] = 65, [0x14CC] = 66, [0x14CD] = 67,
    [0x14CE] = 68, [0x14CF] = 69, [0x270F] = 70, [0x2710] = 71, [0x14DD] = 72,
    [0x14DE] = 73, [0x14DF] = 74, [0x14E0] = 75, [0x14E1] = 76, [0x14E2] = 77,
    [0x14DA] = 78, [0x14D0] = 79, [0x2711] = 80, [0x2713] = 81, [0x2715] = 82,
    [0x2723] = 83, [0x2717] = 84, [0x273E] = 85, [0x2740] = 85, [0x2719] = 86,
    [0x271C] = 87, [0x271E] = 88, [0x2720] = 89, [0x2725] = 90, [0x2727] = 91,
    [0x2742] = 92, [0x2722] = 93, [0x272B] = 94, [0x272D] = 95, [0x272F] = 96,
    [0x2732] = 97, [0x2734] = 98, [0x2744] = 99, [0x1411] = 100, [0x140F] = 101,
    [0x1451] = 102, [0x1460] = 103, [0x1401] = 104, [0x1402] = 105, [0x1464] = 106,
    [0x1463] = 107, [0x1469] = 108, [0x142B] = 109, [0x1407] = 110, [0x1424] = 111,
    [0x144D] = 112, [0x143D] = 113, [0x143E] = 114, [0x1418] = 115, [0x1427] = 116,
    [0x1417] = 117, [0x1416] = 118, [0x1420] = 119, [0x142E] = 120, [0x143F] = 121,
    [0x147D] = 122, [0x147C] = 122, [0x1440] = 123, [0x1441] = 124, [0x1442] = 125,
    [0x1408] = 126, [0x140A] = 127, [0x1443] = 128, [0x2731] = 129, [0x146F] = 130,
    [0x1482] = 134, [0x1483] = 135, [0x2746] = 136, [0x2748] = 137, [0x1465] = 139,
    [0x146C] = 140, [0x141F] = 141, [0x145E] = 142, [0x1466] = 143, [0x141A] = 144,
    [0x1421] = 145, [0x1419] = 146, [0x142A] = 147, [0x1428] = 148, [0x1468] = 149,
    [0x146D] = 150, [0x1423] = 151, [0x1404] = 152, [0x1444] = 153, [0x1437] = 154,
    [0x140C] = 157, [0x140B] = 158, [0x1436] = 159, [0x1435] = 160, [0x1426] = 161,
    [0x1425] = 161, [0x1450] = 162, [0x144F] = 162, [0x1439] = 163, [0x2736] = 164,
    [0x145D] = 165, [0x142D] = 166, [0x1432] = 167, [0x143B] = 168, [0x141D] = 169,
    [0x145C] = 170, [0x2729] = 171, [0x1461] = 172, [0x145B] = 173, [0x145A] = 174,
    [0x271A] = 175, [0x1459] = 176, [0x1471] = 177, [0x1470] = 177, [0x1472] = 178,
    [0x14B3] = 179, [0x1473] = 180, [0x1474] = 181, [0x140D] = 184, [0x147E] = 185,
    [0x147F] = 186, [0x1480] = 187, [0x1481] = 188, [0x146E] = 190, [0x1462] = 191,
    [0x141C] = 192, [0x1403] = 193, [0x141B] = 194, [0x146A] = 195, [0x1467] = 196,
    [0x142C] = 197, [0x1415] = 198, [0x1414] = 200, [0x1477] = 201, [0x1475] = 202,
    [0x147A] = 203, [0x144A] = 204, [0x1458] = 205, [0x146B] = 206, [0x1478] = 207,
    [0x1457] = 208, [0x1476] = 209, [0x1479] = 211, [0x1434] = 212, [0x1433] = 213,
    [0x144C] = 230, [0x1430] = 231, [0x1452] = 232, [0x1422] = 233, [0x1446] = 234,
    [0x1456] = 235, [0x143C] = 236, [0x142F] = 237, [0x143A] = 238, [0x1454] = 239,
    [0x1445] = 240, [0x1438] = 241, [0x1455] = 242, [0x1413] = 243, [0x144E] = 244,
    [0x140E] = 245, [0x1406] = 246, [0x1431] = 247, [0x145F] = 248, [0x141E] = 249,
    [0x1429] = 250, [0x147B] = 251, [0x1409] = 252, [0x274C] = 256, [0x274D] = 257,
    [0x274E] = 258, [0x274F] = 260, [0x2750] = 261, [0x2751] = 262, [0x2756] = 263,
    [0x2752] = 265, [0x2757] = 266, [0x275C] = 267, [0x2753] = 268, [0x2754] = 269,
    [0x2755] = 270, [0x2758] = 272, [0x275D] = 273, [0x2712] = 274, [0x275A] = 280,
    [0x2759] = 284, [0x275B] = 285, [0x271B] = 289, [0x271D] = 291,
}

local function decode_autotrans_zone(s)
    -- Find 0xFD (253) marker and read groupId/messageId (LSB format:
    -- groupId = s[atpos+3], messageId = s[atpos+4]).
    for i = 1, #s do
        if s:byte(i) == 253 and i + 4 <= #s then
            local group = s:byte(i + 3)
            local msg = s:byte(i + 4)
            if group and msg then
                local zid = AUTOTRANS_ZONE_MAP[group * 256 + msg]
                if zid then return zid end
            end
        end
    end
    return nil
end

local function resolve_zone_name(name)
    -- 1) Autotranslate: if the arg contains a 0xFD byte, decode via the
    --    (groupId, messageId) → zone_id map.
    local at_zid = decode_autotrans_zone(name)
    if at_zid then
        local rm = AshitaCore:GetResourceManager()
        local zname = rm:GetString('zones.names', at_zid)
        return at_zid, zname or tostring(at_zid)
    end
    -- 2) Strip any leftover autotrans/encoding bytes and fall back to
    --    plain-text substring matching.
    name = name:gsub('[\xEF\xFD].', '')
    local rm = AshitaCore:GetResourceManager()
    local name_lower = name:lower():match('^%s*(.-)%s*$') or name
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

local function load_instances(zone_id)
    state.instances = nil
    if not zone_id or zone_id == 0 then return end
    local data = read_json(ipc_path('instances/' .. zone_id .. '.json'))
    if data and data.instances then
        state.instances = data.instances
        msg(string.format('Loaded %d object instances for zone %d.', #data.instances, zone_id))
    end
end

local function load_dropoffs(zone_id)
    state.dropoffs = nil
    if not zone_id or zone_id == 0 then return end
    local data = read_json(ipc_path('dropoffs/' .. zone_id .. '.json'))
    if data and data.connections then
        -- Apply overrides: strip auto entries that appear in overrides.removed
        -- (matched on start XY within 1y), append overrides.added.
        local overrides = data.overrides or {}
        local removed = overrides.removed or {}
        local added = overrides.added or {}
        local function is_removed(c)
            local sx, sy = c.start[1], c.start[2]
            for _, r in ipairs(removed) do
                if math.abs(r.start[1] - sx) < 1.0 and math.abs(r.start[2] - sy) < 1.0 then
                    return true
                end
            end
            return false
        end
        local out = {}
        for _, c in ipairs(data.connections) do
            if not is_removed(c) then table.insert(out, c) end
        end
        for _, c in ipairs(added) do table.insert(out, c) end
        state.dropoffs = out
        msg(string.format('Loaded %d drop-off connections for zone %d.', #out, zone_id))
    end
end

-------------------------------------------------------------------------------
-- Navigation requests
-------------------------------------------------------------------------------

local function request_path(px, py, pz, tx, ty, obstacle, wider_radius)
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
    if wider_radius then
        data.wider_radius = true
    end
    write_json(ipc_path('nav_request.json'), data)
    state.pending_seq = seq
    state.goal = { x = tx, y = ty }
    local tag = wider_radius and ' [wide]' or ''
    msg(string.format('Requesting path to (%.0f, %.0f)... [#%d]%s', tx, ty, seq, tag))
end

-- Reset the retry state machine and arm it for a new top-level goal.
-- Called whenever the user issues a fresh /nav goto.
local function arm_retry(tx, ty, zone_id)
    state.retry.phase = 'normal'
    state.retry.target = { x = tx, y = ty, zone_id = zone_id }
    state.retry.walks = 0
    state.retry.is_walk = false
end

local function clear_retry()
    state.retry.phase = 'normal'
    state.retry.target = nil
    state.retry.walks = 0
    state.retry.is_walk = false
end

-- Append a failed-route record so we can review and repair these later.
-- One JSON-per-line file is easier than maintaining a single array.
local function log_failure(px, py, pz)
    local tgt = state.retry.target
    if not tgt then return end
    local entry = {
        time      = os.time(),
        zone_id   = state.zone_id,
        target    = { tgt.x, tgt.y, tgt.zone_id },
        last_pos  = { px, py, pz },
        attempts  = state.retry.walks,
    }
    -- Read existing list, append, write back.
    local path = ipc_path('nav_failures.json')
    local existing = read_json(path) or { failures = {} }
    if type(existing.failures) ~= 'table' then
        existing.failures = {}
    end
    existing.failures[#existing.failures + 1] = entry
    write_json(path, existing)
end

-- Called when the active goto cannot make progress (no_path returned, or
-- stuck-detection fired). Walks the retry state machine and either issues
-- the next attempt or gives up + logs the failure.
local function retry_or_fail(px, py, pz)
    local tgt = state.retry.target
    if not tgt or state.retry.phase == 'failed' then
        cancel_all()
        return
    end
    if state.retry.phase == 'normal' then
        -- First fallback: keep most of the original default-radius path
        -- intact, only re-plan the short stretch around the stuck point
        -- at the wider FALLBACK_AGENT_RADIUS. Pick a target ~5 waypoints
        -- past the stuck spot, save the in-progress waypoints, and
        -- request a wide-radius detour to that intermediate point. Once
        -- the detour arrives, on_destination_reached splices the saved
        -- waypoints back in and continues the original long route.
        local DETOUR_AHEAD = 5
        if state.waypoints and state.wp_idx and not state.detour then
            local resume_idx = math.min(state.wp_idx + DETOUR_AHEAD, #state.waypoints)
            local resume_wp = state.waypoints[resume_idx]
            if resume_wp then
                state.detour = {
                    saved_waypoints = state.waypoints,
                    resume_idx      = resume_idx,
                    original_target = tgt,
                }
                state.retry.target = { x = resume_wp[1], y = resume_wp[2],
                                       zone_id = state.zone_id }
                state.retry.phase = 'wide'
                state.retry.is_walk = false
                msg(string.format('Retry: wide-radius detour to wp %d/%d (%.0f, %.0f).',
                    resume_idx, #state.detour.saved_waypoints, resume_wp[1], resume_wp[2]))
                request_path(px, py, pz, resume_wp[1], resume_wp[2], nil, true)
                return
            end
        end
        -- Fallback: no waypoints to detour-splice. Re-plan the full path
        -- with the wider radius (legacy behaviour).
        state.retry.phase = 'wide'
        state.retry.is_walk = false
        msg('Retry: rerouting full path with wider agent radius.')
        request_path(px, py, pz, tgt.x, tgt.y, nil, true)
        return
    end
    -- Either 'wide' (the wider-radius attempt also failed) or 'walking'
    -- (we just finished a random walk and the resumed goal failed again).
    if state.retry.walks >= 3 then
        msg('Giving up after 3 random-walk retries; logged for repair.')
        log_failure(px, py, pz)
        state.retry.phase = 'failed'
        cancel_all()
        return
    end
    state.retry.walks = state.retry.walks + 1
    state.retry.phase = 'walking'
    state.retry.is_walk = true
    -- Pick a random nearby point in unobstructed direction; keep it short
    -- (~5y) so we don't strand the agent far from the original waypoint.
    local angle = math.random() * 2 * math.pi
    local dist  = 5.0
    local wx = px + math.cos(angle) * dist
    local wy = py + math.sin(angle) * dist
    msg(string.format('Retry %d/3: short walk to (%.1f, %.1f) then resume goal.',
        state.retry.walks, wx, wy))
    -- This sub-goto MUST NOT recursively trigger retries — request_path
    -- alone re-arms `state.goal`, but `state.retry.target` keeps the real
    -- destination so on completion we know where to go next.
    request_path(px, py, pz, wx, wy, nil, false)
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
            -- Summarize the route in chat regardless of preview vs. execute
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

            if state.preview_mode then
                -- Preview flow: render the first-segment path without moving.
                -- Keep preview_mode=true so the subsequent single-zone response
                -- is tagged "Preview:" and does not trigger auto-walk.
                local seg1 = data.route[1]
                state.route = nil
                msg(string.format('Preview route: %s (%d segments)',
                    table.concat(names, ' > '), #data.route))
                local px, py, pz = get_player_pos()
                if px and seg1.target then
                    request_path(px, py, pz, seg1.target[1], seg1.target[2])
                end
            else
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
                msg(string.format('Route: %s (%d segments)',
                    table.concat(names, ' > '), #data.route))
            end
        elseif data.status == 'already_there' then
            msg('Already in target zone.')
            state.route = nil
            state.preview_mode = false
        elseif data.status == 'no_route' then
            msg('No route found to destination zone.')
            state.route = nil
            state.preview_mode = false
        else
            msg('Cross-zone error: ' .. (data.message or data.status or 'unknown'))
            state.route = nil
            state.preview_mode = false
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
        local preview = state.preview_mode
        state.preview_mode = false
        state.moving = not preview
        state.stuck_count = 0
        state.check_frame = state.frame
        local tag = preview and 'Preview' or 'Path received'
        if data.status == 'partial' then
            msg(string.format('%s (partial): %d waypoints (ends %.0f yalms from target).',
                tag, #data.waypoints, data.end_dist or 0))
        else
            msg(string.format('%s: %d waypoints.', tag, #data.waypoints))
        end
    elseif data.status == 'partial' and state.search and (data.end_dist or 0) > 30 then
        msg(string.format('Search point unreachable (%.0fy short) - skipping.', data.end_dist or 0))
        stop_movement()
        state.pending_seq = nil
    elseif data.status == 'no_path' then
        if state.search then
            msg('Search point unreachable - skipping.')
        elseif state.retry.target then
            msg('No path found - escalating retry.')
            local px, py, pz = get_player_pos()
            if px then
                retry_or_fail(px, py, pz)
            else
                cancel_all()
            end
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
-- Obstacle avoidance (disabled - pauses on stuck for debugging)
-------------------------------------------------------------------------------

-------------------------------------------------------------------------------
-- Movement tick
-------------------------------------------------------------------------------

-- Called when the active waypoint chain is exhausted. Decides whether
-- this was a random-walk sub-goal (resume the real target), a wide-
-- radius detour (splice the saved long path back in), or the real
-- target (clear retry state, let search/route progress, or announce).
local function on_destination_reached(px, py, pz)
    state.goal = nil
    -- A random-walk sub-goal *always* takes priority — the agent
    -- reached the random helper waypoint, not the real target.
    -- Reissue the active goal (may be the detour resume point or the
    -- original target) so the route eventually completes.
    if state.retry.is_walk and state.retry.target then
        local tgt = state.retry.target
        state.retry.is_walk = false
        msg(string.format('Random walk done - resuming goal (%.0f, %.0f).',
            tgt.x, tgt.y))
        request_path(px, py, pz, tgt.x, tgt.y, nil, false)
        return
    end
    -- Detour complete: we just arrived at the 5-ahead resume point on
    -- the saved default-radius path. Splice the remainder back in and
    -- restore the original retry target. No new server round-trip
    -- needed — the original waypoints are already valid for this
    -- stretch onward.
    if state.detour then
        local d = state.detour
        state.detour = nil
        state.retry.target = d.original_target
        state.retry.phase = 'normal'
        state.retry.walks = 0
        local remaining = {}
        for i = d.resume_idx, #d.saved_waypoints do
            remaining[#remaining + 1] = d.saved_waypoints[i]
        end
        if #remaining > 0 then
            state.waypoints = remaining
            state.wp_idx = 1
            state.moving = true
            state.stuck_count = 0
            state.check_x = px
            state.check_y = py
            state.check_frame = state.frame
            msg(string.format('Detour complete - resuming original path (%d wps remaining).',
                #remaining))
            return
        end
        msg('Detour reached final goal.')
        clear_retry()
        return
    end
    if state.search or state.route then
        return  -- search/route loops handle their own progression
    end
    msg('Reached destination.')
    clear_retry()
end

local function movement_tick(px, py, pz)
    if not state.moving or not state.waypoints then return end

    local wp = state.waypoints[state.wp_idx]
    if not wp then
        stop_movement()
        on_destination_reached(px, py, pz)
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
        -- Forward progress on the active path means whatever escalation
        -- got us here worked — reset the retry chain so a *new* stuck
        -- later in the path starts fresh from 'normal' (wider radius
        -- first, walks 0/3) instead of immediately giving up.
        if state.retry.phase ~= 'normal' and not state.retry.is_walk then
            state.retry.phase = 'normal'
            state.retry.walks = 0
        end

        if state.wp_idx > #state.waypoints then
            stop_movement()
            on_destination_reached(px, py, pz)
            return
        end
        wp = state.waypoints[state.wp_idx]
        wx, wy = wp[1], wp[2]
    end

    drive_toward(px, py, wx, wy)

    -- Stuck detection: pause and report position for debugging
    if state.frame - state.check_frame >= STUCK_CHECK_FRAMES then
        local wp_progress = state.wp_idx - (state.check_wp_idx or state.wp_idx)
        local pos_progress = dist2d(px, py, state.check_x, state.check_y)
        state.check_x = px
        state.check_y = py
        state.check_wp_idx = state.wp_idx
        state.check_frame = state.frame

        if wp_progress < 3 and pos_progress < 3.0 then
            if state.retry.target and not state.search then
                msg(string.format('Stuck at (%.1f, %.1f) elev=%.1f wp %d/%d - escalating retry.',
                    px, py, pz, state.wp_idx, #state.waypoints))
                stop_movement()
                retry_or_fail(px, py, pz)
            else
                msg(string.format('Stuck at (%.1f, %.1f) elev=%.1f wp %d/%d - pausing (retry.target=%s).',
                    px, py, pz, state.wp_idx, #state.waypoints, tostring(state.retry.target ~= nil)))
                stop_movement()
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

        if not state.waypoints then return end

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

local function draw_object_boxes(px, py, pz)
    if not draw_ok then return end
    if not state.draw_objects then return end
    if not state.instances then return end

    local ok, err = pcall(function()
        local fg = imgui.GetForegroundDrawList()
        if not fg then return end
        local dev = d3d8.get_device()
        if not dev then return end
        local r1, view = dev:GetTransform(2)
        local r2, proj = dev:GetTransform(3)
        if r1 ~= 0 or r2 ~= 0 or not view or not proj then return end
        -- Snapshot view/proj matrices into plain Lua locals. Ashita's d3d8
        -- binding returns a live object whose fields can go nil between the
        -- validation pass and subsequent reads (observed when stepping
        -- through many instances on /nav objects on). Copying to locals
        -- up front ensures the projection math always sees numbers. If any
        -- field is nil at snapshot time, skip this frame — next frame will
        -- try again.
        local v11, v12, v13, v14 = view._11, view._12, view._13, view._14
        local v21, v22, v23, v24 = view._21, view._22, view._23, view._24
        local v31, v32, v33, v34 = view._31, view._32, view._33, view._34
        local v41, v42, v43, v44 = view._41, view._42, view._43, view._44
        local p11, p12, p14 = proj._11, proj._12, proj._14
        local p21, p22, p24 = proj._21, proj._22, proj._24
        local p31, p32, p34 = proj._31, proj._32, proj._34
        local p41, p42, p44 = proj._41, proj._42, proj._44
        if not (v11 and v12 and v13 and v14 and v21 and v22 and v23 and v24
                and v31 and v32 and v33 and v34 and v41 and v42 and v43 and v44
                and p11 and p12 and p14 and p21 and p22 and p24
                and p31 and p32 and p34 and p41 and p42 and p44) then
            return
        end
        local r3, vp = dev:GetViewport()
        if r3 ~= 0 or not vp or type(vp.Width) ~= 'number' or type(vp.Height) ~= 'number' then
            return
        end
        local vp_w, vp_h, vp_x, vp_y = vp.Width, vp.Height, vp.X or 0, vp.Y or 0

        local function project(gx, gy, gz)
            local wx, wy, wz = gx, gz, gy
            local vx = wx*v11 + wy*v21 + wz*v31 + v41
            local vy = wx*v12 + wy*v22 + wz*v32 + v42
            local vz = wx*v13 + wy*v23 + wz*v33 + v43
            local vw = wx*v14 + wy*v24 + wz*v34 + v44
            local cx = vx*p11 + vy*p21 + vz*p31 + vw*p41
            local cy = vx*p12 + vy*p22 + vz*p32 + vw*p42
            local cw = vx*p14 + vy*p24 + vz*p34 + vw*p44
            if cw <= 0.001 then return nil, nil end
            return (cx/cw * 0.5 + 0.5) * vp_w + vp_x,
                   (-cy/cw * 0.5 + 0.5) * vp_h + vp_y
        end

        local yellow = 0xFF00FFFF
        local cyan   = 0xFFFFFF00
        local range  = state.object_range or 50.0
        local range_sq = range * range

        for i = 1, #state.instances do
            local inst = state.instances[i]
            -- Only show objects that still carry collision in the navmesh.
            -- Decorations (single-submesh `_m`, `_col` suffix, `col_*` prefix)
            -- are tagged collides=false by the extractor and hidden here so
            -- /nav objects matches what the pathfinder actually sees.
            if inst.collides == false then
                goto continue
            end
            local mn, mx = inst.bbox_min, inst.bbox_max
            local cx = (mn[1] + mx[1]) * 0.5
            local cy = (mn[2] + mx[2]) * 0.5
            local dx, dy = cx - px, cy - py
            if dx*dx + dy*dy <= range_sq then
                local col = (inst.collision == 0) and yellow or cyan

                if inst.tris and inst.verts then
                    -- Mesh outline mode: draw each wall triangle's edges so
                    -- the user sees the actual collision shape (not an AABB).
                    local verts = inst.verts
                    local tris = inst.tris
                    local ps = {}
                    for k = 1, #verts do
                        local v = verts[k]
                        ps[k] = { project(v[1], v[2], v[3]) }
                    end
                    for t = 1, #tris do
                        local tri = tris[t]
                        local a = ps[tri[1] + 1]
                        local b = ps[tri[2] + 1]
                        local c = ps[tri[3] + 1]
                        if a and b and c then
                            if a[1] and b[1] then fg:AddLine({ a[1], a[2] }, { b[1], b[2] }, col, 1.0) end
                            if b[1] and c[1] then fg:AddLine({ b[1], b[2] }, { c[1], c[2] }, col, 1.0) end
                            if c[1] and a[1] then fg:AddLine({ c[1], c[2] }, { a[1], a[2] }, col, 1.0) end
                        end
                    end
                    -- Label at the first projected vertex
                    local lp = ps[1]
                    if lp and lp[1] and lp[2] then
                        local label = string.format('%s (%.0f,%.0f,%.0f)',
                            inst.name, cx, cy, (mn[3]+mx[3])*0.5)
                        fg:AddText({ lp[1] + 2, lp[2] - 12 }, col, label)
                    end
                else
                    -- Fallback: AABB wireframe + label for instances without mesh detail.
                    local x0, y0, z0 = mn[1], mn[2], mn[3]
                    local x1, y1, z1 = mx[1], mx[2], mx[3]
                    local c = {
                        { x0, y0, z0 }, { x1, y0, z0 }, { x1, y1, z0 }, { x0, y1, z0 },
                        { x0, y0, z1 }, { x1, y0, z1 }, { x1, y1, z1 }, { x0, y1, z1 },
                    }
                    local s = {}
                    for k = 1, 8 do
                        s[k] = { project(c[k][1], c[k][2], c[k][3]) }
                    end
                    local edges = {
                        {1,2},{2,3},{3,4},{4,1},
                        {5,6},{6,7},{7,8},{8,5},
                        {1,5},{2,6},{3,7},{4,8},
                    }
                    for _, e in ipairs(edges) do
                        local a, b = s[e[1]], s[e[2]]
                        if a[1] and b[1] then
                            fg:AddLine({ a[1], a[2] }, { b[1], b[2] }, col, 1.0)
                        end
                    end
                    local lp = s[8]
                    if lp and lp[1] and lp[2] then
                        local label = string.format('%s (%.0f,%.0f,%.0f)',
                            inst.name, cx, cy, (mn[3]+mx[3])*0.5)
                        fg:AddText({ lp[1] + 2, lp[2] - 12 }, col, label)
                    end
                end
            end
            ::continue::
        end
    end)

    if not ok then
        if not draw_err_msg or draw_err_msg ~= tostring(err) then
            draw_err_msg = tostring(err)
            msg('Object draw error: ' .. draw_err_msg)
        end
        draw_ok = false
    end
end

local function draw_dropoff_arrows(px, py, pz)
    if not draw_ok then return end
    if not state.draw_dropoffs then return end
    if not state.dropoffs then return end

    local ok, err = pcall(function()
        local fg = imgui.GetForegroundDrawList()
        if not fg then return end
        local dev = d3d8.get_device()
        if not dev then return end
        local r1, view = dev:GetTransform(2)
        local r2, proj = dev:GetTransform(3)
        if r1 ~= 0 or r2 ~= 0 or not view or not proj then return end
        local v11, v12, v13, v14 = view._11, view._12, view._13, view._14
        local v21, v22, v23, v24 = view._21, view._22, view._23, view._24
        local v31, v32, v33, v34 = view._31, view._32, view._33, view._34
        local v41, v42, v43, v44 = view._41, view._42, view._43, view._44
        local p11, p12, p14 = proj._11, proj._12, proj._14
        local p21, p22, p24 = proj._21, proj._22, proj._24
        local p31, p32, p34 = proj._31, proj._32, proj._34
        local p41, p42, p44 = proj._41, proj._42, proj._44
        if not (v11 and v12 and v13 and v14 and v21 and v22 and v23 and v24
                and v31 and v32 and v33 and v34 and v41 and v42 and v43 and v44
                and p11 and p12 and p14 and p21 and p22 and p24
                and p31 and p32 and p34 and p41 and p42 and p44) then
            return
        end
        local r3, vp = dev:GetViewport()
        if r3 ~= 0 or not vp or type(vp.Width) ~= 'number' or type(vp.Height) ~= 'number' then
            return
        end
        local vp_w, vp_h, vp_x, vp_y = vp.Width, vp.Height, vp.X or 0, vp.Y or 0

        local function project(gx, gy, gz)
            local wx, wy, wz = gx, gz, gy
            local vx = wx*v11 + wy*v21 + wz*v31 + v41
            local vy = wx*v12 + wy*v22 + wz*v32 + v42
            local vz = wx*v13 + wy*v23 + wz*v33 + v43
            local vw = wx*v14 + wy*v24 + wz*v34 + v44
            local cx = vx*p11 + vy*p21 + vz*p31 + vw*p41
            local cy = vx*p12 + vy*p22 + vz*p32 + vw*p42
            local cw = vx*p14 + vy*p24 + vz*p34 + vw*p44
            if cw <= 0.001 then return nil, nil end
            return (cx/cw * 0.5 + 0.5) * vp_w + vp_x,
                   (-cy/cw * 0.5 + 0.5) * vp_h + vp_y
        end

        -- Green line along the connection, red arrowhead at the landing end.
        local green = 0xFF00FF00
        local red   = 0xFF0000FF
        local range    = state.dropoff_range or 80.0
        local range_sq = range * range
        local shown = 0
        for i = 1, #state.dropoffs do
            local c = state.dropoffs[i]
            local s = c.start
            local e = c["end"]
            if s and e then
                local mx = (s[1] + e[1]) * 0.5
                local my = (s[2] + e[2]) * 0.5
                local dx, dy = mx - px, my - py
                if dx*dx + dy*dy <= range_sq then
                    local sx, sy = project(s[1], s[2], s[3])
                    local ex, ey = project(e[1], e[2], e[3])
                    if sx and ex then
                        fg:AddLine({ sx, sy }, { ex, ey }, green, 2.0)
                        -- Arrowhead: perpendicular short lines from ex,ey
                        -- toward sx,sy direction.
                        local vx, vy = sx - ex, sy - ey
                        local vl = math.sqrt(vx*vx + vy*vy)
                        if vl > 4.0 then
                            local k = 10.0 / vl
                            local ux, uy = vx * k, vy * k
                            -- perpendicular
                            local nxx, nyy = -uy * 0.5, ux * 0.5
                            fg:AddLine({ ex, ey }, { ex + ux + nxx, ey + uy + nyy }, red, 2.0)
                            fg:AddLine({ ex, ey }, { ex + ux - nxx, ey + uy - nyy }, red, 2.0)
                        end
                        -- Label with drop height at start.
                        local drop = s[3] - e[3]
                        if sx then
                            fg:AddText({ sx + 4, sy - 10 }, green,
                                       string.format('%.1fy', drop))
                        end
                        shown = shown + 1
                    end
                end
            end
        end
    end)

    if not ok then
        if not draw_err_msg or draw_err_msg ~= tostring(err) then
            draw_err_msg = tostring(err)
            msg('Drop-off draw error: ' .. draw_err_msg)
        end
        draw_ok = false
    end
end

-- Project a single world-space point (game coords: X east-west, Y north-south,
-- Z elevation) into screen space. Returns sx, sy or nil if off-screen/behind camera.
local function project_point(gx, gy, gz, view, proj, vp)
    local wx, wy, wz = gx, gz, gy
    local vx = wx*view._11 + wy*view._21 + wz*view._31 + view._41
    local vy = wx*view._12 + wy*view._22 + wz*view._32 + view._42
    local vz = wx*view._13 + wy*view._23 + wz*view._33 + view._43
    local vw = wx*view._14 + wy*view._24 + wz*view._34 + view._44
    local cx = vx*proj._11 + vy*proj._21 + vz*proj._31 + vw*proj._41
    local cy = vx*proj._12 + vy*proj._22 + vz*proj._32 + vw*proj._42
    local cw = vx*proj._14 + vy*proj._24 + vz*proj._34 + vw*proj._44
    if cw <= 0.001 then return nil, nil end
    return (cx/cw * 0.5 + 0.5) * vp.Width + vp.X,
           (-cy/cw * 0.5 + 0.5) * vp.Height + vp.Y
end

-------------------------------------------------------------------------------
-- Events
-------------------------------------------------------------------------------

ashita.events.register('load', 'nav_load', function()
    state.data_path = get_data_path()
    local f = io.open(ipc_path('nav_path.json'), 'w')
    if f then f:write('{}') f:close() end
    local zone_id = AshitaCore:GetMemoryManager():GetParty():GetMemberZone(0)
    if zone_id and zone_id ~= 0 then
        state.zone_id = zone_id
        load_entities(zone_id)
        load_instances(zone_id)
        load_dropoffs(zone_id)
    end
    msg('Loaded v' .. addon.version .. '. /nav goto <x> <y> [zone] | goto <zone> | find "name"')
end)

ashita.events.register('unload', 'nav_unload', function()
    save_entities(state.zone_id)
    cancel_all()
end)

ashita.events.register('packet_in', 'nav_packet', function(e)
    if e.id == 0x057 then
        local ok, weather = pcall(struct.unpack, 'H', e.data, 0x04 + 1)
        if ok and weather then
            state.weather_id = weather
            entities.set_weather(weather)
        end
    end
end)

ashita.events.register('d3d_present', 'nav_render', function()
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
            load_instances(zone_id)
            load_dropoffs(zone_id)
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

    -- Route tick: request path for current cross-zone segment. Arm the
    -- retry state machine on the segment's target so a stuck/no-path
    -- inside this segment escalates (wider radius, then random walks)
    -- instead of just pausing.
    if state.route and state.route.needs_path and not state.pending_seq then
        local r = state.route
        local seg = r.segments[r.seg_idx]
        if seg and seg.target then
            r.needs_path = false
            arm_retry(seg.target[1], seg.target[2], state.zone_id)
            request_path(px, py, pz, seg.target[1], seg.target[2])
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

    -- Recording: append sample when player moves > 0.3y
    if state.record_active and state.zone_id == state.record_zone then
        local lx, ly = state.record_last_x, state.record_last_y
        if lx == nil or ((px-lx)*(px-lx) + (py-ly)*(py-ly)) > 0.09 then
            state.record_path[#state.record_path + 1] = { px, py, pz, state.frame }
            state.record_last_x = px
            state.record_last_y = py
        end
    end

    -- Draw overlays
    draw_path()
    draw_object_boxes(px, py, pz)
    draw_dropoff_arrows(px, py, pz)
end)

ashita.events.register('command', 'nav_cmd', function(e)
    local args = e.command:args()
    if #args == 0 or args[1] ~= '/nav' then return end
    e.blocked = true

    local cmd = args[2] and args[2]:lower() or 'help'

    local function parse_name(args_tbl, start_idx)
        if #args_tbl < start_idx then return nil end
        local rest = table.concat(args_tbl, ' ', start_idx)
        return rest:match('^"(.-)"$') or rest:match("^'(.-)'$") or rest
    end

    if cmd == 'goto' then
        if #args < 3 then
            msg('Usage: /nav goto <x> <y> [zone] | goto <zone> | goto "entity"')
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
                    arm_retry(tx, ty, state.zone_id)
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
                arm_retry(tx, ty, state.zone_id)
                request_path(px, py, pz, tx, ty)
            end
        else
            local name = parse_name(args, 3)
            if not name or name == '' then
                msg('Usage: /nav goto <x> <y> [zone] | goto <zone> | goto "entity"')
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
                arm_retry(match.x, match.y, state.zone_id)
                request_path(px, py, pz, match.x, match.y)
            else
                msg(string.format('No zone or entity "%s" found.', name))
            end
        end

    elseif cmd == 'find' then
        local name = parse_name(args, 3)
        if not name or name == '' then
            msg('Usage: /nav find "entity name"')
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
            arm_retry(match.x, match.y, state.zone_id)
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

    elseif cmd == 'preview' then
        -- Same syntax as `goto` but loads waypoints for visualization only
        -- (does not move the character).
        if #args < 3 then
            msg('Usage: /nav preview <x> <y> [zone] | preview <zone> | preview "entity"')
            return
        end
        local tx = tonumber(args[3])
        local ty = tonumber(args[4])
        local px, py, pz = get_player_pos()
        if not px then
            msg('Cannot get player position.')
            return
        end
        state.preview_mode = true
        if tx and ty then
            if #args >= 5 then
                local zone_name = table.concat(args, ' ', 5)
                zone_name = zone_name:gsub('\xEF.', '')
                local zone_id, resolved_name = resolve_zone_name(zone_name)
                if not zone_id then
                    msg(string.format('Unknown zone: %s', zone_name))
                    state.preview_mode = false
                    return
                end
                if zone_id == state.zone_id then
                    cancel_all()
                    state.preview_mode = true
                    request_path(px, py, pz, tx, ty)
                else
                    -- Cross-zone preview: ask server to plan route, then
                    -- show the first segment (zone exit) as a preview path.
                    cancel_all()
                    state.last_seq = state.last_seq + 1
                    local seq = state.last_seq
                    write_json(ipc_path('nav_request.json'), {
                        action = 'cross_zone_goto',
                        zone_id = state.zone_id,
                        player = { px, py, pz },
                        target = { tx, ty, 0 },
                        target_zone = zone_id,
                        preview = true,
                        seq = seq,
                    })
                    state.pending_seq = seq
                    state.preview_mode = true
                    msg(string.format('Previewing route to (%.0f, %.0f) in %s...',
                        tx, ty, resolved_name))
                end
            else
                cancel_all()
                state.preview_mode = true
                request_path(px, py, pz, tx, ty)
            end
        else
            local name = parse_name(args, 3)
            if not name or name == '' then
                msg('Usage: /nav preview <x> <y> [zone] | preview <zone> | preview "entity"')
                state.preview_mode = false
                return
            end

            -- Try zone name first (same resolution order as /nav goto)
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
                    preview = true,
                    seq = seq,
                })
                state.pending_seq = seq
                state.preview_mode = true
                msg(string.format('Previewing route to %s...', resolved_name))
                return
            end

            -- Fall back to entity search
            local match = entities.find_by_name(state.zone_id, name, px, py)
            if match then
                cancel_all()
                state.preview_mode = true
                request_path(px, py, pz, match.x, match.y)
                msg(string.format('Previewing path to %s at (%.0f, %.0f).', match.name, match.x, match.y))
            else
                msg(string.format('No zone or entity "%s" found.', name))
                state.preview_mode = false
            end
        end

    elseif cmd == 'objects' then
        local sub = args[3] and args[3]:lower()
        if sub == 'on' then
            state.draw_objects = true
        elseif sub == 'off' then
            state.draw_objects = false
        elseif sub == 'range' and args[4] then
            local r = tonumber(args[4])
            if r and r > 0 then state.object_range = r end
        elseif sub == 'reload' then
            load_instances(state.zone_id)
        elseif sub == 'near' then
            local px, py, pz = get_player_pos()
            if not px or not state.instances then
                msg('No player pos or no instances loaded.')
                return
            end
            local ranked = {}
            for _, i in ipairs(state.instances) do
                local cx = (i.bbox_min[1] + i.bbox_max[1]) * 0.5
                local cy = (i.bbox_min[2] + i.bbox_max[2]) * 0.5
                local cz = (i.bbox_min[3] + i.bbox_max[3]) * 0.5
                local dx, dy = cx - px, cy - py
                table.insert(ranked, {
                    d = math.sqrt(dx*dx + dy*dy),
                    name = i.name, cx = cx, cy = cy, cz = cz,
                    col = i.collision,
                    mn = i.bbox_min, mx = i.bbox_max,
                })
            end
            table.sort(ranked, function(a, b) return a.d < b.d end)
            msg(string.format('Player (%.1f,%.1f,%.1f) — 5 nearest:', px, py, pz))
            for k = 1, math.min(5, #ranked) do
                local r = ranked[k]
                msg(string.format('  %.1fy  %s  col=%d  center=(%.1f,%.1f,%.1f)  bbox=(%.0f..%.0f, %.0f..%.0f, %.0f..%.0f)',
                    r.d, r.name, r.col, r.cx, r.cy, r.cz,
                    r.mn[1], r.mx[1], r.mn[2], r.mx[2], r.mn[3], r.mx[3]))
            end
        else
            state.draw_objects = not state.draw_objects
        end
        if sub ~= 'near' then
            local n = state.instances and #state.instances or 0
            msg(string.format('Object boxes: %s (zone=%d, %d instances, range=%.0f yalms)',
                state.draw_objects and 'ON' or 'OFF',
                state.zone_id or 0, n, state.object_range or 0))
        end

    elseif cmd == 'dropoffs' then
        local sub = args[3] and args[3]:lower()
        if sub == 'on' then
            state.draw_dropoffs = true
        elseif sub == 'off' then
            state.draw_dropoffs = false
        elseif sub == 'range' and args[4] then
            local r = tonumber(args[4])
            if r and r > 0 then state.dropoff_range = r end
        elseif sub == 'reload' then
            load_dropoffs(state.zone_id)
        elseif sub == 'near' then
            local px, py, pz = get_player_pos()
            if not px or not state.dropoffs then
                msg('No player pos or no drop-offs loaded.')
                return
            end
            local ranked = {}
            for _, c in ipairs(state.dropoffs) do
                local mx = (c.start[1] + c["end"][1]) * 0.5
                local my = (c.start[2] + c["end"][2]) * 0.5
                local dx, dy = mx - px, my - py
                table.insert(ranked, {
                    d = math.sqrt(dx*dx + dy*dy), c = c,
                })
            end
            table.sort(ranked, function(a, b) return a.d < b.d end)
            msg(string.format('Player (%.1f,%.1f,%.1f) — 5 nearest drop-offs:', px, py, pz))
            for k = 1, math.min(5, #ranked) do
                local r = ranked[k]
                local c = r.c
                msg(string.format('  %.1fy  start=(%.1f,%.1f,%.1f)  end=(%.1f,%.1f,%.1f)  drop=%.1fy',
                    r.d, c.start[1], c.start[2], c.start[3],
                    c["end"][1], c["end"][2], c["end"][3], c.start[3] - c["end"][3]))
            end
        else
            state.draw_dropoffs = not state.draw_dropoffs
        end
        if sub ~= 'near' then
            local n = state.dropoffs and #state.dropoffs or 0
            msg(string.format('Drop-offs: %s (zone=%d, %d connections, range=%.0f yalms)',
                state.draw_dropoffs and 'ON' or 'OFF',
                state.zone_id or 0, n, state.dropoff_range or 0))
        end

    elseif cmd == 'refresh' then
        local all = args[3] and args[3]:lower() == 'all'
        state.last_seq = state.last_seq + 1
        local req = {
            action  = 'clear_cache',
            seq     = state.last_seq,
        }
        if not all then
            req.zone_id = state.zone_id
        end
        write_json(ipc_path('nav_request.json'), req)
        if all then
            msg('Requesting server mesh refresh for ALL zones...')
        else
            msg(string.format('Requesting server mesh refresh for zone %d (use "refresh all" for all zones)...',
                state.zone_id or 0))
        end

    elseif cmd == 'record' then
        local sub = args[3] and args[3]:lower()
        if sub == 'start' then
            state.record_active = true
            state.record_path = {}
            state.record_zone = state.zone_id
            state.record_last_x = nil
            state.record_last_y = nil
            msg(string.format('Recording started for zone %d. Walk the route, then /nav record stop.',
                state.zone_id or 0))
        elseif sub == 'stop' then
            if state.record_path and #state.record_path > 0 then
                write_json(ipc_path('nav_record.json'), {
                    zone_id = state.record_zone,
                    points = state.record_path,
                })
                msg(string.format('Recording stopped: %d samples written to nav_record.json (zone %d).',
                    #state.record_path, state.record_zone or 0))
            else
                msg('Recording stopped: no samples captured.')
            end
            state.record_active = false
        else
            local n = state.record_path and #state.record_path or 0
            msg(string.format('Recording: %s (%d samples, zone %d). Subcommands: start | stop',
                state.record_active and 'ACTIVE' or 'off',
                n, state.record_zone or 0))
        end

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
        msg('Commands: goto <x> <y> [zone] | goto <zone> | goto "name" | find "name" | stop | pos | status | objects [on|off|range <y>|reload]')
    end
end)
