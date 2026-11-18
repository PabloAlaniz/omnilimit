/**
 * Next.js Route Handlers and `proxy.ts`/`middleware.ts`. These are the Fetch API helpers of
 * `omnilimit/web` under the name Next users look for.
 */
import type { WebKeyContext, WebRateLimitOptions } from './web.js';

export { checkRateLimit, ipKey, withRateLimit } from './web.js';
export type { Denial, IpKeyOptions, KeyContext, KeyFn, LimiterSource, MissingKeyPolicy } from './web.js';

export type NextKeyContext<Extra = unknown> = WebKeyContext<Extra>;
export type NextRateLimitOptions<Extra = unknown> = WebRateLimitOptions<Extra>;
