--[[
* mapper - entities.lua
* Entity tracking: records mob positions, wander ranges, time-of-day, and weather.
* Keyed by ServerId (unique per spawn point). Ignores claimed and dead entities.
--]]

local entities = {}

entities.zone_records = {}

local ENTITY_TYPES = { [2] = true, [5] = true }
local VANA_EPOCH = 1009810800
local current_weather = 0

local function get_vana_hour()
    local vana_secs = (os.time() - VANA_EPOCH) * 25
    return math.floor(vana_secs / 3600) % 24
end

local function should_track(entity)
    local ok_s, status = pcall(function() return entity.Status end)
    if ok_s and status ~= nil and status ~= 0 and status ~= 1 then return false end
    local ok_c, claim = pcall(function() return entity.ClaimServerId end)
    if ok_c and claim ~= nil and claim ~= 0 then return false end
    return true
end

function entities.set_weather(id)
    current_weather = id or 0
end

function entities.reset(zone_id)
    if zone_id then
        entities.zone_records[zone_id] = entities.zone_records[zone_id] or {}
    end
end

function entities.scan(zone_id)
    if zone_id == nil or zone_id == 0 then return end

    local hour = get_vana_hour()
    local weather = current_weather
    local records = entities.zone_records[zone_id]
    if records == nil then
        entities.zone_records[zone_id] = {}
        records = entities.zone_records[zone_id]
    end

    for index = 0, 2303 do
        local ok_e, e = pcall(GetEntity, index)
        if ok_e and e ~= nil then
        local ok_ptr, ptr = pcall(function() return e.ActorPointer end)
        if ok_ptr and ptr ~= nil and ptr ~= 0 then
        pcall(function()
            if e.ActorPointer == 0 then return end
            local name = e.Name
            if name == nil or name == '' then return end
            if not ENTITY_TYPES[e.Type] then return end
            if not should_track(e) then return end

            local sid = e.ServerId
            local x = e.Movement.LocalPosition.X
            local y = e.Movement.LocalPosition.Y
            local z = e.Movement.LocalPosition.Z

            local rec = records[sid]
            if rec == nil then
                rec = {
                    server_id = sid,
                    name = name,
                    type = e.Type,
                    center_x = x, center_y = y, center_z = z,
                    wander_radius = 0,
                    min_x = x, max_x = x,
                    min_y = y, max_y = y,
                    tod_hours = {},
                    weather_ids = {},
                    sightings = 0,
                }
                records[sid] = rec
            end

            if x < rec.min_x then rec.min_x = x end
            if x > rec.max_x then rec.max_x = x end
            if y < rec.min_y then rec.min_y = y end
            if y > rec.max_y then rec.max_y = y end

            rec.center_x = (rec.min_x + rec.max_x) / 2
            rec.center_y = (rec.min_y + rec.max_y) / 2
            rec.center_z = z

            local dx = x - rec.center_x
            local dy = y - rec.center_y
            local dist = math.sqrt(dx * dx + dy * dy)
            if dist > rec.wander_radius then
                rec.wander_radius = dist
            end

            if hour ~= nil then rec.tod_hours[hour] = true end
            if weather ~= nil then rec.weather_ids[weather] = true end

            rec.sightings = rec.sightings + 1
            rec.name = name
        end)
        end
        end
    end
end

function entities.find_by_name(zone_id, name, player_x, player_y)
    local records = entities.zone_records[zone_id]
    if not records then return nil end

    local name_lower = name:lower()
    local matches = {}

    for _, rec in pairs(records) do
        if rec.name:lower() == name_lower then
            matches[#matches + 1] = rec
        end
    end

    if #matches == 0 then
        for _, rec in pairs(records) do
            if rec.name:lower():find(name_lower, 1, true) then
                matches[#matches + 1] = rec
            end
        end
    end

    if #matches == 0 then return nil end

    local best = nil
    local best_dist = math.huge
    for _, rec in ipairs(matches) do
        local dx = rec.center_x - player_x
        local dy = rec.center_y - player_y
        local d = dx * dx + dy * dy
        if d < best_dist then
            best_dist = d
            best = rec
        end
    end

    return { x = best.center_x, y = best.center_y, z = best.center_z, name = best.name }
end

function entities.serialize(zone_id)
    local records = entities.zone_records[zone_id]
    if records == nil then return nil end
    local out = {}
    for sid, rec in pairs(records) do
        local tod = {}
        for h, _ in pairs(rec.tod_hours) do tod[#tod + 1] = h end
        table.sort(tod)
        local weather = {}
        for w, _ in pairs(rec.weather_ids) do weather[#weather + 1] = w end
        table.sort(weather)
        out[tostring(sid)] = {
            server_id = rec.server_id,
            name = rec.name,
            type = rec.type,
            center_x = rec.center_x,
            center_y = rec.center_y,
            center_z = rec.center_z,
            wander_radius = rec.wander_radius,
            min_x = rec.min_x, max_x = rec.max_x,
            min_y = rec.min_y, max_y = rec.max_y,
            tod_hours = tod,
            weather_ids = weather,
            sightings = rec.sightings,
        }
    end
    return out
end

function entities.load(data, zone_id)
    entities.zone_records[zone_id] = {}
    if data == nil then return end
    if data[1] ~= nil then return end

    local records = entities.zone_records[zone_id]
    for sid_str, rec in pairs(data) do
        local sid = tonumber(sid_str) or rec.server_id
        if sid then
            local tod_set = {}
            if rec.tod_hours then
                for _, h in ipairs(rec.tod_hours) do
                    tod_set[tonumber(h) or h] = true
                end
            end
            local weather_set = {}
            if rec.weather_ids then
                for _, w in ipairs(rec.weather_ids) do
                    weather_set[tonumber(w) or w] = true
                end
            end
            records[sid] = {
                server_id = sid,
                name = rec.name or '',
                type = rec.type or 2,
                center_x = rec.center_x or 0,
                center_y = rec.center_y or 0,
                center_z = rec.center_z or 0,
                wander_radius = rec.wander_radius or 0,
                min_x = rec.min_x or rec.center_x or 0,
                max_x = rec.max_x or rec.center_x or 0,
                min_y = rec.min_y or rec.center_y or 0,
                max_y = rec.max_y or rec.center_y or 0,
                tod_hours = tod_set,
                weather_ids = weather_set,
                sightings = rec.sightings or 0,
            }
        end
    end
end

function entities.count(zone_id)
    if zone_id == nil then
        local total = 0
        for _, records in pairs(entities.zone_records) do
            for _ in pairs(records) do total = total + 1 end
        end
        return total
    end
    local records = entities.zone_records[zone_id]
    if records == nil then return 0 end
    local n = 0
    for _ in pairs(records) do n = n + 1 end
    return n
end

return entities
