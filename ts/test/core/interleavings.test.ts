// Interleavings that a vector cannot force: a store that pauses one call until the test lets it
// go, so another request runs in the gap. See "What recipes do not promise" in the spec.
import { describe, expect, it } from 'vitest';
import { createLimiter } from '../../src/core/limiter.js';
import { checkAll, createLockout, peekAll, reserveAll, SettledError } from '../../src/core/recipes.js';
import { memoryStore } from '../../src/stores/memory.js';
import type { HitParams, Limiter, Store } from '../../src/core/types.js';

type Op = 'hit' | 'peek' | 'reset' | 'refund';

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => (resolve = r));
  return { promise, resolve };
}

/**
 * A memory store on a fixed clock, so a refund gives back all it took, that logs every call and
 * can hold one: `pauseAfter(op, scope)` lets the next such call reach the store, then holds its
 * answer until `release()`. `reached` resolves once it is held.
 */
function barrierStore() {
  const inner = memoryStore({ now: () => 1_700_000_000_000 });
  const calls: string[] = [];
  let trap: { op: Op; scope: string; reached: () => void; release: Promise<void> } | null = null;
  const after = async (op: Op, key: string) => {
    calls.push(`${op} ${key.split(':')[2]}`);
    if (trap && trap.op === op && key.split(':')[2] === trap.scope) {
      const { reached, release } = trap;
      trap = null;
      reached();
      await release;
    }
  };
  const wrap =
    <A extends unknown[], R>(op: Op, run: (key: string, ...rest: A) => Promise<R>) =>
    async (key: string, ...rest: A): Promise<R> => {
      const result = await run(key, ...rest);
      await after(op, key);
      return result;
    };
  const store: Store = {
    tier: 'exact',
    hit: wrap('hit', (key, params: HitParams) => inner.hit(key, params)),
    peek: wrap('peek', (key, params: HitParams) => inner.peek!(key, params)),
    reset: wrap('reset', (key) => inner.reset!(key)),
    refund: wrap('refund', (key, params: HitParams, sinceMs: number) => inner.refund!(key, params, sinceMs)),
  };
  return {
    store,
    calls,
    pauseAfter(op: Op, scope: string) {
      const reached = deferred();
      const release = deferred();
      trap = { op, scope, reached: reached.resolve, release: release.promise };
      return { reached: reached.promise, release: release.resolve };
    },
  };
}

const limiterOn = (store: Store, scope: string, burst: number): Limiter =>
  createLimiter({ limit: 1, windowMs: 60_000, burst, store, scope, onStoreError: 'closed' });

/** Units held by a bucket: a peek's `remaining` counts what is left. */
const used = async (limiter: Limiter, id: string, burst: number) => burst - (await limiter.peek(id)).remaining!;

describe('peek mode under an interleaving', () => {
  it('commit counts the first limit, then is denied by the second, which another request took', async () => {
    const { store, pauseAfter } = barrierStore();
    const a = limiterOn(store, 'a', 3);
    const b = limiterOn(store, 'b', 1);
    const pending = await peekAll([
      { limiter: a, id: 'x' },
      { limiter: b, id: 'y' },
    ]);
    expect(pending.allowed).toBe(true);
    const paused = pauseAfter('hit', 'a');
    const committing = pending.commit();
    await paused.reached;
    // In the gap, another request takes b's last unit.
    const other = await checkAll([{ limiter: b, id: 'y' }]);
    expect(other.allowed).toBe(true);
    paused.release();
    const result = await committing;
    expect(result.allowed).toBe(false);
    expect(result.decisions.map((d) => d.allowed)).toEqual([true, false]);
    expect(result.decision).toBe(result.decisions[1]);
    // The limit counted before the denial stays counted.
    expect(await used(a, 'x', 3)).toBe(1);
    expect(await used(b, 'y', 1)).toBe(1);
  });

  it('an advisory lockout runs the verification more often than its burst', async () => {
    // Every attempt passes its peek before any failure is counted: no hit goes through until
    // all the peeks have.
    const inner = memoryStore({ now: () => 1_700_000_000_000 });
    const attempts = 5;
    let peeks = 0;
    const allPeeked = deferred();
    const store: Store = {
      tier: 'exact',
      async peek(key, params) {
        const result = await inner.peek!(key, params);
        if (++peeks === attempts) allPeeked.resolve();
        return result;
      },
      async hit(key, params) {
        await allPeeked.promise;
        return inner.hit(key, params);
      },
    };
    const limiter = limiterOn(store, 'login', 2);
    let verifications = 0;
    const lockout = createLockout({ limits: [{ limiter }] });
    const results = await Promise.all(
      Array.from({ length: attempts }, () =>
        lockout.attempt('u', async () => {
          verifications += 1;
          return false;
        }),
      ),
    );
    expect(verifications).toBe(attempts);
    expect(verifications).toBeGreaterThan(2);
    // The failures were still counted one by one: only the burst of them was allowed.
    expect(results.filter((r) => r.decision.allowed)).toHaveLength(2);
  });
});

