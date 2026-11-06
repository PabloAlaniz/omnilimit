import { ceilDiv } from './gcra.js';
import type { HeaderInput, HeaderProfile } from './types.js';

const toSeconds = (ms: number): number => ceilDiv(ms, 1000);

/**
 * Build response headers. `draft-11` is pinned to draft-ietf-httpapi-ratelimit-headers-11,
 * which is not yet an RFC: a newer draft becomes a new profile, never a silent change.
 */
export function buildHeaders(profile: HeaderProfile, input: HeaderInput): Record<string, string> {
  const { decision, scope, limit, windowMs, effectiveMs } = input;
  const out: Record<string, string> = {};
  if (decision.remaining !== null) {
    const t = toSeconds(effectiveMs);
    if (profile === 'draft-11') {
      out['ratelimit-policy'] = `"${scope}";q=${limit};w=${toSeconds(windowMs)}`;
      out['ratelimit'] = `"${scope}";r=${decision.remaining};t=${t}`;
    } else if (profile === 'legacy') {
      out['x-ratelimit-limit'] = String(limit);
      out['x-ratelimit-remaining'] = String(decision.remaining);
      out['x-ratelimit-reset'] = String(t);
    }
  }
  if (!decision.allowed && decision.retryAfterMs !== null) {
    out['retry-after'] = String(toSeconds(decision.retryAfterMs));
  }
  return out;
}
