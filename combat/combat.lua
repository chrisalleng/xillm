--[[
* combat - combat.lua
*
* Ashita v4 addon that publishes the combat slice of the agent's
* world state. Reads player + target + party data from the Ashita
* memory manager every N frames, atomically writes
* `<install>/config/addons/nav/state/<char>/combat.json` so the
* Python orchestrator (agent_core) can build a combat-aware world
* model. (The IPC directory still lives under nav/ for now -
* unification under config/addons/agent/ comes with Phase 1b.)
*
* Phase 3a scope: state publishing only - no gambit engine, no
* command channel. The engine arrives in Phase 3b alongside an AST
* the orchestrator deploys via commands/<char>/combat.json.
*
* Companion addon to `nav`. Keeping concerns separate so combat
* can be reloaded / disabled without touching nav.
--]]

addon.name    = 'combat'
addon.author  = 'xillm'
addon.version = '0.21'
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
    -- Per-mob /check + widescan cache. Keyed by ServerId so it survives
    -- entity-index reuse across zone changes. Populated by listening
    -- for the same packets the third-party `checker` addon parses
    -- (0x0029 Message Basic, 0x00F4 Widescan). Each entry:
    --   { level: int|nil, check_type: int|nil, conditions: int|nil,
    --     checked_at_unix: int }
    -- Surfaced via nearby_enemies so the agent_core farming director
    -- can decide whether a mob is engageable before /ta'ing it.
    -- Wiped on zone change AND on level change - /check responses are
    -- relative to current level, so leveling up makes the cached
    -- check_types stale (a "tough" mob at lvl 2 may be "easy prey"
    -- at lvl 5). We track last_known_level to detect this.
    entity_checks = {},
    last_known_level = nil,
    -- Runtime non-mob blacklist - server_ids agent_core has confirmed
    -- aren't engageable (e.g. /check timed out without a valid response).
    -- Map sid -> os.time() when blacklisted; entries expire after
    -- NON_MOB_TTL_S so a misclassification (e.g. /check timed out
    -- because a menu was open, not because the entity is a non-mob)
    -- self-corrects without a manual reset. Populated via
    -- /combat blacklist <sid>. Wiped fully on zone change since
    -- server_ids recycle.
    non_mob_sids = {},
}

