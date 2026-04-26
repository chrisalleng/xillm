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
addon.version = '0.6'
addon.desc    = 'Combat state publisher + gambit engine (Tier 1 for agent_core)'

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
    -- Gambit engine state (Phase 3b).
    -- gambits: ordered list of validated gambits from the orchestrator
    -- gambits_seq: last commands seq we processed (idempotency)
    -- gambit_cooldowns: per-gambit-id last-fired timestamp
    gambits = nil,
    gambits_seq = 0,
    gambit_cooldowns = {},
    last_gambit_load_check = 0,
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
-- Gambit engine (Phase 3b)
-------------------------------------------------------------------------------

-- The orchestrator writes a gambit list here:
--    <ipc_base>/commands/<character>/combat.json
-- Schema documented in agent_core/gambits.py. The reader is idempotent:
-- it re-reads only when the file's seq is greater than the one we
-- already loaded.
-- One-shot diagnostic so we know which branch load_gambits exited on
-- when we expect gambits but they aren't firing. Each unique reason
-- prints once per addon-load, then quietens.
local _load_gambits_logged = {}
local function _load_diag(reason)
    if _load_gambits_logged[reason] then return end
    _load_gambits_logged[reason] = true
    msg('load_gambits: ' .. reason)
end

local function load_gambits()
    local char = state.last_character
    if char == nil then _load_diag('no character yet'); return end
    local path = get_data_path() .. 'commands/' .. char .. '/combat.json'
    local f = io.open(path, 'r')
    if not f then _load_diag('file missing: ' .. path); return end
    local body = f:read('*a')
    f:close()
    if body == nil or body == '' then _load_diag('file empty'); return end
    local ok, data = pcall(json.decode, body)
    if not ok or type(data) ~= 'table' then _load_diag('json decode failed'); return end
    local seq = data.seq or 0
    if seq <= state.gambits_seq then return end  -- silent: re-poll noise
    state.gambits = data.gambits or {}
    state.gambits_seq = seq
    state.gambit_cooldowns = {}
    msg(string.format('Loaded %d gambit(s) (seq %d)', #state.gambits, seq))
end

-- Walk a dotted path into the publish payload (`self.hp_pct`,
-- `target.distance`, `party.0.hp_pct`). Returns nil for any miss
-- (no field, intermediate nil, etc) — comparisons against nil all
-- evaluate false, which is the right semantics for "no target → don't
-- fire target-conditional gambits".
local function resolve_ref(payload, path)
    local cur = payload
    for segment in path:gmatch('[^.]+') do
        if cur == nil then return nil end
        local idx = tonumber(segment)
        if idx ~= nil then
            -- arrays in our JSON are 0-indexed in agent_core but Lua's
            -- json decoder gives 1-based tables. translate.
            cur = cur[idx + 1]
        else
            cur = cur[segment]
        end
    end
    return cur
end

-- Recursive expression evaluator. Operates on the already-published
-- payload (state.payload) so triggers see exactly the world the
-- orchestrator sees. Numeric comparisons treat nil as "always false."
local function eval_expr(expr, payload)
    if type(expr) ~= 'table' then return false end
    local op = expr.op
    if op == 'lit' then return expr.value end
    if op == 'ref' then return resolve_ref(payload, expr.path or '') end
    if op == 'and' then
        local args = expr.args or {}
        for i = 1, #args do
            if not eval_expr(args[i], payload) then return false end
        end
        return true
    end
    if op == 'or' then
        local args = expr.args or {}
        for i = 1, #args do
            if eval_expr(args[i], payload) then return true end
        end
        return false
    end
    if op == 'not' then
        return not eval_expr(expr.a, payload)
    end
    if op == 'in' then
        local needle = eval_expr(expr.needle, payload)
        local hay = eval_expr(expr.haystack, payload)
        if type(hay) ~= 'table' then return false end
        for i = 1, #hay do
            if hay[i] == needle then return true end
        end
        return false
    end
    -- comparison ops
    local a = eval_expr(expr.a, payload)
    local b = eval_expr(expr.b, payload)
    if a == nil or b == nil then return false end
    if op == 'lt'  then return a <  b end
    if op == 'lte' then return a <= b end
    if op == 'gt'  then return a >  b end
    if op == 'gte' then return a >= b end
    if op == 'eq'  then return a == b end
    if op == 'ne'  then return a ~= b end
    return false
end

-- Format an action node into a single Ashita /command line and queue it.
-- Targets default to <t> for magic/weaponskill, <me> for ability when
-- omitted — these match how a player would type the command by hand.
local function fire_action(action)
    if type(action) ~= 'table' then return end
    local cmd = nil
    local kind = action.kind
    if kind == 'ability' then
        local target = action.target or '<me>'
        cmd = string.format('/ja "%s" %s', action.name, target)
    elseif kind == 'magic' then
        local target = action.target or '<t>'
        cmd = string.format('/ma "%s" %s', action.name, target)
    elseif kind == 'weaponskill' then
        local target = action.target or '<t>'
        cmd = string.format('/ws "%s" %s', action.name, target)
    elseif kind == 'engage' then
        cmd = '/attack on'
    elseif kind == 'disengage' then
        cmd = '/attack off'
    elseif kind == 'raw' then
        cmd = action.command
    end
    if cmd ~= nil and cmd ~= '' then
        AshitaCore:GetChatManager():QueueCommand(1, cmd)
    end
end

-- One pass over the gambit list. Fires the first matching gambit whose
-- cooldown has elapsed, then stops (one action per tick is enough; the
-- next tick will re-evaluate after game state updates).
local function gambit_tick(payload)
    local gambits = state.gambits
    if gambits == nil or #gambits == 0 then return end
    local now = os.time()
    -- Sort-stable by priority (lower = higher priority). We don't sort
    -- in place every tick; the orchestrator should send them ordered.
    -- Instead we evaluate in list order, which the orchestrator can
    -- guarantee.
    for i = 1, #gambits do
        local g = gambits[i]
        if type(g) == 'table' then
            local cd = g.cooldown or 0
            local last = state.gambit_cooldowns[g.id] or 0
            if cd <= 0 or now - last >= cd then
                local ok, hit = pcall(eval_expr, g.trigger, payload)
                if ok and hit then
                    fire_action(g.action)
                    state.gambit_cooldowns[g.id] = now
                    msg(string.format('gambit fired: %s', g.id))
                    return
                end
            end
        end
    end
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
    -- Re-read the gambit command file once a second (every 10 ticks at
    -- our 10 Hz cadence). The orchestrator overwrites it atomically;
    -- if the seq hasn't bumped, load_gambits is a cheap no-op.
    if state.frame - state.last_gambit_load_check >= 60 then
        state.last_gambit_load_check = state.frame
        load_gambits()
    end
    -- Evaluate the gambit list against the just-published world state.
    gambit_tick(payload)
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
