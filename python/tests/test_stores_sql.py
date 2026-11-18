"""The SQLAlchemy and asyncpg stores against real Postgres, with every driver they claim.

Set OMNILIMIT_TEST_POSTGRES_URLS. These tests write random keys: only point them at throwaway
databases.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

import asyncpg
import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Session
from support import (
    run_concurrency,
    run_concurrency_async,
    run_sequence,
    run_sequence_async,
    vectors_of,
)
from test_stores import PARAMS, SMOKE, assert_real_clock_flow, suffix

from omnilimit import AsyncLimiter, Limiter
from omnilimit.stores.asyncpg import AsyncpgStore
from omnilimit.stores.postgres import MIGRATION_SQL
from omnilimit.stores.sqlalchemy import AsyncSqlAlchemyStore, SqlAlchemyStore

POSTGRES_URLS = [u for u in os.environ.get("OMNILIMIT_TEST_POSTGRES_URLS", "").split(",") if u]
SYNC_DRIVERS = ["psycopg", "psycopg2", "pg8000"]
ASYNC_DRIVERS = ["asyncpg", "psycopg"]

SEQUENCES = [v for v in vectors_of("sequence") if "exact" in v["tiers"]]
# Every driver runs the named vectors; the random ones add nothing per driver.
NAMED = [v for v in SEQUENCES if "random" not in v["id"]]
sequences = pytest.mark.parametrize("vector", NAMED, ids=lambda v: v["id"])
concurrency = pytest.mark.parametrize("vector", vectors_of("concurrency"), ids=lambda v: v["id"])


def _dialect(url: str, driver: str) -> str:
    return re.sub(r"^postgres(ql)?://", f"postgresql+{driver}://", url)


def _plain(url: str) -> str:
    return re.sub(r"^postgres(ql)?://", "postgresql://", url)


@pytest.fixture(params=POSTGRES_URLS)
def pg_url(request: pytest.FixtureRequest) -> str:
    url: str = request.param
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(MIGRATION_SQL)
    return url


@pytest.fixture(params=SYNC_DRIVERS)
def engine(request: pytest.FixtureRequest, pg_url: str) -> Any:
    built = create_engine(_dialect(pg_url, request.param), pool_size=20, max_overflow=0)
    yield built
    built.dispose()


@pytest.fixture(params=ASYNC_DRIVERS)
def async_url(request: pytest.FixtureRequest, pg_url: str) -> str:
    return _dialect(pg_url, request.param)


@sequences
def test_sqlalchemy_sequence(engine: Any, vector: dict[str, Any]) -> None:
    run_sequence(vector, SqlAlchemyStore(engine), suffix())


@concurrency
def test_sqlalchemy_concurrency(engine: Any, vector: dict[str, Any]) -> None:
    run_concurrency(vector, SqlAlchemyStore(engine), suffix())


def test_sqlalchemy_real_clock(engine: Any) -> None:
    limiter = Limiter(**SMOKE, store=SqlAlchemyStore(engine), scope="smoke", salt=suffix())
    assert_real_clock_flow(lambda: limiter.check("a"), time.sleep)


def test_sqlalchemy_peek_reset_and_cleanup(engine: Any) -> None:
    store = SqlAlchemyStore(engine)
    key = f"k{suffix()}"
    assert store.peek(key, PARAMS).allowed
    assert store.hit(key, PARAMS).allowed
    assert not store.peek(key, PARAMS).allowed
    assert not store.hit(key, PARAMS).allowed
    store.reset(key)
    assert store.hit(key, PARAMS).allowed
    expired = f"old{suffix()}"
    store.hit(expired, PARAMS, now_ms=1)
    assert store.cleanup(10_000) >= 1
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT key FROM omnilimit_state WHERE key IN (:a, :b)"), {"a": key, "b": expired}
        ).fetchall()
    assert [row[0] for row in rows] == [key]


def test_sqlalchemy_counts_outside_the_callers_transaction(engine: Any) -> None:
    """A hit commits on its own: a rollback of the application's work must not undo it."""
    store = SqlAlchemyStore(engine)
    key = f"k{suffix()}"
    with Session(engine) as session:
        session.execute(text("SELECT 1"))
        assert store.hit(key, PARAMS).allowed
        session.rollback()
    assert not store.hit(key, PARAMS).allowed
    # The application's engine keeps its own isolation level.
    with engine.connect() as connection:
        assert connection.get_isolation_level() != "AUTOCOMMIT"


