// What a network loses, against real stores: the reply, the request, or the connection in the
// middle of a call. Conformance vectors simulate a store fault at the limiter; these go through
// each real client, where a lost reply and a resent command are things that happen.
import { randomUUID } from 'node:crypto';
import { Redis as IORedis } from 'ioredis';
import pg from 'pg';
import { createClient } from 'redis';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { createLimiter } from '../../src/core/limiter.js';
import { MIGRATION_SQL, postgresStore } from '../../src/stores/postgres.js';
import { fromIoredis, fromNodeRedis, redisStore } from '../../src/stores/redis.js';
import type { Limiter, Store } from '../../src/core/types.js';
import { faultProxy, until, type FaultProxy } from '../support/fault-proxy.js';

const redisUrls = (process.env['OMNILIMIT_TEST_REDIS_URLS'] ?? '').split(',').filter(Boolean);
const postgresUrls = (process.env['OMNILIMIT_TEST_POSTGRES_URLS'] ?? '').split(',').filter(Boolean);

const BURST = 10;
const TIMEOUT_MS = 400;

interface Client {
  /** A store whose client connects through the proxy. */
  faulty: (url: string) => Promise<{ store: Store; end: () => Promise<void> }>;
  /** A store on a connection of its own, straight to the server: it reads what was counted. */
  direct: () => Promise<{ store: Store; end: () => Promise<void> }>;
}

function suite(name: string, target: string, client: Client): void {
  describe(`${name} ${target}`, () => {
    let proxy: FaultProxy;
    let faulty: Awaited<ReturnType<Client['faulty']>>;
    let direct: Awaited<ReturnType<Client['direct']>>;

    beforeAll(async () => {
      proxy = await faultProxy(target);
      faulty = await client.faulty(proxy.url);
      direct = await client.direct();
    });

    afterAll(async () => {
      await faulty.end();
      await direct.end();
      await proxy.close();
    });

    // Before any fault: the connection is open and every script is loaded. Otherwise the first
    // reply to be lost is a handshake or a NOSCRIPT error, not the call under test.
    const warm = async (limiter: Limiter) => {
      await limiter.check('warm');
      await limiter.peek('warm');
      await limiter.refund((await limiter.reserve('warm')).reservation);
    };

    const pair = async (onStoreError: 'open' | 'closed' = 'closed') => {
      const options = { limit: 1, windowMs: 60_000, burst: BURST, scope: 'faults', salt: randomUUID(), onStoreError, storeTimeoutMs: TIMEOUT_MS };
      const through = createLimiter({ ...options, store: faulty.store });
      await warm(through);
      const straight = createLimiter({ ...options, store: direct.store });
      return { through, used: async () => BURST - (await straight.peek('a')).remaining! };
    };

    /** The client is usable again: it reconnected, or its held replies arrived. */
    const recovered = (limiter: Limiter) => until(async () => (await limiter.peek('a')).reason !== 'store_error', 'the client to recover', 10_000);

    it('a reply that never comes is a store failure at the timeout, and the hit counted once', async () => {
      const { through, used } = await pair();
      expect((await through.check('a')).allowed).toBe(true);
      proxy.holdReplies();
      const started = Date.now();
      const decision = await through.check('a');
      const elapsed = Date.now() - started;
      expect(decision).toMatchObject({ allowed: false, reason: 'store_error', status: 503 });
      expect(elapsed).toBeGreaterThanOrEqual(TIMEOUT_MS - 20);
      expect(elapsed).toBeLessThan(TIMEOUT_MS + 1000);
      // The store did count it: the limiter gave up waiting, the store did not undo anything.
      expect(await used()).toBe(2);
      proxy.resume();
      await recovered(through);
      // The late reply changes nothing, and the count goes on from where the store has it.
      expect((await through.check('a')).allowed).toBe(true);
      expect(await used()).toBe(3);
    });

    it('with `open`, the same failure lets the request through without headers', async () => {
      const { through, used } = await pair('open');
      proxy.holdReplies();
      const decision = await through.check('a');
      expect(decision).toMatchObject({ allowed: true, reason: 'store_error', status: null, headers: {} });
      expect(await used()).toBe(1);
      proxy.resume();
      await recovered(through);
    });

    it('a connection cut after the store ran the hit counts it once, not twice', async () => {
      const { through, used } = await pair();
      proxy.holdReplies();
      const checking = through.check('a');
      await until(() => proxy.heldReplyBytes() > 0, 'the store to answer');
      proxy.cut();
      expect(await checking).toMatchObject({ allowed: false, reason: 'store_error' });
      await recovered(through);
      // A client that sent the command again on reconnecting would have counted two.
      expect(await used()).toBe(1);
      expect((await through.check('a')).allowed).toBe(true);
      expect(await used()).toBe(2);
    });

    it('a refund whose reply is lost reports false and is applied once', async () => {
      const { through, used } = await pair();
      await through.check('a');
      await through.check('a');
      const { reservation } = await through.reserve('a');
      expect(await used()).toBe(3);
      proxy.holdReplies();
      const refunding = through.refund(reservation);
      await until(() => proxy.heldReplyBytes() > 0, 'the store to answer');
      proxy.cut();
      expect(await refunding).toBe(false);
      await recovered(through);
      // Applied twice, it would give back a unit the reservation never held.
      expect(await used()).toBe(2);
      // And the limiter does not try again: the reservation is spent.
      expect(await through.refund(reservation)).toBe(false);
      expect(await used()).toBe(2);
    });

    it('a request that never reaches the store counts nothing', async () => {
      const { through, used } = await pair();
      proxy.holdRequests();
      const decision = await through.check('a');
      expect(decision).toMatchObject({ allowed: false, reason: 'store_error', status: 503 });
      expect(proxy.heldRequestBytes()).toBeGreaterThan(0);
      proxy.cut();
      await recovered(through);
      expect(await used()).toBe(0);
    });

    it('after all of it, the limit is still exactly the burst', async () => {
      const { through, used } = await pair();
      proxy.holdReplies();
      await through.check('a');
      proxy.resume();
      await recovered(through);
      const decisions = [];
      for (let i = 0; i < BURST; i += 1) decisions.push((await through.check('a')).allowed);
      // One unit went to the call that timed out, so one of these is denied.
      expect(decisions.filter(Boolean).length).toBe(BURST - 1);
      expect(await used()).toBe(BURST);
    });
  });
}

