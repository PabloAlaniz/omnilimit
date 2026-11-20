"""Public data types. See spec/SPEC.md."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

Tier = Literal["exact", "local", "approximate"]
Reason = Literal["ok", "limited", "store_error"]
Op = Literal["check", "peek", "reset", "refund"]
OnStoreError = Literal["open", "closed"]
HeaderProfile = Literal["draft-11", "legacy", "none"]


@dataclass(frozen=True, slots=True)
class HitParams:
    """Integers computed once by the core, so stores never divide."""

    limit: int
    window_ms: int
    capacity: int
    drain_ms: int


@dataclass(frozen=True, slots=True)
class RawHit:
    """What a store answers. Approximate stores leave `level` and `ts` as `None`.

    `ts` is the timestamp the hit stored, on the store's clock: for an allowed hit, the `since`
    of its reservation.
    """

    allowed: bool
    level: int | None
    ts: int | None
    retry_after_ms: int | None = None


@dataclass(frozen=True, slots=True)
class RawRefund:
    """What a store answers to a refund: the level and timestamp it left, or found."""

    level: int
    ts: int


@runtime_checkable
class Store(Protocol):
    """A blocking store. It must apply the hit atomically and use its own clock.

    A store may also offer optional methods, which `Limiter.peek`, `Limiter.reset` and
    `Limiter.reserve` need: `peek(key, params, now_ms=None) -> RawHit` answers what `hit` would,
    without writing, with the drained level and the stored timestamp (or now, with no bucket);
    `reset(key) -> None` deletes the bucket; `refund(key, params, since, now_ms=None) ->
    RawRefund` gives back a hit counted at `since` as far as it has not drained, never below
    zero (see "Refund" in the spec). Approximate stores cannot offer them.

    A store may set `default_timeout_ms`, the timeout a limiter uses for it when
    `store_timeout_ms` is not given.
    """

    # Read-only, so a store may narrow it to the one tier it promises.
    @property
    def tier(self) -> Tier: ...

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit: ...


@runtime_checkable
class AsyncStore(Protocol):
    """Same contract as `Store`, for event loops, including the optional async `peek`, `reset`
    and `refund`."""

    @property
    def tier(self) -> Tier: ...

    async def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit: ...


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of one check. `status` is what to answer with when denied."""

    allowed: bool
    reason: Reason
    status: Literal[429, 503] | None
    remaining: int | None
    reset_ms: int | None
    retry_after_ms: int | None
    tier: Tier
    headers: dict[str, str] = field(default_factory=dict)


class Reservation:
    """A counted request that `refund` can give back once.

    Opaque: only the limiter that made it can refund it. `scope` and `cost` are for reading.
    """

    __slots__ = ("_key", "_owner", "_params", "_since", "_spent", "cost", "scope")

    scope: str
    cost: int

    def __init__(
        self, *, owner: object, scope: str, cost: int, key: str, params: HitParams, since: int
    ) -> None:
        self.scope = scope
        self.cost = cost
        self._owner = owner
        self._key = key
        self._params = params
        self._since = since
        self._spent = False

    def __repr__(self) -> str:
        return f"Reservation(scope={self.scope!r}, cost={self.cost})"


@dataclass(frozen=True, slots=True)
class Reserved:
    """What `reserve` answers: the decision `check` would give, and the reservation.

    `reservation` is `None` when nothing was counted: the request was denied, or allowed by
    `on_store_error="open"` after a store failure.
    """

    decision: Decision
    reservation: Reservation | None


@dataclass(frozen=True, slots=True)
class HeaderInput:
    """What a custom header function receives. `decision.headers` is still empty."""

    scope: str
    limit: int
    window_ms: int
    decision: Decision
    effective_ms: int


@dataclass(frozen=True, slots=True)
class LimiterEvent:
    """Observability event. `error` is only set for `store_error`.

    `op` is the limiter method the event comes from, and `reserve` reports as `check`. A
    successful `reset` emits type `reset`, and a successful `refund` type `refund`.
    """

    type: Literal["allowed", "limited", "store_error", "reset", "refund"]
    scope: str
    tier: Tier
    error: Exception | None = None
    op: Op = "check"


HeaderOption = HeaderProfile | Callable[[HeaderInput], dict[str, str]]
EventHook = Callable[[LimiterEvent], None]
