#!/usr/bin/env node
// What a check costs with each store, measured against the cheapest call the same connection
// can make. Runs the built TypeScript port.
//
// Absolute numbers depend on the machine and on where the store is, so they are reported, not
// judged. What is judged is the ratio: a check is one round trip, and it should cost about what
// one round trip costs. `--check` fails when a ratio passes its ceiling, which is how CI notices
// a change that made a check do more work.
//
// Usage: bench.mjs [--check] [--json FILE]
//   OMNILIMIT_TEST_REDIS_URLS, OMNILIMIT_TEST_POSTGRES_URLS: the first of each is measured.
import { createRequire } from 'node:module';
import { webcrypto } from 'node:crypto';
import { appendFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const requireFromTs = createRequire(join(root, 'ts', 'package.json'));
const dist = (name) => import(join(root, 'ts', 'dist', `${name}.js`));
const { createLimiter, memoryStore, checkAll, reserveAll, createLockout } = await dist('index');

const SEQUENTIAL = 3000;
const WARMUP = 300;
const CONCURRENT = 20_000;
const CONCURRENCY = 64;

/**
 * How many times a baseline a check may cost, at the median. Loose on purpose: on loopback the
 * baseline is a few microseconds, so the ratio is far above what a real network gives, and it
 * moves with the machine. These catch a check that became several times dearer, nothing finer.
 */
const CEILINGS = { memory: 8, redis: 10, postgres: 40 };

/**
 * Calls to the store's client that each operation makes, with every script already loaded.
 * Exact, and the same on any machine: this is what notices an operation that gained a call.
 */
const ROUND_TRIPS = {
  check: 1,
  'check, denied': 1,
  peek: 1,
  reset: 1,
  reserve: 1,
  refund: 1,
  'checkAll of 2': 4,
  'reserveAll of 2': 2,
  'cancel of 2': 2,
  'lockout attempt, 2 limits, wrong secret': 4,
  'lockout attempt in reserve mode, 2 limits, wrong secret': 2,
};

const micros = (ns) => Number(ns) / 1000;

async function latencies(run, count) {
  for (let i = 0; i < WARMUP; i += 1) await run(i);
  const samples = new Array(count);
  for (let i = 0; i < count; i += 1) {
    const started = process.hrtime.bigint();
    await run(WARMUP + i);
    samples[i] = micros(process.hrtime.bigint() - started);
  }
  samples.sort((a, b) => a - b);
  const at = (q) => samples[Math.min(count - 1, Math.floor(q * count))];
  return { p50: at(0.5), p95: at(0.95), p99: at(0.99) };
}

async function throughput(run) {
  let next = 0;
  const started = process.hrtime.bigint();
  await Promise.all(
    Array.from({ length: CONCURRENCY }, async () => {
      while (next < CONCURRENT) await run(next++);
    }),
  );
  return Math.round(CONCURRENT / (Number(process.hrtime.bigint() - started) / 1e9));
}

async function measure(name, store, baseline) {
  const run = `${Date.now()}-${Math.random()}`;
  const limiter = (scope, burst) =>
    createLimiter({ limit: 1000, windowMs: 60_000, burst, store, scope, salt: run, onStoreError: 'closed', storeTimeoutMs: 30_000 });
  const allowed = limiter('allowed', 1000);
  const denied = limiter('denied', 1);
  const second = limiter('second', 1000);
  await denied.check('hot');

  const base = await latencies(baseline, SEQUENTIAL);
  const check = await latencies((i) => allowed.check(`k${i}`), SEQUENTIAL);
  const deny = await latencies(() => denied.check('hot'), SEQUENTIAL);
  const peek = await latencies((i) => allowed.peek(`k${i}`), SEQUENTIAL);
  const both = await latencies(
    (i) =>
      checkAll([
        { limiter: allowed, id: `c${i}` },
        { limiter: second, id: `c${i}` },
      ]),
    SEQUENTIAL,
  );
  const spread = await throughput((i) => allowed.check(`t${i % 5000}`));
  const hot = await throughput(() => second.check('one-key'));
  return {
    store: name,
    baseline_us: base,
    check_us: check,
    denied_us: deny,
    peek_us: peek,
    check_all_2_us: both,
    ratio_check: check.p50 / base.p50,
    ratio_check_all_2: both.p50 / base.p50,
    throughput_many_keys: spread,
    throughput_one_key: hot,
  };
}

/** Count the client calls each operation makes. `calls` reads the counter of the client. */
async function roundTrips(store, calls) {
  const run = `${Date.now()}-${Math.random()}`;
  const limiter = (scope, burst = 100) =>
    createLimiter({ limit: 100, windowMs: 60_000, burst, store, scope, salt: run, onStoreError: 'closed', storeTimeoutMs: 30_000 });
  const a = limiter('rt-a');
  const b = limiter('rt-b');
  const one = limiter('rt-one', 1);
  // Load every script first: the first use of each costs a second call.
  await a.check('warm');
  await a.peek('warm');
  await a.refund((await a.reserve('warm')).reservation);
  await one.check('x');
  const entries = () => [
    { limiter: a, id: 'x' },
    { limiter: b, id: 'x' },
  ];
  const limits = [
    { limiter: a, idOf: (id) => id },
    { limiter: b, idOf: (id) => id },
  ];
  const out = {};
  const counted = async (name, work) => {
    const before = calls();
    await work();
    out[name] = calls() - before;
  };
  await counted('check', () => a.check('x'));
  await counted('check, denied', () => one.check('x'));
  await counted('peek', () => a.peek('x'));
  await counted('reset', () => a.reset('x'));
  let reservation;
  await counted('reserve', async () => ({ reservation } = await a.reserve('x')));
  await counted('refund', () => a.refund(reservation));
  await counted('checkAll of 2', () => checkAll(entries()));
  let pending;
  await counted('reserveAll of 2', async () => (pending = await reserveAll(entries())));
  await counted('cancel of 2', () => pending.cancel());
  await counted('lockout attempt, 2 limits, wrong secret', () => createLockout({ limits }).attempt('u', () => false));
  await counted('lockout attempt in reserve mode, 2 limits, wrong secret', () => createLockout({ limits, mode: 'reserve' }).attempt('u', () => false));
  return out;
}

const results = [];
const trips = {};

// Memory: the baseline is the SHA-256 that derives a key, the one thing every check must do.
{
  const input = new TextEncoder().encode('s\0an-id-of-ordinary-length');
  results.push(await measure('memory', memoryStore({ maxKeys: 1_000_000 }), () => webcrypto.subtle.digest('SHA-256', input)));
}

const redisUrl = (process.env.OMNILIMIT_TEST_REDIS_URLS ?? '').split(',').filter(Boolean)[0];
if (redisUrl) {
  const { redisStore, fromIoredis } = await dist('redis');
  const { Redis } = requireFromTs('ioredis');
  const client = new Redis(redisUrl);
  results.push(await measure('redis', redisStore(fromIoredis(client)), () => client.ping()));
  let calls = 0;
  const counting = {
    evalsha: (...args) => ((calls += 1), client.evalsha(...args)),
    eval: (...args) => ((calls += 1), client.eval(...args)),
    del: (...args) => ((calls += 1), client.del(...args)),
  };
  trips.redis = await roundTrips(redisStore(fromIoredis(counting)), () => calls);
  client.disconnect();
}

const postgresUrl = (process.env.OMNILIMIT_TEST_POSTGRES_URLS ?? '').split(',').filter(Boolean)[0];
if (postgresUrl) {
  const { postgresStore, MIGRATION_SQL } = await dist('postgres');
  const pg = requireFromTs('pg');
  const pool = new pg.Pool({ connectionString: postgresUrl, max: 20 });
  await pool.query(MIGRATION_SQL);
  results.push(await measure('postgres', postgresStore(pool), () => pool.query('SELECT 1')));
  let calls = 0;
  const counting = { query: (...args) => ((calls += 1), pool.query(...args)) };
  trips.postgres = await roundTrips(postgresStore(counting), () => calls);
  await pool.end();
}

const f = (n) => (n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2));
const lines = [
  `Node ${process.version}, ${SEQUENTIAL} calls one after another, then ${CONCURRENT} calls from ${CONCURRENCY} at once.`,
  '',
  '| Store | Baseline p50 | `check` p50 | p99 | Denied p50 | `peek` p50 | `checkAll` of 2, p50 | `check` / baseline | Checks/s, many keys | Checks/s, one key |',
  '|---|---|---|---|---|---|---|---|---|---|',
  ...results.map(
    (r) =>
      `| ${r.store} | ${f(r.baseline_us.p50)} µs | ${f(r.check_us.p50)} µs | ${f(r.check_us.p99)} µs | ${f(r.denied_us.p50)} µs | ${f(r.peek_us.p50)} µs | ${f(r.check_all_2_us.p50)} µs | ${r.ratio_check.toFixed(2)} | ${r.throughput_many_keys} | ${r.throughput_one_key} |`,
  ),
];
const measured = Object.keys(trips);
if (measured.length) {
  lines.push('', '| Operation | Calls to the store | ' + measured.join(' | ') + ' |', '|---|---|' + measured.map(() => '---|').join(''));
  for (const [name, expected] of Object.entries(ROUND_TRIPS)) lines.push(`| ${name} | ${expected} | ${measured.map((m) => trips[m][name]).join(' | ')} |`);
}
console.log(lines.join('\n'));
if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, `### TypeScript\n\n${lines.join('\n')}\n\n`);

const jsonAt = process.argv.indexOf('--json');
if (jsonAt !== -1) writeFileSync(process.argv[jsonAt + 1], `${JSON.stringify({ port: 'ts', runtime: `node ${process.version}`, results, round_trips: trips }, null, 1)}\n`);

if (process.argv.includes('--check')) {
  const over = results.filter((r) => r.ratio_check > CEILINGS[r.store]);
  for (const r of over) console.error(`${r.store}: a check costs ${r.ratio_check.toFixed(2)} baselines, above the ceiling of ${CEILINGS[r.store]}`);
  let wrong = 0;
  for (const [store, got] of Object.entries(trips)) {
    for (const [name, expected] of Object.entries(ROUND_TRIPS)) {
      if (got[name] === expected) continue;
      wrong += 1;
      console.error(`${store}: ${name} made ${got[name]} calls to the store, not ${expected}`);
    }
  }
  if (process.env.OMNILIMIT_REQUIRE_STORES && results.length < 3) {
    console.error('OMNILIMIT_REQUIRE_STORES is set, but a store was not measured');
    process.exit(1);
  }
  if (over.length || wrong) process.exit(1);
  console.log('\nevery operation makes the calls it should, and every ratio is under its ceiling');
}
