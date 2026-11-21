"""TypeScript and Python take turns on one counter, in real Redis and Postgres.

Sharing a bucket proves they derive the same key and read each other's state. Run `pnpm build`
first: `tools/cross_lang.mjs` imports the built TypeScript package.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
import redis
from psycopg_pool import ConnectionPool
from support import vectors_of

from omnilimit import HitParams, Limiter, RawHit, RawRefund, Reservation
from omnilimit._gcra import resolve_policy
from omnilimit._key import derive_key
from omnilimit.stores.postgres import MIGRATION_SQL, PostgresStore
from omnilimit.stores.redis import RedisStore

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "cross_lang.mjs"
REDIS_URLS = [u for u in os.environ.get("OMNILIMIT_TEST_REDIS_URLS", "").split(",") if u]
POSTGRES_URLS = [u for u in os.environ.get("OMNILIMIT_TEST_POSTGRES_URLS", "").split(",") if u]

# Enough to reach drain, denial, and clock regression, without one Node start per vector step.
HITS = [
    vector
    for vector in vectors_of("sequence")
    if "exact" in vector["tiers"]
    and 3 <= len(vector["steps"]) <= 12
    and all("op" not in step for step in vector["steps"])
][:12]

# Every refund vector short enough to keep the Node starts down: reservations and refunds on
# either side, with hits, peeks and resets of the other one in between.
REFUNDS = [
    vector
    for vector in vectors_of("sequence")
    if "exact" in vector["tiers"]
    and len(vector["steps"]) <= 21
    and any(step.get("op") == "refund" for step in vector["steps"])
]

# Hits alternate starting with Python; refund vectors run once starting with each language.
TURNS = [(vector, "py") for vector in HITS]
TURNS += [(vector, first) for vector in REFUNDS for first in ("py", "ts")]

DECISION = ("allowed", "reason", "status", "remaining", "reset_ms", "retry_after_ms", "state")


def typescript(store: str, url: str, vector: dict[str, Any], salt: str, steps: list[Any]) -> Any:
    payload = {
        "store": store,
        "url": url,
        "policy": vector["policy"],
        "scope": vector["scope"],
        "shadow": vector.get("shadow", False),
        "salt": salt,
        "steps": steps,
    }
    result = subprocess.run(
        ["node", str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)


class Recorded:
    """A store that keeps its latest raw reply, to report the state each step left."""

    def __init__(self, store: Any) -> None:
        self.store, self.tier = store, store.tier
        self.raw: RawHit | RawRefund | None = None

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        raw: RawHit = self.store.hit(key, params, now_ms)
        self.raw = raw
        return raw

    def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        raw: RawHit = self.store.peek(key, params, now_ms)
        self.raw = raw
        return raw

    def reset(self, key: str) -> None:
        self.store.reset(key)

    def refund(
        self, key: str, params: HitParams, since: int, now_ms: int | None = None
    ) -> RawRefund:
        raw: RawRefund = self.store.refund(key, params, since, now_ms)
        self.raw = raw
        return raw


def expected(step: dict[str, Any]) -> dict[str, Any]:
    if step.get("op") == "reset":
        return {"reset": True}
    if step.get("op") == "refund":
        return dict(step["expect"])
    # Vectors write `shadowed` only where it is true.
    return {key: step["expect"][key] for key in (*DECISION, "shadowed") if key in step["expect"]}


def split(
    steps: list[dict[str, Any]], first: str = "py", length: int = 3
) -> list[tuple[str, list[int]]]:
    """Cut a sequence into runs of steps that Python and TypeScript take in turn.

    A reservation is opaque, so the language that reserves is the one that refunds. TypeScript
    only holds one within its own run: a run of it extends past the last refund of every hit it
    reserves, and never takes a refund of a hit Python reserved. Python may hold one across runs
    of TypeScript. A turn TypeScript cannot take under these rules goes to Python.
    """
    last_refund: dict[int, int] = {}
    for index, step in enumerate(steps):
        if step.get("op") == "refund":
            last_refund[step["of"]] = index
    runs: list[tuple[str, list[int]]] = []
    i, turn = 0, first
    while i < len(steps):
        end: int | None = None
        if turn == "ts":
            for size in range(length, 0, -1):
                end = min(len(steps), i + size)
                j = i
                while j < end:
                    if steps[j].get("op") == "refund" and steps[j]["of"] < i:
                        end = None
                        break
                    end = max(end, last_refund.get(j, j) + 1)
                    j += 1
                if end is not None:
                    break
        if end is None:
            turn, end = "py", min(len(steps), i + length)
        runs.append((turn, list(range(i, end))))
        i, turn = end, "ts" if turn == "py" else "py"
    return runs


def held_across(steps: list[dict[str, Any]], runs: list[tuple[str, list[int]]]) -> dict[str, int]:
    """Count the refunds TypeScript made of its own reservations, and the Python reservations
    held while TypeScript counted."""
    lang = {index: turn for turn, indexes in runs for index in indexes}
    count = {"ts": 0, "py-over-ts": 0}
    for index, step in enumerate(steps):
        if step.get("op") != "refund":
            continue
        assert lang[index] == lang[step["of"]], "a reservation is refunded where it was made"
        if lang[index] == "ts":
            count["ts"] += 1
        elif any(lang[k] == "ts" for k in range(step["of"], index)):
            count["py-over-ts"] += 1
    return count


def test_the_turns_cover_both_ways() -> None:
    """TypeScript reserves and refunds between Python's steps, and Python holds a reservation
    while TypeScript counts on the same bucket. Needs no store."""
    total = {"ts": 0, "py-over-ts": 0}
    for vector, first in TURNS:
        for key, n in held_across(vector["steps"], split(vector["steps"], first)).items():
            total[key] += n
    assert total["ts"] >= 10, total
    assert total["py-over-ts"] >= 10, total


def left_by(steps: list[dict[str, Any]], indexes: list[int]) -> list[tuple[str, Any, int]]:
    """The state in which these steps left each id they touched, and a time to read it at: the
    latest step on it that reports one. A reset leaves no bucket; a refund that gave nothing
    reports nothing, and wrote nothing."""
    out = []
    for who in sorted({steps[i]["id"] for i in indexes}):
        for step in reversed(steps[: indexes[-1] + 1]):
            if step["id"] != who:
                continue
            if step.get("op") == "reset":
                out.append((who, {"level": 0, "ts": step["at_ms"]}, step["at_ms"]))
                break
            if "state" in step["expect"]:
                out.append((who, step["expect"]["state"], step["at_ms"]))
                break
    return out


def take_turns(store_name: str, url: str, store: Any, vector: dict[str, Any], first: str) -> None:
    """Run a vector's steps in turns, each language checking its own results. After each turn of
    TypeScript, Python reads every bucket it touched and finds the state the vector expects."""
    salt = vector["salt"] + f":{uuid.uuid4()}"
    policy = vector["policy"]
    recorded = Recorded(store)
    limiter = Limiter(
        store=recorded,
        scope=vector["scope"],
        salt=salt,
        on_store_error="closed",
        store_timeout_ms=10_000,
        limit=policy["limit"],
        window_ms=policy["window_ms"],
        burst=policy["burst"],
        shadow=vector.get("shadow", False),
    )
    params = resolve_policy(**policy)
    steps = vector["steps"]
    refunded = {step["of"] for step in steps if step.get("op") == "refund"}
    reservations: dict[int, Reservation | None] = {}
    for turn, indexes in split(steps, first):
        if turn == "ts":
            local = {index: k for k, index in enumerate(indexes)}
            sent = [
                {
                    "id": steps[i]["id"],
                    "at_ms": steps[i]["at_ms"],
                    **({"op": steps[i]["op"]} if "op" in steps[i] else {}),
                    **({"cost": steps[i]["cost"]} if "cost" in steps[i] else {}),
                    **({"of": local[steps[i]["of"]]} if "of" in steps[i] else {}),
                }
                for i in indexes
            ]
            results = typescript(store_name, url, vector, salt, sent)
            for index, got in zip(indexes, results, strict=True):
                assert got == expected(steps[index]), f"{vector['id']} step {index} (typescript)"
            for who, state, at in left_by(steps, indexes):
                raw = store.peek(derive_key(vector["scope"], params, salt, who), params, at)
                read = {"level": raw.level, "ts": raw.ts}
                assert read == state, f"{vector['id']}: {who} after steps {indexes}"
            continue
        for index in indexes:
            step = steps[index]
            op = step.get("op", "hit")
            recorded.raw = None
            got: dict[str, Any]
            if op == "reset":
                got = {"reset": limiter.reset(step["id"])}
            elif op == "refund":
                got = {"refunded": limiter.refund(reservations[step["of"]], now_ms=step["at_ms"])}
                if got["refunded"]:
                    assert recorded.raw is not None
                    got["state"] = {"level": recorded.raw.level, "ts": recorded.raw.ts}
            else:
                cost = step.get("cost", 1)
                if op == "peek":
                    d = limiter.peek(step["id"], now_ms=step["at_ms"], cost=cost)
                elif index in refunded:
                    reserved = limiter.reserve(step["id"], now_ms=step["at_ms"], cost=cost)
                    reservations[index] = reserved.reservation
                    d = reserved.decision
                else:
                    d = limiter.check(step["id"], now_ms=step["at_ms"], cost=cost)
                assert recorded.raw is not None
                got = {
                    "allowed": d.allowed,
                    "reason": d.reason,
                    "status": d.status,
                    "remaining": d.remaining,
                    "reset_ms": d.reset_ms,
                    "retry_after_ms": d.retry_after_ms,
                    **({"shadowed": True} if d.shadowed else {}),
                    "state": {"level": recorded.raw.level, "ts": recorded.raw.ts},
                }
            assert got == expected(step), f"{vector['id']} step {index} (python)"


turns = pytest.mark.parametrize(
    ("vector", "first"), TURNS, ids=[f"{v['id']}-{first}-first" for v, first in TURNS]
)


@pytest.mark.parametrize("url", REDIS_URLS)
@turns
def test_redis(url: str, vector: dict[str, Any], first: str) -> None:
    with redis.Redis.from_url(url) as client:
        take_turns("redis", url, RedisStore(client), vector, first)


@pytest.mark.parametrize("url", POSTGRES_URLS)
@turns
def test_postgres(url: str, vector: dict[str, Any], first: str) -> None:
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(MIGRATION_SQL)
    with ConnectionPool(url, min_size=1, max_size=2, open=True) as pool:
        take_turns("postgres", url, PostgresStore(pool), vector, first)