const quiet = <T extends { on(event: 'error', listener: () => void): unknown }>(client: T): T => {
  // A cut connection is the point of these tests, not an unhandled error.
  client.on('error', () => {});
  return client;
};

for (const url of redisUrls) {
  suite('ioredis', url, {
    async faulty(proxied) {
      // `autoResendUnfulfilledCommands` is on by default: ioredis then sends a command again
      // after reconnecting when its reply was lost, and the store runs it twice. See docs/stores.md.
      const client = quiet(new IORedis(proxied, { maxRetriesPerRequest: 1, autoResendUnfulfilledCommands: false }));
      return { store: redisStore(fromIoredis(client)), end: async () => client.disconnect() };
    },
    async direct() {
      const client = new IORedis(url);
      return { store: redisStore(fromIoredis(client)), end: async () => client.disconnect() };
    },
  });

  suite('node-redis', url, {
    async faulty(proxied) {
      const client = quiet(createClient({ url: proxied }));
      await client.connect();
      return { store: redisStore(fromNodeRedis(client)), end: async () => client.destroy() };
    },
    async direct() {
      const client = createClient({ url });
      await client.connect();
      return { store: redisStore(fromNodeRedis(client)), end: async () => client.destroy() };
    },
  });
}

for (const url of postgresUrls) {
  suite('pg', url, {
    async faulty(proxied) {
      const pool = quiet(new pg.Pool({ connectionString: proxied, max: 1 }));
      return { store: postgresStore(pool), end: async () => void pool.end().catch(() => {}) };
    },
    async direct() {
      const pool = new pg.Pool({ connectionString: url, max: 2 });
      await pool.query(MIGRATION_SQL);
      return { store: postgresStore(pool), end: () => pool.end() };
    },
  });
}

describe('fault suites', () => {
  it('have stores to run against when CI requires them', () => {
    if (process.env['OMNILIMIT_REQUIRE_STORES']) expect(redisUrls.length * postgresUrls.length).toBeGreaterThan(0);
  });
});
