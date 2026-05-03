--[[
* goals - goals.lua
*
* In-game UI overlay showing what the agent is currently doing:
*   - User-set goal text (top of user_goal.txt)
*   - The persistent goal tree (composites + leaves with state)
*   - Currently active leaf and its dispatch state
*   - Recent agent actions (last N events from events.jsonl)
*
* No interaction surface beyond positioning - this is a read-only
* status panel so the human watching the screen knows what the agent
* is up to without tailing logs.
*
* Drag with SHIFT held to reposition. Position persists to
* persistent/<character>/goals_ui.json so the panel comes back where
* you left it on reload.
*
* Reads:
*   <ipc_base>/persistent/<character>/goals.json
*   <ipc_base>/events.jsonl                          (tailed)
*   <repo>/user_goal.txt                             (via env path)
--]]

addon.name    = 'goals'
addon.author  = 'xillm'
addon.version = '0.2'
addon.desc    = 'In-game goal-status overlay for agent_core'

require('common')
local json  = require('json')
local imgui = require('imgui')

-------------------------------------------------------------------------------
-- Config
-------------------------------------------------------------------------------

-- Refresh cadence for re-reading the goal tree + events.jsonl. Once per
-- second is plenty for a status overlay - goals don't churn faster
-- than the agent_core poll loop, and event-tail jitter at 1Hz looks
-- fine to a human reading.
local REFRESH_EVERY_FRAMES = 60
local RECENT_EVENT_COUNT   = 6

local PANEL_DEFAULT_X = 20
local PANEL_DEFAULT_Y = 80
local PANEL_W         = 380

-- Light text on dark grey, per the spec.
local COL_BG     = 0xE0202020   -- ARGB: ~88% opaque dark grey
local COL_BORDER = 0xFF404040
local COL_TITLE  = 0xFFFFE08C   -- soft amber
local COL_LABEL  = 0xFFAAAAAA
local COL_TEXT   = 0xFFE0E0E0
local COL_ACTIVE = 0xFF80FF80   -- soft green
local COL_FAIL   = 0xFFFF7070   -- soft red
local COL_DIM    = 0xFF808080

-------------------------------------------------------------------------------
-- Filesystem helpers
-------------------------------------------------------------------------------

local function get_data_path()
    local ok, p = pcall(function() return AshitaCore:GetInstallPath() end)
    if ok and p then
        if p:sub(-1) ~= '/' and p:sub(-1) ~= '\\' then p = p .. '/' end
        return p .. 'config/xillm/'
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

local function read_json(path)
    local f = io.open(path, 'r')
    if not f then return nil end
    local s = f:read('*all')
    f:close()
    if s == nil or s == '' then return nil end
    local ok, data = pcall(json.decode, s)
    if not ok then return nil end
    return data
end

local function write_json(path, data)
    local ok, encoded = pcall(json.encode, data)
    if not ok or encoded == nil then return end
    local f = io.open(path, 'w')
    if not f then return end
    f:write(encoded)
    f:close()
end

local function ensure_dir(path)
    pcall(function() ashita.fs.create_directory(path) end)
end

