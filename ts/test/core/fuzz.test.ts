// The port against the independent model on vectors nobody picked: `pnpm fuzz` draws them from
// a new seed. Without OMNILIMIT_FUZZ_VECTORS there is nothing to run, and the suite says so.
import { describe, expect, it } from 'vitest';
import { parsePolicy, resolvePolicy } from '../../src/core/gcra.js';
import { deriveKey } from '../../src/core/key.js';
import { maskIp, resolveClientIp } from '../../src/ip/index.js';
import { memoryStore } from '../../src/stores/memory.js';
import { fuzzOf, fuzzSeed, fuzzVectors, withSeed } from '../support/fuzz.js';
import { runOrder } from '../support/schedule.js';
import { runRecipe, runSequence, toPolicy, type Vector } from '../support/vectors.js';

const named = (vectors: Vector[]): [string, Vector][] => vectors.map((vector) => [vector.id, vector]);
const subject = (name: string): Vector[] => fuzzOf('contract').filter((vector) => vector.subject === name);

describe.skipIf(fuzzSeed === null)(`fuzz vectors, seed ${fuzzSeed}`, () => {
  it('has every kind', () => {
    expect([...new Set(fuzzVectors.map((vector) => vector.kind))].sort()).toEqual(['contract', 'interleaving', 'ip', 'key', 'recipe', 'sequence']);
  });

  it.each(named(fuzzOf('sequence')))('%s', async (_id, vector) => {
    await runSequence(vector, memoryStore());
  });

  it.each(named(fuzzOf('recipe')))('%s', async (_id, vector) => {
    await runRecipe(vector, memoryStore());
  });

  // Not random: every order of the store calls of a few concurrent callers, each with the
  // result the model got when it ran in that order.
  it.each(named(fuzzOf('interleaving')))('%s, in every order', async (_id, vector) => {
    expect(vector.runs.length).toBeGreaterThan(10);
    for (const [picks, allowed, verifyRan, levels] of vector.runs) {
      expect(await runOrder(vector, picks), `order ${JSON.stringify(picks)}`).toEqual({ allowed, verifyRan, levels });
    }
  });

  it('resolves client IPs', () => {
    for (const vector of fuzzOf('ip')) {
      for (const c of vector.cases) {
        const strategy = c.strategy.preset ? { preset: c.strategy.preset } : { xff: c.strategy.xff };
        const context = withSeed(JSON.stringify(c));
        expect(resolveClientIp({ headers: c.headers, peer: c.peer }, strategy), context).toBe(c.expect);
      }
      for (const m of vector.masks) expect(maskIp(m.ip, m.prefix), withSeed(JSON.stringify(m))).toBe(m.expect);
    }
  });

  it('derives keys', async () => {
    for (const vector of fuzzOf('key')) {
      for (const c of vector.cases) {
        const key = await deriveKey(c.scope, resolvePolicy(toPolicy(c.policy)), c.salt, c.id);
        expect(key, withSeed(JSON.stringify(c))).toBe(c.expect);
      }
    }
  });

  it('validates policies', () => {
    for (const vector of subject('policy')) {
      for (const c of vector.cases) {
        const run = () => resolvePolicy(toPolicy(c.policy));
        if (c.valid) expect(run, withSeed(JSON.stringify(c))).not.toThrow();
        else expect(run, withSeed(JSON.stringify(c))).toThrow(RangeError);
      }
    }
  });

  it('reads the policy shorthand', () => {
    for (const vector of subject('shorthand')) {
      for (const c of vector.cases) {
        const context = withSeed(JSON.stringify(c.text));
        if (c.policy) expect(parsePolicy(c.text), context).toEqual(toPolicy(c.policy));
        else expect(() => parsePolicy(c.text), context).toThrow(RangeError);
      }
    }
  });
});
