--[[
* memscan - Phase 1 recon for FFXI menu memory layout
*
* Drops a small probe addon that dumps Ashita's 'menu' pointer chain
* and surrounding bytes to a JSON file we can read from outside the
* game. Used to discover the cursor index field and the select-trigger
* field for NPC dialog menus, so we can drive menus by direct memory
* writes (replacing the abandoned virtual-gamepad path).
*
* Commands (all output goes to /tmp/memscan.json so the orchestrator
* can read it without an in-game chat dump):
*
*   /memscan probe
*       Walks 'menu' pointer chain (mirroring autologin's get_menu_name),
*       dumps the menu name + first 256 bytes of the dereferenced menu
*       struct as hex. Use while an NPC dialog menu is open in-game.
*
*   /memscan ptrs
*       Queries Ashita's PointerManager:Get for a list of common names
*       (menu, inventory, target, party, player, ...) and reports which
*       ones exist + their current values.
*
*   /memscan watch <hex_offset> <bytes>
*       Reads <bytes> bytes starting at the menu base + <hex_offset>
*       and dumps them. Used after probe to compare the same offset
*       across cursor positions (move cursor 0->1->2 and re-run; the
*       byte that flipped 0/1/2 is the cursor field).
*
*   /memscan rawread <hex_addr> <bytes>
*       Absolute-address raw read for follow-up exploration once we
*       have specific addresses to verify.
*
* Output schema (/tmp/memscan.json overwrites on each command):
*   {
*     "command": "probe" | "ptrs" | "watch" | "rawread",
*     "ts": <unix>,
*     "menu_name": "menu    npc_xxx" | null,
*     "menu_base": "0xHEXADDR" | null,
*     "ptr_chain": ["0xHEX", "0xHEX", ...],   // each step of dereferencing
*     "bytes_at_base": "AA BB CC ...",        // first N bytes from menu_base
*     "ptrs": { "menu": "0xHEX" | null, ... }, // for /memscan ptrs
*     "raw": "AA BB CC ..."                    // for /memscan watch / rawread
*   }
--]]

addon.name      = 'memscan';
addon.author    = 'xillm';
addon.version   = '0.1';
addon.desc      = 'NPC dialog menu memory recon';
addon.commands  = { '/memscan' };

require('common');
local ffi = require('ffi');

-- Output file path. We can't use Linux /tmp directly because the
-- addon runs inside Wine, where /tmp resolves differently. Mirror
-- the pattern interact.lua uses: write under <install>/config/xillm/
-- which maps cleanly to a Linux path the orchestrator can read.
local function get_out_path()
    local ok, p = pcall(function() return AshitaCore:GetInstallPath() end)
    if not ok or not p then return 'config/xillm/state/memscan.json' end
    if p:sub(-1) ~= '/' and p:sub(-1) ~= '\\' then p = p .. '/' end
    return p .. 'config/xillm/state/memscan.json'
end

-- Common Ashita PointerManager names worth checking. Most of these
-- are guesses; we report nil for missing ones.
local KNOWN_POINTER_NAMES = {
    'menu', 'inventory', 'party', 'player', 'target',
    'recast', 'castbar', 'autofollow', 'entity', 'zonemap',
    'dialog', 'event', 'eventmenu', 'npcmenu', 'shop',
}

-- Minimal JSON writer (Ashita ships a json lib but doing this by
-- hand keeps this addon dep-free for easy reload).
local function json_escape(s)
    s = tostring(s):gsub('\\', '\\\\'):gsub('"', '\\"')
                   :gsub('\n', '\\n'):gsub('\r', '\\r'):gsub('\t', '\\t')
    return s
end

local function json_value(v)
    local t = type(v)
    if v == nil then return 'null' end
    if t == 'boolean' then return v and 'true' or 'false' end
    if t == 'number'  then return tostring(v) end
    if t == 'string'  then return '"' .. json_escape(v) .. '"' end
    if t == 'table' then
        -- Detect list vs object by integer-keyed contiguity.
        local is_list = true
        local n = 0
        for k, _ in pairs(v) do
            n = n + 1
            if type(k) ~= 'number' then is_list = false; break end
        end
        if is_list then
            if n == 0 then return '[]' end
            local out = {}
            for i = 1, n do out[#out+1] = json_value(v[i]) end
            return '[' .. table.concat(out, ',') .. ']'
        end
        local out = {}
        for k, val in pairs(v) do
            out[#out+1] = '"' .. json_escape(k) .. '":' .. json_value(val)
        end
        return '{' .. table.concat(out, ',') .. '}'
    end
    return 'null'
end

local function write_out(tbl)
    tbl.ts = os.time()
    local out_path = get_out_path()
    -- Ensure the parent dir exists (interact.lua's ensure_dir pattern).
    pcall(function()
        local dir = out_path:match('(.+)[/\\][^/\\]+$')
        if dir then ashita.fs.create_directory(dir) end
    end)
    local ok, f = pcall(io.open, out_path, 'w')
    if not ok or not f then
        print(('[memscan] could not open %s for write'):format(out_path))
        return
    end
    f:write(json_value(tbl))
    f:close()
    print(('[memscan] wrote %s'):format(out_path))
end

-- Safe memory readers; never crash the addon on a bad address.
local function safe_read_u32(addr)
    if not addr or addr == 0 then return nil end
    local ok, v = pcall(function() return ashita.memory.read_uint32(addr) end)
    return ok and v or nil
end

local function safe_read_u8(addr)
    if not addr or addr == 0 then return nil end
    local ok, v = pcall(function() return ashita.memory.read_uint8(addr) end)
    return ok and v or nil
end

local function safe_read_string(addr, len)
    if not addr or addr == 0 then return nil end
    local ok, v = pcall(function() return ashita.memory.read_string(addr, len) end)
    return ok and v or nil
end

local function hex_dump(addr, n)
    if not addr or addr == 0 then return nil end
    local out = {}
    for i = 0, n - 1 do
        local b = safe_read_u8(addr + i)
        out[#out+1] = b and ('%02X'):format(b) or '??'
    end
    return table.concat(out, ' ')
end

local function fmt_addr(a)
    if not a or a == 0 then return nil end
    return ('0x%08X'):format(a)
end

-- Walk the 'menu' pointer chain the way autologin does, capturing
-- each step so we can see exactly where the chain breaks if it does.
local function probe_menu()
    local result = {
        command   = 'probe',
        ptr_chain = {},
        menu_name = nil,
        menu_base = nil,
        bytes_at_base = nil,
        bytes_at_struct = nil,  -- after the second deref step
    }
    local p0 = AshitaCore:GetPointerManager():Get('menu')
    table.insert(result.ptr_chain, fmt_addr(p0))
    if not p0 or p0 == 0 then return result end

    -- Step 1: deref the named pointer to the menu manager pointer.
    local p1 = safe_read_u32(p0)
    table.insert(result.ptr_chain, fmt_addr(p1))
    if not p1 or p1 == 0 then return result end

    -- Step 2: deref to the active menu struct.
    local p2 = safe_read_u32(p1)
    table.insert(result.ptr_chain, fmt_addr(p2))
    if not p2 or p2 == 0 then return result end

    result.menu_base = fmt_addr(p2)
    result.bytes_at_struct = hex_dump(p2, 256)

    -- Step 3 (autologin's path): another deref + 0x04 -> name area.
    local name_block = safe_read_u32(p2 + 0x04)
    table.insert(result.ptr_chain, fmt_addr(name_block))
    if name_block and name_block ~= 0 then
        result.menu_name = safe_read_string(name_block + 0x46, 16)
        result.bytes_at_base = hex_dump(name_block, 256)
    end

    return result
end

local function probe_pointers()
    local result = {
        command = 'ptrs',
        ptrs    = {},
    }
    local pm = AshitaCore:GetPointerManager()
    for _, name in ipairs(KNOWN_POINTER_NAMES) do
        local p = pm:Get(name)
        result.ptrs[name] = (p and p ~= 0) and fmt_addr(p) or nil
    end
    return result
end

local function probe_watch(offset_str, n_str)
    local offset = tonumber(offset_str, 16) or 0
    local n      = tonumber(n_str) or 64
    local p0 = AshitaCore:GetPointerManager():Get('menu')
    local p1 = safe_read_u32(p0)
    local p2 = safe_read_u32(p1)
    local result = {
        command   = 'watch',
        offset    = offset_str,
        bytes     = n,
        menu_base = fmt_addr(p2),
        raw       = nil,
    }
    if p2 and p2 ~= 0 then
        result.raw = hex_dump(p2 + offset, n)
    end
    return result
end

local function probe_raw(addr_str, n_str)
    local addr = tonumber(addr_str, 16) or 0
    local n    = tonumber(n_str) or 64
    return {
        command = 'rawread',
        addr    = fmt_addr(addr),
        bytes   = n,
        raw     = (addr ~= 0) and hex_dump(addr, n) or nil,
    }
end

-- Snapshot held in addon memory between probes so /memscan diff
-- can report byte-level changes without writing two output files.
local last_snapshot = nil

-- Snapshot size in bytes. 2048 captures the menu struct plus any
-- adjacent sibling struct fields that the FFXI client allocates
-- contiguously. The 512-byte version missed cursor-read-side
-- fields living past offset 0x200.
local SNAPSHOT_SIZE = 2048

local function snapshot_struct()
    local p0 = AshitaCore:GetPointerManager():Get('menu')
    local p1 = safe_read_u32(p0)
    local p2 = safe_read_u32(p1)
    if not p2 or p2 == 0 then return nil, nil end
    local bytes = {}
    for i = 0, SNAPSHOT_SIZE - 1 do
        bytes[i+1] = safe_read_u8(p2 + i) or -1
    end
    return p2, bytes
end

-- Snapshot of the struct pointed to by menu_base + offset 0x08.
-- That pointer advanced by 0x70 between cursor positions in the
-- earlier diff, suggesting it walks an array of option records.
-- The cursor index OR the read-mode cursor may live in or near
-- whatever it points to.
local function snapshot_deep(deep_offset_str)
    local deep_offset = tonumber(deep_offset_str, 16) or 0x08
    local p0 = AshitaCore:GetPointerManager():Get('menu')
    local p1 = safe_read_u32(p0)
    local p2 = safe_read_u32(p1)
    if not p2 or p2 == 0 then return nil, nil, nil end
    local target = safe_read_u32(p2 + deep_offset)
    if not target or target == 0 then return p2, nil, nil end
    local bytes = {}
    for i = 0, SNAPSHOT_SIZE - 1 do
        bytes[i+1] = safe_read_u8(target + i) or -1
    end
    return p2, target, bytes
end

local last_deep_snapshot = nil
local last_deep_offset   = 0x08

-- Cheat-Engine-style narrowing scan. We scan a window centered on
-- menu_base for uint32 values matching a target. The first call
-- (find) populates a candidate list; subsequent calls (narrow)
-- retain only candidates whose current value matches the new target.
-- Repeat until the candidate set is small enough to inspect.
local SCAN_WIDTH_BYTES = 0x80000   -- 512 KB window (128K uint32 reads)
local find_candidates  = {}        -- list of addresses

local function safe_read_u32_naked(addr)
    -- Inner loop hot path; avoid pcall overhead by reading raw and
    -- letting any failure return nil naturally. Ashita's
    -- read_uint32 returns 0 on bad addresses (doesn't throw) within
    -- mapped regions; we only call this on a window we expect to
    -- be valid heap.
    return ashita.memory.read_uint32(addr)
end

local function probe_find(value_str, width_str)
    local value = tonumber(value_str) or 0
    local width = tonumber(width_str) or SCAN_WIDTH_BYTES
    local p0 = AshitaCore:GetPointerManager():Get('menu')
    local p1 = safe_read_u32(p0)
    local p2 = safe_read_u32(p1)
    if not p2 or p2 == 0 then
        return { command = 'find', error = 'menu chain broken' }
    end
    local lo = p2 - math.floor(width / 2)
    -- Word-align the lower bound.
    lo = lo - (lo % 4)
    local hi = lo + width
    find_candidates = {}
    for addr = lo, hi - 4, 4 do
        local ok, v = pcall(safe_read_u32_naked, addr)
        if ok and v == value then
            find_candidates[#find_candidates+1] = addr
        end
    end
    -- Sample first/last few addresses for inspection.
    local sample = {}
    for i = 1, math.min(#find_candidates, 8) do
        sample[i] = fmt_addr(find_candidates[i])
    end
    return {
        command = 'find',
        value   = value,
        scan_lo = fmt_addr(lo),
        scan_hi = fmt_addr(hi),
        scan_bytes = width,
        candidates = #find_candidates,
        sample  = sample,
    }
end

local function probe_narrow(value_str)
    local value = tonumber(value_str) or 0
    if #find_candidates == 0 then
        return { command = 'narrow', error = 'no prior /memscan find' }
    end
    local kept = {}
    for _, addr in ipairs(find_candidates) do
        local ok, v = pcall(safe_read_u32_naked, addr)
        if ok and v == value then
            kept[#kept+1] = addr
        end
    end
    find_candidates = kept
    local sample = {}
    for i = 1, math.min(#find_candidates, 16) do
        sample[i] = fmt_addr(find_candidates[i])
    end
    return {
        command = 'narrow',
        value   = value,
        candidates = #find_candidates,
        sample  = sample,
    }
end

-- Same as probe_find but byte-aligned (catches uint8 fields and
-- mis-aligned uint32 fields that the uint32 scan would skip). The
-- 4-byte-aligned scan only checks 25% of addresses; this checks all.
local function probe_find8(value_str, width_str)
    local value = tonumber(value_str) or 0
    local width = tonumber(width_str) or SCAN_WIDTH_BYTES
    local p0 = AshitaCore:GetPointerManager():Get('menu')
    local p1 = safe_read_u32(p0)
    local p2 = safe_read_u32(p1)
    if not p2 or p2 == 0 then
        return { command = 'find8', error = 'menu chain broken' }
    end
    local lo = p2 - math.floor(width / 2)
    local hi = lo + width
    find_candidates = {}
    for addr = lo, hi - 1 do
        local ok, v = pcall(function() return ashita.memory.read_uint8(addr) end)
        if ok and v == value then
            find_candidates[#find_candidates+1] = addr
        end
    end
    local sample = {}
    for i = 1, math.min(#find_candidates, 8) do
        sample[i] = fmt_addr(find_candidates[i])
    end
    return {
        command = 'find8',
        value   = value,
        scan_lo = fmt_addr(lo),
        scan_hi = fmt_addr(hi),
        scan_bytes = width,
        candidates = #find_candidates,
        sample  = sample,
    }
end

local function probe_narrow8(value_str)
    local value = tonumber(value_str) or 0
    if #find_candidates == 0 then
        return { command = 'narrow8', error = 'no prior find' }
    end
    local kept = {}
    for _, addr in ipairs(find_candidates) do
        local ok, v = pcall(function() return ashita.memory.read_uint8(addr) end)
        if ok and v == value then
            kept[#kept+1] = addr
        end
    end
    find_candidates = kept
    local sample = {}
    for i = 1, math.min(#find_candidates, 32) do
        sample[i] = fmt_addr(find_candidates[i])
    end
    return {
        command = 'narrow8',
        value   = value,
        candidates = #find_candidates,
        sample  = sample,
    }
end

local function probe_snap()
    local base, bytes = snapshot_struct()
    if not base then
        return { command = 'snap', error = 'menu chain broken' }
    end
    last_snapshot = { base = base, bytes = bytes }
    return {
        command = 'snap',
        menu_base = fmt_addr(base),
        captured_bytes = #bytes,
    }
end

-- Write a single byte to menu_base + offset. Used to verify that a
-- candidate offset is in fact the cursor index. Safe-ish: worst case
-- the menu closes or shows briefly-incorrect state; recoverable by
-- pressing cancel.
local function probe_poke(offset_str, value_str)
    local offset = tonumber(offset_str, 16) or 0
    local value  = tonumber(value_str) or 0
    local p0 = AshitaCore:GetPointerManager():Get('menu')
    local p1 = safe_read_u32(p0)
    local p2 = safe_read_u32(p1)
    if not p2 or p2 == 0 then
        return { command = 'poke', error = 'menu chain broken' }
    end
    local addr = p2 + offset
    local before = safe_read_u8(addr)
    local ok = pcall(function()
        ashita.memory.write_uint8(addr, value)
    end)
    local after = safe_read_u8(addr)
    return {
        command   = 'poke',
        menu_base = fmt_addr(p2),
        offset    = offset_str,
        addr      = fmt_addr(addr),
        before    = before and ('%02X'):format(before) or '??',
        wrote     = ('%02X'):format(value),
        after     = after and ('%02X'):format(after) or '??',
        ok        = ok,
    }
end

-- 32-bit (uint32) write at menu_base + offset. Used to test pointer
-- fields like the "highlighted option struct" pointer at 0x08.
local function probe_poke32(offset_str, value_str)
    local offset = tonumber(offset_str, 16) or 0
    local value  = tonumber(value_str, 16) or 0
    local p0 = AshitaCore:GetPointerManager():Get('menu')
    local p1 = safe_read_u32(p0)
    local p2 = safe_read_u32(p1)
    if not p2 or p2 == 0 then
        return { command = 'poke32', error = 'menu chain broken' }
    end
    local addr = p2 + offset
    local before = safe_read_u32(addr)
    local ok = pcall(function()
        ashita.memory.write_uint32(addr, value)
    end)
    local after = safe_read_u32(addr)
    return {
        command   = 'poke32',
        menu_base = fmt_addr(p2),
        offset    = offset_str,
        addr      = fmt_addr(addr),
        before    = fmt_addr(before),
        wrote     = ('0x%08X'):format(value),
        after     = fmt_addr(after),
        ok        = ok,
    }
end

local function probe_diff()
    if not last_snapshot then
        return { command = 'diff', error = 'no prior /memscan snap; run that first' }
    end
    local base, bytes = snapshot_struct()
    if not base then
        return { command = 'diff', error = 'menu chain broken' }
    end
    local diffs = {}
    if base ~= last_snapshot.base then
        -- Base address moved - menu was re-allocated. Report it.
        diffs[#diffs+1] = {
            offset = '(base)',
            from   = fmt_addr(last_snapshot.base),
            to     = fmt_addr(base),
        }
    else
        for i = 1, math.min(#bytes, #last_snapshot.bytes) do
            local a = last_snapshot.bytes[i]
            local b = bytes[i]
            if a ~= b then
                diffs[#diffs+1] = {
                    offset = ('0x%03X'):format(i - 1),
                    from   = a >= 0 and ('%02X'):format(a) or '??',
                    to     = b >= 0 and ('%02X'):format(b) or '??',
                }
            end
        end
    end
    -- Update snapshot so consecutive diffs are pairwise.
    last_snapshot = { base = base, bytes = bytes }
    return {
        command   = 'diff',
        menu_base = fmt_addr(base),
        n_diffs   = #diffs,
        diffs     = diffs,
    }
end

ashita.events.register('command', 'memscan_command', function(e)
    local args = {}
    for tok in e.command:gmatch('%S+') do
        table.insert(args, tok)
    end
    if args[1]:lower() ~= '/memscan' then return end
    e.blocked = true

    local sub = (args[2] or 'probe'):lower()
    local result
    if sub == 'probe' then
        result = probe_menu()
    elseif sub == 'ptrs' then
        result = probe_pointers()
    elseif sub == 'watch' then
        result = probe_watch(args[3] or '0', args[4] or '64')
    elseif sub == 'rawread' then
        result = probe_raw(args[3] or '0', args[4] or '64')
    elseif sub == 'snap' then
        result = probe_snap()
    elseif sub == 'diff' then
        result = probe_diff()
    elseif sub == 'poke' then
        result = probe_poke(args[3] or '0', args[4] or '0')
    elseif sub == 'poke32' then
        result = probe_poke32(args[3] or '0', args[4] or '0')
    elseif sub == 'find' then
        -- /memscan find <decimal_value> [width_bytes]
        result = probe_find(args[3] or '0', args[4])
    elseif sub == 'narrow' then
        -- /memscan narrow <decimal_value>
        result = probe_narrow(args[3] or '0')
    elseif sub == 'find8' then
        -- /memscan find8 <decimal_value> [width_bytes]
        result = probe_find8(args[3] or '0', args[4])
    elseif sub == 'narrow8' then
        -- /memscan narrow8 <decimal_value>
        result = probe_narrow8(args[3] or '0')
    elseif sub == 'snapraw' then
        -- /memscan snapraw <hex_addr> [bytes]  (default 2048)
        local addr = tonumber(args[3] or '0', 16) or 0
        local n    = tonumber(args[4]) or 2048
        local bytes = {}
        for i = 0, n - 1 do
            bytes[i+1] = safe_read_u8(addr + i) or -1
        end
        last_snapshot = { base = addr, bytes = bytes }
        result = {
            command = 'snapraw',
            addr    = fmt_addr(addr),
            captured_bytes = #bytes,
        }
    elseif sub == 'diffraw' then
        -- /memscan diffraw  - diffs vs last snapraw at the same address
        if not last_snapshot then
            result = { command = 'diffraw', error = 'no prior snapraw' }
        else
            local diffs = {}
            local addr = last_snapshot.base
            for i = 1, #last_snapshot.bytes do
                local a = last_snapshot.bytes[i]
                local b = safe_read_u8(addr + i - 1)
                if a ~= (b or -1) then
                    diffs[#diffs+1] = {
                        offset = ('0x%03X'):format(i - 1),
                        from = a >= 0 and ('%02X'):format(a) or '??',
                        to   = (b or -1) >= 0 and ('%02X'):format(b) or '??',
                    }
                end
            end
            -- update snapshot for chained diffs
            local new_bytes = {}
            for i = 0, #last_snapshot.bytes - 1 do
                new_bytes[i+1] = safe_read_u8(addr + i) or -1
            end
            last_snapshot.bytes = new_bytes
            result = {
                command = 'diffraw',
                addr = fmt_addr(addr),
                n_diffs = #diffs,
                diffs = diffs,
            }
        end
    elseif sub == 'pad' then
        -- /memscan pad <button_hex> [state] [device]
        -- Uses Ashita IInputManager.GetController():QueueButtonData(...)
        -- (DInput) by default, or 'xinput' for IXInput. The button
        -- code is the platform's button bit for the device.
        --
        -- Use case: after writing cursor state to memory, queue a
        -- benign button press to wake up FFXI's render so it picks
        -- up the new cursor value. The button code can be any -
        -- we don't care if FFXI processes it as a real input,
        -- just that input arrived.
        local btn   = tonumber(args[3] or '0', 16) or 0
        local state = tonumber(args[4] or '1') or 1
        local dev   = (args[5] or 'controller'):lower()
        local ok, err
        if dev == 'xinput' then
            ok, err = pcall(function()
                AshitaCore:GetInputManager():GetXInput():QueueButtonData(btn, state)
            end)
        else
            ok, err = pcall(function()
                AshitaCore:GetInputManager():GetController():QueueButtonData(btn, state)
            end)
        end
        result = {
            command = 'pad',
            button  = ('0x%04X'):format(btn),
            state   = state,
            device  = dev,
            ok      = ok,
            err     = ok and nil or tostring(err),
        }
    elseif sub == 'rawpoke32' then
        -- /memscan rawpoke32 <hex_addr> <decimal_value>
        local addr = tonumber(args[3] or '0', 16) or 0
        local value = tonumber(args[4] or '0') or 0
        local before = safe_read_u32(addr)
        local ok = pcall(function() ashita.memory.write_uint32(addr, value) end)
        local after = safe_read_u32(addr)
        result = {
            command = 'rawpoke32',
            addr    = fmt_addr(addr),
            before  = fmt_addr(before),
            wrote   = ('0x%08X'):format(value),
            after   = fmt_addr(after),
            ok      = ok,
        }
    elseif sub == 'dump' then
        -- /memscan dump <hex_addr> <decimal_bytes> <filename>
        -- Dumps `bytes` bytes starting at `addr` to <install>/config/xillm/state/<filename>.
        -- Used to dump pol.exe's unpacked code region for offline RE
        -- (FFXiMain.dll's .text section is zeros in the file - real
        -- code unpacks at runtime into the RWX POL1 section).
        local addr = tonumber(args[3] or '0', 16) or 0
        local n    = tonumber(args[4]) or 0
        local fname = args[5] or 'memdump.bin'
        if addr == 0 or n <= 0 then
            result = { command = 'dump', error = 'usage: dump <hex_addr> <bytes> <filename>' }
        else
            -- Build path under <install>/config/xillm/state/<fname>
            local out_path
            local ok_ip, ip = pcall(function() return AshitaCore:GetInstallPath() end)
            if ok_ip and ip then
                if ip:sub(-1) ~= '/' and ip:sub(-1) ~= '\\' then ip = ip .. '/' end
                out_path = ip .. 'config/xillm/state/' .. fname
            else
                out_path = 'config/xillm/state/' .. fname
            end
            local f, err = io.open(out_path, 'wb')
            if not f then
                result = { command = 'dump', error = 'open failed: ' .. tostring(err), out = out_path }
            else
                -- Write 4KB chunks at a time. Use uint32 reads for
                -- 4x speedup vs uint8. Fail gracefully on bad pages.
                local bytes_written = 0
                local chunk = {}
                local i = 0
                while i < n do
                    local v = safe_read_u8(addr + i)
                    if v == nil then v = 0 end
                    chunk[#chunk+1] = string.char(v)
                    if #chunk >= 4096 or i == n - 1 then
                        f:write(table.concat(chunk))
                        bytes_written = bytes_written + #chunk
                        chunk = {}
                    end
                    i = i + 1
                end
                f:close()
                result = {
                    command = 'dump',
                    addr    = fmt_addr(addr),
                    bytes   = n,
                    written = bytes_written,
                    out     = out_path,
                }
            end
        end
    elseif sub == 'snapdeep' then
        -- Snapshot the struct pointed to by menu_base + <offset>.
        -- Default deep_offset is 0x08 (the pointer that advanced by
        -- 0x70 in our cursor-move diff).
        last_deep_offset = tonumber(args[3] or '8', 16) or 0x08
        local base, target, bytes = snapshot_deep(args[3] or '8')
        if not base then
            result = { command = 'snapdeep', error = 'menu chain broken' }
        elseif not target then
            result = {
                command = 'snapdeep',
                menu_base = fmt_addr(base),
                deep_offset = ('0x%03X'):format(last_deep_offset),
                error = 'pointer at deep_offset is null',
            }
        else
            last_deep_snapshot = { base = base, target = target, bytes = bytes }
            result = {
                command = 'snapdeep',
                menu_base = fmt_addr(base),
                deep_offset = ('0x%03X'):format(last_deep_offset),
                target = fmt_addr(target),
                captured_bytes = #bytes,
            }
        end
    elseif sub == 'diffdeep' then
        if not last_deep_snapshot then
            result = { command = 'diffdeep', error = 'no prior snapdeep' }
        else
            local base, target, bytes = snapshot_deep(('%X'):format(last_deep_offset))
            if not base or not target then
                result = { command = 'diffdeep', error = 'menu chain broken' }
            else
                local diffs = {}
                if target ~= last_deep_snapshot.target then
                    diffs[#diffs+1] = {
                        offset = '(target_ptr)',
                        from = fmt_addr(last_deep_snapshot.target),
                        to   = fmt_addr(target),
                    }
                else
                    for i = 1, math.min(#bytes, #last_deep_snapshot.bytes) do
                        local a = last_deep_snapshot.bytes[i]
                        local b = bytes[i]
                        if a ~= b then
                            diffs[#diffs+1] = {
                                offset = ('0x%03X'):format(i - 1),
                                from   = a >= 0 and ('%02X'):format(a) or '??',
                                to     = b >= 0 and ('%02X'):format(b) or '??',
                            }
                        end
                    end
                end
                last_deep_snapshot = { base = base, target = target, bytes = bytes }
                result = {
                    command = 'diffdeep',
                    menu_base = fmt_addr(base),
                    target = fmt_addr(target),
                    n_diffs = #diffs,
                    diffs = diffs,
                }
            end
        end
    else
        print('[memscan] commands: /memscan { probe | ptrs | snap | diff | snapdeep [hex_offset] | diffdeep | poke <hex_offset> <decimal_value> | watch <hex_offset> <bytes> | rawread <hex_addr> <bytes> }')
        return
    end
    write_out(result)
    -- Brief in-game summary so the user sees something happened.
    if result.menu_name then
        print(('[memscan] menu_name=%q base=%s'):format(result.menu_name, result.menu_base or 'nil'))
    elseif result.menu_base then
        print(('[memscan] base=%s (no name)'):format(result.menu_base))
    elseif result.ptrs then
        local found = {}
        for k, v in pairs(result.ptrs) do
            if v then found[#found+1] = k end
        end
        print(('[memscan] %d pointers found: %s'):format(#found, table.concat(found, ', ')))
    elseif result.raw then
        print(('[memscan] raw[%d]: %s'):format(result.bytes or 0, (result.raw or ''):sub(1, 96)))
    else
        print('[memscan] no usable result (chain broke)')
    end
end)

ashita.events.register('load', 'memscan_load', function()
    print(('[memscan] v%s loaded - try /memscan probe with a menu open'):format(addon.version))
end)
