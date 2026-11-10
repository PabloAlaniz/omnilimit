-- omnilimit state format 1, read only. KEYS[1] = bucket key.
-- ARGV: limit, window_ms, capacity, drain_ms, now_ms ('' = use the server clock).
-- Returns the decision a hit would get now, without writing: {allowed, drained level, stored ts}.
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])
local drain_ms = tonumber(ARGV[4])

local now
if ARGV[5] ~= nil and ARGV[5] ~= '' then
  now = tonumber(ARGV[5])
else
  local clock = redis.call('TIME')
  now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
end

local stored = redis.call('HMGET', KEYS[1], 'l', 't')
local level = tonumber(stored[1]) or 0
local ts = tonumber(stored[2]) or now

local t = math.max(now, ts)
local drained = math.max(0, level - math.min(t - ts, drain_ms) * limit)
local allowed = 0
if drained + window_ms <= capacity then
  allowed = 1
end
return {allowed, string.format('%.0f', drained), string.format('%.0f', ts)}
