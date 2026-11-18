"""The algorithm, on integers only. Every division in this port lives in this file."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TypedDict

from omnilimit._types import HitParams, RawHit

MIN_WINDOW_MS = 1000
MAX_SAFE_INTEGER = 2**53 - 1
_SCOPE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def ceil_div(a: int, b: int) -> int:
    """`ceil(a / b)` for non-negative integers."""
    return (a + b - 1) // b


def _is_positive_int(value: object) -> bool:
    # bool is an int in Python, but `limit=True` is a bug rather than a limit of one.
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_SAFE_INTEGER


@dataclass(frozen=True, slots=True)
class ResolvedPolicy(HitParams):
    burst: int


def resolve_policy(limit: int, window_ms: int, burst: int | None = None) -> ResolvedPolicy:
    """Validate a policy and compute the integers shared with every store."""
    resolved_burst = limit if burst is None else burst
    if not _is_positive_int(limit):
        raise ValueError("omnilimit: limit must be a positive integer")
    if not _is_positive_int(resolved_burst):
        raise ValueError("omnilimit: burst must be a positive integer")
    if not _is_positive_int(window_ms) or window_ms < MIN_WINDOW_MS:
        raise ValueError(f"omnilimit: window_ms must be an integer of at least {MIN_WINDOW_MS}")
    # Python would not overflow, but the same policy must be valid in every language.
    if 2 * resolved_burst * window_ms + limit > MAX_SAFE_INTEGER:
        raise ValueError(
            "omnilimit: policy too large for exact integer arithmetic (2*burst*window_ms + limit)"
        )
    capacity = resolved_burst * window_ms
    return ResolvedPolicy(
        limit=limit,
        window_ms=window_ms,
        capacity=capacity,
        drain_ms=ceil_div(capacity, limit),
        burst=resolved_burst,
    )


def assert_scope(scope: str) -> None:
    if not isinstance(scope, str) or _SCOPE.fullmatch(scope) is None:
        raise ValueError("omnilimit: scope must match ^[A-Za-z0-9._-]{1,64}$")


_SHORTHAND = re.compile(r"([1-9][0-9]{0,14})/([1-9][0-9]{0,14})?(second|minute|hour|day)")
_SHORTHAND_UNITS = {"second": 1000, "minute": 60_000, "hour": 3_600_000, "day": 86_400_000}


class PolicyArgs(TypedDict):
    """What `parse_policy` returns: spread it into a limiter with `**`."""

    limit: int
    window_ms: int


def parse_policy(text: str) -> PolicyArgs:
    """Read a policy written as `<limit>/<count><unit>`: `10/minute`, `5/15minute`, `1000/day`.

    The grammar is strict on purpose, so a value from configuration means the same in every
    port: see "Policy shorthand" in the spec. Raises `ValueError` on anything else.
    """
    match = _SHORTHAND.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise ValueError(
            'omnilimit: a policy shorthand looks like "10/minute" or "5/15minute" '
            "(second, minute, hour, day)"
        )
    limit = int(match[1])
    window_ms = int(match[2] or "1") * _SHORTHAND_UNITS[match[3]]
    resolve_policy(limit, window_ms)
    return PolicyArgs(limit=limit, window_ms=window_ms)


@dataclass(frozen=True, slots=True)
class MeterState:
    level: int
    ts: int


def apply_hit(
    state: MeterState | None, params: HitParams, now_ms: int
) -> tuple[RawHit, MeterState | None]:
    """Return the hit and the state to persist, which is `None` on denial: denials never write."""
    ts = state.ts if state is not None else now_ms
    level = state.level if state is not None else 0
    t = max(now_ms, ts)
    elapsed = min(t - ts, params.drain_ms)
    drained = max(0, level - elapsed * params.limit)

    if drained + params.window_ms > params.capacity:
        return RawHit(allowed=False, level=drained, ts=ts), None
    new_level = drained + params.window_ms
    return RawHit(allowed=True, level=new_level, ts=t), MeterState(level=new_level, ts=t)


def apply_peek(state: MeterState | None, params: HitParams, now_ms: int) -> RawHit:
    """The answer `apply_hit` would give now, without a state to write.

    `level` is the drained level before the hit and `ts` the stored timestamp, or `now_ms`
    when there is no bucket.
    """
    ts = state.ts if state is not None else now_ms
    level = state.level if state is not None else 0
    t = max(now_ms, ts)
    elapsed = min(t - ts, params.drain_ms)
    drained = max(0, level - elapsed * params.limit)
    return RawHit(allowed=drained + params.window_ms <= params.capacity, level=drained, ts=ts)


@dataclass(frozen=True, slots=True)
class MeterView:
    remaining: int
    reset_ms: int
    retry_after_ms: int | None
    effective_ms: int


def view_of(allowed: bool, level: int, params: HitParams) -> MeterView:
    """Derive the public numbers from a level."""
    remaining = (params.capacity - level) // params.window_ms
    reset_ms = ceil_div(level, params.limit)
    next_unit_ms = ceil_div(max(0, level + params.window_ms - params.capacity), params.limit)
    if not allowed:
        return MeterView(remaining, reset_ms, next_unit_ms, next_unit_ms)
    return MeterView(remaining, reset_ms, None, next_unit_ms if remaining == 0 else reset_ms)
