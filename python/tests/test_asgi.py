"""The ASGI middleware, under a real Starlette app and against raw ASGI messages."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from omnilimit import AsyncLimiter, HitParams, Limiter, MemoryStore, RawHit
from omnilimit.asgi import AsgiContext, Denial, DeniedResponse, Headers, RateLimitMiddleware, ip_key

POLICY: dict[str, Any] = {"scope": "t", "limit": 1, "window_ms": 60_000, "on_store_error": "closed"}


async def _ok(request: Any) -> PlainTextResponse:
    return PlainTextResponse("ok", headers={"x-own": "1"})


def _client(**options: Any) -> TestClient:
    options.setdefault("key", ip_key(preset="cloudflare"))
    options.setdefault("on_missing_key", "reject")
    app = Starlette(
        routes=[Route("/", _ok), Route("/health", _ok)],
        middleware=[Middleware(RateLimitMiddleware, **options)],
    )
    return TestClient(app)


def _limiters() -> list[Limiter | AsyncLimiter]:
    return [Limiter(**POLICY, store=MemoryStore()), AsyncLimiter(**POLICY, store=MemoryStore())]


both = pytest.mark.parametrize("limiter", _limiters(), ids=["sync", "async"])
CF = {"cf-connecting-ip": "203.0.113.1"}


def _fresh(limiter: Limiter | AsyncLimiter) -> Limiter | AsyncLimiter:
    return type(limiter)(**POLICY, store=MemoryStore())


@both
def test_passes_with_headers_then_denies_with_429(limiter: Any) -> None:
    client = _client(limiter=_fresh(limiter))
    first = client.get("/", headers=CF)
    assert first.status_code == 200
    assert first.text == "ok"
    assert first.headers["x-own"] == "1"
    assert first.headers["ratelimit"] == '"t";r=0;t=60'
    denied = client.get("/", headers=CF)
    assert denied.status_code == 429
    assert denied.json() == {"error": "rate_limited"}
    assert denied.headers["retry-after"] == "60"
    assert denied.headers["content-type"] == "application/json"
    assert client.get("/", headers={"cf-connecting-ip": "203.0.113.2"}).status_code == 200


@both
def test_missing_key_policies(limiter: Any) -> None:
    rejected = _client(limiter=_fresh(limiter)).get("/")
    assert rejected.status_code == 400
    assert rejected.json() == {"error": "client_not_identified"}
    skipping = _client(limiter=_fresh(limiter), on_missing_key="skip")
    assert [skipping.get("/").status_code for _ in range(3)] == [200, 200, 200]
    shared = _client(limiter=_fresh(limiter), on_missing_key="shared")
    assert [shared.get("/").status_code for _ in range(2)] == [200, 429]


def test_on_missing_key_is_required_and_known() -> None:
    with pytest.raises(TypeError):
        RateLimitMiddleware(_ok, limiter=_limiters()[0], key=lambda _: "k")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="on_missing_key"):
        RateLimitMiddleware(_ok, limiter=_limiters()[0], key=lambda _: "k", on_missing_key="x")  # type: ignore[arg-type]


@both
def test_skip_exempts_paths_without_counting_or_headers(limiter: Any) -> None:
    client = _client(limiter=_fresh(limiter), skip=lambda context: context.path == "/health")
    for _ in range(3):
        response = client.get("/health", headers=CF)
        assert response.status_code == 200
        assert "ratelimit" not in response.headers
    assert client.get("/", headers=CF).status_code == 200
    assert client.get("/", headers=CF).status_code == 429
    assert client.get("/health", headers=CF).status_code == 200


def test_key_skip_and_limiter_may_be_async() -> None:
    limiter = AsyncLimiter(**POLICY, store=MemoryStore())

    async def key(context: AsgiContext) -> str | None:
        return context.headers.get("x-user")

    async def skip(context: AsgiContext) -> bool:
        return context.method == "OPTIONS"

    async def choose(context: AsgiContext) -> AsyncLimiter:
        return limiter

    client = _client(limiter=choose, key=key, skip=skip)
    assert client.get("/", headers={"x-user": "a"}).status_code == 200
    assert client.get("/", headers={"x-user": "a"}).status_code == 429
    assert client.options("/", headers={"x-user": "a"}).status_code != 429


def test_the_limiter_can_be_chosen_per_request() -> None:
    plans = {
        "free": Limiter(**POLICY, store=MemoryStore()),
        "pro": Limiter(**{**POLICY, "limit": 2}, store=MemoryStore()),
    }
    client = _client(
        limiter=lambda context: plans[context.headers.get("x-plan", "free")],
        key=lambda context: context.headers.get("x-user"),
    )
    assert [client.get("/", headers={"x-user": "a"}).status_code for _ in range(2)] == [200, 429]
    pro = {"x-user": "b", "x-plan": "pro"}
    assert [client.get("/", headers=pro).status_code for _ in range(3)] == [200, 200, 429]


@both
def test_cost_weighs_a_request_by_what_it_carries(limiter: Any) -> None:
    weighed = type(limiter)(**{**POLICY, "limit": 5}, store=MemoryStore())
    client = _client(
        limiter=weighed,
        key=lambda _: "k",
        cost=lambda context: int(context.headers.get("x-items", "1")),
    )
    assert client.get("/", headers={"x-items": "3"}).headers["ratelimit"] == '"t";r=2;t=36'
    assert client.get("/", headers={"x-items": "3"}).status_code == 429
    assert client.get("/", headers={"x-items": "2"}).status_code == 200


def test_rotating_the_left_of_x_forwarded_for_does_not_evade() -> None:
    client = _client(limiter=Limiter(**POLICY, store=MemoryStore()), key=ip_key(xff_hops=0))
    assert client.get("/", headers={"x-forwarded-for": "6.6.6.6, 203.0.113.9"}).status_code == 200
    assert client.get("/", headers={"x-forwarded-for": "7.7.7.7, 203.0.113.9"}).status_code == 429


def test_a_second_header_line_cannot_replace_the_trusted_value() -> None:
    headers = Headers([(b"X-Forwarded-For", b"6.6.6.6"), (b"x-forwarded-for", b"203.0.113.9")])
    assert headers.get("X-Forwarded-For") == "6.6.6.6,203.0.113.9"
    assert headers.getlist("x-forwarded-for") == ["6.6.6.6", "203.0.113.9"]
    assert headers.get("missing") is None
    assert headers.get("missing", "d") == "d"
    assert headers.getlist("missing") == []
    key = ip_key(xff_hops=0)
    context = AsgiContext(scope={}, headers=headers, peer=None, method="GET", path="/")
    assert key(context) == "203.0.113.9"


def test_peer_key_uses_the_connection() -> None:
    client = _client(limiter=Limiter(**POLICY, store=MemoryStore()), key=lambda c: c.peer)
    assert [client.get("/").status_code for _ in range(2)] == [200, 429]


def test_store_failure_answers_503() -> None:
    class Down:
        tier = "exact"

        def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            raise ConnectionError("down")

    client = _client(limiter=Limiter(**POLICY, store=Down()), key=lambda _: "k")
    response = client.get("/")
    assert response.status_code == 503
    assert response.json() == {"error": "rate_limiter_unavailable"}
    assert response.headers["retry-after"] == "1"


@both
def test_on_denied_builds_the_response(limiter: Any) -> None:
    seen: list[Denial] = []

    def on_denied(context: AsgiContext, denial: Denial) -> DeniedResponse:
        seen.append(denial)
        return DeniedResponse(
            body=f"slow down on {context.path}".encode(),
            media_type="text/plain",
            headers={"x-reason": denial.error},
        )

    client = _client(limiter=_fresh(limiter), on_denied=on_denied)
    client.get("/", headers=CF)
    denied = client.get("/", headers=CF)
    assert denied.status_code == 429
    assert denied.text == "slow down on /"
    assert denied.headers["content-type"].startswith("text/plain")
    assert denied.headers["x-reason"] == "rate_limited"
    assert denied.headers["retry-after"] == "60"
    assert seen[0].decision is not None
    missing = client.get("/")
    assert missing.status_code == 400
    assert seen[1].decision is None


def test_on_denied_may_be_async_and_set_the_status() -> None:
    async def on_denied(context: AsgiContext, denial: Denial) -> DeniedResponse:
        return DeniedResponse(body=b"{}", status=418)

    client = _client(limiter=AsyncLimiter(**POLICY, store=MemoryStore()), on_denied=on_denied)
    client.get("/", headers=CF)
    assert client.get("/", headers=CF).status_code == 418


def test_limits_a_mounted_app_and_leaves_the_rest_alone() -> None:
    limited = RateLimitMiddleware(
        Starlette(routes=[Route("/", _ok)]),
        limiter=Limiter(**POLICY, store=MemoryStore()),
        key=lambda _: "k",
        on_missing_key="reject",
    )
    client = TestClient(Starlette(routes=[Route("/open", _ok), Mount("/api", app=limited)]))
    assert [client.get("/api/").status_code for _ in range(2)] == [200, 429]
    assert [client.get("/open").status_code for _ in range(3)] == [200, 200, 200]


def test_non_http_scopes_pass_straight_through() -> None:
    calls: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        calls.append(scope["type"])

    def key(context: AsgiContext) -> str:
        raise AssertionError("only HTTP requests are keyed")

    middleware = RateLimitMiddleware(
        app, limiter=Limiter(**POLICY, store=MemoryStore()), key=key, on_missing_key="reject"
    )

    async def nothing(*_: Any) -> Any:
        return {}

    asyncio.run(middleware({"type": "lifespan"}, nothing, nothing))
    asyncio.run(middleware({"type": "websocket"}, nothing, nothing))
    assert calls == ["lifespan", "websocket"]


def test_a_bare_scope_without_client_or_headers() -> None:
    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204})
        await send({"type": "http.response.body", "body": b""})

    async def send(message: Any) -> None:
        sent.append(dict(message))

    async def receive() -> Any:
        return {}

    middleware = RateLimitMiddleware(
        app,
        limiter=Limiter(**POLICY, store=MemoryStore()),
        key=lambda context: context.peer or "nobody",
        on_missing_key="reject",
    )
    asyncio.run(middleware({"type": "http"}, receive, send))
    assert sent[0]["status"] == 204
    assert (b"ratelimit", b'"t";r=0;t=60') in sent[0]["headers"]
    asyncio.run(middleware({"type": "http"}, receive, send))
    assert sent[2]["status"] == 429
