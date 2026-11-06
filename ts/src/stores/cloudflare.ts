import type { HitParams, RawHit, Store } from '../core/types.js';

/** The Workers Rate Limiting binding (`env.MY_LIMITER`). */
export interface RateLimitBinding {
  limit(options: { key: string }): Promise<{ success: boolean }>;
}

export interface CloudflareBindingOptions {
  /** The binding. A missing one counts as a store failure, handled by `onStoreError`. */
  binding: RateLimitBinding | undefined;
  /** Must repeat the `period` from your Wrangler configuration: 10 or 60. */
  periodSec: 10 | 60;
}

/**
 * Approximate and per location: Cloudflare keeps the real limit in its own configuration,
 * and this store only learns allowed or denied. For a strict limit use `./cloudflare/do`.
 */
export function cloudflareBindingStore(options: CloudflareBindingOptions): Store {
  const { binding, periodSec } = options;
  if (periodSec !== 10 && periodSec !== 60) {
    throw new RangeError('omnilimit: the Cloudflare binding only supports a period of 10 or 60 seconds');
  }
  return {
    tier: 'approximate',
    async hit(key: string, _params: HitParams): Promise<RawHit> {
      if (!binding) throw new Error('omnilimit: Cloudflare rate limit binding is missing');
      const { success } = await binding.limit({ key });
      return success
        ? { allowed: true, level: null, ts: null }
        : { allowed: false, level: null, ts: null, retryAfterMs: periodSec * 1000 };
    },
  };
}
