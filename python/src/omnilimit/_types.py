"""Public data types. See spec/SPEC.md."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

Tier = Literal["exact", "local", "approximate"]
Reason = Literal["ok", "limited", "store_error"]
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
    """A blocking store. It must apply the hit atomically and use its own clock."""

    tier: Tier

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit: ...


@runtime_checkable
class AsyncStore(Protocol):
    """Same contract as `Store`, for event loops."""

    tier: Tier

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
    """Observability event. `error` is only set for `store_error`."""

    type: Literal["allowed", "limited", "store_error"]
    scope: str
    tier: Tier
    error: Exception | None = None


HeaderOption = HeaderProfile | Callable[[HeaderInput], dict[str, str]]
EventHook = Callable[[LimiterEvent], None]
