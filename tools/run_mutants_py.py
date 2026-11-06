#!/usr/bin/env python3
"""Prove the Python suite can fail: each mutation is a realistic bug, and must make it fail.

Set OMNILIMIT_TEST_REDIS_URLS and OMNILIMIT_TEST_POSTGRES_URLS to include the store mutants.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "python" / "src" / "omnilimit"

CORE = [
    (
        "retry-after rounds down",
        "_gcra.py",
        "next_unit_ms = ceil_div(max(0, level + params.window_ms - params.capacity), params.limit)",
        "next_unit_ms = max(0, level + params.window_ms - params.capacity) // params.limit",
    ),
    (
        "drain is not capped",
        "_gcra.py",
        "elapsed = min(t - ts, params.drain_ms)",
        "elapsed = t - ts\n"
        "    if elapsed > params.drain_ms:\n"
        "        new_level = params.window_ms + 1\n"
        "        return RawHit(allowed=True, level=new_level, ts=t), "
        "MeterState(level=new_level, ts=t)",
    ),
    ("clock regression refunds quota", "_gcra.py", "t = max(now_ms, ts)", "t = now_ms"),
    (
        "capacity check is off by one",
        "_gcra.py",
        "if drained + params.window_ms > params.capacity:",
        "if drained + params.window_ms >= params.capacity:",
    ),
    (
        "denied requests write state",
        "_gcra.py",
        "return RawHit(allowed=False, level=drained, ts=ts), None",
        "return RawHit(allowed=False, level=drained, ts=t), MeterState(level=drained, ts=t)",
    ),
    (
        "bool passes as a limit",
        "_gcra.py",
        "and not isinstance(value, bool) and",
        "and",
    ),
    (
        "X-Forwarded-For read from the left",
        "ip.py",
        "return canonical_ip(entries[len(entries) - 1 - hops])",
        "return canonical_ip(entries[0])",
    ),
    (
        "short chain falls back to first value",
        "ip.py",
        "if len(entries) <= hops:\n        return None",
        "if len(entries) <= hops:\n        return canonical_ip(entries[0])",
    ),
    (
        "IPv4-mapped not normalised",
        "ip.py",
        "if address.ipv4_mapped is not None:",
        "if False:",
    ),
    ("zone identifiers accepted", "ip.py", 'if "%" in raw:', "if False:"),
    (
        "only the first header line is read",
        "ip.py",
        "value = getlist(name) if callable(getlist) else headers.get(name)",
        "value = headers.get(name)",
    ),
    (
        "store failure inverts open/closed",
        "_limiter.py",
        'allowed = self.on_store_error == "open"',
        'allowed = self.on_store_error == "closed"',
    ),
    (
        "sync store timeout never fires",
        "_limiter.py",
        "if not finished.wait(max(0.0, deadline - time.monotonic())):",
        "if not finished.wait(None):",
    ),
    (
        "async store timeout never fires",
        "_limiter.py",
        "await asyncio.wait_for(result, core.timeout_s)",
        "await result",
    ),
    (
        "store threads are not capped",
        "_limiter.py",
        "if not slots.acquire(timeout=timeout_s):",
        "if False:",
    ),
    ("key ignores the salt", "_key.py", "_utf8(salt) + b", 'b"" + b'),
    ("key has no separator", "_key.py", 'b"\\x00"', 'b""'),
    (
        "retry-after header dropped",
        "_headers.py",
        'out["retry-after"] = str(_seconds(decision.retry_after_ms))',
        "pass",
    ),
    (
        "memory store races",
        "stores/memory.py",
        "with self._lock:",
        "if True:",
    ),
]

STORES = [
    (
        "redis: NOSCRIPT not recognised",
        "stores/redis.py",
        'type(error).__name__ == "NoScriptError"\n'
        '        or "NOSCRIPT" in str(error)\n'
        '        or "No matching script" in str(error)',
        "False",
    ),
    (
        "redis: reads only decoded replies",
        "stores/redis.py",
        "level=int(reply[1])",
        "level=int(reply[1].decode() if False else str(reply[1]))",
    ),
    (
        "postgres: bare connection may hold the row lock",
        "stores/postgres.py",
        'if not _is_pool(db) and getattr(db, "autocommit", False) is not True:',
        "if False:",
    ),
]

# "memory store races" needs threads to interleave, which the GIL makes rare without this.
SUITE_TIMEOUT_S = 120
RACE_ENV = {"OMNILIMIT_TEST_SWITCH_INTERVAL": "0.000001"}


def fails(tests: list[str], extra_env: dict[str, str] | None = None) -> bool:
    env = {**os.environ, **(extra_env or {})}
    try:
        result = subprocess.run(  # noqa: S603
            # The venv's Python directly: a timeout must kill pytest itself, not just a uv parent.
            [str(ROOT / "python/.venv/bin/python"), "-m", "pytest", "-x", "-q", *tests],
            cwd=ROOT / "python",
            env=env,
            capture_output=True,
            text=True,
            timeout=SUITE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # A mutant that hangs the suite has been noticed just as much as one that fails it.
        return True
    return result.returncode != 0


def run(mutants: list[tuple[str, str, str, str]], tests: list[str]) -> int:
    survived = 0
    for name, file, before, after in mutants:
        path = PACKAGE / file
        original = path.read_text(encoding="utf-8")
        if before not in original:
            raise SystemExit(f'mutant "{name}" no longer applies to {file}')
        try:
            path.write_text(original.replace(before, after), encoding="utf-8")
            killed = fails(tests, RACE_ENV)
        finally:
            path.write_text(original, encoding="utf-8")
        print(f"{'killed  ' if killed else 'SURVIVED'}  {name}", flush=True)
        survived += not killed
    return survived


def main() -> int:
    core_tests = ["tests/test_conformance.py", "tests/test_limiter.py", "tests/test_fastapi.py"]
    if fails(core_tests):
        raise SystemExit("the unmutated suite must pass first")
    survived = run(CORE, core_tests)
    has_stores = os.environ.get("OMNILIMIT_TEST_REDIS_URLS") and os.environ.get(
        "OMNILIMIT_TEST_POSTGRES_URLS"
    )
    if has_stores:
        survived += run(STORES, ["tests/test_stores.py"])
    elif os.environ.get("CI"):
        raise SystemExit("store mutants must run in CI")
    else:
        print("skipped store mutants: set OMNILIMIT_TEST_*_URLS")
    if survived:
        print(f"{survived} mutant(s) survived: the suite has a hole", file=sys.stderr)
        return 1
    print("all mutants killed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
