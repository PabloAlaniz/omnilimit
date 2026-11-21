import { randomUUID } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { Miniflare } from 'miniflare';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { expectConcurrency, vectorsOf, type Vector } from '../support/vectors.js';

const here = dirname(fileURLToPath(import.meta.url));
let mf: Miniflare;

async function call(body: unknown): Promise<Vector> {
  const response = await mf.dispatchFetch('http://x/', { method: 'POST', body: JSON.stringify(body) });
  const text = await response.text();
  if (!response.ok) throw new Error(text);
  return JSON.parse(text);
}

beforeAll(async () => {
  const bundle = await build({
    entryPoints: [join(here, 'worker.ts')],
    bundle: true,
    format: 'esm',
    platform: 'neutral',
    mainFields: ['module', 'main'],
    external: ['cloudflare:workers'],
    write: false,
  });
  mf = new Miniflare({
    modules: true,
    script: bundle.outputFiles[0]!.text,
    compatibilityDate: '2026-07-01',
    durableObjects: { OMNI: { className: 'OmnilimitDurableObject', useSQLite: true } },
    ratelimits: { RL: { namespace_id: '1001', simple: { limit: 2, period: 60 } } },
  });
  await mf.ready;
}, 60_000);

afterAll(async () => {
  await mf?.dispose();
});

function compare(vector: Vector, decisions: Vector[]): void {
  decisions.forEach((d, index) => {
    if (vector.steps[index].op === 'reset') {
      expect(d, `${vector.id} step ${index}`).toEqual({ reset: true });
      return;
    }
    if (vector.steps[index].op === 'refund') {
      expect(d, `${vector.id} step ${index}`).toEqual({ refunded: vector.steps[index].expect.refunded });
      return;
    }
    const want = vector.steps[index].expect;
    expect(
      {
        allowed: d.allowed,
        reason: d.reason,
        status: d.status,
        remaining: d.remaining,
        reset_ms: d.resetMs,
        retry_after_ms: d.retryAfterMs,
        shadowed: d.shadowed,
        headers: d.headers,
      },
      `${vector.id} step ${index}`,
    ).toEqual({
      allowed: want.allowed,
      reason: want.reason,
      status: want.status,
      remaining: want.remaining,
      reset_ms: want.reset_ms,
      retry_after_ms: want.retry_after_ms,
      // Vectors write `shadowed` only where it is true.
      shadowed: want.shadowed ?? false,
      headers: want.headers['draft-11'],
    });
  });
}

