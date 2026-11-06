-- omnilimit state format 1. KEYS[1] = bucket key.
-- ARGV: limit, window_ms, capacity, drain_ms, now_ms ('' = use the server clock).
-- The script has no division on policy values: every port computes capacity and
-- drain_ms once, so it behaves identically regardless of the calling language.
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])
local drain_ms = tonumber(ARGV[4])

local now
local injected = ARGV[5] ~= nil and ARGV[5] ~= ''
if injected then
  now = tonumber(ARGV[5])
else
  local clock = redis.call('TIME')
  now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
end

local stored = redis.call('HMGET', KEYS[1], 'l', 't')
local level = tonumber(stored[1]) or 0
local ts = tonumber(stored[2]) or now

local t = math.max(now, ts)
local elapsed = math.min(t - ts, drain_ms)
local drained = math.max(0, level - elapsed * limit)

if drained + window_ms > capacity then
  -- Denied requests never write, so a retry storm cannot extend the wait.
  return {0, string.format('%.0f', drained), string.format('%.0f', ts)}
end

local new_level = drained + window_ms
-- tostring() would render large doubles in scientific notation and lose digits.
redis.call('HSET', KEYS[1], 'l', string.format('%.0f', new_level), 't', string.format('%.0f', t))
-- A TTL runs on real time, so it cannot be mixed with an injected clock (conformance vectors only).
if not injected then
  redis.call('PEXPIRE', KEYS[1], drain_ms)
end
return {1, string.format('%.0f', new_level), string.format('%.0f', t)}
