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
  ['retry-after rounds down', 'ts/src/core/gcra.ts', 'const retryAfterMs = ceilDiv(level + amount - capacity, limit);', 'const retryAfterMs = Math.floor((level + amount - capacity) / limit);'],
  ['drain is not capped', 'ts/src/core/gcra.ts', 'const elapsed = Math.min(t - ts, params.drainMs);', 'const elapsed = t - ts;\n  if (elapsed > params.drainMs) return { hit: { allowed: true, level: params.windowMs + 1, ts: t }, write: { level: params.windowMs + 1, ts: t } };'],
  ['clock regression refunds quota', 'ts/src/core/gcra.ts', 'const t = Math.max(nowMs, ts);', 'const t = nowMs;'],
  ['capacity check is off by one', 'ts/src/core/gcra.ts', 'if (drained + params.windowMs > params.capacity) {', 'if (drained + params.windowMs >= params.capacity) {'],
  ['denied requests write state', 'ts/src/core/gcra.ts', 'return { hit: { allowed: false, level: drained, ts }, write: null };', 'return { hit: { allowed: false, level: drained, ts: t }, write: { level: drained, ts: t } };'],
  ['X-Forwarded-For read from the left', 'ts/src/ip/index.ts', 'return canonicalIp(entries[entries.length - 1 - hops]);', 'return canonicalIp(entries[0]);'],
  ['short chain falls back to first value', 'ts/src/ip/index.ts', 'if (entries.length <= hops) return null;', 'if (entries.length <= hops) return canonicalIp(entries[0]);'],
  ['IPv4-mapped not normalised', 'ts/src/ip/index.ts', 'if (isMapped) {', 'if (false) {'],
  ['store failure inverts open/closed', 'ts/src/core/limiter.ts', "const allowed = onStoreError === 'open';", "const allowed = onStoreError === 'closed';"],
  ['store timeout never fires', 'ts/src/core/limiter.ts', 'return await Promise.race([work, timeout]);', 'void timeout.catch(() => {}); return await Promise.race([work, new Promise<T>(() => {}), Promise.resolve().then(() => work)]);'],
  ['shorthand accepts plurals', 'ts/src/core/gcra.ts', '(second|minute|hour|day)$/;', '(second|minute|hour|day)s?$/;'],
  ['shorthand accepts a leading zero', 'ts/src/core/gcra.ts', "/^([1-9][0-9]{0,14})\\/", "/^([0-9]{1,15})\\/"],
  ['checkAll counts before looking', 'ts/src/core/recipes.ts', 'peeks.push(await limiter.peek(id, options(cost)));', 'peeks.push(await limiter.check(id, options(cost)));'],
  ['checkAll answers with the shortest wait', 'ts/src/core/recipes.ts', '(decision.retryAfterMs ?? 0) > (governing.retryAfterMs ?? 0)', '(decision.retryAfterMs ?? 0) < (governing.retryAfterMs ?? 0)'],
  ['checkAll stops peeking at the first denial', 'ts/src/core/recipes.ts', 'for (const { limiter, id, cost } of entries) peeks.push(await limiter.peek(id, options(cost)));', 'for (const { limiter, id, cost } of entries) { peeks.push(await limiter.peek(id, options(cost))); if (!peeks[peeks.length - 1]!.allowed) break; }'],
  ['checkAll denial tie picks the last', 'ts/src/core/recipes.ts', '(decision.retryAfterMs ?? 0) > (governing.retryAfterMs ?? 0)', '(decision.retryAfterMs ?? 0) >= (governing.retryAfterMs ?? 0)'],
  ['checkAll remaining tie picks the last', 'ts/src/core/recipes.ts', 'decision.remaining < governing.remaining', 'decision.remaining <= governing.remaining'],
  ['checkAll lets a null remaining govern', 'ts/src/core/recipes.ts', '    if (decision.remaining === null) continue;\n', ''],
  ['checkAll keeps counting after a denial', 'ts/src/core/recipes.ts', '    if (!decision.allowed) return { allowed: false, decision, decisions };\n  }\n', "  }\n  const denied = decisions.find((d) => !d.allowed);\n  if (denied) return { allowed: false, decision: denied, decisions };\n"],
  ['recipes validate costs lazily', 'ts/src/core/recipes.ts', '    assertLimiterCost(limiter, cost);\n', ''],
  ['recipes notice approximate stores at their peek', 'ts/src/core/recipes.ts', "    if (limiter.tier === 'approximate') {\n", "    if (limiter.tier === 'approximate' && false) {\n"],
  ['reserveAll does not compensate a denial', 'ts/src/core/recipes.ts', '      const refunded = await refundEach(entries, held);\n', '      const refunded = held.map(() => null);\n'],
  ['compensation reports the denial as a failed refund', 'ts/src/core/recipes.ts', 'refunded.push(reservation === null ? null :', 'refunded.push(reservation === null ? false :'],
  ['compensation stops at a failed refund', 'ts/src/core/recipes.ts', "    refunded.push(reservation === null ? null : await (entries[index] as LimitEntry).limiter.refund(reservation));\n", "    refunded.push(reservation === null ? null : await (entries[index] as LimitEntry).limiter.refund(reservation));\n    if (refunded[refunded.length - 1] === false) break;\n"],
  ['reserveAll trusts a store without refund', 'ts/src/core/recipes.ts', "    if (mode === 'reserve') assertLimiterRefund(limiter);\n", ''],
  ['commit counts a denied pending', 'ts/src/core/recipes.ts', "mode === 'peek' && own.allowed ? countAll(entries) : own", "mode === 'peek' ? countAll(entries) : own"],
  ['a repeated settlement runs again', 'ts/src/core/recipes.ts', '      if (settledBy === null) {\n', '      if (settledBy === null || settledBy === op) {\n'],
  ['settlement removed', 'ts/src/core/recipes.ts', '      if (settledBy === null) {\n', '      if (true) {\n'],
  ['settlement marked after the store calls', 'ts/src/core/recipes.ts', '        settledBy = op;\n        outcome = work();\n', '        outcome = work();\n        void outcome.then(() => (settledBy = op));\n'],
  ['reserve cancel does not refund', 'ts/src/core/recipes.ts', "cancel: () => once.settle('cancel', async () => ({ refunded: own.allowed ? await refundEach(entries, held) : [...refunded] })),", "cancel: () => once.settle('cancel', async () => ({ refunded: own.allowed ? held.map(() => null) : [...refunded] })),"],
  ['reserveAll validates lazily', 'ts/src/core/recipes.ts', "  validate(recipe, entries, 'reserve');\n", ''],
  ['lockout fail stops at the first denial', 'ts/src/core/recipes.ts', 'for (const { limiter, id } of entries) checks.push(await limiter.check(id));', 'for (const { limiter, id } of entries) {\n      checks.push(await limiter.check(id));\n      if (!checks[checks.length - 1]!.allowed) break;\n    }'],
  ['lockout succeed ignores resetOnSuccess', 'ts/src/core/recipes.ts', 'if (resetting[index]) resets[index]', 'if (resetting[index] || true) resets[index]'],
  ['lockout succeed on a locked gate resets', 'ts/src/core/recipes.ts', '    if (own.allowed) {\n      for (const [index', '    if (true) {\n      for (const [index'],
  ['lockout succeed stops after a failed reset', 'ts/src/core/recipes.ts', 'if (resetting[index]) resets[index] = await limiter.reset(id);\n', 'if (resetting[index]) {\n          resets[index] = await limiter.reset(id);\n          if (!resets[index]) break;\n        }\n'],
  ['lockout reserve fail counts again', 'ts/src/core/recipes.ts', "if (mode === 'reserve' || !own.allowed) return own;", 'if (!own.allowed) return own;'],
  ['lockout reserve succeed keeps the limits it does not reset', 'ts/src/core/recipes.ts', "else if (mode === 'reserve') given[index]", 'else if (false) given[index]'],
  ['lockout cancel does not refund', 'ts/src/core/recipes.ts', 'const cancel = async (): Promise<Cancelled> => ({ refunded: own.allowed ? await refundEach(entries, held) : [...refunded] });', 'const cancel = async (): Promise<Cancelled> => ({ refunded: [...refunded] });'],
  ['lockout counts a failure when verify raises', 'ts/src/core/recipes.ts', '        await opened.cancel();\n        throw error;', '        await opened.fail();\n        throw error;'],
  ['lockout verifies while locked', 'ts/src/core/recipes.ts', '      if (!opened.allowed) return { allowed: false, verified: null, decision: opened.decision, decisions: opened.decisions };\n', ''],
  ['lockout counts successes', 'ts/src/core/recipes.ts', 'const { decision, decisions, resets } = await opened.succeed();', 'const { decision, decisions } = await opened.fail();\n        const resets = decisions.map(() => null);'],
  ['cost is ignored', 'ts/src/core/limiter.ts', 'return { limit, windowMs: windowMs * cost, capacity, drainMs };', 'return { limit, windowMs, capacity, drainMs };'],
  ['cost above the burst is accepted', 'ts/src/core/gcra.ts', 'if (!isPositiveInt(cost) || cost > policy.burst) {', 'if (!isPositiveInt(cost)) {'],
  ['a denied cost reports the wait for one unit', 'ts/src/core/gcra.ts', 'const retryAfterMs = ceilDiv(level + amount - capacity, limit);', 'const retryAfterMs = ceilDiv(level + windowMs - capacity, limit);'],
  ['key ignores the salt', 'ts/src/core/key.ts', 'const saltBytes = encoder.encode(salt);', "const saltBytes = encoder.encode('');"],
  ['key has no separator', 'ts/src/core/key.ts', 'input.set(idBytes, saltBytes.length + 1);', 'input.set(idBytes, saltBytes.length);'],
  ['retry-after header dropped', 'ts/src/core/headers.ts', "out['retry-after'] = String(toSeconds(decision.retryAfterMs));", ''],
  ['peek reports the level after a hit', 'ts/src/core/gcra.ts', 'return { allowed: drained + params.windowMs <= params.capacity, level: drained, ts };', 'return { allowed: drained + params.windowMs <= params.capacity, level: drained + params.windowMs, ts };'],
  ['peek uses the request clock as ts', 'ts/src/core/gcra.ts', 'return { allowed: drained + params.windowMs <= params.capacity, level: drained, ts };', 'return { allowed: drained + params.windowMs <= params.capacity, level: drained, ts: t };'],
  ['memory peek consumes quota', 'ts/src/stores/memory.ts', 'return applyPeek(entries.get(key) ?? null, params, nowMs ?? clock());', 'const r = applyHit(entries.get(key) ?? null, params, nowMs ?? clock()); if (r.write) entries.set(key, { ...r.write, expiresAt: 0 }); return applyPeek(entries.get(key) ?? null, params, nowMs ?? clock());'],
  ['memory reset does not delete', 'ts/src/stores/memory.ts', '      entries.delete(key);\n    },\n    async refund', '      void key;\n    },\n    async refund'],
  ['peek counts like check', 'ts/src/core/limiter.ts', "return decide('peek', params.windowMs, () => peekFn(key, params, peekOptions?.nowMs));", "return decide('peek', params.windowMs, () => store.hit(key, params, peekOptions?.nowMs));"],
  ['refund gives the whole cost whatever the drain', 'ts/src/core/gcra.ts', 'Math.max(0, params.windowMs - Math.min(t - sinceMs, params.drainMs) * params.limit)', 'params.windowMs'],
  ['refund goes below zero', 'ts/src/core/gcra.ts', 'const level = Math.max(0, drained - give);', 'const level = drained - give;'],
  ['refund drains from now instead of since', 'ts/src/core/gcra.ts', 'Math.min(t - sinceMs, params.drainMs)', 'Math.min(t - nowMs, params.drainMs)'],
  ['refund of a reservation older than the bucket gives', 'ts/src/core/gcra.ts', 'const give = t < sinceMs ? 0 :', 'const give ='],
  ['refund does not drain the bucket first', 'ts/src/core/gcra.ts', 'const drained = Math.max(0, state.level - Math.min(t - state.ts, params.drainMs) * params.limit);', 'const drained = state.level;'],
  ['refund that gives nothing writes', 'ts/src/core/gcra.ts', 'if (drained === 0 || give === 0) return { refund: { level: drained, ts: state.ts }, write: null };', ''],
  ['refund on a missing bucket creates it', 'ts/src/core/gcra.ts', 'if (!state) return { refund: { level: 0, ts: nowMs }, write: null };', 'if (!state) return { refund: { level: 0, ts: nowMs }, write: { level: 0, ts: nowMs } };'],
  ['refund allowed twice', 'ts/src/core/limiter.ts', '      if (state.spent) return false;\n', ''],
  ['failed refund reports success', 'ts/src/core/limiter.ts', "        emit({ type: 'store_error', op: 'refund', scope, tier: store.tier, error });\n        return false;", "        emit({ type: 'store_error', op: 'refund', scope, tier: store.tier, error });\n        return true;"],
  ['a denial is a reservation', 'ts/src/core/limiter.ts', 'if (!hit?.allowed || hit.ts === null) return { decision, reservation: null };', 'if (!hit || hit.ts === null) return { decision, reservation: null };'],
  ['memory refund does not write', 'ts/src/stores/memory.ts', 'if (write) entries.set(key, { ...write, expiresAt: write.ts + params.drainMs });', 'void write;'],
  ['failed reset reports success', 'ts/src/core/limiter.ts', "        emit({ type: 'store_error', op: 'reset', scope, tier: store.tier, error });\n        return false;", "        emit({ type: 'store_error', op: 'reset', scope, tier: store.tier, error });\n        return true;"],
];

