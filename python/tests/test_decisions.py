"""A limiter builds its decisions, headers and events the short way. They must be, value for
value, what the long way gives: `expected` below builds them from the public pieces alone."""

from __future__ import annotations

import asyncio
import copy
import pickle
from collections.abc import Callable, Iterator
from dataclasses import asdict, fields, replace
from itertools import product
from typing import Any

import pytest

from omnilimit import (
    AsyncLimiter,
    Decision,
    HeaderInput,
    HitParams,
    Limiter,
    LimiterEvent,
    MemoryStore,
    RawHit,
    RawRefund,
    build_headers,
)

SCOPE = "t"
LIMIT = 3
WINDOW_MS = 1000
BURSTS = (3, 5)
PROFILES = ("draft-11", "legacy", "none")


def custom(data: HeaderInput) -> dict[str, str]:
    return {"x-scope": data.scope, "x-effective": str(data.effective_ms)}


def _ceil(a: int, b: int) -> int:
    return -(-a // b)


def expected(
    hit: RawHit, *, burst: int, tier: str, cost: int, headers: Any, shadow: bool
) -> tuple[Decision, int]:
    """The decision for a hit, and its effective window, as the spec derives them."""
    capacity = burst * WINDOW_MS
    remaining = reset_ms = retry_after_ms = None
    if hit.level is None:
        if not hit.allowed:
            retry_after_ms = hit.retry_after_ms if hit.retry_after_ms is not None else WINDOW_MS
        effective_ms = retry_after_ms or 0
    else:
        remaining = (capacity - hit.level) // WINDOW_MS
        reset_ms = _ceil(hit.level, LIMIT)
        if not hit.allowed:
            retry_after_ms = _ceil(max(0, hit.level + cost * WINDOW_MS - capacity), LIMIT)
            effective_ms = retry_after_ms
        elif remaining == 0:
            effective_ms = _ceil(max(0, hit.level + WINDOW_MS - capacity), LIMIT)
        else:
            effective_ms = reset_ms
    decision = Decision(
        allowed=hit.allowed,
        reason="ok" if hit.allowed else "limited",
        status=None if hit.allowed else 429,
        remaining=remaining,
        reset_ms=reset_ms,
        retry_after_ms=retry_after_ms,
        tier=tier,  # type: ignore[arg-type]
    )
    return _finished(decision, effective_ms, headers, shadow), effective_ms


def expected_failure(*, tier: str, mode: str, headers: Any, shadow: bool) -> Decision:
    allowed = mode == "open"
    decision = Decision(
        allowed=allowed,
        reason="store_error",
        status=None if allowed else 503,
        remaining=None,
        reset_ms=None,
        retry_after_ms=None if allowed else 1000,
        tier=tier,  # type: ignore[arg-type]
    )
    return _finished(decision, 0, headers, shadow)


def _finished(decision: Decision, effective_ms: int, headers: Any, shadow: bool) -> Decision:
    if shadow:
        if decision.allowed:
            return decision
        return replace(decision, allowed=True, status=None, shadowed=True)
    data = HeaderInput(SCOPE, LIMIT, WINDOW_MS, decision, effective_ms)
    return replace(
        decision, headers=headers(data) if callable(headers) else build_headers(headers, data)
    )


class Scripted:
    """A store that answers what it is told to, or fails."""

    def __init__(self, tier: str) -> None:
        self.tier = tier
        self.answer: RawHit | Exception = RawHit(True, 0, 0)

    def _answer(self) -> RawHit:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        return self._answer()

    def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        return self._answer()

    def refund(
        self, key: str, params: HitParams, since: int, now_ms: int | None = None
    ) -> RawRefund:
        return RawRefund(0, 0)


class AsyncScripted(Scripted):
    async def hit(  # type: ignore[override]
        self, key: str, params: HitParams, now_ms: int | None = None
    ) -> RawHit:
        return self._answer()

    async def peek(  # type: ignore[override]
        self, key: str, params: HitParams, now_ms: int | None = None
    ) -> RawHit:
        return self._answer()

    async def refund(  # type: ignore[override]
        self, key: str, params: HitParams, since: int, now_ms: int | None = None
    ) -> RawRefund:
        return RawRefund(0, 0)


def hits(burst: int, tier: str) -> Iterator[RawHit]:
    if tier == "approximate":
        yield RawHit(True, None, None)
        yield RawHit(False, None, None)
        yield RawHit(False, None, None, retry_after_ms=2500)
        return
    capacity = burst * WINDOW_MS
    for level in sorted({*range(0, capacity + 1, 250), 1, 999, 1001, capacity - 1}):
        yield RawHit(True, level, 7)
        yield RawHit(False, level, 7)


Run = Callable[[str, int], Decision]


def limiters(tier: str, **options: Any) -> Iterator[tuple[str, Scripted, Callable[[str], Run]]]:
    """Every way a hit reaches a decision: each limiter, on a blocking and an async store."""
    blocking = Scripted(tier)
    sync = Limiter(store=blocking, **options)

    def sync_run(op: str) -> Run:
        if op == "reserve":
            return lambda id_value, cost: sync.reserve(id_value, cost=cost).decision
        return lambda id_value, cost: getattr(sync, op)(id_value, cost=cost)

    yield "Limiter", blocking, sync_run

    stores: list[Scripted] = [AsyncScripted(tier)]
    if tier == "local":
        stores.append(Scripted(tier))
    for store in stores:
        limiter = AsyncLimiter(store=store, **options)

        def async_run(op: str, limiter: AsyncLimiter = limiter) -> Run:
            if op == "reserve":
                return lambda id_value, cost: (
                    asyncio.run(limiter.reserve(id_value, cost=cost)).decision
                )
            return lambda id_value, cost: asyncio.run(getattr(limiter, op)(id_value, cost=cost))

        yield f"AsyncLimiter on {type(store).__name__}", store, async_run


@pytest.mark.parametrize("tier", ["local", "exact", "approximate"])
@pytest.mark.parametrize("headers", [*PROFILES, custom])
@pytest.mark.parametrize("shadow", [False, True])
def test_decisions_headers_and_events_are_what_the_spec_derives(
    tier: str, headers: Any, shadow: bool
) -> None:
    costs = (1,) if tier == "approximate" else (1, 2)
    ops = ("check",) if tier == "approximate" else ("check", "peek", "reserve")
    for burst in BURSTS:
        events: list[LimiterEvent] = []
        options = {
            "scope": SCOPE,
            "limit": LIMIT,
            "window_ms": WINDOW_MS,
            "burst": burst,
            "on_store_error": "closed",
            "headers": headers,
            "shadow": shadow,
            "on_event": events.append,
        }
        for name, store, run in limiters(tier, **options):
            for hit, cost, op in product(hits(burst, tier), costs, ops):
                store.answer = hit
                events.clear()
                decision = run(op)("id", cost)
                want, _ = expected(
                    hit, burst=burst, tier=tier, cost=cost, headers=headers, shadow=shadow
                )
                where = f"{name} {op} {hit} cost {cost}"
                assert decision == want, where
                assert want == decision, where
                assert repr(decision) == repr(want), where
                assert asdict(decision) == asdict(want), where
                assert type(decision) is Decision and type(decision.headers) is dict, where
                assert events == [
                    LimiterEvent(
                        "allowed" if hit.allowed else "limited",
                        SCOPE,
                        tier,  # type: ignore[arg-type]
                        op="peek" if op == "peek" else "check",
                        shadowed=want.shadowed,
                    )
                ], where


@pytest.mark.parametrize("tier", ["local", "exact", "approximate"])
@pytest.mark.parametrize("headers", [*PROFILES, custom])
@pytest.mark.parametrize("shadow", [False, True])
@pytest.mark.parametrize("mode", ["open", "closed"])
def test_store_failures_are_decided_and_reported_the_same(
    tier: str, headers: Any, shadow: bool, mode: str
) -> None:
    events: list[LimiterEvent] = []
    options = {
        "scope": SCOPE,
        "limit": LIMIT,
        "window_ms": WINDOW_MS,
        "on_store_error": mode,
        "headers": headers,
        "shadow": shadow,
        "on_event": events.append,
    }
    error = RuntimeError("down")
    want = expected_failure(tier=tier, mode=mode, headers=headers, shadow=shadow)
    for name, store, run in limiters(tier, **options):
        store.answer = error
        for op in ("check", "peek", "reserve"):
            events.clear()
            decision = run(op)("id", 1)
            assert decision == want, f"{name} {op}"
            assert repr(decision) == repr(want), f"{name} {op}"
            assert events == [
                LimiterEvent(
                    "store_error",
                    SCOPE,
                    tier,  # type: ignore[arg-type]
                    error,
                    op="peek" if op == "peek" else "check",
                    shadowed=want.shadowed,
                )
            ], f"{name} {op}"


def test_a_limiter_without_a_listener_decides_the_same() -> None:
    for tier, burst in product(("local", "exact", "approximate"), BURSTS):
        for name, store, run in limiters(
            tier, scope=SCOPE, limit=LIMIT, window_ms=WINDOW_MS, burst=burst, on_store_error="open"
        ):
            for hit in hits(burst, tier):
                store.answer = hit
                want, _ = expected(
                    hit, burst=burst, tier=tier, cost=1, headers="draft-11", shadow=False
                )
                assert run("check")("id", 1) == want, f"{name} {hit}"
            store.answer = RuntimeError("down")
            assert run("check")("id", 1) == expected_failure(
                tier=tier, mode="open", headers="draft-11", shadow=False
            )


def test_a_header_function_runs_once_per_decision_when_it_is_made() -> None:
    seen: list[HeaderInput] = []

    def headers(data: HeaderInput) -> dict[str, str]:
        seen.append(data)
        return {"x-calls": str(len(seen))}

    store = Scripted("local")
    limiter = Limiter(
        store=store,
        scope=SCOPE,
        limit=LIMIT,
        window_ms=WINDOW_MS,
        on_store_error="closed",
        headers=headers,
    )
    store.answer = RawHit(False, 3000, 7)
    decision = limiter.check("id")
    # Called before anyone reads the headers, and not again when they do.
    assert len(seen) == 1
    assert decision.headers == {"x-calls": "1"}
    assert decision.headers == {"x-calls": "1"}
    assert len(seen) == 1
    data = seen[0]
    assert (data.scope, data.limit, data.window_ms, data.effective_ms) == (SCOPE, 3, 1000, 334)
    # What the function receives is the decision without headers, and it stays that way.
    assert data.decision is not decision
    assert data.decision.headers == {}
    assert data.decision == replace(decision, headers={})


def test_a_header_function_may_answer_anything_and_may_raise() -> None:
    store = Scripted("local")
    options: dict[str, Any] = {
        "store": store,
        "scope": SCOPE,
        "limit": LIMIT,
        "window_ms": WINDOW_MS,
        "on_store_error": "closed",
    }
    nothing = Limiter(**options, headers=lambda data: None)  # type: ignore[arg-type,return-value]
    assert nothing.check("id").headers is None

    def broken(data: HeaderInput) -> dict[str, str]:
        raise LookupError("no headers")

    with pytest.raises(LookupError, match="no headers"):
        Limiter(**options, headers=broken).check("id")


class TestHeadersAreBuiltOnTheFirstRead:
    def decision(self, **options: Any) -> Decision:
        store = MemoryStore()
        limiter = Limiter(
            store=store, scope=SCOPE, limit=1, window_ms=1000, on_store_error="closed", **options
        )
        limiter.check("id", now_ms=5000)
        return limiter.check("id", now_ms=5000)

    def test_every_read_gives_the_same_dict(self) -> None:
        decision = self.decision()
        headers = decision.headers
        assert headers == {
            "ratelimit-policy": '"t";q=1;w=1',
            "ratelimit": '"t";r=0;t=1',
            "retry-after": "1",
        }
        assert decision.headers is headers
        headers["x-added"] = "1"
        assert decision.headers["x-added"] == "1"

    def test_each_decision_has_its_own_dict(self) -> None:
        first, second = self.decision(), self.decision()
        assert first.headers == second.headers
        assert first.headers is not second.headers
        shadowed = self.decision(shadow=True), self.decision(shadow=True)
        assert shadowed[0].headers == shadowed[1].headers == {}
        assert shadowed[0].headers is not shadowed[1].headers

    def test_copies_carry_the_headers(self) -> None:
        want = self.decision().headers
        assert pickle.loads(pickle.dumps(self.decision())).headers == want  # noqa: S301
        assert copy.copy(self.decision()).headers == want
        assert copy.deepcopy(self.decision()).headers == want
        assert replace(self.decision(), allowed=True).headers == want
        assert asdict(self.decision())["headers"] == want

    def test_a_decision_is_still_the_same_frozen_dataclass(self) -> None:
        decision = self.decision()
        assert [f.name for f in fields(decision)] == [
            "allowed", "reason", "status", "remaining", "reset_ms", "retry_after_ms", "tier",
            "headers", "shadowed",
        ]  # fmt: skip
        assert Decision.__slots__ == tuple(f.name for f in fields(Decision))
        assert not hasattr(decision, "__dict__")
        with pytest.raises(AttributeError, match="cannot assign to field 'headers'"):
            decision.headers = {}  # type: ignore[misc]
        with pytest.raises(AttributeError, match="'Decision' object has no attribute 'nope'"):
            decision.nope  # type: ignore[attr-defined]  # noqa: B018
        with pytest.raises(TypeError, match="unhashable"):
            hash(decision)

    def test_a_decision_built_by_hand_is_unchanged(self) -> None:
        decision = Decision(True, "ok", None, 1, 2, None, "local")
        assert decision.headers == {}
        assert decision.headers is decision.headers
        assert decision.headers is not Decision(True, "ok", None, 1, 2, None, "local").headers


def test_an_async_limiter_awaits_what_a_plain_function_returns() -> None:
    """A local store may hand back an awaitable from a plain `hit`."""

    class Deferring:
        tier = "local"

        def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> Any:
            async def answer() -> RawHit:
                return RawHit(False, params.capacity, 0)

            return answer()

    limiter = AsyncLimiter(
        store=Deferring(),  # type: ignore[arg-type]
        scope=SCOPE, limit=LIMIT, window_ms=WINDOW_MS, on_store_error="open",
    )  # fmt: skip
    decision = asyncio.run(limiter.check("id"))
    assert (decision.allowed, decision.reason, decision.remaining) == (False, "limited", 0)
