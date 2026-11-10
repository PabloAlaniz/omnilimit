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

/** Why a request was turned away, handed to `onDenied` so the app can answer in its own format. */
export interface Denial {
  /** 429 when limited, 503 when the store failed closed, 400 when the client could not be identified. */
  status: number;
  /** Headers to send, including `Retry-After` when there is one. */
  headers: Record<string, string>;
  error: 'rate_limited' | 'rate_limiter_unavailable' | 'client_not_identified';
  /** The limiter's decision, or `null` when no check ran (`client_not_identified`). */
  decision: Decision | null;
}

export type Outcome = { kind: 'pass'; headers: Record<string, string> } | ({ kind: 'deny' } & Denial);

export async function evaluate<C extends KeyContext>(options: RateLimitOptions<C>, context: C): Promise<Outcome> {
  const { limiter, key, onMissingKey } = options;
  const id = await key(context);
  if (id === null) {
    if (onMissingKey === 'skip') return { kind: 'pass', headers: {} };
    if (onMissingKey === 'reject') {
      return { kind: 'deny', status: 400, headers: {}, error: 'client_not_identified', decision: null };
    }
    return toOutcome(await limiter.check(SHARED_ID));
  }
  return toOutcome(await limiter.check(id));
}

function toOutcome(decision: Decision): Outcome {
  if (decision.allowed) return { kind: 'pass', headers: decision.headers };
  const error = decision.reason === 'store_error' ? 'rate_limiter_unavailable' : 'rate_limited';
  return { kind: 'deny', status: decision.status ?? 429, headers: decision.headers, error, decision };
}

/** The default denial response: `{"error": "..."}` as JSON with the decision's headers. */
export function defaultDenialResponse(denial: Denial): Response {
  return new Response(JSON.stringify({ error: denial.error }), {
    status: denial.status,
    headers: { 'content-type': 'application/json', ...denial.headers },
  });
}

export function toDenial(outcome: Extract<Outcome, { kind: 'deny' }>): Denial {
  const { status, headers, error, decision } = outcome;
  return { status, headers, error, decision };
}
