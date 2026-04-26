--[[
* combat - combat.lua
*
* Ashita v4 addon that publishes the combat slice of the agent's
* world state. Reads player + target + party data from the Ashita
* memory manager every N frames, atomically writes
* `<install>/config/addons/nav/state/<char>/combat.json` so the
* Python orchestrator (agent_core) can build a combat-aware world
* model. (The IPC directory still lives under nav/ for now —
* unification under config/addons/agent/ comes with Phase 1b.)
*
* Phase 3a scope: state publishing only — no gambit engine, no
* command channel. The engine arrives in Phase 3b alongside an AST
* the orchestrator deploys via commands/<char>/combat.json.
*
* Companion addon to `nav`. Keeping concerns separate so combat
* can be reloaded / disabled without touching nav.
--]]

addon.name    = 'combat'
addon.author  = 'xillm'
addon.version = '0.4'
addon.desc    = 'Combat state publisher (Tier 1 addon for the agent_core orchestrator)'

require('common')
local json = require('json')

-------------------------------------------------------------------------------
-- Config
-------------------------------------------------------------------------------

-- Publish state on every Nth frame. 60 fps / 6 = 10 Hz, matching the
-- Tier-1 budget in docs/agent-architecture.md (sub-second reactions on
-- frame-rate state changes; polled by Tier-2 at 5 Hz).
local PUBLISH_EVERY_FRAMES = 6

-- Single-character MVP: every state file lives under
-- <ipc_base>/state/<character>/combat.json. The character name comes
-- from Ashita's logged-in player. If no player is logged in (zoning,
-- main menu) we skip publishing rather than write to "state//".
local function get_data_path()
    local ok, p = pcall(function() return AshitaCore:GetInstallPath() end)
    if ok and p then
        if p:sub(-1) ~= '/' and p:sub(-1) ~= '\\' then p = p .. '/' end
        return p .. 'config/addons/nav/'
    end
    return ''
end

local function get_character_name()
    local ok, p = pcall(function()
        return AshitaCore:GetMemoryManager():GetParty():GetMemberName(0)
    end)
    if ok and p ~= nil and p ~= '' then return p end
    return nil
end

-------------------------------------------------------------------------------
-- State
-------------------------------------------------------------------------------

local state = {
    frame = 0,
    last_character = nil,
    -- Cache of the current state we're about to publish. Building it
    -- in-place every tick avoids GC pressure from rebuilding nested
    -- tables 10x/sec.
    payload = {},
}

local function msg(text)
    print('\30\06[combat]\30\01 ' .. text)
end

local function ensure_dir(path)
    -- ashita.fs.create_directory is recursive (creates intermediate
    -- parents) per Ashita's annotations. Available on every Ashita v4
    -- build we'd target.
    pcall(function() ashita.fs.create_directory(path) end)
end

