// The recipe form of `concurrency` vectors, without vitest: it also runs inside workerd, against
// the built package, so the functions it needs are passed in.
import type { LimitEntry, checkAll, createLockout, reserveAll } from '../../src/core/recipes.js';
import type { createLimiter } from '../../src/core/limiter.js';
import type { Decision, HitParams, Store } from '../../src/core/types.js';

export interface RecipeApi {
  createLimiter: typeof createLimiter;
  checkAll: typeof checkAll;
  reserveAll: typeof reserveAll;
  createLockout: typeof createLockout;
}

/** What a run reports, compared with the vector by `expectConcurrency`. */
export interface ConcurrencySummary {
  allowed: number;
  verifyRan: number;
  /** Units of the allowed hits under each limiter: what was counted, before any refund. */
  counted: Record<string, number>;
  /** Each limiter's level at the vector's time, once everything settled. */
  levels: Record<string, number>;
  /** Decisions that carry a store failure, and refunds that failed. Must be none. */
  errors: number;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Vector = any;

/**
 * Run `requests` copies of the vector's recipe at once. Every limiter sees the store through a
 * wrapper that passes the vector's time as `nowMs` and remembers its key, so its level can be
 * read afterwards. Gates and pendings open all at once, and then all settle at once.
 */
export async function runRecipeConcurrency(api: RecipeApi, vector: Vector, store: Store, saltSuffix = ''): Promise<ConcurrencySummary> {
  const now: number = vector.at_ms;
  const seen: Record<string, { key: string; params: HitParams }> = {};
  const counted: Record<string, number> = {};
  const limiters: Record<string, ReturnType<typeof createLimiter>> = {};
  for (const [name, config] of Object.entries<Vector>(vector.limiters)) {
    counted[name] = 0;
    const window: number = config.policy.window_ms;
    const wrapped: Store = {
      tier: store.tier,
      async hit(key, params) {
        seen[name] = { key, params };
        const hit = await store.hit(key, params, now);
        // Every allowed hit is counted here, whatever recipe step made it.
        if (hit.allowed) counted[name]! += params.windowMs / window;
        return hit;
      },
      peek: (key, params) => ((seen[name] = { key, params }), store.peek!(key, params, now)),
      reset: (key) => store.reset!(key),
      refund: (key, params, sinceMs) => store.refund!(key, params, sinceMs, now),
    };
    limiters[name] = api.createLimiter({
      limit: config.policy.limit,
      windowMs: window,
      burst: config.policy.burst,
      store: wrapped,
      scope: config.scope,
      salt: `${vector.salt}${saltSuffix}`,
      onStoreError: config.on_store_error,
      storeTimeoutMs: 30_000,
    });
  }

  const recipe = vector.recipe;
  const each = <T>(run: () => Promise<T>) => Promise.all(Array.from({ length: vector.requests as number }, run));
  let errors = 0;
  let verifyRan = 0;
  // A closed limiter answers a store failure with a denial: count them, so none goes unnoticed.
  const failures = <T extends { decisions: Decision[] }>(result: T): T => {
    errors += result.decisions.filter((decision) => decision.reason === 'store_error').length;
    return result;
  };
  const allowedOf = (results: { allowed: boolean }[]) => results.filter((result) => result.allowed).length;

  let allowed: number;
  if (recipe.op === 'gate' || recipe.op === 'attempt') {
    const lockout = api.createLockout<string[]>({
      limits: recipe.lockout.limits.map((limit: Vector, index: number) => ({
        limiter: limiters[limit.limiter]!,
        idOf: (ids: string[]) => ids[index]!,
      })),
      ...(recipe.lockout.mode === undefined ? {} : { mode: recipe.lockout.mode }),
    });
    if (recipe.op === 'attempt') {
      const verify = async () => {
        verifyRan += 1;
        // Yield, so that other attempts run while this one verifies.
        await new Promise((resolve) => setTimeout(resolve, 1));
        return recipe.verify as boolean;
      };
      allowed = allowedOf((await each(() => lockout.attempt(recipe.ids, verify))).map(failures));
    } else {
      const gates = (await each(() => lockout.begin(recipe.ids))).map(failures);
      allowed = allowedOf(gates);
      (await Promise.all(gates.map((gate) => gate.fail()))).forEach(failures);
    }
  } else {
    const entries = (): LimitEntry[] =>
      recipe.entries.map((entry: Vector) => ({ limiter: limiters[entry.limiter]!, id: entry.id, ...(entry.cost ? { cost: entry.cost } : {}) }));
    if (recipe.op === 'check_all') {
      allowed = allowedOf((await each(() => api.checkAll(entries()))).map(failures));
    } else {
      const pendings = (await each(() => api.reserveAll(entries()))).map(failures);
      allowed = allowedOf(pendings);
      // The first `commit` allowed ones, in request order, commit; every other one cancels.
      const committing = new Set(pendings.filter((pending) => pending.allowed).slice(0, recipe.commit as number));
      await Promise.all(
        pendings.map(async (pending) => {
          if (committing.has(pending)) failures(await pending.commit());
          else errors += (await pending.cancel()).refunded.filter((done) => done === false).length;
        }),
      );
    }
  }

  const levels: Record<string, number> = {};
  for (const name of Object.keys(vector.limiters)) {
    const { key, params } = seen[name]!;
    levels[name] = (await store.peek!(key, params, now)).level!;
  }
  return { allowed, verifyRan, counted, levels, errors };
}
