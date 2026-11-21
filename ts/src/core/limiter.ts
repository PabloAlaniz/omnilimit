import { assertCost, assertScope, resolvePolicy, viewOf } from './gcra.js';
import { buildHeaders } from './headers.js';
import { deriveKey } from './key.js';
import type {
  Decision,
  HeaderInput,
  HitParams,
  Limiter,
  LimiterEvent,
  LimiterOptions,
  RawHit,
  Reservation,
  Store,
} from './types.js';

const DEFAULT_STORE_TIMEOUT_MS = 1000;
const STORE_ERROR_RETRY_MS = 1000;
// A registered symbol, so that a limiter from one bundle is understood by recipes from another.
const ASSERT_COST = Symbol.for('omnilimit.assertCost');
const ASSERT_REFUND = Symbol.for('omnilimit.assertRefund');

/**
 * Validate a cost for this limiter without calling its store, as recipes must before their first
 * store call. A limiter not made by `createLimiter` is left to validate it when called.
 */
export function assertLimiterCost(limiter: Limiter, cost: number | undefined): void {
  (limiter as { [ASSERT_COST]?: (cost: number | undefined) => void })[ASSERT_COST]?.(cost);
}

/**
 * Throw the `TypeError` that `reserve` would if this limiter's store cannot refund, without calling
 * it, as recipes must before their first store call. A limiter not made by `createLimiter` is
 * judged by its tier alone.
 */
export function assertLimiterRefund(limiter: Limiter): void {
  (limiter as { [ASSERT_REFUND]?: () => void })[ASSERT_REFUND]?.();
}

