import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import express from 'express';
import { Hono } from 'hono';
import { afterEach, describe, expect, it } from 'vitest';
import { createLimiter, memoryStore, type Store } from '../../src/index.js';
import { ipKey as expressIpKey, rateLimit as expressRateLimit } from '../../src/adapters/express.js';
import { ipKey as honoIpKey, rateLimit as honoRateLimit } from '../../src/adapters/hono.js';
import { checkRateLimit, ipKey as nextIpKey, withRateLimit } from '../../src/adapters/next.js';
import { checkRateLimit as webCheck, ipKey as webIpKey, withRateLimit as webWrap } from '../../src/adapters/web.js';

const limiter = (limit = 2, store: Store = memoryStore()) =>
  createLimiter({ limit, windowMs: 60_000, store, scope: 'a', onStoreError: 'closed' });
const broken: Store = { tier: 'exact', hit: () => Promise.reject(new Error('down')) };
const cf = (ip: string) => ({ 'cf-connecting-ip': ip });

describe('hono', () => {
  const build = (options: Partial<Parameters<typeof honoRateLimit>[0]> = {}) => {
    const app = new Hono();
    app.use('*', honoRateLimit({ limiter: limiter(), key: honoIpKey({ preset: 'cloudflare' }), onMissingKey: 'reject', ...options }));
    app.get('/', (c) => c.text('ok'));
    return app;
  };

  it('passes with headers, then responds 429 with retry-after', async () => {
    const app = build();
    const first = await app.request('/', { headers: cf('203.0.113.1') });
    expect(first.status).toBe(200);
    expect(first.headers.get('ratelimit')).toBe('"a";r=1;t=30');
    await app.request('/', { headers: cf('203.0.113.1') });
    const denied = await app.request('/', { headers: cf('203.0.113.1') });
    expect(denied.status).toBe(429);
    expect(denied.headers.get('retry-after')).toBe('30');
    expect(await denied.json()).toEqual({ error: 'rate_limited' });
  });

  it('rejects an unidentified client when asked to', async () => {
    const response = await build().request('/');
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: 'client_not_identified' });
  });

  it('can skip or share unidentified clients', async () => {
    const skip = build({ onMissingKey: 'skip' });
    for (let i = 0; i < 5; i += 1) expect((await skip.request('/')).status).toBe(200);
    const shared = build({ onMissingKey: 'shared' });
    const statuses = [];
    for (let i = 0; i < 3; i += 1) statuses.push((await shared.request('/')).status);
    expect(statuses).toEqual([200, 200, 429]);
  });

  it('responds 503 when closed and the store is down', async () => {
    const app = build({ limiter: limiter(2, broken) });
    const response = await app.request('/', { headers: cf('203.0.113.1') });
    expect(response.status).toBe(503);
    expect(response.headers.get('retry-after')).toBe('1');
    expect(await response.json()).toEqual({ error: 'rate_limiter_unavailable' });
  });

  it('reads the Node peer and supports a custom one', async () => {
    const direct = build({ key: honoIpKey({ preset: 'direct' }) });
    const env = { incoming: { socket: { remoteAddress: '203.0.113.2' } } };
    expect((await direct.request('/', {}, env)).status).toBe(200);
    const custom = build({ key: honoIpKey({ preset: 'direct' }), peer: () => '203.0.113.3' });
    expect((await custom.request('/')).status).toBe(200);
  });

  it('rotating X-Forwarded-For cannot evade the limit', async () => {
    const app = build({ limiter: limiter(1), key: honoIpKey({ xff: { hops: 1 } }) });
    const statuses = [];
    for (const spoof of ['1.1.1.1', '2.2.2.2', '3.3.3.3']) {
      const response = await app.request('/', { headers: { 'x-forwarded-for': `${spoof}, 203.0.113.9, 198.51.100.2` } });
      statuses.push(response.status);
    }
    expect(statuses).toEqual([200, 429, 429]);
  });

  it('onDenied can answer in the app format, or throw to app.onError with the headers kept', async () => {
    const custom = build({
      limiter: limiter(1),
      onDenied: (denial, c) => new Response(`${denial.error}:${denial.decision?.remaining}`, { status: denial.status, headers: denial.headers }),
    });
    await custom.request('/', { headers: cf('203.0.113.1') });
    const denied = await custom.request('/', { headers: cf('203.0.113.1') });
    expect([denied.status, await denied.text(), denied.headers.get('retry-after')]).toEqual([429, 'rate_limited:0', '60']);

    const app = new Hono();
    app.onError((error, c) => c.json({ code: (error as Error).message }, 429));
    app.use(
      '*',
      honoRateLimit({
        limiter: limiter(1),
        key: honoIpKey({ preset: 'cloudflare' }),
        onMissingKey: 'reject',
        onDenied: (denial) => {
          throw new Error(denial.error);
        },
      }),
    );
    app.get('/', (c) => c.text('ok'));
    await app.request('/', { headers: cf('203.0.113.1') });
    const thrown = await app.request('/', { headers: cf('203.0.113.1') });
    expect(await thrown.json()).toEqual({ code: 'rate_limited' });
    expect(thrown.headers.get('retry-after')).toBe('60');
  });

  it('onDenied receives client_not_identified without a decision', async () => {
    const seen: unknown[] = [];
    const app = build({
      onDenied: (denial) => {
        seen.push([denial.error, denial.status, denial.decision]);
        return new Response(null, { status: 400 });
      },
    });
    expect((await app.request('/')).status).toBe(400);
    expect(seen).toEqual([['client_not_identified', 400, null]]);
  });
  it('skip lets a request through without counting or headers', async () => {
    const app = build({ skip: ({ c }) => new URL(c.req.raw.url).pathname === '/health' });
    app.get('/health', (c) => c.text('up'));
    for (let i = 0; i < 5; i += 1) {
      const response = await app.request('/health', { headers: cf('203.0.113.1') });
      expect(response.status).toBe(200);
      expect(response.headers.get('ratelimit')).toBeNull();
    }
    // Nothing was counted: the whole budget is still there.
    const statuses = [];
    for (let i = 0; i < 3; i += 1) statuses.push((await app.request('/', { headers: cf('203.0.113.1') })).status);
    expect(statuses).toEqual([200, 200, 429]);
  });

  it('finds the limiter per request, for example from env', async () => {
    type Bindings = { limit: number };
    const cache = new Map<number, ReturnType<typeof limiter>>();
    const app = new Hono<{ Bindings: Bindings }>();
    app.use(
      '*',
      honoRateLimit({
        limiter: async ({ c }) => {
          const { limit } = c.env as Bindings;
          if (!cache.has(limit)) cache.set(limit, limiter(limit));
          return cache.get(limit) as ReturnType<typeof limiter>;
        },
        key: honoIpKey({ preset: 'cloudflare' }),
        onMissingKey: 'reject',
      }),
    );
    app.get('/', (c) => c.text('ok'));
    const statuses = [];
    for (let i = 0; i < 2; i += 1) statuses.push((await app.request('/', { headers: cf('203.0.113.1') }, { limit: 1 })).status);
    expect(statuses).toEqual([200, 429]);
    // Another env gets another limiter, and the function is not asked when the key is rejected.
    expect((await app.request('/', { headers: cf('203.0.113.1') }, { limit: 5 })).status).toBe(200);
    expect((await app.request('/', {}, { limit: 9 })).status).toBe(400);
    expect(cache.has(9)).toBe(false);
  });
});

