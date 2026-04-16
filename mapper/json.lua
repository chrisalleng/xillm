--[[
* Minimal JSON encoder/decoder for the mapper addon.
* Bundled locally so the addon has no dependency on Ashita's libs/json.lua.
*
* Supports: null, boolean, number, string, array, object.
* Numbers are serialized with up to 10 significant digits.
--]]

local json = {}

-- ============================================================
-- ENCODE
-- ============================================================

local function encode_value(val, seen)
    local t = type(val)

    if val == nil or val == json.null then
        return 'null'

    elseif t == 'boolean' then
        return val and 'true' or 'false'

    elseif t == 'number' then
        if val ~= val then return 'null' end                     -- NaN
        if val == math.huge or val == -math.huge then return 'null' end
        -- Use integer form when safe
        if math.floor(val) == val and math.abs(val) < 1e15 then
            return string.format('%d', val)
        end
        return string.format('%.10g', val)

    elseif t == 'string' then
        -- Escape special characters
        local escaped = val:gsub('[\\"\0-\31]', function(c)
            local special = {
                ['"']  = '\\"',
                ['\\'] = '\\\\',
                ['\n'] = '\\n',
                ['\r'] = '\\r',
                ['\t'] = '\\t',
                ['\b'] = '\\b',
                ['\f'] = '\\f',
            }
            return special[c] or string.format('\\u%04x', c:byte())
        end)
        return '"' .. escaped .. '"'

    elseif t == 'table' then
        -- Cycle detection
        if seen[val] then error('Circular reference in table') end
        seen[val] = true

        -- Determine if this is an array: contiguous integer keys starting at 1
        local n = 0
        local max_key = 0
        for k, _ in pairs(val) do
            if type(k) == 'number' and math.floor(k) == k and k >= 1 then
                if k > max_key then max_key = k end
                n = n + 1
            else
                -- Non-integer key: treat as object
                max_key = -1
                break
            end
        end

        local result
        if max_key > 0 and max_key == n then
            -- Array
            local parts = {}
            for i = 1, n do
                parts[i] = encode_value(val[i], seen)
            end
            result = '[' .. table.concat(parts, ',') .. ']'
        else
            -- Object
            local parts = {}
            for k, v in pairs(val) do
                local key_str
                if type(k) == 'string' then
                    key_str = encode_value(k, seen)
                elseif type(k) == 'number' then
                    key_str = '"' .. tostring(k) .. '"'
                else
                    -- skip non-string/number keys
                    key_str = nil
                end
                if key_str then
                    table.insert(parts, key_str .. ':' .. encode_value(v, seen))
                end
            end
            result = '{' .. table.concat(parts, ',') .. '}'
        end

        seen[val] = nil
        return result
    else
        error('Cannot encode type: ' .. t)
    end
end

function json.encode(val)
    return encode_value(val, {})
end

-- ============================================================
-- DECODE
-- ============================================================

local function decode_value(s, pos)
    -- Skip whitespace
    pos = s:match('^%s*()', pos)

    local first = s:sub(pos, pos)

    -- null
    if s:sub(pos, pos + 3) == 'null' then
        return nil, pos + 4

    -- true
    elseif s:sub(pos, pos + 3) == 'true' then
        return true, pos + 4

    -- false
    elseif s:sub(pos, pos + 4) == 'false' then
        return false, pos + 5

    -- string
    elseif first == '"' then
        local result = {}
        local i = pos + 1
        while i <= #s do
            local c = s:sub(i, i)
            if c == '"' then
                return table.concat(result), i + 1
            elseif c == '\\' then
                local esc = s:sub(i + 1, i + 1)
                local special = {
                    ['"'] = '"', ['\\'] = '\\', ['/'] = '/',
                    ['n'] = '\n', ['r'] = '\r', ['t'] = '\t',
                    ['b'] = '\b', ['f'] = '\f',
                }
                if special[esc] then
                    table.insert(result, special[esc])
                    i = i + 2
                elseif esc == 'u' then
                    local hex = s:sub(i + 2, i + 5)
                    local code = tonumber(hex, 16)
                    if code then
                        -- Basic UTF-8 encoding for BMP characters
                        if code < 0x80 then
                            table.insert(result, string.char(code))
                        elseif code < 0x800 then
                            table.insert(result, string.char(
                                0xC0 + math.floor(code / 64),
                                0x80 + (code % 64)))
                        else
                            table.insert(result, string.char(
                                0xE0 + math.floor(code / 4096),
                                0x80 + math.floor((code % 4096) / 64),
                                0x80 + (code % 64)))
                        end
                    end
                    i = i + 6
                else
                    table.insert(result, esc)
                    i = i + 2
                end
            else
                table.insert(result, c)
                i = i + 1
            end
        end
        error('Unterminated string at pos ' .. pos)

    -- number
    elseif first == '-' or (first >= '0' and first <= '9') then
        local num_str, end_pos = s:match('^(-?%d+%.?%d*[eE]?[+-]?%d*)()', pos)
        if num_str then
            return tonumber(num_str), end_pos
        end
        error('Invalid number at pos ' .. pos)

    -- array
    elseif first == '[' then
        local arr = {}
        pos = pos + 1
        pos = s:match('^%s*()', pos)
        if s:sub(pos, pos) == ']' then
            return arr, pos + 1
        end
        while true do
            local val
            val, pos = decode_value(s, pos)
            table.insert(arr, val)
            pos = s:match('^%s*()', pos)
            local sep = s:sub(pos, pos)
            if sep == ']' then
                return arr, pos + 1
            elseif sep == ',' then
                pos = pos + 1
            else
                error('Expected , or ] in array at pos ' .. pos)
            end
        end

    -- object
    elseif first == '{' then
        local obj = {}
        pos = pos + 1
        pos = s:match('^%s*()', pos)
        if s:sub(pos, pos) == '}' then
            return obj, pos + 1
        end
        while true do
            pos = s:match('^%s*()', pos)
            if s:sub(pos, pos) ~= '"' then
                error('Expected string key in object at pos ' .. pos)
            end
            local key
            key, pos = decode_value(s, pos)
            pos = s:match('^%s*()', pos)
            if s:sub(pos, pos) ~= ':' then
                error('Expected : in object at pos ' .. pos)
            end
            pos = pos + 1
            local val
            val, pos = decode_value(s, pos)
            obj[key] = val
            pos = s:match('^%s*()', pos)
            local sep = s:sub(pos, pos)
            if sep == '}' then
                return obj, pos + 1
            elseif sep == ',' then
                pos = pos + 1
            else
                error('Expected , or } in object at pos ' .. pos)
            end
        end

    else
        error('Unexpected character "' .. first .. '" at pos ' .. pos)
    end
end

-- Sentinel for explicit null values (optional; nil decodes to Lua nil)
json.null = setmetatable({}, { __tostring = function() return 'null' end })

function json.decode(s)
    if type(s) ~= 'string' then error('json.decode expects a string') end
    local val, pos = decode_value(s, 1)
    -- Ensure nothing significant follows
    local rest = s:match('^%s*()', pos)
    if rest <= #s then
        -- Trailing content: ignore (tolerant mode)
    end
    return val
end

return json
