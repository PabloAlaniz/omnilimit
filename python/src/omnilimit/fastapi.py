"""FastAPI dependency.

`rate_limit` raises `HTTPException` on denial, so your own error handlers format the response.
When the request passes, headers go on the injected `Response`: FastAPI does not copy them if
the route returns a `Response` object of its own, as a file download usually does.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import HTTPException, Request, Response

from omnilimit._limiter import AsyncLimiter, Limiter
from omnilimit._types import Decision
from omnilimit.ip import IpPreset, mask_ip, resolve_client_ip

MissingKeyPolicy = Literal["shared", "skip", "reject"]
"""`shared`: everyone without a key shares a bucket. `skip`: no limit. `reject`: answer 400."""

_SHARED_ID = "__omnilimit_shared__"


@dataclass(frozen=True, slots=True)
class KeyContext:
    request: Request
    headers: Any
    peer: str | None


KeyFn = Callable[[KeyContext], "Awaitable[str | None] | str | None"]
"""Returns the bucket id, or `None` if it cannot be determined."""


def ip_key(
    *,
    preset: IpPreset | None = None,
    xff_hops: int | None = None,
    header: str | None = None,
    use_peer: bool = False,
    ipv6_prefix: int = 64,
) -> Callable[[KeyContext], str | None]:
    """Key requests by trusted client IP. See `omnilimit.ip.resolve_client_ip`."""

    def key(context: KeyContext) -> str | None:
        ip = resolve_client_ip(
            context.headers,
            context.peer,
            preset=preset,
            xff_hops=xff_hops,
            header=header,
            use_peer=use_peer,
        )
        return None if ip is None else mask_ip(ip, ipv6_prefix)

    return key


def _context(request: Request) -> KeyContext:
    peer = request.client.host if request.client is not None else None
    return KeyContext(request=request, headers=request.headers, peer=peer)


def _missing(on_missing_key: MissingKeyPolicy) -> str | None:
    """The id to count a keyless request under, or `None` to skip the limit."""
    if on_missing_key == "skip":
        return None
    if on_missing_key == "reject":
        raise HTTPException(status_code=400, detail="client_not_identified")
    return _SHARED_ID


def _apply(decision: Decision, response: Response) -> None:
    if not decision.allowed:
        detail = "rate_limiter_unavailable" if decision.reason == "store_error" else "rate_limited"
        raise HTTPException(
            status_code=decision.status or 429, detail=detail, headers=decision.headers
        )
    for name, value in decision.headers.items():
        response.headers[name] = value


def rate_limit(
    limiter: Limiter | AsyncLimiter, *, key: KeyFn, on_missing_key: MissingKeyPolicy
) -> Callable[..., Any]:
    """Build a dependency: `Depends(rate_limit(limiter, key=..., on_missing_key=...))`.

    `on_missing_key` is required so a misconfigured proxy cannot pass unnoticed.
    """
    if on_missing_key not in ("shared", "skip", "reject"):
        raise ValueError("omnilimit: on_missing_key must be 'shared', 'skip' or 'reject'")

    if isinstance(limiter, AsyncLimiter):

        async def async_dependency(request: Request, response: Response) -> None:
            result = key(_context(request))
            id_value = await result if inspect.isawaitable(result) else result
            if id_value is None:
                id_value = _missing(on_missing_key)
            if id_value is not None:
                _apply(await limiter.check(id_value), response)

        return async_dependency

    # A plain `def`: FastAPI runs it in its thread pool, so a blocking store is fine.
    def dependency(request: Request, response: Response) -> None:
        result: Any = key(_context(request))
        if inspect.iscoroutine(result):
            result.close()
            raise TypeError("omnilimit: an async key function needs an AsyncLimiter")
        id_value = _missing(on_missing_key) if result is None else result
        if id_value is not None:
            _apply(limiter.check(id_value), response)

    return dependency
