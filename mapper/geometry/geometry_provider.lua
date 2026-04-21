--[[
* nav - geometry/geometry_provider.lua
*
* Loads repository-stored extracted collision files for a zone.
*
* Expected file location (two search paths, first found wins):
*   <ashita_path>/config/addons/mapper/collision/<zone_id>.json
*   <addon_dir>/data/collision/<zone_id>.json
*
* JSON format produced by tools/extract_collision/extract_collision.py:
*   {
*     "zone_id": <int>,
*     "metadata": { "source": "MZB", "vertex_count": N, "triangle_count": N },
*     "bounds": { "min": [x,y,z], "max": [x,y,z] },
*     "vertices": [[x,y,z], ...],      -- unique vertices (2 dp)
*     "triangles": [[i0,i1,i2], ...]   -- index triples into vertices array
*   }
*
* Coordinate convention (Ashita LocalPosition space):
*   x = east/west   (LocalPosition.X)
*   y = north/south (LocalPosition.Y)  ← large horizontal axis
*   z = elevation   (LocalPosition.Z)  ← small vertical axis (up)
*
* At load time the indexed representation is expanded into a flat triangle
* array so the spatial_index and raycast modules need no format knowledge.
*
* This module does NOT parse DATs at runtime.
--]]

local json = require('json')

local provider = {}

local COLLISION_DIR = 'config/addons/mapper/collision/'

local function candidate_paths(zone_id, data_path)
    local base = data_path or ''
    if base ~= '' and base:sub(-1) ~= '/' and base:sub(-1) ~= '\\' then
        base = base .. '/'
    end
    return {
        string.format('%s%s%d.json', base, COLLISION_DIR, zone_id),
        string.format('data/collision/%d.json', zone_id),
    }
end

-------------------------------------------------------------------------------
-- Expand the indexed JSON format into a flat {v0,v1,v2} triangle list.
-- Each triangle: { v0={x,y,z}, v1={x,y,z}, v2={x,y,z} }
-- Raycast and spatial_index only ever read v0/v1/v2.
-------------------------------------------------------------------------------
-- The MZB extraction pipeline writes y = -MZB.Z, but Ashita's LocalPosition.Y
-- equals +MZB.Z (i.e. the negation of what is stored).  Negate y here so every
-- downstream module (spatial_index, raycast, explorer) can use LocalPosition
-- coordinates directly without any conversion.
local function expand_triangles(raw_verts, raw_tris)
    local out = {}
    for _, idx in ipairs(raw_tris) do
        local va = raw_verts[idx[1] + 1]  -- Lua 1-based; JSON indices are 0-based
        local vb = raw_verts[idx[2] + 1]
        local vc = raw_verts[idx[3] + 1]
        if va and vb and vc then
            out[#out + 1] = {
                v0 = { va[1], -va[2], va[3] },
                v1 = { vb[1], -vb[2], vb[3] },
                v2 = { vc[1], -vc[2], vc[3] },
            }
        end
    end
    return out
end

-------------------------------------------------------------------------------
-- provider.load(zone_id, data_path) → collision table or nil
-- Returns { zone_id, triangles, bounds, metadata } on success.
-------------------------------------------------------------------------------
function provider.load(zone_id, data_path)
    if zone_id == nil or zone_id == 0 then return nil end

    local content
    for _, path in ipairs(candidate_paths(zone_id, data_path)) do
        local f = io.open(path, 'r')
        if f then
            content = f:read('*all')
            f:close()
            break
        end
    end
    if content == nil then return nil end

    local ok, data = pcall(json.decode, content)
    if not ok or type(data) ~= 'table' then return nil end

    -- Handle indexed format (vertices + triangles as index arrays).
    if type(data.vertices) == 'table' and type(data.triangles) == 'table' then
        data.triangles = expand_triangles(data.vertices, data.triangles)
        data.vertices  = nil  -- free raw vertex list after expansion
    end

    if type(data.triangles) ~= 'table' then return nil end

    data.zone_id = zone_id

    -- Always recompute bounds from expanded triangles (JSON bounds may have
    -- un-negated Y from the extraction pipeline).
    data.bounds = provider.compute_bounds(data.triangles)

    return data
end

-------------------------------------------------------------------------------
-- Compute AABB over a flat {v0,v1,v2} triangle list.
-------------------------------------------------------------------------------
function provider.compute_bounds(triangles)
    local mn = {  math.huge,  math.huge,  math.huge }
    local mx = { -math.huge, -math.huge, -math.huge }
    for _, tri in ipairs(triangles) do
        for _, v in ipairs({ tri.v0, tri.v1, tri.v2 }) do
            for i = 1, 3 do
                if v[i] < mn[i] then mn[i] = v[i] end
                if v[i] > mx[i] then mx[i] = v[i] end
            end
        end
    end
    return { min = mn, max = mx }
end

return provider