-- Simple writer. We don't bother with temp+rename on Windows because
-- os.rename fails when the target exists (it's not POSIX atomic-replace),
-- and the readers (agent_core) already tolerate a partial JSON read by
-- pcalling json.load and skipping on failure. Combat state refreshes
-- every ~100ms anyway, so a single missed parse is invisible.
local last_encode_error = nil
local function write_json(path, data)
    local ok, encoded = pcall(json.encode, data)
    if not ok or encoded == nil then
        local err_str = tostring(encoded)
        if err_str ~= last_encode_error then
            msg('json.encode failed: ' .. err_str)
            last_encode_error = err_str
        end
        return
    end
    local f, err = io.open(path, 'w')
    if not f then
        msg('write fail: ' .. tostring(err))
        return
    end
    f:write(encoded)
    f:close()
end

-------------------------------------------------------------------------------
-- Game-state readers
-------------------------------------------------------------------------------

local function pcget(fn)
    local ok, v = pcall(fn)
    if ok then return v end
    return nil
end

local function read_self()
    local mm = AshitaCore:GetMemoryManager()
    local p = mm:GetPlayer()
    local party = mm:GetParty()
    local hp     = pcget(function() return party:GetMemberHP(0) end) or 0
    local hp_max = pcget(function() return party:GetMemberHPMax(0) end) or 0
    local mp     = pcget(function() return party:GetMemberMP(0) end) or 0
    local mp_max = pcget(function() return party:GetMemberMPMax(0) end) or 0
    local tp     = pcget(function() return party:GetMemberTP(0) end) or 0
    local main_job     = pcget(function() return p:GetMainJob() end) or 0
    local main_job_lvl = pcget(function() return p:GetMainJobLevel() end) or 0
    local sub_job      = pcget(function() return p:GetSubJob() end) or 0
    local sub_job_lvl  = pcget(function() return p:GetSubJobLevel() end) or 0
    local buffs = {}
    local raw_buffs = pcget(function() return p:GetBuffs() end)
    if type(raw_buffs) == 'table' then
        for i = 1, #raw_buffs do
            local id = raw_buffs[i]
            if id ~= nil and id ~= 255 and id ~= -1 then
                buffs[#buffs + 1] = id
            end
        end
    end
    return {
        hp     = hp,
        hp_max = hp_max,
        hp_pct = (hp_max > 0) and (100.0 * hp / hp_max) or 0,
        mp     = mp,
        mp_max = mp_max,
        mp_pct = (mp_max > 0) and (100.0 * mp / mp_max) or 0,
        tp     = tp,
        main_job     = main_job,
        main_job_lvl = main_job_lvl,
        sub_job      = sub_job,
        sub_job_lvl  = sub_job_lvl,
        buffs        = buffs,
    }
end

local function read_target()
    local mm = AshitaCore:GetMemoryManager()
    local target = mm:GetTarget()
    local idx = pcget(function() return target:GetTargetIndex(0) end) or 0
    if idx == 0 then return nil end
    local e = pcget(function() return GetEntity(idx) end)
    if e == nil then return nil end
    -- Defensive: every field is pcalled so a bad pointer doesn't crash
    -- the addon mid-publish.
    local hp_pct = pcget(function() return e.HPPercent end) or 0
    local name   = pcget(function() return e.Name end) or ''
    local sid    = pcget(function() return e.ServerId end) or 0
    local etype  = pcget(function() return e.Type end) or 0
    local status = pcget(function() return e.Status end) or 0
    local claim  = pcget(function() return e.ClaimServerId end) or 0
    local x      = pcget(function() return e.Movement.LocalPosition.X end) or 0
    local y      = pcget(function() return e.Movement.LocalPosition.Y end) or 0
    local z      = pcget(function() return e.Movement.LocalPosition.Z end) or 0
    local self_x = pcget(function() return GetPlayerEntity().Movement.LocalPosition.X end) or 0
    local self_y = pcget(function() return GetPlayerEntity().Movement.LocalPosition.Y end) or 0
    local dx, dy = x - self_x, y - self_y
    local dist = math.sqrt(dx * dx + dy * dy)
    local my_sid = pcget(function() return GetPlayerEntity().ServerId end) or 0
    return {
        server_id     = sid,
        name          = name,
        type          = etype,
        hp_pct        = hp_pct,
        x             = x,
        y             = y,
        z             = z,
        distance      = dist,
        status        = status,
        alive         = (status ~= 2 and status ~= 3),  -- 2/3 = dead/dying
        claimed       = (claim ~= 0),
        claimed_by_us = (claim ~= 0 and claim == my_sid),
    }
end

local function read_party()
    local party = AshitaCore:GetMemoryManager():GetParty()
    local out = {}
    for slot = 0, 5 do
        local active = pcget(function() return party:GetMemberIsActive(slot) end) or 0
        if active ~= 0 then
            local name   = pcget(function() return party:GetMemberName(slot) end) or ''
            local hp     = pcget(function() return party:GetMemberHP(slot) end) or 0
            local hp_max = pcget(function() return party:GetMemberHPMax(slot) end) or 0
            local mp     = pcget(function() return party:GetMemberMP(slot) end) or 0
            local mp_max = pcget(function() return party:GetMemberMPMax(slot) end) or 0
            local tp     = pcget(function() return party:GetMemberTP(slot) end) or 0
            local zone   = pcget(function() return party:GetMemberZone(slot) end) or 0
            out[#out + 1] = {
                slot   = slot,
                name   = name,
                hp     = hp,
                hp_max = hp_max,
                hp_pct = (hp_max > 0) and (100.0 * hp / hp_max) or 0,
                mp     = mp,
                mp_max = mp_max,
                mp_pct = (mp_max > 0) and (100.0 * mp / mp_max) or 0,
                tp     = tp,
                zone   = zone,
            }
        end
    end
    return out
end

-- The "engaged" state is exposed by the player struct: status==1 means
-- "engaged in combat" in the standard Ashita convention.
local function read_engaged()
    local p = AshitaCore:GetMemoryManager():GetPlayer()
    local status = pcget(function() return p:GetStatus() end)
    return status == 1
end

-------------------------------------------------------------------------------
-- Publish loop
-------------------------------------------------------------------------------

local function publish()
    local char = get_character_name()
    if char == nil or char == '' then return end
    if char ~= state.last_character then
        local dir = get_data_path() .. 'state/' .. char
        ensure_dir(dir)
        state.last_character = char
    end
    local payload = state.payload
    payload.ts        = os.time()
    payload.character = char
    payload.engaged   = read_engaged()
    payload.self      = read_self()
    payload.target    = read_target()
    payload.party     = read_party()
    local path = get_data_path() .. 'state/' .. char .. '/combat.json'
    write_json(path, payload)
end

-------------------------------------------------------------------------------
-- Ashita events
-------------------------------------------------------------------------------

ashita.events.register('load', 'combat_load', function()
    msg('Loaded v' .. addon.version .. ' — publishing combat state to state/<char>/combat.json')
end)

ashita.events.register('unload', 'combat_unload', function() end)

ashita.events.register('d3d_present', 'combat_render', function()
    state.frame = state.frame + 1
    if state.frame % PUBLISH_EVERY_FRAMES ~= 0 then return end
    pcall(publish)
end)