describe('reserve mode under an interleaving', () => {
  it('compensation over-counts the first limit until its refund lands, then restores it', async () => {
    const { store, calls, pauseAfter } = barrierStore();
    const a = limiterOn(store, 'a', 1);
    const b = limiterOn(store, 'b', 1);
    expect((await b.check('y')).allowed).toBe(true);
    calls.length = 0;
    // The denial on b is held before its compensation refunds a.
    const paused = pauseAfter('hit', 'b');
    const opening = reserveAll([
      { limiter: a, id: 'x' },
      { limiter: b, id: 'y' },
    ]);
    await paused.reached;
    expect(await used(a, 'x', 1)).toBe(1);
    // A request in the gap is denied by the reservation about to be given back: only towards denying.
    expect((await a.check('x')).allowed).toBe(false);
    paused.release();
    const pending = await opening;
    expect(pending.allowed).toBe(false);
    expect(pending.refunded).toEqual([true, null]);
    expect(await used(a, 'x', 1)).toBe(0);
    expect(calls).toEqual(['hit a', 'hit b', 'peek a', 'hit a', 'refund a', 'peek a']);
    expect((await a.check('x')).allowed).toBe(true);
  });
});

describe('settlement under concurrency', () => {
  it('two refunds of one reservation at once reach the store once', async () => {
    const { store, calls, pauseAfter } = barrierStore();
    const a = limiterOn(store, 'a', 3);
    const { reservation } = await a.reserve('x');
    await a.reserve('x');
    calls.length = 0;
    const paused = pauseAfter('refund', 'a');
    const first = a.refund(reservation);
    await paused.reached;
    const second = a.refund(reservation);
    paused.release();
    expect(await Promise.all([first, second])).toEqual([true, false]);
    expect(calls).toEqual(['refund a']);
    expect(await used(a, 'x', 3)).toBe(1);
  });

  it('two commits of a peek pending make one set of store calls and share the result', async () => {
    const { store, calls, pauseAfter } = barrierStore();
    const a = limiterOn(store, 'a', 3);
    const b = limiterOn(store, 'b', 3);
    const pending = await peekAll([
      { limiter: a, id: 'x' },
      { limiter: b, id: 'y' },
    ]);
    calls.length = 0;
    const paused = pauseAfter('hit', 'a');
    const first = pending.commit();
    await paused.reached;
    const second = pending.commit();
    paused.release();
    const [one, two] = await Promise.all([first, second]);
    expect(one).toBe(two);
    expect(calls).toEqual(['hit a', 'hit b']);
    expect(await used(a, 'x', 3)).toBe(1);
  });

  it('two cancels of a reserve pending refund once', async () => {
    const { store, calls, pauseAfter } = barrierStore();
    const a = limiterOn(store, 'a', 3);
    const b = limiterOn(store, 'b', 3);
    const pending = await reserveAll([
      { limiter: a, id: 'x' },
      { limiter: b, id: 'y' },
    ]);
    calls.length = 0;
    const paused = pauseAfter('refund', 'a');
    const first = pending.cancel();
    await paused.reached;
    const second = pending.cancel();
    paused.release();
    const [one, two] = await Promise.all([first, second]);
    expect(one).toBe(two);
    expect(one.refunded).toEqual([true, true]);
    expect(calls).toEqual(['refund a', 'refund b']);
    expect(await used(a, 'x', 3)).toBe(0);
  });

  it('two failures of an advisory gate count once', async () => {
    const { store, calls, pauseAfter } = barrierStore();
    const a = limiterOn(store, 'a', 3);
    const gate = await createLockout({ limits: [{ limiter: a }] }).begin('u');
    calls.length = 0;
    const paused = pauseAfter('hit', 'a');
    const both = Promise.all([gate.fail(), gate.fail()]);
    await paused.reached;
    paused.release();
    const [one, two] = await both;
    expect(one).toBe(two);
    expect(calls).toEqual(['hit a']);
    expect(await used(a, 'u', 3)).toBe(1);
  });

  it('crossed settlements: the first wins, the other is refused without a store call', async () => {
    const { store, calls, pauseAfter } = barrierStore();
    const a = limiterOn(store, 'a', 3);
    const pending = await peekAll([{ limiter: a, id: 'x' }]);
    calls.length = 0;
    const paused = pauseAfter('hit', 'a');
    const committing = pending.commit();
    await paused.reached;
    // Settled before its first store call: the cancel is refused while the commit is in flight.
    await expect(pending.cancel()).rejects.toBeInstanceOf(SettledError);
    paused.release();
    expect((await committing).allowed).toBe(true);
    expect(calls).toEqual(['hit a']);

    const gate = await createLockout({ limits: [{ limiter: a }], mode: 'reserve' }).begin('u');
    calls.length = 0;
    const refunding = pauseAfter('refund', 'a');
    const cancelling = gate.cancel();
    await refunding.reached;
    const [failed, succeeded] = await Promise.allSettled([gate.fail(), gate.succeed()]);
    expect(failed).toMatchObject({ status: 'rejected', reason: expect.any(SettledError) });
    expect(succeeded).toMatchObject({ status: 'rejected', reason: expect.any(SettledError) });
    refunding.release();
    expect((await cancelling).refunded).toEqual([true]);
    expect(calls).toEqual(['refund a']);
  });
});
