import { evaluate, ipKey, type IpKeyOptions, type KeyContext, type KeyFn, type MissingKeyPolicy, type Outcome } from './shared.js';
import type { Limiter } from '../core/types.js';

export { ipKey };
export type { IpKeyOptions, KeyContext, KeyFn, MissingKeyPolicy };

export interface NextKeyContext extends KeyContext {
  request: Request;
}

export interface NextRateLimitOptions {
  limiter: Limiter;
  key: KeyFn<NextKeyContext>;
  onMissingKey: MissingKeyPolicy;
}

function denyResponse(outcome: Extract<Outcome, { kind: 'deny' }>): Response {
  return new Response(JSON.stringify(outcome.body), {
    status: outcome.status,
    headers: { 'content-type': 'application/json', ...outcome.headers },
  });
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
  if (outcome.kind === 'deny') return { denied: denyResponse(outcome) };
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
