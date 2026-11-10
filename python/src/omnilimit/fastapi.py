"""FastAPI dependency.

`rate_limit` raises `HTTPException` on denial, so your own error handlers format the response.
To build the denial response yourself, pass `on_denied` and register `denied_handler` for
`RateLimitDenied`. When the request passes, headers go on the injected `Response`: FastAPI does
not copy them if the route returns a `Response` object of its own, as a file download usually does.
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

DenialError = Literal["rate_limited", "rate_limiter_unavailable", "client_not_identified"]


@dataclass(frozen=True, slots=True)
class Denial:
    """What `on_denied` receives. `decision` is `None` for `client_not_identified`."""

    status: int
    headers: dict[str, str]
    error: DenialError
    decision: Decision | None


OnDenied = Callable[[Request, Denial], "Awaitable[Response] | Response"]
"""Builds the response for a denied request. The default raises `HTTPException`."""


class RateLimitDenied(HTTPException):
    """Raised with the response `on_denied` built. `denied_handler` sends it."""

    def __init__(self, denial: Denial, response: Response) -> None:
        super().__init__(status_code=denial.status, detail=denial.error, headers=denial.headers)
        self.denial = denial
        self.response = response


async def denied_handler(request: Request, exc: Exception) -> Response:
    """`app.add_exception_handler(RateLimitDenied, denied_handler)`, needed by `on_denied`."""
    if not isinstance(exc, RateLimitDenied):  # pragma: no cover - registered for it alone
        raise exc
    return exc.response


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


def _missing(on_missing_key: MissingKeyPolicy) -> Denial | str | None:
    """The id to count a keyless request under, `None` to skip the limit, or a denial."""
    if on_missing_key == "skip":
        return None
    if on_missing_key == "reject":
        return Denial(status=400, headers={}, error="client_not_identified", decision=None)
    return _SHARED_ID


def _denial(decision: Decision) -> Denial:
    error: DenialError = (
        "rate_limiter_unavailable" if decision.reason == "store_error" else "rate_limited"
    )
    return Denial(
        status=decision.status or 429, headers=decision.headers, error=error, decision=decision
    )


def _default_denial(denial: Denial) -> HTTPException:
    return HTTPException(status_code=denial.status, detail=denial.error, headers=denial.headers)


def _check_handler(request: Request) -> None:
    # Without the handler, FastAPI would answer the default HTTPException body and drop the
    # response the hook built: fail loudly instead.
    if RateLimitDenied not in request.app.exception_handlers:
        raise TypeError(
            "omnilimit: on_denied needs app.add_exception_handler(RateLimitDenied, denied_handler)"
        )


def _pass(decision: Decision, response: Response) -> None:
    for name, value in decision.headers.items():
        response.headers[name] = value


def rate_limit(
    limiter: Limiter | AsyncLimiter,
    *,
    key: KeyFn,
    on_missing_key: MissingKeyPolicy,
    on_denied: OnDenied | None = None,
) -> Callable[..., Any]:
    """Build a dependency: `Depends(rate_limit(limiter, key=..., on_missing_key=...))`.

    `on_missing_key` is required so a misconfigured proxy cannot pass unnoticed. `on_denied`
    builds the response for every denial (429, 503 and 400); it needs `denied_handler`.
    """
    if on_missing_key not in ("shared", "skip", "reject"):
        raise ValueError("omnilimit: on_missing_key must be 'shared', 'skip' or 'reject'")

    if isinstance(limiter, AsyncLimiter):

        async def deny_async(request: Request, denial: Denial) -> HTTPException:
            if on_denied is None:
                return _default_denial(denial)
            _check_handler(request)
            built = on_denied(request, denial)
            return RateLimitDenied(denial, await built if inspect.isawaitable(built) else built)

        async def async_dependency(request: Request, response: Response) -> None:
            result = key(_context(request))
            id_value = await result if inspect.isawaitable(result) else result
            target = _missing(on_missing_key) if id_value is None else id_value
            if isinstance(target, Denial):
                raise await deny_async(request, target)
            if target is None:
                return
            decision = await limiter.check(target)
            if not decision.allowed:
                raise await deny_async(request, _denial(decision))
            _pass(decision, response)

        return async_dependency

    # A plain `def`: FastAPI runs it in its thread pool, so a blocking store is fine.
    def deny(request: Request, denial: Denial) -> HTTPException:
        if on_denied is None:
            return _default_denial(denial)
        _check_handler(request)
        built: Any = on_denied(request, denial)
        if inspect.iscoroutine(built):
            built.close()
            raise TypeError("omnilimit: an async on_denied needs an AsyncLimiter")
        return RateLimitDenied(denial, built)

    def dependency(request: Request, response: Response) -> None:
        result: Any = key(_context(request))
        if inspect.iscoroutine(result):
            result.close()
            raise TypeError("omnilimit: an async key function needs an AsyncLimiter")
        target = _missing(on_missing_key) if result is None else result
        if isinstance(target, Denial):
            raise deny(request, target)
        if target is None:
            return
        decision = limiter.check(target)
        if not decision.allowed:
            raise deny(request, _denial(decision))
        _pass(decision, response)

    return dependency
