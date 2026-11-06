"""Vectors against real Redis, Valkey and Postgres.

Set OMNILIMIT_TEST_REDIS_URLS and OMNILIMIT_TEST_POSTGRES_URLS. These tests write random keys and
truncate `omnilimit_state`: only point them at throwaway databases.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
import redis
import redis.asyncio
import valkey
import valkey.asyncio
from psycopg_pool import AsyncConnectionPool, ConnectionPool
from support import (
    run_concurrency,
    run_concurrency_async,
    run_sequence,
    run_sequence_async,
    vectors_of,
)

from omnilimit import AsyncLimiter, HitParams, Limiter
from omnilimit._generated import REDIS_HIT_LUA, REDIS_HIT_SHA1
from omnilimit.stores.postgres import MIGRATION_SQL, AsyncPostgresStore, PostgresStore
from omnilimit.stores.redis import AsyncRedisStore, RedisStore

REDIS_URLS = [u for u in os.environ.get("OMNILIMIT_TEST_REDIS_URLS", "").split(",") if u]
POSTGRES_URLS = [u for u in os.environ.get("OMNILIMIT_TEST_POSTGRES_URLS", "").split(",") if u]

if os.environ.get("OMNILIMIT_REQUIRE_STORES") and not (REDIS_URLS and POSTGRES_URLS):
    raise RuntimeError("OMNILIMIT_REQUIRE_STORES is set, but store URLs are missing")

SEQUENCES = [v for v in vectors_of("sequence") if "exact" in v["tiers"]]
CONCURRENCY = vectors_of("concurrency")
sequences = pytest.mark.parametrize("vector", SEQUENCES, ids=lambda v: v["id"])
concurrency = pytest.mark.parametrize("vector", CONCURRENCY, ids=lambda v: v["id"])
PARAMS = HitParams(limit=1, window_ms=5000, capacity=5000, drain_ms=5000)
SMOKE: dict[str, Any] = {"limit": 2, "window_ms": 1000, "burst": 2, "on_store_error": "closed"}


def suffix() -> str:
    return f":{uuid.uuid4()}"


def assert_real_clock_flow(check: Callable[[], Any], sleep: Callable[[float], Any]) -> None:
    assert check().allowed
    assert check().allowed
    denied = check()
    assert denied.reason == "limited"
    assert 0 < denied.retry_after_ms <= 500
    sleep((denied.retry_after_ms + 20) / 1000)
    assert check().allowed


# One entry per client library. Valkey's Python client mirrors redis-py, including its errors.
REDIS_CLIENTS = [
    pytest.param((url, module, decode), id=f"{module.__name__}-decode={decode}-{url}")
    for url in REDIS_URLS
    for module in (redis, valkey)
    for decode in (False, True)
]


@pytest.fixture(params=REDIS_CLIENTS)
def sync_redis(request: pytest.FixtureRequest) -> Iterator[Any]:
    url, module, decode = request.param
    client = module.Redis.from_url(url, decode_responses=decode, max_connections=64)
    yield client
    client.close()


@pytest.fixture(params=REDIS_CLIENTS)
def async_redis_factory(request: pytest.FixtureRequest) -> Callable[[], Any]:
    url, module, decode = request.param

    # An asyncio client is tied to its event loop, and each test runs its own.
    # The default pool raises past 100 connections; a blocking one waits, as the docs advise.
    def make() -> Any:
        pool = module.asyncio.BlockingConnectionPool.from_url(
            url, decode_responses=decode, max_connections=50, timeout=30
        )
        return module.asyncio.Redis.from_pool(pool)

    return make


@sequences
def test_redis_sequence(sync_redis: Any, vector: dict[str, Any]) -> None:
    run_sequence(vector, RedisStore(sync_redis), suffix())


@concurrency
def test_redis_concurrency(sync_redis: Any, vector: dict[str, Any]) -> None:
    run_concurrency(vector, RedisStore(sync_redis), suffix())


def test_redis_real_clock(sync_redis: Any) -> None:
    limiter = Limiter(**SMOKE, store=RedisStore(sync_redis), scope="smoke", salt=suffix())
    assert_real_clock_flow(lambda: limiter.check("a"), time.sleep)


def test_redis_loads_the_script_on_noscript(sync_redis: Any) -> None:
    sync_redis.script_flush()
    RedisStore(sync_redis).hit(f"ol:test:{uuid.uuid4()}", PARAMS)
    assert sync_redis.script_exists(REDIS_HIT_SHA1) == [True]
    loaded = sync_redis.script_load(REDIS_HIT_LUA)
    assert (loaded.decode() if isinstance(loaded, bytes) else loaded) == REDIS_HIT_SHA1


def test_redis_expires_the_bucket_after_draining(sync_redis: Any) -> None:
    key = f"ol:test:{uuid.uuid4()}"
    RedisStore(sync_redis).hit(key, PARAMS)
    assert 0 < sync_redis.pttl(key) <= 5000


def test_redis_does_not_write_on_denial(sync_redis: Any) -> None:
    key = f"ol:test:{uuid.uuid4()}"
    store = RedisStore(sync_redis)
    store.hit(key, PARAMS, 1_000_000)
    before = sync_redis.hgetall(key)
    assert not store.hit(key, PARAMS, 1_001_000).allowed
    assert sync_redis.hgetall(key) == before


def test_redis_other_errors_are_not_retried_as_noscript() -> None:
    class Broken:
        def evalsha(self, *args: Any) -> Any:
            raise ConnectionError("down")

        def eval(self, *args: Any) -> Any:
            raise AssertionError("must not fall back")

    with pytest.raises(ConnectionError):
        RedisStore(Broken()).hit("k", PARAMS)


@pytest.mark.parametrize("reply", [None, "x", b"xyz", [1, 2], [1, 2, 3, 4]])
def test_redis_unexpected_reply(reply: Any) -> None:
    class Odd:
        def evalsha(self, *args: Any) -> Any:
            return reply

    with pytest.raises(ValueError, match="unexpected reply"):
        RedisStore(Odd()).hit("k", PARAMS)


def test_async_redis_other_errors_are_not_retried_as_noscript() -> None:
    class Broken:
        async def evalsha(self, *args: Any) -> Any:
            raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        asyncio.run(AsyncRedisStore(Broken()).hit("k", PARAMS))


@sequences
def test_async_redis_sequence(async_redis_factory: Any, vector: dict[str, Any]) -> None:
    async def run() -> None:
        client = async_redis_factory()
        try:
            await run_sequence_async(vector, AsyncRedisStore(client), suffix())
        finally:
            await client.aclose()

    asyncio.run(run())


@concurrency
def test_async_redis_concurrency(async_redis_factory: Any, vector: dict[str, Any]) -> None:
    async def run() -> None:
        client = async_redis_factory()
        try:
            await run_concurrency_async(vector, AsyncRedisStore(client), suffix())
        finally:
            await client.aclose()

    asyncio.run(run())


def test_async_redis_real_clock_and_noscript(async_redis_factory: Any) -> None:
    async def run() -> None:
        client = async_redis_factory()
        try:
            await client.script_flush()
            limiter = AsyncLimiter(
                **SMOKE, store=AsyncRedisStore(client), scope="smoke", salt=suffix()
            )
            assert (await limiter.check("a")).allowed
            assert (await limiter.check("a")).allowed
            denied = await limiter.check("a")
            assert denied.reason == "limited"
            assert denied.retry_after_ms is not None
            await asyncio.sleep((denied.retry_after_ms + 20) / 1000)
            assert (await limiter.check("a")).allowed
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.fixture(params=POSTGRES_URLS)
def pg_url(request: pytest.FixtureRequest) -> str:
    url: str = request.param
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(MIGRATION_SQL)
        connection.execute(MIGRATION_SQL)  # must stay idempotent
    return url


@pytest.fixture
def pg_pool(pg_url: str) -> Iterator[ConnectionPool]:
    with ConnectionPool(pg_url, min_size=1, max_size=20, open=True, timeout=30) as pool:
        yield pool


@sequences
def test_postgres_sequence(pg_pool: ConnectionPool, vector: dict[str, Any]) -> None:
    run_sequence(vector, PostgresStore(pg_pool), suffix())


@concurrency
def test_postgres_concurrency(pg_pool: ConnectionPool, vector: dict[str, Any]) -> None:
    run_concurrency(vector, PostgresStore(pg_pool), suffix())


def test_postgres_real_clock(pg_pool: ConnectionPool) -> None:
    limiter = Limiter(**SMOKE, store=PostgresStore(pg_pool), scope="smoke", salt=suffix())
    assert_real_clock_flow(lambda: limiter.check("a"), time.sleep)


def test_postgres_accepts_an_autocommit_connection(pg_url: str) -> None:
    with psycopg.connect(pg_url, autocommit=True) as connection:
        store = PostgresStore(connection)
        key = f"ol:test:{uuid.uuid4()}"
        assert store.hit(key, PARAMS, 1_000_000).allowed
        assert not store.hit(key, PARAMS, 1_001_000).allowed
        # Nothing is left waiting on a transaction.
        assert connection.info.transaction_status.name == "IDLE"


def test_postgres_refuses_a_connection_that_would_keep_the_lock(pg_url: str) -> None:
    with (
        psycopg.connect(pg_url) as connection,
        pytest.raises(ValueError, match="autocommit"),
    ):
        PostgresStore(connection)


def test_postgres_cleanup_removes_only_expired(pg_pool: ConnectionPool) -> None:
    store = PostgresStore(pg_pool)
    with pg_pool.connection() as connection:
        connection.execute("TRUNCATE omnilimit_state")
    fast = HitParams(limit=1, window_ms=1000, capacity=1000, drain_ms=1000)
    store.hit("old", fast, 1_000_000)
    store.hit("live", fast)
    assert store.cleanup() == 1
    with pg_pool.connection() as connection:
        keys = connection.execute("SELECT key FROM omnilimit_state").fetchall()
    assert keys == [("live",)]


def test_postgres_does_not_write_on_denial(pg_pool: ConnectionPool) -> None:
    store = PostgresStore(pg_pool)
    key = f"ol:test:{uuid.uuid4()}"
    sql = "SELECT level, ts_ms, expires_at_ms FROM omnilimit_state WHERE key = %s"
    store.hit(key, PARAMS, 1_000_000)
    with pg_pool.connection() as connection:
        before = connection.execute(sql, (key,)).fetchall()
    assert not store.hit(key, PARAMS, 1_001_000).allowed
    with pg_pool.connection() as connection:
        assert connection.execute(sql, (key,)).fetchall() == before


def test_postgres_no_rows_is_an_error() -> None:
    class Cursor:
        def fetchone(self) -> None:
            return None

    class Db:
        autocommit = True

        def execute(self, *args: Any) -> Cursor:
            return Cursor()

    with pytest.raises(ValueError, match="no rows"):
        PostgresStore(Db()).hit("k", PARAMS)


async def _async_pool(url: str) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(url, min_size=1, max_size=20, open=False, timeout=30)
    await pool.open()
    return pool


@sequences
def test_async_postgres_sequence(pg_url: str, vector: dict[str, Any]) -> None:
    async def run() -> None:
        pool = await _async_pool(pg_url)
        try:
            await run_sequence_async(vector, AsyncPostgresStore(pool), suffix())
        finally:
            await pool.close()

    asyncio.run(run())


@concurrency
def test_async_postgres_concurrency(pg_url: str, vector: dict[str, Any]) -> None:
    async def run() -> None:
        pool = await _async_pool(pg_url)
        try:
            await run_concurrency_async(vector, AsyncPostgresStore(pool), suffix())
        finally:
            await pool.close()

    asyncio.run(run())


def test_async_postgres_connection_and_cleanup(pg_url: str) -> None:
    async def run() -> None:
        connection = await psycopg.AsyncConnection.connect(pg_url, autocommit=True)
        try:
            store = AsyncPostgresStore(connection)
            key = f"ol:test:{uuid.uuid4()}"
            assert (await store.hit(key, PARAMS, 1_000_000)).allowed
            assert not (await store.hit(key, PARAMS, 1_001_000)).allowed
            assert await store.cleanup() >= 1
        finally:
            await connection.close()

    asyncio.run(run())