describe('workerd', () => {
  const sequences = vectorsOf('sequence');

  it.each(sequences.map((v): [string, Vector] => [v.id, v]))('memory: %s', async (_id, vector) => {
    compare(vector, await call({ op: 'sequence', store: 'memory', ...vector }));
  });

  it.each(sequences.map((v): [string, Vector] => [v.id, v]))('durable object: %s', async (_id, vector) => {
    compare(vector, await call({ op: 'sequence', store: 'do', ...vector, salt: `${vector.salt}:${randomUUID()}` }));
  });

  it('durable object: concurrency allows exactly the burst', async () => {
    for (const vector of vectorsOf('concurrency').filter((v) => !v.recipe)) {
      const result = await call({ op: 'concurrency', store: 'do', ...vector, salt: randomUUID() });
      expect(result).toEqual({ allowed: vector.expect_allowed, errors: 0 });
    }
  });

  it.each(vectorsOf('concurrency').filter((v) => v.recipe).map((v): [string, Vector] => [v.id, v]))(
    'durable object: %s',
    async (_id, vector) => {
      expectConcurrency(vector, await call({ op: 'recipe-concurrency', store: 'do', vector, salt: `:${randomUUID()}` }));
    },
  );

  // Requests sent to workerd at once, each its own invocation, on one object per bucket.
  const at = 1_700_000_000_000;
  const W = 60_000;

  it('durable object: concurrent requests reserve and refund, and the bucket holds what stayed reserved', async () => {
    const salt = randomUUID();
    const results = await Promise.all(Array.from({ length: 24 }, (_, i) => call({ op: 'reserve-refund', store: 'do', salt, at_ms: at, keep: i % 3 === 0 })));
    expect(results.filter((r) => r.error)).toEqual([]);
    const kept = results.filter((r) => r.kept).length;
    // Refunds free units for later requests, so how many got one depends on the order; but never
    // more than the burst stayed, and the bucket holds exactly those.
    // The first hit to reach the object is allowed, whatever the order.
    expect(results.filter((r) => r.allowed).length).toBeGreaterThanOrEqual(Math.max(1, kept));
    expect(kept).toBeLessThanOrEqual(3);
    expect(await call({ op: 'levels', store: 'do', salt, at_ms: at, buckets: [['r', 3, 'a']] })).toEqual({ r: kept * W });
  });

  it('durable object: concurrent reserveAll across two objects keeps exactly what was committed', async () => {
    const salt = randomUUID();
    const results = await Promise.all(Array.from({ length: 24 }, (_, i) => call({ op: 'reserve-all', store: 'do', salt, at_ms: at, keep: i % 2 === 0 })));
    expect(results.reduce((sum, r) => sum + r.errors, 0)).toBe(0);
    const kept = results.filter((r) => r.kept).length;
    expect(results.filter((r) => r.allowed).length).toBeGreaterThanOrEqual(Math.max(1, kept));
    expect(kept).toBeLessThanOrEqual(3);
    // Compensations and cancels gave back every unit they took, in both objects.
    const levels = await call({ op: 'levels', store: 'do', salt, at_ms: at, buckets: [['a', 4, 'x'], ['b', 3, 'y']] });
    expect(levels).toEqual({ a: kept * W, b: kept * W });
  });

  it('durable object: peeks racing with hits never take quota', async () => {
    for (const vector of vectorsOf('concurrency').filter((v) => !v.recipe)) {
      const result = await call({ op: 'mixed-concurrency', store: 'do', ...vector, hits: vector.hits * 2, salt: randomUUID() });
      expect(result).toEqual({ allowed: vector.expect_allowed, errors: 0 });
    }
  });

  it('durable object: reset with the real clock clears the bucket and its alarm', async () => {
    const result = await call({ op: 'real-reset', store: 'do', salt: randomUUID() });
    expect(result.first.allowed).toBe(true);
    expect(result.denied.reason).toBe('limited');
    expect(result.reset).toBe(true);
    expect(result.after.allowed).toBe(true);
  });

  it('durable object: refund with the real clock gives the unit back once', async () => {
    const result = await call({ op: 'real-refund', store: 'do', salt: randomUUID() });
    expect(result.denied.reason).toBe('limited');
    expect(result.refunded).toBe(true);
    expect(result.again).toBe(false);
    expect(result.after.allowed).toBe(true);
  });

  it('durable object: smoke with the real clock', async () => {
    const [first, second] = await call({ op: 'real-clock', store: 'do', salt: randomUUID() });
    expect(first.allowed).toBe(true);
    expect(second.reason).toBe('limited');
    expect(second.tier).toBe('exact');
  });

  it('rate limit binding: real binding, reduced decision', async () => {
    const decisions = await call({ op: 'binding' });
    expect(decisions.map((d: Vector) => d.allowed)).toEqual([true, true, false]);
    expect(decisions[2]).toMatchObject({ status: 429, tier: 'approximate', remaining: null, retryAfterMs: 60_000, headers: { 'retry-after': '60' } });
  });

  it('resolves IPs with the runtime Headers object', async () => {
    const [vector] = vectorsOf('ip');
    for (const c of vector.cases) {
      expect((await call({ op: 'ip', case: c })).ip, c.name).toBe(c.expect);
    }
  });
});
