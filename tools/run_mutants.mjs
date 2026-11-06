#!/usr/bin/env node
// Prove the suite can fail: each mutation below is a realistic bug, and must make it fail.
// Set OMNILIMIT_TEST_* to also exercise the Redis and Postgres script mutants.
import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const hasStores = Boolean(process.env.OMNILIMIT_TEST_REDIS_URLS && process.env.OMNILIMIT_TEST_POSTGRES_URLS);

const core = [
  ['retry-after rounds down', 'ts/src/core/gcra.ts', 'const retryAfterMs = ceilDiv(level + windowMs - capacity, limit);', 'const retryAfterMs = Math.floor((level + windowMs - capacity) / limit);'],
  ['drain is not capped', 'ts/src/core/gcra.ts', 'const elapsed = Math.min(t - ts, params.drainMs);', 'const elapsed = t - ts;\n  if (elapsed > params.drainMs) return { hit: { allowed: true, level: params.windowMs + 1, ts: t }, write: { level: params.windowMs + 1, ts: t } };'],
  ['clock regression refunds quota', 'ts/src/core/gcra.ts', 'const t = Math.max(nowMs, ts);', 'const t = nowMs;'],
  ['capacity check is off by one', 'ts/src/core/gcra.ts', 'if (drained + params.windowMs > params.capacity) {', 'if (drained + params.windowMs >= params.capacity) {'],
  ['denied requests write state', 'ts/src/core/gcra.ts', 'return { hit: { allowed: false, level: drained, ts }, write: null };', 'return { hit: { allowed: false, level: drained, ts: t }, write: { level: drained, ts: t } };'],
  ['X-Forwarded-For read from the left', 'ts/src/ip/index.ts', 'return canonicalIp(entries[entries.length - 1 - hops]);', 'return canonicalIp(entries[0]);'],
  ['short chain falls back to first value', 'ts/src/ip/index.ts', 'if (entries.length <= hops) return null;', 'if (entries.length <= hops) return canonicalIp(entries[0]);'],
  ['IPv4-mapped not normalised', 'ts/src/ip/index.ts', 'if (isMapped) {', 'if (false) {'],
  ['store failure inverts open/closed', 'ts/src/core/limiter.ts', "const allowed = onStoreError === 'open';", "const allowed = onStoreError === 'closed';"],
  ['store timeout never fires', 'ts/src/core/limiter.ts', 'return await Promise.race([work, timeout]);', 'void timeout.catch(() => {}); return await Promise.race([work, new Promise<T>(() => {}), Promise.resolve().then(() => work)]);'],
  ['key ignores the salt', 'ts/src/core/key.ts', 'const saltBytes = encoder.encode(salt);', "const saltBytes = encoder.encode('');"],
  ['key has no separator', 'ts/src/core/key.ts', 'input.set(idBytes, saltBytes.length + 1);', 'input.set(idBytes, saltBytes.length);'],
  ['retry-after header dropped', 'ts/src/core/headers.ts', "out['retry-after'] = String(toSeconds(decision.retryAfterMs));", ''],
];

const scripts = [
  ['lua: denied requests write state', 'spec/scripts/redis/hit.lua', "  return {0, string.format('%.0f', drained), string.format('%.0f', ts)}", "  redis.call('HSET', KEYS[1], 'l', string.format('%.0f', drained), 't', string.format('%.0f', t))\n  return {0, string.format('%.0f', drained), string.format('%.0f', t)}"],
  ['lua: loses digits with tostring', 'spec/scripts/redis/hit.lua', "'l', string.format('%.0f', new_level), 't', string.format('%.0f', t))", "'l', tostring(new_level), 't', tostring(t))"],
  ['lua: no expiry', 'spec/scripts/redis/hit.lua', "  redis.call('PEXPIRE', KEYS[1], drain_ms)\n", ''],
  ['sql: no row lock', 'spec/scripts/postgres/001_v1.sql', '  WHERE s.key = p_key\n  FOR UPDATE;', '  WHERE s.key = p_key;'],
  ['sql: denied requests write state', 'spec/scripts/postgres/001_v1.sql', '    RETURN QUERY SELECT false, v_drained, v_ts;', '    UPDATE omnilimit_state AS s SET level = v_drained, ts_ms = v_t WHERE s.key = p_key;\n    RETURN QUERY SELECT false, v_drained, v_t;'],
];

function testsFail(command) {
  const result = spawnSync(command[0], command.slice(1), { cwd: join(root, 'ts'), encoding: 'utf8', stdio: 'pipe', env: process.env });
  return result.status !== 0;
}

function sync() {
  const result = spawnSync('python3', ['tools/sync_assets.py'], { cwd: root, encoding: 'utf8' });
  if (result.status !== 0) throw new Error(result.stderr);
}

function run(mutants, command, afterEdit) {
  let survived = 0;
  for (const [name, file, from, to] of mutants) {
    const path = join(root, file);
    const original = readFileSync(path, 'utf8');
    if (!original.includes(from)) throw new Error(`mutant "${name}" no longer applies to ${file}`);
    try {
      writeFileSync(path, original.replace(from, to));
      afterEdit?.();
      const killed = testsFail(command);
      console.log(`${killed ? 'killed  ' : 'SURVIVED'}  ${name}`);
      if (!killed) survived += 1;
    } finally {
      writeFileSync(path, original);
      afterEdit?.();
    }
  }
  return survived;
}

let survived = 0;
if (testsFail(['pnpm', 'exec', 'vitest', 'run', 'test/core'])) throw new Error('the unmutated suite must pass first');
survived += run(core, ['pnpm', 'exec', 'vitest', 'run', 'test/core']);
if (hasStores) {
  survived += run(scripts, ['pnpm', 'exec', 'vitest', 'run', 'test/stores'], sync);
} else {
  console.log('skipped script mutants: set OMNILIMIT_TEST_REDIS_URLS and OMNILIMIT_TEST_POSTGRES_URLS');
  if (process.env.CI) throw new Error('script mutants must run in CI');
}
if (survived > 0) {
  console.error(`${survived} mutant(s) survived: the suite has a hole`);
  process.exit(1);
}
console.log('all mutants killed');
