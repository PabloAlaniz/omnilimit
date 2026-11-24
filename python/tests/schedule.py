"""Drive a recipe through one chosen interleaving, store call by store call.

`tools/explore_interleavings.py` lists every order the store calls of a few concurrent callers
can take, with the result the independent model got in each. Here the port is put through the
same order: every store call waits at a gate, and the schedule says which caller goes next.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any

from omnilimit import (
    AsyncLimiter,
    AsyncLockout,
    HitParams,
    Limiter,
    Lockout,
    LockoutLimit,
    MemoryStore,
    check_all,
    check_all_async,
    reserve_all,
    reserve_all_async,
)

_WAIT_S = 30.0

Result = tuple[int, int, list[int]]


class _Caller:
    def __init__(self, work: Callable[[], Any]) -> None:
        self.work = work
        self.started = False
        self.done = False
        self.waiting = False
        self.released = False
        self.error: BaseException | None = None
        self.thread: threading.Thread | None = None


class Turns:
    """Callers that take turns: only the one the schedule picked moves, by one store call.

    The blocking limiter runs each store call on a thread of its own, so a caller is not told
    apart by its thread: whoever calls the store while a caller has the turn is that caller.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._current: _Caller | None = None
        self.violations: list[str] = []

    def gate(self, op: str) -> None:
        """Every store call waits here until its caller is picked again."""
        with self._cond:
            caller = self._current
            if caller is None:
                self.violations.append(f"{op}: a store call the model does not make")
                return
            if caller.waiting:
                self.violations.append(f"{op}: two store calls at once from one caller")
            caller.waiting, caller.released = True, False
            self._cond.notify_all()
            if not self._cond.wait_for(lambda c=caller: c.released, timeout=_WAIT_S):
                raise TimeoutError("the schedule never came back to this caller")

    def _body(self, caller: _Caller) -> None:
        try:
            caller.work()
        except BaseException as error:
            caller.error = error
        finally:
            with self._cond:
                caller.done = True
                self._cond.notify_all()

    def run(self, callers: list[_Caller], picks: Iterator[int]) -> None:
        """The model's `run`: a pick is an index into the callers still running. The first step
        of a caller takes it to its first store call, and each later one lets one call through."""
        live = list(callers)
        while live:
            pick = next(picks)
            caller = live[pick]
            with self._cond:
                self._current = caller
                if not caller.started:
                    caller.started = True
                    caller.thread = threading.Thread(target=self._body, args=(caller,), daemon=True)
                    caller.thread.start()
                else:
                    caller.waiting, caller.released = False, True
                    self._cond.notify_all()
                if not self._cond.wait_for(lambda c=caller: c.waiting or c.done, timeout=_WAIT_S):
                    raise TimeoutError("a caller neither finished nor reached the store")
                self._current = None
            if caller.done:
                assert caller.thread is not None
                caller.thread.join()
                if caller.error is not None:
                    raise caller.error
                live.pop(pick)


def _limiters(vector: dict[str, Any], gate: Any, seen: dict[str, Any], *, is_async: bool) -> Any:
    now: int = vector["at_ms"]
    inner = MemoryStore()
    limiters: dict[str, Any] = {}
    for name, config in vector["limiters"].items():
        if is_async:

            async def ahit(key: str, params: HitParams, _now: Any = None, name: str = name) -> Any:
                seen[name] = (key, params)
                await gate(f"hit {name}")
                return inner.hit(key, params, now)

            async def apeek(key: str, params: HitParams, _now: Any = None, name: str = name) -> Any:
                seen[name] = (key, params)
                await gate(f"peek {name}")
                return inner.peek(key, params, now)

            async def areset(key: str, name: str = name) -> None:
                await gate(f"reset {name}")
                inner.reset(key)

            async def arefund(
                key: str, params: HitParams, since: int, _now: Any = None, name: str = name
            ) -> Any:
                await gate(f"refund {name}")
                return inner.refund(key, params, since, now)

            store = SimpleNamespace(
                tier="exact", hit=ahit, peek=apeek, reset=areset, refund=arefund
            )
        else:

            def hit(key: str, params: HitParams, _now: Any = None, name: str = name) -> Any:
                seen[name] = (key, params)
                gate(f"hit {name}")
                return inner.hit(key, params, now)

            def peek(key: str, params: HitParams, _now: Any = None, name: str = name) -> Any:
                seen[name] = (key, params)
                gate(f"peek {name}")
                return inner.peek(key, params, now)

            def reset(key: str, name: str = name) -> None:
                gate(f"reset {name}")
                inner.reset(key)

            def refund(
                key: str, params: HitParams, since: int, _now: Any = None, name: str = name
            ) -> Any:
                gate(f"refund {name}")
                return inner.refund(key, params, since, now)

            store = SimpleNamespace(tier="exact", hit=hit, peek=peek, reset=reset, refund=refund)
        limiters[name] = (AsyncLimiter if is_async else Limiter)(
            **config["policy"],
            store=store,
            scope=config["scope"],
            salt=vector["salt"],
            on_store_error=config["on_store_error"],
            store_timeout_ms=60_000,
        )
    return inner, limiters


