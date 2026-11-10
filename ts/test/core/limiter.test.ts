import { describe, expect, it, vi } from 'vitest';
import { createLimiter, memoryStore, SPEC_VERSION, type Store } from '../../src/index.js';
import { cloudflareBindingStore } from '../../src/stores/cloudflare.js';
import { ipKey } from '../../src/adapters/shared.js';
import { maskIp, resolveClientIp } from '../../src/ip/index.js';

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
});

describe('cloudflareBindingStore', () => {
  it('rejects unsupported periods', () => {
    expect(() => cloudflareBindingStore({ binding: undefined, periodSec: 30 as never })).toThrow(RangeError);
  });
});

describe('ip helpers', () => {
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
