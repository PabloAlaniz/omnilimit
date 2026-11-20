"""`Limiter` and `AsyncLimiter`. The decision logic is shared; only store calls differ."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Literal, TypeVar

from omnilimit._gcra import assert_cost, assert_scope, resolve_policy, view_of
from omnilimit._headers import build_headers
from omnilimit._key import derive_key
from omnilimit._types import (
    AsyncStore,
    Decision,
    EventHook,
    HeaderInput,
    HeaderOption,
    HitParams,
    LimiterEvent,
    OnStoreError,
    Op,
    RawHit,
    Reservation,
    Reserved,
    Store,
    Tier,
)

_DEFAULT_STORE_TIMEOUT_MS = 1000
_DEFAULT_MAX_STORE_THREADS = 32
_STORE_ERROR_RETRY_MS = 1000

_T = TypeVar("_T")


def _timeout_for(store: Any, given: int | None) -> int:
    """The limiter's timeout: what the caller gave, else the store's own default, else 1000."""
    if given is not None:
        return given
    default = getattr(store, "default_timeout_ms", None)
    return default if isinstance(default, int) else _DEFAULT_STORE_TIMEOUT_MS


def _store_method(store: Any, op: Literal["peek", "reset", "refund"]) -> Any:
    """The store's optional method, or a `TypeError`: a missing one is a configuration error."""
    method = getattr(store, op, None)
    if not callable(method):
        raise TypeError(f"omnilimit: this store does not support {op}")
    return method


class _Core:
    """Everything that does no I/O: validation, keys and turning hits into decisions."""

    def __init__(
        self,
        *,
        tier: Tier,
        scope: str,
        limit: int,
        window_ms: int,
        burst: int | None,
        on_store_error: OnStoreError,
        store_timeout_ms: int,
        headers: HeaderOption,
        salt: str,
        on_event: EventHook | None,
    ) -> None:
        self.policy = resolve_policy(limit, window_ms, burst)
        assert_scope(scope)
        if on_store_error not in ("open", "closed"):
            raise ValueError("omnilimit: on_store_error is required: 'open' or 'closed'")
        if store_timeout_ms <= 0:
            raise ValueError("omnilimit: store_timeout_ms must be positive")
        self.tier: Tier = tier
        self.scope = scope
        self.on_store_error: OnStoreError = on_store_error
        self.timeout_s = store_timeout_ms / 1000
        self.headers = headers
        self.salt = salt
        self.on_event = on_event
        self._spend_lock = threading.Lock()

    def key(self, id_value: str) -> str:
        return derive_key(self.scope, self.policy, self.salt, id_value)

    def _emit(self, event: LimiterEvent) -> None:
        if self.on_event is None:
            return
        # An observability hook must never change a rate limit decision.
        with contextlib.suppress(Exception):
            self.on_event(event)

    def _finish(self, decision: Decision, effective_ms: int) -> Decision:
        data = HeaderInput(
            scope=self.scope,
            limit=self.policy.limit,
            window_ms=self.policy.window_ms,
            decision=decision,
            effective_ms=effective_ms,
        )
        built = self.headers(data) if callable(self.headers) else build_headers(self.headers, data)
        return replace(decision, headers=built)

    def weighed(self, cost: int) -> HitParams:
        """What the store receives for a request of this cost.

        Stores add `window_ms` per hit, so a request that weighs `cost` units is the same hit
        with `cost * window_ms` in its place.
        """
        if cost == 1 and not isinstance(cost, bool):
            return self.policy
        assert_cost(cost, self.policy)
        if self.tier == "approximate":
            raise TypeError(
                "omnilimit: this store (tier approximate) only counts requests: "
                "it cannot weigh a cost"
            )
        policy = self.policy
        return HitParams(
            limit=policy.limit,
            window_ms=policy.window_ms * cost,
            capacity=policy.capacity,
            drain_ms=policy.drain_ms,
        )

    def from_hit(self, hit: RawHit, op: Op = "check", amount: int | None = None) -> Decision:
        event_type: Literal["allowed", "limited"] = "allowed" if hit.allowed else "limited"
        self._emit(LimiterEvent(event_type, self.scope, self.tier, op=op))
        base = Decision(
            allowed=hit.allowed,
            reason="ok" if hit.allowed else "limited",
            status=None if hit.allowed else 429,
            remaining=None,
            reset_ms=None,
            retry_after_ms=None,
            tier=self.tier,
        )
        if hit.level is None:
            retry_after_ms = None
            if not hit.allowed:
                retry_after_ms = (
                    hit.retry_after_ms if hit.retry_after_ms is not None else self.policy.window_ms
                )
            return self._finish(replace(base, retry_after_ms=retry_after_ms), retry_after_ms or 0)
        view = view_of(hit.allowed, hit.level, self.policy, amount)
        decision = replace(
            base,
            remaining=view.remaining,
            reset_ms=view.reset_ms,
            retry_after_ms=view.retry_after_ms,
        )
        return self._finish(decision, view.effective_ms)

    def from_failure(self, error: Exception, op: Op = "check") -> Decision:
        self.report_failure(error, op)
        # Reports nothing about quota: the store could not say anything about it.
        allowed = self.on_store_error == "open"
        decision = Decision(
            allowed=allowed,
            reason="store_error",
            status=None if allowed else 503,
            remaining=None,
            reset_ms=None,
            retry_after_ms=None if allowed else _STORE_ERROR_RETRY_MS,
            tier=self.tier,
        )
        return self._finish(decision, 0)

    def report_failure(self, error: Exception, op: Op) -> None:
        self._emit(LimiterEvent("store_error", self.scope, self.tier, error, op=op))

    def report_reset(self) -> None:
        self._emit(LimiterEvent("reset", self.scope, self.tier, op="reset"))

    def report_refund(self) -> None:
        self._emit(LimiterEvent("refund", self.scope, self.tier, op="refund"))

    def reserved(
        self, decision: Decision, hit: RawHit | None, key: str, params: HitParams, cost: int
    ) -> Reserved:
        # Only what the store counted can be given back: not a denial, not an `open` failure.
        if hit is None or not hit.allowed or hit.ts is None:
            return Reserved(decision, None)
        reservation = Reservation(
            owner=self, scope=self.scope, cost=cost, key=key, params=params, since=hit.ts
        )
        return Reserved(decision, reservation)

    def spend(self, reservation: Reservation | None) -> Reservation | None:
        """Mark a reservation refunded, returning it, or `None` when nothing is left to refund."""
        if reservation is None:
            return None
        if not isinstance(reservation, Reservation) or reservation._owner is not self:
            raise TypeError("omnilimit: this reservation was not made by this limiter")
        # Spent before the store call, so a second refund, even a concurrent one, does nothing.
        with self._spend_lock:
            if reservation._spent:
                return None
            reservation._spent = True
            return reservation


