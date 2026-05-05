--[[
* interact - interact.lua
*
* Ashita v4 addon that bridges agent_core to the FFXI client's UI:
* NPC dialog, vendor menus, trade windows, home points, the death
* menu - anything that's a server-driven menu the player has to
* navigate. Two halves:
*
*   STATE PUBLISH (incoming):
*     Reads the live menu state from FFXI client memory each tick
*     and atomically writes:
*       <install>/config/xillm/state/<char>/menu.json
*     The orchestrator polls this file to know what's on screen.
*
*   ACTION CHANNEL (outgoing):
*     Polls
*       <install>/config/xillm/commands/<char>/interact.json
*     for a single action object; on read, deletes the file and
*     dispatches it via either a memory write (the +0x548 submit
*     path) or TkEventMsg2 keypress injection (Enter for text
*     frames). Built outgoing packets are kept as a fallback for
*     a few flows (buy/sell, force escape).
*
* ============================================================
* MEMORY STRUCTURES (verified empirically 2026-05-02; see
* memory note `project_menu_complete_reference.md` for the
* full reference and `project_menu_act_as_player.md` for the
* end-to-end mechanism)
* ============================================================
*
* POINTER CHAIN to the live menu state:
*
*   AshitaCore:GetPointerManager():Get('menu')   -- static head
*     -> ptr1 (uint32 deref)
*     -> menu_base (uint32 deref of ptr1)
*         menu_base + 0x08 = option_array_base (master records, 0x70 each)
*         menu_base + 0x0C = cursor_struct
*         menu_base + 0x14 = heap pointer (UI element list head)
*         menu_base + 0x18 = heap pointer (UI element list tail)
*
* The chain may resolve even when no menu is "really" open from the
* player's POV - the structures persist between menus and get
* repopulated. Detect liveness by whether `read_menu_state` returns
* a sane prompt + option count, NOT by the static head pointer alone.
*
* CURSOR_STRUCT layout (the polymorphic IwMenu-like class):
*
*   +0x00  uint32  vtable pointer (varies by menu type; see
*                  MENU_KIND_BY_VTABLE table below)
*   +0x08  uint32  back-pointer to menu_base
*   +0x14  uint32  widget-list head pointer (linked list of per-option widgets)
*   +0x18  uint32  widget-list tail pointer (== head when single-item)
*   +0x24  uint32  visible option count
*   +0x28  uint32  count again (mirror)
*   +0x30  uint8   "input handler's cursor mirror" - tracks where the
*                  input handler thinks the cursor is. WRITES HERE
*                  DON'T MOVE the visible cursor and don't affect
*                  what Enter selects. Useful only as a read for
*                  "where is the cursor right now".
*   +0x32  uint8   "input direction marker" - changes when user
*                  presses arrows (read-only signal)
*   +0x34  ...     prompt text (FFXI-encoded; same format as option
*                  labels, see TEXT ENCODING below)
*   +0x548 uint16  THE SUBMIT FLAG. Writing N here makes the client
*                  per-frame loop (consumer at FFXIMain 0x01B02860,
*                  alt path at 0x01B02909) interpret it as "player
*                  picked option (N-1)", look up the proper option
*                  index for that visible widget (handles opaque
*                  encodings like 0x80A1 for conquest items), send
*                  0x05B itself, AND dismiss/transition the UI
*                  cleanly. Useful range 1..0xFE; 0xFF crashes the
*                  client.
*
* WIDGET LIST (where labels live):
*
*   `cursor_struct + 0x14` is the head of a doubly-linked list of
*   widgets. Each list node is 0x30 bytes:
*
*     +0x00  uint32  next pointer
*     +0x04  uint32  prev pointer
*     +0x08-0x0F     padding/zeros
*     +0x10  uint32  payload pointer (the widget itself)
*     +0x14-0x2B     widget metadata (varies; we don't decode)
*     +0x2C  uint8   MASTER OPTION INDEX (1-based; the value to
*                    write to +0x548 to pick this widget). Critical
*                    for menus that filter rank-locked items -
*                    visible position N != master index N!
*     +0x2D-0x2F     more metadata + class-id sentinel
*                    (`75 80` for option items, `46 81` for some
*                    navigation widgets)
*
*   The first `count` (= cursor_struct + 0x24) nodes correspond to
*   visible options 0..count-1 in cursor order. Walk via next ptrs;
*   don't trust stride (some menus skip widget allocations).
*
*   Each widget payload starts with:
*     +0x00  4 bytes  header (always 00 00 00 00 in observation)
*     +0x04  ...      FFXI-encoded label text
*
*   Highlighted item names (Scroll names, key items, etc.) are
*   bracketed by `0xFEFE` (open) and `0xFFFE` (close) wide-char
*   escape markers. The text inside uses normal cap/digit/punct
*   encoding (see below).
*
*   STALE BUFFER PROBLEM: widget memory is reused across menus
*   without zero-padding. Short text (e.g. "Next page." 10 chars)
*   in a buffer that previously held longer text leaves the suffix
*   bytes valid as text. No length field exposed in the structure.
*   Workaround: detect "template" widgets by first encoded char in
*   range 0x10..0x19 (digit) - those need the relaxed period+null+
*   period terminator to keep "1000-pt. items (rank N required)."
*   intact. Everything else uses a tight terminator (any null after
*   sentence-end punctuation = end of label). See read_option_label.
*
* TEXT ENCODING (same for prompt and labels):
*
*   Each glyph is one byte, stored as a UTF-16-LE-shaped wide char
*   (high byte = 0 for valid text). Three regions:
*
*     0x10..0x19  Digits 0-9 (algebraic: code - 0x10)
*     0x21..0x3A  Capital A-Z (algebraic: letter = code - 0x20)
*     0x41..0x5A  ASCII A-Z bytes that the FFXI font draws as
*                 LOWERCASE a-z
*
*   Punctuation lives in a small explicit table:
*     0x00 = inline space (also null terminator at end)
*     0x07 = newline
*     0x08 = '('       0x09 = ')'
*     0x0C = ','       0x0D = '-'       0x0E = '.'
*     0x1A = ':'       0x1F = '?'
*     0x3B = '['       0x3D = ']'
*
*   Plus zero-width text-style escape markers (skip in decode,
*   don't terminate):
*     0xFEFE = open "highlighted text"  (item names, key items, etc)
*     0xFFFE = close highlight
*
*   Multi-byte font/color escapes (0xFE FE patterns and similar in
*   chat-style frames) aren't fully decoded; the surrounded text
*   passes through.
*
* MENU KINDS (distinguished by cursor_struct vtable):
*
*   0x01D84280 = option_list  (multi-choice menu, e.g. main NPC
*                              dialog, items category, items list,
*                              Yes/No menus)
*   0x01D829A0 = text_frame   (preview / "Are you sure?" frames -
*                              press Enter to advance to a real menu)
*   other      = unknown (fall back to label-shape heuristics)
*
*   The full Bastok-conquest-purchase chain is 5 picks:
*     1. main menu       option_list -> "spend conquest points"
*     2. items category  option_list -> "Common items (all ranks)"
*     3. item list       option_list -> the specific item
*     4. preview         text_frame  -> Enter to advance
*     5. Yes/No menu     option_list -> "Yes, purchase item"
*
* WHAT DOES NOT WORK (don't try):
*   - Writing cursor_struct + 0x30 (uint8) to position the cursor:
*     the byte updates but the visible cursor and input handler don't
*     follow.
*   - Building 0x05B packets manually with captured option_indexes
*     (e.g. 0x80A1): packet sends but dialog UI doesn't always
*     dismiss because client never saw the input pipeline.
*   - Virtual gamepad (uinput service): worked but Wine reliability
*     pushed us to the +0x548 path instead.
--]]

addon.name    = 'interact'
addon.author  = 'xillm'
addon.version = '0.43'
addon.desc    = 'NPC menu / vendor / dialog bridge for agent_core'
addon.commands = {'/interact'}

require('common')
require('win32types')
local ffi  = require('ffi')
local C    = ffi.C
local json = require('json')

-- Win32 API for keyboard input injection. keybd_event() synthesizes
-- a key press at the OS level (kernel32 level), which Wine then
-- routes through the same input pipeline that physical USB keyboard
-- presses use. Critically, this PASSES THROUGH DirectInput - the
-- FFXI client's input handler reads it as a real player keypress.
-- xdotool can't do this because it injects synthesized X11 events
-- at a higher level that Wine's DirectInput shim ignores.
ffi.cdef[[
    void keybd_event(uint8_t bVk, uint8_t bScan, uint32_t dwFlags, uintptr_t dwExtraInfo);

    /* PostMessage / SendMessage - send a WM_KEYDOWN/WM_KEYUP directly
     * to the FFXI window's message queue. Bypasses focus checks but
     * Win32 GUI apps must be processing message loops for the key to
     * land. FFXI's window proc DOES process messages. */
    typedef int BOOL;
    typedef unsigned int UINT;
    typedef void* HWND;
    typedef uintptr_t WPARAM;
    typedef intptr_t LPARAM;
    BOOL PostMessageA(HWND hWnd, UINT Msg, WPARAM wParam, LPARAM lParam);

    /* SendInput - modern replacement for keybd_event. Same OS-level
     * injection path. INPUT struct is 28 bytes on Win32 (with x64
     * padding it'd be 32). LuaJIT FFI handles this fine. */
    typedef struct {
        unsigned short wVk;
        unsigned short wScan;
        unsigned int   dwFlags;
        unsigned int   time;
        uintptr_t      dwExtraInfo;
    } KEYBDINPUT;
    typedef struct {
        unsigned int type;
        union {
            KEYBDINPUT ki;
            uint8_t    pad[24];   /* MOUSEINPUT / HARDWAREINPUT also fit */
        } u;
    } INPUT_t;
    UINT SendInput(UINT cInputs, INPUT_t* pInputs, int cbSize);
]]

local WM_KEYDOWN = 0x0100
local WM_KEYUP   = 0x0101
local INPUT_KEYBOARD = 1

-- Win32 KEYEVENTF_KEYUP flag - passed to keybd_event for the release
-- half of a press. Pressing-and-releasing in the same call is not
-- supported; you call keybd_event TWICE per logical press.
local KEYEVENTF_KEYUP = 0x0002

-- Windows Virtual-Key codes for the keys the agent needs. Full list
-- at https://learn.microsoft.com/en-us/windows/win32/inputdev/virtual-key-codes
local VK_RETURN  = 0x0D   -- 13
local VK_ESCAPE  = 0x1B   -- 27
local VK_LEFT    = 0x25   -- 37
local VK_UP      = 0x26   -- 38
local VK_RIGHT   = 0x27   -- 39
local VK_DOWN    = 0x28   -- 40
local VK_MAP = {
    enter  = VK_RETURN,
    return_ = VK_RETURN,
    escape = VK_ESCAPE,
    esc    = VK_ESCAPE,
    up     = VK_UP,
    down   = VK_DOWN,
    left   = VK_LEFT,
    right  = VK_RIGHT,
}

-- Inject a Win32 keypress (down + up) for the given VK code. Tries
-- THREE injection methods for max compatibility with Wine/DInput:
--
--   1. SendInput (modern, preferred) - injects at OS level via the
--      Win32 input pipeline. Most likely to be picked up by DI's
--      GetDeviceState polling.
--   2. keybd_event (legacy but widely supported) - same OS-level
--      injection. Some Wine versions only stub one of these.
--   3. PostMessage(WM_KEYDOWN/UP) to the FFXI HWND - bypasses
--      input devices entirely; goes through the window's message
--      pump. Doesn't reach DInput but FFXI's GUI menus may also
--      read WM_KEYDOWN directly.
--
-- We send all three back-to-back; whichever Wine actually honors
-- will trigger FFXI's input handler. Returns true if any of the
-- three succeeded (i.e. didn't throw). Empirical tuning is the only
-- way to know which Wine version supports which path.
local function press_vk(vk)
    if vk == nil or vk == 0 then return false end
    local any_ok = false
    -- Method 1: SendInput
    local ok1 = pcall(function()
        local inp = ffi.new('INPUT_t[2]')
        inp[0].type = INPUT_KEYBOARD
        inp[0].u.ki.wVk = vk
        inp[1].type = INPUT_KEYBOARD
        inp[1].u.ki.wVk = vk
        inp[1].u.ki.dwFlags = KEYEVENTF_KEYUP
        C.SendInput(2, inp, ffi.sizeof('INPUT_t'))
    end)
    if ok1 then any_ok = true end
    -- Method 2: keybd_event (legacy)
    local ok2 = pcall(function()
        C.keybd_event(vk, 0, 0, 0)
        C.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    end)
    if ok2 then any_ok = true end
    -- Method 3: PostMessage to FFXI window
    local ok3 = pcall(function()
        local hwnd = ffi.cast('HWND', AshitaCore:GetProperties():GetFinalFantasyHwnd())
        if hwnd ~= nil then
            C.PostMessageA(hwnd, WM_KEYDOWN, vk, 0)
            C.PostMessageA(hwnd, WM_KEYUP,   vk, 0)
        end
    end)
    if ok3 then any_ok = true end
    return any_ok
end

-------------------------------------------------------------------------------
-- Config
-------------------------------------------------------------------------------

-- Liveness publish: the menu doesn't change every frame; we still want
-- the file's mtime fresh so the orchestrator's stale-snapshot check
-- doesn't refuse to dispatch interact actions during a long menu.
local PUBLISH_EVERY_FRAMES = 30   -- ~2 Hz

-- Action poll: the orchestrator writes a single action JSON file and
-- expects us to drain + delete it. ~10 Hz is plenty - menu input has
-- generous server tolerance.
local POLL_EVERY_FRAMES = 6

-- After how many frames of no menu activity we declare the menu closed
-- (in case 0x05C "Event End" was missed). Defensive bound only -
-- the close packet is reliable. Set to 0 to disable entirely (the
-- preferred mode while iterating on parsing - autoclose hides what
-- the addon is or isn't capturing). The orchestrator's leaf state-
-- timeout is the real safety net for stuck dialogs (60s in the
-- interact_director); this addon-side timeout was redundant.
local MENU_TIMEOUT_FRAMES = 0     -- disabled (was 600 = 10s)

-------------------------------------------------------------------------------
-- Paths (mirror inventory.lua / combat.lua patterns)
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

local function ensure_dir(path)
    pcall(function() ashita.fs.create_directory(path) end)
end

local function msg(text)
    print('\30\06[interact]\30\01 ' .. text)
end

local function pcget(fn)
    local ok, v = pcall(fn)
    if ok then return v end
    return nil
end

-------------------------------------------------------------------------------
-- Atomic write (temp + rename) - the orchestrator may read while we
-- write, and partial JSON would crash the read. Same pattern combat.lua
-- and inventory.lua use, lifted intact for consistency.
-------------------------------------------------------------------------------

local function write_json_atomic(path, data)
    local ok, encoded = pcall(json.encode, data)
    if not ok or encoded == nil then return false end
    -- Match the existing pattern in inventory.lua / combat.lua:
    -- direct overwrite. The tmp+rename idiom we initially used here
    -- doesn't work cleanly on Wine because os.rename refuses to
    -- replace an existing destination, leaving stale .tmp files
    -- around. Direct write has a tiny torn-read window but the
    -- consumer side does errors='replace' on the JSON parse, so a
    -- rare partial read just falls back to the cached snapshot.
    local f = io.open(path, 'w')
    if not f then return false end
    f:write(encoded)
    f:close()
    return true
end

-------------------------------------------------------------------------------
-- TkEventMsg2::OnKeyDown injection - lifted from atom0s' `stepdialog`
-- addon. Lets us simulate the player pressing Enter to advance dialog
-- text WITHOUT having to know the outgoing packet format. Key=5 is
-- the FFXI internal "advance/confirm" code stepdialog uses.
--
-- Other key codes (UP/DOWN/ESC) are not yet mapped here - menu cursor
-- navigation via simulated arrows is one possible path for Phase C
-- but we'll prefer outgoing packet selection where possible.
-------------------------------------------------------------------------------

ffi.cdef[[
    typedef void (__thiscall* TkEventMsg2_OnKeyDown_f)(int32_t, int16_t, int16_t);
]]

-- Bring in u8/u16/u32 typedefs we use to cast packet buffers byte-by-byte
-- when filling them via QueuePacket callbacks.
ffi.cdef[[
    typedef uint8_t  u8;
    typedef uint16_t u16;
    typedef uint32_t u32;
]]

local injection = {
    -- Pattern bytes lifted verbatim from stepdialog v1.1 (atom0s).
    -- Resolved at load time; nil/0 if the function can't be found
    -- (different client build, future patch breaking the pattern).
    func_ptr = nil,
    this_ptr = nil,
    available = false,
}

local function init_injection()
    injection.func_ptr = ashita.memory.find(0, 0,
        '538B5C240856578B7C24148BF15753E8????????8B0D????????3BF174', 0, 0)
    injection.this_ptr = ashita.memory.find(0, 0,
        '8B0D????????85C90F??????????8B410885C00F', 2, 0)
    injection.available = injection.func_ptr ~= nil and injection.func_ptr ~= 0
                      and injection.this_ptr ~= nil and injection.this_ptr ~= 0
    if not injection.available then
        msg('TkEventMsg2 injection unavailable - advance_text will no-op. '
            .. '(Stepdialog patterns not found; client build may have changed.)')
    end
end

local KEY_ENTER  = 5  -- FFXI internal "confirm" key code (stepdialog source).
-- FFXI internal "back/cancel" - the equivalent of pressing Escape.
-- This is a best-guess based on the typical FFXI input mapping
-- (5=confirm, 6=cancel pattern shared by many menu systems). If 6
-- doesn't work, try other low integers - this is empirical and we
-- don't have a stepdialog-equivalent source citing the canonical
-- value.
local KEY_ESCAPE = 6

local function inject_key(keycode)
    if not injection.available then return false end
    local this = ashita.memory.read_uint32(injection.this_ptr)
    if this == nil or this == 0 then return false end
    this = ashita.memory.read_uint32(this)
    if this == nil or this == 0 then return false end
    local func = ffi.cast('TkEventMsg2_OnKeyDown_f', injection.func_ptr)
    if func == nil then return false end
    local ok = pcall(function() func(this, keycode, 0xFFFF) end)
    return ok
end

-------------------------------------------------------------------------------
-- Menu state model
-------------------------------------------------------------------------------

local menu = {
    open         = false,
    kind         = 'unknown',  -- dialog | vendor | trade | death | unknown
    npc_name     = '',
    npc_sid      = 0,
    -- Event context fields captured from incoming 0x032/0x033/0x034.
    -- The outgoing 0x05B response echoes event_id in its EventPara
    -- field; without that the server's validate() rejects with
    -- "not in event."
    event_act_index = 0,
    event_id     = 0,
    prompt       = '',
    options      = {},
    cursor       = 0,
    vendor_items = {},
    -- Frame stamp of the last meaningful update. Used for
    -- MENU_TIMEOUT_FRAMES auto-close.
    last_active  = 0,
    -- Internal: did we change since the last publish? Skips writes
    -- when nothing's new (cheaper for the orchestrator's mtime check).
    dirty        = false,
}

local function reset_menu()
    if menu.open then menu.dirty = true end
    menu.open            = false
    menu.kind            = 'unknown'
    menu.npc_name        = ''
    menu.npc_sid         = 0
    menu.event_act_index = 0
    menu.event_id        = 0
    menu.prompt          = ''
    menu.options         = {}
    menu.cursor          = 0
    menu.vendor_items    = {}
end

-- Strip FFXI inline color/format escape sequences so the JSON we
-- publish has clean text. Lifted from xillm.lua's earlier impl.
local function strip_codes(s)
    return (s or ''):gsub('\x1e.', ''):gsub('\x1f.', '')
                    :gsub('\x7f', ''):gsub('\x80\x80', ' ')
end

-------------------------------------------------------------------------------
-- Packet hooks
--
-- IDs verified against LSB src/map/packets/{s2c,c2s}. Earlier versions
-- of this addon used 0x05A/0x05B/0x05C which are WRONG: 0x05A is
-- motionmes (emotes), 0x05B INCOMING doesn't exist (0x05B is the
-- client's OUTGOING response packet), 0x05C is pendingnum.
--
-- INCOMING (s2c):
--   0x032 (event):      NPC starts an event. Carries UniqueNo
--                       (NPC server id), ActIndex (NPC actor index),
--                       EventNum (event id). We capture all three so
--                       the outgoing response packet can echo them.
--   0x033 (eventstr):   event with string params - same context fields.
--   0x034 (eventnum):   event with numeric params - same context fields.
--   0x036 (talknum):    NPC speech (non-event single line).
--   0x03B (eventmes):   event message text.
--   0x052 (eventucoff): event closing / update offer. Authoritative
--                       close signal.
--   0x03C (shop_list):  vendor item list. Phase C: parse for vendor
--                       stock auto-cataloging.
--   0x03E (shop_open):  shop UI opens after 0x03C.
--
-- OUTGOING (c2s):
--   0x05B (eventend):   sent BY us when player picks an option.
--                       UniqueNo + ActIndex + EventNum echo what
--                       came in on 0x032; EndPara holds the option
--                       index; Mode=1 (UpdatePending) keeps the event
--                       going for multi-stage menus, Mode=0 (End)
--                       finishes it.
--   0x083 (shop_buy):   item purchase. Phase C.
--   0x084/0x085:        sell req + set (two-step). Phase C.
-------------------------------------------------------------------------------

-- Read primitives at byte offsets. The `offset` is 0-based (matching
-- the LSB header docs); we add 1 internally for Lua's 1-indexed
-- strings. All FFXI fields are little-endian.
local function read_u32(data, offset)
    if data == nil or #data < offset + 4 then return 0 end
    local b1 = data:byte(offset + 1) or 0
    local b2 = data:byte(offset + 2) or 0
    local b3 = data:byte(offset + 3) or 0
    local b4 = data:byte(offset + 4) or 0
    return b1 + b2 * 0x100 + b3 * 0x10000 + b4 * 0x1000000
end

local function read_u16(data, offset)
    if data == nil or #data < offset + 2 then return 0 end
    local b1 = data:byte(offset + 1) or 0
    local b2 = data:byte(offset + 2) or 0
    return b1 + b2 * 0x100
end

-- Look up an entity's act_index AND name by server_id. The Ashita
-- entity manager indexes by act_index (0..2304); we linear-scan to
-- find the slot matching the given server id. Cheap; only called on
-- menu-open events, not per-frame. Returns (act_index, name) or (0, '').
local function lookup_entity(sid)
    if sid == nil or sid == 0 then return 0, '' end
    local em = pcget(function() return AshitaCore:GetMemoryManager():GetEntity() end)
    if em == nil then return 0, '' end
    for idx = 0, 2304 do
        local eid = pcget(function() return em:GetServerId(idx) end) or 0
        if eid == sid then
            local nm = pcget(function() return em:GetName(idx) end) or ''
            return idx, nm
        end
    end
    return 0, ''
end

local function lookup_npc_name(sid)
    local _, nm = lookup_entity(sid)
    return nm
end

-- Mutable runtime state. Declared BEFORE the packet hooks register so
-- the closures captured by ashita.events.register pick up the local
-- via lexical scoping, NOT the (nil) global. Earlier versions hit
-- "attempt to index global 'state' (a nil value)" at packet_in time
-- because state was declared later in the file.
local state = {
    frame = 0,
    last_character = nil,
    last_publish_frame = 0,
}

-- Debug packet capture: when /interact debug is on, every packet of
-- interest gets a hex dump appended to debug_packets.log. Use this to
-- empirically confirm packet structures before writing parsers.
local debug_capture = false

-- Structured packet capture: JSON-per-line file with parsed fields
-- for the packets we care about (0x01A, 0x05B outgoing; 0x032, 0x034,
-- 0x052, 0x05C incoming). Used to capture a real player's exact
-- interaction with an NPC so we can replay it byte-for-byte AND
-- analyze the field semantics offline.
--
-- Activated via `/interact capture start [filename]`. Writes to
-- <install>/config/xillm/state/<filename> (default: capture.jsonl).
-- Stop with `/interact capture stop`.
local capture_active = false
local capture_path   = nil
local capture_count  = 0

local function capture_jsonl_write(tbl)
    if not capture_active or not capture_path then return end
    -- Hand-rolled JSON to keep the addon dep-free. The shape is a
    -- flat table (no nested objects) so this is simple enough.
    local parts = {}
    for k, v in pairs(tbl) do
        local val
        if type(v) == 'string' then
            local esc = v:gsub('\\', '\\\\'):gsub('"', '\\"')
            val = '"' .. esc .. '"'
        elseif type(v) == 'number' then
            val = tostring(v)
        elseif type(v) == 'boolean' then
            val = v and 'true' or 'false'
        else
            val = 'null'
        end
        parts[#parts+1] = '"' .. k .. '":' .. val
    end
    local line = '{' .. table.concat(parts, ',') .. '}'
    local f = io.open(capture_path, 'a')
    if not f then return end
    f:write(line, '\n')
    f:close()
    capture_count = capture_count + 1
end

-- Parsers for the packets we care about. Returns a flat table of
-- field-name -> value, ready to merge into a JSONL row.
local function parse_packet(direction, id, data)
    local fields = {}
    -- Helper: read little-endian word
    local function u32(off) return data:byte(off+1) + data:byte(off+2)*0x100 + data:byte(off+3)*0x10000 + data:byte(off+4)*0x1000000 end
    local function u16(off) return data:byte(off+1) + data:byte(off+2)*0x100 end
    local function u8(off)  return data:byte(off+1) end

    if direction == 'out' and id == 0x01A then
        -- Action / Talk
        fields.target          = u32(0x04)
        fields.target_index    = u16(0x08)
        fields.category        = u8(0x0A)
        fields.param           = u16(0x0C)
    elseif direction == 'out' and id == 0x05B then
        -- Dialog choice (per Windower fields.lua schema)
        fields.target            = u32(0x04)
        fields.option_index      = u16(0x08)
        fields.unknown1          = u16(0x0A)
        fields.target_index      = u16(0x0C)
        fields.automated_message = u8(0x0E)
        fields.unknown2          = u8(0x0F)
        fields.zone              = u16(0x10)
        fields.menu_id           = u16(0x12)
    elseif direction == 'out' and id == 0x05C then
        -- Warp Request
        fields.target_id         = u32(0x10)
        fields.zone              = u16(0x18)
        fields.menu_id           = u16(0x1A)
    elseif direction == 'in' and (id == 0x032 or id == 0x033) then
        -- Event begin (short form)
        fields.npc_sid       = u32(0x04)
        fields.act_index     = u16(0x08)
        fields.event_num     = u16(0x0A)
        fields.event_para    = u16(0x0C)  -- this is the menu_id
        fields.mode          = u16(0x0E)
    elseif direction == 'in' and id == 0x034 then
        -- Event begin with 8 numeric params (used by conquest officer)
        fields.npc_sid       = u32(0x04)
        fields.act_index     = u16(0x28)
        fields.event_num     = u16(0x2A)
        fields.event_para    = u16(0x2C)  -- menu_id
        fields.mode          = u16(0x2E)
    elseif direction == 'in' and id == 0x052 then
        -- Event end / NPC release - usually empty body of interest
        -- (no parsable fields beyond the header)
    elseif direction == 'in' and id == 0x05C then
        -- Dialogue Information (vendor stock, etc)
        -- Body varies by NPC; just record raw bytes
    end
    return fields
end

local function capture_packet(direction, id, data)
    if not capture_active then return end
    -- Filter: only the packets we care about for NPC interactions
    local is_interesting = (
        (direction == 'out' and (id == 0x01A or id == 0x05B or id == 0x05C)) or
        (direction == 'in'  and (id == 0x032 or id == 0x033 or id == 0x034 or
                                  id == 0x052 or id == 0x05C))
    )
    if not is_interesting then return end

    -- Hex-encode the body for byte-level analysis later.
    local hex = {}
    for i = 1, math.min(#data, 64) do
        hex[#hex + 1] = ('%02X'):format(data:byte(i))
    end

    local row = {
        ts        = os.time(),
        direction = direction,
        id        = ('0x%03X'):format(id),
        len       = #data,
        bytes     = table.concat(hex, ' '),
    }
    -- Merge in parsed fields.
    local parsed = parse_packet(direction, id, data)
    for k, v in pairs(parsed) do row[k] = v end

    capture_jsonl_write(row)
end

local function debug_log_packet(direction, id, data)
    if not debug_capture then return end
    local path = get_data_path() .. 'state/' .. (state.last_character or 'unknown') .. '/interact_packets.log'
    local f = io.open(path, 'a')
    if not f then return end
    local hex = {}
    for i = 1, math.min(#data, 128) do
        hex[#hex + 1] = ('%02X'):format(data:byte(i))
    end
    f:write(('[%s] dir=%s id=0x%03X len=%d %s\n'):format(
        os.date('%H:%M:%S'), direction, id, #data, table.concat(hex, ' ')))
    f:close()
end

-- Capture event context from one of the event-begin packet variants.
-- Layouts verified against LSB src/map/packets/s2c AND empirical
-- byte-dumps in-game. The field NAMES in LSB's struct headers are
-- misleading PS2 holdovers: the field LSB calls "EventPara" is what
-- the server's validate() in packet 0x05B compares against
-- PChar->currentEvent->eventId, so EventPara IS the event id we
-- need to echo. The field LSB calls "EventNum" is something else
-- (a sub-state index? menu page?) and the server's process() does
-- not use it on the response packet.
--
-- Layouts (body offsets after the 4-byte header):
--   0x032 (event):    UniqueNo(4) | ActIndex(2) | EventNum(2) |
--                     EventPara(2) | Mode(2) | ...
--                     -> event_id (== EventPara) at body offset 8
--                                                = absolute 0x0C
--   0x033 (eventstr): same as 0x032.
--   0x034 (eventnum): UniqueNo(4) | num[8]=32 bytes | ActIndex(2) |
--                     EventNum(2) | EventPara(2) | Mode(2) | ...
--                     -> event_id at body offset 40 = absolute 0x2C
--
-- 0x034 specifically: conquest overseers (Rabid Wolf, I.M.) and many
-- quest NPCs use this variant, with 32 bytes of int32 params before
-- ActIndex.
local function capture_event_context(packet_id, data)
    local act_offset, event_id_offset
    if packet_id == 0x034 then
        -- 0x04 UniqueNo + 32 bytes num[8] + 2 ActIndex + 2 EventNum
        -- + 2 EventPara
        act_offset       = 0x04 + 4 + 32             -- 0x28
        event_id_offset  = act_offset + 4            -- 0x2C (EventPara)
    else
        act_offset       = 0x08
        event_id_offset  = 0x0C                      -- EventPara
    end
    local sid       = read_u32(data, 0x04)
    local act_index = read_u16(data, act_offset)
    local event_id  = read_u16(data, event_id_offset)
    if sid == 0 then return end
    menu.open            = true
    menu.npc_sid         = sid
    menu.npc_name        = lookup_npc_name(sid)
    menu.event_act_index = act_index
    menu.event_id        = event_id
    if menu.kind == 'unknown' then menu.kind = 'dialog' end
    -- Reset prompt / options when we see a NEW event begin; they'll
    -- accumulate again from text_in.
    menu.prompt   = ''
    menu.options  = {}
    menu.cursor   = 0
    menu.last_active = state.frame
    menu.dirty    = true
end

-- Counter for the first-N-packets liveness probe. Bounded so the
-- chat doesn't get spammed; once we've confirmed packets are
-- flowing the user can disable via /interact debug toggling it on
-- while the dialog is open.
local _probe_count = 0
local _probe_max = 30

-- Latest observed OUTGOING packet's sync field. The FFXI client
-- maintains a global sync counter that advances for every packet
-- it sends; the server uses it for replay/forgery protection and
-- silently drops packets whose sync is too low or out-of-window.
-- Ashita's AddOutgoingPacket does NOT auto-populate this field,
-- so addon-injected packets with sync=0 get dropped.
--
-- We sniff the client's own outgoing packets to track the rolling
-- sync, then stamp our injected packets with `latest + 1`. The
-- client's next packet bumps to a higher sync naturally so we
-- don't collide. This isn't perfect (race on simultaneous sends)
-- but in practice the client only sends ~1pkt/sec idle, so a
-- collision window of ~50ms is exceedingly rare.
local _latest_out_sync = 0

local function inject_sync(p)
    -- Bump the tracked sync, write into the packet's sync field
    -- (bytes 3-4, little-endian u16). The 1-indexed array slots are
    -- p[3] (sync_low) and p[4] (sync_high).
    _latest_out_sync = bit.band(_latest_out_sync + 1, 0xFFFF)
    p[3] = bit.band(_latest_out_sync, 0xFF)
    p[4] = bit.band(bit.rshift(_latest_out_sync, 8), 0xFF)
end

ashita.events.register('packet_out', 'interact_packet_out', function(e)
    -- Track the FFXI client's rolling sync counter so our injected
    -- packets can use a fresh value. The sync is bytes 2-3 (0-based)
    -- of the packet header, little-endian u16.
    if #e.data >= 4 then
        local s = (e.data:byte(3) or 0) + (e.data:byte(4) or 0) * 256
        if s > _latest_out_sync or (_latest_out_sync > 0xF000 and s < 0x1000) then
            _latest_out_sync = s
        end
    end

    -- Outgoing-packet probe. Dumps our own 0x05B / 0x01A / 0x083 etc.
    -- so we can verify the packet bytes the client is actually
    -- transmitting (sometimes Ashita rewrites the header before
    -- send). Helpful when "we sent the packet" prints in chat but
    -- the server never responds.
    if e.id == 0x05B or e.id == 0x01A or e.id == 0x083 or e.id == 0x085 then
        local hex = {}
        for i = 1, math.min(#e.data, 32) do
            hex[#hex + 1] = ('%02X'):format(e.data:byte(i))
        end
        msg(('OUT 0x%03X len=%d %s'):format(e.id, #e.data,
            table.concat(hex, ' ')))
    end
    -- Per-frame debug: when /interact debug is on, log EVERY outgoing
    -- packet ID to a file so we can see the full sequence the client
    -- sends during a dialog. Skips position 0x015 (very noisy) unless
    -- user explicitly opts in via /interact debug_full.
    if debug_capture and e.id ~= 0x015 then
        debug_log_packet('out', e.id, e.data)
    end
    capture_packet('out', e.id, e.data)
end)

ashita.events.register('packet_in', 'interact_packet_in', function(e)
    -- Liveness probe: announce the first few packets we see in chat,
    -- so we can confirm the hook is firing at all without depending
    -- on file I/O. Once we've seen the first 0x032 (event begin) the
    -- counter still ticks but the noise is bounded.
    if _probe_count < _probe_max then
        _probe_count = _probe_count + 1
        if e.id == 0x032 or e.id == 0x033 or e.id == 0x034
                or e.id == 0x036 or e.id == 0x052 or e.id == 0x03B
                or e.id == 0x03C or e.id == 0x03E or e.id == 0x05A
                or e.id == 0x05B or e.id == 0x05C then
            msg(('PROBE in: id=0x%03X len=%d'):format(e.id, #e.data))
        end
    end

    -- When debug is on, log EVERY packet ID we see so we can identify
    -- the actual IDs this server uses. Cheap; only writes when debug
    -- is toggled on.
    if debug_capture then
        debug_log_packet('in', e.id, e.data)
    end
    capture_packet('in', e.id, e.data)

    -- Event begin / variants: capture the event context.
    if e.id == 0x032 or e.id == 0x033 or e.id == 0x034 then
        capture_event_context(e.id, e.data)
        -- One-shot dump of the FIRST event packet's bytes so we can
        -- verify field offsets empirically. After the first dump the
        -- counter pins so the chat doesn't fill up.
        if _probe_count <= _probe_max then
            local hex = {}
            for i = 1, math.min(#e.data, 64) do
                hex[#hex + 1] = ('%02X'):format(e.data:byte(i))
            end
            msg(('PROBE 0x%03X bytes: %s'):format(e.id, table.concat(hex, ' ')))
            msg(('PROBE captured: sid=%d act=%d event_id=%d')
                :format(menu.npc_sid, menu.event_act_index,
                        menu.event_id))
        end
        return
    end

    -- 0x036 (talknum): NPC speaks a single line, no event involved.
    -- Carries the NPC sid; we treat it as a lightweight "dialog open"
    -- for menus the server doesn't bother starting a full event for.
    if e.id == 0x036 then
        local sid = read_u32(e.data, 0x04)
        if sid ~= 0 then
            menu.open      = true
            menu.npc_sid   = sid
            menu.npc_name  = lookup_npc_name(sid)
            if menu.kind == 'unknown' then menu.kind = 'dialog' end
            menu.last_active = state.frame
            menu.dirty     = true
        end
        return
    end

    -- 0x03B (eventmes): event message - just refreshes the activity
    -- timestamp so the auto-close timeout (when enabled) doesn't trip.
    if e.id == 0x03B then
        if menu.open then menu.last_active = state.frame end
        return
    end

    -- 0x052 (eventucoff): server adjusts the client's event-control
    -- state. Has FIVE modes per LSB s2c/0x052_eventucoff.h:
    --   0 = Standard         (adjust standard control)
    --   1 = EventRecvPending (event is CONTINUING, not closing!)
    --   2 = CancelEvent      (close - what we previously treated all as)
    --   3 = CancelInput      (numerical/string input cancel)
    --   4 = Fishing          (release fishing event lock)
    -- Only mode=2 should drop our local menu state. Earlier we treated
    -- every 0x052 as a close, which broke multi-stage menus (vendor
    -- buy/sell, conquest CP-item purchase): the server sends mode=1
    -- between Update and the next event-begin, our addon flagged the
    -- menu closed, the director re-Talked from scratch and lost the
    -- per-event localVar the server had set during Update.
    if e.id == 0x052 then
        local mode = read_u32(e.data, 0x04)
        if mode == 2 then
            reset_menu()
        end
        return
    end

    -- 0x03C (shop_list): vendor item list. Phase C will parse the
    -- item array into menu.vendor_items.
    if e.id == 0x03C then
        menu.kind = 'vendor'
        menu.last_active = state.frame
        menu.dirty = true
        -- TODO(phase-c): parse item rows from this packet's body.
        return
    end

    -- 0x03E (shop_open): shop UI fully opened (after 0x03C).
    if e.id == 0x03E then
        menu.kind = 'vendor'
        menu.last_active = state.frame
        menu.dirty = true
        return
    end
end)

-- text_in handler for dialog text. FFXI routes NPC speech through
-- the chat stream with mode=150 (and assorted higher-bit variants
-- like 662 = 0x296 whose low byte is also 150). On the user's LSB
-- server we observed mode 150 for the dialog body and 254 for a
-- duplicate-render copy. We listen for any mode whose low byte
-- matches a known NPC-text mode and use that signal alone to decide
-- "a dialog is open right now."
--
-- Why text_in flags menu.open itself: some NPCs (Bastok conquest
-- overseers, info NPCs) don't fire 0x05A/0x032 - they push dialog
-- via chat packets only. Without this autonomy the addon never
-- sees the dialog open.
local NPC_TEXT_MODES = {
    [9]   = true,   -- "NPC speech" (classic FFXI mode)
    [150] = true,   -- event/dialog text (LSB observed)
    [151] = true,   -- variant of 150
}

ashita.events.register('text_in', 'interact_text_in', function(e)
    if e.blocked then return end
    local raw_mode = e.mode_modified or e.mode or 0
    local mid = bit.band(raw_mode, 0x000000FF)
    if not NPC_TEXT_MODES[mid] then return end
    local text = strip_codes(e.message_modified or e.message or '')
    if text == '' then return end
    -- Mark the menu as open if it wasn't already. The packet hooks
    -- (0x05A / 0x032) populate npc_sid / npc_name when they fire;
    -- if they don't, we publish without those fields - the consumer
    -- can still drive a dialog walker against prompt + options alone.
    if not menu.open then
        menu.open = true
        if menu.kind == 'unknown' then menu.kind = 'dialog' end
    end
    -- Treat the FIRST chunk of a freshly-opened menu as the prompt;
    -- subsequent chunks until reset accumulate as options. This is
    -- fragile heuristic territory - the menu_judge LLM fallback in
    -- Phase B is what saves us when this misclassifies.
    if menu.prompt == '' then
        menu.prompt = text
    else
        menu.options[#menu.options + 1] = text
    end
    menu.cursor = 0  -- we don't (yet) read cursor position; default 0
    menu.last_active = state.frame
    menu.dirty = true
end)

-------------------------------------------------------------------------------
-- Action dispatch
-------------------------------------------------------------------------------

local function action_advance_text()
    if not inject_key(KEY_ENTER) then
        msg('advance_text: TkEventMsg2 injection failed (not available?)')
        return false
    end
    return true
end

-- Build + send 0x05B (CLI EVENTEND). Layout from LSB
-- src/map/packets/c2s/0x05b_eventend.h:
--   header (4):     id=0x5B, size=5 dwords (byte 1 = 0x0A), sync auto
--   UniqueNo  (4):  NPC server id (echo of incoming 0x032's UniqueNo)
--   EndPara   (4):  the option index the player picked
--   ActIndex  (2):  NPC actor index (echo of 0x032's ActIndex)
--   Mode      (2):  1 = UpdatePending (event continues - default;
--                        used for multi-stage menus where the server
--                        sends another menu after we pick), 0 = End
--                        (close the event entirely).
--   EventNum  (2):  echo of 0x032's EventNum
--   EventPara (2):  echo of 0x032's EventPara - LSB's validate()
--                   uses this to confirm we're still in the same event
--
-- Total = 4 header + 16 body = 20 bytes (5 dwords). The size byte
-- in the header is `size << 1` per the FFXI convention; for size=5
-- that's 0x0A.
--
-- ALTERNATIVES considered (kept here in case we need to fall back):
--
--   (A) TkEventMsg2 key injection - same trick stepdialog uses for
--       Enter, extended with arrow keys to navigate menus. Pro: works
--       for ANY menu type uniformly; visible to player. Con: 100ms+
--       per option (one Enter per arrow press); we'd need to find
--       the FFXI-internal keycodes for UP/DOWN/ESC (key=5 is Enter
--       per stepdialog source, others not yet identified).
--
--   (B) Memory cursor write - write the cursor index directly into
--       the menu options pointer's cursor field, then send Enter.
--       Pro: instant. Con: requires finding MENU_PTR_ADDR via
--       Cheat Engine probing (legacy xillm.lua tried this with
--       MENU_PTR_ADDR=0x0 placeholder; was never filled in).
--
-- Outgoing packets (chosen here) is the most reliable: server-
-- authoritative, no client-version-specific memory layout assumptions,
-- and the LSB packet struct guarantees correctness against this
-- specific server fork.
-- Submit a menu pick by setting the m_IsEnd-equivalent flag at
-- offset 0x548 of the menu's cursor struct. The game's own per-
-- frame menu update loop (at runtime address ~0x01B02860) reads
-- this flag, decrements it to get the picked option index, and
-- runs the full submit cascade: sends the 0x05B EventEnd packet
-- AND dismisses the client UI.
--
-- Discovered 2026-05-01 by reverse engineering pol.exe's unpacked
-- code via a memscan dump + rizin disassembly of the input handler
-- at 0x01B9E2C0. Two write paths in the input handler set the
-- flag to 0xFF (special path - triggers a 0xFE error code branch
-- that crashes when invoked without proper context). The ALT
-- branch in the consumer (0x01B02909) interprets non-0xFF values
-- as `option_index + 1`, decrementing to recover the index.
-- Writing (option+1) hits the safe path: server processes the
-- pick, server sends event_end, client closes the dialog. Single
-- atomic write does it all.
--
-- Pointer chain:
--   p0 = AshitaCore:GetPointerManager():Get('menu')   (static)
--   p1 = read_uint32(p0)                              (manager)
--   menu_base = read_uint32(p1)                       (active menu)
--   cursor_struct = read_uint32(menu_base + 0x0C)
--   write_uint16(cursor_struct + 0x548, index + 1)
--
-- Returns true on successful write. Does NOT need a separate 0x05B
-- packet send - the game sends it as part of the submit flow.
--
-- IMPORTANT: this is verified for the "menu    query" menu type
-- (NPC dialog menus). Other menu types (IwSelectMenu used during
-- character select, IwYesNoMenu, etc) may store their submit flag
-- at a different offset. Test before assuming this works for
-- vendor / shop / trade menus.
-- Walk the same pointer chain submit_menu_pick uses; return the
-- three pointers (menu_base, option_array_base, cursor_struct) plus
-- the static head pointer. Returns nil for any deref that fails so
-- callers can fail fast rather than reading garbage.
local function walk_menu_chain()
    local pm = AshitaCore:GetPointerManager()
    if pm == nil then return nil end
    local p0 = pm:Get('menu')
    if p0 == nil or p0 == 0 then return nil end
    local p1 = pcget(function() return ashita.memory.read_uint32(p0) end)
    if p1 == nil or p1 == 0 then return nil end
    local menu_base = pcget(function() return ashita.memory.read_uint32(p1) end)
    if menu_base == nil or menu_base == 0 then return nil end
    local cursor_struct = pcget(function()
        return ashita.memory.read_uint32(menu_base + 0x0C)
    end)
    local option_array = pcget(function()
        return ashita.memory.read_uint32(menu_base + 0x08)
    end)
    return {
        menu_base     = menu_base,
        cursor_struct = cursor_struct,
        option_array  = option_array,
    }
end

-- Read a null-terminated UTF-16LE string from `addr`, decoding to
-- UTF-8. Bounded by `max_chars` (each char = 2 bytes). FFXI's menu
-- text is stored as wide-char so this is the canonical reader.
-- BMP only (no surrogate handling) — sufficient for FFXI's English
-- locale.
local function read_wide_string(addr, max_chars)
    if not addr or addr == 0 then return '' end
    max_chars = max_chars or 256
    local out = {}
    for i = 0, max_chars - 1 do
        local cp = pcget(function()
            return ashita.memory.read_uint16(addr + i * 2)
        end)
        if cp == nil or cp == 0 then break end
        if cp < 0x80 then
            out[#out+1] = string.char(cp)
        elseif cp < 0x800 then
            out[#out+1] = string.char(0xC0 + math.floor(cp / 0x40))
            out[#out+1] = string.char(0x80 + (cp % 0x40))
        else
            out[#out+1] = string.char(0xE0 + math.floor(cp / 0x1000))
            out[#out+1] = string.char(0x80 + math.floor((cp % 0x1000) / 0x40))
            out[#out+1] = string.char(0x80 + (cp % 0x40))
        end
    end
    return table.concat(out)
end

-- FFXI menu-option text uses a custom glyph encoding distinct from
-- ASCII. Each glyph is one byte (the "wide char" high byte is always
-- 0). The encoding has three regions, mapped by the decoder below:
--
--   0x21..0x3A = capital letters A-Z, algebraic: letter = code - 0x20
--                (so 0x21='A', 0x29='I', 0x33='S', 0x37='W', 0x3A='Z')
--   0x10..0x19 = digits 0-9, algebraic: digit = code - 0x10
--                (so 0x10='0', 0x11='1' .. 0x19='9'). Used for prices
--                and ranks: "1000-pt. items (rank 1 required)" encodes
--                as 11 10 10 10 0D ... 11.
--   0x41..0x5A = ASCII A-Z bytes that the FFXI font draws as
--                LOWERCASE a-z. The decoder lowercases them so the
--                output string matches what the player sees.
--
-- The explicit table below covers punctuation and other glyphs that
-- don't fit either algebraic mapping. Verified empirically against
-- Rabid Wolf, I.M.'s main menu and conquest items sub-menu (2026-05-02).
local FFXI_GLYPH = {
    [0x00] = ' ',   -- inline space (also null terminator at end)
    [0x07] = '\n',  -- newline
    [0x08] = '(',   -- open paren
    [0x09] = ')',   -- close paren
    [0x0C] = ',',   -- comma
    [0x0D] = '-',   -- hyphen (verified in "1000-pt. items")
    [0x0E] = '.',   -- period
    [0x1F] = '?',   -- question mark
}

-- Decode FFXI menu-option text starting at `addr`. The text begins at
-- addr+0x04 (4-byte header). Each char is a uint16 little-endian
-- where the HIGH byte is always 0 for valid FFXI text bytes
-- (including the custom-cap and punctuation glyphs); a non-zero
-- high byte means we've run off the end of the label and are
-- reading widget scratch (e.g. an asset path written as raw 8-bit
-- ASCII, which would have the next char's byte in the high half).
-- Two consecutive 0x0000 wide chars also terminate (single 0x0000
-- is an inline space between words).
local function read_option_label(addr)
    if not addr or addr == 0 then return '' end
    -- Detect the widget shape from its FIRST visible char (after the
    -- 4-byte prefix). Templates that start with a digit code
    -- (0x10..0x19) are price/rank labels like "1000-pt. items (rank
    -- 1 required)." - those have legitimate mid-text periods (the
    -- "pt.") and need the relaxed "period+null+period" terminator
    -- to stay intact. Everything else (item names with FE FE
    -- markers, plain navigation buttons like "Next page." / "Back.",
    -- dialog options like "Would you cast Signet on me?") can use a
    -- TIGHT terminator that breaks on the first sentence-ending
    -- punctuation followed by a null. This catches stale buffer
    -- leftovers from short-text widgets that got allocated into
    -- previously-longer buffers (the rendered text really IS just
    -- "Next page." but the bytes after are stale "tems (rank 7
    -- required)." from the previous menu's widget that lived in
    -- this slot).
    local first_byte = pcget(function()
        return ashita.memory.read_uint8(addr + 0x04)
    end) or 0
    local is_template = (first_byte >= 0x10 and first_byte <= 0x19)
    local out = {}
    local last_was_null = false
    local last_was_period = false
    for i = 0, 255 do
        local cp = pcget(function()
            return ashita.memory.read_uint16(addr + 0x04 + i * 2)
        end)
        if cp == nil then break end
        local low  = cp % 256
        local high = math.floor(cp / 256)
        -- 0xFEFE / 0xFFFE are FFXI text-style escape markers used to
        -- bracket "highlighted" text (item names, key items, etc. -
        -- the colored phrases you see in chat). Item-name DAT
        -- substitutions come back as `FE FE <cap-encoded text> FF FE`.
        -- Skip the markers entirely; the text inside decodes normally
        -- against our cap/digit/punctuation tables.
        if cp == 0xFEFE or cp == 0xFFFE then
            -- zero-width: consume and continue
        elseif high ~= 0 then
            -- Past end of label (raw 8-bit data follows).
            break
        elseif low == 0 then
            if last_was_null and #out > 0 then break end
            if last_was_period then
                if not is_template then
                    -- Tight terminator: any null after sentence-end
                    -- punctuation is the real end (stale leftovers
                    -- from longer previous widgets get cut here).
                    break
                end
                -- Template widgets ("1000-pt. items (rank N required).")
                -- have a legitimate mid-text period in "pt.", so we
                -- can't break on every period+null. Only break on
                -- the "period, null, period" pattern (which marks
                -- "real text ended . metadata starts ."). Peek ahead.
                local peek = pcget(function()
                    return ashita.memory.read_uint16(addr + 0x04 + (i + 1) * 2)
                end)
                if peek == 0x000E then break end
            end
            last_was_null = true
            last_was_period = false
            out[#out+1] = ' '
        else
            last_was_null = false
            -- Treat both period (0x0E) and question mark (0x1F) as
            -- sentence enders for the "stop on null after end-of-
            -- sentence" terminator above.
            last_was_period = (low == 0x0E or low == 0x1F)
            local mapped = FFXI_GLYPH[low]
            if mapped ~= nil then
                out[#out+1] = mapped
            elseif low >= 0x10 and low <= 0x19 then
                -- Digit range: 0x10='0'..0x19='9'.
                out[#out+1] = string.char(0x30 + (low - 0x10))
            elseif low >= 0x21 and low <= 0x3A then
                -- Cap-letter range: 0x21='A'..0x3A='Z'.
                out[#out+1] = string.char(0x40 + (low - 0x20))
            elseif low >= 0x20 and low <= 0x7E then
                -- ASCII range: cap A-Z bytes are rendered as
                -- lowercase by FFXI's font; lowercase here so the
                -- visible string matches what a player sees.
                if low >= 0x41 and low <= 0x5A then
                    out[#out+1] = string.char(low + 0x20)
                else
                    out[#out+1] = string.char(low)
                end
            else
                out[#out+1] = '?'
            end
        end
    end
    local s = table.concat(out)
    return (s:gsub('%s+$', ''))
end

-- Read the option labels from the cursor_struct's child-widget list.
-- Layout (verified 2026-05-02 against Rabid Wolf, I.M. main menu):
--   cursor_struct + 0x14  ptr  head of doubly-linked widget list
--   each list node:
--     +0x00  ptr  next
--     +0x04  ptr  prev
--     +0x10  ptr  payload (the widget itself)
--     +0x2C  u8   master option index (1-based; the value the client's
--                 +0x548 submit uses to identify which option got
--                 picked - INCLUDES rank-locked / hidden options that
--                 the player can't actually see, so visible position
--                 N != master index N for filtered menus)
--     ...0x30 bytes total
--   each widget payload:
--     +0x00  4 bytes  header
--     +0x04  ...      FFXI-encoded label text (see read_option_label)
--
-- The first `count` nodes correspond to options 0..count-1 in order.
-- (We do NOT trust stride=0x120 across menus - some menus skip it -
-- so we walk the linked list explicitly.)
--
-- Returns two parallel arrays: labels and master_indices. master_indices
-- is the value to pass to submit_menu_pick (after subtracting 1, since
-- submit writes +0x548 = master_idx + 1). Both are 1-indexed Lua tables.
local function read_option_labels(cursor_struct, count)
    local labels = {}
    local master = {}
    if not cursor_struct or cursor_struct == 0 or not count or count <= 0 then
        return labels, master
    end
    local node = pcget(function()
        return ashita.memory.read_uint32(cursor_struct + 0x14)
    end)
    if node == nil or node == 0 then return labels, master end
    for i = 1, count do
        local payload = pcget(function()
            return ashita.memory.read_uint32(node + 0x10)
        end)
        if payload == nil or payload == 0 then break end
        labels[i] = read_option_label(payload)
        -- Master option index lives at node + 0x2C (1-based). The
        -- client's +0x548 submit treats (flag - 1) as the picked
        -- option, so we record the 1-based value directly. Falls back
        -- to the visible position if the node isn't shaped like an
        -- option-list widget (e.g. text frames).
        master[i] = pcget(function()
            return ashita.memory.read_uint8(node + 0x2C)
        end) or i
        local next_node = pcget(function()
            return ashita.memory.read_uint32(node + 0x00)
        end)
        if next_node == nil or next_node == 0 then break end
        node = next_node
    end
    return labels, master
end

-- Read the live menu state from client memory. Returns nil when no
-- menu is open (chain doesn't resolve), otherwise a table with:
--   prompt        - the dialog title / current text frame, UTF-8
--   cursor        - current cursor position (0-indexed)
--   count         - number of visible options
--   menu_base     - debug: heap addr of the menu_base struct
--   cursor_struct - debug: heap addr of the cursor_struct
--   option_array  - debug: heap addr of the option array
--
-- Offsets verified empirically 2026-05-01:
--   cursor_struct + 0x24  uint32  visible option count
--   cursor_struct + 0x30  uint8   cursor 0-indexed (read by input handler on Enter)
--   cursor_struct + 0x34  UTF-16LE wide-char  prompt text (e.g. "WHICH ITEM DO YOU WISH TO PURCHASE")
-- Map known cursor_struct vtable pointers to a menu kind. The vtable
-- (cursor_struct + 0x00) is set by the FFXI client when it constructs
-- the dialog widget; different menu types use different C++ classes.
-- Verified 2026-05-02:
--   0x01D84280 = option list (multi-choice menu, e.g. "Spend conquest
--                points / Get signet / ...")
--   0x01D829A0 = confirmation/preview text frame (e.g. "Are you sure
--                you want to purchase X?" - press Enter to advance to
--                a real Yes/No menu)
-- Other vtables we encounter in the wild become 'unknown'; the
-- director can fall back to label-shape heuristics.
local MENU_KIND_BY_VTABLE = {
    [0x01D84280] = 'option_list',
    [0x01D829A0] = 'text_frame',
}

local function read_menu_state()
    local chain = walk_menu_chain()
    if chain == nil or chain.cursor_struct == nil or chain.cursor_struct == 0 then
        return nil
    end
    local cs = chain.cursor_struct
    local vtable = pcget(function() return ashita.memory.read_uint32(cs + 0x00) end) or 0
    local kind = MENU_KIND_BY_VTABLE[vtable] or 'unknown'
    local cursor = pcget(function() return ashita.memory.read_uint8(cs + 0x30) end) or 0
    local count  = pcget(function() return ashita.memory.read_uint32(cs + 0x24) end) or 0
    -- Prompt has the same 4-byte header / FFXI-glyph encoding as
    -- option labels. read_option_label takes the widget head (text
    -- starts at +0x04 from there); for the prompt we pass cs+0x34.
    local prompt = read_option_label(cs + 0x34)
    local options, master_indices = read_option_labels(cs, count)
    return {
        prompt          = prompt,
        kind            = kind,
        vtable          = vtable,
        master_indices  = master_indices,
        cursor        = cursor,
        count         = count,
        options       = options,
        menu_base     = chain.menu_base,
        cursor_struct = cs,
        option_array  = chain.option_array,
    }
end

-- Translate a visible option position (0-indexed, what the agent
-- and player see) to the master option index the client uses for
-- 0x05B + per-frame submit. Filtered menus (rank-locked items,
-- conditional dialogue) hide some master entries from the visible
-- list, so visible position N != master index N. Each widget node
-- carries its master index at +0x2C; we walk the list to find the
-- N-th visible widget and read its master.
--
-- Returns the 1-based master index (suitable for direct write to
-- +0x548). Falls back to (visible + 1) when the chain isn't a
-- shaped option list, so menus that don't filter (most plain
-- dialogs) still work without per-menu calibration.
local function visible_to_master(cursor_struct, visible_idx)
    local default_flag = math.max(1, math.min(0xFE, visible_idx + 1))
    if not cursor_struct or cursor_struct == 0 then
        return default_flag
    end
    local node = pcget(function()
        return ashita.memory.read_uint32(cursor_struct + 0x14)
    end)
    if node == nil or node == 0 then return default_flag end
    for i = 0, visible_idx - 1 do
        local nxt = pcget(function()
            return ashita.memory.read_uint32(node + 0x00)
        end)
        if nxt == nil or nxt == 0 then return default_flag end
        node = nxt
    end
    local master = pcget(function()
        return ashita.memory.read_uint8(node + 0x2C)
    end)
    if master == nil or master == 0 then return default_flag end
    return master
end

local function submit_menu_pick(index)
    local pm = AshitaCore:GetPointerManager()
    if pm == nil then return false end
    local p0 = pm:Get('menu')
    if p0 == nil or p0 == 0 then return false end
    local p1 = pcget(function() return ashita.memory.read_uint32(p0) end)
    if p1 == nil or p1 == 0 then return false end
    local menu_base = pcget(function() return ashita.memory.read_uint32(p1) end)
    if menu_base == nil or menu_base == 0 then return false end

    local cursor_struct = pcget(function()
        return ashita.memory.read_uint32(menu_base + 0x0C)
    end)
    if cursor_struct == nil or cursor_struct == 0 then return false end

    -- Flag = visible_position + 1. The client's per-frame consumer at
    -- FFXIMain 0x01B02909 reads this as (flag - 1) = the picked
    -- visible option, then dispatches its own per-NPC option lookup.
    --
    -- DO NOT use widget +0x2C as the flag value. Earlier exploration
    -- found the widget node has a uint8 at +0x2C that varies per
    -- widget and tracked closely with master indices on the items
    -- list / Republic Signet staff test, suggesting it was the
    -- "right" submit value. But cross-menu testing shows it's
    -- actually a UI sequence number / row-id that doesn't map to
    -- the per-frame consumer's option-lookup index. For the main
    -- conversation menu the +0x2C values are [11, 12, 14, 15, 16]
    -- and writing those values either no-ops or closes the dialog;
    -- only flag = visible+1 (i.e. [1, 2, 3, 4, 5]) advances correctly.
    --
    -- Useful range: 1..0xFE. 0xFF crashes the client (special path).
    --
    -- For HIDDEN options (rank-locked items, etc.) that aren't in
    -- the visible widget list, the flag has to be hand-picked from
    -- the option_array indices - that path is exposed via
    -- /interact pickraw <hex_flag> for testing.
    local flag = (tonumber(index) or 0) + 1
    flag = math.max(1, math.min(0xFE, flag))

    return pcall(function()
        ashita.memory.write_uint16(cursor_struct + 0x548, flag)
    end)
end

-- Diagnostic-only: write the input handler's cursor mirror at
-- cursor_struct + 0x30. Tested 2026-05-02: this byte tracks the
-- current cursor position from the input handler's perspective, but
-- writing it does NOT move the visible cursor highlight and does
-- NOT change which option Enter selects. The visible cursor /
-- input-target lives somewhere else we haven't located. Use
-- submit_menu_pick (+0x548 write) for the real "act like a player"
-- pick path - it goes through the client's per-frame submit loop
-- which handles cursor + option_index lookup + 0x05B send + UI
-- dismiss in one shot.
local function write_cursor_index(idx)
    local chain = walk_menu_chain()
    if chain == nil or chain.cursor_struct == nil or chain.cursor_struct == 0 then
        return false
    end
    local v = math.max(0, math.min(0xFF, tonumber(idx) or 0))
    return pcall(function()
        ashita.memory.write_uint8(chain.cursor_struct + 0x30, v)
    end)
end

-- Backwards-compatible alias for the older "set cursor mirrors"
-- approach. New code should call submit_menu_pick directly.
local function write_cursor_state(index)
    return submit_menu_pick(index)
end

-- Pick a menu option (or advance a text-only frame) by sending 0x05B
-- (DIALOG_CHOICE) with the EXACT field layout used by FFXI's client
-- when the player manually clicks. Per Windower's packet schema for
-- outgoing 0x05B (verified against fields.lua):
--
--   offset  size  field              notes
--   0x04    u32   Target             NPC server id
--   0x08    u16   Option Index       0-indexed master option
--   0x0A    u16   _unknown1          ALWAYS 0x4000 for player picks
--   0x0C    u16   Target Index       NPC entity index
--   0x0E    u8    Automated Message  0 = manual click, 1 = auto-generated
--   0x0F    u8    _unknown2          0
--   0x10    u16   Zone               player's zone id
--   0x12    u16   Menu ID            from incoming 0x032 / 0x034
--
-- Total body: 16 bytes.
--
-- The pre-2026-05-01 code path had THREE bugs in the field layout:
--   - Wrote Option Index as u32 (0x08-0x0B), CLOBBERING _unknown1 to 0
--   - _unknown1 stayed 0 instead of 0x4000 (the magic "real player click"
--     value the server expects)
--   - Wrote a u16 "Mode" at 0x0E spanning the bool Automated Message AND
--     _unknown2; setting "mode=1" meant Automated Message=1 (the server
--     thought our packets were auto-generated, not real clicks)
--
-- With these bugs fixed, the addon's 0x05B looks byte-identical to a
-- real player click. This handles BOTH menu picks AND text-frame
-- advances - text advance is just a 0x05B with Option Index=0 and the
-- right Menu ID. Verified pattern matches NpcInteract addon's capture
-- + replay approach.
-- Low-level: send a 0x05B with explicit option_index and
-- automated_message values. All other fields populated from the
-- captured menu context (npc_sid, target_index, menu_id, zone).
-- Used by both action_select_option (the agent path) and the
-- /interact pick command (manual testing path).
local function send_dialog_pick(option_index, automated_message)
    if menu.event_id == 0 or menu.npc_sid == 0 then
        msg(('send_dialog_pick: no active event captured (no incoming '
             .. '0x032/0x034 seen). Talk to an NPC first.')
            :format())
        return false
    end
    local pm = AshitaCore:GetPacketManager()
    if pm == nil then
        msg('send_dialog_pick: no packet manager?')
        return false
    end

    local opt_idx   = tonumber(option_index) or 0
    local auto_msg  = (automated_message and automated_message ~= 0) and 1 or 0
    local sid       = menu.npc_sid
    local act_idx   = menu.event_act_index
    local event_id  = menu.event_id
    local zone_id   = pcget(function()
        return AshitaCore:GetMemoryManager():GetParty():GetMemberZone(0)
    end) or 0

    pm:QueuePacket(0x5B, 0x14, 0x00, 0x00, 0x00, function(ptr)
        local p = ffi.cast('u8*', ptr)
        ffi.fill(p + 0x04, 0x10)                       -- zero the 16 body bytes
        ffi.cast('uint32_t*', p + 0x04)[0] = sid       -- Target (u32)
        ffi.cast('uint16_t*', p + 0x08)[0] = opt_idx   -- Option Index (u16, can be 0x80A1-style)
        ffi.cast('uint16_t*', p + 0x0A)[0] = 0         -- _unknown1 (LSB: 0; retail uses 0x4000)
        ffi.cast('uint16_t*', p + 0x0C)[0] = act_idx   -- Target Index (u16)
        p[0x0E] = auto_msg                             -- Automated Message (u8 bool)
        p[0x0F] = 0                                    -- _unknown2 (u8)
        ffi.cast('uint16_t*', p + 0x10)[0] = zone_id   -- Zone (u16)
        ffi.cast('uint16_t*', p + 0x12)[0] = event_id  -- Menu ID (u16)
    end)
    msg(('pick: queued 0x05B option=0x%04X auto=%d Menu_ID=%d zone=%d')
        :format(opt_idx, auto_msg, event_id, zone_id))
    return true
end

local function action_select_option(index, mode)
    -- Backwards-compatible agent path. Always sends with
    -- automated_message=0 (manual click). The `mode` arg is now
    -- ignored - it was a leftover misinterpretation of the bytes
    -- at offset 0x0E (which is actually Automated Message u8 +
    -- _unknown2 u8, not a u16 "Mode" field).
    return send_dialog_pick(index, 0)
end

-- Build + send 0x083 (CLI SHOP_BUY). Layout from LSB
-- src/map/packets/c2s/0x083_shop_buy.h:
--   header (4):              id=0x83, size=4 dwords (byte 1 = 0x08)
--   ItemNum            (4):  quantity to buy
--   ShopNo             (2):  shop ID (zero is fine for most cases;
--                            server tracks shop context server-side)
--   ShopItemIndex      (2):  index into the vendor's stock list
--   PropertyItemIndex  (1):  property/category index (0 for normal)
--   padding00          (3):  zeroed
--
-- Total = 4 header + 12 body = 16 bytes (4 dwords).
local function action_buy(item_index, qty)
    local idx = tonumber(item_index) or -1
    qty       = tonumber(qty) or 1
    if idx < 0 then
        msg('buy: missing item_index')
        return false
    end
    local pm = AshitaCore:GetPacketManager()
    if pm == nil then return false end
    -- Total = 4 header + 12 body = 16 bytes. Body: ItemNum(4) +
    -- ShopNo(2) + ShopItemIndex(2) + PropertyItemIndex(1) + pad(3).
    pm:QueuePacket(0x83, 0x10, 0x00, 0x00, 0x00, function(ptr)
        local p = ffi.cast('u8*', ptr)
        ffi.fill(p + 0x04, 0x0C)                  -- zero the body
        ffi.cast('u32*', p + 0x04)[0] = qty       -- ItemNum (qty)
        -- ShopNo at offset 8 stays 0 (server tracks shop context)
        ffi.cast('u16*', p + 0x0A)[0] = idx       -- ShopItemIndex
        -- PropertyItemIndex (u8) at 0x0C + 3 bytes padding stay zero
    end)
    msg(('buy: queued 0x083 (shop_index=%d, qty=%d)'):format(idx, qty))
    return true
end

-- Sell is a TWO-STEP flow in FFXI:
--   1. Client sends 0x084 (SHOP_SELL_REQ) with the inventory item to
--      "appraise" - server responds with the price via 0x03D.
--   2. Client sends 0x085 (SHOP_SELL_SET) with SellFlag=1 to confirm.
-- We implement only step (2) here; the orchestrator does step (1)
-- via a /sell <item> chat command (which is built-in FFXI). Once the
-- appraisal dialog is up, this confirms the sale.
local function action_sell(item_index, qty)
    -- TODO(phase-c): full two-step implementation. For now this
    -- is a stub - see comment above for the protocol shape.
    msg(('sell(%s, %d) - two-step protocol not yet implemented')
        :format(tostring(item_index), tonumber(qty) or 1))
    return false
end

-- Close any open event/menu cleanly. Sends 0x05B Mode=End (0) with
-- EndPara=0 + EventPara=0, which the server interprets as "the player
-- closed the menu without picking an actionable option" and runs
-- onEventFinish with result=0 (or the previously-stored option if the
-- player picked one earlier with Mode=UpdatePending). Used both by
-- explicit close requests AND by the director's stop() path so a
-- failed leaf doesn't leave a dialog box stuck on the player's screen.
local function action_close_menu()
    if menu.event_id == 0 or menu.npc_sid == 0 then
        -- No active event captured - nothing for the server to close.
        -- Fall back to Enter injection in case the menu is a passive
        -- "OK" dialog that's not formally an event server-side.
        return action_advance_text()
    end
    local pm = AshitaCore:GetPacketManager()
    if pm == nil then return false end
    local sid       = menu.npc_sid
    local act_idx   = menu.event_act_index
    local event_id  = menu.event_id
    local zone_id = pcget(function()
        return AshitaCore:GetMemoryManager():GetParty():GetMemberZone(0)
    end) or 0
    pm:QueuePacket(0x5B, 0x14, 0x00, 0x00, 0x00, function(ptr)
        local p = ffi.cast('u8*', ptr)
        ffi.fill(p + 0x04, 0x10)                  -- zero the body
        ffi.cast('u32*', p + 0x04)[0] = sid       -- UniqueNo
        ffi.cast('u32*', p + 0x08)[0] = 0         -- EndPara=0 (no pick)
        ffi.cast('u16*', p + 0x0C)[0] = act_idx   -- ActIndex
        ffi.cast('u16*', p + 0x0E)[0] = 0         -- Mode=0 (End)
        ffi.cast('u16*', p + 0x10)[0] = zone_id   -- EventNum (zone)
        ffi.cast('u16*', p + 0x12)[0] = event_id  -- EventPara (event id)
    end)
    msg(('close_menu: queued 0x05B End (event_id=%d zone=%d)')
        :format(event_id, zone_id))
    -- Also clear local state so we don't double-fire if the server's
    -- 0x052 close response is delayed.
    reset_menu()
    return true
end

-- Build + send 0x01A (CLI_COMMAND_ACTION) with ActionID=0x00 (Talk).
-- This is what the FFXI client sends when the player presses Enter
-- on a targeted NPC to initiate dialog. The TkEventMsg2 Enter trick
-- (used for advance_text) only advances ALREADY-OPEN dialog text;
-- it doesn't start a fresh interaction.
--
-- Layout from LSB src/map/packets/c2s/0x01a_action.h:
--   header (4):              id=0x1A, size=7 dwords (byte 1 = 0x0E)
--   UniqueNo  (4):           target NPC server id
--   ActIndex  (2):           target NPC actor index (in entity mgr)
--   ActionID  (2):           0x0000 (Talk)
--   ActionBuf (16):          union, zeroed for Talk action
-- Total = 4 header + 4 + 2 + 2 + 16 = 28 bytes (7 dwords).
local function action_open_dialog(target_sid)
    if target_sid == nil or target_sid == 0 then
        msg('open_dialog: no target_sid')
        return false
    end
    local act_index, npc_name = lookup_entity(target_sid)
    if act_index == 0 then
        msg(('open_dialog(sid=%d): NPC not found in entity manager (out of range?)')
            :format(target_sid))
        return false
    end
    local pm = AshitaCore:GetPacketManager()
    if pm == nil then return false end
    -- Total = 4 header + 24 body = 28 bytes (0x1C). Body: UniqueNo(4)
    -- + ActIndex(2) + ActionID(2) + ActionBuf(16) = 24.
    pm:QueuePacket(0x1A, 0x1C, 0x00, 0x00, 0x00, function(ptr)
        local p = ffi.cast('u8*', ptr)
        ffi.fill(p + 0x04, 0x18)                       -- zero the body
        ffi.cast('u32*', p + 0x04)[0] = target_sid     -- UniqueNo
        ffi.cast('u16*', p + 0x08)[0] = act_index      -- ActIndex
        ffi.cast('u16*', p + 0x0A)[0] = 0              -- ActionID = Talk
        -- ActionBuf (16 bytes at offset 0x0C..0x1B) stays zeroed
    end)
    msg(('open_dialog: queued 0x01A Talk to %q (sid=%d, act_idx=%d)')
        :format(npc_name, target_sid, act_index))
    return true
end

local function dispatch_action(act)
    if type(act) ~= 'table' then return end
    local kind = act.action
    if kind == 'advance_text' then
        action_advance_text()
    elseif kind == 'select_option' then
        -- mode is optional: the orchestrator passes it when it needs
        -- Mode=0 (End - close the event entirely) instead of the
        -- default Mode=1 (UpdatePending - keep going).
        action_select_option(act.index, act.mode)
    elseif kind == 'buy' then
        -- act.index is the position in the vendor's stock list;
        -- the orchestrator resolves item name -> index via the
        -- LSB-mined catalog before dispatching this action.
        action_buy(act.index, act.qty or 1)
    elseif kind == 'sell' then
        action_sell(act.index, act.qty or 1)
    elseif kind == 'close_menu' then
        action_close_menu()
    elseif kind == 'escape' then
        -- Best-effort fallback: inject FFXI's cancel key. Used by
        -- the orchestrator's failure path so a wedged dialog gets
        -- dismissed without manual user intervention.
        if not inject_key(KEY_ESCAPE) then
            msg('escape: TkEventMsg2 injection failed')
        else
            msg(('escape: injected key=%d'):format(KEY_ESCAPE))
        end
    elseif kind == 'open_dialog' then
        action_open_dialog(act.target_sid)
    elseif kind == 'set_cursor' then
        -- Memory-only cursor write; no packet sent. Used for script
        -- entries like {key: down, n: N} that adjust cursor without
        -- picking. The next select_option call will operate on the
        -- written cursor.
        local idx = tonumber(act.index) or 0
        if write_cursor_state(idx) then
            msg(('set_cursor(%d) -> wrote memory mirrors'):format(idx))
        else
            msg(('set_cursor(%d) -> FAILED (menu pointer chain broken?)'):format(idx))
        end
    elseif kind == 'select_by_input' then
        -- Phase C1: act like a player. Move the visible cursor to
        -- act.index then inject Enter through TkEventMsg2. The client
        -- computes the option_index and sends 0x05B itself, so we
        -- don't need to know the per-NPC encoding (e.g. 0x80A1 for
        -- conquest items). Also dismisses the UI cleanly because
        -- input flows through the real handler.
        action_select_by_input(tonumber(act.index) or 0)
    else
        msg('unknown action: ' .. tostring(kind))
    end
end

-------------------------------------------------------------------------------
-- Action poll
-------------------------------------------------------------------------------

local function action_path()
    local char = state.last_character
    if char == nil or char == '' then return nil end
    return get_data_path() .. 'commands/' .. char .. '/interact.json'
end

local function poll_actions()
    local path = action_path()
    if path == nil then return end
    local f = io.open(path, 'r')
    if not f then return end
    local body = f:read('*a') or ''
    f:close()
    -- Drain by deletion: the orchestrator writes a fresh file per
    -- action and we consume it once. This matches the nav_request.json
    -- contract used elsewhere.
    os.remove(path)
    if body == '' then return end
    -- The file may contain one or more JSON action objects on
    -- separate lines (matches the schema in the plan). Process each.
    for line in body:gmatch('[^\r\n]+') do
        local ok, parsed = pcall(json.decode, line)
        if ok and type(parsed) == 'table' then
            dispatch_action(parsed)
        end
    end
end

-------------------------------------------------------------------------------
-- State publish
-------------------------------------------------------------------------------
-- (state declared near the top of the file so the packet-hook closures
-- can capture it via lexical scoping.)

local function publish()
    local char = state.last_character
    if char == nil or char == '' then return end
    local path = get_data_path() .. 'state/' .. char .. '/menu.json'

    -- Memory is the source of truth for menu state. The packet-derived
    -- menu.open flag is unreliable across addon reloads (we missed the
    -- 0x032/0x034 packets that were sent before we loaded), so always
    -- probe the pointer chain. If it resolves, a menu IS open.
    local mem = read_menu_state()
    local is_open = menu.open or (mem ~= nil)

    local prompt = menu.prompt
    local cursor = menu.cursor
    local options = menu.options
    local kind = menu.kind
    local master_indices = nil
    local mem_count = nil
    local mem_menu_base = nil
    local mem_cursor_struct = nil
    if mem ~= nil then
        if mem.prompt ~= nil and mem.prompt ~= '' then prompt = mem.prompt end
        cursor = mem.cursor
        mem_count = mem.count
        mem_menu_base = mem.menu_base
        mem_cursor_struct = mem.cursor_struct
        if mem.options ~= nil and #mem.options > 0 then
            options = mem.options
            master_indices = mem.master_indices
        end
        -- Memory-derived kind (from cursor_struct vtable) takes
        -- precedence over the packet-history-derived kind, which can
        -- be stale across menu transitions within a single event.
        if mem.kind ~= nil and mem.kind ~= 'unknown' then
            kind = mem.kind
        end
    end

    local payload = {
        ts             = os.time(),
        open           = is_open,
        kind           = kind,
        npc_name       = menu.npc_name,
        npc_sid        = menu.npc_sid,
        prompt         = prompt,
        options        = options,             -- Phase B: from cursor_struct + 0x14 widget list
        master_indices = master_indices,      -- 1-based master option indices per visible option
        cursor         = cursor,
        option_count   = mem_count,           -- Phase B0: from cursor_struct + 0x24
        vendor_items   = menu.vendor_items,
        -- Debug pointers so the orchestrator can verify the chain
        -- without re-walking. Unset when no menu is open.
        menu_base     = mem_menu_base and ('0x%08X'):format(mem_menu_base) or nil,
        cursor_struct = mem_cursor_struct and ('0x%08X'):format(mem_cursor_struct) or nil,
    }
    write_json_atomic(path, payload)
    menu.dirty = false
    state.last_publish_frame = state.frame
end

-------------------------------------------------------------------------------
-- Ashita events
-------------------------------------------------------------------------------

ashita.events.register('load', 'interact_load', function()
    init_injection()
    msg('Loaded v' .. addon.version
        .. ' - state -> menu.json, actions <- interact.json'
        .. (injection.available and '' or ' (key injection DISABLED)'))
end)

ashita.events.register('command', 'interact_command', function(e)
    local args = e.command:args()
    if #args == 0 or args[1] ~= '/interact' then return end
    e.blocked = true
    local sub = args[2] or ''
    if sub == 'debug' then
        debug_capture = not debug_capture
        msg('packet debug logging ' .. (debug_capture and 'ON' or 'OFF'))
    elseif sub == 'mem' then
        -- Read the live menu state from client memory and dump it to
        -- chat. Verifies the pointer chain + read_menu_state() works
        -- before we trust it for the LLM-driven flow.
        local mem = read_menu_state()
        if mem == nil then
            msg('mem: no menu open (pointer chain broken or no event active)')
        else
            msg(('mem: menu_base=0x%08X cursor_struct=0x%08X option_array=0x%08X'):format(
                mem.menu_base or 0, mem.cursor_struct or 0, mem.option_array or 0))
            msg(('mem: cursor=%d count=%d'):format(mem.cursor, mem.count))
            local p = mem.prompt or ''
            -- FFXI chat truncates around 150 chars; trim hard.
            if #p > 140 then p = p:sub(1, 137) .. '...' end
            msg(('mem: prompt=%q'):format(p))
            for i, label in ipairs(mem.options or {}) do
                local marker = (i - 1 == mem.cursor) and '>' or ' '
                local s = label
                if #s > 120 then s = s:sub(1, 117) .. '...' end
                msg(('mem: %s [%d] %s'):format(marker, i - 1, s))
            end
        end
    elseif sub == 'navigate' then
        -- Phase C1 manual test: write the cursor index without
        -- pressing Enter, so we can watch the visible highlight move.
        -- Usage: /interact navigate <idx>
        local idx = tonumber(args[3])
        if idx == nil then
            msg('usage: /interact navigate <idx>')
        elseif write_cursor_index(idx) then
            msg(('navigate: cursor -> %d'):format(idx))
        else
            msg('navigate: write failed (no menu open?)')
        end
    elseif sub == 'confirm' then
        -- Phase C1 manual test: inject Enter at the current cursor
        -- without first repositioning. The client computes the
        -- option_index for the current cursor and submits.
        if inject_key(KEY_ENTER) then
            msg('confirm: Enter injected')
        else
            msg('confirm: inject_key(ENTER) failed')
        end
    elseif sub == 'pickinput' then
        -- Phase C1 manual test: composite "act like a player" pick.
        -- Writes cursor to <idx> then injects Enter. The client does
        -- the rest (computes option_index, sends 0x05B, dismisses UI).
        -- Usage: /interact pickinput <idx>
        local idx = tonumber(args[3])
        if idx == nil then
            msg('usage: /interact pickinput <idx>')
        else
            action_select_by_input(idx)
        end
    elseif sub == 'pick' then
        -- Manual menu pick. Usage:
        --   /interact pick <hex_option_index> [automated]
        -- Examples:
        --   /interact pick 80A1 1   -- buy instant warp, first packet (auto-confirm)
        --   /interact pick 80A1 0   -- buy instant warp, second packet (commit)
        --   /interact pick 0        -- pick option 0 with automated=0 (default)
        if not args[3] then
            msg('usage: /interact pick <hex_option_index> [automated_msg]')
            msg('  example: /interact pick 80A1 1  (then 80A1 0 to confirm)')
        else
            local opt = tonumber(args[3], 16)
            local auto = tonumber(args[4]) or 0
            if opt == nil then
                msg(('pick: invalid hex option_index: %q'):format(args[3]))
            else
                send_dialog_pick(opt, auto)
            end
        end
    elseif sub == 'capture' then
        local op = args[3] or 'status'
        if op == 'start' then
            local fname = args[4] or 'capture.jsonl'
            capture_path = get_data_path() .. 'state/' .. fname
            -- Truncate any prior file at this path so each run is clean.
            local f = io.open(capture_path, 'w')
            if f then f:close() end
            capture_count = 0
            capture_active = true
            msg(('capture START -> %s'):format(capture_path))
        elseif op == 'stop' then
            capture_active = false
            msg(('capture STOP (%d packets written to %s)'):format(
                capture_count, tostring(capture_path)))
        elseif op == 'status' then
            msg(('capture: %s, count=%d, path=%s'):format(
                capture_active and 'ON' or 'OFF',
                capture_count, tostring(capture_path)))
        else
            msg('usage: /interact capture { start [filename] | stop | status }')
        end
    elseif sub == 'state' then
        msg(('open=%s kind=%s npc=%q sid=%d opts=%d')
            :format(tostring(menu.open), menu.kind,
                    menu.npc_name, menu.npc_sid, #menu.options))
    elseif sub == 'enter' then
        action_advance_text()
    elseif sub == 'escape' then
        -- Inject FFXI's cancel/back key. Useful when a stuck client-
        -- side dialog won't close in response to outgoing packets
        -- (server already considers the event closed but the client
        -- UI still shows the dialog box).
        if not inject_key(KEY_ESCAPE) then
            msg('escape: TkEventMsg2 injection failed')
        else
            msg(('escape: injected key=%d'):format(KEY_ESCAPE))
        end
    elseif sub == 'key' then
        -- Inject an arbitrary TkEventMsg2 keycode. For empirical
        -- discovery of the FFXI internal mapping (5=enter is known;
        -- others not yet cataloged). Usage: /interact key <N>
        local k = tonumber(args[3])
        if k == nil then
            msg('usage: /interact key <number>')
        elseif inject_key(k) then
            msg(('key: injected keycode=%d'):format(k))
        else
            msg('key: injection failed')
        end
    elseif sub == 'pad' then
        -- Simulate a gamepad button press via Ashita's IXInput.
        -- FFXI accepts gamepad input alongside keyboard, and XInput
        -- is a separate subsystem from DirectInput - so even though
        -- DInput key injection failed under Wine, XInput button
        -- injection might land. FFXI menu UI accepts:
        --   A button (0x1000) = Confirm  (like Enter)
        --   B button (0x2000) = Cancel   (like Escape)
        --   D-pad Up/Down (0x0001/0x0002) = cursor navigation
        -- Usage:  /interact pad confirm | cancel | up | down
        --         /interact pad <button_hex> [state]
        local k = args[3]
        if k == nil then
            msg('usage: /interact pad <name|hex>  (name: confirm|cancel|up|down|left|right|start|back)')
            return
        end
        local PAD = {
            up      = 0x0001,
            down    = 0x0002,
            left    = 0x0004,
            right   = 0x0008,
            start   = 0x0010,
            back    = 0x0020,
            confirm = 0x1000,   -- A
            a       = 0x1000,
            cancel  = 0x2000,   -- B
            b       = 0x2000,
            x       = 0x4000,
            y       = 0x8000,
        }
        local btn = PAD[k:lower()] or tonumber(k)
        if btn == nil then
            msg(('pad: unknown button %q'):format(k))
            return
        end
        local ok = pcall(function()
            local xin = AshitaCore:GetInputManager():GetXInput()
            -- state=1 is pressed, state=0 is released. Send both
            -- back-to-back to simulate a tap (like a player click).
            xin:QueueButtonData(btn, 1)
            xin:QueueButtonData(btn, 0)
        end)
        if not ok then
            -- Fall back to IController (DirectInput gamepad) if the
            -- XInput interface isn't bound on this Ashita build.
            local ok2 = pcall(function()
                local ctl = AshitaCore:GetInputManager():GetController()
                ctl:QueueButtonData(btn, 1)
                ctl:QueueButtonData(btn, 0)
            end)
            if ok2 then
                msg(('pad: queued via IController (XInput failed) btn=0x%04X (%s)')
                    :format(btn, k))
            else
                msg('pad: both XInput and Controller QueueButtonData failed')
            end
            return
        end
        msg(('pad: queued via IXInput btn=0x%04X (%s)'):format(btn, k))
    elseif sub == 'winkey' then
        -- Inject a Win32 virtual-key press (down + up) via
        -- keybd_event(). This goes through the OS input pipeline
        -- so DirectInput sees it as a real keypress. Usage:
        --   /interact winkey escape   (named alias)
        --   /interact winkey 27       (raw VK code)
        local k = args[3]
        if k == nil then
            msg('usage: /interact winkey <name|vk>  (name: enter|escape|up|down|left|right)')
            return
        end
        local vk = VK_MAP[k:lower()] or tonumber(k)
        if vk == nil then
            msg(('winkey: unknown key %q'):format(k))
            return
        end
        if press_vk(vk) then
            msg(('winkey: injected vk=0x%02X (%s)'):format(vk, k))
        else
            msg('winkey: keybd_event call failed')
        end
    elseif sub == 'close' then
        -- Manual recovery: clear a stuck dialog when a test goes
        -- wrong. Sends 0x05B Mode=End so the server-side event
        -- closes too, not just our local bookkeeping.
        action_close_menu()
    elseif sub == 'force_close' then
        -- Heavier recovery: send 0x05B Mode=End for an event whose
        -- ID we know (typically from LSB script mining) but which
        -- we DIDN'T capture this addon-load - e.g. dialog opened
        -- before the addon reloaded, or session leftover from a
        -- previous run. Reads the currently-targeted NPC's sid via
        -- Ashita's target memory + the act_idx via entity manager,
        -- and uses the supplied event_id to satisfy the server's
        -- isInEvent() validator.
        --
        -- Usage: /interact force_close <event_id>
        --   e.g. /interact force_close 32761  (Rabid Wolf's event)
        local event_id = tonumber(args[3])
        if event_id == nil then
            msg('usage: /interact force_close <event_id>')
            return
        end
        local tgt_idx = pcget(function()
            return AshitaCore:GetMemoryManager():GetTarget():GetTargetIndex(0)
        end) or 0
        if tgt_idx == 0 then
            msg('force_close: no target. /target an NPC first.')
            return
        end
        local sid = pcget(function()
            return AshitaCore:GetMemoryManager():GetEntity():GetServerId(tgt_idx)
        end) or 0
        if sid == 0 then
            msg('force_close: target has no server id?')
            return
        end
        -- Build and send 0x05B Mode=End directly. We bypass the
        -- in-memory menu state since the whole point is to close a
        -- dialog the addon's state machine doesn't know about.
        local pm = AshitaCore:GetPacketManager()
        if pm == nil then return end
        local zone_id = pcget(function()
            return AshitaCore:GetMemoryManager():GetParty():GetMemberZone(0)
        end) or 0
        pm:QueuePacket(0x5B, 0x14, 0x00, 0x00, 0x00, function(ptr)
            local p = ffi.cast('u8*', ptr)
            ffi.fill(p + 0x04, 0x10)
            ffi.cast('u32*', p + 0x04)[0] = sid
            ffi.cast('u32*', p + 0x08)[0] = 0           -- EndPara=0
            ffi.cast('u16*', p + 0x0C)[0] = tgt_idx     -- ActIndex
            ffi.cast('u16*', p + 0x0E)[0] = 0           -- Mode=End
            ffi.cast('u16*', p + 0x10)[0] = zone_id     -- EventNum
            ffi.cast('u16*', p + 0x12)[0] = event_id    -- EventPara
        end)
        msg(('force_close: queued 0x05B End for sid=%d act=%d event_id=%d')
            :format(sid, tgt_idx, event_id))
        reset_menu()
    else
        msg('usage: /interact { debug | state | enter | close | force_close <event_id> }')
    end
end)

ashita.events.register('d3d_present', 'interact_render', function()
    state.frame = state.frame + 1

    -- One-shot character setup once the player entity is loaded.
    if state.last_character == nil then
        local char = get_character_name()
        if char ~= nil and char ~= '' then
            state.last_character = char
            ensure_dir(get_data_path() .. 'state/' .. char)
            ensure_dir(get_data_path() .. 'commands/' .. char)
        end
    end

    -- Defensive auto-close: if the menu has been quiet for longer
    -- than MENU_TIMEOUT_FRAMES, force-close. Real close packets
    -- (0x05C) almost always fire reliably, so this only catches
    -- pathological cases (zone change mid-dialog, etc.). Disabled
    -- when MENU_TIMEOUT_FRAMES == 0 (the iterating-on-parsing mode -
    -- a forced close hides what we are/aren't capturing).
    if MENU_TIMEOUT_FRAMES > 0
            and menu.open
            and (state.frame - menu.last_active) > MENU_TIMEOUT_FRAMES then
        msg('menu timeout - forcing close (no activity in '
            .. MENU_TIMEOUT_FRAMES .. ' frames)')
        reset_menu()
    end

    -- Publish on change OR on the heartbeat. The heartbeat keeps the
    -- file's mtime fresh so the orchestrator's stale-snapshot guard
    -- doesn't trip.
    if menu.dirty or (state.frame - state.last_publish_frame) >= PUBLISH_EVERY_FRAMES then
        pcall(publish)
    end

    -- Drain action queue at a steady cadence. ~10Hz is plenty -
    -- menus tolerate sub-second latency on input.
    if state.frame % POLL_EVERY_FRAMES == 0 then
        pcall(poll_actions)
    end
end)

ashita.events.register('unload', 'interact_unload', function()
    msg('Unloaded.')
end)
