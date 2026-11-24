// The last published release and this code, taking turns on one counter in a real store.
//
// During a rolling deploy both versions run at once against the same Redis or Postgres. State
// format 1 promises they read each other's buckets, and the stability policy promises that
// within a major version. This is the test of that promise: `omnilimit-released` is the package
// from npm, under another name.
import { randomUUID } from 'node:crypto';
import { Redis as IORedis } from 'ioredis';
import { createLimiter as createReleased } from 'omnilimit-released';
import { MIGRATION_SQL as RELEASED_MIGRATION_SQL, postgresStore as releasedPostgresStore } from 'omnilimit-released/postgres';
import { fromIoredis as releasedFromIoredis, redisStore as releasedRedisStore } from 'omnilimit-released/redis';
import pg from 'pg';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { createLimiter } from '../../src/core/limiter.js';
import { MIGRATION_SQL, postgresStore } from '../../src/stores/postgres.js';
import { fromIoredis, redisStore } from '../../src/stores/redis.js';
import type { Limiter, Store } from '../../src/core/types.js';
import { toPolicy, vectorsOf, type Vector } from '../support/vectors.js';

const redisUrls = (process.env['OMNILIMIT_TEST_REDIS_URLS'] ?? '').split(',').filter(Boolean);
const postgresUrls = (process.env['OMNILIMIT_TEST_POSTGRES_URLS'] ?? '').split(',').filter(Boolean);

// Vectors made only of hits and peeks: a reservation lives in one process, so a refund cannot
// cross versions, and a reset would hide what the other version wrote.
const shared = vectorsOf('sequence').filter(
  (vector) => vector.tiers.includes('exact') && !vector.shadow && vector.steps.every((step: Vector) => step.op === undefined || step.op === 'peek'),
);

type Check = Pick<Limiter, 'check' | 'peek'>;

function suite(name: string, stores: () => { current: Store; released: unknown }): void {
  describe(name, () => {
    for (const first of ['released', 'current'] as const) {
      it.each(shared.map((v): [string, Vector] => [v.id, v]))(`%s, the ${first} version first`, async (_id, vector) => {
        const options = { ...toPolicy(vector.policy), scope: vector.scope, salt: `${vector.salt}:${randomUUID()}`, onStoreError: 'closed' as const };
        const { current, released } = stores();
        const limiters: Record<'released' | 'current', Check> = {
          current: createLimiter({ ...options, store: current }),
          released: createReleased({ ...options, store: released as never }),
        };
        const order = first === 'released' ? (['released', 'current'] as const) : (['current', 'released'] as const);
        for (const [index, step] of vector.steps.entries()) {
          const limiter = limiters[order[index % 2]!];
          const call = { nowMs: step.at_ms as number, ...(step.cost ? { cost: step.cost as number } : {}) };
          const decision = step.op === 'peek' ? await limiter.peek(step.id, call) : await limiter.check(step.id, call);
          expect(
            { allowed: decision.allowed, remaining: decision.remaining, reset_ms: decision.resetMs, retry_after_ms: decision.retryAfterMs },
            `${vector.id} step ${index}, by the ${order[index % 2]} version`,
          ).toEqual({
            allowed: step.expect.allowed,
            remaining: step.expect.remaining,
            reset_ms: step.expect.reset_ms,
            retry_after_ms: step.expect.retry_after_ms,
          });
        }
      });
    }
  });
}

it('has vectors to share', () => {
  expect(shared.length).toBeGreaterThan(60);
});

for (const url of redisUrls) {
  describe(`redis ${url}`, () => {
    const client = new IORedis(url, { lazyConnect: true });
    beforeAll(() => client.connect());
    afterAll(() => client.disconnect());
    suite('released and current', () => ({ current: redisStore(fromIoredis(client)), released: releasedRedisStore(releasedFromIoredis(client)) }));
  });
}

for (const url of postgresUrls) {
  describe(`postgres ${url}`, () => {
    const pool = new pg.Pool({ connectionString: url, max: 5 });
    beforeAll(async () => {
      // An upgrade: the schema of the release is there, and this version's is applied over it.
      await pool.query(RELEASED_MIGRATION_SQL);
      await pool.query(MIGRATION_SQL);
    });
    afterAll(() => pool.end());
    suite('released and current', () => ({ current: postgresStore(pool), released: releasedPostgresStore(pool) }));
  });
}
