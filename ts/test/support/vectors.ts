import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect } from 'vitest';
import { createLimiter } from '../../src/core/limiter.js';
import { buildHeaders } from '../../src/core/headers.js';
import type { HitParams, RawHit, Store } from '../../src/core/types.js';

const here = dirname(fileURLToPath(import.meta.url));
export const SPEC_DIR = join(here, '..', '..', '..', 'spec');
const VECTOR_DIR = join(SPEC_DIR, 'vectors');

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Vector = any;

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : path.endsWith('.json') ? [path] : [];
  });
}

export const allVectors: Vector[] = walk(VECTOR_DIR).map((path) => {
  const vector = JSON.parse(readFileSync(path, 'utf8'));
  // A vector's id doubles as its path, so a renamed file cannot silently fall out of a suite.
  expect(vector.id).toBe(relative(VECTOR_DIR, path).replace(/\.json$/, ''));
  return vector;
});

export const vectorsOf = (kind: string): Vector[] => allVectors.filter((vector) => vector.kind === kind);

export function toPolicy(policy: { limit: number; window_ms: number; burst: number }) {
  return { limit: policy.limit, windowMs: policy.window_ms, burst: policy.burst };
}

/** Run one sequence vector against a store and compare decisions, raw state and headers. */
export async function runSequence(vector: Vector, store: Store, saltSuffix = ''): Promise<void> {
  let lastRaw: RawHit | undefined;
  const recording: Store = {
    tier: store.tier,
    async hit(key: string, params: HitParams, nowMs?: number) {
      lastRaw = await store.hit(key, params, nowMs);
      return lastRaw;
    },
  };
  let profiles: Record<string, Record<string, string>> = {};
  const limiter = createLimiter({
    ...toPolicy(vector.policy),
    store: recording,
    scope: vector.scope,
    salt: `${vector.salt}${saltSuffix}`,
    onStoreError: 'closed',
    storeTimeoutMs: 10_000,
    headers: (input) => {
      profiles = { 'draft-11': buildHeaders('draft-11', input), legacy: buildHeaders('legacy', input) };
      return {};
    },
  });
  for (const [index, step] of vector.steps.entries()) {
    const decision = await limiter.check(step.id, { nowMs: step.at_ms });
    const context = `${vector.id} step ${index}`;
    const { expect: want } = step;
    expect(
      {
        allowed: decision.allowed,
        reason: decision.reason,
        status: decision.status,
        remaining: decision.remaining,
        reset_ms: decision.resetMs,
        retry_after_ms: decision.retryAfterMs,
        state: { level: lastRaw?.level, ts: lastRaw?.ts },
        headers: profiles,
      },
      context,
    ).toEqual(want);
  }
}

export async function runConcurrency(vector: Vector, store: Store, saltSuffix = ''): Promise<void> {
  const limiter = createLimiter({
    ...toPolicy(vector.policy),
    store,
    scope: vector.scope,
    salt: `${vector.salt}${saltSuffix}`,
    onStoreError: 'closed',
    storeTimeoutMs: 30_000,
  });
  const decisions = await Promise.all(
    Array.from({ length: vector.hits as number }, () => limiter.check(vector.id_value, { nowMs: vector.at_ms })),
  );
  expect(decisions.filter((decision) => decision.reason === 'store_error')).toEqual([]);
  expect(decisions.filter((decision) => decision.allowed)).toHaveLength(vector.expect_allowed);
}