describe('web', () => {
  type Env = { limiter: ReturnType<typeof limiter>; off?: boolean };
  const request = (ip = '203.0.113.1') => new Request('https://x.test/', { headers: cf(ip) });

  it('checks a plain Request, with the limiter taken from what the caller passes', async () => {
    const env: Env = { limiter: limiter(1) };
    const options = {
      limiter: ({ extra }: { extra: Env }) => extra.limiter,
      key: webIpKey({ preset: 'cloudflare' }),
      onMissingKey: 'reject' as const,
    };
    const first = await webCheck(request(), options, env);
    expect(first.denied).toBeUndefined();
    expect(first.headers).toHaveProperty('ratelimit');
    const second = await webCheck(request(), options, env);
    expect(second.denied?.status).toBe(429);
    expect(await second.denied?.json()).toEqual({ error: 'rate_limited' });
  });

  it('skip sees the request and the extra, and sends no headers', async () => {
    const env: Env = { limiter: limiter(1), off: true };
    const options = {
      limiter: ({ extra }: { extra: Env }) => extra.limiter,
      key: webIpKey({ preset: 'cloudflare' }),
      onMissingKey: 'reject' as const,
      skip: ({ extra, request: r }: { extra: Env; request: Request }) => extra.off === true && r.method === 'GET',
    };
    for (let i = 0; i < 3; i += 1) expect(await webCheck(request(), options, env)).toEqual({ headers: {} });
    env.off = false;
    expect((await webCheck(request(), options, env)).headers).toHaveProperty('ratelimit');
  });

  it('reads the peer when the runtime can give one', async () => {
    const options = {
      limiter: limiter(1),
      key: webIpKey({ preset: 'direct' }),
      onMissingKey: 'reject' as const,
    };
    expect((await webCheck(request(), options)).denied?.status).toBe(400);
    const withPeer = { ...options, peer: ({ extra }: { extra: { address: string } }) => extra.address };
    expect((await webCheck(request(), withPeer, { address: '198.51.100.9' })).denied).toBeUndefined();
    expect((await webCheck(request(), withPeer, { address: '198.51.100.9' })).denied?.status).toBe(429);
    expect((await webCheck(request(), withPeer, { address: '198.51.100.10' })).denied).toBeUndefined();
  });

  it('cost weighs a request by what it carries', async () => {
    const options = {
      limiter: limiter(5),
      key: webIpKey({ preset: 'cloudflare' }),
      onMissingKey: 'reject' as const,
      cost: async ({ request: r }: { request: Request }) => Number(r.headers.get('x-items') ?? 1),
    };
    const batch = (items: number) =>
      new Request('https://x.test/', { headers: { ...cf('203.0.113.1'), 'x-items': String(items) } });
    expect((await webCheck(batch(3), options)).headers?.ratelimit).toBe('"a";r=2;t=36');
    expect((await webCheck(batch(3), options)).denied?.status).toBe(429);
    expect((await webCheck(batch(2), options)).denied).toBeUndefined();
    await expect(webCheck(batch(9), options)).rejects.toThrow(RangeError);
  });

  it('wraps a fetch-style handler and hands its second argument on as extra', async () => {
    const env: Env = { limiter: limiter(1) };
    const handler = webWrap(async (_request: Request, e: Env) => new Response(e.off ? 'off' : 'on'), {
      limiter: ({ extra }) => extra.limiter,
      key: webIpKey({ preset: 'cloudflare' }),
      onMissingKey: 'shared',
    });
    const first = await handler(request(), env);
    expect(await first.text()).toBe('on');
    expect(first.headers.get('ratelimit')).toBe('"a";r=0;t=60');
    expect((await handler(request(), env)).status).toBe(429);
  });
});

