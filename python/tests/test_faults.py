"""What a network loses, against real stores: the reply, the request, or the connection in the
middle of a call.

Conformance vectors simulate a store fault at the limiter. These go through each real client,
where a lost reply and a command sent again are things that happen. Each client is told apart
by what it does when the connection dies after the store ran the call: `counts_after_cut`.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
import redis
import redis.asyncio
import valkey
from fault_proxy import FaultProxy, until
from psycopg_pool import ConnectionPool

from omnilimit import AsyncLimiter, Limiter
from omnilimit.stores.postgres import MIGRATION_SQL, PostgresStore
from omnilimit.stores.redis import AsyncRedisStore, RedisStore

REDIS_URLS = [u for u in os.environ.get("OMNILIMIT_TEST_REDIS_URLS", "").split(",") if u]
POSTGRES_URLS = [
    u.replace("postgres://", "postgresql://", 1)
    for u in os.environ.get("OMNILIMIT_TEST_POSTGRES_URLS", "").split(",")
    if u
]

if os.environ.get("OMNILIMIT_REQUIRE_STORES") and not (REDIS_URLS and POSTGRES_URLS):
    raise RuntimeError("OMNILIMIT_REQUIRE_STORES is set, but store URLs are missing")

BURST = 10
TIMEOUT_MS = 400


@dataclass
class Rig:
    proxy: FaultProxy
    faulty: Any
    direct: Any
    #: Whether a call the store ran, on a connection that then died, stays counted. A Redis
    #: script is done once it ran. psycopg commits after the reply, so the hit is rolled back.
    counts_after_cut: bool

    def pair(self, on_store_error: str = "closed") -> tuple[Limiter, Callable[[], int]]:
        options: dict[str, Any] = {
            "limit": 1,
            "window_ms": 60_000,
            "burst": BURST,
            "scope": "faults",
            "salt": str(uuid.uuid4()),
            "on_store_error": on_store_error,
            "store_timeout_ms": TIMEOUT_MS,
        }
        through = Limiter(store=self.faulty, **options)
        straight = Limiter(store=self.direct, **options)

        def used() -> int:
            remaining = straight.peek("a").remaining
            assert remaining is not None
            return BURST - remaining

        # Before any fault: the connection is open and every script is loaded. Otherwise the
        # first reply to be lost is a handshake or a NOSCRIPT error, not the call under test.
        through.check("warm")
        through.peek("warm")
        through.refund(through.reserve("warm").reservation)
        return through, used


def _redis_rig(url: str, module: Any) -> Iterator[Rig]:
    proxy = FaultProxy(url)
    faulty, direct = module.Redis.from_url(proxy.url), module.Redis.from_url(url)
    yield Rig(proxy, RedisStore(faulty), RedisStore(direct), counts_after_cut=True)
    faulty.close()
    direct.close()
    proxy.close()


def _postgres_rig(url: str) -> Iterator[Rig]:
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(MIGRATION_SQL)
    proxy = FaultProxy(url)
    with (
        ConnectionPool(proxy.url, min_size=1, max_size=1, open=True, timeout=5) as faulty,
        ConnectionPool(url, min_size=1, max_size=2, open=True, timeout=5) as direct,
    ):
        yield Rig(proxy, PostgresStore(faulty), PostgresStore(direct), counts_after_cut=False)
        proxy.close()


RIGS = [
    *(
        pytest.param(("redis", url, module), id=f"{module.__name__}-{url}")
        for url in REDIS_URLS
        for module in (redis, valkey)
    ),
    *(pytest.param(("postgres", url, None), id=f"psycopg-{url}") for url in POSTGRES_URLS),
]


@pytest.fixture(params=RIGS)
def rig(request: pytest.FixtureRequest) -> Iterator[Rig]:
    kind, url, module = request.param
    yield from (_redis_rig(url, module) if kind == "redis" else _postgres_rig(url))


def recovered(limiter: Limiter) -> None:
    """The client is usable again: it reconnected, or its held replies arrived."""
    until(lambda: limiter.peek("a").reason != "store_error", "the client to recover")


def in_thread(work: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    out: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: out.update(result=work()), daemon=True)
    thread.start()
    return thread, out


def test_a_reply_that_never_comes_is_a_store_failure_at_the_timeout(rig: Rig) -> None:
    through, used = rig.pair()
    assert through.check("a").allowed
    rig.proxy.hold_replies()
    started = time.monotonic()
    decision = through.check("a")
    elapsed_ms = (time.monotonic() - started) * 1000
    assert (decision.allowed, decision.reason, decision.status) == (False, "store_error", 503)
    assert TIMEOUT_MS - 20 <= elapsed_ms < TIMEOUT_MS + 1000
    rig.proxy.resume()
    recovered(through)
    # The limiter gave up waiting; the store did not undo anything. Counted once, not twice.
    assert used() == 2
    assert through.check("a").allowed
    assert used() == 3


def test_open_lets_the_request_through_without_headers(rig: Rig) -> None:
    through, used = rig.pair("open")
    rig.proxy.hold_replies()
    decision = through.check("a")
    assert (decision.allowed, decision.reason, decision.status) == (True, "store_error", None)
    assert decision.headers == {}
    rig.proxy.resume()
    recovered(through)
    assert used() == 1


def test_a_connection_cut_after_the_store_ran_the_hit_never_counts_it_twice(rig: Rig) -> None:
    through, used = rig.pair()
    rig.proxy.hold_replies()
    thread, out = in_thread(lambda: through.check("a"))
    until(lambda: rig.proxy.held_reply_bytes() > 0, "the store to answer")
    rig.proxy.cut()
    thread.join()
    assert (out["result"].allowed, out["result"].reason) == (False, "store_error")
    recovered(through)
    # A client that sent the command again on reconnecting would have counted two.
    counted = 1 if rig.counts_after_cut else 0
    assert used() == counted
    assert through.check("a").allowed
    assert used() == counted + 1


def test_a_refund_whose_reply_is_lost_reports_false_and_is_never_applied_twice(rig: Rig) -> None:
    through, used = rig.pair()
    through.check("a")
    through.check("a")
    reservation = through.reserve("a").reservation
    assert used() == 3
    rig.proxy.hold_replies()
    thread, out = in_thread(lambda: through.refund(reservation))
    until(lambda: rig.proxy.held_reply_bytes() > 0, "the store to answer")
    rig.proxy.cut()
    thread.join()
    assert out["result"] is False
    recovered(through)
    # Applied twice, it would give back a unit the reservation never held.
    after = 2 if rig.counts_after_cut else 3
    assert used() == after
    # And the limiter does not try again: the reservation is spent.
    assert through.refund(reservation) is False
    assert used() == after


def test_a_request_that_never_reaches_the_store_counts_nothing(rig: Rig) -> None:
    through, used = rig.pair()
    rig.proxy.hold_requests()
    decision = through.check("a")
    assert (decision.allowed, decision.reason, decision.status) == (False, "store_error", 503)
    assert rig.proxy.held_request_bytes() > 0
    rig.proxy.cut()
    recovered(through)
    assert used() == 0


def test_after_all_of_it_the_limit_is_still_exactly_the_burst(rig: Rig) -> None:
    through, used = rig.pair()
    rig.proxy.hold_replies()
    through.check("a")
    rig.proxy.resume()
    recovered(through)
    allowed = sum(through.check("a").allowed for _ in range(BURST))
    # One unit went to the call that timed out, so one of these is denied.
    assert allowed == BURST - 1
    assert used() == BURST


@pytest.mark.parametrize("url", REDIS_URLS)
def test_async_redis_under_the_same_faults(url: str) -> None:
    async def run() -> None:
        proxy = FaultProxy(url)
        faulty, direct = redis.asyncio.Redis.from_url(proxy.url), redis.asyncio.Redis.from_url(url)
        options: dict[str, Any] = {
            "limit": 1,
            "window_ms": 60_000,
            "burst": BURST,
            "scope": "faults",
            "salt": str(uuid.uuid4()),
            "on_store_error": "closed",
            "store_timeout_ms": TIMEOUT_MS,
        }
        through = AsyncLimiter(store=AsyncRedisStore(faulty), **options)
        straight = AsyncLimiter(store=AsyncRedisStore(direct), **options)

        async def used() -> int:
            remaining = (await straight.peek("a")).remaining
            assert remaining is not None
            return BURST - remaining

        async def wait_for(done: Callable[[], Any], what: str) -> None:
            deadline = time.monotonic() + 10
            while True:
                value = done()
                if asyncio.iscoroutine(value):
                    value = await value
                if value:
                    return
                assert time.monotonic() < deadline, f"timed out waiting for {what}"
                await asyncio.sleep(0.01)

        async def usable() -> bool:
            return (await through.peek("a")).reason != "store_error"

        try:
            await through.check("warm")
            await through.peek("warm")
            await through.refund((await through.reserve("warm")).reservation)
            # The reply never comes: a failure at the timeout, counted once.
            proxy.hold_replies()
            started = time.monotonic()
            decision = await through.check("a")
            assert (decision.allowed, decision.reason) == (False, "store_error")
            assert TIMEOUT_MS - 20 <= (time.monotonic() - started) * 1000 < TIMEOUT_MS + 1000
            proxy.resume()
            await wait_for(usable, "the client to recover")
            assert await used() == 1

            # The connection dies after the store ran the hit: still once.
            proxy.hold_replies()
            checking = asyncio.ensure_future(through.check("a"))
            await wait_for(lambda: proxy.held_reply_bytes() > 0, "the store to answer")
            proxy.cut()
            assert (await checking).reason == "store_error"
            await wait_for(usable, "the client to recover")
            assert await used() == 2

            # A refund whose reply is lost: false, applied once, and never tried again.
            reservation = (await through.reserve("a")).reservation
            assert await used() == 3
            proxy.hold_replies()
            refunding = asyncio.ensure_future(through.refund(reservation))
            await wait_for(lambda: proxy.held_reply_bytes() > 0, "the store to answer")
            proxy.cut()
            assert await refunding is False
            await wait_for(usable, "the client to recover")
            assert await used() == 2
            assert await through.refund(reservation) is False
            assert await used() == 2
        finally:
            await faulty.aclose()
            await direct.aclose()
            proxy.close()

    asyncio.run(run())
