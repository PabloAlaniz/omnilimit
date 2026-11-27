import { describe, expect, it, vi } from 'vitest';
import {
  checkAll,
  createLimiter,
  createLockout,
  memoryStore,
  peekAll,
  reserveAll,
  SettledError,
  SPEC_VERSION,
  type Store,
} from '../../src/index.js';
import { cloudflareBindingStore } from '../../src/stores/cloudflare.js';
import { ipKey } from '../../src/adapters/shared.js';
import { clientKey, maskIp, resolveClientIp } from '../../src/ip/index.js';

const base = { limit: 1, windowMs: 1000, scope: 't', onStoreError: 'closed' } as const;

describe('createLimiter', () => {
  it('requires onStoreError at runtime for JavaScript callers', () => {
    const options = { limit: 1, windowMs: 1000, scope: 't', store: memoryStore() };
    expect(() => createLimiter(options as never)).toThrow(/onStoreError/);
  });

  it('sends draft-11 headers by default and supports other profiles', async () => {
    const hitWith = async (headers?: 'legacy' | 'none') =>
      (await createLimiter({ ...base, store: memoryStore(), ...(headers ? { headers } : {}) }).check('a')).headers;
    expect(await hitWith()).toEqual({ 'ratelimit-policy': '"t";q=1;w=1', ratelimit: '"t";r=0;t=1' });
    expect(await hitWith('legacy')).toHaveProperty('x-ratelimit-remaining', '0');
    expect(await hitWith('none')).toEqual({});
  });

  it('always sends retry-after when denied, even with no profile', async () => {
    const limiter = createLimiter({ ...base, store: memoryStore(), headers: 'none' });
    await limiter.check('a');
    expect((await limiter.check('a')).headers).toEqual({ 'retry-after': '1' });
  });

  it('reports events and survives a throwing hook', async () => {
    const onEvent = vi.fn(() => {
      throw new Error('hook failure');
    });
    const limiter = createLimiter({ ...base, store: memoryStore(), onEvent });
    expect((await limiter.check('a')).allowed).toBe(true);
    expect((await limiter.check('a')).allowed).toBe(false);
    expect(onEvent.mock.calls.map((call) => (call as unknown as [{ type: string }])[0].type)).toEqual(['allowed', 'limited']);
  });

  it('reports store errors with the cause', async () => {
    const error = new Error('down');
    const store: Store = { tier: 'exact', hit: () => Promise.reject(error) };
    const events: unknown[] = [];
    await createLimiter({ ...base, store, onEvent: (e) => events.push(e) }).check('a');
    expect(events).toEqual([{ type: 'store_error', op: 'check', scope: 't', tier: 'exact', error }]);
  });

  it('uses the real clock when no time is injected', async () => {
    const decision = await createLimiter({ ...base, store: memoryStore() }).check('a');
    expect(decision.allowed).toBe(true);
    expect(SPEC_VERSION).toBe('1');
  });

  it('falls back to the window when an approximate store gives no retry time', async () => {
    const store: Store = { tier: 'approximate', hit: async () => ({ allowed: false, level: null, ts: null }) };
    const decision = await createLimiter({ ...base, store }).check('a');
    expect(decision.retryAfterMs).toBe(1000);
  });
});

describe('store timeout', () => {
  const slow = (ms: number, defaultTimeoutMs?: number): Store => ({
    tier: 'exact',
    ...(defaultTimeoutMs === undefined ? {} : { defaultTimeoutMs }),
    hit: () => new Promise((resolve) => setTimeout(() => resolve({ allowed: true, level: 1000, ts: 0 }), ms)),
  });
  const base = { scope: 't', limit: 1, windowMs: 1000, onStoreError: 'closed' as const };

  it("uses the store's own default when the caller gives none", async () => {
    const patient = createLimiter({ ...base, store: slow(60, 500) });
    expect((await patient.check('a')).allowed).toBe(true);
    const hasty = createLimiter({ ...base, store: slow(60, 10) });
    expect((await hasty.check('a')).status).toBe(503);
  });

  it("lets the caller's value win over the store's", async () => {
    const limiter = createLimiter({ ...base, store: slow(60, 500), storeTimeoutMs: 10 });
    expect((await limiter.check('a')).status).toBe(503);
  });

  it('sets no timer for a store in memory, which has nothing to wait for', async () => {
    const timers = vi.spyOn(globalThis, 'setTimeout');
    try {
      const memory = createLimiter({ ...base, limit: 5, store: memoryStore() });
      await memory.check('a');
      await memory.peek('a');
      const { reservation } = await memory.reserve('a');
      await memory.refund(reservation);
      await memory.reset('a');
      expect(timers).not.toHaveBeenCalled();
      // Any other tier is timed on every call.
      const remote = memoryStore();
      await createLimiter({ ...base, store: { tier: 'exact', hit: remote.hit } }).check('a');
      expect(timers).toHaveBeenCalledTimes(1);
    } finally {
      timers.mockRestore();
    }
  });

  it('still times a local store when the caller or the store names a timeout', async () => {
    const hangs = (defaultTimeoutMs?: number): Store => ({
      tier: 'local',
      ...(defaultTimeoutMs === undefined ? {} : { defaultTimeoutMs }),
      hit: () => new Promise(() => {}),
    });
    expect((await createLimiter({ ...base, store: hangs(), storeTimeoutMs: 10 }).check('a')).status).toBe(503);
    expect((await createLimiter({ ...base, store: hangs(10) }).check('a')).status).toBe(503);
  });

  it('reports a local store that throws, timed or not', async () => {
    const store: Store = {
      tier: 'local',
      hit: () => {
        throw new Error('broken');
      },
    };
    const events: string[] = [];
    const limiter = createLimiter({ ...base, store, onEvent: (e) => events.push(e.type) });
    expect(await limiter.check('a')).toMatchObject({ allowed: false, reason: 'store_error', status: 503 });
    expect(events).toEqual(['store_error']);
  });
});

