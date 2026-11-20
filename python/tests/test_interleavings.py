"""Interleavings that a vector cannot force: a store that holds one call until the test lets it
go, so another request runs in the gap. See "What recipes do not promise" in the spec.

Blocking code runs each request in its own thread, async code in `asyncio.gather`.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from omnilimit import (
    AsyncLimiter,
    AsyncLockout,
    HitParams,
    Limiter,
    Lockout,
    LockoutLimit,
    MemoryStore,
    RawHit,
    RawRefund,
    SettledError,
    check_all,
    check_all_async,
    peek_all,
    peek_all_async,
    reserve_all,
    reserve_all_async,
)

NOW = 1_700_000_000_000
WAIT = 10


def scope_of(key: str) -> str:
    return key.split(":")[2]


class Barrier:
    """A memory store on a fixed clock, so a refund gives back all it took, that logs every call
    and can hold one. `pause_after(op, scope)` lets the next such call reach the store, then holds
    its answer until `release` is set; `reached` is set once it is held."""

    tier = "exact"

    def __init__(self) -> None:
        self.inner = MemoryStore(now=lambda: NOW)
        self.calls: list[str] = []
        self.trap: tuple[str, str, Any, Any] | None = None

    def caught(self, op: str, key: str) -> tuple[Any, Any] | None:
        self.calls.append(f"{op} {scope_of(key)}")
        trap = self.trap
        if trap is not None and trap[:2] == (op, scope_of(key)):
            self.trap = None
            return trap[2], trap[3]
        return None


class SyncBarrier(Barrier):
    def pause_after(self, op: str, scope: str) -> tuple[threading.Event, threading.Event]:
        reached, release = threading.Event(), threading.Event()
        self.trap = (op, scope, reached, release)
        return reached, release

    def _after(self, op: str, key: str) -> None:
        caught = self.caught(op, key)
        if caught is not None:
            caught[0].set()
            assert caught[1].wait(WAIT)

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        result = self.inner.hit(key, params)
        self._after("hit", key)
        return result

    def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        result = self.inner.peek(key, params)
        self._after("peek", key)
        return result

    def reset(self, key: str) -> None:
        self.inner.reset(key)
        self._after("reset", key)

    def refund(self, key: str, params: HitParams, since: int, now_ms: Any = None) -> RawRefund:
        result = self.inner.refund(key, params, since)
        self._after("refund", key)
        return result


class AsyncBarrier(Barrier):
    def pause_after(self, op: str, scope: str) -> tuple[asyncio.Event, asyncio.Event]:
        reached, release = asyncio.Event(), asyncio.Event()
        self.trap = (op, scope, reached, release)
        return reached, release

    async def _after(self, op: str, key: str) -> None:
        caught = self.caught(op, key)
        if caught is not None:
            caught[0].set()
            await asyncio.wait_for(caught[1].wait(), WAIT)

    async def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        result = self.inner.hit(key, params)
        await self._after("hit", key)
        return result

    async def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        result = self.inner.peek(key, params)
        await self._after("peek", key)
        return result

    async def reset(self, key: str) -> None:
        self.inner.reset(key)
        await self._after("reset", key)

    async def refund(
        self, key: str, params: HitParams, since: int, now_ms: Any = None
    ) -> RawRefund:
        result = self.inner.refund(key, params, since)
        await self._after("refund", key)
        return result


def limiter(store: Any, scope: str, burst: int) -> Limiter:
    return Limiter(
        limit=1, window_ms=60_000, burst=burst, store=store, scope=scope,
        on_store_error="closed", store_timeout_ms=WAIT * 1000,
    )  # fmt: skip


def alimiter(store: Any, scope: str, burst: int) -> AsyncLimiter:
    return AsyncLimiter(
        limit=1, window_ms=60_000, burst=burst, store=store, scope=scope,
        on_store_error="closed", store_timeout_ms=WAIT * 1000,
    )  # fmt: skip


def used(limiter: Limiter, id_value: str, burst: int) -> int:
    remaining = limiter.peek(id_value).remaining
    assert remaining is not None
    return burst - remaining


async def aused(limiter: AsyncLimiter, id_value: str, burst: int) -> int:
    remaining = (await limiter.peek(id_value)).remaining
    assert remaining is not None
    return burst - remaining


def in_thread(pool: ThreadPoolExecutor, work: Callable[[], Any]) -> Any:
    return pool.submit(work)


# Peek mode: commit counts the first limit, then is denied by the second, which another request
# took while it was held.


def test_commit_is_denied_by_a_limit_taken_in_the_gap() -> None:
    store = SyncBarrier()
    a, b = limiter(store, "a", 3), limiter(store, "b", 1)
    pending = peek_all([(a, "x"), (b, "y")])
    assert pending.allowed
    reached, release = store.pause_after("hit", "a")
    with ThreadPoolExecutor(max_workers=1) as pool:
        committing = in_thread(pool, pending.commit)
        assert reached.wait(WAIT)
        assert check_all([(b, "y")]).allowed
        release.set()
        result = committing.result(WAIT)
    assert not result.allowed
    assert [d.allowed for d in result.decisions] == [True, False]
    assert result.decision is result.decisions[1]
    # The limit counted before the denial stays counted.
    assert used(a, "x", 3) == 1
    assert used(b, "y", 1) == 1


def test_commit_is_denied_by_a_limit_taken_in_the_gap_async() -> None:
    async def main() -> None:
        store = AsyncBarrier()
        a, b = alimiter(store, "a", 3), alimiter(store, "b", 1)
        pending = await peek_all_async([(a, "x"), (b, "y")])
        reached, release = store.pause_after("hit", "a")
        committing = asyncio.ensure_future(pending.commit())
        await reached.wait()
        assert (await check_all_async([(b, "y")])).allowed
        release.set()
        result = await committing
        assert [d.allowed for d in result.decisions] == [True, False]
        assert not result.allowed and result.decision is result.decisions[1]
        assert await aused(a, "x", 3) == 1

    asyncio.run(main())


# An advisory lockout can run more verifications than its burst: here every attempt passes its
# peek before any failure is counted, because no hit goes through until all peeks have.


class _PeeksFirst:
    tier = "exact"

    def __init__(self, attempts: int, event: Any) -> None:
        self.inner = MemoryStore(now=lambda: NOW)
        self.attempts, self.peeks, self.all_peeked = attempts, 0, event
        self.lock = threading.Lock()

    def peeked(self) -> None:
        with self.lock:
            self.peeks += 1
            if self.peeks == self.attempts:
                self.all_peeked.set()


def test_advisory_lockout_verifies_more_than_its_burst() -> None:
    class Store(_PeeksFirst):
        def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            result = self.inner.peek(key, params)
            self.peeked()
            return result

        def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            assert self.all_peeked.wait(WAIT)
            return self.inner.hit(key, params)

    attempts = 5
    lockout = Lockout(limits=[LockoutLimit(limiter(Store(attempts, threading.Event()), "l", 2))])
    verifications: list[bool] = []

    def verify() -> bool:
        verifications.append(True)
        return False

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        results = list(pool.map(lambda _: lockout.attempt("u", verify), range(attempts)))
    assert len(verifications) == attempts > 2
    # The failures were still counted one by one: only the burst of them was allowed.
    assert sum(r.decision.allowed for r in results) == 2


def test_advisory_lockout_verifies_more_than_its_burst_async() -> None:
    async def main() -> None:
        class Store(_PeeksFirst):
            async def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
                result = self.inner.peek(key, params)
                self.peeked()
                return result

            async def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
                await self.all_peeked.wait()
                return self.inner.hit(key, params)

        attempts = 5
        store = Store(attempts, asyncio.Event())
        lockout = AsyncLockout(limits=[LockoutLimit(alimiter(store, "l", 2))])
        verifications: list[bool] = []

        async def verify() -> bool:
            verifications.append(True)
            return False

        results = await asyncio.gather(*(lockout.attempt("u", verify) for _ in range(attempts)))
        assert len(verifications) == attempts > 2
        assert sum(r.decision.allowed for r in results) == 2

    asyncio.run(main())


# Reserve mode: when a later entry denies, the earlier ones stay counted until their refunds land.

COMPENSATED = ["hit a", "hit b", "peek a", "hit a", "refund a", "peek a"]


def test_compensation_over_counts_until_its_refund_lands() -> None:
    store = SyncBarrier()
    a, b = limiter(store, "a", 1), limiter(store, "b", 1)
    assert b.check("y").allowed
    store.calls.clear()
    reached, release = store.pause_after("hit", "b")
    with ThreadPoolExecutor(max_workers=1) as pool:
        opening = in_thread(pool, lambda: reserve_all([(a, "x"), (b, "y")]))
        assert reached.wait(WAIT)
        assert used(a, "x", 1) == 1
        # A request in the gap is denied by the reservation about to be given back.
        assert not a.check("x").allowed
        release.set()
        pending = opening.result(WAIT)
    assert not pending.allowed
    assert pending.refunded == (True, None)
    assert used(a, "x", 1) == 0
    assert store.calls == COMPENSATED
    assert a.check("x").allowed


def test_compensation_over_counts_until_its_refund_lands_async() -> None:
    async def main() -> None:
        store = AsyncBarrier()
        a, b = alimiter(store, "a", 1), alimiter(store, "b", 1)
        assert (await b.check("y")).allowed
        store.calls.clear()
        reached, release = store.pause_after("hit", "b")
        opening = asyncio.ensure_future(reserve_all_async([(a, "x"), (b, "y")]))
        await reached.wait()
        assert await aused(a, "x", 1) == 1
        assert not (await a.check("x")).allowed
        release.set()
        pending = await opening
        assert pending.refunded == (True, None)
        assert await aused(a, "x", 1) == 0
        assert store.calls == COMPENSATED

    asyncio.run(main())


# Settlement: one set of store calls however many callers, and a crossed one is refused.


def test_concurrent_refunds_of_one_reservation_reach_the_store_once() -> None:
    store = SyncBarrier()
    a = limiter(store, "a", 3)
    reservation = a.reserve("x").reservation
    a.reserve("x")
    store.calls.clear()
    reached, release = store.pause_after("refund", "a")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = in_thread(pool, lambda: a.refund(reservation))
        assert reached.wait(WAIT)
        second = in_thread(pool, lambda: a.refund(reservation))
        assert second.result(WAIT) is False
        release.set()
        assert first.result(WAIT) is True
    assert store.calls == ["refund a"]
    assert used(a, "x", 3) == 1


def test_concurrent_refunds_of_one_reservation_reach_the_store_once_async() -> None:
    async def main() -> None:
        store = AsyncBarrier()
        a = alimiter(store, "a", 3)
        reservation = (await a.reserve("x")).reservation
        await a.reserve("x")
        store.calls.clear()
        reached, release = store.pause_after("refund", "a")
        first = asyncio.ensure_future(a.refund(reservation))
        await reached.wait()
        assert await a.refund(reservation) is False
        release.set()
        assert await first is True
        assert store.calls == ["refund a"]
        assert await aused(a, "x", 3) == 1

    asyncio.run(main())


def test_concurrent_commits_share_one_set_of_calls() -> None:
    store = SyncBarrier()
    a, b = limiter(store, "a", 3), limiter(store, "b", 3)
    pending = peek_all([(a, "x"), (b, "y")])
    store.calls.clear()
    reached, release = store.pause_after("hit", "a")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = in_thread(pool, pending.commit)
        assert reached.wait(WAIT)
        second = in_thread(pool, pending.commit)
        release.set()
        one, two = first.result(WAIT), second.result(WAIT)
    assert one is two
    assert store.calls == ["hit a", "hit b"]
    assert used(a, "x", 3) == 1


def test_concurrent_cancels_refund_once() -> None:
    store = SyncBarrier()
    a, b = limiter(store, "a", 3), limiter(store, "b", 3)
    pending = reserve_all([(a, "x"), (b, "y")])
    store.calls.clear()
    reached, release = store.pause_after("refund", "a")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = in_thread(pool, pending.cancel)
        assert reached.wait(WAIT)
        second = in_thread(pool, pending.cancel)
        release.set()
        one, two = first.result(WAIT), second.result(WAIT)
    assert one is two and one.refunded == (True, True)
    assert store.calls == ["refund a", "refund b"]
    assert used(a, "x", 3) == 0


def test_concurrent_failures_count_once() -> None:
    store = SyncBarrier()
    a = limiter(store, "a", 3)
    gate = Lockout(limits=[LockoutLimit(a)]).begin("u")
    store.calls.clear()
    reached, release = store.pause_after("hit", "a")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = in_thread(pool, gate.fail)
        assert reached.wait(WAIT)
        second = in_thread(pool, gate.fail)
        release.set()
        assert first.result(WAIT) is second.result(WAIT)
    assert store.calls == ["hit a"]
    assert used(a, "u", 3) == 1


def test_crossed_settlement_is_refused_without_a_store_call() -> None:
    store = SyncBarrier()
    a = limiter(store, "a", 3)
    pending = peek_all([(a, "x")])
    store.calls.clear()
    reached, release = store.pause_after("hit", "a")
    with ThreadPoolExecutor(max_workers=2) as pool:
        committing = in_thread(pool, pending.commit)
        assert reached.wait(WAIT)
        # It waits for the commit in flight, and then is refused.
        cancelling = in_thread(pool, pending.cancel)
        release.set()
        assert committing.result(WAIT).allowed
        with pytest.raises(SettledError):
            cancelling.result(WAIT)
    assert store.calls == ["hit a"]


def test_concurrent_settlements_async() -> None:
    async def main() -> None:
        store = AsyncBarrier()
        a, b = alimiter(store, "a", 3), alimiter(store, "b", 3)
        pending = await peek_all_async([(a, "x"), (b, "y")])
        store.calls.clear()
        reached, release = store.pause_after("hit", "a")
        both = asyncio.gather(pending.commit(), pending.commit())
        await reached.wait()
        # Settled before its first store call: the cancel is refused while the commit runs.
        with pytest.raises(SettledError):
            await pending.cancel()
        release.set()
        one, two = await both
        assert one is two and store.calls == ["hit a", "hit b"]

        reserved = await reserve_all_async([(a, "x"), (b, "y")])
        store.calls.clear()
        reached, release = store.pause_after("refund", "a")
        cancels = asyncio.gather(reserved.cancel(), reserved.cancel())
        await reached.wait()
        release.set()
        first, second = await cancels
        assert first is second and first.refunded == (True, True)
        assert store.calls == ["refund a", "refund b"]

        gate = await AsyncLockout(limits=[LockoutLimit(a)], mode="reserve").begin("u")
        store.calls.clear()
        reached, release = store.pause_after("refund", "a")
        cancelling = asyncio.ensure_future(gate.cancel())
        await reached.wait()
        crossed = await asyncio.gather(gate.fail(), gate.succeed(), return_exceptions=True)
        assert all(isinstance(error, SettledError) for error in crossed)
        release.set()
        assert (await cancelling).refunded == (True,)
        assert store.calls == ["refund a"]

        failing = await AsyncLockout(limits=[LockoutLimit(a)]).begin("v")
        store.calls.clear()
        reached, release = store.pause_after("hit", "a")
        fails = asyncio.gather(failing.fail(), failing.fail())
        await reached.wait()
        release.set()
        one_fail, two_fail = await fails
        assert one_fail is two_fail and store.calls == ["hit a"]

    asyncio.run(main())
