--[[
* nav - core/map_coords.lua
*
* Convert between FFXI map grid labels (e.g. "H-10") and world coordinates.
* Per-zone offset/scale data from data/map_offsets.lua.
--]]

local offsets = require('data.map_offsets')

local map_coords = {}

function map_coords.world_to_grid(zone_id, x, y)
    local info = offsets[zone_id]
    if info == nil then return nil end
    if info.scale == 0 then return nil end

    local bx = info.offsetX + info.scale * x
    local by = info.offsetY - info.scale * y

    local col = math.floor(bx / 16)
    local row = math.floor(by / 16)

    if col < 0 then col = 0 elseif col > 15 then col = 15 end
    if row < 0 then row = 0 elseif row > 15 then row = 15 end

    return string.char(string.byte('A') + col) .. '-' .. (row + 1)
end

function map_coords.grid_to_world(zone_id, label)
    local info = offsets[zone_id]
    if info == nil then return nil, nil end
    if info.scale == 0 then return nil, nil end

    local letter, num = label:match('^([A-Pa-p])%-?(%d+)$')
    if letter == nil then return nil, nil end

    local col = string.byte(letter:upper()) - string.byte('A')
    local row = tonumber(num) - 1

    if col < 0 or col > 15 or row < 0 or row > 15 then return nil, nil end

    local bx = (col + 0.5) * 16
    local by = (row + 0.5) * 16

    local wx = (bx - info.offsetX) / info.scale
    local wy = (info.offsetY - by) / info.scale

    return wx, wy
end

return map_coords
