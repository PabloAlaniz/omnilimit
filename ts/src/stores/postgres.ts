import { POSTGRES_MIGRATION_SQL } from '../generated/assets.js';
import type { HitParams, RawHit, Store } from '../core/types.js';

/** Structurally compatible with a `pg` Pool or Client. */
export interface PgQueryable {
  query(sql: string, params?: unknown[]): Promise<{ rows: Array<Record<string, unknown>> }>;
}

export interface PostgresStore extends Store {
  /** Delete up to `batch` expired buckets and return how many were removed. Schedule it yourself. */
  cleanup(batch?: number): Promise<number>;
}

/** SQL creating the table and functions. Apply it with your migration tool; it never runs on its own. */
export const MIGRATION_SQL = POSTGRES_MIGRATION_SQL;

/**
 * Atomic and shared, using a row lock per bucket and the database clock. Good when
 * Postgres is already there. Each hit is a write, so it is not meant for high traffic.
 */
export function postgresStore(db: PgQueryable): PostgresStore {
  return {
    tier: 'exact',
    async hit(key: string, params: HitParams, nowMs?: number): Promise<RawHit> {
      const { rows } = await db.query('SELECT allowed, new_level, new_ts FROM omnilimit_hit_v1($1, $2, $3, $4, $5, $6)', [
        key,
        params.limit,
        params.windowMs,
        params.capacity,
        params.drainMs,
        nowMs ?? null,
      ]);
      const row = rows[0];
      if (!row) throw new Error('omnilimit: omnilimit_hit_v1 returned no rows');
      // pg returns bigint as string; every value is below 2^53 by policy validation.
      return { allowed: row['allowed'] === true, level: Number(row['new_level']), ts: Number(row['new_ts']) };
    },
    async cleanup(batch = 1000): Promise<number> {
      const { rows } = await db.query('SELECT omnilimit_cleanup_v1($1) AS deleted', [batch]);
      return Number(rows[0]?.['deleted'] ?? 0);
    },
  };
}
