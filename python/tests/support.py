"""Load the shared vectors and run them against any store."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnilimit import AsyncLimiter, HeaderInput, HitParams, Limiter, RawHit, build_headers

SPEC_DIR = Path(__file__).resolve().parents[2] / "spec"
VECTOR_DIR = SPEC_DIR / "vectors"


def _load() -> list[dict[str, Any]]:
    vectors = []
    for path in sorted(VECTOR_DIR.rglob("*.json")):
        vector = json.loads(path.read_text(encoding="utf-8"))
        # A vector's id doubles as its path, so a renamed file cannot fall out of a suite.
        assert vector["id"] == path.relative_to(VECTOR_DIR).with_suffix("").as_posix()
        vectors.append(vector)
    return vectors


ALL_VECTORS = _load()


def vectors_of(kind: str) -> list[dict[str, Any]]:
    return [vector for vector in ALL_VECTORS if vector["kind"] == kind]


def policy_of(vector: dict[str, Any]) -> dict[str, int]:
    policy = vector["policy"]
    return {"limit": policy["limit"], "window_ms": policy["window_ms"], "burst": policy["burst"]}


def outcome(decision: Any, raw: RawHit | None, profiles: dict[str, Any]) -> dict[str, Any]:
    return {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "status": decision.status,
        "remaining": decision.remaining,
        "reset_ms": decision.reset_ms,
        "retry_after_ms": decision.retry_after_ms,
        "state": {"level": raw.level if raw else None, "ts": raw.ts if raw else None},
        "headers": profiles,
    }


@dataclass
class Recording:
    """Keeps the raw reply and both header profiles of the latest check."""

    tier: Any
    raw: RawHit | None = None
    profiles: dict[str, Any] = field(default_factory=dict)

    def headers(self, data: HeaderInput) -> dict[str, str]:
        self.profiles = {
            "draft-11": build_headers("draft-11", data),
            "legacy": build_headers("legacy", data),
        }
        return {}


def run_sequence(vector: dict[str, Any], store: Any, salt_suffix: str = "") -> None:
    recording = Recording(store.tier)

    class Recorder:
        tier = store.tier

        def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            recording.raw = store.hit(key, params, now_ms)
            return recording.raw

    limiter = Limiter(
        **policy_of(vector),
        store=Recorder(),
        scope=vector["scope"],
        salt=vector["salt"] + salt_suffix,
        on_store_error="closed",
        store_timeout_ms=10_000,
        headers=recording.headers,
    )
    for index, step in enumerate(vector["steps"]):
        decision = limiter.check(step["id"], now_ms=step["at_ms"])
        got = outcome(decision, recording.raw, recording.profiles)
        assert got == step["expect"], f"{vector['id']} step {index}"


async def run_sequence_async(vector: dict[str, Any], store: Any, salt_suffix: str = "") -> None:
    recording = Recording(store.tier)

    class Recorder:
        tier = store.tier

        async def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            result = store.hit(key, params, now_ms)
            recording.raw = await result if asyncio.iscoroutine(result) else result
            return recording.raw

    limiter = AsyncLimiter(
        **policy_of(vector),
        store=Recorder(),
        scope=vector["scope"],
        salt=vector["salt"] + salt_suffix,
        on_store_error="closed",
        store_timeout_ms=10_000,
        headers=recording.headers,
    )
    for index, step in enumerate(vector["steps"]):
        decision = await limiter.check(step["id"], now_ms=step["at_ms"])
        got = outcome(decision, recording.raw, recording.profiles)
        assert got == step["expect"], f"{vector['id']} step {index}"


def run_concurrency(vector: dict[str, Any], store: Any, salt_suffix: str = "") -> None:
    limiter = Limiter(
        **policy_of(vector),
        store=store,
        scope=vector["scope"],
        salt=vector["salt"] + salt_suffix,
        on_store_error="closed",
        store_timeout_ms=30_000,
    )
    with ThreadPoolExecutor(max_workers=32) as pool:
        decisions = list(
            pool.map(
                lambda _: limiter.check(vector["id_value"], now_ms=vector["at_ms"]),
                range(vector["hits"]),
            )
        )
    assert [d for d in decisions if d.reason == "store_error"] == []
    assert sum(d.allowed for d in decisions) == vector["expect_allowed"]


async def run_concurrency_async(vector: dict[str, Any], store: Any, salt_suffix: str = "") -> None:
    limiter = AsyncLimiter(
        **policy_of(vector),
        store=store,
        scope=vector["scope"],
        salt=vector["salt"] + salt_suffix,
        on_store_error="closed",
        store_timeout_ms=30_000,
    )
    decisions = await asyncio.gather(
        *(limiter.check(vector["id_value"], now_ms=vector["at_ms"]) for _ in range(vector["hits"]))
    )
    assert [d for d in decisions if d.reason == "store_error"] == []
    assert sum(d.allowed for d in decisions) == vector["expect_allowed"]
