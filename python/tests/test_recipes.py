"""Combined checks and lockouts: the "Recipes" section of the spec."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnilimit import (
    AsyncLimiter,
    AsyncLockout,
    HitParams,
    Limiter,
    Lockout,
    MemoryStore,
    RawHit,
    check_all,
    check_all_async,
)


class Clock:
    def __init__(self) -> None:
        self.t = 0

    def __call__(self) -> int:
        return self.t


def _pair(ip_limit: int, user_limit: int, kind: Any = Limiter) -> tuple[Any, Any, Clock]:
    clock = Clock()
    store = MemoryStore(now=clock)

    def make(scope: str, limit: int) -> Any:
        return kind(
            store=store, scope=scope, limit=limit, window_ms=60_000, on_store_error="closed"
        )

    return make("ip", ip_limit), make("user", user_limit), clock


def test_counts_every_limit_and_reports_the_fewest_left() -> None:
    ip, user, _ = _pair(10, 3)
    result = check_all([(ip, "a"), (user, "u")])
    assert result.allowed
    assert [d.remaining for d in result.decisions] == [9, 2]
    assert result.decision is result.decisions[1]
    assert '"user"' in result.decision.headers["ratelimit"]


def test_counts_nothing_when_one_would_deny() -> None:
    ip, user, _ = _pair(1, 5)
    ip.check("a")
    result = check_all([(user, "u"), (ip, "a")])
    assert not result.allowed
    assert result.decision.status == 429
    assert '"ip"' in result.decision.headers["ratelimit"]
    assert user.peek("u").remaining == 5


def test_answers_with_the_longest_wait() -> None:
    ip, user, clock = _pair(1, 1)
    ip.check("a")
    clock.t += 30_000
    user.check("u")
    result = check_all([(ip, "a"), (user, "u")])
    assert not result.allowed
    assert [d.retry_after_ms for d in result.decisions] == [30_000, 60_000]
    assert result.decision is result.decisions[1]


class Racing:
    """Peek always allows and the hit denies: another request took the last unit."""

    tier = "exact"

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        return RawHit(allowed=False, level=60_000, ts=0)

    def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        return RawHit(allowed=True, level=0, ts=0)

    def reset(self, key: str) -> None: ...


class Down:
    tier = "exact"

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        raise ConnectionError("down")

    def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        raise ConnectionError("down")

    def reset(self, key: str) -> None: ...


def test_stops_at_a_limit_that_denies_while_counting() -> None:
    ip, user, _ = _pair(5, 5)
    racing = Limiter(
        store=Racing(), scope="race", limit=1, window_ms=60_000, on_store_error="closed"
    )
    result = check_all([(ip, "a"), (racing, "r"), (user, "u")])
    assert not result.allowed
    assert len(result.decisions) == 2
    assert result.decision is result.decisions[1]
    assert ip.peek("a").remaining == 4
    assert user.peek("u").remaining == 5


def test_follows_on_store_error_and_needs_a_list() -> None:
    closed = Limiter(store=Down(), scope="c", limit=1, window_ms=1000, on_store_error="closed")
    refused = check_all([(closed, "x")])
    assert not refused.allowed
    assert refused.decision.status == 503
    opened = Limiter(store=Down(), scope="o", limit=1, window_ms=1000, on_store_error="open")
    ip, _, _ = _pair(2, 2)
    passed = check_all([(opened, "x"), (ip, "a")])
    assert passed.allowed
    assert passed.decision.remaining == 1
    assert check_all([(opened, "x")]).decision.remaining is None
    with pytest.raises(ValueError, match="at least one"):
        check_all([])


def test_check_all_async() -> None:
    async def run() -> None:
        ip, user, _ = _pair(1, 5, AsyncLimiter)
        first = await check_all_async([(ip, "a"), (user, "u")])
        assert first.allowed
        assert first.decision is first.decisions[0]
        second = await check_all_async([(user, "u"), (ip, "a")])
        assert not second.allowed
        assert (await user.peek("u")).remaining == 4
        racing = AsyncLimiter(
            store=MemoryStore(), scope="z", limit=1, window_ms=60_000, on_store_error="closed"
        )
        await racing.check("r")
        third = await check_all_async([(racing, "r")])
        assert not third.allowed
        with pytest.raises(ValueError, match="at least one"):
            await check_all_async([])

    asyncio.run(run())


def test_check_all_async_stops_at_a_racing_limit() -> None:
    class AsyncRacing:
        tier = "exact"

        async def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            return RawHit(allowed=False, level=60_000, ts=0)

        async def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            return RawHit(allowed=True, level=0, ts=0)

        async def reset(self, key: str) -> None: ...

    async def run() -> None:
        ip, user, _ = _pair(5, 5, AsyncLimiter)
        racing = AsyncLimiter(
            store=AsyncRacing(), scope="race", limit=1, window_ms=60_000, on_store_error="closed"
        )
        result = await check_all_async([(ip, "a"), (racing, "r"), (user, "u")])
        assert not result.allowed
        assert len(result.decisions) == 2
        assert (await user.peek("u")).remaining == 5

    asyncio.run(run())


def _lockout(**options: Any) -> tuple[Lockout, Limiter, Clock]:
    clock = Clock()
    limiter = Limiter(
        store=MemoryStore(now=clock),
        scope="failures",
        limit=2,
        window_ms=60_000,
        on_store_error="closed",
    )
    return Lockout(limiter, **options), limiter, clock


def test_lockout_counts_failures_only_and_then_refuses_even_a_correct_attempt() -> None:
    lockout, _, clock = _lockout()
    first = lockout.attempt("u", lambda: False)
    assert (first.allowed, first.verified, first.decision.remaining) == (True, False, 1)
    lockout.attempt("u", lambda: False)
    calls: list[int] = []

    def verify() -> bool:
        calls.append(1)
        return True

    locked = lockout.attempt("u", verify)
    assert (locked.allowed, locked.verified) == (False, None)
    assert locked.decision.status == 429
    assert locked.decision.retry_after_ms == 30_000
    assert calls == []
    clock.t += 30_000
    assert lockout.attempt("u", verify).verified is True
    assert calls == [1]


def test_lockout_success_clears_the_count_unless_told_to_keep_it() -> None:
    clearing, limiter, _ = _lockout()
    clearing.attempt("u", lambda: False)
    clearing.attempt("u", lambda: True)
    assert limiter.peek("u").remaining == 2
    keeping, kept, _ = _lockout(reset_on_success=False)
    keeping.attempt("u", lambda: False)
    keeping.attempt("u", lambda: True)
    assert kept.peek("u").remaining == 1


def test_lockout_counts_nothing_when_verify_raises() -> None:
    lockout, limiter, _ = _lockout()

    def verify() -> bool:
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError, match="db down"):
        lockout.attempt("u", verify)
    assert limiter.peek("u").remaining == 2


def test_lockout_keeps_ids_apart() -> None:
    lockout, _, _ = _lockout()
    lockout.attempt("a", lambda: False)
    lockout.attempt("a", lambda: False)
    assert not lockout.attempt("a", lambda: True).allowed
    assert lockout.attempt("b", lambda: True).allowed


def test_async_lockout() -> None:
    async def run() -> None:
        limiter = AsyncLimiter(
            store=MemoryStore(), scope="f", limit=1, window_ms=60_000, on_store_error="closed"
        )
        lockout = AsyncLockout(limiter)

        async def wrong() -> bool:
            return False

        assert (await lockout.attempt("u", lambda: True)).verified is True
        failed = await lockout.attempt("u", wrong)
        assert (failed.allowed, failed.verified) == (True, False)
        locked = await lockout.attempt("u", lambda: True)
        assert (locked.allowed, locked.verified) == (False, None)
        keeping = AsyncLockout(limiter, reset_on_success=False)
        assert (await keeping.attempt("other", lambda: True)).verified is True

    asyncio.run(run())