const scripts = [
  ['lua: denied requests write state', 'spec/scripts/redis/hit.lua', "  return {0, string.format('%.0f', drained), string.format('%.0f', ts)}", "  redis.call('HSET', KEYS[1], 'l', string.format('%.0f', drained), 't', string.format('%.0f', t))\n  return {0, string.format('%.0f', drained), string.format('%.0f', t)}"],
  ['lua: loses digits with tostring', 'spec/scripts/redis/hit.lua', "'l', string.format('%.0f', new_level), 't', string.format('%.0f', t))", "'l', tostring(new_level), 't', tostring(t))"],
  ['lua: no expiry', 'spec/scripts/redis/hit.lua', "  redis.call('PEXPIRE', KEYS[1], drain_ms)\n", ''],
  ['sql: no row lock', 'spec/scripts/postgres/001_v1.sql', '  WHERE s.key = p_key\n  FOR UPDATE;', '  WHERE s.key = p_key;'],
  ['sql: denied requests write state', 'spec/scripts/postgres/001_v1.sql', '    RETURN QUERY SELECT false, v_drained, v_ts;', '    UPDATE omnilimit_state AS s SET level = v_drained, ts_ms = v_t WHERE s.key = p_key;\n    RETURN QUERY SELECT false, v_drained, v_t;'],
  ['lua peek: does not drain', 'spec/scripts/redis/peek.lua', 'local drained = math.max(0, level - math.min(t - ts, drain_ms) * limit)', 'local drained = level'],
  ['lua peek: capacity check is off by one', 'spec/scripts/redis/peek.lua', 'if drained + window_ms <= capacity then', 'if drained + window_ms < capacity then'],
  ['sql peek: missing row reports ts 0', 'spec/scripts/postgres/002_peek.sql', '    v_ts := v_now;', '    v_ts := 0;'],
  ['lua refund: does not drain the bucket first', 'spec/scripts/redis/refund.lua', 'local drained = math.max(0, level - math.min(t - ts, drain_ms) * limit)', 'local drained = level'],
  ['lua refund: gives the whole cost', 'spec/scripts/redis/refund.lua', '  give = math.max(0, window_ms - math.min(t - since, drain_ms) * limit)', '  give = window_ms'],
  ['lua refund: goes below zero', 'spec/scripts/redis/refund.lua', 'local new_level = math.max(0, drained - give)', 'local new_level = drained - give'],
  ['sql refund: gives the whole cost', 'spec/scripts/postgres/003_refund.sql', '    v_give := GREATEST(0, p_window_ms - LEAST(v_t - p_since_ms, p_drain_ms) * p_limit);', '    v_give := p_window_ms;'],
  ['sql refund: writes when it gives nothing', 'spec/scripts/postgres/003_refund.sql', '  IF v_drained = 0 OR v_give = 0 THEN', '  IF false THEN'],
  ['sql refund: drains from now instead of since', 'spec/scripts/postgres/003_refund.sql', 'LEAST(v_t - p_since_ms, p_drain_ms)', 'LEAST(v_t - v_now, p_drain_ms)'],
  ['sql peek: writes a placeholder row', 'spec/scripts/postgres/002_peek.sql', 'BEGIN\n  SELECT s.level', "BEGIN\n  INSERT INTO omnilimit_state (key, level, ts_ms, expires_at_ms) VALUES (p_key, 0, 0, 0) ON CONFLICT (key) DO NOTHING;\n  SELECT s.level"],
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
