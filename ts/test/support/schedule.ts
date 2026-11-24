// Drive a recipe through one chosen interleaving, store call by store call.
//
// `tools/explore_interleavings.py` lists every order the store calls of a few concurrent callers
// can take, with the result the independent model got in each. Here the port is put through the
// same order: every store call waits at a gate, and the schedule says which caller goes next.
import { expect } from 'vitest';
import { createLimiter } from '../../src/core/limiter.js';
import { checkAll, createLockout, reserveAll, type LimitEntry } from '../../src/core/recipes.js';
import { memoryStore } from '../../src/stores/memory.js';
import type { HitParams, Store } from '../../src/core/types.js';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Vector = any;

interface Task {
  run: () => Promise<void>;
  started: boolean;
  done: boolean;
  /** Lets the store call this caller is waiting on go ahead. */
  release: (() => void) | null;
  finished: Promise<void> | null;
  atGate: (() => void) | null;
}

/** Callers that take turns: only the one the schedule picked moves, and only by one store call. */
class Turns {
  private current: Task | null = null;
  readonly violations: string[] = [];

  /** Every store call waits here until its caller is picked again. */
  gate(op: string): Promise<void> {
    const task = this.current;
    if (!task) {
      this.violations.push(`${op}: a store call the model does not make`);
      return Promise.resolve();
    }
    if (task.release) this.violations.push(`${op}: two store calls at once from one caller`);
    return new Promise<void>((resolve) => {
      task.release = resolve;
      task.atGate?.();
    });
  }

  /**
   * The model's `run`: a pick is an index into the callers still running. The first step of a
   * caller takes it to its first store call, and each later one lets one call through.
   */
  async run(tasks: Task[], next: () => number): Promise<void> {
    const live = [...tasks];
    while (live.length > 0) {
      const pick = next();
      const task = live[pick]!;
      this.current = task;
      const atGate = new Promise<void>((resolve) => (task.atGate = resolve));
      if (!task.started) {
        task.started = true;
        task.finished = task.run().then(() => {
          task.done = true;
        });
      } else {
        const release = task.release!;
        task.release = null;
        release();
      }
      await Promise.race([task.finished!, atGate]);
      this.current = null;
      if (task.done) live.splice(pick, 1);
    }
  }
}

const task = (run: () => Promise<void>): Task => ({ run, started: false, done: false, release: null, finished: null, atGate: null });

export interface OrderResult {
  allowed: number;
  verifyRan: number;
  /** The level of each limiter, in the order the vector declares them. */
  levels: number[];
}

/** Run the vector's recipe in the order `picks` gives, and report what the model reports. */
export async function runOrder(vector: Vector, picks: number[]): Promise<OrderResult> {
  const now: number = vector.at_ms;
  const inner = memoryStore();
  const turns = new Turns();
  const seen: Record<string, { key: string; params: HitParams }> = {};
  const limiters: Record<string, ReturnType<typeof createLimiter>> = {};
  for (const [name, config] of Object.entries<Vector>(vector.limiters)) {
    const gated: Store = {
      tier: 'exact',
      async hit(key, params) {
        seen[name] = { key, params };
        await turns.gate(`hit ${name}`);
        return inner.hit(key, params, now);
      },
      async peek(key, params) {
        seen[name] = { key, params };
        await turns.gate(`peek ${name}`);
        return inner.peek!(key, params, now);
      },
      async reset(key) {
        await turns.gate(`reset ${name}`);
        return inner.reset!(key);
      },
      async refund(key, params, sinceMs) {
        await turns.gate(`refund ${name}`);
        return inner.refund!(key, params, sinceMs, now);
      },
    };
    limiters[name] = createLimiter({
      limit: config.policy.limit,
      windowMs: config.policy.window_ms,
      burst: config.policy.burst,
      store: gated,
      scope: config.scope,
      salt: vector.salt,
      onStoreError: config.on_store_error,
      storeTimeoutMs: 60_000,
    });
  }

  let at = 0;
  const next = () => {
    expect(at, 'the port made more store calls than the model').toBeLessThan(picks.length);
    return picks[at++]!;
  };
  const recipe = vector.recipe;
  const callers = Array.from({ length: vector.requests as number }, (_, index) => index);
  let allowed = 0;
  let verifyRan = 0;

  if (recipe.op === 'gate' || recipe.op === 'attempt') {
    const mode: string = recipe.lockout.mode ?? 'advisory';
    const lockout = createLockout<string[]>({
      limits: recipe.lockout.limits.map((limit: Vector, index: number) => ({
        limiter: limiters[limit.limiter]!,
        idOf: (ids: string[]) => ids[index]!,
      })),
      ...(recipe.lockout.mode === undefined ? {} : { mode: recipe.lockout.mode }),
    });
    if (recipe.op === 'attempt') {
      const verify = async () => {
        verifyRan += 1;
        return recipe.verify as boolean;
      };
      await turns.run(
        callers.map(() =>
          task(async () => {
            if ((await lockout.attempt(recipe.ids, verify)).allowed) allowed += 1;
          }),
        ),
        next,
      );
    } else {
      const gates: Awaited<ReturnType<typeof lockout.begin>>[] = [];
      await turns.run(
        callers.map((index) =>
          task(async () => {
            gates[index] = await lockout.begin(recipe.ids);
          }),
        ),
        next,
      );
      allowed = gates.filter((gate) => gate.allowed).length;
      // Only an advisory failure counts anything, and only for a gate that was allowed.
      const counting = gates.filter((gate) => mode === 'advisory' && gate.allowed);
      await turns.run(
        counting.map((gate) =>
          task(async () => {
            await gate.fail();
          }),
        ),
        next,
      );
      for (const gate of gates) if (!counting.includes(gate)) await gate.fail();
    }
  } else {
    const entries = (): LimitEntry[] =>
      recipe.entries.map((entry: Vector) => ({ limiter: limiters[entry.limiter]!, id: entry.id, ...(entry.cost ? { cost: entry.cost } : {}) }));
    if (recipe.op === 'check_all') {
      await turns.run(
        callers.map(() =>
          task(async () => {
            if ((await checkAll(entries())).allowed) allowed += 1;
          }),
        ),
        next,
      );
    } else {
      const pendings: Awaited<ReturnType<typeof reserveAll>>[] = [];
      await turns.run(
        callers.map((index) =>
          task(async () => {
            pendings[index] = await reserveAll(entries());
          }),
        ),
        next,
      );
      const open = pendings.filter((pending) => pending.allowed);
      allowed = open.length;
      // The first `commit` allowed ones commit, which calls nothing; every other one cancels.
      const cancelling = open.slice(recipe.commit as number);
      await turns.run(
        cancelling.map((pending) =>
          task(async () => {
            await pending.cancel();
          }),
        ),
        next,
      );
      for (const pending of pendings) {
        if (cancelling.includes(pending)) continue;
        if (pending.allowed) await pending.commit();
        else await pending.cancel();
      }
    }
  }

  expect(at, 'the port made fewer store calls than the model').toBe(picks.length);
  expect(turns.violations).toEqual([]);
  const levels: number[] = [];
  for (const name of Object.keys(vector.limiters)) {
    const used = seen[name];
    levels.push(used ? (await inner.peek!(used.key, used.params, now)).level! : 0);
  }
  return { allowed, verifyRan, levels };
}
