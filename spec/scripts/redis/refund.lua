-- omnilimit state format 1: refund. KEYS[1] = bucket key.
-- ARGV: limit, window_ms, capacity, drain_ms, now_ms ('' = use the server clock), since_ms.
-- window_ms is the reservation's cost times the window, and since_ms the ts its hit stored.
-- Gives back what is left of the reservation after draining since then, never below zero.
-- Returns {level, ts} after the refund. A refund that changes nothing writes nothing.
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local drain_ms = tonumber(ARGV[4])
local since = tonumber(ARGV[6])

local now
local injected = ARGV[5] ~= nil and ARGV[5] ~= ''
if injected then
  now = tonumber(ARGV[5])
else
  local clock = redis.call('TIME')
  now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
end

local stored = redis.call('HMGET', KEYS[1], 'l', 't')
if stored[1] == false then
  -- The bucket is gone, and the reservation with it.
  return {'0', string.format('%.0f', now)}
end
local level = tonumber(stored[1])
local ts = tonumber(stored[2])

local t = math.max(now, ts)
local drained = math.max(0, level - math.min(t - ts, drain_ms) * limit)
local give = 0
if t >= since then
  give = math.max(0, window_ms - math.min(t - since, drain_ms) * limit)
end

if drained == 0 or give == 0 then
  return {string.format('%.0f', drained), string.format('%.0f', ts)}
end

local new_level = math.max(0, drained - give)
redis.call('HSET', KEYS[1], 'l', string.format('%.0f', new_level), 't', string.format('%.0f', t))
if not injected then
  redis.call('PEXPIRE', KEYS[1], drain_ms)
end
return {string.format('%.0f', new_level), string.format('%.0f', t)}