local NON_MOB_TTL_S = 300   -- 5 minutes; long enough to avoid re-trying
                            -- a true non-mob in a single farming run,
                            -- short enough that mistakes fade quickly.

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
    -- Current HP/MP/TP: IParty slot 0 is reliable for the local player.
    local hp = pcget(function() return party:GetMemberHP(0) end) or 0
    local mp = pcget(function() return party:GetMemberMP(0) end) or 0
    local tp = pcget(function() return party:GetMemberTP(0) end) or 0
    -- Max HP/MP: IParty.GetMemberHPMax(0) returns 0 when the player is
    -- solo (the party struct only populates slot-0 max values in an
    -- actual party - known Ashita quirk; xiui works around it via
    -- IPlayer.GetHPMax). Try IPlayer first, fall back to IParty, last
    -- resort back-compute from HPP. Always gives us a real number when
    -- the player is alive.
    local function read_max(getplayer, getparty, getpct, current)
        local v = pcget(getplayer) or 0
        if v > 0 then return v end
        v = pcget(getparty) or 0
        if v > 0 then return v end
        local pct = pcget(getpct) or 0
        if current > 0 and pct > 0 then
            return math.floor(current / (pct / 100.0) + 0.5)
        end
        return 0
    end
    local hp_max = read_max(
        function() return p:GetHPMax() end,
        function() return party:GetMemberHPMax(0) end,
        function() return p:GetHPPercent() end,
        hp)
    local mp_max = read_max(
        function() return p:GetMPMax() end,
        function() return party:GetMemberMPMax(0) end,
        function() return p:GetMPPercent() end,
        mp)
    local main_job     = pcget(function() return p:GetMainJob() end) or 0
    local main_job_lvl = pcget(function() return p:GetMainJobLevel() end) or 0
    local sub_job      = pcget(function() return p:GetSubJob() end) or 0
    local sub_job_lvl  = pcget(function() return p:GetSubJobLevel() end) or 0
    -- Status drives death detection AND rest-state detection. Standard
    -- FFXI codes: 0 = idle, 1 = engaged, 2 = dead-pending-menu,
    -- 3 = dead/zoning, 33 = healing, 38 = chocobo, 44 = riding.
    -- IPlayer:GetStatus() does NOT update for engaged/healing - same
    -- known issue we hit with read_engaged(). The player entity's
    -- Status field is the live source - that's what /heal toggle and
    -- engagement state actually flip. Fall back to IPlayer if the
    -- entity isn't readable yet (zoning).
    local status = pcget(function() return GetPlayerEntity().Status end)
    if status == nil then
        status = pcget(function() return p:GetStatus() end) or 0
    end
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
    -- Menu-open flag - true when an interactive menu (NPC dialog,
    -- inventory, map, options, etc.) is up. /check and /attack don't
    -- work while a menu is open, and /check responses get swallowed.
    -- agent_core uses this to suppress non-mob blacklisting when a
    -- /check timed out only because a menu was blocking input.
    local menu_open = pcget(function()
        return AshitaCore:GetMemoryManager():GetTarget():GetIsMenuOpen()
    end)
    menu_open = (menu_open ~= nil and menu_open ~= 0)
    return {
        hp     = hp,
        hp_max = hp_max,
        hp_pct = (hp_max > 0) and (100.0 * hp / hp_max) or 0,
        mp     = mp,
        mp_max = mp_max,
        mp_pct = (mp_max > 0) and (100.0 * mp / mp_max) or 0,
        tp     = tp,
        status       = status,
        main_job     = main_job,
        main_job_lvl = main_job_lvl,
        sub_job      = sub_job,
        sub_job_lvl  = sub_job_lvl,
        buffs        = buffs,
        menu_open    = menu_open,
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

-- "engaged in combat" status. The player struct's GetStatus() does
-- NOT flip to 1 when the player is mid-fight (it tracks a different
-- status concept). The entity-level Status field on the player's
-- entity is what every other Ashita addon reads for engage detection,
-- and what we need here so /attack on retries don't fire while we're
-- already swinging.
local function read_engaged()
    local e = pcget(function() return GetPlayerEntity() end)
    if e == nil then return false end
    local status = pcget(function() return e.Status end)
    return status == 1
end

-- Mob entity types (Ashita convention): 2 = mob, 5 = mob (variant).
-- Skipping types 0/1 (player/PC), 3 (NPC), 4 (door), etc.
local NEARBY_MOB_TYPES = { [2] = true, [5] = true }
-- Names that share a mob entity type but are NOT enemies (you can't
-- /attack them; /check returns nonsense). Filtered out of
-- nearby_enemies so agent_core's engage flow never picks them up.
-- Extensible: add new names as we discover them.
local NON_ENEMY_NAMES = {
    ['Treasure Casket']    = true,
    ['Goblin Footprint']   = true,
    ['Goblin Footprints']  = true,
    ['Glowing Footprint']  = true,
    ['Glowing Footprints'] = true,
    ['Mining Point']       = true,
    ['Logging Point']      = true,
    ['Harvesting Point']   = true,
    ['Excavation Point']   = true,
    ['Magicked Skull']     = true,
    ['???']                = true,
}
-- Runtime non-mob blacklist - server_ids that agent_core has learned
-- aren't real mobs (e.g. /check returned no valid response). Wiped on
-- zone change since server_ids reuse. Populated via the
-- /combat blacklist <sid> command from agent_core.
local function is_non_enemy(name)
    if name == nil or name == '' then return true end
    return NON_ENEMY_NAMES[name] == true
end
-- 50y is past FFXI's /ta range (~25y) but well within the client's
-- entity load range (~50-60y). Catching them at this distance lets
-- the engage_nearby acquire state see them BEFORE the agent walks
-- out of range - the mid-wander interrupt then aborts the wander
-- and the agent can approach via /ta + /follow. With the previous
-- 30y radius, the agent would often walk past mobs entirely without
-- the acquire state ever seeing them in a snapshot.
local NEARBY_RADIUS_Y  = 50.0
local NEARBY_MAX       = 16      -- cap so the JSON stays small

-- Scan every entity slot for unclaimed live mobs within NEARBY_RADIUS_Y
-- of the player. Sorted by distance ascending and capped at NEARBY_MAX.
-- Used by agent_core's engage_nearby goal to pick a /ta target by name
-- (FFXI has no `/ta <enemy>` placeholder - must use exact mob names).
local function read_nearby_enemies()
    local self_x = pcget(function() return GetPlayerEntity().Movement.LocalPosition.X end)
    local self_y = pcget(function() return GetPlayerEntity().Movement.LocalPosition.Y end)
    if self_x == nil or self_y == nil then return {} end
    local nearby = {}
    for index = 0, 2303 do
        local ok_e, e = pcall(GetEntity, index)
        if ok_e and e ~= nil then
            pcall(function()
                if e.ActorPointer == 0 then return end
                local etype = e.Type
                if not NEARBY_MOB_TYPES[etype] then return end
                if is_non_enemy(e.Name) then return end
                local sid = e.ServerId
                if sid then
                    local bl_at = state.non_mob_sids[sid]
                    if bl_at ~= nil then
                        if os.time() - bl_at < NON_MOB_TTL_S then
                            return
                        end
                        -- TTL elapsed; remove and let this entity
                        -- re-surface in nearby_enemies. agent_core
                        -- will re-/check it; if still a non-mob,
                        -- the timeout path re-blacklists.
                        state.non_mob_sids[sid] = nil
                    end
                end
                local status = e.Status
                -- 0 = idle, 1 = engaged. 2/3 = dead/dying. Anything
                -- outside [0, 1] means mob isn't a valid target.
                if status ~= 0 and status ~= 1 then return end
                local claim = e.ClaimServerId
                if claim ~= nil and claim ~= 0 then return end
                local hp_pct = e.HPPercent or 0
                if hp_pct == 0 then return end
                local x = e.Movement.LocalPosition.X
                local y = e.Movement.LocalPosition.Y
                local dx = x - self_x
                local dy = y - self_y
                local d = math.sqrt(dx * dx + dy * dy)
                if d > NEARBY_RADIUS_Y then return end
                local entry = {
                    name      = e.Name,
                    distance  = d,
                    hp_pct    = hp_pct,
                    server_id = e.ServerId,
                    status    = status,
                }
                -- Attach level / check_type if we've /check'd this
                -- specific spawn before. nil fields tell agent_core
                -- "level unknown - /check before deciding to engage."
                local cached = state.entity_checks[e.ServerId]
                if cached ~= nil then
                    entry.level         = cached.level
                    entry.check_type    = cached.check_type
                    entry.conditions    = cached.conditions
                    entry.checked_at_unix = cached.checked_at_unix
                end
                nearby[#nearby + 1] = entry
            end)
        end
    end
    table.sort(nearby, function(a, b) return a.distance < b.distance end)
    if #nearby > NEARBY_MAX then
        local trimmed = {}
        for i = 1, NEARBY_MAX do trimmed[i] = nearby[i] end
        nearby = trimmed
    end
    return nearby
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
-- (no field, intermediate nil, etc) - comparisons against nil all
-- evaluate false, which is the right semantics for "no target -> don't
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
-- omitted - these match how a player would type the command by hand.
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
    payload.engaged         = read_engaged()
    payload.self            = read_self()
    payload.target          = read_target()
    payload.party           = read_party()
    payload.nearby_enemies  = read_nearby_enemies()
    -- Level-change cache invalidation. /check returns level-relative
    -- difficulty; leveling up changes what's "tough" vs "easy prey",
    -- so any cached check_type from before the level-up is stale.
    -- The farming director on the agent_core side does the matching
    -- spawn_blacklist wipe via the same self_lvl signal in combat.json.
    local lvl_now = (payload.self or {}).main_job_lvl
    if lvl_now ~= nil and lvl_now ~= 0 then
        if state.last_known_level == nil then
            state.last_known_level = lvl_now
        elseif lvl_now ~= state.last_known_level then
            msg(string.format('level %d -> %d; clearing %d cached /check entries',
                state.last_known_level, lvl_now,
                (function() local n=0; for _ in pairs(state.entity_checks) do n=n+1 end; return n end)()))
            state.entity_checks = {}
            state.last_known_level = lvl_now
        end
    end
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
    msg('Loaded v' .. addon.version .. ' - publishing combat state to state/<char>/combat.json')
end)

ashita.events.register('unload', 'combat_unload', function() end)

-- Listen for /check responses (packet 0x0029, "Message Basic") and
-- widescan results (0x00F4) so we can attach mob levels + relative
-- difficulty to nearby_enemies entries. Same packet shape the
-- third-party `checker` addon parses; we don't depend on that addon
-- being loaded - we read the wire ourselves so the data is always
-- there for agent_core whether or not the player has checker on.
--
-- Zone change wipes entity_checks because entity indices reuse and
-- a stored check from another zone would be wildly wrong.
ashita.events.register('packet_in', 'combat_packet_in', function(e)
    -- Zone Enter/Leave -> drop any cached checks AND non-mob blacklist
    -- from the prior zone (server_ids recycle across zones).
    if e.id == 0x000A or e.id == 0x000B then
        state.entity_checks = {}
        state.non_mob_sids  = {}
        return
    end

    -- Message Basic (0x0029): /check response carries level + check
    -- type for the targeted entity. Field offsets match the checker
    -- addon's parsing. We don't block the packet - the checker addon
    -- (if loaded) still gets to render its chat message.
    if e.id == 0x0029 then
        -- Wrapped in pcall: malformed packets / version skews shouldn't
        -- crash the addon. Bail silently if struct.unpack errors.
        pcall(function()
            local lvl    = struct.unpack('l', e.data, 0x0C + 0x01)
            local ctype  = struct.unpack('L', e.data, 0x10 + 0x01)
            local m      = struct.unpack('H', e.data, 0x18 + 0x01)
            local target = struct.unpack('H', e.data, 0x16 + 0x01)
            -- Filter: only accept genuine /check messages. The same
            -- 0x0029 packet carries lots of unrelated chat-style
            -- messages; we identify /check by the m field being either
            -- the "impossible to gauge" code (0xF9) or one of the
            -- conditions table values (0xAA-0xB2 from the checker addon).
            local is_check = (m == 0xF9)
                or (m >= 0xAA and m <= 0xB2)
            if not is_check then return end
            local entity = GetEntity(target)
            if entity == nil then return end
            local sid = pcget(function() return entity.ServerId end)
            if sid == nil or sid == 0 then return end
            state.entity_checks[sid] = {
                level           = (lvl > 0) and lvl or nil,
                check_type      = ctype,  -- 0x40-0x47 incl. impossible
                conditions      = m,
                checked_at_unix = os.time(),
            }
        end)
        return
    end

    -- Widescan results (0x00F4): if the player is a job that has
    -- /widescan (RNG, BST), each entry returns level by entity index.
    -- We map index -> ServerId and store level (no check_type).
    if e.id == 0x00F4 then
        pcall(function()
            local idx = struct.unpack('H', e.data, 0x04 + 0x01)
            local lvl = struct.unpack('b', e.data, 0x06 + 0x01)
            if lvl == nil or lvl <= 0 then return end
            local entity = GetEntity(idx)
            if entity == nil then return end
            local sid = pcget(function() return entity.ServerId end)
            if sid == nil or sid == 0 then return end
            local cur = state.entity_checks[sid] or {}
            cur.level           = lvl
            cur.checked_at_unix = os.time()
            state.entity_checks[sid] = cur
        end)
        return
    end
end)

ashita.events.register('d3d_present', 'combat_render', function()
    state.frame = state.frame + 1
    if state.frame % PUBLISH_EVERY_FRAMES ~= 0 then return end
    pcall(publish)
end)

-- /combat target <serverid> - lock the main target slot onto the entity
-- with the given ServerId. Replaces `/ta "<name>"` for cases where the
-- agent has the server_id from nearby_enemies and needs to disambiguate
-- multiple same-name mobs (3 Wild Rabbits, etc). FFXI's `/ta` placeholder
-- only works on exact names and can't pick a specific spawn when several
-- share a name; ITarget:SetTarget(index, force) is the supported Ashita
-- API for programmatic lock-on. The `force` flag bypasses the client's
-- own targetability checks (range, LoS) - we want the lock to land
-- regardless so /follow + /attack sticks once we're in engage range.
ashita.events.register('command', 'combat_cmd', function(e)
    local args = e.command:args()
    if #args == 0 or args[1] ~= '/combat' then return end
    e.blocked = true

    local cmd = args[2] and args[2]:lower() or 'help'

    if cmd == 'target' then
        local sid = tonumber(args[3])
        if not sid then
            msg('Usage: /combat target <serverid>')
            return
        end
        for index = 0, 2303 do
            local ok, ent = pcall(GetEntity, index)
            if ok and ent ~= nil then
                local ent_sid = pcget(function() return ent.ServerId end)
                if ent_sid == sid then
                    local locked, err = pcall(function()
                        AshitaCore:GetMemoryManager():GetTarget():SetTarget(index, true)
                    end)
                    if not locked then
                        msg(string.format('SetTarget failed for sid %d (idx %d): %s',
                            sid, index, tostring(err)))
                    end
                    return
                end
            end
        end
        msg(string.format('No entity with ServerId %d in this zone.', sid))
        return
    end

    -- /combat blacklist <sid> - mark a server_id as a non-mob so it
    -- gets filtered out of nearby_enemies. agent_core fires this when
    -- a candidate's /check times out (real mobs respond within 1-2s).
    -- Wiped on zone change.
    if cmd == 'blacklist' then
        local sid = tonumber(args[3])
        if not sid then
            msg('Usage: /combat blacklist <serverid>')
            return
        end
        state.non_mob_sids[sid] = os.time()
        return
    end

    -- /combat stop - fully halt movement. /follow off only cancels the
    -- "walk to target" state but leaves the underlying IsAutoRunning
    -- flag set, so the player keeps running forward in their facing
    -- direction. We have to clear all three: autorun target, follow
    -- target, and the autorun flag itself.
    if cmd == 'stop' then
        pcall(function()
            local af = AshitaCore:GetMemoryManager():GetAutoFollow()
            af:SetTargetIndex(0)
            af:SetTargetServerId(0)
            af:SetFollowTargetIndex(0)
            af:SetFollowTargetServerId(0)
            af:SetIsAutoRunning(0)
        end)
        return
    end

    msg('Usage: /combat target <serverid> | /combat stop')
end)
