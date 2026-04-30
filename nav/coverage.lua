--[[
* nav - coverage.lua
*
* Per-zone coverage grid: tracks which 30y x 30y cells of a zone the
* agent has been in, when it was there (real time + Vana'diel hour),
* and how many times. Sits alongside entities.lua but answers a
* different question - "where have I been?" vs "what have I seen?"
*
* Cell key is "<cx>:<cy>" where cx = floor(x / CELL_SIZE_Y),
* cy = floor(y / CELL_SIZE_Y). 30y matches FFXI's typical mob-detection
* range, so a cell-visited == "every mob spawning in this cell would
* have been catalogued by entities.lua had it been there."
*
* Per-cell record:
*     {
*       cx, cy:                 grid coordinates (signed ints)
*       center_x, center_y:     world-space cell midpoint (for goto)
*       visit_count:            how many position samples landed here
*       first_visited_unix:     epoch of first-ever sample
*       last_visited_unix:      epoch of most-recent sample
*       vana_hours_seen:        { [hour]=count } over all visits
*       last_vana_hour:         hour of most-recent sample
*     }
*
* Persisted per-character to:
*     <ipc_base>/persistent/<character>/coverage/<zone>.json
*
* Module is read-write, called by nav.lua. Coverage data feeds the
* agent_core farming director's "go to nearest unvisited cell" logic.
--]]

local coverage = {}

-- 30y matches FFXI's typical mob-detection / claim range. Cells smaller
-- than this would over-fragment the grid; larger than this would let
-- mobs slip between cells unobserved.
local CELL_SIZE_Y = 30.0

-- {[zone_id] = { [cell_key] = record }}, in-memory copy. Persisted by
-- nav.lua's coverage_save(). Reset on /addon reload via load_zone().
coverage.zones = {}

local function cell_key(cx, cy)
    return tostring(cx) .. ':' .. tostring(cy)
end

local function cell_coords(x, y)
    local cx = math.floor(x / CELL_SIZE_Y)
    local cy = math.floor(y / CELL_SIZE_Y)
    return cx, cy
end

-- Note a position sample. Cheap - single dict lookup + counter bumps.
-- nav.lua calls this every status-publish tick (~5 Hz) so coverage
-- accumulates naturally as the agent moves.
function coverage.observe(zone_id, x, y, vana_hour)
    if zone_id == nil or zone_id == 0 then return end
    if x == nil or y == nil then return end
    local zone = coverage.zones[zone_id]
    if zone == nil then
        zone = {}
        coverage.zones[zone_id] = zone
    end
    local cx, cy = cell_coords(x, y)
    local key = cell_key(cx, cy)
    local rec = zone[key]
    local now_unix = os.time()
    if rec == nil then
        rec = {
            cx = cx, cy = cy,
            center_x = (cx + 0.5) * CELL_SIZE_Y,
            center_y = (cy + 0.5) * CELL_SIZE_Y,
            visit_count = 0,
            first_visited_unix = now_unix,
            last_visited_unix  = now_unix,
            vana_hours_seen = {},
            last_vana_hour  = vana_hour,
        }
        zone[key] = rec
    end
    rec.visit_count = rec.visit_count + 1
    rec.last_visited_unix = now_unix
    if vana_hour ~= nil then
        rec.last_vana_hour = vana_hour
        rec.vana_hours_seen[tostring(vana_hour)] =
            (rec.vana_hours_seen[tostring(vana_hour)] or 0) + 1
    end
end

-- Replace the in-memory zone record with what was loaded from disk.
-- nav.lua calls this on zone entry. Missing/empty data is fine - the
-- next observe() will lazily create the table.
function coverage.load(zone_id, data)
    if zone_id == nil or zone_id == 0 then return end
    if type(data) == 'table' and type(data.cells) == 'table' then
        coverage.zones[zone_id] = data.cells
    else
        coverage.zones[zone_id] = {}
    end
end

-- Serialise a zone for writing. Returns nil if nothing to save (empty
-- zone) so the caller can avoid producing useless empty files.
function coverage.serialize(zone_id)
    local zone = coverage.zones[zone_id]
    if zone == nil then return nil end
    -- Count entries to early-out on empty zones.
    local n = 0
    for _ in pairs(zone) do n = n + 1 end
    if n == 0 then return nil end
    return {
        zone_id   = zone_id,
        cell_size = CELL_SIZE_Y,
        cells     = zone,
        ts        = os.time(),
    }
end

function coverage.count(zone_id)
    local zone = coverage.zones[zone_id]
    if zone == nil then return 0 end
    local n = 0
    for _ in pairs(zone) do n = n + 1 end
    return n
end

function coverage.cell_size_y()
    return CELL_SIZE_Y
end

return coverage
