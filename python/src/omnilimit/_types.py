"""Public data types. See spec/SPEC.md."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

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


class _Deferred:
    """A slot beside the fields of `Decision`, for what its headers will be built from."""

    __slots__ = ("_deferred",)


@dataclass(frozen=True, slots=True)
class Decision(_Deferred):
    """The outcome of one check. `status` is what to answer with when denied.

    `shadowed` is `True` when a limiter in shadow mode let the request through although it would
    have denied it: `allowed` is then `True` and `status` `None`, while `reason`, `remaining`,
    `reset_ms` and `retry_after_ms` keep the values of the denial.
    """

    allowed: bool
    reason: Reason
    status: Literal[429, 503] | None
    remaining: int | None
    reset_ms: int | None
    retry_after_ms: int | None
    tier: Tier
    headers: dict[str, str] = field(default_factory=dict)
    shadowed: bool = False

    # Hidden from type checkers, which would otherwise accept any attribute on a decision.
    if not TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any:
            # Python only comes here for a slot that holds nothing, and a limiter leaves just
            # `headers` empty: most checks never read them, so the first read builds them. They
            # depend on nothing but the decision, so the caller cannot tell when that happened.
            if name != "headers":
                raise AttributeError(
                    f"{type(self).__name__!r} object has no attribute {name!r}",
                    name=name,
                    obj=self,
                )
            try:
                source, profile, effective_ms = _deferred_of(self)
            except AttributeError:
                built = {}
            else:
                built = source.profile_headers(profile, self, effective_ms)
            _set(self, "headers", built)
            return built


_set = object.__setattr__
_new = object.__new__
_deferred_of = _Deferred._deferred.__get__  # type: ignore[attr-defined]


class _Unset:
    """The type of `_UNSET`: no headers given, which is not the same as `None` given."""


_UNSET = _Unset()


class _Draft(_Deferred):
    """A `Decision` that is still being written: the same slots, without the frozen writes."""

    __slots__ = Decision.__slots__

    allowed: bool
    reason: Reason
    status: Literal[429, 503] | None
    remaining: int | None
    reset_ms: int | None
    retry_after_ms: int | None
    tier: Tier
    headers: dict[str, str]
    shadowed: bool


def make_decision(
    allowed: bool,
    reason: Reason,
    status: Literal[429, 503] | None,
    remaining: int | None,
    reset_ms: int | None,
    retry_after_ms: int | None,
    tier: Tier,
    headers: dict[str, str] | _Unset = _UNSET,
) -> Decision:
    """What `Decision(...)` builds, for the limiter, which builds one per check.

    The `__init__` of a frozen dataclass pays an `object.__setattr__` per field. This writes the
    same slots on a class that is not frozen and then turns the object into a `Decision`, which
    Python allows between classes of one layout. Without `headers` they stay unset: empty, unless
    `defer_headers` says how to build them.
    """
    draft = _new(_Draft)
    draft.allowed = allowed
    draft.reason = reason
    draft.status = status
    draft.remaining = remaining
    draft.reset_ms = reset_ms
    draft.retry_after_ms = retry_after_ms
    draft.tier = tier
    draft.shadowed = False
    if headers is not _UNSET:
        draft.headers = cast("dict[str, str]", headers)
    draft.__class__ = Decision  # type: ignore[assignment]
    return cast(Decision, draft)


# `defer_headers(decision, (source, profile, effective_ms))` has `decision.headers` be
# `source.profile_headers(profile, decision, effective_ms)`, built on its first read.
defer_headers: Callable[[Decision, tuple[Any, Any, int]], None] = _Deferred._deferred.__set__  # type: ignore[attr-defined]


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

    `reservation` is `None` when nothing was counted: the request was denied, shadowed or not, or
    allowed by `on_store_error="open"` after a store failure.
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
    successful `reset` emits type `reset`, and a successful `refund` type `refund`. `shadowed` is
    `True` only on the event of a decision that shadow mode let through: a `limited` one, or a
    `store_error` under `on_store_error="closed"`.
    """

    type: Literal["allowed", "limited", "store_error", "reset", "refund"]
    scope: str
    tier: Tier
    error: Exception | None = None
    op: Op = "check"
    shadowed: bool = False


HeaderOption = HeaderProfile | Callable[[HeaderInput], dict[str, str]]
EventHook = Callable[[LimiterEvent], None]
