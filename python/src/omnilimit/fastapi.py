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
from typing import Any

from fastapi import Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from omnilimit._adapter import (
    PROBLEM_JSON,
    Denial,
    DenialError,
    MissingKeyPolicy,
    check_policy,
    problem_body,
    problem_details,
)
from omnilimit._adapter import (
    denial_of as _denial,
)
from omnilimit._adapter import (
    missing_key as _missing,
)
from omnilimit._limiter import AsyncLimiter, Limiter
from omnilimit._types import Decision
from omnilimit.ip import IpPreset, client_key

__all__ = [
    "PROBLEM_JSON",
    "CostFn",
    "Denial",
    "DenialError",
    "KeyContext",
    "KeyFn",
    "LimiterFn",
    "MissingKeyPolicy",
    "OnDenied",
    "RateLimitDenied",
    "SkipFn",
    "denied_handler",
    "ip_key",
    "problem_details",
    "rate_limit",
]

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
    identity: Any = None
    """What the `identity` dependency of `rate_limit` returned, or `None` without one."""


KeyFn = Callable[[KeyContext], "Awaitable[str | None] | str | None"]
"""Returns the bucket id, or `None` if it cannot be determined."""

LimiterFn = Callable[[KeyContext], "Awaitable[Limiter | AsyncLimiter] | Limiter | AsyncLimiter"]
"""Finds the limiter for a request, for example one per plan. It runs on every request."""

CostFn = Callable[[KeyContext], "Awaitable[int] | int"]
"""Returns how many units the request weighs, from 1 to the limiter's burst."""

SkipFn = Callable[[KeyContext], "Awaitable[bool] | bool"]
"""Returns `True` to let the request through without counting it."""


def ip_key(
    *,
    preset: IpPreset | None = None,
    xff_hops: int | None = None,
    forwarded_hops: int | None = None,
    header: str | None = None,
    use_peer: bool = False,
    ipv6_prefix: int = 64,
) -> Callable[[KeyContext], str | None]:
    """Key requests by trusted client IP. See `omnilimit.ip.resolve_client_ip`."""

    def key(context: KeyContext) -> str | None:
        return client_key(
            context.headers,
            context.peer,
            preset=preset,
            xff_hops=xff_hops,
            forwarded_hops=forwarded_hops,
            header=header,
            use_peer=use_peer,
            ipv6_prefix=ipv6_prefix,
        )

    return key


def _context(request: Request, identity: Any = None) -> KeyContext:
    peer = request.client.host if request.client is not None else None
    return KeyContext(request=request, headers=request.headers, peer=peer, identity=identity)


def _default_denial(denial: Denial) -> HTTPException:
    return HTTPException(status_code=denial.status, detail=denial.error, headers=denial.headers)


def _check_handler(request: Request, option: str = "on_denied") -> None:
    # Without the handler, FastAPI would answer the default HTTPException body and drop the
    # response that was built: fail loudly instead.
    if RateLimitDenied not in request.app.exception_handlers:
        raise TypeError(
            f"omnilimit: {option} needs app.add_exception_handler(RateLimitDenied, denied_handler)"
        )


def _problem(request: Request, denial: Denial) -> HTTPException:
    _check_handler(request, "problem_details")
    response = Response(
        content=problem_body(denial),
        status_code=denial.status,
        media_type=PROBLEM_JSON,
        headers=denial.headers,
    )
    return RateLimitDenied(denial, response)


def _pass(decision: Decision, response: Response) -> None:
    for name, value in decision.headers.items():
        response.headers[name] = value


def _with_identity(
    run: Callable[..., Any], identity: Callable[..., Any] | None
) -> Callable[..., Any]:
    """Give `run` the signature FastAPI reads: with the identity dependency only if there is one."""
    resolve = Depends(identity) if identity is not None else None
    if inspect.iscoroutinefunction(run):
        if identity is None:

            async def async_plain(request: Request, response: Response) -> None:
                await run(request, response, None)

            return async_plain

        async def async_identified(
            request: Request, response: Response, resolved: Any = resolve
        ) -> None:
            await run(request, response, resolved)

        return async_identified

    if identity is None:
        # A plain `def`: FastAPI runs it in its thread pool, so a blocking store is fine.
        def plain(request: Request, response: Response) -> None:
            run(request, response, None)

        return plain

    def identified(request: Request, response: Response, resolved: Any = resolve) -> None:
        run(request, response, resolved)

    return identified


