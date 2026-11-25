"""Load the shared vectors and run them against any store."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from omnilimit import (
    AsyncLimiter,
    AsyncLockout,
    CombinedDecision,
    Decision,
    HeaderInput,
    HitParams,
    Limiter,
    Lockout,
    LockoutLimit,
    RawHit,
    RawRefund,
    Reservation,
    SettledError,
    build_headers,
    check_all,
    check_all_async,
    peek_all,
    peek_all_async,
    reserve_all,
    reserve_all_async,
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


def strategy_kwargs(strategy: dict[str, Any]) -> dict[str, Any]:
    """An `ip` vector's strategy as the keyword arguments of `resolve_client_ip`."""
    if "preset" in strategy:
        return {"preset": strategy["preset"]}
    if "forwarded" in strategy:
        return {"forwarded_hops": strategy["forwarded"]["hops"]}
    if "header" in strategy:
        return {"header": strategy["header"]}
    if "peer" in strategy:
        return {"use_peer": True}
    return {"xff_hops": strategy["xff"]["hops"]}


def vectors_of(kind: str) -> list[dict[str, Any]]:
    return [vector for vector in ALL_VECTORS if vector["kind"] == kind]


def _load_fuzz() -> tuple[str | None, list[dict[str, Any]]]:
    """Vectors drawn for this run by `tools/fuzz_vectors.py`, when OMNILIMIT_FUZZ_VECTORS names
    their directory. They have the shape of the committed ones and go through the same runners."""
    name = os.environ.get("OMNILIMIT_FUZZ_VECTORS")
    if not name:
        return None, []
    base = Path(name).resolve()
    if not base.is_dir():
        raise RuntimeError(
            f"OMNILIMIT_FUZZ_VECTORS: {base} does not exist; run tools/fuzz_vectors.py"
        )
    seed = (base / "SEED").read_text(encoding="utf-8").strip()
    return seed, [json.loads(p.read_text(encoding="utf-8")) for p in sorted(base.rglob("*.json"))]


FUZZ_SEED, FUZZ_VECTORS = _load_fuzz()


def fuzz_of(kind: str, subject: str | None = None) -> list[dict[str, Any]]:
    return [
        vector
        for vector in FUZZ_VECTORS
        if vector["kind"] == kind and (subject is None or vector.get("subject") == subject)
    ]


def policy_of(vector: dict[str, Any]) -> dict[str, int]:
    policy = vector["policy"]
    return {"limit": policy["limit"], "window_ms": policy["window_ms"], "burst": policy["burst"]}


def shadowed_field(decision: Any) -> dict[str, Any]:
    """Vectors write `shadowed` only where it is true."""
    assert isinstance(decision.shadowed, bool), "every decision says whether it was shadowed"
    return {"shadowed": True} if decision.shadowed else {}