describe('next', () => {
  const options = { limiter: limiter(1), key: nextIpKey({ preset: 'vercel' }), onMissingKey: 'reject' as const };
  const request = () => new Request('https://x.test/', { headers: { 'x-vercel-forwarded-for': '203.0.113.1' } });

  it('wraps a route handler and merges headers, even when immutable', async () => {
    const handler = withRateLimit(async (_request: Request, context: { id: string }) => {
      const upstream = new Response(context.id, { headers: { 'x-own': '1' } });
      Object.freeze(upstream);
      return upstream;
    }, { ...options, limiter: limiter(1) });
    const first = await handler(request(), { id: 'ok' });
    expect(first.status).toBe(200);
    expect(await first.text()).toBe('ok');
    expect(first.headers.get('x-own')).toBe('1');
    expect(first.headers.get('ratelimit')).toBe('"a";r=0;t=60');
    const denied = await handler(request(), { id: 'ok' });
    expect(denied.status).toBe(429);
  });

  it('checkRateLimit returns a denial or the headers', async () => {
    const shared = { ...options, limiter: limiter(1) };
    const first = await checkRateLimit(request(), shared);
    expect(first.denied).toBeUndefined();
    expect(first.headers).toHaveProperty('ratelimit');
    const second = await checkRateLimit(request(), shared);
    expect(second.denied?.status).toBe(429);
  });

  it('onDenied builds the denial response', async () => {
    const shared = {
      ...options,
      limiter: limiter(1),
      onDenied: (denial: { status: number; headers: Record<string, string> }) =>
        Response.json({ code: 'slow_down' }, { status: denial.status, headers: denial.headers }),
    };
    await checkRateLimit(request(), shared);
    const { denied } = await checkRateLimit(request(), shared);
    expect(denied?.headers.get('retry-after')).toBe('60');
    expect(await denied?.json()).toEqual({ code: 'slow_down' });
  });
});

