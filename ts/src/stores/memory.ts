import { applyHit, applyPeek, type MeterState } from '../core/gcra.js';
import type { HitParams, RawHit, Store } from '../core/types.js';

export interface MemoryStoreOptions {
  /** Bucket cap, so rotating ids cannot exhaust memory. Oldest buckets go first. Defaults to 100000. */
  maxKeys?: number;
}

interface Entry extends MeterState {
  expiresAt: number;
}

/**
 * Exact within one process and independent per instance: N instances allow roughly N
 * times the limit, and a serverless platform may give each request a new counter.
 */
export function memoryStore(options: MemoryStoreOptions = {}): Store {
  const maxKeys = options.maxKeys ?? 100_000;
  if (!Number.isSafeInteger(maxKeys) || maxKeys < 1) {
    throw new RangeError('omnilimit: maxKeys must be a positive integer');
  }
  const entries = new Map<string, Entry>();

  return {
    tier: 'local',
    async hit(key: string, params: HitParams, nowMs?: number): Promise<RawHit> {
      const now = nowMs ?? Date.now();
      const current = entries.get(key) ?? null;
      const { hit, write } = applyHit(current, params, now);
      if (write) {
        // Delete first so the Map's insertion order tracks recency.
        entries.delete(key);
        entries.set(key, { ...write, expiresAt: write.ts + params.drainMs });
        evict(entries, maxKeys, now);
      }
      return hit;
    },
    async peek(key: string, params: HitParams, nowMs?: number): Promise<RawHit> {
      // Reading does not refresh recency: only counted requests keep a bucket alive.
      return applyPeek(entries.get(key) ?? null, params, nowMs ?? Date.now());
    },
    async reset(key: string): Promise<void> {
      entries.delete(key);
    },
  };
}

function evict(entries: Map<string, Entry>, maxKeys: number, now: number): void {
  if (entries.size <= maxKeys) return;
  for (const [key, entry] of entries) {
    if (entry.expiresAt <= now) entries.delete(key);
    if (entries.size <= maxKeys) return;
  }
  for (const key of entries.keys()) {
    entries.delete(key);
    if (entries.size <= maxKeys) return;
  }
}