async function withTimeout<T>(work: Promise<T>, timeoutMs: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`omnilimit: store timed out after ${timeoutMs}ms`)), timeoutMs);
  });
  try {
    return await Promise.race([work, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

/** What a limiter keeps for each reservation it made. Callers only hold the opaque handle. */
interface ReservationState {
  key: string;
  params: HitParams;
  sinceMs: number;
  spent: boolean;
}

export function createLimiter(options: LimiterOptions): Limiter {
  const { store, scope, onStoreError, onEvent, salt = '' } = options;
  const shadow = options.shadow ?? false;
  const headers = options.headers ?? 'draft-11';
  const storeTimeoutMs = options.storeTimeoutMs ?? store.defaultTimeoutMs ?? DEFAULT_STORE_TIMEOUT_MS;
  const policy = resolvePolicy(options);
  assertScope(scope);
  if (onStoreError !== 'open' && onStoreError !== 'closed') {
    throw new TypeError("omnilimit: onStoreError is required and must be 'open' or 'closed'");
  }
  if (typeof shadow !== 'boolean') throw new TypeError('omnilimit: shadow must be a boolean');

  const emit = (event: LimiterEvent): void => {
    try {
      onEvent?.(event);
    } catch {
      // An observability hook must never change a rate limit decision.
    }
  };

  const finish = (decision: Omit<Decision, 'headers'>, effectiveMs: number): Decision => {
    // A client must not see, and adapt to, a limit that is not enforced.
    if (shadow) return { ...decision, headers: {} };
    const input: HeaderInput = { scope, limit: policy.limit, windowMs: policy.windowMs, decision, effectiveMs };
    const built = typeof headers === 'function' ? headers(input) : buildHeaders(headers, input);
    return { ...decision, headers: built };
  };

  const fromHit = (hit: RawHit, amount: number): Decision => {
    const reason = hit.allowed ? 'ok' : 'limited';
    const status = hit.allowed ? null : 429;
    if (hit.level === null) {
      const retryAfterMs = hit.allowed ? null : (hit.retryAfterMs ?? policy.windowMs);
      return finish(
        { allowed: hit.allowed, reason, status, remaining: null, resetMs: null, retryAfterMs, tier: store.tier, shadowed: false },
        retryAfterMs ?? 0,
      );
    }
    const view = viewOf(hit.allowed, hit.level, policy, amount);
    return finish(
      {
        allowed: hit.allowed,
        reason,
        status,
        remaining: view.remaining,
        resetMs: view.resetMs,
        retryAfterMs: view.retryAfterMs,
        tier: store.tier,
        shadowed: false,
      },
      view.effectiveMs,
    );
  };

  const fromFailure = (): Decision => {
    // Reports nothing about quota: the store could not say anything about it.
    const allowed = onStoreError === 'open';
    return finish(
      {
        allowed,
        reason: 'store_error',
        status: allowed ? null : 503,
        remaining: null,
        resetMs: null,
        retryAfterMs: allowed ? null : STORE_ERROR_RETRY_MS,
        tier: store.tier,
        shadowed: false,
      },
      0,
    );
  };

  /** In shadow mode, a decision that would deny lets the request through, keeping its values. */
  const shadowed = (decision: Decision): Decision =>
    shadow && !decision.allowed ? { ...decision, allowed: true, status: null, shadowed: true } : decision;
  const marked = (decision: Decision): { shadowed?: true } => (decision.shadowed ? { shadowed: true } : {});

  /** The decision, and the store's answer when there was one (`null` after a store failure). */
  const decideRaw = async (
    op: 'check' | 'peek',
    amount: number,
    run: () => Promise<RawHit>,
  ): Promise<{ decision: Decision; hit: RawHit | null }> => {
    let hit: RawHit;
    try {
      hit = await withTimeout(run(), storeTimeoutMs);
    } catch (error) {
      const decision = shadowed(fromFailure());
      emit({ type: 'store_error', op, scope, tier: store.tier, error, ...marked(decision) });
      return { decision, hit: null };
    }
    const decision = shadowed(fromHit(hit, amount));
    if (hit.allowed) emit({ type: 'allowed', op, scope, tier: store.tier });
    else emit({ type: 'limited', op, scope, tier: store.tier, ...marked(decision) });
    return { decision, hit };
  };

  const decide = async (op: 'check' | 'peek', amount: number, run: () => Promise<RawHit>): Promise<Decision> =>
    (await decideRaw(op, amount, run)).decision;

  const reservations = new WeakMap<Reservation, ReservationState>();

  /**
   * What the store receives for a request of this cost. Stores add `windowMs` per hit, so a
   * request that weighs `cost` units is the same hit with `cost * windowMs` in its place.
   */
  const weighed = (cost: number | undefined): HitParams => {
    if (cost === undefined || cost === 1) return policy;
    assertCost(cost, policy);
    if (store.tier === 'approximate') {
      throw new TypeError('omnilimit: this store (tier approximate) only counts requests: it cannot weigh a cost');
    }
    const { limit, windowMs, capacity, drainMs } = policy;
    return { limit, windowMs: windowMs * cost, capacity, drainMs };
  };

  const unsupported = (op: 'peek' | 'reset' | 'refund') =>
    new TypeError(`omnilimit: this store (tier ${store.tier}) does not support ${op}`);

  const limiter: Limiter = {
    scope,
    tier: store.tier,
    async check(id, checkOptions) {
      const params = weighed(checkOptions?.cost);
      const key = await deriveKey(scope, policy, salt, id);
      return decide('check', params.windowMs, () => store.hit(key, params, checkOptions?.nowMs));
    },
    async peek(id, peekOptions) {
      const peekFn = store.peek?.bind(store);
      if (!peekFn) throw unsupported('peek');
      const params = weighed(peekOptions?.cost);
      const key = await deriveKey(scope, policy, salt, id);
      return decide('peek', params.windowMs, () => peekFn(key, params, peekOptions?.nowMs));
    },
    async reset(id) {
      const resetFn = store.reset?.bind(store);
      if (!resetFn) throw unsupported('reset');
      const key = await deriveKey(scope, policy, salt, id);
      try {
        await withTimeout(resetFn(key), storeTimeoutMs);
      } catch (error) {
        emit({ type: 'store_error', op: 'reset', scope, tier: store.tier, error });
        return false;
      }
      emit({ type: 'reset', op: 'reset', scope, tier: store.tier });
      return true;
    },
    async reserve(id, reserveOptions) {
      if (!store.refund) throw unsupported('refund');
      const params = weighed(reserveOptions?.cost);
      const key = await deriveKey(scope, policy, salt, id);
      const { decision, hit } = await decideRaw('check', params.windowMs, () => store.hit(key, params, reserveOptions?.nowMs));
      // Only what the store counted can be given back: not a denial, shadowed or not, and not an
      // `open` failure.
      if (!hit?.allowed || hit.ts === null) return { decision, reservation: null };
      const reservation: Reservation = Object.freeze({ scope, cost: reserveOptions?.cost ?? 1 });
      reservations.set(reservation, { key, params, sinceMs: hit.ts, spent: false });
      return { decision, reservation };
    },
    async refund(reservation, refundOptions) {
      if (reservation === null || reservation === undefined) return false;
      const state = reservations.get(reservation);
      if (!state) throw new TypeError('omnilimit: this reservation was not made by this limiter');
      // Spent before the store call, so a second refund, even a concurrent one, does nothing.
      if (state.spent) return false;
      state.spent = true;
      const refundFn = store.refund as NonNullable<Store['refund']>;
      try {
        await withTimeout(refundFn.call(store, state.key, state.params, state.sinceMs, refundOptions?.nowMs), storeTimeoutMs);
      } catch (error) {
        // No retry: a refund that timed out may have been applied, and must not be applied twice.
        emit({ type: 'store_error', op: 'refund', scope, tier: store.tier, error });
        return false;
      }
      emit({ type: 'refund', op: 'refund', scope, tier: store.tier });
      return true;
    },
  };
  Object.defineProperty(limiter, ASSERT_COST, { value: (cost: number | undefined) => void weighed(cost) });
  Object.defineProperty(limiter, ASSERT_REFUND, {
    value: () => {
      if (!store.refund) throw unsupported('refund');
    },
  });
  return limiter;
}
