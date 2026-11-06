"""Postgres stores: atomic and shared, using a row lock per bucket and the database clock.

They are good when Postgres is already there. Each hit is a write, so they are not meant for
high traffic. They take a psycopg 3 pool, or a connection with `autocommit=True`: anything else
would hold the bucket's row lock until the surrounding transaction ends.
"""

from __future__ import annotations

from typing import Any, Literal

from omnilimit._generated import POSTGRES_MIGRATION_SQL
from omnilimit._types import HitParams, RawHit

MIGRATION_SQL = POSTGRES_MIGRATION_SQL
"""Creates the table and functions. Apply it with your migration tool; it never runs on its own."""

_HIT_SQL = (
    "SELECT allowed, new_level, new_ts FROM omnilimit_hit_v1("
    "%s::text, %s::bigint, %s::bigint, %s::bigint, %s::bigint, %s::bigint)"
)
_CLEANUP_SQL = "SELECT omnilimit_cleanup_v1(%s::integer)"


def _is_pool(db: Any) -> bool:
    # A connection also has a `.connection` attribute, so that cannot tell them apart.
    return not hasattr(db, "execute")


def _check_db(db: Any) -> None:
    if not _is_pool(db) and getattr(db, "autocommit", False) is not True:
        raise ValueError("omnilimit: pass a psycopg pool, or a connection with autocommit=True")


def _hit_params(key: str, params: HitParams, now_ms: int | None) -> tuple[Any, ...]:
    return (key, params.limit, params.window_ms, params.capacity, params.drain_ms, now_ms)


def _parse(row: Any) -> RawHit:
    if row is None:
        raise ValueError("omnilimit: omnilimit_hit_v1 returned no rows")
    return RawHit(allowed=row[0] is True, level=int(row[1]), ts=int(row[2]))


class PostgresStore:
    """For blocking connections and pools. Use it with `Limiter`."""

    tier: Literal["exact"] = "exact"

    def __init__(self, db: Any) -> None:
        _check_db(db)
        self._db = db

    def _fetch(self, sql: str, params: tuple[Any, ...]) -> Any:
        if _is_pool(self._db):
            with self._db.connection() as connection:
                return connection.execute(sql, params).fetchone()
        return self._db.execute(sql, params).fetchone()

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        return _parse(self._fetch(_HIT_SQL, _hit_params(key, params, now_ms)))

    def cleanup(self, batch: int = 1000) -> int:
        """Delete up to `batch` expired buckets and return how many. Schedule it yourself."""
        return int(self._fetch(_CLEANUP_SQL, (batch,))[0])


class AsyncPostgresStore:
    """For async connections and pools. Use it with `AsyncLimiter`."""

    tier: Literal["exact"] = "exact"

    def __init__(self, db: Any) -> None:
        _check_db(db)
        self._db = db

    async def _fetch(self, sql: str, params: tuple[Any, ...]) -> Any:
        if _is_pool(self._db):
            async with self._db.connection() as connection:
                cursor = await connection.execute(sql, params)
                return await cursor.fetchone()
        cursor = await self._db.execute(sql, params)
        return await cursor.fetchone()

    async def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        return _parse(await self._fetch(_HIT_SQL, _hit_params(key, params, now_ms)))

    async def cleanup(self, batch: int = 1000) -> int:
        """Delete up to `batch` expired buckets and return how many. Schedule it yourself."""
        row = await self._fetch(_CLEANUP_SQL, (batch,))
        return int(row[0])
