import { randomUUID } from 'node:crypto';
import { Redis as IORedis } from 'ioredis';
import pg from 'pg';
import { createClient } from 'redis';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { REDIS_HIT_LUA, REDIS_HIT_SHA1 } from '../../src/generated/assets.js';
import { createLimiter } from '../../src/core/limiter.js';
import { MIGRATION_SQL, postgresStore } from '../../src/stores/postgres.js';
import { fromIoredis, fromNodeRedis, redisStore } from '../../src/stores/redis.js';
import type { Store } from '../../src/core/types.js';
import { runConcurrency, runSequence, vectorsOf, type Vector } from '../support/vectors.js';

const sequences = vectorsOf('sequence').filter((v) => v.tiers.includes('exact'));
const concurrency = vectorsOf('concurrency');
const redisUrls = (process.env['OMNILIMIT_TEST_REDIS_URLS'] ?? '').split(',').filter(Boolean);
const postgresUrls = (process.env['OMNILIMIT_TEST_POSTGRES_URLS'] ?? '').split(',').filter(Boolean);

if (process.env['OMNILIMIT_REQUIRE_STORES'] && (!redisUrls.length || !postgresUrls.length)) {
  throw new Error('OMNILIMIT_REQUIRE_STORES is set, but store URLs are missing: CI would pass without testing anything');
}

function suite(name: string, getStore: () => Store): void {
  describe(name, () => {
    it.each(sequences.map((v): [string, Vector] => [v.id, v]))('%s', async (_id, vector) => {
      await runSequence(vector, getStore(), `:${randomUUID()}`);
    });

    it.each(concurrency.map((v): [string, Vector] => [v.id, v]))('%s', async (_id, vector) => {
      await runConcurrency(vector, getStore(), `:${randomUUID()}`);
    });

    // Vectors inject the time, so they never reach the clock branch of each script.
    it('smoke: uses the real store clock', async () => {
      const limiter = createLimiter({
        limit: 2,
        windowMs: 1000,
        burst: 2,
        store: getStore(),
        scope: 'smoke',
        salt: randomUUID(),
        onStoreError: 'closed',
      });
      expect((await limiter.check('a')).allowed).toBe(true);
      expect((await limiter.check('a')).allowed).toBe(true);
      const denied = await limiter.check('a');
      expect(denied.reason).toBe('limited');
      expect(denied.retryAfterMs).toBeGreaterThan(0);
      expect(denied.retryAfterMs).toBeLessThanOrEqual(500);
      await new Promise((resolve) => setTimeout(resolve, denied.retryAfterMs! + 20));
      expect((await limiter.check('a')).allowed).toBe(true);
    });
  });
}

for (const url of redisUrls) {
  describe(`redis ${url}`, () => {
    const io = new IORedis(url, { lazyConnect: true, maxRetriesPerRequest: 2 });
    const node = createClient({ url });
    let ioStore: Store;
    let nodeStore: Store;

    beforeAll(async () => {
      await io.connect();
      await node.connect();
      ioStore = redisStore(fromIoredis(io));
      nodeStore = redisStore(fromNodeRedis(node));
    });

    afterAll(async () => {
      io.disconnect();
      await node.close();
    });

    it('loads the script on NOSCRIPT and its hash matches the sent bytes', async () => {
      await io.script('FLUSH');
      const params = { limit: 1, windowMs: 1000, capacity: 1000, drainMs: 1000 };
      await ioStore.hit(`ol:test:${randomUUID()}`, params);
      expect(await io.script('EXISTS', REDIS_HIT_SHA1)).toEqual([1]);
      expect(await io.script('LOAD', REDIS_HIT_LUA)).toBe(REDIS_HIT_SHA1);
    });

    it('expires the bucket after draining', async () => {
      const key = `ol:test:${randomUUID()}`;
      await ioStore.hit(key, { limit: 1, windowMs: 5000, capacity: 5000, drainMs: 5000 });
      const ttl = await io.pttl(key);
      expect(ttl).toBeGreaterThan(0);
      expect(ttl).toBeLessThanOrEqual(5000);
    });

    it('does not write on denial', async () => {
      const key = `ol:test:${randomUUID()}`;
      const params = { limit: 1, windowMs: 5000, capacity: 5000, drainMs: 5000 };
      await ioStore.hit(key, params, 1_000_000);
      const before = await io.hgetall(key);
      const denied = await ioStore.hit(key, params, 1_001_000);
      expect(denied.allowed).toBe(false);
      expect(await io.hgetall(key)).toEqual(before);
    });

    suite('ioredis', () => ioStore);
    suite('node-redis', () => nodeStore);
  });
}

for (const url of postgresUrls) {
  describe(`postgres ${url}`, () => {
    const pool = new pg.Pool({ connectionString: url, max: 20 });
    const store = postgresStore(pool);

    beforeAll(async () => {
      await pool.query(MIGRATION_SQL);
      await pool.query(MIGRATION_SQL); // must stay idempotent
    });

    afterAll(async () => {
      await pool.end();
    });

    it('cleanup removes only expired buckets', async () => {
      await pool.query('TRUNCATE omnilimit_state');
      const params = { limit: 1, windowMs: 1000, capacity: 1000, drainMs: 1000 };
      await store.hit('old', params, 1_000_000);
      await store.hit('live', params);
      expect(await store.cleanup()).toBe(1);
      const { rows } = await pool.query('SELECT key FROM omnilimit_state');
      expect(rows.map((row) => row.key)).toEqual(['live']);
    });

    it('does not write on denial', async () => {
      const key = `ol:test:${randomUUID()}`;
      const params = { limit: 1, windowMs: 5000, capacity: 5000, drainMs: 5000 };
      await store.hit(key, params, 1_000_000);
      const read = async () => (await pool.query('SELECT level, ts_ms, expires_at_ms FROM omnilimit_state WHERE key = $1', [key])).rows;
      const before = await read();
      expect((await store.hit(key, params, 1_001_000)).allowed).toBe(false);
      expect(await read()).toEqual(before);
    });

    suite('pg', () => store);
  });
}

describe('cross-client state', () => {
  it.runIf(redisUrls.length > 0)('ioredis and node-redis share one counter', async () => {
    const io = new IORedis(redisUrls[0]!);
    const node = createClient({ url: redisUrls[0]! });
    await node.connect();
    const salt = randomUUID();
    const make = (store: Store) =>
      createLimiter({ limit: 1, windowMs: 60_000, burst: 2, store, scope: 'cross', salt, onStoreError: 'closed' });
    const results = [
      await make(redisStore(fromIoredis(io))).check('a'),
      await make(redisStore(fromNodeRedis(node))).check('a'),
      await make(redisStore(fromIoredis(io))).check('a'),
    ];
    io.disconnect();
    await node.close();
    expect(results.map((r) => r.allowed)).toEqual([true, true, false]);
  });
});
