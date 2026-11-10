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

from omnilimit import Limiter
from omnilimit.stores.postgres import MIGRATION_SQL, PostgresStore
from omnilimit.stores.redis import RedisStore

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "cross_lang.mjs"
REDIS_URLS = [u for u in os.environ.get("OMNILIMIT_TEST_REDIS_URLS", "").split(",") if u]
POSTGRES_URLS = [u for u in os.environ.get("OMNILIMIT_TEST_POSTGRES_URLS", "").split(",") if u]

# Enough to reach drain, denial, and clock regression, without one Node start per vector step.
# Only hits: `tools/cross_lang.mjs` drives `check` alone.
VECTORS = [
    vector
    for vector in vectors_of("sequence")
    if "exact" in vector["tiers"]
    and 3 <= len(vector["steps"]) <= 12
    and all("op" not in step for step in vector["steps"])
][:12]


def typescript(store: str, url: str, vector: dict[str, Any], salt: str, steps: list[Any]) -> Any:
    payload = {
        "store": store,
        "url": url,
        "policy": vector["policy"],
        "scope": vector["scope"],
        "salt": salt,
        "steps": [{"id": step["id"], "at_ms": step["at_ms"]} for step in steps],
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


def view(decision: Any) -> dict[str, Any]:
    return {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "status": decision.status,
        "remaining": decision.remaining,
        "reset_ms": decision.reset_ms,
        "retry_after_ms": decision.retry_after_ms,
    }


def expected(step: dict[str, Any]) -> dict[str, Any]:
    keys = ("allowed", "reason", "status", "remaining", "reset_ms", "retry_after_ms")
    return {key: step["expect"][key] for key in keys}


def alternate(store_name: str, url: str, store: Any, vector: dict[str, Any]) -> None:
    salt = vector["salt"] + f":{uuid.uuid4()}"
    policy = vector["policy"]
    limiter = Limiter(
        store=store,
        scope=vector["scope"],
        salt=salt,
        on_store_error="closed",
        store_timeout_ms=10_000,
        limit=policy["limit"],
        window_ms=policy["window_ms"],
        burst=policy["burst"],
    )
    for index, step in enumerate(vector["steps"]):
        if index % 2 == 0:
            got = view(limiter.check(step["id"], now_ms=step["at_ms"]))
        else:
            got = typescript(store_name, url, vector, salt, [step])[0]
        assert got == expected(step), f"{vector['id']} step {index}"


@pytest.mark.parametrize("url", REDIS_URLS)
@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["id"])
def test_redis(url: str, vector: dict[str, Any]) -> None:
    with redis.Redis.from_url(url) as client:
        alternate("redis", url, RedisStore(client), vector)


@pytest.mark.parametrize("url", POSTGRES_URLS)
@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["id"])
def test_postgres(url: str, vector: dict[str, Any]) -> None:
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(MIGRATION_SQL)
    with ConnectionPool(url, min_size=1, max_size=2, open=True) as pool:
        alternate("postgres", url, PostgresStore(pool), vector)
