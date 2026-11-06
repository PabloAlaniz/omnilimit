/** What a store promises about its counters. See spec/SPEC.md. */
export type Tier = 'exact' | 'local' | 'approximate';

export interface Policy {
  /** Requests replenished per window. */
  limit: number;
  /** Window length in integer milliseconds, at least 1000. */
  windowMs: number;
  /** Maximum instantaneous requests. Defaults to `limit`. */
  burst?: number;
}

/** Integer values computed once by the core, so stores never divide. */
export interface HitParams {
  limit: number;
  windowMs: number;
  capacity: number;
  drainMs: number;
}

export interface RawHit {
  allowed: boolean;
  /** Meter level after this hit, or `null` for approximate stores. */
  level: number | null;
  /** Timestamp stored by the hit, or `null` for approximate stores. */
  ts: number | null;
  /** Only set by approximate stores, which cannot compute it from a level. */
  retryAfterMs?: number;
}

/**
 * A rate limit store. Implementations must apply the hit atomically and use their
 * own clock unless `nowMs` is given (conformance vectors use it).
 */
export interface Store {
  readonly tier: Tier;
  hit(key: string, params: HitParams, nowMs?: number): Promise<RawHit>;
}

export type DecisionReason = 'ok' | 'limited' | 'store_error';

export interface Decision {
  allowed: boolean;
  reason: DecisionReason;
  /** HTTP status to respond with when denied (429, or 503 for a store failure); `null` when allowed. */
  status: 429 | 503 | null;
  remaining: number | null;
  resetMs: number | null;
  retryAfterMs: number | null;
  headers: Record<string, string>;
  tier: Tier;
}

export type HeaderProfile = 'draft-11' | 'legacy' | 'none';

export interface HeaderInput {
  scope: string;
  limit: number;
  windowMs: number;
  decision: Omit<Decision, 'headers'>;
  /** Milliseconds used as the effective window in headers. */
  effectiveMs: number;
}

export type LimiterEvent =
  | { type: 'allowed' | 'limited'; scope: string; tier: Tier }
  | { type: 'store_error'; scope: string; tier: Tier; error: unknown };

export interface LimiterOptions extends Policy {
  store: Store;
  /** Namespace for this limiter: letters, digits, `.`, `_` and `-`, up to 64 characters. */
  scope: string;
  /**
   * What to do when the store fails or times out. Required on purpose: a login route
   * usually wants `closed`, a public read route usually wants `open`.
   */
  onStoreError: 'open' | 'closed';
  /** Milliseconds before a store call counts as failed. Defaults to 1000. */
  storeTimeoutMs?: number;
  /** Response headers. Defaults to `draft-11`. `Retry-After` is always sent when denied. */
  headers?: HeaderProfile | ((input: HeaderInput) => Record<string, string>);
  /** Mixed into the key hash so stored keys cannot be matched against known IPs. */
  salt?: string;
  /** Observability hook. Exceptions it throws are ignored. */
  onEvent?: (event: LimiterEvent) => void;
}

export interface Limiter {
  readonly scope: string;
  readonly tier: Tier;
  check(id: string, options?: { nowMs?: number }): Promise<Decision>;
}