def _run_with_timeout(
    call: Callable[[], _T], timeout_s: float, slots: threading.BoundedSemaphore
) -> _T:
    """Run a blocking call, giving up after `timeout_s`.

    Python cannot abandon a blocking call, so it runs in its own daemon thread: if it hangs,
    the request moves on and interpreter exit is not held up. `slots` caps those threads,
    so a store that hangs turns into timeouts instead of an unbounded pile of threads.
    """
    deadline = time.monotonic() + timeout_s
    if not slots.acquire(timeout=timeout_s):
        raise TimeoutError("omnilimit: too many store calls in flight")

    outcome: list[_T | Exception] = []
    finished = threading.Event()

    def work() -> None:
        try:
            outcome.append(call())
        except Exception as error:
            outcome.append(error)
        finally:
            slots.release()
            finished.set()

    try:
        threading.Thread(target=work, name="omnilimit-store", daemon=True).start()
    except RuntimeError:
        slots.release()
        raise
    if not finished.wait(max(0.0, deadline - time.monotonic())):
        raise TimeoutError(f"omnilimit: store timed out after {timeout_s}s")
    result = outcome[0]
    if isinstance(result, Exception):
        raise result
    return result


class Limiter:
    """A limiter for blocking code: Flask, Django, `def` dependencies in FastAPI.

    `on_store_error` is required on purpose: a login route usually wants `closed`, a public
    read route usually wants `open`. Configure socket timeouts on your store's client too.
    """

    def __init__(
        self,
        *,
        store: Store,
        scope: str,
        limit: int,
        window_ms: int,
        on_store_error: OnStoreError,
        burst: int | None = None,
        store_timeout_ms: int | None = None,
        max_store_threads: int = _DEFAULT_MAX_STORE_THREADS,
        headers: HeaderOption = "draft-11",
        salt: str = "",
        on_event: EventHook | None = None,
    ) -> None:
        if inspect.iscoroutinefunction(store.hit):
            raise TypeError("omnilimit: this store is async: use AsyncLimiter")
        if max_store_threads < 1:
            raise ValueError("omnilimit: max_store_threads must be positive")
        self._store = store
        self._core = _Core(
            tier=store.tier,
            scope=scope,
            limit=limit,
            window_ms=window_ms,
            burst=burst,
            on_store_error=on_store_error,
            store_timeout_ms=_timeout_for(store, store_timeout_ms),
            headers=headers,
            salt=salt,
            on_event=on_event,
        )
        self._slots = threading.BoundedSemaphore(max_store_threads)

    @property
    def scope(self) -> str:
        return self._core.scope

    @property
    def tier(self) -> Tier:
        return self._core.tier

    def _call(self, call: Callable[[], _T]) -> _T:
        # A local store has no I/O, so it cannot hang and does not need a thread.
        if self._core.tier == "local":
            return call()
        return _run_with_timeout(call, self._core.timeout_s, self._slots)

    def _decide_raw(
        self, op: Op, call: Callable[[], RawHit], amount: int | None = None
    ) -> tuple[Decision, RawHit | None]:
        try:
            hit = self._call(call)
        except Exception as error:
            return self._core.from_failure(error, op), None
        return self._core.from_hit(hit, op, amount), hit

    def _decide(self, op: Op, call: Callable[[], RawHit], amount: int | None = None) -> Decision:
        return self._decide_raw(op, call, amount)[0]

    def check(self, id_value: str, *, now_ms: int | None = None, cost: int = 1) -> Decision:
        """Count one request for `id_value`.

        `cost` makes it weigh several units, such as a batch of items: an integer from 1 to the
        burst, anything else raises `ValueError`. A denied request counts nothing, whatever its
        cost. `now_ms` exists for conformance vectors only.
        """
        params = self._core.weighed(cost)
        key = self._core.key(id_value)
        return self._decide("check", lambda: self._store.hit(key, params, now_ms), params.window_ms)

    def peek(self, id_value: str, *, now_ms: int | None = None, cost: int = 1) -> Decision:
        """The decision `check` would give now, without counting.

        When allowed, `remaining` includes the request asked about. It is advisory: a concurrent
        request can take the last unit, so the `check` that follows is still the authority.
        Raises `TypeError` if the store cannot peek; store failures follow `on_store_error`.
        """
        peek = _store_method(self._store, "peek")
        params = self._core.weighed(cost)
        key = self._core.key(id_value)
        return self._decide("peek", lambda: peek(key, params, now_ms), params.window_ms)

    def reset(self, id_value: str) -> bool:
        """Forget everything counted for `id_value`.

        Returns `True` once deleted (also when there was nothing to delete) and `False` if the
        store failed or timed out, which is reported through `on_event`; it never raises for a
        store failure. Raises `TypeError` if the store cannot reset.
        """
        reset = _store_method(self._store, "reset")
        key = self._core.key(id_value)
        try:
            self._call(lambda: reset(key))
        except Exception as error:
            self._core.report_failure(error, "reset")
            return False
        self._core.report_reset()
        return True

    def reserve(self, id_value: str, *, now_ms: int | None = None, cost: int = 1) -> Reserved:
        """`check`, keeping what is needed to give the request back with `refund`.

        The decision is the one `check` would give. `reservation` is `None` when nothing was
        counted. Raises `TypeError` if the store cannot refund, before counting anything.
        """
        _store_method(self._store, "refund")
        params = self._core.weighed(cost)
        key = self._core.key(id_value)
        decision, hit = self._decide_raw(
            "check", lambda: self._store.hit(key, params, now_ms), params.window_ms
        )
        return self._core.reserved(decision, hit, key, params, cost)

    def refund(self, reservation: Reservation | None, *, now_ms: int | None = None) -> bool:
        """Give back a reservation, as far as it has not drained yet.

        A request refunded at once leaves no trace; one refunded after the meter drained its
        units gives nothing. Returns `True` once applied, and `False` for `None`, for a
        reservation already refunded (without calling the store), or if the store failed or timed
        out, which `on_event` reports. It never raises for a store failure and never retries: a
        refund that timed out may have been applied, and the reservation is spent either way.
        Raises `TypeError` for a reservation from another limiter.
        """
        held = self._core.spend(reservation)
        if held is None:
            return False
        refund = _store_method(self._store, "refund")
        try:
            self._call(lambda: refund(held._key, held._params, held._since, now_ms))
        except Exception as error:
            self._core.report_failure(error, "refund")
            return False
        self._core.report_refund()
        return True