def outcome(decision: Any, raw: RawHit | None, profiles: dict[str, Any]) -> dict[str, Any]:
    return {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "status": decision.status,
        "remaining": decision.remaining,
        "reset_ms": decision.reset_ms,
        "retry_after_ms": decision.retry_after_ms,
        **shadowed_field(decision),
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
        shadow=vector.get("shadow", False),
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
        # A shadow limiter never calls the header function, so nothing may carry over.
        recording.profiles = {"draft-11": {}, "legacy": {}}
        cost = step.get("cost", 1)
        if op == "peek":
            decision = limiter.peek(step["id"], now_ms=step["at_ms"], cost=cost)
        elif index in refunded:
            reserved = limiter.reserve(step["id"], now_ms=step["at_ms"], cost=cost)
            # Only what the store counted is a reservation: not a denial, shadowed or not.
            expect = step["expect"]
            assert (reserved.reservation is None) is (
                not expect["allowed"] or expect.get("shadowed", False)
            ), context
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
        shadow=vector.get("shadow", False),
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
        # A shadow limiter never calls the header function, so nothing may carry over.
        recording.profiles = {"draft-11": {}, "legacy": {}}
        cost = step.get("cost", 1)
        if op == "peek":
            decision = await limiter.peek(step["id"], now_ms=step["at_ms"], cost=cost)
        elif index in refunded:
            reserved = await limiter.reserve(step["id"], now_ms=step["at_ms"], cost=cost)
            # Only what the store counted is a reservation: not a denial, shadowed or not.
            expect = step["expect"]
            assert (reserved.reservation is None) is (
                not expect["allowed"] or expect.get("shadowed", False)
            ), context
            reservations[index] = reserved.reservation
            decision = reserved.decision
        else:
            decision = await limiter.check(step["id"], now_ms=step["at_ms"], cost=cost)
        got = outcome(decision, recording.raw, recording.profiles)
        assert got == step["expect"], context


class ConcurrentRecipe:
    """The recipe form of a `concurrency` vector: what the run saw, and the limiters it used.

    Every limiter sees the store through a wrapper that passes the vector's time as `now_ms`,
    remembers its key so its level can be read afterwards, and counts the units of every
    allowed hit.
    """

    def __init__(self, vector: dict[str, Any], store: Any, salt_suffix: str, *, is_async: bool):
        self.vector = vector
        self.store = store
        self.now: int = vector["at_ms"]
        self.seen: dict[str, tuple[str, HitParams]] = {}
        self.counted = dict.fromkeys(vector["limiters"], 0)
        self.allowed = 0
        self.verify_ran = 0
        self.errors = 0
        self._lock = threading.Lock()
        self.limiters: dict[str, Any] = {}
        for name, config in vector["limiters"].items():
            kind = AsyncLimiter if is_async else Limiter
            self.limiters[name] = kind(
                **config["policy"],
                store=self._wrap(name, config["policy"]["window_ms"], is_async=is_async),
                scope=config["scope"],
                salt=vector["salt"] + salt_suffix,
                on_store_error=config["on_store_error"],
                store_timeout_ms=30_000,
            )
        recipe = vector["recipe"]
        self.subject = tuple(recipe.get("ids", ()))
        if "lockout" in recipe:
            limits = [
                LockoutLimit(self.limiters[limit["limiter"]], id_of=lambda ids, i=i: ids[i])
                for i, limit in enumerate(recipe["lockout"]["limits"])
            ]
            mode = recipe["lockout"].get("mode", "advisory")
            self.lockout: Any = (AsyncLockout if is_async else Lockout)(limits=limits, mode=mode)

    def _wrap(self, name: str, window: int, *, is_async: bool) -> Any:
        run = self
        inner = self.store

        def counted(params: HitParams, hit: RawHit) -> RawHit:
            if hit.allowed:
                with run._lock:
                    run.counted[name] += params.window_ms // window
            return hit

        if is_async:

            async def settle(result: Any) -> Any:
                return await result if inspect.isawaitable(result) else result

            async def ahit(key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
                run.seen[name] = (key, params)
                return counted(params, await settle(inner.hit(key, params, run.now)))

            async def apeek(key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
                run.seen[name] = (key, params)
                return cast(RawHit, await settle(inner.peek(key, params, run.now)))

            async def arefund(key: str, params: HitParams, since: int, now_ms: Any = None) -> Any:
                return await settle(inner.refund(key, params, since, run.now))

            return SimpleNamespace(tier=inner.tier, hit=ahit, peek=apeek, refund=arefund)

        def hit(key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            run.seen[name] = (key, params)
            return counted(params, inner.hit(key, params, run.now))

        def peek(key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            run.seen[name] = (key, params)
            return cast(RawHit, inner.peek(key, params, run.now))

        def refund(key: str, params: HitParams, since: int, now_ms: Any = None) -> Any:
            return inner.refund(key, params, since, run.now)

        return SimpleNamespace(tier=inner.tier, hit=hit, peek=peek, refund=refund)

    def entries(self) -> list[tuple[Any, ...]]:
        return [
            (self.limiters[e["limiter"]], e["id"], *([e["cost"]] if "cost" in e else []))
            for e in self.vector["recipe"]["entries"]
        ]

    def failures(self, result: Any) -> Any:
        """A closed limiter answers a store failure with a denial: count them all."""
        with self._lock:
            self.errors += sum(d.reason == "store_error" for d in result.decisions)
        return result

    def committing(self, pendings: list[Any]) -> set[int]:
        """The first `commit` allowed pendings, in request order, commit; the others cancel."""
        allowed = [i for i, pending in enumerate(pendings) if pending.allowed]
        return set(allowed[: self.vector["recipe"]["commit"]])

    def cancelled(self, result: Any) -> None:
        with self._lock:
            self.errors += sum(done is False for done in result.refunded)

    def check(self, levels: dict[str, int]) -> None:
        """Compare what the run saw with what the vector promises."""
        vector, context = self.vector, self.vector["id"]
        assert self.errors == 0, f"{context}: store failures"
        if "expect_allowed" in vector:
            assert self.allowed == vector["expect_allowed"], context
        if "expect_allowed_at_most" in vector:
            # Something must get through, or the bound says nothing.
            assert 0 < self.allowed <= vector["expect_allowed_at_most"], context
        if "expect_verify_ran" in vector:
            assert self.verify_ran == vector["expect_verify_ran"], context
        if "expect_counted" in vector:
            assert self.counted == vector["expect_counted"], context
        if "expect_levels" in vector:
            assert levels == vector["expect_levels"], context
        recipe = vector["recipe"]
        for name, config in vector["limiters"].items():
            burst, window = config["policy"]["burst"], config["policy"]["window_ms"]
            if vector.get("levels_within_capacity"):
                assert levels[name] <= burst * window, f"{context}: {name}"
            if vector.get("levels_match_counted"):
                assert levels[name] == self.counted[name] * window, f"{context}: {name}"
            if recipe["op"] == "check_all":
                # An allowed result was counted under every limit it names.
                entry = next(e for e in recipe["entries"] if e["limiter"] == name)
                assert self.counted[name] >= self.allowed * entry.get("cost", 1), context


def _concurrent_recipe(vector: dict[str, Any], store: Any, salt_suffix: str) -> None:
    """Every request in its own thread, released together by a barrier; gates and pendings all
    open, and then all settle."""
    run = ConcurrentRecipe(vector, store, salt_suffix, is_async=False)
    recipe, n = vector["recipe"], vector["requests"]
    barrier = threading.Barrier(n, timeout=30)

    def each(work: Callable[[int], Any]) -> list[Any]:
        def together(index: int) -> Any:
            barrier.wait()
            return work(index)

        with ThreadPoolExecutor(max_workers=n) as pool:
            return list(pool.map(together, range(n)))

    def verify() -> bool:
        with run._lock:
            run.verify_ran += 1
        # Let other attempts run while this one verifies.
        time.sleep(0.001)
        return bool(recipe["verify"])

    if recipe["op"] == "attempt":
        attempts = each(lambda _: run.failures(run.lockout.attempt(run.subject, verify)))
        run.allowed = sum(a.allowed for a in attempts)
    elif recipe["op"] == "gate":
        gates = each(lambda _: run.failures(run.lockout.begin(run.subject)))
        run.allowed = sum(gate.allowed for gate in gates)
        each(lambda i: run.failures(gates[i].fail()))
    elif recipe["op"] == "check_all":
        results = each(lambda _: run.failures(check_all(run.entries())))
        run.allowed = sum(result.allowed for result in results)
    else:
        pendings = each(lambda _: run.failures(reserve_all(run.entries())))
        run.allowed = sum(pending.allowed for pending in pendings)
        committing = run.committing(pendings)

        def settle(index: int) -> None:
            if index in committing:
                run.failures(pendings[index].commit())
            else:
                run.cancelled(pendings[index].cancel())

        each(settle)
    levels = {}
    for name in vector["limiters"]:
        key, params = run.seen[name]
        level = store.peek(key, params, run.now).level
        assert level is not None
        levels[name] = level
    run.check(levels)


async def _concurrent_recipe_async(vector: dict[str, Any], store: Any, salt_suffix: str) -> None:
    run = ConcurrentRecipe(vector, store, salt_suffix, is_async=True)
    recipe, n = vector["recipe"], vector["requests"]

    async def each(work: Callable[[int], Awaitable[Any]]) -> list[Any]:
        return list(await asyncio.gather(*(work(i) for i in range(n))))

    async def verify() -> bool:
        run.verify_ran += 1
        await asyncio.sleep(0.001)
        return bool(recipe["verify"])

    async def attempt(_: int) -> Any:
        return run.failures(await run.lockout.attempt(run.subject, verify))

    if recipe["op"] == "attempt":
        run.allowed = sum(a.allowed for a in await each(attempt))
    elif recipe["op"] == "gate":

        async def begin(_: int) -> Any:
            return run.failures(await run.lockout.begin(run.subject))

        gates = await each(begin)
        run.allowed = sum(gate.allowed for gate in gates)

        async def fail(i: int) -> Any:
            return run.failures(await gates[i].fail())

        await each(fail)
    elif recipe["op"] == "check_all":

        async def check(_: int) -> Any:
            return run.failures(await check_all_async(run.entries()))

        run.allowed = sum(result.allowed for result in await each(check))
    else:

        async def open_(_: int) -> Any:
            return run.failures(await reserve_all_async(run.entries()))

        pendings = await each(open_)
        run.allowed = sum(pending.allowed for pending in pendings)
        committing = run.committing(pendings)

        async def settle(index: int) -> None:
            if index in committing:
                run.failures(await pendings[index].commit())
            else:
                run.cancelled(await pendings[index].cancel())

        await each(settle)
    levels = {}
    for name in vector["limiters"]:
        key, params = run.seen[name]
        raw = store.peek(key, params, run.now)
        level = (await raw if inspect.isawaitable(raw) else raw).level
        assert level is not None
        levels[name] = level
    run.check(levels)


def run_concurrency(vector: dict[str, Any], store: Any, salt_suffix: str = "") -> None:
    if "recipe" in vector:
        _concurrent_recipe(vector, store, salt_suffix)
        return
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
    if "recipe" in vector:
        await _concurrent_recipe_async(vector, store, salt_suffix)
        return
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
        **shadowed_field(decision),
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
    if isinstance(error, VerifyRaised):
        return "verify"
    if isinstance(error, SettledError):
        return "state"
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
                shadow=config.get("shadow", False),
            )
            self.raw[name] = (inner, policy, key)
        self.handles: dict[str, Any] = {}
        # The earlier form takes the id as subject; the other one the step's ids, one per limit.
        self.lockouts: dict[str, Any] = {}
        self.earlier: set[str] = set()
        lockout = AsyncLockout if is_async else Lockout
        for name, given in vector.get("lockouts", {}).items():
            if isinstance(given, list):
                assert len(given) == 1, "the earlier form takes one limit"
                (limit,) = given
                self.earlier.add(name)
                self.lockouts[name] = lockout(
                    self.limiters[limit["limiter"]],
                    reset_on_success=limit.get("reset_on_success", True),
                )
                continue
            limits = [
                LockoutLimit(
                    self.limiters[limit["limiter"]],
                    id_of=lambda ids, i=i: ids[i],
                    reset_on_success=limit.get("reset_on_success", True),
                )
                for i, limit in enumerate(given["limits"])
            ]
            self.lockouts[name] = lockout(limits=limits, mode=given.get("mode", "advisory"))

    def subject(self, step: dict[str, Any]) -> Any:
        return step["ids"][0] if step["lockout"] in self.earlier else tuple(step["ids"])

    def attempted(self, step: dict[str, Any], attempt: Any, ran: bool) -> dict[str, Any]:
        result = {"allowed": attempt.allowed, "verified": attempt.verified, "verify_ran": ran}
        result.update({k: v for k, v in _combined(attempt).items() if k != "allowed"})
        if attempt.allowed and attempt.verified:
            assert attempt.resets is not None
            if step["lockout"] not in self.earlier:
                result["resets"] = list(attempt.resets)
        return result

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

            async def arefund(key: str, params: HitParams, since: int, now_ms: Any = None) -> Any:
                return await async_call(
                    "refund", key, lambda: inner.refund(key, params, since, harness.now)
                )

            wrapped.hit = ahit
            if hasattr(inner, "peek"):
                wrapped.peek = apeek
            if hasattr(inner, "reset"):
                wrapped.reset = areset
            if hasattr(inner, "refund"):
                wrapped.refund = arefund
            return wrapped

        def hit(key: str, params: HitParams, now_ms: int | None = None) -> Any:
            return sync_call("hit", key, lambda: inner.hit(key, params, harness.now))

        def peek(key: str, params: HitParams, now_ms: int | None = None) -> Any:
            return sync_call("peek", key, lambda: inner.peek(key, params, harness.now))

        def reset(key: str) -> Any:
            return sync_call("reset", key, lambda: inner.reset(key))

        def refund(key: str, params: HitParams, since: int, now_ms: Any = None) -> Any:
            return sync_call("refund", key, lambda: inner.refund(key, params, since, harness.now))

        wrapped.hit = hit
        if hasattr(inner, "peek"):
            wrapped.peek = peek
        if hasattr(inner, "reset"):
            wrapped.reset = reset
        if hasattr(inner, "refund"):
            wrapped.refund = refund
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

    def opened(self, step: dict[str, Any], pending: Any) -> dict[str, Any]:
        """Keep a pending under its handle, and report it like a combined result."""
        assert pending.mode == step["op"][: -len("_all")]
        assert not pending.settled
        self.handles[step["handle"]] = pending
        return {**_combined(pending), "refunded": list(pending.refunded)}

    def gated(self, step: dict[str, Any], gate: Any) -> dict[str, Any]:
        assert gate.mode == self.vector["lockouts"][step["lockout"]].get("mode", "advisory")
        assert not gate.settled
        self.handles[step["handle"]] = gate
        return {**_combined(gate), "refunded": list(gate.refunded)}

    def settled(self, step: dict[str, Any], result: Any) -> dict[str, Any]:
        assert self.handles[step["handle"]].settled
        if step["op"] == "cancel":
            return {"refunded": list(result.refunded)}
        if step["op"] == "succeed":
            extra = {"resets": list(result.resets), "refunded": list(result.refunded)}
            return {**_combined(result), **extra}
        return _combined(result)

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
            elif op in ("peek_all", "reserve_all"):
                opener = peek_all if op == "peek_all" else reserve_all
                got["result"] = h.opened(step, opener(h.entries(step)))
            elif op in ("commit", "cancel", "fail", "succeed"):
                pending = h.handles[step["handle"]]
                got["result"] = h.settled(step, getattr(pending, op)())
            elif op == "gate":
                got["result"] = h.gated(step, h.lockouts[step["lockout"]].begin(h.subject(step)))
            elif op == "attempt":
                attempt = h.lockouts[step["lockout"]].attempt(h.subject(step), verify)
                got["result"] = h.attempted(step, attempt, bool(ran))
            elif op in ("check", "peek"):
                limiter = h.limiters[step["limiter"]]
                call = limiter.check if op == "check" else limiter.peek
                got["result"] = short_decision(call(step["id"], cost=step.get("cost", 1)))
            elif op == "reset":
                got["result"] = h.limiters[step["limiter"]].reset(step["id"])
            else:
                raise AssertionError(f"unknown op {op}")
        except (ValueError, TypeError, VerifyRaised, SettledError) as error:
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
            elif op in ("peek_all", "reserve_all"):
                opener = peek_all_async if op == "peek_all" else reserve_all_async
                got["result"] = h.opened(step, await opener(h.entries(step)))
            elif op in ("commit", "cancel", "fail", "succeed"):
                pending = h.handles[step["handle"]]
                got["result"] = h.settled(step, await getattr(pending, op)())
            elif op == "gate":
                gate = await h.lockouts[step["lockout"]].begin(h.subject(step))
                got["result"] = h.gated(step, gate)
            elif op == "attempt":
                attempt = await h.lockouts[step["lockout"]].attempt(h.subject(step), verify)
                got["result"] = h.attempted(step, attempt, bool(ran))
            elif op in ("check", "peek"):
                limiter = h.limiters[step["limiter"]]
                call = limiter.check if op == "check" else limiter.peek
                got["result"] = short_decision(await call(step["id"], cost=step.get("cost", 1)))
            elif op == "reset":
                got["result"] = await h.limiters[step["limiter"]].reset(step["id"])
            else:
                raise AssertionError(f"unknown op {op}")
        except (ValueError, TypeError, VerifyRaised, SettledError) as error:
            got["error"] = _error_kind(error)
        h.compare(step, got, index)
