import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect } from 'vitest';
import { resolvePolicy } from '../../src/core/gcra.js';
import { createLimiter } from '../../src/core/limiter.js';
import { buildHeaders } from '../../src/core/headers.js';
import { deriveKey } from '../../src/core/key.js';
import {
  checkAll,
  createLockout,
  peekAll,
  reserveAll,
  SettledError,
  type CombinedDecision,
  type Lockout,
  type LockoutGate,
  type Pending,
} from '../../src/core/recipes.js';
import { cloudflareBindingStore } from '../../src/stores/cloudflare.js';
import type { Decision, HitParams, Limiter, RawHit, RawRefund, Reservation, Store } from '../../src/core/types.js';

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
  let lastRaw: RawHit | RawRefund | undefined;
  let refundCalls = 0;
  const recording: Store = {
    tier: store.tier,
    async hit(key: string, params: HitParams, nowMs?: number) {
      lastRaw = await store.hit(key, params, nowMs);
      return lastRaw;
    },
    async peek(key: string, params: HitParams, nowMs?: number) {
      lastRaw = await store.peek!(key, params, nowMs);
      return lastRaw;
    },
    reset: (key: string) => store.reset!(key),
    async refund(key: string, params: HitParams, sinceMs: number, nowMs?: number) {
      refundCalls += 1;
      lastRaw = await store.refund!(key, params, sinceMs, nowMs);
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
  // Hits that a later step refunds are reservations; the rest stay plain checks.
  const refunded = new Set<number>(vector.steps.filter((step: Vector) => step.op === 'refund').map((step: Vector) => step.of));
  const reservations = new Map<number, Reservation | null>();
  for (const [index, step] of vector.steps.entries()) {
    const context = `${vector.id} step ${index}`;
    if (step.op === 'reset') {
      expect(await limiter.reset(step.id), context).toBe(true);
      continue;
    }
    if (step.op === 'refund') {
      const calls = refundCalls;
      const done = await limiter.refund(reservations.get(step.of)!, { nowMs: step.at_ms });
      const raw = lastRaw as RawRefund;
      const got = done ? { refunded: done, state: { level: raw.level, ts: raw.ts } } : { refunded: done };
      expect(got, context).toEqual(step.expect);
      // A spent reservation does not reach the store again.
      expect(refundCalls - calls, context).toBe(done ? 1 : 0);
      continue;
    }
    const options = { nowMs: step.at_ms, ...(step.cost ? { cost: step.cost } : {}) };
    let decision;
    if (step.op === 'peek') decision = await limiter.peek(step.id, options);
    else if (refunded.has(index)) {
      const reserved = await limiter.reserve(step.id, options);
      expect(reserved.reservation === null, context).toBe(!step.expect.allowed);
      reservations.set(index, reserved.reservation);
      decision = reserved.decision;
    } else decision = await limiter.check(step.id, options);
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

const shortDecision = (d: Decision) => ({
  allowed: d.allowed,
  reason: d.reason,
  status: d.status,
  remaining: d.remaining,
  reset_ms: d.resetMs,
  retry_after_ms: d.retryAfterMs,
});

function combined(result: CombinedDecision) {
  // Rule 3 asks for one of the reported decisions, so this is identity and not equality.
  const governing = result.decisions.indexOf(result.decision);
  expect(governing, 'the governing decision is one of the decisions').toBeGreaterThanOrEqual(0);
  return { allowed: result.allowed, governing, decisions: result.decisions.map(shortDecision) };
}

/** The error kinds of recipe vectors. Anything else is a failure. */
function errorKind(thrown: unknown, fromVerify: unknown): string {
  if (thrown === fromVerify) return 'verify';
  if (thrown instanceof SettledError) return 'state';
  if (thrown instanceof RangeError) return 'value';
  if (thrown instanceof TypeError) return 'type';
  throw thrown;
}

type Call = { op: 'peek' | 'hit' | 'reset' | 'refund'; limiter: string; id: string; failed?: true };

/**
 * Run one recipe vector. Each limiter's store is the one under test seen through a wrapper that
 * passes the vector's clock as `nowMs`, fails or hangs while a fault is armed, and logs every
 * call by limiter and id. Limiters of tier `approximate` get a binding store that always allows.
 */
export async function runRecipe(vector: Vector, store: Store, saltSuffix = ''): Promise<void> {
  const salt = `${vector.salt}${saltSuffix}`;
  const timeline: Vector[] = vector.timeline;
  const ids = new Set<string>(
    timeline.flatMap((step) => [...(step.entries ?? []).map((e: Vector) => e.id), ...(step.ids ?? []), ...(step.id === undefined ? [] : [step.id])]),
  );
  const storeTimeoutMs = timeline.some((step) => step.fault === 'timeout') ? 250 : 10_000;
  let now = 0;
  const calls: Call[] = [];
  const faults = new Map<string, { fault: 'error' | 'timeout'; on: string[] }>();
  const limiters: Record<string, Limiter> = {};
  const raw: Record<string, { store: Store; params: HitParams; key: (id: string) => Promise<string> }> = {};

  for (const [name, config] of Object.entries<Vector>(vector.limiters)) {
    const policy = resolvePolicy(toPolicy(config.policy));
    const inner =
      config.tier === 'approximate'
        ? cloudflareBindingStore({ binding: { limit: async () => ({ success: true }) }, periodSec: 60 })
        : store;
    const key = (id: string) => deriveKey(config.scope, policy, salt, id);
    const idOf = new Map<string, string>();
    for (const id of ids) idOf.set(await key(id), id);
    const call = <T>(op: Call['op'], k: string, run: () => Promise<T>): Promise<T> => {
      const fault = faults.get(name);
      const failed = fault?.on.includes(op) ?? false;
      calls.push({ op, limiter: name, id: idOf.get(k) ?? `unknown key ${k}`, ...(failed ? { failed: true as const } : {}) });
      if (!failed) return run();
      return fault!.fault === 'timeout' ? new Promise<T>(() => {}) : Promise.reject(new Error('omnilimit test: injected fault'));
    };
    const wrapped: Store = {
      tier: inner.tier,
      hit: (k, params) => call('hit', k, () => inner.hit(k, params, now)),
      ...(inner.peek ? { peek: (k: string, params: HitParams) => call('peek', k, () => inner.peek!(k, params, now)) } : {}),
      ...(inner.reset ? { reset: (k: string) => call('reset', k, () => inner.reset!(k)) } : {}),
      ...(inner.refund
        ? { refund: (k: string, params: HitParams, sinceMs: number) => call('refund', k, () => inner.refund!(k, params, sinceMs, now)) }
        : {}),
    };
    limiters[name] = createLimiter({
      ...toPolicy(config.policy),
      store: wrapped,
      scope: config.scope,
      salt,
      onStoreError: config.on_store_error,
      storeTimeoutMs,
    });
    raw[name] = { store: inner, params: policy, key };
  }

  // The earlier form takes the id as subject; the other one takes the step's ids, one per limit.
  const lockouts: Record<string, { lockout: Lockout<string[]>; earlier: boolean }> = {};
  for (const [name, given] of Object.entries<Vector>(vector.lockouts ?? {})) {
    if (Array.isArray(given)) {
      expect(given, `${vector.id}: the earlier form takes one limit`).toHaveLength(1);
      const [limit] = given as [Vector];
      const single = createLockout({ limiter: limiters[limit.limiter]!, resetOnSuccess: limit.reset_on_success ?? true });
      lockouts[name] = {
        earlier: true,
        lockout: { begin: (ids) => single.begin(ids[0]!), attempt: (ids, verify) => single.attempt(ids[0]!, verify) },
      };
    } else {
      const lockout = createLockout<string[]>({
        limits: given.limits.map((limit: Vector, index: number) => ({
          limiter: limiters[limit.limiter]!,
          idOf: (ids: string[]) => ids[index]!,
          ...(limit.reset_on_success === undefined ? {} : { resetOnSuccess: limit.reset_on_success }),
        })),
        ...(given.mode === undefined ? {} : { mode: given.mode }),
      });
      lockouts[name] = { earlier: false, lockout };
    }
  }

  const withCost = (cost: number | undefined) => (cost === undefined ? {} : { cost });
  const entriesOf = (step: Vector) => step.entries.map((e: Vector) => ({ limiter: limiters[e.limiter]!, id: e.id, ...withCost(e.cost) }));
  const handles = new Map<string, Pending>();
  const gates = new Map<string, LockoutGate>();
  const settledOf = (handle: string) => (handles.get(handle) ?? gates.get(handle))!;
  for (const [index, step] of timeline.entries()) {
    const context = `${vector.id} step ${index} (${step.op})`;
    if (step.at_ms !== undefined) now = step.at_ms;
    if (step.op === 'fault') {
      faults.set(step.limiter, { fault: step.fault, on: step.on });
      continue;
    }
    if (step.op === 'heal') {
      faults.delete(step.limiter);
      continue;
    }
    if (step.op === 'inspect') {
      const { store: inner, params, key } = raw[step.limiter]!;
      const hit = await inner.peek!(await key(step.id), params, now);
      expect({ state: { level: hit.level, ts: hit.ts } }, context).toEqual(step.expect);
      continue;
    }
    calls.length = 0;
    const fromVerify = new Error('omnilimit test: verify raised');
    let verifyRan = false;
    const got: Record<string, unknown> = {};
    try {
      switch (step.op) {
        case 'check_all':
          got['result'] = combined(await checkAll(entriesOf(step)));
          break;
        case 'peek_all':
        case 'reserve_all': {
          const opened = await (step.op === 'peek_all' ? peekAll : reserveAll)(entriesOf(step));
          expect(opened.mode, context).toBe(step.op === 'peek_all' ? 'peek' : 'reserve');
          expect(opened.settled, context).toBe(false);
          handles.set(step.handle, opened);
          got['result'] = { ...combined(opened), refunded: opened.refunded };
          break;
        }
        case 'commit':
          got['result'] = combined(await handles.get(step.handle)!.commit());
          expect(handles.get(step.handle)!.settled, context).toBe(true);
          break;
        case 'cancel':
          got['result'] = await settledOf(step.handle).cancel();
          expect(settledOf(step.handle).settled, context).toBe(true);
          break;
        case 'gate': {
          const opened = await lockouts[step.lockout]!.lockout.begin(step.ids);
          expect(opened.settled, context).toBe(false);
          gates.set(step.handle, opened);
          got['result'] = { ...combined(opened), refunded: opened.refunded };
          break;
        }
        case 'fail':
          got['result'] = combined(await gates.get(step.handle)!.fail());
          expect(gates.get(step.handle)!.settled, context).toBe(true);
          break;
        case 'succeed': {
          const done = await gates.get(step.handle)!.succeed();
          got['result'] = { ...combined(done), resets: done.resets, refunded: done.refunded };
          expect(gates.get(step.handle)!.settled, context).toBe(true);
          break;
        }
        case 'attempt': {
          const { lockout, earlier } = lockouts[step.lockout]!;
          const attempt = await lockout.attempt(step.ids, () => {
            verifyRan = true;
            if (step.verify === 'raise') throw fromVerify;
            return step.verify;
          });
          const { governing, decisions } = combined(attempt);
          got['result'] = { allowed: attempt.allowed, verified: attempt.verified, verify_ran: verifyRan, governing, decisions };
          if (attempt.allowed && attempt.verified) {
            expect(attempt.resets, context).toBeDefined();
            if (!earlier) got['result'] = { ...(got['result'] as object), resets: attempt.resets };
          }
          break;
        }
        case 'check':
          got['result'] = shortDecision(await limiters[step.limiter]!.check(step.id, withCost(step.cost)));
          break;
        case 'peek':
          got['result'] = shortDecision(await limiters[step.limiter]!.peek(step.id, withCost(step.cost)));
          break;
        case 'reset':
          got['result'] = await limiters[step.limiter]!.reset(step.id);
          break;
        default:
          throw new Error(`${context}: unknown op`);
      }
    } catch (thrown) {
      got['error'] = errorKind(thrown, fromVerify);
    }
    expect({ calls: [...calls], ...got }, context).toEqual(step.expect);
  }
}