describe('peek and reset', () => {
  const policy = { limit: 2, windowMs: 60_000, scope: 't', onStoreError: 'closed' } as const;

  it('peek never counts, and reset gives the quota back', async () => {
    const limiter = createLimiter({ ...policy, store: memoryStore() });
    for (let i = 0; i < 5; i++) expect((await limiter.peek('a')).allowed).toBe(true);
    expect((await limiter.check('a')).remaining).toBe(1);
    expect((await limiter.peek('a')).remaining).toBe(1);
    await limiter.check('a');
    const denied = await limiter.peek('a');
    expect(denied).toMatchObject({ allowed: false, reason: 'limited', status: 429 });
    expect(denied.headers['retry-after']).toBeDefined();
    expect(await limiter.reset('a')).toBe(true);
    expect((await limiter.peek('a')).remaining).toBe(2);
  });

  it('reports the operation in events', async () => {
    const events: Array<{ type: string; op: string }> = [];
    const limiter = createLimiter({ ...policy, store: memoryStore(), onEvent: (e) => events.push(e) });
    await limiter.peek('a');
    await limiter.check('a');
    await limiter.reset('a');
    expect(events.map((e) => [e.type, e.op])).toEqual([
      ['allowed', 'peek'],
      ['allowed', 'check'],
      ['reset', 'reset'],
    ]);
  });

  it('throws a TypeError when the store cannot peek or reset', async () => {
    const limiter = createLimiter({ ...policy, store: cloudflareBindingStore({ binding: undefined, periodSec: 60 }) });
    await expect(limiter.peek('a')).rejects.toThrow(TypeError);
    await expect(limiter.reset('a')).rejects.toThrow(/does not support reset/);
  });

  it('peek follows onStoreError, and a failed reset resolves false with an event', async () => {
    const error = new Error('down');
    const store: Store = {
      tier: 'exact',
      hit: () => Promise.reject(error),
      peek: () => Promise.reject(error),
      reset: () => new Promise(() => {}),
    };
    const events: Array<{ type: string; op: string }> = [];
    const closed = createLimiter({ ...policy, store, storeTimeoutMs: 10, onEvent: (e) => events.push(e) });
    expect(await closed.peek('a')).toMatchObject({ allowed: false, reason: 'store_error', status: 503 });
    expect(await closed.reset('a')).toBe(false);
    expect(events.map((e) => [e.type, e.op])).toEqual([
      ['store_error', 'peek'],
      ['store_error', 'reset'],
    ]);
    const open = createLimiter({ ...policy, store, onStoreError: 'open' });
    expect(await open.peek('a')).toMatchObject({ allowed: true, reason: 'store_error' });
  });

  it('memory peek does not keep a bucket alive against eviction', async () => {
    const store = memoryStore({ maxKeys: 1 });
    const params = { limit: 1, windowMs: 1000, capacity: 1000, drainMs: 1000 };
    await store.hit('a', params, 5000);
    expect((await store.peek!('a', params, 5001)).allowed).toBe(false);
    await store.hit('b', params, 5002); // evicts `a`
    expect((await store.peek!('a', params, 5003)).allowed).toBe(true);
  });
});

