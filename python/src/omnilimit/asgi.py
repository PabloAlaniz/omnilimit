"""ASGI middleware, for a limit over a whole application.

It speaks plain ASGI and imports no framework, so it works under Starlette, FastAPI, Litestar,
Quart or Django, and around an application mounted inside another. It only looks at HTTP
requests: WebSocket and lifespan messages go straight through.

    app.add_middleware(
        RateLimitMiddleware,
        limiter=limiter,
        key=ip_key(preset="cloud-run"),
        on_missing_key="shared",
        skip=lambda context: context.path in ("/health", "/metrics"),
    )

A middleware runs before routing and before any dependency, so it cannot key by an identity
your framework resolves later. For that, use the framework adapter on the routes.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any

from omnilimit._adapter import (
    Denial,
    DenialError,
    MissingKeyPolicy,
    check_policy,
    denial_of,
    missing_key,
)
from omnilimit._limiter import AsyncLimiter, Limiter
from omnilimit.ip import IpPreset, client_key

__all__ = [
    "AsgiContext",
    "Denial",
    "DenialError",
    "DeniedResponse",
    "Headers",
    "MissingKeyPolicy",
    "RateLimitMiddleware",
    "ip_key",
]

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]


class Headers:
    """The request headers by lowercase name. A header sent on several lines keeps them all."""

    def __init__(self, raw: Any) -> None:
        self._lines: dict[str, list[str]] = {}
        for name, value in raw or ():
            self._lines.setdefault(name.decode("latin-1").lower(), []).append(
                value.decode("latin-1")
            )

    def get(self, name: str, default: str | None = None) -> str | None:
        lines = self._lines.get(name.lower())
        return ",".join(lines) if lines else default

    def getlist(self, name: str) -> list[str]:
        return list(self._lines.get(name.lower(), ()))


@dataclass(frozen=True, slots=True)
class AsgiContext:
    scope: Scope
    headers: Headers
    peer: str | None
    method: str
    path: str


@dataclass(frozen=True, slots=True)
class DeniedResponse:
    """What `on_denied` returns. The denial's own headers, `Retry-After` among them, are always
    sent; `headers` adds to them. `status` defaults to the denial's.
    """

    body: bytes
    media_type: str = "application/json"
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)


KeyFn = Callable[[AsgiContext], "Awaitable[str | None] | str | None"]
SkipFn = Callable[[AsgiContext], "Awaitable[bool] | bool"]
CostFn = Callable[[AsgiContext], "Awaitable[int] | int"]
LimiterFn = Callable[[AsgiContext], "Awaitable[Limiter | AsyncLimiter] | Limiter | AsyncLimiter"]
OnDenied = Callable[[AsgiContext, Denial], "Awaitable[DeniedResponse] | DeniedResponse"]


def ip_key(
    *,
    preset: IpPreset | None = None,
    xff_hops: int | None = None,
    forwarded_hops: int | None = None,
    header: str | None = None,
    use_peer: bool = False,
    ipv6_prefix: int = 64,
) -> Callable[[AsgiContext], str | None]:
    """Key requests by trusted client IP. See `omnilimit.ip.resolve_client_ip`."""

    def key(context: AsgiContext) -> str | None:
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


async def _resolved(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class RateLimitMiddleware:
    """Limit every HTTP request that reaches `app`. See the module docstring.

    `limiter` may be a blocking `Limiter`, which then runs in a worker thread, an
    `AsyncLimiter`, or a function of the context that returns either. `cost` returns how many
    units a request weighs, from 1 to the limiter's burst.
    """

    def __init__(
        self,
        app: App,
        *,
        limiter: Limiter | AsyncLimiter | LimiterFn,
        key: KeyFn,
        on_missing_key: MissingKeyPolicy,
        skip: SkipFn | None = None,
        on_denied: OnDenied | None = None,
        cost: CostFn | None = None,
    ) -> None:
        check_policy(on_missing_key)
        self.app = app
        self._limiter = limiter
        self._key = key
        self._on_missing_key: MissingKeyPolicy = on_missing_key
        self._skip = skip
        self._on_denied = on_denied
        self._cost = cost

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        context = AsgiContext(
            scope=scope,
            headers=Headers(scope.get("headers")),
            peer=client[0] if client else None,
            method=scope.get("method", ""),
            path=scope.get("path", ""),
        )

        if self._skip is not None and await _resolved(self._skip(context)):
            await self.app(scope, receive, send)
            return

        id_value = await _resolved(self._key(context))
        target = missing_key(self._on_missing_key) if id_value is None else id_value
        if isinstance(target, Denial):
            await self._deny(context, target, send)
            return
        if target is None:
            await self.app(scope, receive, send)
            return

        limiter = self._limiter
        if not isinstance(limiter, Limiter | AsyncLimiter):
            limiter = await _resolved(limiter(context))
        weight = 1 if self._cost is None else await _resolved(self._cost(context))
        if isinstance(limiter, AsyncLimiter):
            decision = await limiter.check(target, cost=weight)
        else:
            decision = await asyncio.to_thread(limiter.check, target, cost=weight)
        if not decision.allowed:
            await self._deny(context, denial_of(decision), send)
            return

        extra = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in decision.headers.items()
        ]

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", ()), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)

    async def _deny(self, context: AsgiContext, denial: Denial, send: Send) -> None:
        if self._on_denied is None:
            built = DeniedResponse(body=json.dumps({"error": denial.error}).encode())
        else:
            built = await _resolved(self._on_denied(context, denial))
        merged = {"content-type": built.media_type, **denial.headers, **built.headers}
        merged["content-length"] = str(len(built.body))
        await send(
            {
                "type": "http.response.start",
                "status": built.status or denial.status,
                "headers": [
                    (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in merged.items()
                ],
            }
        )
        await send({"type": "http.response.body", "body": built.body})
