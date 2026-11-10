import { assertScope, resolvePolicy, viewOf } from './gcra.js';
import { buildHeaders } from './headers.js';
import { deriveKey } from './key.js';
import type { Decision, HeaderInput, Limiter, LimiterEvent, LimiterOptions, RawHit } from './types.js';

const DEFAULT_STORE_TIMEOUT_MS = 1000;
const STORE_ERROR_RETRY_MS = 1000;

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

export function createLimiter(options: LimiterOptions): Limiter {
  const { store, scope, onStoreError, onEvent, salt = '' } = options;
  const headers = options.headers ?? 'draft-11';
  const storeTimeoutMs = options.storeTimeoutMs ?? DEFAULT_STORE_TIMEOUT_MS;
  const policy = resolvePolicy(options);
  assertScope(scope);
  if (onStoreError !== 'open' && onStoreError !== 'closed') {
    throw new TypeError("omnilimit: onStoreError is required and must be 'open' or 'closed'");
  }

  const emit = (event: LimiterEvent): void => {
    try {
      onEvent?.(event);
    } catch {
      // An observability hook must never change a rate limit decision.
    }
  };

  const finish = (decision: Omit<Decision, 'headers'>, effectiveMs: number): Decision => {
    const input: HeaderInput = { scope, limit: policy.limit, windowMs: policy.windowMs, decision, effectiveMs };
    const built = typeof headers === 'function' ? headers(input) : buildHeaders(headers, input);
    return { ...decision, headers: built };
  };

  const fromHit = (hit: RawHit): Decision => {
    const reason = hit.allowed ? 'ok' : 'limited';
    const status = hit.allowed ? null : 429;
    if (hit.level === null) {
      const retryAfterMs = hit.allowed ? null : (hit.retryAfterMs ?? policy.windowMs);
      return finish(
        { allowed: hit.allowed, reason, status, remaining: null, resetMs: null, retryAfterMs, tier: store.tier },
        retryAfterMs ?? 0,
      );
    }
    const view = viewOf(hit.allowed, hit.level, policy);
    return finish(
      {
        allowed: hit.allowed,
        reason,
        status,
        remaining: view.remaining,
        resetMs: view.resetMs,
        retryAfterMs: view.retryAfterMs,
        tier: store.tier,
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
      },
      0,
    );
  };

  const decide = async (op: 'check' | 'peek', run: () => Promise<RawHit>): Promise<Decision> => {
    let hit: RawHit;
    try {
      hit = await withTimeout(run(), storeTimeoutMs);
    } catch (error) {
      emit({ type: 'store_error', op, scope, tier: store.tier, error });
      return fromFailure();
    }
    emit({ type: hit.allowed ? 'allowed' : 'limited', op, scope, tier: store.tier });
    return fromHit(hit);
  };

  const unsupported = (op: 'peek' | 'reset') =>
    new TypeError(`omnilimit: this store (tier ${store.tier}) does not support ${op}`);

  return {
    scope,
    tier: store.tier,
    async check(id, checkOptions) {
      const key = await deriveKey(scope, policy, salt, id);
      return decide('check', () => store.hit(key, policy, checkOptions?.nowMs));
    },
    async peek(id, peekOptions) {
      const peekFn = store.peek?.bind(store);
      if (!peekFn) throw unsupported('peek');
      const key = await deriveKey(scope, policy, salt, id);
      return decide('peek', () => peekFn(key, policy, peekOptions?.nowMs));
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
  };
}
