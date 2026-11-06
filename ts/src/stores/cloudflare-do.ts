import { DurableObject } from 'cloudflare:workers';
import { applyHit, type MeterState } from '../core/gcra.js';
import type { HitParams, RawHit, Store } from '../core/types.js';

const STATE_KEY = 'meter';

interface StoredMeter extends MeterState {
  expiresAt: number;
}

/**
 * One object per bucket. Export it from your Worker and bind it in your Wrangler
 * configuration. Workers input gates serialize the read and write of a hit, and an
 * alarm deletes the bucket's storage once it has fully drained.
 */
export class OmnilimitDurableObject extends DurableObject {
  async hit(params: HitParams, nowMs?: number): Promise<RawHit> {
    const now = nowMs ?? Date.now();
    const current = (await this.ctx.storage.get<StoredMeter>(STATE_KEY)) ?? null;
    const { hit, write } = applyHit(current, params, now);
    if (write) {
      const expiresAt = write.ts + params.drainMs;
      await this.ctx.storage.put<StoredMeter>(STATE_KEY, { ...write, expiresAt });
      if (nowMs === undefined) await this.ctx.storage.setAlarm(expiresAt);
    }
    return hit;
  }

  override async alarm(): Promise<void> {
    const current = await this.ctx.storage.get<StoredMeter>(STATE_KEY);
    if (!current) return;
    if (Date.now() >= current.expiresAt) {
      await this.ctx.storage.deleteAll();
    } else {
      await this.ctx.storage.setAlarm(current.expiresAt);
    }
  }
}

interface DurableObjectStubLike {
  hit(params: HitParams, nowMs?: number): Promise<RawHit>;
}

/** Structural subset of `DurableObjectNamespace<OmnilimitDurableObject>`. */
export interface OmnilimitNamespace {
  idFromName(name: string): unknown;
  get(id: never): unknown;
}

/** Atomic and global, using the Durable Object's clock. */
export function durableObjectStore(namespace: OmnilimitNamespace | undefined): Store {
  return {
    tier: 'exact',
    async hit(key: string, params: HitParams, nowMs?: number): Promise<RawHit> {
      if (!namespace) throw new Error('omnilimit: Durable Object namespace binding is missing');
      const stub = namespace.get(namespace.idFromName(key) as never) as DurableObjectStubLike;
      // Workers RPC rejects `undefined` differently across versions, so pass plain values only.
      const plain: HitParams = {
        limit: params.limit,
        windowMs: params.windowMs,
        capacity: params.capacity,
        drainMs: params.drainMs,
      };
      const reply = await (nowMs === undefined ? stub.hit(plain) : stub.hit(plain, nowMs));
      return { allowed: reply.allowed, level: reply.level, ts: reply.ts };
    },
  };
}
