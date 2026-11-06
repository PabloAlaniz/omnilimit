#!/usr/bin/env node
// Runs steps through the built TypeScript port, for python/tests/test_cross_language.py.
// Reads one JSON request on stdin and writes a JSON array of decisions to stdout.
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

const limiter = createLimiter({
  limit: request.policy.limit,
  windowMs: request.policy.window_ms,
  burst: request.policy.burst,
  store,
  scope: request.scope,
  salt: request.salt,
  onStoreError: 'closed',
  storeTimeoutMs: 10_000,
});

const decisions = [];
for (const step of request.steps) {
  const d = await limiter.check(step.id, { nowMs: step.at_ms });
  decisions.push({
    allowed: d.allowed,
    reason: d.reason,
    status: d.status,
    remaining: d.remaining,
    reset_ms: d.resetMs,
    retry_after_ms: d.retryAfterMs,
  });
}
await close();
process.stdout.write(JSON.stringify(decisions));