describe('express', () => {
  let server: Server | undefined;
  afterEach(() => server?.close());

  async function start(options: Partial<Parameters<typeof expressRateLimit>[0]> = {}) {
    const app = express();
    app.use(expressRateLimit({ limiter: limiter(1), key: expressIpKey({ preset: 'direct' }), onMissingKey: 'reject', ...options }));
    app.get('/', (_req, res) => {
      res.send('ok');
    });
    app.use((error: Error, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
      res.status(500).json({ message: error.message });
    });
    server = createServer(app);
    await new Promise<void>((resolve) => server!.listen(0, '127.0.0.1', resolve));
    return `http://127.0.0.1:${(server.address() as AddressInfo).port}/`;
  }

  it('limits by socket address over real HTTP, ignoring X-Forwarded-For', async () => {
    const url = await start();
    const first = await fetch(url, { headers: { 'x-forwarded-for': '1.1.1.1' } });
    expect(first.status).toBe(200);
    expect(first.headers.get('ratelimit')).toBe('"a";r=0;t=60');
    const second = await fetch(url, { headers: { 'x-forwarded-for': '2.2.2.2' } });
    expect(second.status).toBe(429);
    expect(second.headers.get('retry-after')).toBe('60');
    expect(await second.json()).toEqual({ error: 'rate_limited' });
  });

  it('forwards key errors to the error handler', async () => {
    const url = await start({
      key: () => {
        throw new Error('bad key');
      },
    });
    const response = await fetch(url);
    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({ message: 'bad key' });
  });

  it('onDenied answers with the headers already set, and its errors reach next', async () => {
    const url = await start({
      onDenied: (denial, _req, res) => {
        res.status(denial.status).json({ code: 'slow_down' });
      },
    });
    await fetch(url);
    const denied = await fetch(url);
    expect([denied.status, denied.headers.get('retry-after')]).toEqual([429, '60']);
    expect(await denied.json()).toEqual({ code: 'slow_down' });
    server?.close();

    const failing = await start({
      onDenied: async () => {
        throw new Error('custom denial');
      },
    });
    await fetch(failing);
    const response = await fetch(failing);
    expect(await response.json()).toEqual({ message: 'custom denial' });
  });
  it('skip passes the request on without counting', async () => {
    const calls: string[] = [];
    const middleware = expressRateLimit({
      limiter: limiter(1),
      key: () => 'k',
      onMissingKey: 'reject',
      skip: ({ req }) => req.headers['x-internal'] === '1',
    });
    const res = { setHeader: (name: string) => calls.push(name), status: () => ({ json: () => calls.push('json') }) };
    const run = (headers: Record<string, string>) =>
      new Promise<void>((resolve) => void middleware({ headers }, res, () => (calls.push('next'), resolve())));
    await run({ 'x-internal': '1' });
    await run({ 'x-internal': '1' });
    expect(calls).toEqual(['next', 'next']);
    await run({});
    expect(calls.filter((call) => call === 'next')).toHaveLength(3);
  });
});
