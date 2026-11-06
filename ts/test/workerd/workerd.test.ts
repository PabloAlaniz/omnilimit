import { randomUUID } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { Miniflare } from 'miniflare';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { vectorsOf, type Vector } from '../support/vectors.js';

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
    const want = vector.steps[index].expect;
    expect(
      { allowed: d.allowed, reason: d.reason, status: d.status, remaining: d.remaining, reset_ms: d.resetMs, retry_after_ms: d.retryAfterMs, headers: d.headers },
      `${vector.id} step ${index}`,
    ).toEqual({
      allowed: want.allowed,
      reason: want.reason,
      status: want.status,
      remaining: want.remaining,
      reset_ms: want.reset_ms,
      retry_after_ms: want.retry_after_ms,
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
    for (const vector of vectorsOf('concurrency')) {
      const result = await call({ op: 'concurrency', store: 'do', ...vector, salt: randomUUID() });
      expect(result).toEqual({ allowed: vector.expect_allowed, errors: 0 });
    }
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
