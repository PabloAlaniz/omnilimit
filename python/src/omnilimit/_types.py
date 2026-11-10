"""Public data types. See spec/SPEC.md."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

Tier = Literal["exact", "local", "approximate"]
Reason = Literal["ok", "limited", "store_error"]
Op = Literal["check", "peek", "reset"]
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
    """What a store answers. Approximate stores leave `level` and `ts` as `None`."""

    allowed: bool
    level: int | None
    ts: int | None
    retry_after_ms: int | None = None


@runtime_checkable
class Store(Protocol):
    """A blocking store. It must apply the hit atomically and use its own clock.

    A store may also offer two optional methods, which `Limiter.peek` and `Limiter.reset` need:
    `peek(key, params, now_ms=None) -> RawHit` answers what `hit` would, without writing, with
    the drained level and the stored timestamp (or now, with no bucket); `reset(key) -> None`
    deletes the bucket. Approximate stores cannot offer them.
    """

    # Read-only, so a store may narrow it to the one tier it promises.
    @property
    def tier(self) -> Tier: ...

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit: ...


@runtime_checkable
class AsyncStore(Protocol):
    """Same contract as `Store`, for event loops, including the optional async `peek` and
    `reset`."""

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

    `op` is the limiter method the event comes from. A successful `reset` emits type `reset`.
    """

    type: Literal["allowed", "limited", "store_error", "reset"]
    scope: str
    tier: Tier
    error: Exception | None = None
    op: Op = "check"


HeaderOption = HeaderProfile | Callable[[HeaderInput], dict[str, str]]
EventHook = Callable[[LimiterEvent], None]
