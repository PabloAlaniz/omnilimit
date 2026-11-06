import type { Decision, Limiter } from '../core/types.js';
import { maskIp, resolveClientIp, type HeaderSource, type IpStrategy } from '../ip/index.js';

export interface KeyContext {
  headers: HeaderSource;
  peer: string | null;
}

/** Returns the bucket id, or `null` if it cannot be determined. */
export type KeyFn<C extends KeyContext = KeyContext> = (context: C) => string | null | Promise<string | null>;

export type MissingKeyPolicy =
  /** Everyone without a key shares one bucket: safe, but a single abuser can exhaust it. */
  | 'shared'
  /** Skip rate limiting for this request. */
  | 'skip'
  /** Reject with a 400. */
  | 'reject';

export interface RateLimitOptions<C extends KeyContext = KeyContext> {
  limiter: Limiter;
  key: KeyFn<C>;
  /** What to do when `key` returns `null`. Required so a misconfigured proxy cannot pass unnoticed. */
  onMissingKey: MissingKeyPolicy;
}

export type IpKeyOptions = IpStrategy & {
  /** IPv6 prefix to group by. Defaults to 64. */
  ipv6Prefix?: number;
};

/** Key requests by trusted client IP. */
export function ipKey(options: IpKeyOptions): KeyFn {
  const { ipv6Prefix = 64, ...strategy } = options;
  return ({ headers, peer }) => {
    const ip = resolveClientIp({ headers, peer }, strategy as IpStrategy);
    return ip === null ? null : maskIp(ip, ipv6Prefix);
  };
}

const SHARED_ID = '__omnilimit_shared__';

export type Outcome =
  | { kind: 'pass'; headers: Record<string, string> }
  | { kind: 'deny'; status: number; headers: Record<string, string>; body: { error: string } };

export async function evaluate<C extends KeyContext>(options: RateLimitOptions<C>, context: C): Promise<Outcome> {
  const { limiter, key, onMissingKey } = options;
  const id = await key(context);
  if (id === null) {
    if (onMissingKey === 'skip') return { kind: 'pass', headers: {} };
    if (onMissingKey === 'reject') {
      return { kind: 'deny', status: 400, headers: {}, body: { error: 'client_not_identified' } };
    }
    return toOutcome(await limiter.check(SHARED_ID));
  }
  return toOutcome(await limiter.check(id));
}

function toOutcome(decision: Decision): Outcome {
  if (decision.allowed) return { kind: 'pass', headers: decision.headers };
  const error = decision.reason === 'store_error' ? 'rate_limiter_unavailable' : 'rate_limited';
  return { kind: 'deny', status: decision.status ?? 429, headers: decision.headers, body: { error } };
}