-- Tail the last N lines of a file. Used for events.jsonl which appends
-- one JSON-per-line. Reads the whole file (small for our purposes -
-- events.jsonl is rotated by agent_core, doesn't grow unbounded). For
-- huge files we'd seek-from-end; not worth the complexity here.
local function tail_lines(path, n)
    local f = io.open(path, 'r')
    if not f then return {} end
    local lines = {}
    for line in f:lines() do
        lines[#lines + 1] = line
        if #lines > n * 4 then
            -- Keep the buffer bounded - drop the front when far past N.
            -- 4x N gives us slack so we don't drop+regrow on every line.
            local keep = {}
            for i = #lines - n + 1, #lines do
                keep[#keep + 1] = lines[i]
            end
            lines = keep
        end
    end
    f:close()
    if #lines <= n then return lines end
    local out = {}
    for i = #lines - n + 1, #lines do
        out[#out + 1] = lines[i]
    end
    return out
end

-------------------------------------------------------------------------------
-- State
-------------------------------------------------------------------------------

local state = {
    frame = 0,
    last_character = nil,
    -- Cached snapshots for rendering. Refreshed every REFRESH_EVERY_FRAMES.
    user_goal_text = '',
    goals          = {nodes = {}, roots = {}},
    events         = {},
    active_leaf_id = nil,
    -- Window position + drag state.
    pos_x = PANEL_DEFAULT_X,
    pos_y = PANEL_DEFAULT_Y,
    drag_active = false,
    drag_anchor_x = 0,  -- mouse x at drag start
    drag_anchor_y = 0,
    drag_origin_x = 0,  -- panel x at drag start
    drag_origin_y = 0,
}

local function ui_config_path()
    local char = get_character_name()
    if char == nil then return nil end
    return get_data_path() .. 'persistent/' .. char .. '/goals_ui.json'
end

local function load_ui_config()
    local path = ui_config_path()
    if path == nil then return end
    local d = read_json(path)
    if type(d) == 'table' then
        if type(d.pos_x) == 'number' then state.pos_x = d.pos_x end
        if type(d.pos_y) == 'number' then state.pos_y = d.pos_y end
    end
end

local function save_ui_config()
    local path = ui_config_path()
    if path == nil then return end
    ensure_dir(get_data_path() .. 'persistent/' .. (get_character_name() or ''))
    write_json(path, {pos_x = state.pos_x, pos_y = state.pos_y})
end

-------------------------------------------------------------------------------
-- Data refresh
-------------------------------------------------------------------------------

-- The user_goal file lives in the repo, NOT under the Ashita install
-- dir. We don't have a clean way to reference it from in-game without
-- hard-coding. Easiest: read it via an env var the user sets, or skip
-- displaying user goal text entirely (the goal tree titles cover most
-- of the intent anyway).
local USER_GOAL_PATH = os.getenv('AGENT_USER_GOAL_FILE')
    or '/home/chris/workspace/xillm/user_goal.txt'

local function refresh_user_goal()
    local f = io.open(USER_GOAL_PATH, 'r')
    if not f then state.user_goal_text = ''; return end
    local s = f:read('*all') or ''
    f:close()
    -- Strip comment lines + leading whitespace; collapse to a single
    -- short summary fitting one line of the panel. Show first 3
    -- non-comment, non-empty lines.
    local kept = {}
    for line in s:gmatch('[^\r\n]+') do
        local stripped = line:match('^%s*(.-)%s*$')
        if stripped ~= '' and stripped:sub(1, 1) ~= '#' then
            kept[#kept + 1] = stripped
            if #kept >= 3 then break end
        end
    end
    state.user_goal_text = table.concat(kept, '\n')
end

local function refresh_goals()
    local char = state.last_character
    if char == nil then return end
    local data = read_json(get_data_path() .. 'persistent/' .. char .. '/goals.json')
    if type(data) == 'table' then
        state.goals = {
            nodes = data.nodes or {},
            roots = data.roots or {},
        }
    end
end

local function refresh_events()
    local char = state.last_character
    if char == nil then return end
    local lines = tail_lines(get_data_path() .. 'events.jsonl', RECENT_EVENT_COUNT * 3)
    -- Filter to events for this character + a curated set of types so
    -- the panel shows the agent's *decisions and actions*, not every
    -- internal heartbeat. Keep the most recent RECENT_EVENT_COUNT.
    local interesting = {
        ['goal_active']     = true,
        ['goal_completed']  = true,
        ['goal_failed']     = true,
        ['plan_generated']  = true,
        ['farm_kill']       = true,
        ['farm_started']    = true,
        ['farm_state']      = true,
        ['farm_death']      = true,
        ['llm_call']        = false,    -- noisy
        ['gambits_deployed']= false,    -- noisy
    }
    local out = {}
    for _, raw in ipairs(lines) do
        local ok, ev = pcall(json.decode, raw)
        if ok and type(ev) == 'table' then
            local evchar = ev.character or ev.char
            local evtype = ev.type or ev.type_  -- handle either field name
            if (evchar == nil or evchar == char) and interesting[evtype] then
                out[#out + 1] = ev
                if #out > RECENT_EVENT_COUNT then
                    table.remove(out, 1)
                end
            end
        end
    end
    state.events = out
end

local function find_active_leaf()
    -- Walk the goal tree DFS, mirror of agent_core's _find_active_leaf:
    -- first non-terminal leaf wins. We don't try to reproduce composite
    -- auto-completion here - agent_core owns that, our snapshot just
    -- reflects what the on-disk file says.
    local nodes = state.goals.nodes or {}
    local function visit(gid)
        local n = nodes[gid]
        if n == nil then return nil end
        local s = n.state or 'pending'
        if s == 'completed' or s == 'failed' or s == 'abandoned' then
            return nil
        end
        local sgs = n.subgoals
        if type(sgs) == 'table' and #sgs > 0 then
            for _, cid in ipairs(sgs) do
                local hit = visit(cid)
                if hit ~= nil then return hit end
            end
            return nil
        end
        return gid
    end
    for _, rid in ipairs(state.goals.roots or {}) do
        local hit = visit(rid)
        if hit ~= nil then return hit end
    end
    return nil
end

local function refresh_all()
    refresh_user_goal()
    refresh_goals()
    refresh_events()
    state.active_leaf_id = find_active_leaf()
end

-------------------------------------------------------------------------------
-- Drag handling (shift + left-click anywhere in the window)
-------------------------------------------------------------------------------

local function handle_drag()
    local io_t = imgui.GetIO()
    -- ImGuiIO has KeyShift; KeyMods on newer versions. Try both.
    local shift_down = false
    if io_t ~= nil then
        if io_t.KeyShift ~= nil then
            shift_down = io_t.KeyShift
        elseif io_t.KeyMods ~= nil then
            -- ImGuiKeyModFlags_Shift = 1 << 1 = 2
            shift_down = (io_t.KeyMods % 4) >= 2
        end
    end
    local mouse_down = imgui.IsMouseDown(0)
    local mx, my = imgui.GetMousePos()

    if state.drag_active then
        if not mouse_down or not shift_down then
            -- Drag released (or shift let go). Persist new position.
            state.drag_active = false
            save_ui_config()
        else
            state.pos_x = state.drag_origin_x + (mx - state.drag_anchor_x)
            state.pos_y = state.drag_origin_y + (my - state.drag_anchor_y)
        end
        return
    end

    if shift_down and mouse_down then
        -- Begin drag if cursor is inside the panel rect.
        if mx >= state.pos_x and mx <= state.pos_x + PANEL_W
           and my >= state.pos_y and my <= state.pos_y + 400 then
            state.drag_active = true
            state.drag_anchor_x = mx
            state.drag_anchor_y = my
            state.drag_origin_x = state.pos_x
            state.drag_origin_y = state.pos_y
        end
    end
end

-------------------------------------------------------------------------------
-- Rendering
-------------------------------------------------------------------------------

local function fmt_age(unix_ts)
    if unix_ts == nil then return '' end
    local d = os.time() - unix_ts
    if d < 60 then return string.format('%ds', d) end
    if d < 3600 then return string.format('%dm', math.floor(d / 60)) end
    return string.format('%dh', math.floor(d / 3600))
end

local function event_summary(ev)
    local t = ev.type or ev.type_ or '?'
    if t == 'goal_active' then
        return 'started ' .. (ev.goal_id or '?') .. ' "' .. (ev.title or '') .. '"'
    elseif t == 'goal_completed' then
        return 'completed ' .. (ev.goal_id or '?')
    elseif t == 'goal_failed' then
        return 'failed ' .. (ev.goal_id or '?')
    elseif t == 'plan_generated' then
        local tok = (ev.input_tokens or 0) + (ev.output_tokens or 0)
        return string.format('plan (%dt, %.1fs)',
            tok, ev.latency_s or 0)
    elseif t == 'farm_kill' then
        return 'kill #' .. (ev.kills or '?') .. ' ' .. (ev.target_name or '')
    elseif t == 'farm_started' then
        return 'farm started'
    elseif t == 'farm_death' then
        return 'died (' .. (ev.state_at_death or '?') .. ')'
    elseif t == 'farm_state' then
        return 'farm: ' .. (ev.state or '?')
    end
    return t
end

local function render_panel()
    -- Use a borderless ImGui window so we get docked text wrapping and
    -- font handling, but draw our own background via the underlying
    -- draw list for the colors we want. AlwaysAutoResize keeps the
    -- window snug to its content height.
    imgui.SetNextWindowPos({state.pos_x, state.pos_y})
    imgui.SetNextWindowSize({PANEL_W, 0})
    imgui.PushStyleColor(ImGuiCol_WindowBg, COL_BG)
    imgui.PushStyleColor(ImGuiCol_Border,    COL_BORDER)
    imgui.PushStyleVar(ImGuiStyleVar_WindowBorderSize, 1.0)
    imgui.PushStyleVar(ImGuiStyleVar_WindowPadding,    {10, 8})

    local flags = bit.bor(
        ImGuiWindowFlags_NoTitleBar,
        ImGuiWindowFlags_NoResize,
        ImGuiWindowFlags_NoMove,            -- we drive movement via shift+drag
        ImGuiWindowFlags_NoSavedSettings,
        ImGuiWindowFlags_AlwaysAutoResize,
        ImGuiWindowFlags_NoFocusOnAppearing,
        ImGuiWindowFlags_NoNav
    )

    if imgui.Begin('agent_goals_panel', true, flags) then
        -- Title bar (also indicates how to move the panel).
        imgui.PushStyleColor(ImGuiCol_Text, COL_TITLE)
        imgui.Text('AGENT GOALS')
        imgui.PopStyleColor()
        imgui.PushStyleColor(ImGuiCol_Text, COL_DIM)
        imgui.SameLine()
        imgui.Text('  (shift+drag to move)')
        imgui.PopStyleColor()
        imgui.Separator()

        -- USER GOAL section - what the human asked for, verbatim.
        if state.user_goal_text ~= '' then
            imgui.PushStyleColor(ImGuiCol_Text, COL_LABEL)
            imgui.Text('USER GOAL')
            imgui.PopStyleColor()
            imgui.PushStyleColor(ImGuiCol_Text, COL_TEXT)
            imgui.TextWrapped(state.user_goal_text)
            imgui.PopStyleColor()
            imgui.Spacing()
        end

        -- PLAN section - the goal tree as the agent_core sees it.
        local nodes = state.goals.nodes or {}
        local roots = state.goals.roots or {}
        imgui.PushStyleColor(ImGuiCol_Text, COL_LABEL)
        imgui.Text('PLAN')
        imgui.PopStyleColor()
        if #roots == 0 then
            imgui.PushStyleColor(ImGuiCol_Text, COL_DIM)
            imgui.Text('  (no active goal tree)')
            imgui.PopStyleColor()
        else
            local function render_node(gid, depth)
                local n = nodes[gid]
                if n == nil then return end
                local s = n.state or 'pending'
                local marker = '  '
                local color = COL_TEXT
                if gid == state.active_leaf_id then
                    marker = '> '; color = COL_ACTIVE
                elseif s == 'completed' then
                    marker = '+ '; color = COL_DIM
                elseif s == 'failed' or s == 'abandoned' then
                    marker = '! '; color = COL_FAIL
                elseif s == 'active' then
                    marker = '- '; color = COL_ACTIVE
                end
                local prefix = string.rep('  ', depth)
                local line = string.format('%s%s[%s] %s',
                    prefix, marker, s, n.title or n.id or '?')
                imgui.PushStyleColor(ImGuiCol_Text, color)
                imgui.Text(line)
                imgui.PopStyleColor()
                local sgs = n.subgoals
                if type(sgs) == 'table' then
                    for _, cid in ipairs(sgs) do
                        render_node(cid, depth + 1)
                    end
                end
            end
            for _, rid in ipairs(roots) do render_node(rid, 0) end
        end
        imgui.Spacing()

        -- RECENT section - last few notable events from events.jsonl.
        imgui.PushStyleColor(ImGuiCol_Text, COL_LABEL)
        imgui.Text('RECENT')
        imgui.PopStyleColor()
        if #state.events == 0 then
            imgui.PushStyleColor(ImGuiCol_Text, COL_DIM)
            imgui.Text('  (no recent events)')
            imgui.PopStyleColor()
        else
            -- Show newest first (events are tailed in chronological order).
            for i = #state.events, 1, -1 do
                local ev = state.events[i]
                local age = fmt_age(ev.ts)
                imgui.PushStyleColor(ImGuiCol_Text, COL_DIM)
                imgui.Text(string.format('%5s', age))
                imgui.PopStyleColor()
                imgui.SameLine()
                imgui.PushStyleColor(ImGuiCol_Text, COL_TEXT)
                imgui.Text('  ' .. event_summary(ev))
                imgui.PopStyleColor()
            end
        end
    end
    imgui.End()

    imgui.PopStyleVar(2)
    imgui.PopStyleColor(2)
end

-------------------------------------------------------------------------------
-- Ashita events
-------------------------------------------------------------------------------

ashita.events.register('load', 'goals_load', function()
    print('\30\06[goals]\30\01 Loaded v' .. addon.version
        .. ' - shift+drag to reposition the goal panel'
        .. ' (/goals refresh forces a replan)')
end)

-- /goals refresh - write a sentinel file that agent_core's
-- poll_replan_request picks up on its next tick and fires a fresh
-- planner.plan() call against the current user_goal.txt content.
-- Useful when the agent has wedged itself on a stale plan or you
-- (the player) just teleported and want it to re-evaluate.
ashita.events.register('command', 'goals_cmd', function(e)
    local args = e.command:args()
    if #args == 0 or args[1] ~= '/goals' then return end
    e.blocked = true

    local cmd = args[2] and args[2]:lower() or 'help'

    if cmd == 'refresh' then
        local path = get_data_path() .. 'replan_request.txt'
        local f = io.open(path, 'w')
        if f == nil then
            print('\30\06[goals]\30\01 refresh: could not open ' .. path)
            return
        end
        f:write(tostring(os.time()) .. '\n')
        f:close()
        print('\30\06[goals]\30\01 refresh: replan requested')
        return
    end

    print('\30\06[goals]\30\01 Usage: /goals refresh')
end)

ashita.events.register('d3d_present', 'goals_render', function()
    state.frame = state.frame + 1
    -- Pick up character name once we have one.
    if state.last_character == nil then
        state.last_character = get_character_name()
        if state.last_character ~= nil then
            load_ui_config()
            refresh_all()
        end
    end
    -- Refresh data on a slow cadence; cheap reads but no need to do
    -- them every frame.
    if state.frame % REFRESH_EVERY_FRAMES == 0 then
        pcall(refresh_all)
    end
    -- Drag state runs every frame so shift+drag is responsive.
    pcall(handle_drag)
    pcall(render_panel)
end)
