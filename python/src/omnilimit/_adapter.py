"""What every framework adapter shares: the denial and what to do without a key."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

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


PROBLEM_JSON = "application/problem+json"
"""The media type of a problem details body."""

_PROBLEM_TITLES = {400: "Bad Request", 429: "Too Many Requests", 503: "Service Unavailable"}
_PROBLEM_DETAILS: dict[str, str] = {
    "rate_limited": "Rate limit exceeded.",
    "rate_limiter_unavailable": "The rate limiter is unavailable.",
    "client_not_identified": "The client could not be identified.",
}


def problem_details(denial: Denial) -> dict[str, Any]:
    """A denial as a problem details object (RFC 9457). Send it as `application/problem+json`.

    `error` is the code to act on, the same one the default body carries. `retry_after` is the
    number of the `Retry-After` header, when there is one. The members and their order are part
    of the spec.
    """
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": _PROBLEM_TITLES.get(denial.status, "Error"),
        "status": denial.status,
        "detail": _PROBLEM_DETAILS[denial.error],
        "error": denial.error,
    }
    if "retry-after" in denial.headers:
        body["retry_after"] = int(denial.headers["retry-after"])
    return body


def problem_body(denial: Denial) -> bytes:
    """`problem_details` as the bytes to send: no whitespace, the same in every port."""
    return json.dumps(problem_details(denial), separators=(",", ":")).encode()


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
