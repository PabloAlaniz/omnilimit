#!/usr/bin/env node
// Runs steps through the built TypeScript port, for python/tests/test_cross_language.py.
// Reads one JSON request on stdin and writes a JSON array of results to stdout, one per step.
//
// A step is a hit unless `op` says `peek`, `reset` or `refund`. A refund's `of` is the index,
// in this request, of the hit it gives back, which then runs as a reservation: a reservation is
// opaque and lives in this process, so it is made and refunded in the same request.
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { readFileSync } from 'node:fs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const requireFromTs = createRequire(join(root, 'ts', 'package.json'));
const dist = (name) => import(join(root, 'ts', 'dist', `${name}.js`));

const request = JSON.parse(readFileSync(0, 'utf8'));
const { createLimiter } = await dist('index');

let store;
let close;
if (request.store === 'redis') {
  const { redisStore, fromIoredis } = await dist('redis');
  const { Redis } = requireFromTs('ioredis');
  const client = new Redis(request.url);
  store = redisStore(fromIoredis(client));
  close = async () => client.disconnect();
} else {
  const { postgresStore } = await dist('postgres');
  const pg = requireFromTs('pg');
  const pool = new pg.Pool({ connectionString: request.url, max: 1 });
  store = postgresStore(pool);
  close = () => pool.end();
}

// The raw reply of the latest store call, to report the state each step left.
let raw = null;
const recording = {
  tier: store.tier,
  hit: async (...args) => (raw = await store.hit(...args)),
  peek: async (...args) => (raw = await store.peek(...args)),
  reset: (key) => store.reset(key),
  refund: async (...args) => (raw = await store.refund(...args)),
};

const limiter = createLimiter({
  limit: request.policy.limit,
  windowMs: request.policy.window_ms,
  burst: request.policy.burst,
  store: recording,
  scope: request.scope,
  salt: request.salt,
  onStoreError: 'closed',
  storeTimeoutMs: 10_000,
});

const refunded = new Set(request.steps.filter((step) => step.op === 'refund').map((step) => step.of));
const reservations = new Map();
const results = [];
for (const [index, step] of request.steps.entries()) {
  raw = null;
  if (step.op === 'reset') {
    results.push({ reset: await limiter.reset(step.id) });
    continue;
  }
  if (step.op === 'refund') {
    if (!reservations.has(step.of)) throw new Error(`step ${index} refunds ${step.of}, which is not a reservation of this request`);
    const done = await limiter.refund(reservations.get(step.of), { nowMs: step.at_ms });
    results.push(done ? { refunded: true, state: { level: raw.level, ts: raw.ts } } : { refunded: false });
    continue;
  }
  const options = { nowMs: step.at_ms, cost: step.cost ?? 1 };
  let d;
  if (step.op === 'peek') d = await limiter.peek(step.id, options);
  else if (refunded.has(index)) {
    const reserved = await limiter.reserve(step.id, options);
    reservations.set(index, reserved.reservation);
    d = reserved.decision;
  } else d = await limiter.check(step.id, options);
  results.push({
    allowed: d.allowed,
    reason: d.reason,
    status: d.status,
    remaining: d.remaining,
    reset_ms: d.resetMs,
    retry_after_ms: d.retryAfterMs,
    state: { level: raw?.level ?? null, ts: raw?.ts ?? null },
  });
}
await close();
process.stdout.write(JSON.stringify(results));
