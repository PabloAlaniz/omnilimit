import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import express from 'express';
import { Hono } from 'hono';
import { afterEach, describe, expect, it } from 'vitest';
import { createLimiter, memoryStore, type Store } from '../../src/index.js';
import { ipKey as expressIpKey, rateLimit as expressRateLimit } from '../../src/adapters/express.js';
import { ipKey as honoIpKey, rateLimit as honoRateLimit } from '../../src/adapters/hono.js';
import { checkRateLimit, ipKey as nextIpKey, withRateLimit } from '../../src/adapters/next.js';

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
});
