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
  /**
   * How many units this request weighs: an integer from 1 to the limiter's burst. Use it for
   * requests that do several things at once, such as a batch. Defaults to 1.
   */
  cost?: (context: C) => number | Promise<number>;
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
  const cost = options.cost ? { cost: await options.cost(context) } : {};
  return toOutcome(await limiter.check(id ?? SHARED_ID, cost));
}

function toOutcome(decision: Decision): Outcome {
  if (decision.allowed) return { kind: 'pass', headers: decision.headers };
  const error = decision.reason === 'store_error' ? 'rate_limiter_unavailable' : 'rate_limited';
  return { kind: 'deny', status: decision.status ?? 429, headers: decision.headers, error, decision };
}

/** The media type of a problem details body. */
export const PROBLEM_JSON = 'application/problem+json';

const PROBLEM_TITLES: Record<number, string> = { 400: 'Bad Request', 429: 'Too Many Requests', 503: 'Service Unavailable' };
const PROBLEM_DETAILS: Record<Denial['error'], string> = {
  rate_limited: 'Rate limit exceeded.',
  rate_limiter_unavailable: 'The rate limiter is unavailable.',
  client_not_identified: 'The client could not be identified.',
};

/** A denial as a problem details object (RFC 9457). Send it as `application/problem+json`. */
export interface ProblemDetails {
  type: 'about:blank';
  /** The phrase of the status code, as RFC 9457 asks of `about:blank`. */
  title: string;
  status: number;
  detail: string;
  /** The code to act on: the same one the default body carries. */
  error: Denial['error'];
  /** Seconds to wait, when the denial has a `Retry-After` header. */
  retry_after?: number;
}

/**
 * The problem details body of a denial. Its members and their order are part of the spec, so
 * `JSON.stringify` of it gives the same bytes in every port.
 */
export function problemDetails(denial: Pick<Denial, 'status' | 'error' | 'headers'>): ProblemDetails {
  const body: ProblemDetails = {
    type: 'about:blank',
    title: PROBLEM_TITLES[denial.status] ?? 'Error',
    status: denial.status,
    detail: PROBLEM_DETAILS[denial.error],
    error: denial.error,
  };
  const retryAfter = denial.headers['retry-after'];
  if (retryAfter !== undefined) body.retry_after = Number(retryAfter);
  return body;
}

/**
 * The default denial response: `{"error": "..."}` as JSON with the decision's headers, or the
 * problem details body when `problem` is set.
 */
export function defaultDenialResponse(denial: Denial, problem = false): Response {
  return new Response(JSON.stringify(problem ? problemDetails(denial) : { error: denial.error }), {
    status: denial.status,
    headers: { 'content-type': problem ? PROBLEM_JSON : 'application/json', ...denial.headers },
  });
}

export function toDenial(outcome: Extract<Outcome, { kind: 'deny' }>): Denial {
  const { status, headers, error, decision } = outcome;
  return { status, headers, error, decision };
}
