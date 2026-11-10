"""The adapter, through real FastAPI apps."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient

from omnilimit import AsyncLimiter, HitParams, Limiter, MemoryStore, RawHit
from omnilimit.fastapi import (
    Denial,
    KeyContext,
    RateLimitDenied,
    denied_handler,
    ip_key,
    rate_limit,
)

POLICY: dict[str, Any] = {"scope": "t", "limit": 1, "window_ms": 60_000, "on_store_error": "closed"}


def _app(dependency: Any) -> TestClient:
    app = FastAPI(dependencies=[Depends(dependency)])

    @app.get("/")
    def root() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/raw")
    def raw() -> PlainTextResponse:
        return PlainTextResponse("ok")

    return TestClient(app)


def _limiters() -> list[Limiter | AsyncLimiter]:
    return [
        Limiter(**POLICY, store=MemoryStore()),
        AsyncLimiter(**POLICY, store=MemoryStore()),
    ]


both = pytest.mark.parametrize("limiter", _limiters(), ids=["sync", "async"])


def _fresh(limiter: Limiter | AsyncLimiter) -> Limiter | AsyncLimiter:
    return type(limiter)(**POLICY, store=MemoryStore())


@both
def test_denies_with_429_and_headers(limiter: Any) -> None:
    client = _app(
        rate_limit(_fresh(limiter), key=ip_key(preset="cloud-run"), on_missing_key="reject")
    )
    forwarded = {"X-Forwarded-For": "203.0.113.7"}

    allowed = client.get("/", headers=forwarded)
    assert allowed.status_code == 200
    assert allowed.headers["ratelimit-policy"] == '"t";q=1;w=60'
    assert allowed.headers["ratelimit"].startswith('"t";r=0;')

    denied = client.get("/", headers=forwarded)
    assert denied.status_code == 429
    assert denied.json() == {"detail": "rate_limited"}
    assert 0 < int(denied.headers["retry-after"]) <= 60


@both
def test_rotating_the_left_of_x_forwarded_for_does_not_evade(limiter: Any) -> None:
    client = _app(
        rate_limit(_fresh(limiter), key=ip_key(preset="cloud-run"), on_missing_key="reject")
    )
    assert client.get("/", headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.7"}).status_code == 200
    rotated = client.get("/", headers={"X-Forwarded-For": "2.2.2.2, 203.0.113.7"})
    assert rotated.status_code == 429


@both
def test_a_second_header_line_cannot_replace_the_trusted_value(limiter: Any) -> None:
    client = _app(
        rate_limit(_fresh(limiter), key=ip_key(preset="cloud-run"), on_missing_key="reject")
    )
    lines = [("x-forwarded-for", "1.1.1.1"), ("x-forwarded-for", "203.0.113.7")]
    rotated = [("x-forwarded-for", "2.2.2.2"), ("x-forwarded-for", "203.0.113.7")]
    assert client.get("/", headers=lines).status_code == 200
    assert client.get("/", headers=rotated).status_code == 429


@both
def test_missing_key_policies(limiter: Any) -> None:
    key = ip_key(preset="cloud-run")

    rejecting = _app(rate_limit(_fresh(limiter), key=key, on_missing_key="reject"))
    response = rejecting.get("/")
    assert (response.status_code, response.json()) == (400, {"detail": "client_not_identified"})

    skipping = _app(rate_limit(_fresh(limiter), key=key, on_missing_key="skip"))
    assert {skipping.get("/").status_code for _ in range(3)} == {200}

    sharing = _app(rate_limit(_fresh(limiter), key=key, on_missing_key="shared"))
    assert [sharing.get("/").status_code for _ in range(2)] == [200, 429]


def test_on_missing_key_is_required_and_known() -> None:
    limiter = Limiter(**POLICY, store=MemoryStore())
    with pytest.raises(TypeError, match="on_missing_key"):
        rate_limit(limiter, key=ip_key(preset="direct"))  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="on_missing_key"):
        rate_limit(limiter, key=ip_key(preset="direct"), on_missing_key="ignore")  # type: ignore[arg-type]


def test_store_failure_answers_503() -> None:
    class Failing:
        tier = "exact"

        def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            raise ConnectionError("down")

    limiter = Limiter(**POLICY, store=Failing())
    client = _app(rate_limit(limiter, key=lambda _: "x", on_missing_key="reject"))
    response = client.get("/")
    assert response.status_code == 503
    assert response.json() == {"detail": "rate_limiter_unavailable"}
    assert response.headers["retry-after"] == "1"


def test_async_key_functions() -> None:
    async def key(context: KeyContext) -> str:
        return context.headers.get("x-user", "anon")

    limiter = AsyncLimiter(**POLICY, store=MemoryStore())
    client = _app(rate_limit(limiter, key=key, on_missing_key="reject"))
    assert client.get("/", headers={"x-user": "a"}).status_code == 200
    assert client.get("/", headers={"x-user": "b"}).status_code == 200
    assert client.get("/", headers={"x-user": "a"}).status_code == 429


def test_async_key_function_needs_an_async_limiter() -> None:
    async def key(context: KeyContext) -> str:
        return "x"

    limiter = Limiter(**POLICY, store=MemoryStore())
    client = _app(rate_limit(limiter, key=key, on_missing_key="reject"))
    with pytest.raises(TypeError, match="AsyncLimiter"):
        client.get("/")


def test_peer_key_uses_the_connection() -> None:
    limiter = Limiter(**POLICY, store=MemoryStore())
    client = _app(rate_limit(limiter, key=ip_key(use_peer=True), on_missing_key="reject"))
    # TestClient's peer is "testclient", which is not an IP: the key cannot be determined.
    assert client.get("/").status_code == 400


def test_a_returned_response_is_limited_but_carries_no_headers() -> None:
    """Documented limitation: FastAPI does not copy headers onto a returned Response."""
    limiter = Limiter(**POLICY, store=MemoryStore())
    client = _app(rate_limit(limiter, key=lambda _: "x", on_missing_key="reject"))
    response = client.get("/raw")
    assert response.status_code == 200
    assert "ratelimit" not in response.headers
    assert client.get("/raw").status_code == 429


def _custom_app(limiter: Any, on_denied: Any, *, handler: bool = True, **kwargs: Any) -> TestClient:
    app = FastAPI(
        dependencies=[
            Depends(rate_limit(limiter, on_missing_key="reject", on_denied=on_denied, **kwargs))
        ]
    )
    if handler:
        app.add_exception_handler(RateLimitDenied, denied_handler)

    @app.get("/")
    def root() -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app)


@both
def test_on_denied_builds_the_response(limiter: Any) -> None:
    seen: list[Denial] = []

    def on_denied(request: Request, denial: Denial) -> Response:
        seen.append(denial)
        return JSONResponse(
            {"error": denial.error, "path": request.url.path},
            status_code=denial.status,
            headers=denial.headers,
        )

    client = _custom_app(_fresh(limiter), on_denied, key=lambda _: "x")
    assert client.get("/").status_code == 200
    denied = client.get("/")
    assert denied.status_code == 429
    assert denied.json() == {"error": "rate_limited", "path": "/"}
    assert 0 < int(denied.headers["retry-after"]) <= 60
    assert seen[0].decision is not None
    assert seen[0].decision.reason == "limited"


def test_on_denied_may_be_async_with_an_async_limiter() -> None:
    async def on_denied(request: Request, denial: Denial) -> Response:
        return PlainTextResponse("slow down", status_code=418)

    limiter = AsyncLimiter(**POLICY, store=MemoryStore())
    client = _custom_app(limiter, on_denied, key=lambda _: "x")
    assert client.get("/").status_code == 200
    denied = client.get("/")
    assert (denied.status_code, denied.text) == (418, "slow down")


def test_an_async_on_denied_needs_an_async_limiter() -> None:
    async def on_denied(request: Request, denial: Denial) -> Response:
        return PlainTextResponse("x")

    limiter = Limiter(**{**POLICY, "limit": 1}, store=MemoryStore())
    client = _custom_app(limiter, on_denied, key=lambda _: "x")
    client.get("/")
    with pytest.raises(TypeError, match="async on_denied"):
        client.get("/")


@both
def test_on_denied_sees_store_failures_and_missing_keys(limiter: Any) -> None:
    seen: list[Denial] = []

    def on_denied(request: Request, denial: Denial) -> Response:
        seen.append(denial)
        return PlainTextResponse(denial.error, status_code=denial.status, headers=denial.headers)

    class Failing:
        tier = "exact"

        def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            raise ConnectionError("down")

    class AsyncFailing:
        tier = "exact"

        async def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            raise ConnectionError("down")

    store = AsyncFailing() if isinstance(limiter, AsyncLimiter) else Failing()
    failing = type(limiter)(**POLICY, store=store)
    response = _custom_app(failing, on_denied, key=lambda _: "x").get("/")
    assert (response.status_code, response.text) == (503, "rate_limiter_unavailable")
    assert response.headers["retry-after"] == "1"

    missing = _custom_app(_fresh(limiter), on_denied, key=lambda _: None).get("/")
    assert (missing.status_code, missing.text) == (400, "client_not_identified")
    assert seen[-1] == Denial(status=400, headers={}, error="client_not_identified", decision=None)


@both
def test_on_denied_without_the_handler_fails_loudly(limiter: Any) -> None:
    def on_denied(request: Request, denial: Denial) -> Response:
        return PlainTextResponse("x")

    client = _custom_app(_fresh(limiter), on_denied, handler=False, key=lambda _: "x")
    assert client.get("/").status_code == 200
    with pytest.raises(TypeError, match="denied_handler"):
        client.get("/")
