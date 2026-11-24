import {
  defaultDenialResponse,
  evaluate,
  ipKey,
  PROBLEM_JSON,
  problemDetails,
  toDenial,
  type Denial,
  type IpKeyOptions,
  type KeyContext,
  type KeyFn,
  type LimiterSource,
  type MissingKeyPolicy,
  type ProblemDetails,
} from './shared.js';

export { ipKey, PROBLEM_JSON, problemDetails };
export type { Denial, IpKeyOptions, KeyContext, KeyFn, LimiterSource, MissingKeyPolicy, ProblemDetails };

export interface WebKeyContext<Extra = unknown> extends KeyContext {
  request: Request;
  /**
   * Whatever you passed as `extra` to `checkRateLimit`, or the handler's second argument under
   * `withRateLimit`: the `env` of a Worker, the context of an Astro or SvelteKit route.
   */
  extra: Extra;
}

export interface WebRateLimitOptions<Extra = unknown> {
  /** The limiter, or a function that finds it for each request, for example from `extra.env`. */
  limiter: LimiterSource<WebKeyContext<Extra>>;
  key: KeyFn<WebKeyContext<Extra>>;
  onMissingKey: MissingKeyPolicy;
  /** Return `true` to let a request through without counting it. */
  skip?: (context: WebKeyContext<Extra>) => boolean | Promise<boolean>;
  /** How many units this request weighs, from 1 to the limiter's burst. Defaults to 1. */
  cost?: (context: WebKeyContext<Extra>) => number | Promise<number>;
  /**
   * The TCP peer, when your runtime exposes it. Without it the `direct` preset finds no
   * address. A Fetch API `Request` does not carry one.
   */
  peer?: (context: { request: Request; extra: Extra }) => string | null | undefined;
  /**
   * Build the response for a denied request, for example in your API's error format. Pass
   * `denial.headers` on so clients still see `Retry-After`. Defaults to `{"error": "..."}` as JSON.
   */
  onDenied?: (denial: Denial, request: Request) => Response | Promise<Response>;
  /**
   * Answer a denied request with a problem details body (RFC 9457), as
   * `application/problem+json`, instead of `{"error": "..."}`. `onDenied` takes precedence.
   */
  problemDetails?: boolean;
}

export type WebCheckResult =
  | { denied: Response; headers?: undefined }
  | { denied?: undefined; headers: Record<string, string> };

/**
 * Check a Fetch API `Request` by hand: a Worker's `fetch`, an Astro or SvelteKit endpoint, a
 * Next `proxy.ts`. Returns the response to send when denied, otherwise the headers to attach
 * to your own response.
 */
export async function checkRateLimit<Extra = undefined>(
  request: Request,
  options: WebRateLimitOptions<Extra>,
  ...rest: undefined extends Extra ? [extra?: Extra] : [extra: Extra]
): Promise<WebCheckResult> {
  const extra = rest[0] as Extra;
  const peer = options.peer?.({ request, extra }) ?? null;
  const outcome = await evaluate(options, { headers: request.headers, peer, request, extra });
  if (outcome.kind === 'deny') {
    const denial = toDenial(outcome);
    return { denied: options.onDenied ? await options.onDenied(denial, request) : defaultDenialResponse(denial, options.problemDetails) };
  }
  return { headers: outcome.headers };
}

/**
 * Wrap a handler that takes a `Request` first: `export const POST = withRateLimit(handler, options)`.
 * The handler's second argument, if any, reaches `key`, `skip` and a `limiter` function as `extra`.
 */
export function withRateLimit<R extends Request, A extends unknown[]>(
  handler: (request: R, ...args: A) => Response | Promise<Response>,
  options: WebRateLimitOptions<A[0]>,
): (request: R, ...args: A) => Promise<Response> {
  return async (request, ...args) => {
    const result = await checkRateLimit<unknown>(request, options as WebRateLimitOptions<unknown>, args[0]);
    if (result.denied) return result.denied;
    const response = await handler(request, ...args);
    // Responses from fetch() or Response.redirect() may have immutable headers.
    const merged = new Response(response.body, response);
    for (const [name, value] of Object.entries(result.headers)) merged.headers.set(name, value);
    return merged;
  };
}