describe('reserve and refund', () => {
  const policy = { limit: 2, windowMs: 60_000, scope: 't', onStoreError: 'closed' } as const;

  it('reserve decides like check, and refund gives the request back', async () => {
    const limiter = createLimiter({ ...policy, store: memoryStore() });
    const first = await limiter.reserve('a', { cost: 2 });
    expect(first.decision).toMatchObject({ allowed: true, remaining: 0 });
    expect(first.reservation).toEqual({ scope: 't', cost: 2 });
    const denied = await limiter.reserve('a');
    expect(denied.decision).toMatchObject({ allowed: false, reason: 'limited' });
    expect(denied.reservation).toBeNull();
    expect(await limiter.refund(denied.reservation)).toBe(false);
    expect(await limiter.refund(first.reservation)).toBe(true);
    expect((await limiter.peek('a')).remaining).toBe(2);
  });

  it('refunds at most once, without calling the store again', async () => {
    const memory = memoryStore();
    const refund = vi.fn(memory.refund!);
    const limiter = createLimiter({ ...policy, store: { ...memory, refund } });
    const { reservation } = await limiter.reserve('a');
    const twice = await Promise.all([limiter.refund(reservation), limiter.refund(reservation)]);
    expect(twice.sort()).toEqual([false, true]);
    expect(await limiter.refund(reservation)).toBe(false);
    expect(refund).toHaveBeenCalledTimes(1);
  });

  it('only refunds reservations it made', async () => {
    const store = memoryStore();
    const one = createLimiter({ ...policy, store });
    const other = createLimiter({ ...policy, store });
    const { reservation } = await one.reserve('a');
    await expect(other.refund(reservation)).rejects.toThrow(/not made by this limiter/);
    await expect(one.refund({ scope: 't', cost: 1 })).rejects.toThrow(TypeError);
    expect(await one.refund(reservation)).toBe(true);
  });

  it('throws a TypeError before counting when the store cannot refund', async () => {
    const hit = vi.fn(async () => ({ allowed: true, level: null, ts: null }));
    const limiter = createLimiter({ ...policy, store: { tier: 'approximate', hit } });
    await expect(limiter.reserve('a')).rejects.toThrow(/does not support refund/);
    expect(hit).not.toHaveBeenCalled();
  });

  it('reports events, and a failed refund resolves false and spends the reservation', async () => {
    const memory = memoryStore();
    let fail = true;
    const store: Store = {
      tier: 'exact',
      hit: memory.hit,
      peek: memory.peek!,
      refund: (...args) => (fail ? new Promise(() => {}) : memory.refund!(...args)),
    };
    const events: Array<[string, string]> = [];
    const limiter = createLimiter({ ...policy, store, storeTimeoutMs: 10, onEvent: (e) => events.push([e.type, e.op]) });
    const lost = await limiter.reserve('a');
    expect(await limiter.refund(lost.reservation)).toBe(false);
    fail = false;
    expect(await limiter.refund(lost.reservation)).toBe(false);
    const kept = await limiter.reserve('a');
    expect(await limiter.refund(kept.reservation)).toBe(true);
    expect(events).toEqual([
      ['allowed', 'check'],
      ['store_error', 'refund'],
      ['allowed', 'check'],
      ['refund', 'refund'],
    ]);
    expect((await limiter.peek('a')).remaining).toBe(1);
  });

  it('a request allowed by a store failure is not a reservation', async () => {
    const store: Store = { tier: 'exact', hit: () => Promise.reject(new Error('down')), refund: memoryStore().refund! };
    const open = createLimiter({ ...policy, store, onStoreError: 'open' });
    const { decision, reservation } = await open.reserve('a');
    expect(decision).toMatchObject({ allowed: true, reason: 'store_error' });
    expect(reservation).toBeNull();
  });

  it('gives back only what has not drained, on the real clock', async () => {
    let clock = 1_000_000;
    const limiter = createLimiter({ ...policy, limit: 1, windowMs: 1000, store: memoryStore({ now: () => clock }) });
    const { reservation } = await limiter.reserve('a');
    clock += 1000;
    expect(await limiter.refund(reservation)).toBe(true);
    const next = await limiter.check('a');
    expect(next.allowed).toBe(true);
    expect((await limiter.peek('a')).allowed).toBe(false);
  });

  it('memory refund does not keep a bucket alive against eviction', async () => {
    const store = memoryStore({ maxKeys: 2 });
    const params = { limit: 1, windowMs: 1000, capacity: 2000, drainMs: 2000 };
    await store.hit('a', params, 5000);
    await store.hit('a', params, 5000);
    await store.hit('b', params, 5001);
    expect(await store.refund!('a', params, 5000, 5002)).toEqual({ level: 1000, ts: 5002 });
    await store.hit('c', params, 5003); // evicts `a`, the oldest write, despite its refund
    expect(await store.refund!('a', params, 5000, 5004)).toEqual({ level: 0, ts: 5004 });
    expect(await store.refund!('b', params, 5001, 5004)).toEqual({ level: 0, ts: 5004 });
  });
});

describe('shadow mode', () => {
  const shadow = { ...base, shadow: true } as const;

  it('lets a denial through, keeping its values, with no headers', async () => {
    const headers = vi.fn(() => ({ 'x-custom': '1' }));
    const limiter = createLimiter({ ...shadow, store: memoryStore(), headers });
    const first = await limiter.check('a', { nowMs: 0 });
    expect(first).toMatchObject({ allowed: true, reason: 'ok', shadowed: false, headers: {} });
    const second = await limiter.check('a', { nowMs: 0 });
    expect(second).toEqual({
      allowed: true,
      reason: 'limited',
      status: null,
      remaining: 0,
      resetMs: 1000,
      retryAfterMs: 1000,
      headers: {},
      tier: 'local',
      shadowed: true,
    });
    expect((await limiter.peek('a', { nowMs: 0 })).shadowed).toBe(true);
    expect(headers).not.toHaveBeenCalled();
  });

  it('counts exactly what an enforcing limiter counts', async () => {
    const store = memoryStore();
    const enforcing = createLimiter({ ...base, store, scope: 'e' });
    const watching = createLimiter({ ...shadow, store, scope: 'w' });
    for (const nowMs of [0, 0, 0, 500, 1000, 1000, 1999]) {
      const [e, w] = [await enforcing.check('a', { nowMs }), await watching.check('a', { nowMs })];
      expect({ ...w, allowed: e.allowed, status: e.status, headers: e.headers, shadowed: false }).toEqual(e);
      expect(w.shadowed).toBe(!e.allowed);
    }
  });

  it('marks only the events of shadowed decisions', async () => {
    const events: unknown[] = [];
    const limiter = createLimiter({ ...shadow, store: memoryStore(), onEvent: (e) => events.push(e) });
    await limiter.check('a', { nowMs: 0 });
    await limiter.check('a', { nowMs: 0 });
    await limiter.reset('a');
    expect(events).toEqual([
      { type: 'allowed', op: 'check', scope: 't', tier: 'local' },
      { type: 'limited', op: 'check', scope: 't', tier: 'local', shadowed: true },
      { type: 'reset', op: 'reset', scope: 't', tier: 'local' },
    ]);
  });

  it('lets a closed store failure through, and leaves an open one as it is', async () => {
    const error = new Error('down');
    const store: Store = { tier: 'exact', hit: () => Promise.reject(error) };
    const events: unknown[] = [];
    const closed = await createLimiter({ ...shadow, store, onEvent: (e) => events.push(e) }).check('a');
    expect(closed).toMatchObject({ allowed: true, reason: 'store_error', status: null, retryAfterMs: 1000, shadowed: true, headers: {} });
    const open = await createLimiter({ ...shadow, onStoreError: 'open', store, onEvent: (e) => events.push(e) }).check('a');
    expect(open).toMatchObject({ allowed: true, reason: 'store_error', shadowed: false });
    expect(events).toEqual([
      { type: 'store_error', op: 'check', scope: 't', tier: 'exact', error, shadowed: true },
      { type: 'store_error', op: 'check', scope: 't', tier: 'exact', error },
    ]);
  });

  it('a shadowed hit is not a reservation', async () => {
    const limiter = createLimiter({ ...shadow, store: memoryStore() });
    expect((await limiter.reserve('a', { nowMs: 0 })).reservation).not.toBeNull();
    const shadowed = await limiter.reserve('a', { nowMs: 0 });
    expect(shadowed.decision.shadowed).toBe(true);
    expect(shadowed.reservation).toBeNull();
    expect(await limiter.refund(shadowed.reservation)).toBe(false);
  });

  it('rejects a shadow option that is not a boolean', () => {
    expect(() => createLimiter({ ...base, store: memoryStore(), shadow: 'yes' as never })).toThrow(TypeError);
  });
});

