"""Every vector that does not need a real database."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import jsonschema
import pytest
from support import (
    ALL_VECTORS,
    SPEC_DIR,
    policy_of,
    run_concurrency,
    run_concurrency_async,
    run_sequence,
    run_sequence_async,
    vectors_of,
)

from omnilimit import SPEC_VERSION, AsyncLimiter, HitParams, Limiter, MemoryStore, RawHit
from omnilimit._gcra import assert_scope, resolve_policy
from omnilimit._key import derive_key
from omnilimit.ip import canonical_ip, mask_ip, resolve_client_ip


def _by_id(kind: str) -> Any:
    return pytest.mark.parametrize("vector", vectors_of(kind), ids=lambda v: v["id"])


def test_vectors_cover_every_kind() -> None:
    kinds = sorted({vector["kind"] for vector in ALL_VECTORS})
    assert kinds == ["concurrency", "contract", "failure", "ip", "key", "sequence"]
    assert len(vectors_of("sequence")) >= 50


def test_vectors_match_the_schema() -> None:
    schema = json.loads((SPEC_DIR / "schemas" / "vector.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for vector in ALL_VECTORS:
        errors = list(validator.iter_errors(vector))
        assert errors == [], vector["id"]


def test_spec_version_is_the_one_in_the_repo() -> None:
    assert (SPEC_DIR / "VERSION").read_text(encoding="utf-8").strip() == SPEC_VERSION


@_by_id("sequence")
def test_sequence_on_memory(vector: dict[str, Any]) -> None:
    run_sequence(vector, MemoryStore())


@_by_id("sequence")
def test_sequence_on_memory_async(vector: dict[str, Any]) -> None:
    asyncio.run(run_sequence_async(vector, MemoryStore()))


@_by_id("concurrency")
def test_concurrency_on_memory(vector: dict[str, Any]) -> None:
    run_concurrency(vector, MemoryStore())


@_by_id("concurrency")
def test_concurrency_on_memory_async(vector: dict[str, Any]) -> None:
    asyncio.run(run_concurrency_async(vector, MemoryStore()))


def test_key_vectors() -> None:
    for vector in vectors_of("key"):
        for case in vector["cases"]:
            policy = resolve_policy(**case["policy"])
            assert derive_key(case["scope"], policy, case["salt"], case["id"]) == case["expect"]


_IP = vectors_of("ip")[0]


class MultiLine:
    """A framework's headers: `.get` returns only the first line, `.getlist` returns them all."""

    def __init__(self, headers: dict[str, str]) -> None:
        self._headers = headers

    def get(self, name: str) -> str | None:
        raise AssertionError("must read every line of a header")

    def getlist(self, name: str) -> list[str]:
        value = self._headers.get(name)
        return [] if value is None else [value]


@pytest.mark.parametrize("case", _IP["cases"], ids=lambda c: c["name"])
def test_ip_strategies(case: dict[str, Any]) -> None:
    strategy = case["strategy"]
    kwargs: dict[str, Any] = (
        {"preset": strategy["preset"]}
        if "preset" in strategy
        else {"xff_hops": strategy["xff"]["hops"]}
    )
    assert resolve_client_ip(case["headers"], case.get("peer"), **kwargs) == case["expect"]
    # A framework's headers object must behave the same as a plain mapping.
    wrapped = MultiLine(case["headers"])
    assert resolve_client_ip(wrapped, case.get("peer"), **kwargs) == case["expect"]


@pytest.mark.parametrize("mask", _IP["masks"], ids=lambda m: f"{m['ip']}/{m['prefix']}")
def test_ip_masks(mask: dict[str, Any]) -> None:
    assert mask_ip(mask["ip"], mask["prefix"]) == mask["expect"]


class _Failing:
    tier = "exact"

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        raise ConnectionError("store down")


class _Hanging:
    tier = "exact"

    def __init__(self) -> None:
        self.release = threading.Event()

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        self.release.wait(5)
        raise ConnectionError("released")


class _AsyncFailing:
    tier = "exact"

    async def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        raise ConnectionError("store down")


class _AsyncHanging:
    tier = "exact"

    async def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


def _failure_view(decision: Any) -> dict[str, Any]:
    return {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "status": decision.status,
        "remaining": decision.remaining,
        "reset_ms": decision.reset_ms,
        "retry_after_ms": decision.retry_after_ms,
        "headers": decision.headers,
    }


@_by_id("failure")
def test_failure(vector: dict[str, Any]) -> None:
    # Python has no platform binding: a missing one fails like any other store error.
    hanging = _Hanging()
    store = hanging if vector["fault"] == "timeout" else _Failing()
    limiter = Limiter(
        **policy_of(vector),
        store=store,
        scope="t",
        on_store_error=vector["on_store_error"],
        store_timeout_ms=20,
    )
    try:
        assert _failure_view(limiter.check("a")) == vector["expect"]
    finally:
        hanging.release.set()


@_by_id("failure")
def test_failure_async(vector: dict[str, Any]) -> None:
    store = _AsyncHanging() if vector["fault"] == "timeout" else _AsyncFailing()
    limiter = AsyncLimiter(
        **policy_of(vector),
        store=store,
        scope="t",
        on_store_error=vector["on_store_error"],
        store_timeout_ms=20,
    )
    assert _failure_view(asyncio.run(limiter.check("a"))) == vector["expect"]


def _contract(subject: str) -> dict[str, Any]:
    return next(v for v in vectors_of("contract") if v["subject"] == subject)


@pytest.mark.parametrize("case", _contract("policy")["cases"], ids=lambda c: json.dumps(c))
def test_policy_validation(case: dict[str, Any]) -> None:
    if case["valid"]:
        resolve_policy(**case["policy"])
    else:
        with pytest.raises(ValueError, match="omnilimit"):
            resolve_policy(**case["policy"])


@pytest.mark.parametrize("case", _contract("scope")["cases"], ids=lambda c: c["scope"][:20])
def test_scope_validation(case: dict[str, Any]) -> None:
    if case["valid"]:
        assert_scope(case["scope"])
    else:
        with pytest.raises(ValueError, match="omnilimit"):
            assert_scope(case["scope"])


def test_approximate_decision() -> None:
    vector = _contract("approximate")
    for case in vector["cases"]:

        class Approximate:
            tier = "approximate"
            allows = case["store_allows"]

            def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
                retry = None if self.allows else vector["period_sec"] * 1000
                return RawHit(allowed=self.allows, level=None, ts=None, retry_after_ms=retry)

        limiter = Limiter(
            **policy_of(vector), store=Approximate(), scope="t", on_store_error="closed"
        )
        decision = limiter.check("a")
        assert {**_failure_view(decision), "tier": decision.tier} == case["expect"]


@pytest.mark.parametrize(
    "bad",
    [
        "1::2::3",
        ":::",
        "12345::",
        "1:2:3:4:5:6:7",
        "1:2:3:4:5:6:7:8:9",
        "::1.2.3",
        "1:2:3:4:5:6:7::8",
        "g::1",
    ],
)
def test_canonical_ip_rejects_malformed_ipv6(bad: str) -> None:
    assert canonical_ip(bad) is None


def test_canonical_ip_rejects_empty() -> None:
    assert canonical_ip(None) is None
    assert canonical_ip("") is None
