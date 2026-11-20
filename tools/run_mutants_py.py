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
        "cost is ignored",
        "_limiter.py",
        "window_ms=policy.window_ms * cost,",
        "window_ms=policy.window_ms,",
    ),
    (
        "cost above the burst is accepted",
        "_gcra.py",
        "if not _is_positive_int(cost) or cost > policy.burst:",
        "if not _is_positive_int(cost):",
    ),
    (
        "check_all counts before looking",
        "_recipes.py",
        "_refusal([entry[0].peek(entry[1], cost=_cost(entry)) for entry in entries])",
        "_refusal([entry[0].check(entry[1], cost=_cost(entry)) for entry in entries])",
    ),
    (
        "lockout counts successes",
        "_recipes.py",
        "            if self._reset_on_success:\n                self._limiter.reset(id_value)",
        "            self._limiter.check(id_value)",
    ),
    (
        "check_all stops peeking at the first denial",
        "_recipes.py",
        "refusal = _refusal([entry[0].peek(entry[1], cost=_cost(entry)) for entry in entries])",
        "peeks = []\n"
        "    for entry in entries:\n"
        "        peeks.append(entry[0].peek(entry[1], cost=_cost(entry)))\n"
        "        if not peeks[-1].allowed:\n"
        "            break\n"
        "    refusal = _refusal(peeks)",
    ),
    (
        "check_all denial tie picks the last",
        "_recipes.py",
        "if (decision.retry_after_ms or 0) > (governing.retry_after_ms or 0):",
        "if (decision.retry_after_ms or 0) >= (governing.retry_after_ms or 0):",
    ),
    (
        "check_all remaining tie picks the last",
        "_recipes.py",
        "decision.remaining < governing.remaining",
        "decision.remaining <= governing.remaining",
    ),
    (
        "check_all lets a null remaining govern",
        "_recipes.py",
        "        if decision.remaining is None:\n            continue\n",
        "        if decision.remaining is None:\n            governing = decision\n            continue\n",
    ),
    (
        "check_all keeps counting after a denial",
        "_recipes.py",
        "        decision = entry[0].check(entry[1], cost=_cost(entry))\n"
        "        decisions.append(decision)\n"
        "        if not decision.allowed:\n"
        "            return CombinedDecision(allowed=False, decision=decision, decisions=tuple(decisions))",
        "        decision = entry[0].check(entry[1], cost=_cost(entry))\n"
        "        decisions.append(decision)\n"
        "    for decision in decisions:\n"
        "        if not decision.allowed:\n"
        "            return CombinedDecision(allowed=False, decision=decision, decisions=tuple(decisions))",
    ),
    (
        "recipes validate costs lazily",
        "_recipes.py",
        "        limiter._core.weighed(_cost(entry))\n",
        "",
    ),
    (
        "recipes notice approximate stores at their peek",
        "_recipes.py",
        'if limiter.tier == "approximate":',
        'if limiter.tier == "approximate" and False:',
    ),
    (
        "lockout counts a failure when verify raises",
        "_recipes.py",
        "        if verify():",
        "        try:\n"
        "            verified = verify()\n"
        "        except Exception:\n"
        "            self._limiter.check(id_value)\n"
        "            raise\n"
        "        if verified:",
    ),
    (
        "shorthand accepts plurals",
        "_gcra.py",
        '(second|minute|hour|day)")',
        '(second|minute|hour|day)s?")',
    ),
    (
        "shorthand matches a prefix",
        "_gcra.py",
        "_SHORTHAND.fullmatch(text)",
        "_SHORTHAND.match(text)",
    ),
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
        "await asyncio.wait_for(result, self._core.timeout_s)",
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
    (
        "peek writes like a hit",
        "stores/memory.py",
        "return apply_peek(entry[0] if entry else None, params, now)",
        "hit, write = apply_hit(entry[0] if entry else None, params, now)\n"
        "            if write is not None:\n"
        "                self._entries[key] = (write, write.ts + params.drain_ms)\n"
        "            return hit",
    ),
    (
        "refund gives the whole cost whatever the drain",
        "_gcra.py",
        "give = max(0, params.window_ms - min(t - since, params.drain_ms) * params.limit)",
        "give = params.window_ms",
    ),
    (
        "refund goes below zero",
        "_gcra.py",
        "level = max(0, drained - give)",
        "level = drained - give",
    ),
    (
        "refund drains from now instead of since",
        "_gcra.py",
        "min(t - since, params.drain_ms)",
        "min(t - now_ms, params.drain_ms)",
    ),
    (
        "refund of a reservation older than the bucket gives",
        "_gcra.py",
        "if t >= since:",
        "if True:",
    ),
    (
        "refund on a missing bucket creates it",
        "_gcra.py",
        "return RawRefund(level=0, ts=now_ms), None",
        "return RawRefund(level=0, ts=now_ms), MeterState(level=0, ts=now_ms)",
    ),
    (
        "refund allowed twice",
        "_limiter.py",
        "            if reservation._spent:\n                return None\n",
        "",
    ),
    (
        "failed refund reports success",
        "_limiter.py",
        '            self._core.report_failure(error, "refund")\n            return False',
        '            self._core.report_failure(error, "refund")\n            return True',
    ),
    (
        "a denial is a reservation",
        "_limiter.py",
        "if hit is None or not hit.allowed or hit.ts is None:",
        "if hit is None or hit.ts is None:",
    ),
    (
        "a reservation from another limiter is refunded",
        "_limiter.py",
        "or reservation._owner is not self:",
        ":",
    ),
    (
        "memory refund does not write",
        "stores/memory.py",
        "                self._entries[key] = (write, write.ts + params.drain_ms)\n            return refund",
        "                pass\n            return refund",
    ),
    (
        "reset does not delete",
        "stores/memory.py",
        "self._entries.pop(key, None)",
        "self._entries.get(key)",
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
        "redis: refund reply read as a hit",
        "stores/redis.py",
        "return RawRefund(level=int(reply[0]), ts=int(reply[1]))",
        "return RawRefund(level=int(reply[1]), ts=int(reply[0]))",
    ),
    (
        "postgres: refund ignores since",
        "stores/postgres.py",
        "params.drain_ms, since, now_ms)",
        "params.drain_ms, 0, now_ms)",
    ),
    (
        "postgres: bare connection may hold the row lock",
        "stores/postgres.py",
        'if not _is_pool(db) and getattr(db, "autocommit", False) is not True:',
        "if False:",
    ),
]

SUITE_TIMEOUT_S = 120


def fails(tests: list[str]) -> bool:
    try:
        result = subprocess.run(  # noqa: S603
            # The venv's Python directly: a timeout must kill pytest itself, not just a uv parent.
            [str(ROOT / "python/.venv/bin/python"), "-m", "pytest", "-x", "-q", *tests],
            cwd=ROOT / "python",
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
            killed = fails(tests)
        finally:
            path.write_text(original, encoding="utf-8")
        print(f"{'killed  ' if killed else 'SURVIVED'}  {name}", flush=True)
        survived += not killed
    return survived


def main() -> int:
    core_tests = [
        "tests/test_conformance.py",
        "tests/test_limiter.py",
        "tests/test_fastapi.py",
        "tests/test_recipes.py",
    ]
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