def _lockout(vector: dict[str, Any], limiters: dict[str, Any], *, is_async: bool) -> Any:
    recipe = vector["recipe"]
    limits = [
        LockoutLimit(limiters[limit["limiter"]], id_of=lambda ids, i=i: ids[i])
        for i, limit in enumerate(recipe["lockout"]["limits"])
    ]
    mode = recipe["lockout"].get("mode", "advisory")
    return (AsyncLockout if is_async else Lockout)(limits=limits, mode=mode), mode


def _entries(vector: dict[str, Any], limiters: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [
        (limiters[e["limiter"]], e["id"], *([e["cost"]] if "cost" in e else []))
        for e in vector["recipe"]["entries"]
    ]


def _levels(vector: dict[str, Any], inner: MemoryStore, seen: dict[str, Any]) -> list[int]:
    levels = []
    for name in vector["limiters"]:
        if name not in seen:
            levels.append(0)
            continue
        key, params = seen[name]
        level = inner.peek(key, params, vector["at_ms"]).level
        assert level is not None
        levels.append(level)
    return levels


def run_order(vector: dict[str, Any], picks: list[int]) -> Result:
    """Run the vector's recipe in the order `picks` gives, and report what the model reports."""
    turns = Turns()
    seen: dict[str, Any] = {}
    inner, limiters = _limiters(vector, turns.gate, seen, is_async=False)
    recipe, n = vector["recipe"], vector["requests"]
    order = iter(picks)
    count = {"allowed": 0, "verify_ran": 0}
    lock = threading.Lock()

    def add(name: str, by: int = 1) -> None:
        with lock:
            count[name] += by

    if recipe["op"] in ("gate", "attempt"):
        lockout, mode = _lockout(vector, limiters, is_async=False)
        subject = tuple(recipe["ids"])
        if recipe["op"] == "attempt":

            def verify() -> bool:
                add("verify_ran")
                return bool(recipe["verify"])

            def attempt() -> None:
                add("allowed", int(lockout.attempt(subject, verify).allowed))

            turns.run([_Caller(attempt) for _ in range(n)], order)
        else:
            gates: list[Any] = [None] * n

            def begin(index: int) -> None:
                gates[index] = lockout.begin(subject)

            turns.run([_Caller(lambda i=i: begin(i)) for i in range(n)], order)
            count["allowed"] = sum(gate.allowed for gate in gates)
            # Only an advisory failure counts anything, and only for a gate that was allowed.
            counting = [gate for gate in gates if mode == "advisory" and gate.allowed]
            turns.run([_Caller(gate.fail) for gate in counting], order)
            for gate in gates:
                if not any(gate is other for other in counting):
                    gate.fail()
    elif recipe["op"] == "check_all":

        def check() -> None:
            add("allowed", int(check_all(_entries(vector, limiters)).allowed))

        turns.run([_Caller(check) for _ in range(n)], order)
    else:
        pendings: list[Any] = [None] * n

        def reserve(index: int) -> None:
            pendings[index] = reserve_all(_entries(vector, limiters))

        turns.run([_Caller(lambda i=i: reserve(i)) for i in range(n)], order)
        opened = [pending for pending in pendings if pending.allowed]
        count["allowed"] = len(opened)
        # The first `commit` allowed ones commit, which calls nothing; every other one cancels.
        cancelling = opened[recipe["commit"] :]
        turns.run([_Caller(pending.cancel) for pending in cancelling], order)
        for pending in pendings:
            if any(pending is other for other in cancelling):
                continue
            if pending.allowed:
                pending.commit()
            else:
                pending.cancel()

    assert next(order, None) is None, "the port made fewer store calls than the model"
    assert turns.violations == []
    return count["allowed"], count["verify_ran"], _levels(vector, inner, seen)


class _AsyncCaller:
    def __init__(self, work: Callable[[], Awaitable[Any]]) -> None:
        self.work = work
        self.job: asyncio.Future[Any] | None = None
        self.release: asyncio.Future[None] | None = None
        self.at_gate = asyncio.Event()


class AsyncTurns:
    """`Turns` on one event loop: whoever calls the store while a caller has the turn is it."""

    def __init__(self) -> None:
        self._current: _AsyncCaller | None = None
        self.violations: list[str] = []

    async def gate(self, op: str) -> None:
        caller = self._current
        if caller is None:
            self.violations.append(f"{op}: a store call the model does not make")
            return
        if caller.release is not None:
            self.violations.append(f"{op}: two store calls at once from one caller")
        caller.release = asyncio.get_running_loop().create_future()
        caller.at_gate.set()
        await caller.release

    async def run(self, callers: list[_AsyncCaller], picks: Iterator[int]) -> None:
        live = list(callers)
        while live:
            pick = next(picks)
            caller = live[pick]
            self._current = caller
            caller.at_gate.clear()
            if caller.job is None:
                caller.job = asyncio.ensure_future(caller.work())
            else:
                release, caller.release = caller.release, None
                assert release is not None
                release.set_result(None)
            reached = asyncio.ensure_future(caller.at_gate.wait())
            await asyncio.wait(
                {caller.job, reached}, return_when=asyncio.FIRST_COMPLETED, timeout=_WAIT_S
            )
            reached.cancel()
            self._current = None
            if caller.job.done():
                caller.job.result()
                live.pop(pick)
            elif not caller.at_gate.is_set():
                raise TimeoutError("a caller neither finished nor reached the store")


async def run_order_async(vector: dict[str, Any], picks: list[int]) -> Result:
    """`run_order` for the async limiter and recipes."""
    turns = AsyncTurns()
    seen: dict[str, Any] = {}
    inner, limiters = _limiters(vector, turns.gate, seen, is_async=True)
    recipe, n = vector["recipe"], vector["requests"]
    order = iter(picks)
    count = {"allowed": 0, "verify_ran": 0}

    if recipe["op"] in ("gate", "attempt"):
        lockout, mode = _lockout(vector, limiters, is_async=True)
        subject = tuple(recipe["ids"])
        if recipe["op"] == "attempt":

            async def verify() -> bool:
                count["verify_ran"] += 1
                return bool(recipe["verify"])

            async def attempt() -> None:
                # Await first: `count[...] += await ...` reads the count before it waits.
                result = await lockout.attempt(subject, verify)
                count["allowed"] += int(result.allowed)

            await turns.run([_AsyncCaller(attempt) for _ in range(n)], order)
        else:
            gates: list[Any] = [None] * n

            async def begin(index: int) -> None:
                gates[index] = await lockout.begin(subject)

            await turns.run([_AsyncCaller(lambda i=i: begin(i)) for i in range(n)], order)
            count["allowed"] = sum(gate.allowed for gate in gates)
            counting = [gate for gate in gates if mode == "advisory" and gate.allowed]
            await turns.run([_AsyncCaller(gate.fail) for gate in counting], order)
            for gate in gates:
                if not any(gate is other for other in counting):
                    await gate.fail()
    elif recipe["op"] == "check_all":

        async def check() -> None:
            result = await check_all_async(_entries(vector, limiters))
            count["allowed"] += int(result.allowed)

        await turns.run([_AsyncCaller(check) for _ in range(n)], order)
    else:
        pendings: list[Any] = [None] * n

        async def reserve(index: int) -> None:
            pendings[index] = await reserve_all_async(_entries(vector, limiters))

        await turns.run([_AsyncCaller(lambda i=i: reserve(i)) for i in range(n)], order)
        opened = [pending for pending in pendings if pending.allowed]
        count["allowed"] = len(opened)
        cancelling = opened[recipe["commit"] :]
        await turns.run([_AsyncCaller(pending.cancel) for pending in cancelling], order)
        for pending in pendings:
            if any(pending is other for other in cancelling):
                continue
            if pending.allowed:
                await pending.commit()
            else:
                await pending.cancel()

    assert next(order, None) is None, "the port made fewer store calls than the model"
    assert turns.violations == []
    return count["allowed"], count["verify_ran"], _levels(vector, inner, seen)
