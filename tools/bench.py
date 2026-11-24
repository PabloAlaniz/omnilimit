#!/usr/bin/env python3
"""What a check costs with each store, measured against the cheapest call the same connection
can make. Runs the Python port: `cd python && uv run python ../tools/bench.py`.

Absolute numbers depend on the machine and on where the store is, so they are reported, not
judged. `--check` judges two things: the calls each operation makes to the store's client, which
are exact, and the ratio of a check to a baseline call, against a loose ceiling.

Usage: bench.py [--check] [--json FILE]
  OMNILIMIT_TEST_REDIS_URLS, OMNILIMIT_TEST_POSTGRES_URLS: the first of each is measured.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import sys
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from omnilimit import AsyncLimiter, Limiter, Lockout, LockoutLimit, MemoryStore, check_all, reserve_all

SEQUENTIAL = 3000
WARMUP = 300
CONCURRENT = 10_000
THREADS = 16
TASKS = 64

# Loose on purpose: see tools/bench.mjs. The blocking limiter runs each store call on a thread
# of its own, to be able to give up on it, and that is part of what a check costs here.
CEILINGS = {"memory": 150, "redis": 20, "postgres": 40}

ROUND_TRIPS = {
    "check": 1,
    "check, denied": 1,
    "peek": 1,
    "reset": 1,
    "reserve": 1,
    "refund": 1,
    "check_all of 2": 4,
    "reserve_all of 2": 2,
    "cancel of 2": 2,
    "lockout attempt, 2 limits, wrong secret": 4,
    "lockout attempt in reserve mode, 2 limits, wrong secret": 2,
}


def latencies(run: Callable[[int], Any], count: int = SEQUENTIAL) -> dict[str, float]:
    for i in range(WARMUP):
        run(i)
    samples = []
    for i in range(count):
        started = time.perf_counter_ns()
        run(WARMUP + i)
        samples.append((time.perf_counter_ns() - started) / 1000)
    samples.sort()
    at = lambda q: samples[min(count - 1, int(q * count))]  # noqa: E731
    return {"p50": at(0.5), "p95": at(0.95), "p99": at(0.99)}


def threaded(run: Callable[[int], Any]) -> int:
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(run, range(CONCURRENT)))
    return round(CONCURRENT / (time.perf_counter() - started))


def options(store: Any, scope: str, salt: str, burst: int = 1000) -> dict[str, Any]:
    return {
        "limit": 1000,
        "window_ms": 60_000,
        "burst": burst,
        "store": store,
        "scope": scope,
        "salt": salt,
        "on_store_error": "closed",
        "store_timeout_ms": 30_000,
    }


def measure(name: str, store: Any, baseline: Callable[[int], Any], async_store: Any = None) -> dict[str, Any]:
    salt = str(uuid.uuid4())
    allowed = Limiter(**options(store, "allowed", salt))
    denied = Limiter(**options(store, "denied", salt, burst=1))
    second = Limiter(**options(store, "second", salt))
    denied.check("hot")
    base = latencies(baseline)
    check = latencies(lambda i: allowed.check(f"k{i}"))
    deny = latencies(lambda _: denied.check("hot"))
    peek = latencies(lambda i: allowed.peek(f"k{i}"))
    both = latencies(lambda i: check_all([(allowed, f"c{i}"), (second, f"c{i}")]))
    result: dict[str, Any] = {
        "store": name,
        "baseline_us": base,
        "check_us": check,
        "denied_us": deny,
        "peek_us": peek,
        "check_all_2_us": both,
        "ratio_check": check["p50"] / base["p50"],
        "throughput_threads": threaded(lambda i: allowed.check(f"t{i % 5000}")),
    }
    if async_store is not None:

        async def run() -> int:
            store_, close = await async_store()
            limiter = AsyncLimiter(**options(store_, "async", salt))
            await limiter.check("warm")
            pending = iter(range(CONCURRENT))

            async def worker() -> None:
                for i in pending:
                    await limiter.check(f"t{i % 5000}")

            started = time.perf_counter()
            await asyncio.gather(*(worker() for _ in range(TASKS)))
            elapsed = time.perf_counter() - started
            await close()
            return round(CONCURRENT / elapsed)

        result["throughput_async"] = asyncio.run(run())
    return result


def round_trips(store: Any, calls: Callable[[], int]) -> dict[str, int]:
    salt = str(uuid.uuid4())
    a = Limiter(**options(store, "rt-a", salt, burst=100))
    b = Limiter(**options(store, "rt-b", salt, burst=100))
    one = Limiter(**options(store, "rt-one", salt, burst=1))
    # Load every script first: the first use of each costs a second call.
    a.check("warm")
    a.peek("warm")
    a.refund(a.reserve("warm").reservation)
    one.check("x")
    out: dict[str, int] = {}

    def counted(name: str, work: Callable[[], Any]) -> Any:
        before = calls()
        value = work()
        out[name] = calls() - before
        return value

    counted("check", lambda: a.check("x"))
    counted("check, denied", lambda: one.check("x"))
    counted("peek", lambda: a.peek("x"))
    counted("reset", lambda: a.reset("x"))
    reserved = counted("reserve", lambda: a.reserve("x"))
    counted("refund", lambda: a.refund(reserved.reservation))
    counted("check_all of 2", lambda: check_all([(a, "x"), (b, "x")]))
    pending = counted("reserve_all of 2", lambda: reserve_all([(a, "x"), (b, "x")]))
    counted("cancel of 2", pending.cancel)
    limits = [LockoutLimit(a, id_of=lambda who: who), LockoutLimit(b, id_of=lambda who: who)]
    counted("lockout attempt, 2 limits, wrong secret", lambda: Lockout(limits=limits).attempt("u", lambda: False))
    counted(
        "lockout attempt in reserve mode, 2 limits, wrong secret",
        lambda: Lockout(limits=limits, mode="reserve").attempt("u", lambda: False),
    )
    return out


class Counting:
    """A client that counts the calls made through it."""

    def __init__(self, inner: Any, names: tuple[str, ...]) -> None:
        self.calls = 0
        for name in names:
            setattr(self, name, self._counting(getattr(inner, name)))

    def _counting(self, call: Callable[..., Any]) -> Callable[..., Any]:
        def run(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return call(*args, **kwargs)

        return run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", default=None)
    args = parser.parse_args()
    results: list[dict[str, Any]] = []
    trips: dict[str, dict[str, int]] = {}

    # Memory: the baseline is the SHA-256 that derives a key, the one thing every check must do.
    data = b"s\0an-id-of-ordinary-length"
    results.append(measure("memory", MemoryStore(max_keys=1_000_000), lambda _: hashlib.sha256(data).hexdigest()))

    redis_url = next((u for u in os.environ.get("OMNILIMIT_TEST_REDIS_URLS", "").split(",") if u), None)
    if redis_url:
        import redis
        import redis.asyncio

        from omnilimit.stores.redis import AsyncRedisStore, RedisStore

        client = redis.Redis.from_url(redis_url, max_connections=64)

        async def async_redis() -> Any:
            pool = redis.asyncio.BlockingConnectionPool.from_url(redis_url, max_connections=TASKS, timeout=30)
            async_client = redis.asyncio.Redis.from_pool(pool)
            return AsyncRedisStore(async_client), async_client.aclose

        results.append(measure("redis", RedisStore(client), lambda _: client.ping(), async_redis))
        counting = Counting(client, ("evalsha", "eval", "delete"))
        trips["redis"] = round_trips(RedisStore(counting), lambda: counting.calls)
        client.close()

    postgres_url = next((u for u in os.environ.get("OMNILIMIT_TEST_POSTGRES_URLS", "").split(",") if u), None)
    if postgres_url:
        import psycopg
        from psycopg_pool import ConnectionPool

        from omnilimit.stores.postgres import MIGRATION_SQL, PostgresStore

        url = postgres_url.replace("postgres://", "postgresql://", 1)
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(MIGRATION_SQL)
        with ConnectionPool(url, min_size=THREADS, max_size=THREADS, open=True) as pool:

            def select_one(_: int) -> Any:
                with pool.connection() as connection:
                    return connection.execute("SELECT 1").fetchone()

            results.append(measure("postgres", PostgresStore(pool), select_one))
        # One autocommit connection, so that each statement is one call to count.
        with psycopg.connect(url, autocommit=True) as connection:
            counting = Counting(connection, ("execute",))
            counting.autocommit = True  # type: ignore[attr-defined]
            trips["postgres"] = round_trips(PostgresStore(counting), lambda: counting.calls)

    def f(n: float) -> str:
        return f"{n:.0f}" if n >= 100 else f"{n:.1f}" if n >= 10 else f"{n:.2f}"

    lines = [
        f"Python {platform.python_version()}, {SEQUENTIAL} calls one after another, then {CONCURRENT} calls "
        f"from {THREADS} threads, and from {TASKS} tasks with the async limiter.",
        "",
        "| Store | Baseline p50 | `check` p50 | p99 | Denied p50 | `peek` p50 | `check_all` of 2, p50 | `check` / baseline | Checks/s, threads | Checks/s, async |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['store']} | {f(r['baseline_us']['p50'])} µs | {f(r['check_us']['p50'])} µs | {f(r['check_us']['p99'])} µs "
            f"| {f(r['denied_us']['p50'])} µs | {f(r['peek_us']['p50'])} µs | {f(r['check_all_2_us']['p50'])} µs "
            f"| {r['ratio_check']:.2f} | {r['throughput_threads']} | {r.get('throughput_async', '')} |"
        )
    if trips:
        lines += ["", "| Operation | Calls to the store | " + " | ".join(trips) + " |", "|---|---|" + "---|" * len(trips)]
        for name, expected in ROUND_TRIPS.items():
            lines.append(f"| {name} | {expected} | " + " | ".join(str(got[name]) for got in trips.values()) + " |")
    text = "\n".join(lines)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(f"### Python\n\n{text}\n\n")
    if args.json:
        payload = {"port": "python", "runtime": f"python {platform.python_version()}", "results": results, "round_trips": trips}
        with open(args.json, "w", encoding="utf-8") as out:
            out.write(json.dumps(payload, indent=1) + "\n")
    if args.check:
        failed = False
        for r in results:
            if r["ratio_check"] > CEILINGS[r["store"]]:
                failed = True
                print(f"{r['store']}: a check costs {r['ratio_check']:.2f} baselines, above the ceiling of {CEILINGS[r['store']]}", file=sys.stderr)
        for store, got in trips.items():
            for name, expected in ROUND_TRIPS.items():
                if got[name] != expected:
                    failed = True
                    print(f"{store}: {name} made {got[name]} calls to the store, not {expected}", file=sys.stderr)
        if os.environ.get("OMNILIMIT_REQUIRE_STORES") and len(results) < 3:
            failed = True
            print("OMNILIMIT_REQUIRE_STORES is set, but a store was not measured", file=sys.stderr)
        if failed:
            return 1
        print("\nevery operation makes the calls it should, and every ratio is under its ceiling")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
