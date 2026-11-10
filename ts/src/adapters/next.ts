import {
  defaultDenialResponse,
  evaluate,
  ipKey,
  toDenial,
  type Denial,
  type IpKeyOptions,
  type KeyContext,
  type KeyFn,
  type MissingKeyPolicy,
} from './shared.js';
import type { Limiter } from '../core/types.js';

export { ipKey };
export type { Denial, IpKeyOptions, KeyContext, KeyFn, MissingKeyPolicy };

export interface NextKeyContext extends KeyContext {
  request: Request;
}

export interface NextRateLimitOptions {
  limiter: Limiter;
  key: KeyFn<NextKeyContext>;
  onMissingKey: MissingKeyPolicy;
  /**
   * Build the response for a denied request, for example in your API's error format. Pass
   * `denial.headers` on so clients still see `Retry-After`. Defaults to `{"error": "..."}` as JSON.
   */
  onDenied?: (denial: Denial, request: Request) => Response | Promise<Response>;
}

/**
 * Check a request by hand, for example in `proxy.ts`/`middleware.ts`. Returns the response to
 * send when denied, otherwise the headers to attach to your own response.
 */
export async function checkRateLimit(
  request: Request,
  options: NextRateLimitOptions,
): Promise<{ denied: Response; headers?: undefined } | { denied?: undefined; headers: Record<string, string> }> {
  const outcome = await evaluate(options, { headers: request.headers, peer: null, request });
  if (outcome.kind === 'deny') {
    const denial = toDenial(outcome);
    return { denied: options.onDenied ? await options.onDenied(denial, request) : defaultDenialResponse(denial) };
  }
  return { headers: outcome.headers };
}

/** Wrap a Route Handler: `export const POST = withRateLimit(handler, options)`. */
export function withRateLimit<R extends Request, A extends unknown[]>(
  handler: (request: R, ...args: A) => Response | Promise<Response>,
  options: NextRateLimitOptions,
): (request: R, ...args: A) => Promise<Response> {
  return async (request, ...args) => {
    const result = await checkRateLimit(request, options);
    if (result.denied) return result.denied;
    const response = await handler(request, ...args);
    // Responses from fetch() or Response.redirect() may have immutable headers.
    const merged = new Response(response.body, response);
    for (const [name, value] of Object.entries(result.headers)) merged.headers.set(name, value);
    return merged;
  };
}
