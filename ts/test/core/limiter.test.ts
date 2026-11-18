import { describe, expect, it, vi } from 'vitest';
import { checkAll, createLimiter, createLockout, memoryStore, SPEC_VERSION, type Store } from '../../src/index.js';
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

describe('memoryStore', () => {
  it('rejects invalid maxKeys', () => {
    expect(() => memoryStore({ maxKeys: 0 })).toThrow(RangeError);
  });

  it('caps buckets, evicting expired ones and then the oldest', async () => {
    const store = memoryStore({ maxKeys: 2 });
    const params = { limit: 1, windowMs: 1000, capacity: 1000, drainMs: 1000 };
    await store.hit('expired', params, 0);
    await store.hit('old', params, 5000);
    await store.hit('new', params, 5001); // evicts `expired`
    expect((await store.hit('old', params, 5002)).allowed).toBe(false);
    await store.hit('newer', params, 5003); // nothing expired: evicts the oldest write, `old`
    expect((await store.hit('new', params, 5004)).allowed).toBe(false);
    expect((await store.hit('old', params, 5005)).allowed).toBe(true);
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
