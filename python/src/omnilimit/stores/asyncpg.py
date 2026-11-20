"""Postgres store on asyncpg, for applications that already hold an asyncpg pool.

Apply `MIGRATION_SQL` first, as for the psycopg stores. Pass a pool: a connection of yours that
is inside a transaction would hold the bucket's row lock until that transaction ends.
"""

from __future__ import annotations

from typing import Any, Literal

from omnilimit._types import HitParams, RawHit, RawRefund
from omnilimit.stores.postgres import (
    MIGRATION_SQL,
    _hit_params,
    _parse,
    _parse_refund,
    _refund_params,
)

__all__ = ["MIGRATION_SQL", "AsyncpgStore"]

_HIT = (
    "SELECT allowed, new_level, new_ts FROM omnilimit_hit_v1("
    "$1::text, $2::bigint, $3::bigint, $4::bigint, $5::bigint, $6::bigint)"
)
_PEEK = (
    "SELECT allowed, new_level, new_ts FROM omnilimit_peek_v1("
    "$1::text, $2::bigint, $3::bigint, $4::bigint, $5::bigint, $6::bigint)"
)
_REFUND = (
    "SELECT new_level, new_ts FROM omnilimit_refund_v1("
    "$1::text, $2::bigint, $3::bigint, $4::bigint, $5::bigint, $6::bigint, $7::bigint)"
)
_RESET = "DELETE FROM omnilimit_state WHERE key = $1"
_CLEANUP = "SELECT omnilimit_cleanup_v1($1::integer)"


class AsyncpgStore:
    """For an asyncpg pool. Use it with `AsyncLimiter`."""

    tier: Literal["exact"] = "exact"

    def __init__(self, pool: Any) -> None:
        if not callable(getattr(pool, "fetchrow", None)):
            raise TypeError("omnilimit: pass an asyncpg pool")
        self._pool = pool

    async def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        return _parse(await self._pool.fetchrow(_HIT, *_hit_params(key, params, now_ms)))

    async def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        """What `hit` would answer now, without writing or locking."""
        row = await self._pool.fetchrow(_PEEK, *_hit_params(key, params, now_ms))
        return _parse(row, "omnilimit_peek_v1")

    async def reset(self, key: str) -> None:
        await self._pool.execute(_RESET, key)

    async def refund(
        self, key: str, params: HitParams, since: int, now_ms: int | None = None
    ) -> RawRefund:
        """Give back a hit counted at `since`, as far as it has not drained."""
        row = await self._pool.fetchrow(_REFUND, *_refund_params(key, params, since, now_ms))
        return _parse_refund(row)

    async def cleanup(self, batch: int = 1000) -> int:
        """Delete up to `batch` expired buckets and return how many. Schedule it yourself."""
        row = await self._pool.fetchrow(_CLEANUP, batch)
        return int(row[0])
