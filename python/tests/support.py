"""Load the shared vectors and run them against any store."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from omnilimit import (
    AsyncLimiter,
    AsyncLockout,
    CombinedDecision,
    Decision,
    HeaderInput,
    HitParams,
    Limiter,
    Lockout,
    RawHit,
    RawRefund,
    Reservation,
    build_headers,
    check_all,
    check_all_async,
)
from omnilimit._gcra import resolve_policy
from omnilimit._key import derive_key

SPEC_DIR = Path(__file__).resolve().parents[2] / "spec"
VECTOR_DIR = SPEC_DIR / "vectors"


def _load() -> list[dict[str, Any]]:
    vectors = []
    for path in sorted(VECTOR_DIR.rglob("*.json")):
        vector = json.loads(path.read_text(encoding="utf-8"))
        # A vector's id doubles as its path, so a renamed file cannot fall out of a suite.
        assert vector["id"] == path.relative_to(VECTOR_DIR).with_suffix("").as_posix()
        vectors.append(vector)
    return vectors


ALL_VECTORS = _load()


def vectors_of(kind: str) -> list[dict[str, Any]]:
    return [vector for vector in ALL_VECTORS if vector["kind"] == kind]


def policy_of(vector: dict[str, Any]) -> dict[str, int]:
    policy = vector["policy"]
    return {"limit": policy["limit"], "window_ms": policy["window_ms"], "burst": policy["burst"]}


def outcome(decision: Any, raw: RawHit | None, profiles: dict[str, Any]) -> dict[str, Any]:
    return {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "status": decision.status,
        "remaining": decision.remaining,
        "reset_ms": decision.reset_ms,
        "retry_after_ms": decision.retry_after_ms,
        "state": {"level": raw.level if raw else None, "ts": raw.ts if raw else None},
        "headers": profiles,
    }


def _refunded(vector: dict[str, Any]) -> set[int]:
    """Hit steps that a later step refunds: those run as reservations, the rest as checks."""
    return {step["of"] for step in vector["steps"] if step.get("op") == "refund"}


def refund_outcome(done: bool, raw: RawRefund | None) -> dict[str, Any]:
    if not done:
        return {"refunded": False}
    assert raw is not None
    return {"refunded": True, "state": {"level": raw.level, "ts": raw.ts}}


@dataclass
class Recording:
    """Keeps the raw reply and both header profiles of the latest check, and counts refunds."""

    tier: Any
    raw: RawHit | None = None
    refund: RawRefund | None = None
    refund_calls: int = 0
    profiles: dict[str, Any] = field(default_factory=dict)

    def headers(self, data: HeaderInput) -> dict[str, str]:
        self.profiles = {
            "draft-11": build_headers("draft-11", data),
            "legacy": build_headers("legacy", data),
        }
        return {}


def run_sequence(vector: dict[str, Any], store: Any, salt_suffix: str = "") -> None:
    recording = Recording(store.tier)

    class Recorder:
        tier = store.tier

        def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            recording.raw = store.hit(key, params, now_ms)
            return recording.raw

        def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            recording.raw = store.peek(key, params, now_ms)
            return recording.raw

        def reset(self, key: str) -> None:
            store.reset(key)

        def refund(
            self, key: str, params: HitParams, since: int, now_ms: int | None = None
        ) -> RawRefund:
            recording.refund_calls += 1
            recording.refund = store.refund(key, params, since, now_ms)
            return recording.refund

    limiter = Limiter(
        **policy_of(vector),
        store=Recorder(),
        scope=vector["scope"],
        salt=vector["salt"] + salt_suffix,
        on_store_error="closed",
        store_timeout_ms=10_000,
        headers=recording.headers,
    )
    refunded = _refunded(vector)
    reservations: dict[int, Reservation | None] = {}
    for index, step in enumerate(vector["steps"]):
        op = step.get("op", "hit")
        context = f"{vector['id']} step {index}"
        if op == "reset":
            assert limiter.reset(step["id"]) is True, context
            continue
        if op == "refund":
            calls = recording.refund_calls
            done = limiter.refund(reservations[step["of"]], now_ms=step["at_ms"])
            assert refund_outcome(done, recording.refund) == step["expect"], context
            # A spent reservation does not reach the store again.
            assert recording.refund_calls - calls == int(done), context
            continue
        recording.raw = None
        cost = step.get("cost", 1)
        if op == "peek":
            decision = limiter.peek(step["id"], now_ms=step["at_ms"], cost=cost)
        elif index in refunded:
            reserved = limiter.reserve(step["id"], now_ms=step["at_ms"], cost=cost)
            assert (reserved.reservation is None) is not step["expect"]["allowed"], context
            reservations[index] = reserved.reservation
            decision = reserved.decision
        else:
            decision = limiter.check(step["id"], now_ms=step["at_ms"], cost=cost)
        got = outcome(decision, recording.raw, recording.profiles)
        assert got == step["expect"], context


async def run_sequence_async(vector: dict[str, Any], store: Any, salt_suffix: str = "") -> None:
    recording = Recording(store.tier)

    class Recorder:
        tier = store.tier

        async def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            result = store.hit(key, params, now_ms)
            recording.raw = await result if asyncio.iscoroutine(result) else result
            return recording.raw

        async def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            result = store.peek(key, params, now_ms)
            recording.raw = await result if asyncio.iscoroutine(result) else result
            return recording.raw

        async def reset(self, key: str) -> None:
            result = store.reset(key)
            if asyncio.iscoroutine(result):
                await result

        async def refund(
            self, key: str, params: HitParams, since: int, now_ms: int | None = None
        ) -> RawRefund:
            recording.refund_calls += 1
            result = store.refund(key, params, since, now_ms)
            recording.refund = await result if asyncio.iscoroutine(result) else result
            return recording.refund

    limiter = AsyncLimiter(
        **policy_of(vector),
        store=Recorder(),
        scope=vector["scope"],
        salt=vector["salt"] + salt_suffix,
        on_store_error="closed",
        store_timeout_ms=10_000,
        headers=recording.headers,
    )
    refunded = _refunded(vector)
    reservations: dict[int, Reservation | None] = {}
    for index, step in enumerate(vector["steps"]):
        op = step.get("op", "hit")
        context = f"{vector['id']} step {index}"
        if op == "reset":
            assert await limiter.reset(step["id"]) is True, context
            continue
        if op == "refund":
            calls = recording.refund_calls
            done = await limiter.refund(reservations[step["of"]], now_ms=step["at_ms"])
            assert refund_outcome(done, recording.refund) == step["expect"], context
            assert recording.refund_calls - calls == int(done), context
            continue
        recording.raw = None
        cost = step.get("cost", 1)
        if op == "peek":
            decision = await limiter.peek(step["id"], now_ms=step["at_ms"], cost=cost)
        elif index in refunded:
            reserved = await limiter.reserve(step["id"], now_ms=step["at_ms"], cost=cost)
            assert (reserved.reservation is None) is not step["expect"]["allowed"], context
            reservations[index] = reserved.reservation
            decision = reserved.decision
        else:
            decision = await limiter.check(step["id"], now_ms=step["at_ms"], cost=cost)
        got = outcome(decision, recording.raw, recording.profiles)
        assert got == step["expect"], context


def run_concurrency(vector: dict[str, Any], store: Any, salt_suffix: str = "") -> None:
    limiter = Limiter(
        **policy_of(vector),
        store=store,
        scope=vector["scope"],
        salt=vector["salt"] + salt_suffix,
        on_store_error="closed",
        store_timeout_ms=30_000,
    )
    with ThreadPoolExecutor(max_workers=32) as pool:
        decisions = list(
            pool.map(
                lambda _: limiter.check(vector["id_value"], now_ms=vector["at_ms"]),
                range(vector["hits"]),
            )
        )
    assert [d for d in decisions if d.reason == "store_error"] == []
    assert sum(d.allowed for d in decisions) == vector["expect_allowed"]


async def run_concurrency_async(vector: dict[str, Any], store: Any, salt_suffix: str = "") -> None:
    limiter = AsyncLimiter(
        **policy_of(vector),
        store=store,
        scope=vector["scope"],
        salt=vector["salt"] + salt_suffix,
        on_store_error="closed",
        store_timeout_ms=30_000,
    )
    decisions = await asyncio.gather(
        *(limiter.check(vector["id_value"], now_ms=vector["at_ms"]) for _ in range(vector["hits"]))
    )
    assert [d for d in decisions if d.reason == "store_error"] == []
    assert sum(d.allowed for d in decisions) == vector["expect_allowed"]


def short_decision(decision: Decision) -> dict[str, Any]:
    return {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "status": decision.status,
        "remaining": decision.remaining,
        "reset_ms": decision.reset_ms,
        "retry_after_ms": decision.retry_after_ms,
    }


def _combined(result: CombinedDecision) -> dict[str, Any]:
    # Rule 3 asks for one of the reported decisions, so this is identity and not equality.
    governing = next(i for i, d in enumerate(result.decisions) if d is result.decision)
    return {
        "allowed": result.allowed,
        "governing": governing,
        "decisions": [short_decision(d) for d in result.decisions],
    }


class VerifyRaised(Exception):
    """What a vector's `verify: "raise"` raises, to tell it apart from the library's errors."""


def _error_kind(error: Exception) -> str:
    # `state` is reserved: meeting it here is a failure.
    if isinstance(error, VerifyRaised):
        return "verify"
    if isinstance(error, ValueError):
        return "value"
    if isinstance(error, TypeError):
        return "type"
    raise error


class _SyncApproximate:
    tier = "approximate"

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        return RawHit(allowed=True, level=None, ts=None)


class _AsyncApproximate:
    tier = "approximate"

    async def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        return RawHit(allowed=True, level=None, ts=None)


class RecipeHarness:
    """The limiters of a recipe vector, each on the store under test seen through a wrapper.

    The wrapper passes the vector's clock as `now_ms`, fails or hangs while a fault is armed,
    and logs every call by limiter and id. Limiters of tier approximate get a store that
    always allows and cannot peek or reset.
    """

    def __init__(self, vector: dict[str, Any], store: Any, salt_suffix: str, *, is_async: bool):
        self.vector = vector
        self.is_async = is_async
        self.now = 0
        self.calls: list[dict[str, Any]] = []
        self.faults: dict[str, tuple[str, list[str]]] = {}
        timeline = vector["timeline"]
        has_timeout = any(step.get("fault") == "timeout" for step in timeline)
        self.timeout_ms = 250 if has_timeout else 10_000
        salt = vector["salt"] + salt_suffix
        ids = {
            who
            for step in timeline
            for who in [
                *(entry["id"] for entry in step.get("entries", [])),
                *step.get("ids", []),
                *([step["id"]] if "id" in step else []),
            ]
        }
        self.limiters: dict[str, Any] = {}
        self.raw: dict[str, tuple[Any, HitParams, Callable[[str], str]]] = {}
        for name, config in vector["limiters"].items():
            policy = resolve_policy(**config["policy"])
            if config.get("tier") == "approximate":
                inner: Any = _AsyncApproximate() if is_async else _SyncApproximate()
            else:
                inner = store

            def key(who: str, scope: str = config["scope"], policy: Any = policy) -> str:
                return derive_key(scope, policy, salt, who)

            id_of = {key(who): who for who in ids}
            kind = AsyncLimiter if is_async else Limiter
            self.limiters[name] = kind(
                limit=config["policy"]["limit"],
                window_ms=config["policy"]["window_ms"],
                burst=config["policy"]["burst"],
                store=self._wrap(name, inner, id_of),
                scope=config["scope"],
                salt=salt,
                on_store_error=config["on_store_error"],
                store_timeout_ms=self.timeout_ms,
            )
            self.raw[name] = (inner, policy, key)
        self.lockouts: dict[str, Any] = {}
        for name, limits in vector.get("lockouts", {}).items():
            assert len(limits) == 1, "lockouts take one limit"
            (limit,) = limits
            lockout = AsyncLockout if is_async else Lockout
            self.lockouts[name] = lockout(
                self.limiters[limit["limiter"]],
                reset_on_success=limit.get("reset_on_success", True),
            )

    def _log(self, name: str, op: str, key: str, id_of: dict[str, str]) -> str | None:
        """Record a call, and return the armed fault that answers it, if any."""
        fault = self.faults.get(name)
        failed = fault is not None and op in fault[1]
        entry: dict[str, Any] = {"op": op, "limiter": name, "id": id_of.get(key, f"?{key}")}
        if failed:
            entry["failed"] = True
        self.calls.append(entry)
        return fault[0] if fault is not None and failed else None

    def _wrap(self, name: str, inner: Any, id_of: dict[str, str]) -> Any:
        harness = self
        local = inner.tier == "local"

        def sync_call(op: str, key: str, run: Callable[[], Any]) -> Any:
            fault = harness._log(name, op, key, id_of)
            if fault == "timeout" and not local:
                # Hang past the limiter's timeout. A local store is called inline, without one,
                # so for it a timeout can only be the store giving up.
                time.sleep(harness.timeout_ms * 3 / 1000)
            if fault is not None:
                raise (TimeoutError if fault == "timeout" else ConnectionError)("injected fault")
            return run()

        async def async_call(op: str, key: str, run: Callable[[], Any]) -> Any:
            fault = harness._log(name, op, key, id_of)
            if fault == "timeout":
                await asyncio.sleep(3600)
            if fault is not None:
                raise ConnectionError("injected fault")
            result = run()
            return await result if inspect.isawaitable(result) else result

        wrapped = SimpleNamespace(tier=inner.tier)
        if self.is_async:

            async def ahit(key: str, params: HitParams, now_ms: int | None = None) -> Any:
                return await async_call("hit", key, lambda: inner.hit(key, params, harness.now))

            async def apeek(key: str, params: HitParams, now_ms: int | None = None) -> Any:
                return await async_call("peek", key, lambda: inner.peek(key, params, harness.now))

            async def areset(key: str) -> Any:
                return await async_call("reset", key, lambda: inner.reset(key))

            wrapped.hit = ahit
            if hasattr(inner, "peek"):
                wrapped.peek = apeek
            if hasattr(inner, "reset"):
                wrapped.reset = areset
            return wrapped

        def hit(key: str, params: HitParams, now_ms: int | None = None) -> Any:
            return sync_call("hit", key, lambda: inner.hit(key, params, harness.now))

        def peek(key: str, params: HitParams, now_ms: int | None = None) -> Any:
            return sync_call("peek", key, lambda: inner.peek(key, params, harness.now))

        def reset(key: str) -> Any:
            return sync_call("reset", key, lambda: inner.reset(key))

        wrapped.hit = hit
        if hasattr(inner, "peek"):
            wrapped.peek = peek
        if hasattr(inner, "reset"):
            wrapped.reset = reset
        return wrapped

    def before(self, step: dict[str, Any]) -> bool:
        """Apply the clock and the steps that call nothing. True when the step is done."""
        if "at_ms" in step:
            self.now = step["at_ms"]
        if step["op"] == "fault":
            self.faults[step["limiter"]] = (step["fault"], step["on"])
            return True
        if step["op"] == "heal":
            self.faults.pop(step["limiter"], None)
            return True
        self.calls = []
        return False

    def inspected(self, step: dict[str, Any], raw: RawHit) -> None:
        assert {"state": {"level": raw.level, "ts": raw.ts}} == step["expect"]

    def entries(self, step: dict[str, Any]) -> list[tuple[Any, ...]]:
        return [
            (self.limiters[e["limiter"]], e["id"], *([e["cost"]] if "cost" in e else []))
            for e in step["entries"]
        ]

    def compare(self, step: dict[str, Any], got: dict[str, Any], index: int) -> None:
        assert {"calls": self.calls, **got} == step["expect"], (
            f"{self.vector['id']} step {index} ({step['op']})"
        )


def _attempt_result(attempt: Any, verify_ran: bool) -> dict[str, Any]:
    return {
        "allowed": attempt.allowed,
        "verified": attempt.verified,
        "verify_ran": verify_ran,
        "governing": 0,
        "decisions": [short_decision(attempt.decision)],
    }


def run_recipe(vector: dict[str, Any], store: Any, salt_suffix: str = "") -> None:
    h = RecipeHarness(vector, store, salt_suffix, is_async=False)
    for index, step in enumerate(vector["timeline"]):
        if h.before(step):
            continue
        op = step["op"]
        if op == "inspect":
            inner, params, key = h.raw[step["limiter"]]
            h.inspected(step, inner.peek(key(step["id"]), params, h.now))
            continue
        ran: list[bool] = []

        def verify(step: dict[str, Any] = step, ran: list[bool] = ran) -> bool:
            ran.append(True)
            if step["verify"] == "raise":
                raise VerifyRaised
            return bool(step["verify"])

        got: dict[str, Any] = {}
        try:
            if op == "check_all":
                got["result"] = _combined(check_all(h.entries(step)))
            elif op == "attempt":
                attempt = h.lockouts[step["lockout"]].attempt(step["ids"][0], verify)
                got["result"] = _attempt_result(attempt, bool(ran))
            elif op in ("check", "peek"):
                limiter = h.limiters[step["limiter"]]
                call = limiter.check if op == "check" else limiter.peek
                got["result"] = short_decision(call(step["id"], cost=step.get("cost", 1)))
            elif op == "reset":
                got["result"] = h.limiters[step["limiter"]].reset(step["id"])
            else:
                raise AssertionError(f"unknown op {op}")
        except (ValueError, TypeError, VerifyRaised) as error:
            got["error"] = _error_kind(error)
        h.compare(step, got, index)


async def run_recipe_async(vector: dict[str, Any], store: Any, salt_suffix: str = "") -> None:
    h = RecipeHarness(vector, store, salt_suffix, is_async=True)
    for index, step in enumerate(vector["timeline"]):
        if h.before(step):
            continue
        op = step["op"]
        if op == "inspect":
            inner, params, key = h.raw[step["limiter"]]
            raw = inner.peek(key(step["id"]), params, h.now)
            h.inspected(step, await raw if inspect.isawaitable(raw) else raw)
            continue
        ran: list[bool] = []

        async def verify(step: dict[str, Any] = step, ran: list[bool] = ran) -> bool:
            ran.append(True)
            if step["verify"] == "raise":
                raise VerifyRaised
            return bool(step["verify"])

        got: dict[str, Any] = {}
        try:
            if op == "check_all":
                got["result"] = _combined(await check_all_async(h.entries(step)))
            elif op == "attempt":
                attempt = await h.lockouts[step["lockout"]].attempt(step["ids"][0], verify)
                got["result"] = _attempt_result(attempt, bool(ran))
            elif op in ("check", "peek"):
                limiter = h.limiters[step["limiter"]]
                call = limiter.check if op == "check" else limiter.peek
                got["result"] = short_decision(await call(step["id"], cost=step.get("cost", 1)))
            elif op == "reset":
                got["result"] = await h.limiters[step["limiter"]].reset(step["id"])
            else:
                raise AssertionError(f"unknown op {op}")
        except (ValueError, TypeError, VerifyRaised) as error:
            got["error"] = _error_kind(error)
        h.compare(step, got, index)
