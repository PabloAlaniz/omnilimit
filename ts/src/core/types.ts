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
  /**
   * Timestamp stored by the hit, on the store's clock, or `null` for approximate stores. For an
   * allowed hit it is the `since` of its reservation.
   */
  ts: number | null;
  /** Only set by approximate stores, which cannot compute it from a level. */
  retryAfterMs?: number;
}

/** What a store answers to a refund: the level and timestamp it left, or found. */
export interface RawRefund {
  level: number;
  ts: number;
}

/**
 * A rate limit store. Implementations must apply the hit atomically and use their
 * own clock unless `nowMs` is given (conformance vectors use it).
 */
export interface Store {
  readonly tier: Tier;
  /**
   * The timeout a limiter uses for this store when `storeTimeoutMs` is not given. A store sets
   * it when its normal latency does not fit the general default, as a Durable Object waking
   * from cold does.
   */
  readonly defaultTimeoutMs?: number;
  hit(key: string, params: HitParams, nowMs?: number): Promise<RawHit>;
  /**
   * The decision `hit` would give now, without writing. `level` is the drained level and `ts`
   * the stored timestamp. Optional: approximate stores cannot answer it.
   */
  peek?(key: string, params: HitParams, nowMs?: number): Promise<RawHit>;
  /** Delete the bucket, so the next hit starts from an empty meter. Optional. */
  reset?(key: string): Promise<void>;
  /**
   * Give back a hit counted at `sinceMs` (the `ts` it stored) with these params, as far as it has
   * not drained yet, and never below zero. See "Refund" in the spec. Optional: approximate stores
   * cannot answer it.
   */
  refund?(key: string, params: HitParams, sinceMs: number, nowMs?: number): Promise<RawRefund>;
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

/** The limiter operation an event comes from. `reserve` reports as `check`. */
export type LimiterOp = 'check' | 'peek' | 'reset' | 'refund';

export type LimiterEvent =
  | { type: 'allowed' | 'limited'; op: 'check' | 'peek'; scope: string; tier: Tier }
  | { type: 'reset'; op: 'reset'; scope: string; tier: Tier }
  | { type: 'refund'; op: 'refund'; scope: string; tier: Tier }
  | { type: 'store_error'; op: LimiterOp; scope: string; tier: Tier; error: unknown };

export interface LimiterOptions extends Policy {
  store: Store;
  /** Namespace for this limiter: letters, digits, `.`, `_` and `-`, up to 64 characters. */
  scope: string;
  /**
   * What to do when the store fails or times out. Required on purpose: a login route
   * usually wants `closed`, a public read route usually wants `open`.
   */
  onStoreError: 'open' | 'closed';
  /** Milliseconds before a store call counts as failed. Defaults to the store's own default, or 1000. */
  storeTimeoutMs?: number;
  /** Response headers. Defaults to `draft-11`. `Retry-After` is always sent when denied. */
  headers?: HeaderProfile | ((input: HeaderInput) => Record<string, string>);
  /** Mixed into the key hash so stored keys cannot be matched against known IPs. */
  salt?: string;
  /** Observability hook. Exceptions it throws are ignored. */
  onEvent?: (event: LimiterEvent) => void;
}

/**
 * A counted request that can be given back once with `limiter.refund`. Opaque: only the limiter
 * that made it can refund it.
 */
export interface Reservation {
  readonly scope: string;
  /** The units the request weighed. */
  readonly cost: number;
}

/** What `limiter.reserve` answers. */
export interface Reserved {
  decision: Decision;
  /**
   * Set when the request was counted: allowed by the store. `null` when it was denied, or allowed
   * by `onStoreError: 'open'` after a store failure, since nothing was counted then.
   */
  reservation: Reservation | null;
}

export interface Limiter {
  readonly scope: string;
  readonly tier: Tier;
  /**
   * Count one request and decide. `cost` makes it weigh several units, such as a batch of
   * items: an integer from 1 to the burst, anything else throws a `RangeError`. A denied
   * request counts nothing, whatever its cost.
   */
  check(id: string, options?: { nowMs?: number; cost?: number }): Promise<Decision>;
  /**
   * The decision `check` would give now, without counting. When allowed, `remaining` includes
   * the request you asked about. Advisory: a concurrent request can take the last unit, so
   * the `check` that follows is still the authority. Throws a `TypeError` if the store cannot
   * peek (approximate stores); store failures follow `onStoreError` like `check`.
   */
  peek(id: string, options?: { nowMs?: number; cost?: number }): Promise<Decision>;
  /**
   * Forget everything counted for `id`. Resolves `true` once deleted and `false` if the store
   * failed or timed out (reported through `onEvent`); it never rejects for a store failure.
   * Throws a `TypeError` if the store cannot reset.
   */
  reset(id: string): Promise<boolean>;
  /**
   * `check`, keeping what is needed to give the request back with `refund` if its work does not
   * happen. The decision is the one `check` would give. Throws a `TypeError` if the store cannot
   * refund (approximate stores), before counting anything.
   */
  reserve(id: string, options?: { nowMs?: number; cost?: number }): Promise<Reserved>;
  /**
   * Give back a reservation, as far as it has not drained yet: a request refunded at once leaves
   * no trace, and one refunded after the meter drained its units gives nothing. Resolves `true`
   * once applied, and `false` for `null`, for a reservation already refunded (without calling the
   * store), or if the store failed or timed out (reported through `onEvent`). It never rejects for
   * a store failure and never retries: a refund that timed out may have been applied, and the
   * reservation is spent either way. Throws a `TypeError` for a reservation from another limiter.
   */
  refund(reservation: Reservation | null, options?: { nowMs?: number }): Promise<boolean>;
}