def test_sqlalchemy_refuses_a_session(engine: Any) -> None:
    with Session(engine) as session, pytest.raises(TypeError, match="Engine"):
        SqlAlchemyStore(session)
    with pytest.raises(TypeError, match="Engine"):
        AsyncSqlAlchemyStore(object())


@sequences
def test_async_sqlalchemy_sequence(async_url: str, vector: dict[str, Any]) -> None:
    async def run() -> None:
        engine = create_async_engine(async_url, pool_size=20, max_overflow=0)
        try:
            await run_sequence_async(vector, AsyncSqlAlchemyStore(engine), suffix())
        finally:
            await engine.dispose()

    asyncio.run(run())


@concurrency
def test_async_sqlalchemy_concurrency(async_url: str, vector: dict[str, Any]) -> None:
    async def run() -> None:
        engine = create_async_engine(async_url, pool_size=20, max_overflow=0)
        try:
            await run_concurrency_async(vector, AsyncSqlAlchemyStore(engine), suffix())
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_async_sqlalchemy_real_clock_peek_reset_and_cleanup(async_url: str) -> None:
    async def run() -> None:
        engine = create_async_engine(async_url)
        try:
            store = AsyncSqlAlchemyStore(engine)
            limiter = AsyncLimiter(**SMOKE, store=store, scope="smoke", salt=suffix())
            assert (await limiter.check("a")).allowed
            assert (await limiter.peek("a")).allowed
            assert (await limiter.check("a")).allowed
            assert not (await limiter.peek("a")).allowed
            assert await limiter.reset("a")
            assert (await limiter.check("a")).allowed
            await store.hit(f"old{suffix()}", PARAMS, now_ms=1)
            assert await store.cleanup(10_000) >= 1
        finally:
            await engine.dispose()

    asyncio.run(run())


@sequences
def test_asyncpg_sequence(pg_url: str, vector: dict[str, Any]) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(_plain(pg_url), min_size=1, max_size=20)
        try:
            await run_sequence_async(vector, AsyncpgStore(pool), suffix())
        finally:
            await pool.close()

    asyncio.run(run())


@concurrency
def test_asyncpg_concurrency(pg_url: str, vector: dict[str, Any]) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(_plain(pg_url), min_size=1, max_size=20)
        try:
            await run_concurrency_async(vector, AsyncpgStore(pool), suffix())
        finally:
            await pool.close()

    asyncio.run(run())


def test_asyncpg_real_clock_peek_reset_and_cleanup(pg_url: str) -> None:
    async def run() -> None:
        pool = await asyncpg.create_pool(_plain(pg_url), min_size=1, max_size=5)
        try:
            store = AsyncpgStore(pool)
            limiter = AsyncLimiter(**SMOKE, store=store, scope="smoke", salt=suffix())
            assert (await limiter.check("a")).allowed
            assert (await limiter.check("a")).allowed
            denied = await limiter.check("a")
            assert denied.reason == "limited"
            assert not (await limiter.peek("a")).allowed
            assert await limiter.reset("a")
            assert (await limiter.peek("a")).allowed
            await store.hit(f"old{suffix()}", PARAMS, now_ms=1)
            assert await store.cleanup(10_000) >= 1
        finally:
            await pool.close()

    asyncio.run(run())


def test_asyncpg_refuses_what_is_not_a_pool() -> None:
    with pytest.raises(TypeError, match="asyncpg pool"):
        AsyncpgStore(object())