describe('memoryStore', () => {
  it('rejects invalid maxKeys', () => {
    expect(() => memoryStore({ maxKeys: 0 })).toThrow(RangeError);
  });

  it('caps buckets, evicting expired ones and then the least recently hit', async () => {
    const store = memoryStore({ maxKeys: 2 });
    const params = { limit: 1, windowMs: 1000, capacity: 1000, drainMs: 1000 };
    await store.hit('expired', params, 0);
    await store.hit('old', params, 5000);
    await store.hit('new', params, 5001); // evicts `expired`
    expect((await store.hit('old', params, 5002)).allowed).toBe(false); // denied, and still a hit
    await store.hit('newer', params, 5003); // nothing expired: evicts the least recently hit, `new`
    expect((await store.hit('old', params, 5004)).allowed).toBe(false);
    expect((await store.hit('new', params, 5005)).allowed).toBe(true);
  });

  it('keeps a bucket that is being denied, and writes nothing to it', async () => {
    const store = memoryStore({ maxKeys: 2 });
    const params = { limit: 1, windowMs: 1000, capacity: 1000, drainMs: 1000 };
    await store.hit('a', params, 1000);
    await store.hit('b', params, 1000);
    const before = await store.peek!('a', params, 1400);
    expect(await store.hit('a', params, 1400)).toEqual({ allowed: false, level: 600, ts: 1000 });
    // The meter is where it was: the denial neither counted nor moved the timestamp.
    expect(await store.peek!('a', params, 1400)).toEqual(before);
    await store.hit('c', params, 1400); // evicts `b`: `a` was hit since
    expect((await store.peek!('a', params, 1400)).level).toBe(600);
    expect((await store.peek!('b', params, 1400)).level).toBe(0);
    // Nor did it push the expiry back: a full drain after its one counted hit, `a` is free.
    expect(await store.hit('a', params, 2000)).toEqual({ allowed: true, level: 1000, ts: 2000 });
  });

  it('still evicts first a denied bucket that has expired since', async () => {
    const store = memoryStore({ maxKeys: 3 });
    const one = { limit: 1, windowMs: 1000, capacity: 1000, drainMs: 1000 };
    const brief = { limit: 1000, windowMs: 1000, capacity: 1000, drainMs: 1 };
    await store.hit('a', brief, 1000);
    await store.hit('b', one, 1000);
    await store.hit('c', one, 1000);
    // Denied at the same instant, so `a` is the most recent bucket, and it expires at 1001.
    expect((await store.hit('a', brief, 1000)).allowed).toBe(false);
    await store.hit('d', one, 1500);
    for (const key of ['b', 'c', 'd']) expect((await store.peek!(key, one, 1500)).level).toBeGreaterThan(0);
  });

  it('does not reopen the limit of a client being denied while new keys flood the store', async () => {
    // One client at 10 times its limit for 3 windows, while never-seen keys arrive at 12.5 times
    // the cap per window. It gets what it gets with no flood: its burst, then the refill.
    for (const flood of [0, 250]) {
      const [limit, windowMs, cap] = [5, 60_000, 1000];
      const store = memoryStore({ maxKeys: cap });
      const params = { limit, windowMs, capacity: limit * windowMs, drainMs: windowMs };
      const gap = windowMs / (10 * limit);
      const start = 1_800_000_000_000;
      let admitted = 0;
      let others = 0;
      for (let index = 0; index < 3 * 10 * limit; index += 1) {
        if ((await store.hit('abuser', params, start + index * gap)).allowed) admitted += 1;
        for (let extra = 0; extra < flood; extra += 1) {
          expect((await store.hit(`flood:${others}`, params, start + index * gap + 1 + extra * 4)).allowed).toBe(true);
          others += 1;
        }
      }
      // 5 of burst, then one every 12 s for the 178.8 s that follow: 5 + 14.
      expect(admitted).toBe(19);
    }
  });

  it('evicts in the order of the last hit, one bucket for each new one', async () => {
    const store = memoryStore({ maxKeys: 3 });
    const params = { limit: 1, windowMs: 1000, capacity: 2000, drainMs: 2000 };
    const held = async (keys: string[], nowMs: number) => {
      const present: string[] = [];
      for (const key of keys) if (((await store.peek!(key, params, nowMs)).level ?? 0) > 0) present.push(key);
      return present;
    };
    await store.hit('a', params, 5000);
    await store.hit('b', params, 5001);
    await store.hit('c', params, 5002);
    await store.hit('a', params, 5003); // counted: `a` is now the newest write, `b` the oldest
    await store.hit('d', params, 5004); // evicts `b`
    expect(await held(['a', 'b', 'c', 'd'], 5004)).toEqual(['a', 'c', 'd']);
    await store.reset!('c'); // leaves room for one
    await store.hit('e', params, 5005);
    expect(await held(['a', 'c', 'd', 'e'], 5005)).toEqual(['a', 'd', 'e']);
    await store.hit('f', params, 5006); // evicts `a`, written at 5003
    await store.hit('g', params, 5007); // evicts `d`
    expect(await held(['a', 'd', 'e', 'f', 'g'], 5007)).toEqual(['e', 'f', 'g']);
  });

  it('evicts an expired bucket before an older one that is still live, across policies', async () => {
    const store = memoryStore({ maxKeys: 3 });
    const long = { limit: 1, windowMs: 60_000, capacity: 60_000, drainMs: 60_000 };
    const short = { limit: 1, windowMs: 1000, capacity: 1000, drainMs: 1000 };
    await store.hit('long', long, 5000);
    await store.hit('short-1', short, 5001);
    await store.hit('short-2', short, 5002);
    // `short-1` expired at 6001: it goes, not the older `long`. A bucket that is gone reports the
    // time of the peek, one that is held the time of its write.
    await store.hit('short-3', short, 6001);
    expect((await store.peek!('long', long, 6001)).ts).toBe(5000);
    expect((await store.peek!('short-1', short, 6001)).ts).toBe(6001);
    expect((await store.peek!('short-2', short, 6001)).ts).toBe(5002);
    await store.hit('short-4', short, 6001); // nothing has expired: the oldest write goes, `long`
    expect((await store.peek!('long', long, 6001)).ts).toBe(6001);
    expect((await store.peek!('short-2', short, 6001)).ts).toBe(5002);
    // A policy whose buckets are all gone is not in the way of the next eviction.
    await store.hit('long', long, 6001); // evicts `short-2`
    await store.reset!('long');
    await store.hit('short-5', short, 6001);
    await store.hit('short-6', short, 6001); // evicts `short-3`
    expect((await store.peek!('short-3', short, 6001)).level).toBe(0);
    expect((await store.peek!('short-4', short, 6001)).level).toBe(1000);
  });

  it('does the same work for a new key on a full store, whatever the cap', async () => {
    // Counted, not timed: every call into a Map, and every step of an iteration over one.
    const mapWork = (run: () => void): number => {
      const iterator = Object.getPrototypeOf(new Map()[Symbol.iterator]()) as { next: () => unknown };
      const proto = Map.prototype as unknown as Record<string, (...args: unknown[]) => unknown>;
      const originals = { get: proto.get!, set: proto.set!, delete: proto.delete!, has: proto.has!, next: iterator.next };
      let calls = 0;
      const counting = (original: (...args: unknown[]) => unknown) =>
        function (this: unknown, ...args: unknown[]) {
          calls += 1;
          return original.apply(this, args);
        };
      try {
        for (const name of ['get', 'set', 'delete', 'has'] as const) proto[name] = counting(originals[name]);
        iterator.next = counting(originals.next);
        run();
      } finally {
        Object.assign(proto, { get: originals.get, set: originals.set, delete: originals.delete, has: originals.has });
        iterator.next = originals.next;
      }
      return calls;
    };
    const params = { limit: 1, windowMs: 1000, capacity: 1000, drainMs: 1000 };
    const workWhenFull = async (maxKeys: number) => {
      const store = memoryStore({ maxKeys });
      for (let i = 0; i < maxKeys; i++) await store.hit(`k${i}`, params, 5000);
      // The store does all its work before its promise is returned.
      const works = [mapWork(() => void store.hit('new-1', params, 5001)), mapWork(() => void store.hit('new-2', params, 5001))];
      // A bucket that is gone reports the time of the peek, one that is held the time of its write.
      expect((await store.peek!('k0', params, 5001)).ts).toBe(5001);
      expect((await store.peek!('k1', params, 5001)).ts).toBe(5001);
      expect((await store.peek!('k2', params, 5001)).ts).toBe(5000);
      return works;
    };
    const small = await workWhenFull(10);
    expect(await workWhenFull(20_000)).toEqual(small);
    expect(Math.max(...small)).toBeLessThanOrEqual(8);
  });

  it('follows fake timers installed after the store was created', async () => {
    const limiter = createLimiter({ store: memoryStore(), scope: 'fake', limit: 1, windowMs: 60_000, onStoreError: 'closed' });
    vi.useFakeTimers({ toFake: ['Date'] });
    try {
      await limiter.check('k');
      expect((await limiter.check('k')).allowed).toBe(false);
      vi.advanceTimersByTime(60_000);
      expect((await limiter.check('k')).allowed).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('takes its clock from `now`, so tests move time without faking timers', async () => {
    let t = 1_000_000;
    const limiter = createLimiter({
      store: memoryStore({ now: () => t }),
      scope: 'clock',
      limit: 2,
      windowMs: 60_000,
      onStoreError: 'closed',
    });
    await limiter.check('k');
    await limiter.check('k');
    expect((await limiter.check('k')).allowed).toBe(false);
    expect((await limiter.peek('k')).allowed).toBe(false);
    t += 30_000; // one unit comes back
    expect((await limiter.peek('k')).allowed).toBe(true);
    expect((await limiter.check('k')).allowed).toBe(true);
    expect((await limiter.check('k')).allowed).toBe(false);
    // An explicit nowMs still wins over the clock.
    expect((await limiter.check('k', { nowMs: t + 60_000 })).allowed).toBe(true);
  });
});

describe('cloudflareBindingStore', () => {
  it('rejects unsupported periods', () => {
    expect(() => cloudflareBindingStore({ binding: undefined, periodSec: 30 as never })).toThrow(RangeError);
  });
});

describe('ip helpers', () => {
  it('clientKey resolves and masks in one step, or answers null', () => {
    const v6 = { headers: { 'cf-connecting-ip': '2001:db8:1:2:3:4:5:6' } };
    expect(clientKey(v6, { preset: 'cloudflare' })).toBe('2001:db8:1:2::');
    expect(clientKey(v6, { preset: 'cloudflare', ipv6Prefix: 48 })).toBe('2001:db8:1::');
    expect(clientKey({ headers: { 'cf-connecting-ip': '203.0.113.7' } }, { preset: 'cloudflare' })).toBe('203.0.113.7');
    expect(clientKey({ headers: { 'x-forwarded-for': '6.6.6.6, 203.0.113.9' } }, { xff: { hops: 0 } })).toBe('203.0.113.9');
    expect(clientKey({ headers: {} }, { preset: 'cloudflare' })).toBeNull();
    expect(clientKey({ headers: { 'cf-connecting-ip': 'nope' } }, { preset: 'cloudflare' })).toBeNull();
  });

  it('rejects bad options', () => {
    expect(() => maskIp('2001:db8::1', 129)).toThrow(RangeError);
    expect(maskIp('nope', 64)).toBeNull();
    expect(() => resolveClientIp({ headers: {} }, { xff: { hops: -1 } })).toThrow(RangeError);
    expect(() => resolveClientIp({ headers: {} }, { preset: 'nope' as never })).toThrow(RangeError);
  });

  it('supports a custom header, the peer and repeated headers', () => {
    const headers = { 'x-real-ip': '203.0.113.7' };
    expect(resolveClientIp({ headers }, { header: 'X-Real-IP' })).toBe('203.0.113.7');
    expect(resolveClientIp({ headers: {}, peer: '203.0.113.8' }, { peer: true })).toBe('203.0.113.8');
    const repeated = { 'x-forwarded-for': ['6.6.6.6', '203.0.113.9'] };
    expect(resolveClientIp({ headers: repeated }, { xff: { hops: 0 } })).toBe('203.0.113.9');
  });

  it('ipKey groups IPv6 by /64 by default', async () => {
    const context = { headers: { 'cf-connecting-ip': '2001:db8:1:2:3:4:5:6' }, peer: null };
    expect(await ipKey({ preset: 'cloudflare' })(context)).toBe('2001:db8:1:2::');
    expect(await ipKey({ preset: 'cloudflare', ipv6Prefix: 128 })(context)).toBe('2001:db8:1:2:3:4:5:6');
    expect(await ipKey({ preset: 'cloudflare' })({ headers: {}, peer: null })).toBeNull();
  });
});

describe('checkAll', () => {
  const pair = (ipLimit: number, userLimit: number) => {
    let t = 0;
    const store = memoryStore({ now: () => t });
    const make = (scope: string, limit: number) =>
      createLimiter({ store, scope, limit, windowMs: 60_000, onStoreError: 'closed' });
    return { ip: make('ip', ipLimit), user: make('user', userLimit), tick: (ms: number) => void (t += ms) };
  };

  it('counts every limit when all allow, and reports the one with the fewest left', async () => {
    const { ip, user } = pair(10, 3);
    const result = await checkAll([{ limiter: ip, id: 'a' }, { limiter: user, id: 'u' }]);
    expect(result.allowed).toBe(true);
    expect(result.decisions.map((d) => d.remaining)).toEqual([9, 2]);
    expect(result.decision).toBe(result.decisions[1]);
    expect(result.decision.headers.ratelimit).toContain('"user"');
  });

  it('counts nothing when one of them would deny', async () => {
    const { ip, user } = pair(1, 5);
    await ip.check('a');
    const result = await checkAll([{ limiter: user, id: 'u' }, { limiter: ip, id: 'a' }]);
    expect(result.allowed).toBe(false);
    expect(result.decision.status).toBe(429);
    expect(result.decision.headers.ratelimit).toContain('"ip"');
    // The user limit was listed first and still has its whole budget.
    expect((await user.peek('u')).remaining).toBe(5);
  });

  it('answers with the longest wait when several deny', async () => {
    const { ip, user, tick } = pair(1, 1);
    await ip.check('a');
    tick(30_000);
    await user.check('u');
    const result = await checkAll([{ limiter: ip, id: 'a' }, { limiter: user, id: 'u' }]);
    expect(result.allowed).toBe(false);
    expect(result.decisions.map((d) => d.retryAfterMs)).toEqual([30_000, 60_000]);
    expect(result.decision).toBe(result.decisions[1]);
  });

  it('stops at a limit that denies while counting, leaving the earlier ones counted', async () => {
    const { ip, user } = pair(5, 5);
    // A store whose peek always allows: the race where another request took the last unit.
    const racing = createLimiter({
      store: {
        tier: 'exact',
        hit: async () => ({ allowed: false, level: 60_000, ts: 0 }),
        peek: async () => ({ allowed: true, level: 0, ts: 0 }),
      },
      scope: 'race',
      limit: 1,
      windowMs: 60_000,
      onStoreError: 'closed',
    });
    const result = await checkAll([{ limiter: ip, id: 'a' }, { limiter: racing, id: 'r' }, { limiter: user, id: 'u' }]);
    expect(result.allowed).toBe(false);
    expect(result.decisions).toHaveLength(2);
    expect(result.decision).toBe(result.decisions[1]);
    expect((await ip.peek('a')).remaining).toBe(4);
    expect((await user.peek('u')).remaining).toBe(5);
  });

  it('weighs each limit by its own cost', async () => {
    const { ip, user } = pair(10, 10);
    const first = await checkAll([{ limiter: ip, id: 'a' }, { limiter: user, id: 'u', cost: 6 }]);
    expect(first.decisions.map((d) => d.remaining)).toEqual([9, 4]);
    // 6 more do not fit under the user limit, so the ip limit is not spent either.
    const second = await checkAll([{ limiter: ip, id: 'a' }, { limiter: user, id: 'u', cost: 6 }]);
    expect(second.allowed).toBe(false);
    expect((await ip.peek('a')).remaining).toBe(9);
  });

  it('follows onStoreError, needs a list and a store that can peek', async () => {
    const down: Store = { tier: 'exact', hit: () => Promise.reject(new Error('down')), peek: () => Promise.reject(new Error('down')) };
    const closed = createLimiter({ store: down, scope: 'c', limit: 1, windowMs: 1000, onStoreError: 'closed' });
    const refused = await checkAll([{ limiter: closed, id: 'x' }]);
    expect(refused.allowed).toBe(false);
    expect(refused.decision.status).toBe(503);
    const open = createLimiter({ store: down, scope: 'o', limit: 1, windowMs: 1000, onStoreError: 'open' });
    const passed = await checkAll([{ limiter: open, id: 'x' }, { limiter: pair(2, 2).ip, id: 'a' }]);
    expect(passed.allowed).toBe(true);
    // A decision without a count never governs over one with it.
    expect(passed.decision.remaining).toBe(1);
    expect((await checkAll([{ limiter: open, id: 'x' }])).decision.remaining).toBeNull();
    await expect(checkAll([])).rejects.toThrow(RangeError);
    const approximate = createLimiter({
      store: cloudflareBindingStore({ binding: { limit: async () => ({ success: true }) }, periodSec: 60 }),
      scope: 'a',
      limit: 1,
      windowMs: 60_000,
      onStoreError: 'open',
    });
    await expect(checkAll([{ limiter: approximate, id: 'x' }])).rejects.toThrow(TypeError);
  });
});

describe('pending checks', () => {
  const counting = () => {
    const inner = memoryStore();
    const calls: string[] = [];
    const store: Store = {
      tier: 'local',
      hit: (k, p, n) => (calls.push('hit'), inner.hit(k, p, n)),
      peek: (k, p, n) => (calls.push('peek'), inner.peek!(k, p, n)),
      refund: (k, p, s, n) => (calls.push('refund'), inner.refund!(k, p, s, n)),
    };
    const make = (scope: string, limit: number) => createLimiter({ store, scope, limit, windowMs: 60_000, onStoreError: 'closed' });
    return { calls, ip: make('ip', 5), user: make('user', 5) };
  };

  it('settles once: concurrent commits share one result and count once', async () => {
    const { calls, ip, user } = counting();
    const pending = await peekAll([{ limiter: ip, id: 'a' }, { limiter: user, id: 'u' }]);
    expect(pending.settled).toBe(false);
    calls.length = 0;
    const [first, second] = await Promise.all([pending.commit(), pending.commit()]);
    expect(first).toBe(second);
    expect(pending.settled).toBe(true);
    expect(calls).toEqual(['hit', 'hit']);
    await expect(pending.cancel()).rejects.toBeInstanceOf(SettledError);
  });

  it('refuses the other settlement while the first is still running', async () => {
    const { calls, ip, user } = counting();
    const pending = await reserveAll([{ limiter: ip, id: 'a' }, { limiter: user, id: 'u' }]);
    calls.length = 0;
    const cancelling = pending.cancel();
    const error = await pending.commit().catch((thrown: unknown) => thrown);
    expect(error).toBeInstanceOf(SettledError);
    expect((error as Error).name).toBe('SettledError');
    expect(await cancelling).toEqual({ refunded: [true, true] });
    expect(await pending.cancel()).toEqual({ refunded: [true, true] });
    expect(calls).toEqual(['refund', 'refund']);
    expect((await ip.peek('a')).remaining).toBe(5);
  });

  it('reserveAll needs every store to refund, before any store call', async () => {
    const { calls, ip } = counting();
    // An exact store written before refund existed: it peeks and resets, but cannot refund.
    const old = createLimiter({
      store: { tier: 'exact', hit: async () => ({ allowed: true, level: 0, ts: 0 }), peek: async () => ({ allowed: true, level: 0, ts: 0 }) },
      scope: 'old',
      limit: 1,
      windowMs: 1000,
      onStoreError: 'closed',
    });
    await expect(reserveAll([{ limiter: ip, id: 'a' }, { limiter: old, id: 'x' }])).rejects.toThrow(TypeError);
    expect(calls).toEqual([]);
    expect((await peekAll([{ limiter: old, id: 'x' }])).allowed).toBe(true);
  });
});

describe('createLockout', () => {
  const build = (resetOnSuccess?: boolean) => {
    let t = 0;
    const limiter = createLimiter({
      store: memoryStore({ now: () => t }),
      scope: 'failures',
      limit: 2,
      windowMs: 60_000,
      onStoreError: 'closed',
    });
    return { limiter, lockout: createLockout(resetOnSuccess === undefined ? { limiter } : { limiter, resetOnSuccess }), tick: (ms: number) => void (t += ms) };
  };

  it('counts failures only, and refuses even a correct attempt once they are spent', async () => {
    const { lockout, tick } = build();
    const first = await lockout.attempt('u', () => false);
    expect(first).toMatchObject({ allowed: true, verified: false });
    expect(first.decision.remaining).toBe(1);
    await lockout.attempt('u', async () => false);
    const verify = vi.fn(() => true);
    const locked = await lockout.attempt('u', verify);
    expect(locked).toMatchObject({ allowed: false, verified: null });
    expect(locked.decision.status).toBe(429);
    expect(locked.decision.retryAfterMs).toBe(30_000);
    expect(verify).not.toHaveBeenCalled();
    tick(30_000);
    expect(await lockout.attempt('u', verify)).toMatchObject({ allowed: true, verified: true });
    expect(verify).toHaveBeenCalledTimes(1);
  });

  it('a success clears the count, unless told to keep it', async () => {
    const clearing = build();
    await clearing.lockout.attempt('u', () => false);
    await clearing.lockout.attempt('u', () => true);
    expect((await clearing.limiter.peek('u')).remaining).toBe(2);
    for (let i = 0; i < 5; i += 1) expect((await clearing.lockout.attempt('u', () => true)).allowed).toBe(true);

    const keeping = build(false);
    await keeping.lockout.attempt('u', () => false);
    await keeping.lockout.attempt('u', () => true);
    expect((await keeping.limiter.peek('u')).remaining).toBe(1);
  });

  it('counts nothing when verify throws', async () => {
    const { limiter, lockout } = build();
    await expect(lockout.attempt('u', () => Promise.reject(new Error('db down')))).rejects.toThrow('db down');
    expect((await limiter.peek('u')).remaining).toBe(2);
  });

  it('keeps ids apart', async () => {
    const { lockout } = build();
    await lockout.attempt('a', () => false);
    await lockout.attempt('a', () => false);
    expect((await lockout.attempt('a', () => true)).allowed).toBe(false);
    expect((await lockout.attempt('b', () => true)).allowed).toBe(true);
  });
});

describe('createLockout with several limits', () => {
  const limits = (mode?: 'advisory' | 'reserve') => {
    const store = memoryStore();
    const subject = createLimiter({ store, scope: 'subject', limit: 2, windowMs: 60_000, onStoreError: 'closed' });
    const ip = createLimiter({ store, scope: 'ip', limit: 3, windowMs: 60_000, onStoreError: 'closed' });
    const lockout = createLockout<{ name: string; ip: string }>({
      limits: [
        { limiter: subject, idOf: (s) => s.name },
        { limiter: ip, idOf: (s) => s.ip, resetOnSuccess: false },
      ],
      ...(mode ? { mode } : {}),
    });
    return { subject, ip, lockout };
  };

  it('counts a failure under every limit and reports each decision', async () => {
    const { subject, ip, lockout } = limits();
    const failed = await lockout.attempt({ name: 'u', ip: 'a' }, () => false);
    expect(failed.decisions.map((d) => d.remaining)).toEqual([1, 2]);
    const passed = await lockout.attempt({ name: 'u', ip: 'a' }, () => true);
    expect(passed).toMatchObject({ allowed: true, verified: true, resets: [true, null] });
    expect((await subject.peek('u')).remaining).toBe(2);
    expect((await ip.peek('a')).remaining).toBe(2);
  });

  it('in reserve mode, concurrent gates never run more verifications than the limit has units', async () => {
    const { lockout } = limits('reserve');
    const gates = await Promise.all(Array.from({ length: 5 }, () => lockout.begin({ name: 'u', ip: 'a' })));
    expect(gates.filter((g) => g.allowed)).toHaveLength(2);
    const [first] = gates;
    const [one, two] = await Promise.all([first!.succeed(), first!.succeed()]);
    expect(one).toBe(two);
    await expect(first!.fail()).rejects.toBeInstanceOf(SettledError);
  });

  it('refuses what it cannot do before any store call', async () => {
    const { subject } = limits();
    expect(() => createLockout({ limits: [] })).toThrow(RangeError);
    expect(() => createLockout({ limits: [{ limiter: subject }], mode: 'other' as 'reserve' })).toThrow(TypeError);
    // A subject that is not a string needs idOf: the types say so, and so does the runtime.
    const loose = createLockout<unknown>({ limits: [{ limiter: subject } as never] });
    await expect(loose.begin({ name: 'u' })).rejects.toThrow(TypeError);
    expect((await subject.peek('u')).remaining).toBe(2);
    const named = createLockout({ limits: [{ limiter: subject }] });
    expect((await named.begin('u')).allowed).toBe(true);
  });
});
