import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { Ajv } from 'ajv';
import { describe, expect, it } from 'vitest';
import { memoryStore } from '../../src/stores/memory.js';
import { assertScope, resolvePolicy } from '../../src/core/gcra.js';
import { deriveKey } from '../../src/core/key.js';
import { createLimiter } from '../../src/core/limiter.js';
import { cloudflareBindingStore } from '../../src/stores/cloudflare.js';
import { canonicalIp, maskIp, resolveClientIp } from '../../src/ip/index.js';
import type { Store } from '../../src/core/types.js';
import { allVectors, runConcurrency, runSequence, SPEC_DIR, toPolicy, vectorsOf, type Vector } from '../support/vectors.js';

describe('vector files', () => {
  it('cover every kind', () => {
    const kinds = new Set(allVectors.map((vector) => vector.kind));
    expect([...kinds].sort()).toEqual(['concurrency', 'contract', 'failure', 'ip', 'key', 'sequence']);
    expect(vectorsOf('sequence').length).toBeGreaterThanOrEqual(50);
  });

  it('match the schema', () => {
    const schema = JSON.parse(readFileSync(join(SPEC_DIR, 'schemas', 'vector.schema.json'), 'utf8'));
    const validate = new Ajv({ allErrors: true }).compile(schema);
    for (const vector of allVectors) {
      expect(validate(vector), `${vector.id}: ${JSON.stringify(validate.errors)}`).toBe(true);
    }
  });
});

describe('sequence vectors on the memory store', () => {
  it.each(vectorsOf('sequence').map((v): [string, Vector] => [v.id, v]))('%s', async (_id, vector) => {
    await runSequence(vector, memoryStore());
  });
});

describe('concurrency on the memory store', () => {
  it.each(vectorsOf('concurrency').map((v): [string, Vector] => [v.id, v]))('%s', async (_id, vector) => {
    await runConcurrency(vector, memoryStore());
  });
});

describe('key vectors', () => {
  it('derive the exact keys', async () => {
    for (const vector of vectorsOf('key')) {
      for (const c of vector.cases) {
        const key = await deriveKey(c.scope, resolvePolicy(toPolicy(c.policy)), c.salt, c.id);
        expect(key, JSON.stringify(c)).toBe(c.expect);
      }
    }
  });
});

describe('ip vectors', () => {
  const [vector] = vectorsOf('ip');
  it.each<[string, Vector]>(vector.cases.map((c: Vector) => [c.name, c]))('%s', (_name, c) => {
    const strategy = c.strategy.preset ? { preset: c.strategy.preset } : { xff: c.strategy.xff };
    expect(resolveClientIp({ headers: c.headers, peer: c.peer }, strategy)).toBe(c.expect);
    // A fetch `Headers` object must behave the same as a plain Node headers record.
    expect(resolveClientIp({ headers: new Headers(c.headers), peer: c.peer }, strategy)).toBe(c.expect);
  });
  it.each<[string, Vector]>(vector.masks.map((m: Vector) => [`${m.ip}/${m.prefix}`, m]))('mask %s', (_name, m) => {
    expect(maskIp(m.ip, m.prefix)).toBe(m.expect);
  });
});

describe('failure vectors', () => {
  it.each(vectorsOf('failure').map((v): [string, Vector] => [v.id, v]))('%s', async (_id, vector) => {
    const stores: Record<string, Store> = {
      error: {
        tier: 'exact',
        hit: async () => {
          throw new Error('store down');
        },
      },
      timeout: { tier: 'exact', hit: () => new Promise(() => {}) },
      missing_binding: cloudflareBindingStore({ binding: undefined, periodSec: 60 }),
    };
    const limiter = createLimiter({
      ...toPolicy(vector.policy),
      store: stores[vector.fault]!,
      scope: 't',
      onStoreError: vector.on_store_error,
      storeTimeoutMs: 20,
    });
    const d = await limiter.check('a');
    expect({
      allowed: d.allowed,
      reason: d.reason,
      status: d.status,
      remaining: d.remaining,
      reset_ms: d.resetMs,
      retry_after_ms: d.retryAfterMs,
      headers: d.headers,
    }).toEqual(vector.expect);
  });
});

describe('contract vectors', () => {
  const bySubject = (subject: string) => vectorsOf('contract').find((v) => v.subject === subject);

  it('validates policies', () => {
    for (const c of bySubject('policy').cases) {
      const run = () => resolvePolicy(toPolicy(c.policy));
      if (c.valid) expect(run, JSON.stringify(c)).not.toThrow();
      else expect(run, JSON.stringify(c)).toThrow(RangeError);
    }
  });

  it('validates scopes', () => {
    for (const c of bySubject('scope').cases) {
      const run = () => assertScope(c.scope);
      if (c.valid) expect(run, c.scope).not.toThrow();
      else expect(run, c.scope).toThrow(RangeError);
    }
  });

  it('gives approximate stores the reduced decision', async () => {
    const vector = bySubject('approximate');
    for (const c of vector.cases) {
      const store = cloudflareBindingStore({
        binding: { limit: async () => ({ success: c.store_allows }) },
        periodSec: vector.period_sec,
      });
      const limiter = createLimiter({ ...toPolicy(vector.policy), store, scope: 't', onStoreError: 'closed' });
      const d = await limiter.check('a');
      expect({
        allowed: d.allowed,
        reason: d.reason,
        status: d.status,
        remaining: d.remaining,
        reset_ms: d.resetMs,
        retry_after_ms: d.retryAfterMs,
        headers: d.headers,
        tier: d.tier,
      }).toEqual(c.expect);
    }
  });
});

describe('canonicalIp', () => {
  it('rejects malformed IPv6', () => {
    for (const bad of ['1::2::3', ':::', '12345::', '1:2:3:4:5:6:7', '1:2:3:4:5:6:7:8:9', '::1.2.3', '1:2:3:4:5:6:7::8', 'g::1']) {
      expect(canonicalIp(bad), bad).toBeNull();
    }
    expect(canonicalIp(null)).toBeNull();
    expect(canonicalIp('')).toBeNull();
  });
});