class AsyncLimiter:
    """A limiter for event loops. Accepts async stores and `MemoryStore`, which never blocks."""

    def __init__(
        self,
        *,
        store: AsyncStore | Store,
        scope: str,
        limit: int,
        window_ms: int,
        on_store_error: OnStoreError,
        burst: int | None = None,
        store_timeout_ms: int | None = None,
        headers: HeaderOption = "draft-11",
        salt: str = "",
        on_event: EventHook | None = None,
    ) -> None:
        if not inspect.iscoroutinefunction(store.hit) and store.tier != "local":
            raise TypeError("omnilimit: this store would block the event loop: use its async form")
        self._store: Any = store
        self._core = _Core(
            tier=store.tier,
            scope=scope,
            limit=limit,
            window_ms=window_ms,
            burst=burst,
            on_store_error=on_store_error,
            store_timeout_ms=_timeout_for(store, store_timeout_ms),
            headers=headers,
            salt=salt,
            on_event=on_event,
        )

    @property
    def scope(self) -> str:
        return self._core.scope

    @property
    def tier(self) -> Tier:
        return self._core.tier

    async def _call(self, call: Callable[[], Any]) -> Any:
        result = call()
        if inspect.isawaitable(result):
            return await asyncio.wait_for(result, self._core.timeout_s)
        return result

    async def _decide_raw(
        self, op: Op, call: Callable[[], Any], amount: int | None = None
    ) -> tuple[Decision, RawHit | None]:
        try:
            hit: RawHit = await self._call(call)
        except Exception as error:
            return self._core.from_failure(error, op), None
        return self._core.from_hit(hit, op, amount), hit

    async def _decide(self, op: Op, call: Callable[[], Any], amount: int | None = None) -> Decision:
        return (await self._decide_raw(op, call, amount))[0]

    async def check(self, id_value: str, *, now_ms: int | None = None, cost: int = 1) -> Decision:
        """Count one request for `id_value`. See `Limiter.check`."""
        params = self._core.weighed(cost)
        key = self._core.key(id_value)
        return await self._decide(
            "check", lambda: self._store.hit(key, params, now_ms), params.window_ms
        )

    async def peek(self, id_value: str, *, now_ms: int | None = None, cost: int = 1) -> Decision:
        """The decision `check` would give now, without counting. See `Limiter.peek`."""
        peek = _store_method(self._store, "peek")
        params = self._core.weighed(cost)
        key = self._core.key(id_value)
        return await self._decide("peek", lambda: peek(key, params, now_ms), params.window_ms)

    async def reset(self, id_value: str) -> bool:
        """Forget everything counted for `id_value`. See `Limiter.reset`."""
        reset = _store_method(self._store, "reset")
        key = self._core.key(id_value)
        try:
            await self._call(lambda: reset(key))
        except Exception as error:
            self._core.report_failure(error, "reset")
            return False
        self._core.report_reset()
        return True

    async def reserve(self, id_value: str, *, now_ms: int | None = None, cost: int = 1) -> Reserved:
        """`check`, keeping what is needed to give the request back. See `Limiter.reserve`."""
        _store_method(self._store, "refund")
        params = self._core.weighed(cost)
        key = self._core.key(id_value)
        decision, hit = await self._decide_raw(
            "check", lambda: self._store.hit(key, params, now_ms), params.window_ms
        )
        return self._core.reserved(decision, hit, key, params, cost)

    async def refund(self, reservation: Reservation | None, *, now_ms: int | None = None) -> bool:
        """Give back a reservation, as far as it has not drained yet. See `Limiter.refund`."""
        held = self._core.spend(reservation)
        if held is None:
            return False
        refund = _store_method(self._store, "refund")
        try:
            await self._call(lambda: refund(held._key, held._params, held._since, now_ms))
        except Exception as error:
            self._core.report_failure(error, "refund")
            return False
        self._core.report_refund()
        return True
