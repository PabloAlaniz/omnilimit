"""Postgres stores on a SQLAlchemy engine, for applications that already have one.

They borrow connections from the engine's own pool, so there is no second pool to size, and
they work with whichever Postgres driver the engine uses: psycopg, psycopg2, pg8000 or asyncpg.
Apply `MIGRATION_SQL` first, as for the psycopg stores.

Every call runs in AUTOCOMMIT on its own connection. It never joins a session or a transaction
of yours: a hit inside your transaction would hold the bucket's row lock until you commit, and
a rollback would undo the count.
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import text

from omnilimit._types import HitParams, RawHit, RawRefund
from omnilimit.stores.postgres import MIGRATION_SQL, _parse, _parse_refund

__all__ = ["MIGRATION_SQL", "AsyncSqlAlchemyStore", "SqlAlchemyStore"]

_HIT = text(
    "SELECT allowed, new_level, new_ts FROM omnilimit_hit_v1("
    "CAST(:key AS text), CAST(:limit AS bigint), CAST(:window_ms AS bigint), "
    "CAST(:capacity AS bigint), CAST(:drain_ms AS bigint), CAST(:now_ms AS bigint))"
)
_PEEK = text(
    "SELECT allowed, new_level, new_ts FROM omnilimit_peek_v1("
    "CAST(:key AS text), CAST(:limit AS bigint), CAST(:window_ms AS bigint), "
    "CAST(:capacity AS bigint), CAST(:drain_ms AS bigint), CAST(:now_ms AS bigint))"
)
_REFUND = text(
    "SELECT new_level, new_ts FROM omnilimit_refund_v1("
    "CAST(:key AS text), CAST(:limit AS bigint), CAST(:window_ms AS bigint), "
    "CAST(:capacity AS bigint), CAST(:drain_ms AS bigint), CAST(:since AS bigint), "
    "CAST(:now_ms AS bigint))"
)
_RESET = text("DELETE FROM omnilimit_state WHERE key = :key")
_CLEANUP = text("SELECT omnilimit_cleanup_v1(CAST(:batch AS integer))")


def _binds(key: str, params: HitParams, now_ms: int | None) -> dict[str, Any]:
    return {
        "key": key,
        "limit": params.limit,
        "window_ms": params.window_ms,
        "capacity": params.capacity,
        "drain_ms": params.drain_ms,
        "now_ms": now_ms,
    }


def _autocommit(engine: Any) -> Any:
    options = getattr(engine, "execution_options", None)
    if not callable(options) or not hasattr(engine, "connect"):
        raise TypeError("omnilimit: pass a SQLAlchemy Engine or AsyncEngine, not a session")
    # A copy that shares the pool: the application's own engine keeps its isolation level.
    return options(isolation_level="AUTOCOMMIT")


class SqlAlchemyStore:
    """For a blocking `Engine`. Use it with `Limiter`."""

    tier: Literal["exact"] = "exact"

    def __init__(self, engine: Any) -> None:
        self._engine = _autocommit(engine)

    def _fetch(self, statement: Any, binds: dict[str, Any]) -> Any:
        with self._engine.connect() as connection:
            return connection.execute(statement, binds).fetchone()

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        return _parse(self._fetch(_HIT, _binds(key, params, now_ms)))

    def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        """What `hit` would answer now, without writing or locking."""
        return _parse(self._fetch(_PEEK, _binds(key, params, now_ms)), "omnilimit_peek_v1")

    def reset(self, key: str) -> None:
        with self._engine.connect() as connection:
            connection.execute(_RESET, {"key": key})

    def refund(
        self, key: str, params: HitParams, since: int, now_ms: int | None = None
    ) -> RawRefund:
        """Give back a hit counted at `since`, as far as it has not drained."""
        return _parse_refund(self._fetch(_REFUND, {**_binds(key, params, now_ms), "since": since}))

    def cleanup(self, batch: int = 1000) -> int:
        """Delete up to `batch` expired buckets and return how many. Schedule it yourself."""
        return int(self._fetch(_CLEANUP, {"batch": batch})[0])


class AsyncSqlAlchemyStore:
    """For an `AsyncEngine`. Use it with `AsyncLimiter`."""

    tier: Literal["exact"] = "exact"

    def __init__(self, engine: Any) -> None:
        self._engine = _autocommit(engine)

    async def _fetch(self, statement: Any, binds: dict[str, Any]) -> Any:
        async with self._engine.connect() as connection:
            return (await connection.execute(statement, binds)).fetchone()

    async def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        return _parse(await self._fetch(_HIT, _binds(key, params, now_ms)))

    async def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        """What `hit` would answer now, without writing or locking."""
        row = await self._fetch(_PEEK, _binds(key, params, now_ms))
        return _parse(row, "omnilimit_peek_v1")

    async def reset(self, key: str) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(_RESET, {"key": key})

    async def refund(
        self, key: str, params: HitParams, since: int, now_ms: int | None = None
    ) -> RawRefund:
        """Give back a hit counted at `since`, as far as it has not drained."""
        row = await self._fetch(_REFUND, {**_binds(key, params, now_ms), "since": since})
        return _parse_refund(row)

    async def cleanup(self, batch: int = 1000) -> int:
        """Delete up to `batch` expired buckets and return how many. Schedule it yourself."""
        row = await self._fetch(_CLEANUP, {"batch": batch})
        return int(row[0])
