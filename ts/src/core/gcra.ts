import type { HitParams, Policy, RawHit } from './types.js';

const MIN_WINDOW_MS = 1000;
const SCOPE_PATTERN = /^[A-Za-z0-9._-]{1,64}$/;

/** `ceil(a / b)` for non-negative safe integers, without a float rounding step on the result. */
export function ceilDiv(a: number, b: number): number {
  return Math.floor((a + b - 1) / b);
}

function isPositiveInt(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 1;
}

export interface ResolvedPolicy extends HitParams {
  burst: number;
}

/** Validate a policy and compute the integers shared with every store. Throws on invalid input. */
export function resolvePolicy(policy: Policy): ResolvedPolicy {
  const { limit, windowMs } = policy;
  const burst = policy.burst ?? limit;
  if (!isPositiveInt(limit)) throw new RangeError('omnilimit: limit must be a positive integer');
  if (!isPositiveInt(burst)) throw new RangeError('omnilimit: burst must be a positive integer');
  if (!isPositiveInt(windowMs) || windowMs < MIN_WINDOW_MS) {
    throw new RangeError(`omnilimit: windowMs must be an integer of at least ${MIN_WINDOW_MS}`);
  }
  // 2*B*W + L must fit in 2^53-1. Written without the multiplication overflowing silently.
  if (burst > (Number.MAX_SAFE_INTEGER - limit) / (2 * windowMs)) {
    throw new RangeError('omnilimit: policy too large for exact integer arithmetic (2*burst*windowMs + limit)');
  }
  const capacity = burst * windowMs;
  return { limit, windowMs, burst, capacity, drainMs: ceilDiv(capacity, limit) };
}

export function assertScope(scope: string): void {
  if (!SCOPE_PATTERN.test(scope)) {
    throw new RangeError('omnilimit: scope must match /^[A-Za-z0-9._-]{1,64}$/');
  }
}

export interface MeterState {
  level: number;
  ts: number;
}

export interface MeterResult {
  hit: RawHit & { level: number; ts: number };
  /** State to persist, or `null` when the request was denied: denials never write. */
  write: MeterState | null;
}

/** The algorithm in one place, for stores that run JavaScript (memory, Durable Objects). */
export function applyHit(state: MeterState | null, params: HitParams, nowMs: number): MeterResult {
  const ts = state?.ts ?? nowMs;
  const level = state?.level ?? 0;
  const t = Math.max(nowMs, ts);
  const elapsed = Math.min(t - ts, params.drainMs);
  const drained = Math.max(0, level - elapsed * params.limit);

  if (drained + params.windowMs > params.capacity) {
    return { hit: { allowed: false, level: drained, ts }, write: null };
  }
  const next = drained + params.windowMs;
  return { hit: { allowed: true, level: next, ts: t }, write: { level: next, ts: t } };
}

export interface MeterView {
  remaining: number;
  resetMs: number;
  retryAfterMs: number | null;
  /** Time reported as the effective window in headers. */
  effectiveMs: number;
}

/** Derive the public numbers from a level. All port divisions live here. */
export function viewOf(allowed: boolean, level: number, params: HitParams): MeterView {
  const { capacity, windowMs, limit } = params;
  const remaining = Math.floor((capacity - level) / windowMs);
  const resetMs = ceilDiv(level, limit);
  if (!allowed) {
    const retryAfterMs = ceilDiv(level + windowMs - capacity, limit);
    return { remaining, resetMs, retryAfterMs, effectiveMs: retryAfterMs };
  }
  const effectiveMs = remaining === 0 ? ceilDiv(level + windowMs - capacity, limit) : resetMs;
  return { remaining, resetMs, retryAfterMs: null, effectiveMs };
}
