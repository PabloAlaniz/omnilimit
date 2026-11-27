import { applyHit, applyPeek, applyRefund, type MeterState } from '../core/gcra.js';
import type { HitParams, RawHit, RawRefund, Store } from '../core/types.js';

export interface MemoryStoreOptions {
  /** Bucket cap, so rotating ids cannot exhaust memory. Expired buckets go first, then the least recently hit. Defaults to 100000. */
  maxKeys?: number;
  /**
   * The clock, in milliseconds since the epoch. Defaults to `Date.now`. Pass your own to move
   * time in tests without faking timers.
   */
  now?: () => number;
}

interface Entry extends MeterState {
  expiresAt: number;
}

/**
 * A bucket in its queue, which runs from the least recently hit to the most recent. `seq` numbers
 * the hits, to tell which of two queues starts with the older one.
 */
interface Bucket extends Entry {
  key: string;
  seq: number;
  older: Bucket;
  newer: Bucket;
}

/**
 * The buckets by key, and in the order of their last hit: one queue per drain time. Buckets that
 * drain in the same time expire in the order of their counted hits, so the first of a queue is
 * the first of it to expire, unless a denial has since moved an older one behind it. Evicting
 * never looks past the first bucket of each queue: its cost does not grow with the number of
 * buckets.
 */
class Buckets {
  private readonly byKey = new Map<string, Bucket>();
  /** By drain time. A queue is a ring through a bucket that holds nothing: its `newer` is the oldest write. */
  private readonly queues = new Map<number, Bucket>();
  private writes = 0;

  get size(): number {
    return this.byKey.size;
  }

  get(key: string): Bucket | undefined {
    return this.byKey.get(key);
  }

  /** Store a bucket. One that exists is written where it is; a new one is the newest write. */
  set(key: string, entry: Entry): void {
    const bucket = this.byKey.get(key);
    if (bucket) {
      bucket.level = entry.level;
      bucket.ts = entry.ts;
      bucket.expiresAt = entry.expiresAt;
      return;
    }
    const created: Bucket = { level: entry.level, ts: entry.ts, expiresAt: entry.expiresAt, key, seq: 0, older: null as never, newer: null as never };
    this.byKey.set(key, created);
    this.append(created);
  }

  /** Write a bucket that exists, and make it the newest write. */
  renew(bucket: Bucket, level: number, ts: number, expiresAt: number): void {
    bucket.older.newer = bucket.newer;
    bucket.newer.older = bucket.older;
    bucket.level = level;
    bucket.ts = ts;
    bucket.expiresAt = expiresAt;
    this.append(bucket);
  }

  /** Make a bucket the most recently hit, and leave what it holds alone. */
  touch(bucket: Bucket): void {
    bucket.older.newer = bucket.newer;
    bucket.newer.older = bucket.older;
    this.append(bucket);
  }

  delete(key: string): void {
    const bucket = this.byKey.get(key);
    if (!bucket) return;
    bucket.older.newer = bucket.newer;
    bucket.newer.older = bucket.older;
    this.byKey.delete(key);
  }

  /**
   * Drop one bucket: an expired one if a queue starts with one, else a live one, and of two alike
   * the least recently hit.
   */
  evict(now: number): void {
    let victim: Bucket | undefined;
    let expired = false;
    for (const [drainMs, queue] of this.queues) {
      const first = queue.newer;
      if (first === queue) {
        // Every bucket of this drain time is gone.
        this.queues.delete(drainMs);
        continue;
      }
      const gone = first.expiresAt <= now;
      if (!victim || (gone && !expired) || (gone === expired && first.seq < victim.seq)) {
        victim = first;
        expired = gone;
      }
    }
    if (victim) this.delete(victim.key);
  }

  private append(bucket: Bucket): void {
    const drainMs = bucket.expiresAt - bucket.ts;
    let queue = this.queues.get(drainMs);
    if (!queue) {
      queue = { level: 0, ts: 0, expiresAt: 0, key: '', seq: 0, older: null as never, newer: null as never };
      queue.older = queue.newer = queue;
      this.queues.set(drainMs, queue);
    }
    bucket.seq = ++this.writes;
    bucket.older = queue.older;
    bucket.newer = queue;
    queue.older.newer = bucket;
    queue.older = bucket;
  }
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
  // Read `Date.now` on every call: fake timers replace it after the store exists.
  const clock = options.now ?? (() => Date.now());
  const entries = new Buckets();

  return {
    tier: 'local',
    async hit(key: string, params: HitParams, nowMs?: number): Promise<RawHit> {
      const now = nowMs ?? clock();
      const current = entries.get(key);
      const { hit, write } = applyHit(current ?? null, params, now);
      if (!write) {
        // A denial writes nothing, but the bucket is in use: a client that keeps being denied
        // must not age out and come back with an empty meter.
        if (current) entries.touch(current);
      } else {
        const expiresAt = write.ts + params.drainMs;
        if (current) {
          entries.renew(current, write.level, write.ts, expiresAt);
        } else {
          entries.set(key, { level: write.level, ts: write.ts, expiresAt });
          if (entries.size > maxKeys) entries.evict(now);
        }
      }
      return hit;
    },
    async peek(key: string, params: HitParams, nowMs?: number): Promise<RawHit> {
      // Reading does not refresh recency: only a hit, counted or denied, keeps a bucket in the store.
      return applyPeek(entries.get(key) ?? null, params, nowMs ?? clock());
    },
    async reset(key: string): Promise<void> {
      entries.delete(key);
    },
    async refund(key: string, params: HitParams, sinceMs: number, nowMs?: number): Promise<RawRefund> {
      const { refund, write } = applyRefund(entries.get(key) ?? null, params, sinceMs, nowMs ?? clock());
      // Updated in place: giving back a request does not make its bucket more recent.
      if (write) entries.set(key, { ...write, expiresAt: write.ts + params.drainMs });
      return refund;
    },
  };
}
