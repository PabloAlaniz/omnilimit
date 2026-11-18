import type { Decision, Limiter } from '../core/types.js';
import { clientKey, type HeaderSource, type IpStrategy } from '../ip/index.js';

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

/** A limiter, or a function that finds it for each request. */
export type LimiterSource<C extends KeyContext = KeyContext> = Limiter | ((context: C) => Limiter | Promise<Limiter>);

export interface RateLimitOptions<C extends KeyContext = KeyContext> {
  /**
   * The limiter. Pass a function when it can only be built inside the request, as with a
   * Workers binding that lives in `env`. Cache what it returns: it runs on every request.
   */
  limiter: LimiterSource<C>;
  key: KeyFn<C>;
  /** What to do when `key` returns `null`. Required so a misconfigured proxy cannot pass unnoticed. */
  onMissingKey: MissingKeyPolicy;
  /**
   * Return `true` to let a request through without counting it: a health check, a test
   * environment, a feature flag. It runs before `key` and the limiter, and no headers are sent.
   */
  skip?: (context: C) => boolean | Promise<boolean>;
}

export type IpKeyOptions = IpStrategy & {
  /** IPv6 prefix to group by. Defaults to 64. */
  ipv6Prefix?: number;
};

/** Key requests by trusted client IP. */
export function ipKey(options: IpKeyOptions): KeyFn {
  return ({ headers, peer }) => clientKey({ headers, peer }, options);
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
  const { key, onMissingKey, skip } = options;
  if (skip && (await skip(context))) return { kind: 'pass', headers: {} };
  const id = await key(context);
  if (id === null) {
    if (onMissingKey === 'skip') return { kind: 'pass', headers: {} };
    if (onMissingKey === 'reject') {
      return { kind: 'deny', status: 400, headers: {}, error: 'client_not_identified', decision: null };
    }
  }
  const limiter = typeof options.limiter === 'function' ? await options.limiter(context) : options.limiter;
  return toOutcome(await limiter.check(id ?? SHARED_ID));
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