def rate_limit(
    limiter: Limiter | AsyncLimiter | LimiterFn,
    *,
    key: KeyFn,
    on_missing_key: MissingKeyPolicy,
    on_denied: OnDenied | None = None,
    skip: SkipFn | None = None,
    identity: Callable[..., Any] | None = None,
    cost: CostFn | None = None,
    problem_details: bool = False,
) -> Callable[..., Any]:
    """Build a dependency: `Depends(rate_limit(limiter, key=..., on_missing_key=...))`.

    `on_missing_key` is required so a misconfigured proxy cannot pass unnoticed. `on_denied`
    builds the response for every denial (429, 503 and 400); it needs `denied_handler`.
    `problem_details=True` answers them with a problem details body (RFC 9457) instead, as
    `application/problem+json`; it needs `denied_handler` too, and `on_denied` takes precedence.
    `skip` returns `True` for requests to let through uncounted and without headers: a health
    check, a test environment, a feature flag. It runs before `key`.

    `identity` is a FastAPI dependency, such as the one that authenticates the caller. What it
    returns reaches `key`, `skip` and a `limiter` function as `context.identity`, so you can
    key by user or API key without resolving them twice. If it raises, no request is counted.

    `limiter` may be a function of the context instead of a limiter, to pick one per request.

    `cost` returns how many units the request weighs, for requests that do several things at
    once, such as a batch. It defaults to 1.
    """
    check_policy(on_missing_key)

    if not isinstance(limiter, Limiter):
        source = limiter

        async def deny_async(request: Request, denial: Denial) -> HTTPException:
            if on_denied is None:
                return _problem(request, denial) if problem_details else _default_denial(denial)
            _check_handler(request)
            built = on_denied(request, denial)
            return RateLimitDenied(denial, await built if inspect.isawaitable(built) else built)

        async def run_async(request: Request, response: Response, resolved: Any) -> None:
            context = _context(request, resolved)
            if skip is not None:
                skipped = skip(context)
                if await skipped if inspect.isawaitable(skipped) else skipped:
                    return
            result = key(context)
            id_value = await result if inspect.isawaitable(result) else result
            target = _missing(on_missing_key) if id_value is None else id_value
            if isinstance(target, Denial):
                raise await deny_async(request, target)
            if target is None:
                return
            weight = 1
            if cost is not None:
                weighed = cost(context)
                weight = await weighed if inspect.isawaitable(weighed) else weighed
            if isinstance(source, AsyncLimiter):
                decision = await source.check(target, cost=weight)
            else:
                found: Any = source(context)
                active = await found if inspect.isawaitable(found) else found
                if isinstance(active, AsyncLimiter):
                    decision = await active.check(target, cost=weight)
                else:
                    # A blocking limiter chosen per request: keep it off the event loop.
                    decision = await run_in_threadpool(active.check, target, cost=weight)
            if not decision.allowed:
                raise await deny_async(request, _denial(decision))
            _pass(decision, response)

        return _with_identity(run_async, identity)

    blocking = limiter

    def deny(request: Request, denial: Denial) -> HTTPException:
        if on_denied is None:
            return _problem(request, denial) if problem_details else _default_denial(denial)
        _check_handler(request)
        built: Any = on_denied(request, denial)
        if inspect.iscoroutine(built):
            built.close()
            raise TypeError("omnilimit: an async on_denied needs an AsyncLimiter")
        return RateLimitDenied(denial, built)

    def run(request: Request, response: Response, resolved: Any) -> None:
        context = _context(request, resolved)
        if skip is not None:
            skipped: Any = skip(context)
            if inspect.iscoroutine(skipped):
                skipped.close()
                raise TypeError("omnilimit: an async skip function needs an AsyncLimiter")
            if skipped:
                return
        result: Any = key(context)
        if inspect.iscoroutine(result):
            result.close()
            raise TypeError("omnilimit: an async key function needs an AsyncLimiter")
        target = _missing(on_missing_key) if result is None else result
        if isinstance(target, Denial):
            raise deny(request, target)
        if target is None:
            return
        weight: Any = 1 if cost is None else cost(context)
        if inspect.iscoroutine(weight):
            weight.close()
            raise TypeError("omnilimit: an async cost function needs an AsyncLimiter")
        decision = blocking.check(target, cost=weight)
        if not decision.allowed:
            raise deny(request, _denial(decision))
        _pass(decision, response)

    return _with_identity(run, identity)
