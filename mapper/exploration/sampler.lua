--[[
* nav - exploration/sampler.lua
*
* Generate candidate nearby sample positions for graph expansion.
* Samples are used by explorer.lua to find positions worth adding as nodes.
--]]

local sampler = {}

-- Default sampling parameters.
sampler.RADIAL_COUNT  = 8    -- number of evenly-spaced radial samples
sampler.RADIAL_RADIUS = 3.0  -- yalms

-------------------------------------------------------------------------------
-- Radial samples: evenly-spaced points at radius around (cx, cy).
-- Returns array of { x, y } tables.
-------------------------------------------------------------------------------
function sampler.radial(cx, cy, radius, count)
    radius = radius or sampler.RADIAL_RADIUS
    count  = count  or sampler.RADIAL_COUNT
    local result = {}
    local step   = 2 * math.pi / count
    for i = 0, count - 1 do
        local angle = i * step
        table.insert(result, {
            x = cx + radius * math.sin(angle),
            y = cy + radius * math.cos(angle),
        })
    end
    return result
end

-------------------------------------------------------------------------------
-- Directional samples: biased toward a given angle (radians).
-- Returns count points spread within half_arc radians of the direction,
-- all at the given radius.
-------------------------------------------------------------------------------
function sampler.directional(cx, cy, angle, radius, count, half_arc)
    radius   = radius   or sampler.RADIAL_RADIUS
    count    = count    or sampler.RADIAL_COUNT
    half_arc = half_arc or math.pi / 3  -- ±60° spread

    local result = {}
    if count == 1 then
        table.insert(result, {
            x = cx + radius * math.sin(angle),
            y = cy + radius * math.cos(angle),
        })
        return result
    end

    local step = (2 * half_arc) / (count - 1)
    for i = 0, count - 1 do
        local a = angle - half_arc + i * step
        table.insert(result, {
            x = cx + radius * math.sin(a),
            y = cy + radius * math.cos(a),
        })
    end
    return result
end

-------------------------------------------------------------------------------
-- Multi-ring samples: radial at several concentric radii.
-- Returns a flat array of { x, y }.
-------------------------------------------------------------------------------
function sampler.multi_ring(cx, cy, radii, count_per_ring)
    local result = {}
    for _, r in ipairs(radii) do
        local ring = sampler.radial(cx, cy, r, count_per_ring)
        for _, pt in ipairs(ring) do
            table.insert(result, pt)
        end
    end
    return result
end

return sampler
