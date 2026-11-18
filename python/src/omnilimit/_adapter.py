"""What every framework adapter shares: the denial and what to do without a key."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from omnilimit._types import Decision

MissingKeyPolicy = Literal["shared", "skip", "reject"]
"""`shared`: everyone without a key shares a bucket. `skip`: no limit. `reject`: answer 400."""

SHARED_ID = "__omnilimit_shared__"

DenialError = Literal["rate_limited", "rate_limiter_unavailable", "client_not_identified"]


@dataclass(frozen=True, slots=True)
class Denial:
    """What `on_denied` receives. `decision` is `None` for `client_not_identified`."""

    status: int
    headers: dict[str, str]
    error: DenialError
    decision: Decision | None


def check_policy(on_missing_key: object) -> None:
    if on_missing_key not in ("shared", "skip", "reject"):
        raise ValueError("omnilimit: on_missing_key must be 'shared', 'skip' or 'reject'")


def missing_key(on_missing_key: MissingKeyPolicy) -> Denial | str | None:
    """The id to count a keyless request under, `None` to skip the limit, or a denial."""
    if on_missing_key == "skip":
        return None
    if on_missing_key == "reject":
        return Denial(status=400, headers={}, error="client_not_identified", decision=None)
    return SHARED_ID


def denial_of(decision: Decision) -> Denial:
    error: DenialError = (
        "rate_limiter_unavailable" if decision.reason == "store_error" else "rate_limited"
    )
    return Denial(
        status=decision.status or 429, headers=decision.headers, error=error, decision=decision
    )
