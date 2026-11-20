#!/usr/bin/env python3
"""Generate the conformance vectors in spec/vectors.

This is deliberately an independent implementation written in Python with only
the standard library: if the TypeScript port produced its own expectations it
would only be validating itself. Expected IPs come from the `ipaddress` module.

Usage: gen_vectors.py [--check]   (--check fails if the committed vectors differ)
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import random
import shutil
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "spec/vectors"
SPEC = (ROOT / "spec/VERSION").read_text(encoding="utf-8").strip()
BASE = 1_700_000_000_000
MAX_SAFE = 2**53 - 1
SEQ_TIERS = ["exact", "local"]


def ceil_div(a: int, b: int) -> int:
    return -((-a) // b)


class Meter:
    """Integer meter. `check_exact` cross-checks it against rational arithmetic."""

    def __init__(self, limit: int, window_ms: int, burst: int) -> None:
        self.limit, self.window, self.burst = limit, window_ms, burst
        self.capacity = burst * window_ms
        self.drain = ceil_div(self.capacity, limit)
        self.level = 0
        self.ts: int | None = None
        self.exact = Fraction(0)

    def hit(self, now: int, cost: int = 1) -> dict:
        assert 1 <= cost <= self.burst, "a cost above the burst can never pass: it is an error, not a step"
        amount = cost * self.window
        ts = now if self.ts is None else self.ts
        t = max(now, ts)
        elapsed = min(t - ts, self.drain)
        drained = max(0, self.level - elapsed * self.limit)
        rate = Fraction(self.limit, self.window)
        exact = max(Fraction(0), self.exact - elapsed * rate)
        assert Fraction(drained, self.window) == exact, "integer meter diverged from the rational model"
        if drained + amount <= self.capacity:
            self.level, self.ts = drained + amount, t
            self.exact = exact + cost
            allowed = True
            level = self.level
            retry = None
        else:
            allowed = False
            level = drained
            retry = ceil_div(drained + amount - self.capacity, self.limit)
        remaining = (self.capacity - level) // self.window
        reset = ceil_div(level, self.limit)
        if not allowed:
            effective = retry
        elif remaining == 0:
            effective = ceil_div(level + self.window - self.capacity, self.limit)
        else:
            effective = reset
        return {
            "allowed": allowed,
            "remaining": remaining,
            "reset_ms": reset,
            "retry_after_ms": retry,
            "state": {"level": level, "ts": self.ts if self.ts is not None else now},
            "effective_ms": effective,
        }

    def peek(self, now: int, cost: int = 1) -> dict:
        """The decision a hit would get now. Nothing is written."""
        assert 1 <= cost <= self.burst
        amount = cost * self.window
        ts = now if self.ts is None else self.ts
        t = max(now, ts)
        drained = max(0, self.level - min(t - ts, self.drain) * self.limit)
        allowed = drained + amount <= self.capacity
        retry = None if allowed else ceil_div(drained + amount - self.capacity, self.limit)
        remaining = (self.capacity - drained) // self.window
        reset = ceil_div(drained, self.limit)
        # Allowed implies remaining >= 1, so the effective window is the reset time.
        effective = reset if allowed else retry
        return {
            "allowed": allowed,
            "remaining": remaining,
            "reset_ms": reset,
            "retry_after_ms": retry,
            "state": {"level": drained, "ts": ts},
            "effective_ms": effective,
        }

    def reset(self) -> None:
        self.level, self.ts, self.exact = 0, None, Fraction(0)

    def refund(self, now: int, since: int, cost: int) -> dict:
        """Give back a reservation of `cost` units made at `since` (the ts its hit stored)."""
        assert 1 <= cost <= self.burst
        ts = now if self.ts is None else self.ts
        t = max(now, ts)
        elapsed = min(t - ts, self.drain)
        drained = max(0, self.level - elapsed * self.limit)
        rate = Fraction(self.limit, self.window)
        exact = max(Fraction(0), self.exact - elapsed * rate)
        assert Fraction(drained, self.window) == exact, "integer meter diverged from the rational model"
        if t < since:
            # The bucket is older than the reservation: it was reset or expired, so it is gone.
            give, give_exact = 0, Fraction(0)
        else:
            since_elapsed = min(t - since, self.drain)
            give = max(0, cost * self.window - since_elapsed * self.limit)
            give_exact = max(Fraction(0), cost - since_elapsed * rate)
            # The cap at `drain` must not change the answer: past it, nothing is left to give.
            assert give == max(0, cost * self.window - (t - since) * self.limit)
        assert Fraction(give, self.window) == give_exact, "refund diverged from the rational model"
        if self.ts is None or drained == 0 or give == 0:
            # A refund that changes nothing writes nothing, and reports what a peek would.
            return {"level": drained, "ts": ts}
        self.level, self.ts = max(0, drained - give), t
        self.exact = max(Fraction(0), exact - give_exact)
        return {"level": self.level, "ts": t}

    def force(self, now: int, cost: int) -> int:
        """Count a hit without deciding it: replays a hit that was allowed in another history."""
        ts = now if self.ts is None else self.ts
        t = max(now, ts)
        elapsed = min(t - ts, self.drain)
        drained = max(0, self.level - elapsed * self.limit)
        exact = max(Fraction(0), self.exact - elapsed * Fraction(self.limit, self.window))
        self.level, self.ts, self.exact = drained + cost * self.window, t, exact + cost
        return t

    def level_at(self, now: int) -> int:
        ts = now if self.ts is None else self.ts
        return max(0, self.level - min(max(now, ts) - ts, self.drain) * self.limit)


def counterfactual(limit: int, window_ms: int, burst: int, history: list[dict], skip: int, now: int) -> int:
    """The level at `now` had step `skip` not been counted, every other step happening as it did.

    `history` holds one id's steps in order. Hits that were allowed count, whether or not they
    would fit here: the claim is about the same history without one reservation.
    """
    meter = Meter(limit, window_ms, burst)
    # With a clock going backwards, a hit may store another ts here than it did for real.
    since: dict[int, int] = {}
    for event in history:
        if event["index"] == skip:
            continue
        if event["op"] == "hit" and event["allowed"]:
            since[event["index"]] = meter.force(event["at"], event["cost"])
        elif event["op"] == "reset":
            meter.reset()
        elif event["op"] == "refund" and event["applied"]:
            meter.refund(event["at"], since[event["of"]], event["cost"])
    return meter.level_at(now)


def headers(scope: str, limit: int, window_ms: int, r: dict) -> dict:
    t = ceil_div(r["effective_ms"], 1000)
    w = ceil_div(window_ms, 1000)
    draft = {"ratelimit-policy": f'"{scope}";q={limit};w={w}', "ratelimit": f'"{scope}";r={r["remaining"]};t={t}'}
    legacy = {
        "x-ratelimit-limit": str(limit),
        "x-ratelimit-remaining": str(r["remaining"]),
        "x-ratelimit-reset": str(t),
    }
    if not r["allowed"]:
        draft["retry-after"] = legacy["retry-after"] = str(ceil_div(r["retry_after_ms"], 1000))
    return {"draft-11": draft, "legacy": legacy}


def sequence(vector_id: str, limit: int, window_ms: int, burst: int, moves: list[tuple], scope: str = "t") -> dict:
    """`moves` are `(at_ms, id)` hits, or `(at_ms, id, op)` with op `hit`, `peek` or `reset`, or
    `(at_ms, id, op, cost)` for a hit or peek that weighs more than one unit, or
    `(at_ms, id, "refund", of)` giving back the hit at step `of`. `of` may be a function that
    picks one from `(fresh, spent)`, the allowed hit steps of that id not refunded yet and
    already refunded, or returns `None` to drop the move."""
    meters: dict[str, Meter] = {}
    histories: dict[str, list[dict]] = {}
    steps = []
    for move in moves:
        at, who = move[0], move[1]
        op = move[2] if len(move) > 2 else "hit"
        meter = meters.setdefault(who, Meter(limit, window_ms, burst))
        history = histories.setdefault(who, [])
        if op == "reset":
            meter.reset()
            history.append({"index": len(steps), "op": "reset", "at": at})
            steps.append({"at_ms": at, "id": who, "op": "reset"})
            continue
        if op == "refund":
            of = move[3]
            if callable(of):
                fresh = [e["index"] for e in history if e["op"] == "hit" and e["allowed"] and not e["refunded"]]
                spent = [e["index"] for e in history if e["op"] == "hit" and e["allowed"] and e["refunded"]]
                of = of(fresh, spent)
                if of is None:
                    continue
            target = next(e for e in history if e["index"] == of)
            assert target["op"] == "hit" and target["allowed"], "only an allowed hit is a reservation"
            event = {"index": len(steps), "op": "refund", "at": at, "of": of, "since": target["since"], "cost": target["cost"]}
            history.append(event)
            if target["refunded"]:
                event["applied"] = False
                steps.append({"at_ms": at, "id": who, "op": "refund", "of": of, "expect": {"refunded": False}})
                continue
            target["refunded"] = True
            event["applied"] = True
            # Across a reset, a refund may take units counted after it: the spec leaves that case
            # out of the comparison, so it is checked only when no refund since `of` crossed one.
            event["crossed"] = any(e["op"] == "reset" and e["index"] > of for e in history)
            crossed = any(e["op"] == "refund" and e["applied"] and e["crossed"] and e["index"] > of for e in history)
            # Both buckets are compared at the store's time, which a clock going backwards
            # does not move back.
            # The comparison assumes a store clock that does not go backwards: with one that does,
            # the reservation's ts changes how every later step drains, so there is no single
            # history without it to compare with.
            times = [e["at"] for e in history]
            monotone = all(x <= y for x, y in zip(times, times[1:]))
            state = meter.refund(at, target["since"], target["cost"])
            if monotone and not crossed:
                floor = counterfactual(limit, window_ms, burst, history[:-1], of, at)
                assert meter.level_at(at) >= floor, f"{vector_id}: refund of step {of} is more generous than never counting it"
            steps.append({"at_ms": at, "id": who, "op": "refund", "of": of, "expect": {"refunded": True, "state": state}})
            continue
        cost = move[3] if len(move) > 3 else 1
        r = meter.peek(at, cost) if op == "peek" else meter.hit(at, cost)
        if op == "hit":
            history.append(
                {"index": len(steps), "op": "hit", "at": at, "cost": cost, "allowed": r["allowed"], "since": r["state"]["ts"], "refunded": False}
            )
        # `op` and `cost` are only written when they are not the default, so earlier vectors
        # keep their bytes.
        tag = {"op": op} if op != "hit" else {}
        if cost != 1:
            tag["cost"] = cost
        steps.append(
            {
                "at_ms": at,
                "id": who,
                **tag,
                "expect": {
                    "allowed": r["allowed"],
                    "reason": "ok" if r["allowed"] else "limited",
                    "status": None if r["allowed"] else 429,
                    "remaining": r["remaining"],
                    "reset_ms": r["reset_ms"],
                    "retry_after_ms": r["retry_after_ms"],
                    "state": r["state"],
                    "headers": headers(scope, limit, window_ms, r),
                },
            }
        )
    return {
        "spec": SPEC,
        "id": vector_id,
        "kind": "sequence",
        "tiers": SEQ_TIERS,
        "policy": {"limit": limit, "window_ms": window_ms, "burst": burst},
        "scope": scope,
        "salt": "s",
        "steps": steps,
    }


def sequences() -> dict[str, dict]:
    b = BASE
    a = "a"
    named = {
        "burst-refill-3-per-s": (3, 1000, 3, [(b, a), (b, a), (b, a), (b, a), (b + 334, a)]),
        "no-rounding-drift-1000-over-3": (3, 1000, 3, [(b + i * 334, a) for i in range(12)]),
        "five-per-minute-bound-is-b-plus-l-minus-1": (5, 60_000, 5, [(b + i * 6_000, a) for i in range(11)]),
        "login-burst-2": (5, 60_000, 2, [(b, a), (b, a), (b, a), (b + 12_000, a), (b + 12_000, a)]),
        "burst-larger-than-limit": (2, 1000, 10, [(b, a)] * 11 + [(b + 500, a), (b + 500, a)]),
        "burst-of-one": (1, 1000, 1, [(b, a), (b + 999, a), (b + 1000, a)]),
        "idle-beyond-drain-equals-absent": (3, 1000, 3, [(b, a), (b, a), (b + 86_400_000, a)]),
        "clock-regression-does-not-refund": (3, 1000, 3, [(b, a), (b - 5_000, a), (b - 5_000, a), (b - 5_000, a)]),
        "denied-does-not-extend-the-wait": (1, 10_000, 1, [(b, a), (b + 1000, a), (b + 5000, a), (b + 9999, a), (b + 10_000, a)]),
        "ids-are-independent": (1, 1000, 1, [(b, "a"), (b, "b"), (b, "a"), (b, "b")]),
        "hour-window": (100, 3_600_000, 100, [(b + i * 36_000, a) for i in range(5)]),
        "day-window-large-burst": (50_000_000, 86_400_000, 50_000_000, [(b, a), (b + 1, a)]),
        "prime-window": (7, 1009, 3, [(b + i * 50, a) for i in range(14)]),
        # Levels with more than 14 significant digits: Lua's tostring() would corrupt them.
        "levels-keep-every-digit": (1, 123_456_789_012_345, 3, [(b, a), (b, a), (b + 1, a), (b + 1, a)]),
    }
    out = {f"sequence/{k}": sequence(f"sequence/{k}", *v) for k, v in named.items()}
    rng = random.Random(20261106)
    for n in range(40):
        limit = rng.choice([1, 2, 3, 5, 7, 10, 13, 60, 100, 997])
        window = rng.choice([1000, 1009, 1500, 10_000, 60_000, 86_400_000])
        burst = rng.choice([1, 2, 3, limit, limit + 1, limit * 2])
        at = BASE + rng.randrange(1000)
        moves = []
        for _ in range(rng.randrange(8, 40)):
            at += rng.choice([0, 0, 1, 3, 17, 100, 333, 1000, window // limit, window // limit + 1, window])
            moves.append((at, rng.choice("aab")))
        out[f"sequence/random-{n:03d}"] = sequence(f"sequence/random-{n:03d}", limit, window, burst, moves)

    p, r = "peek", "reset"
    with_ops = {
        "peek-does-not-consume": (3, 1000, 3, [(b, a, p), (b, a, p), (b, a), (b, a, p), (b, a), (b, a), (b, a, p), (b, a)]),
        "peek-on-empty-bucket": (5, 60_000, 5, [(b, a, p), (b + 10, a, p)]),
        "peek-after-denial-reports-the-same-wait": (1, 10_000, 1, [(b, a), (b + 1000, a), (b + 1000, a, p), (b + 4000, a, p), (b + 10_000, a, p)]),
        "peek-drains-like-a-hit": (2, 1000, 2, [(b, a), (b, a), (b + 499, a, p), (b + 500, a, p), (b + 500, a)]),
        "peek-clock-regression": (3, 1000, 3, [(b, a), (b - 5_000, a, p), (b - 5_000, a)]),
        "reset-mid-burst-refills": (2, 60_000, 2, [(b, a), (b, a), (b, a), (b + 1, a, r), (b + 2, a), (b + 2, a), (b + 2, a)]),
        "reset-missing-bucket-is-a-no-op": (2, 1000, 2, [(b, a, r), (b, a), (b, a), (b, a)]),
        "reset-only-touches-its-id": (1, 60_000, 1, [(b, "a"), (b, "b"), (b, "a", r), (b, "a"), (b, "b")]),
        # Count failures only: peek before the work, hit on failure, reset on success.
        "count-failures-only": (
            5,
            900_000,
            5,
            [(b + i, a, op) for i, op in enumerate([p, "hit", p, "hit", p, "hit", p, r, p, "hit"] + [x for _ in range(5) for x in (p, "hit")])],
        ),
    }
    for k, v in with_ops.items():
        out[f"sequence/{k}"] = sequence(f"sequence/{k}", *v)

    h = "hit"
    with_cost = {
        "cost-spends-several-units": (10, 60_000, 10, [(b, a, h, 3), (b, a, h, 3), (b, a, h, 3), (b, a, h, 3), (b, a), (b, a)]),
        "cost-equal-to-burst-needs-an-empty-meter": (5, 60_000, 5, [(b, a, h, 5), (b, a), (b + 12_000, a), (b + 12_000, a, h, 5), (b + 72_000, a, h, 5)]),
        "cost-denied-reports-the-wait-for-the-whole-cost": (4, 4000, 4, [(b, a, h, 3), (b, a, h, 2), (b + 500, a, h, 2), (b + 1000, a, h, 2), (b + 1000, a)]),
        "cost-denied-does-not-write": (3, 3000, 3, [(b, a, h, 2), (b, a, h, 2), (b, a), (b, a)]),
        "cost-peek-does-not-consume": (6, 6000, 6, [(b, a, p, 4), (b, a, h, 4), (b, a, p, 4), (b, a, p, 2), (b, a, h, 2), (b, a, p)]),
        "cost-with-burst-below-limit": (10, 10_000, 3, [(b, a, h, 3), (b, a), (b + 1000, a), (b + 3000, a, h, 3), (b + 3000, a, h, 2)]),
        "cost-with-burst-above-limit": (2, 1000, 8, [(b, a, h, 8), (b + 500, a), (b + 500, a), (b + 4000, a, h, 8), (b + 4000, a, h, 7)]),
        "cost-mixed-with-reset": (4, 60_000, 4, [(b, a, h, 4), (b, a, h, 2), (b + 1, a, r), (b + 2, a, h, 2), (b + 2, a, h, 2), (b + 2, a)]),
        "cost-keeps-every-digit": (1, 123_456_789_012_345, 3, [(b, a, h, 2), (b, a, h, 2), (b + 1, a), (b + 1, a)]),
    }
    for k, v in with_cost.items():
        out[f"sequence/{k}"] = sequence(f"sequence/{k}", *v)

    rng = random.Random(20261118)
    for n in range(20):
        limit = rng.choice([2, 3, 5, 7, 10, 60, 997])
        window = rng.choice([1000, 1009, 10_000, 60_000, 900_000])
        burst = rng.choice([2, 3, limit, limit + 1, limit * 2])
        at = BASE + rng.randrange(1000)
        moves = []
        for _ in range(rng.randrange(10, 40)):
            at += rng.choice([0, 0, 1, 17, 333, 1000, window // limit, window // limit + 1, window])
            cost = rng.choice([1, 1, 2, 3, burst, max(1, burst // 2), rng.randrange(1, burst + 1)])
            moves.append((at, rng.choice("aab"), rng.choice([h, h, h, p, r]), min(cost, burst)))
        out[f"sequence/cost-random-{n:03d}"] = sequence(f"sequence/cost-random-{n:03d}", limit, window, burst, moves)

    rng = random.Random(20261110)
    for n in range(20):
        limit = rng.choice([1, 2, 3, 5, 7, 10, 60, 997])
        window = rng.choice([1000, 1009, 10_000, 60_000, 900_000])
        burst = rng.choice([1, 2, 3, limit, limit + 1])
        at = BASE + rng.randrange(1000)
        moves = []
        for _ in range(rng.randrange(10, 40)):
            at += rng.choice([0, 0, 1, 17, 333, 1000, window // limit, window // limit + 1])
            moves.append((at, rng.choice("aab"), rng.choice(["hit", "hit", "hit", p, p, r])))
        out[f"sequence/ops-random-{n:03d}"] = sequence(f"sequence/ops-random-{n:03d}", limit, window, burst, moves)

    f = "refund"
    with_refund = {
        # Given back at once, the reservation leaves no trace: the meter is where it was before it.
        "refund-immediately-restores-the-meter": (3, 1000, 3, [(b, a), (b, a), (b, a, f, 1), (b, a), (b, a), (b, a)]),
        # 200 ms later the meter drained 400 of the 1000 units the hit added: only 600 come back.
        "refund-after-partial-drain-gives-the-rest": (2, 1000, 2, [(b, a), (b, a), (b + 200, a, f, 1), (b + 200, a), (b + 200, a)]),
        "refund-after-full-drain-gives-nothing": (1, 1000, 1, [(b, a), (b + 1000, a, f, 0), (b + 1000, a), (b + 1500, a, f, 2)]),
        "refund-with-cost": (10, 60_000, 10, [(b, a, h, 4), (b, a, h, 3), (b + 6000, a, f, 0), (b + 6000, a, h, 6), (b + 6000, a, h, 2)]),
        # The reservation went with the reset; what was counted after it is not taken below zero.
        "refund-never-goes-below-zero": (1, 10_000, 3, [(b, a, h, 3), (b + 1, a, r), (b + 1, a), (b + 1, a, f, 0), (b + 1, a), (b + 1, a)]),
        # A denied request in between does not make the refund generous: it was never counted.
        "refund-after-other-hits-is-not-more-generous": (1, 1000, 2, [(b, a), (b, a), (b, a), (b + 100, a, f, 0), (b + 100, a), (b + 100, a)]),
        "refund-clock-regression": (
            3,
            1000,
            3,
            [(b, a), (b, a), (b - 5000, a, f, 1), (b - 5000, a), (b - 5000, a), (b, a, r), (b - 5000, a), (b - 5000, a, f, 4), (b - 5000, a, p)],
        ),
        "refund-on-missing-bucket-writes-nothing": (2, 1000, 2, [(b, a), (b + 1, a, r), (b + 2, a, f, 0), (b - 9000, a, p), (b + 2, a), (b + 2, a), (b + 2, a)]),
        "refund-twice-gives-once": (2, 60_000, 2, [(b, a), (b, a), (b, a, f, 1), (b, a, f, 1), (b, a), (b, a)]),
        "refund-only-touches-its-id": (1, 60_000, 1, [(b, "a"), (b, "b"), (b, "a", f, 0), (b, "a"), (b, "b")]),
        # Reservations overlap: each one gives back only its own units.
        "refund-overlapping-reservations": (
            5,
            5000,
            5,
            [(b, a, h, 2), (b + 100, a, h, 2), (b + 300, a), (b + 400, a, f, 1), (b + 900, a, f, 0), (b + 900, a, f, 2), (b + 900, a, h, 5)],
        ),
    }
    for k, v in with_refund.items():
        out[f"sequence/{k}"] = sequence(f"sequence/{k}", *v)

    rng = random.Random(20261120)

    def pick(fresh: list[int], spent: list[int]) -> int | None:
        if spent and rng.random() < 0.1:
            return rng.choice(spent)
        return rng.choice(fresh) if fresh else None

    for n in range(10):
        limit = rng.choice([1, 2, 3, 5, 7, 10, 60, 997])
        window = rng.choice([1000, 1009, 10_000, 60_000, 900_000])
        burst = rng.choice([1, 2, 3, limit, limit + 1, limit * 2])
        at = BASE + rng.randrange(1000)
        moves = []
        for _ in range(rng.randrange(12, 40)):
            at += rng.choice([0, 0, 1, 17, 333, 1000, window // limit, window // limit + 1, window])
            op = rng.choice([h, h, h, f, f, p, r])
            who = rng.choice("aab")
            if op == f:
                moves.append((at, who, f, pick))
            else:
                cost = min(burst, rng.choice([1, 1, 2, 3, burst, max(1, burst // 2)]))
                moves.append((at, who, op, cost))
        out[f"sequence/refund-random-{n:03d}"] = sequence(f"sequence/refund-random-{n:03d}", limit, window, burst, moves)
    return out


def key_for(scope: str, limit: int, window: int, burst: int, salt: str, who: str) -> str:
    digest = hashlib.sha256(salt.encode() + b"\x00" + who.encode()).hexdigest()[:32]
    return f"ol:1:{scope}:{limit}-{window}-{burst}:{digest}"


def keys() -> dict[str, dict]:
    cases = []
    for scope, limit, window, burst, salt, who in [
        ("login", 5, 60_000, 2, "pepper", "203.0.113.7"),
        ("api.v1", 100, 1000, 100, "", "user-1"),
        ("a_b-c", 1, 1000, 1, "ñandú", "josé@example.com"),
        ("t", 3, 1000, 3, "s", ""),
        ("t", 3, 1000, 3, "a\u0000b", "c"),
        ("t", 3, 1000, 3, "a", "\u0000bc"),
        ("t", 3, 1000, 3, "s", "🦙"),
    ]:
        cases.append(
            {
                "scope": scope,
                "policy": {"limit": limit, "window_ms": window, "burst": burst},
                "salt": salt,
                "id": who,
                "expect": key_for(scope, limit, window, burst, salt, who),
            }
        )
    return {"key/derivation": {"spec": SPEC, "id": "key/derivation", "kind": "key", "cases": cases}}


def canonical(raw: str | None) -> str | None:
    if raw is None or "%" in raw:
        return None
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return ip.compressed


def from_xff(value: str | None, hops: int) -> str | None:
    if value is None:
        return None
    parts = [p.strip() for p in value.split(",")]
    if len(parts) <= hops:
        return None
    return canonical(parts[len(parts) - 1 - hops])


def masked(ip: str | None, prefix: int) -> str | None:
    if ip is None:
        return None
    parsed = ipaddress.ip_address(ip)
    if isinstance(parsed, ipaddress.IPv4Address):
        return ip
    return ipaddress.ip_network(f"{ip}/{prefix}", strict=False).network_address.compressed


def ips() -> dict[str, dict]:
    spoof = "6.6.6.6"
    client = "203.0.113.7"
    lb = "198.51.100.2"
    cases = []

    def xff(name, value, hops):
        cases.append(
            {
                "name": name,
                "headers": {} if value is None else {"x-forwarded-for": value},
                "peer": "10.0.0.1",
                "strategy": {"xff": {"hops": hops}},
                "expect": from_xff(value, hops),
            }
        )

    xff("rightmost-with-zero-hops", f"{spoof}, {client}", 0)
    xff("spoofed-left-values-are-ignored", f"{spoof}, 7.7.7.7, {client}, {lb}", 1)
    xff("chain-shorter-than-hops-is-null", client, 1)
    xff("chain-equal-to-hops-is-null", f"{client}, {lb}", 2)
    xff("missing-header-is-null", None, 0)
    xff("empty-header-is-null", "", 0)
    xff("garbage-is-null", "not-an-ip", 0)
    xff("garbage-in-trusted-position-is-null", f"{client}, junk", 0)
    xff("empty-entry-is-null", f"{client}, ", 0)
    xff("ipv4-leading-zero-is-null", "010.0.0.1", 0)
    xff("ipv4-with-port-is-null", "203.0.113.7:443", 0)
    xff("ipv4-out-of-range-is-null", "256.0.0.1", 0)
    xff("ipv6-is-canonicalised", "2001:0DB8:0000:0000:0000:0000:0000:0001", 0)
    xff("ipv6-compresses-longest-run", "2001:db8:0:0:1:0:0:1", 0)
    xff("ipv6-compresses-first-of-equal-runs", "2001:0:0:1:0:0:1:1", 0)
    xff("ipv6-single-zero-not-compressed", "2001:db8:0:1:1:1:1:1", 0)
    xff("ipv4-mapped-becomes-ipv4", "::ffff:203.0.113.7", 0)
    xff("ipv4-mapped-hex-becomes-ipv4", "::ffff:cb00:7107", 0)
    xff("ipv6-zone-is-null", "fe80::1%eth0", 0)
    xff("ipv6-unspecified", "::", 0)
    xff("ipv6-trailing-ipv4", "64:ff9b::192.0.2.33", 0)

    for name, headers_, peer, preset, expect in [
        ("cloudflare-reads-its-header", {"cf-connecting-ip": client, "x-forwarded-for": spoof}, "10.0.0.1", "cloudflare", client),
        ("cloudflare-without-header-is-null", {"x-forwarded-for": client}, "10.0.0.1", "cloudflare", None),
        ("cloudflare-with-list-is-null", {"cf-connecting-ip": f"{spoof}, {client}"}, "10.0.0.1", "cloudflare", None),
        ("cloud-run-takes-rightmost", {"x-forwarded-for": f"{spoof}, {client}"}, "169.254.1.1", "cloud-run", client),
        ("cloud-run-lb-skips-the-balancer", {"x-forwarded-for": f"{spoof}, {client}, {lb}"}, "169.254.1.1", "cloud-run-lb", client),
        ("vercel-reads-its-header", {"x-vercel-forwarded-for": client, "x-forwarded-for": spoof}, None, "vercel", client),
        ("direct-uses-peer-and-ignores-xff", {"x-forwarded-for": spoof}, client, "direct", client),
        ("direct-canonicalises-mapped-peer", {}, f"::ffff:{client}", "direct", client),
        ("direct-without-peer-is-null", {"x-forwarded-for": spoof}, None, "direct", None),
    ]:
        cases.append({"name": name, "headers": headers_, "peer": peer, "strategy": {"preset": preset}, "expect": expect})

    masks = []
    for ip, prefix in [
        ("2001:db8:1:2:3:4:5:6", 64),
        ("2001:db8:1:2:3:4:5:6", 48),
        ("2001:db8:1:2:3:4:5:6", 128),
        ("2001:db8:1:2:ffff::", 57),
        ("203.0.113.7", 64),
    ]:
        masks.append({"ip": ip, "prefix": prefix, "expect": masked(ip, prefix)})
    return {"ip/strategies": {"spec": SPEC, "id": "ip/strategies", "kind": "ip", "cases": cases, "masks": masks}}


def failures() -> dict[str, dict]:
    out = {}
    open_expect = {"allowed": True, "reason": "store_error", "status": None, "remaining": None, "reset_ms": None, "retry_after_ms": None, "headers": {}}
    closed_expect = {"allowed": False, "reason": "store_error", "status": 503, "remaining": None, "reset_ms": None, "retry_after_ms": 1000, "headers": {"retry-after": "1"}}
    for fault in ["error", "timeout", "missing_binding"]:
        for mode, expect in [("open", open_expect), ("closed", closed_expect)]:
            vid = f"failure/{fault}-{mode}"
            out[vid] = {
                "spec": SPEC,
                "id": vid,
                "kind": "failure",
                "fault": fault,
                "on_store_error": mode,
                "policy": {"limit": 5, "window_ms": 60_000, "burst": 5},
                "expect": expect,
            }
    # A refund that fails is reported as `false` whatever the mode: no request is being decided.
    # Only the refund fails: the hit before it is what makes the reservation.
    for fault in ["error", "timeout"]:
        for mode in ["open", "closed"]:
            vid = f"failure/refund-{fault}-{mode}"
            out[vid] = {
                "spec": SPEC,
                "id": vid,
                "kind": "failure",
                "op": "refund",
                "fault": fault,
                "on_store_error": mode,
                "policy": {"limit": 5, "window_ms": 60_000, "burst": 5},
                "expect": {"refunded": False},
            }
    return out


def concurrency() -> dict[str, dict]:
    return {
        "concurrency/exactly-burst-allowed": {
            "spec": SPEC,
            "id": "concurrency/exactly-burst-allowed",
            "kind": "concurrency",
            "tiers": ["exact"],
            "policy": {"limit": 10, "window_ms": 60_000, "burst": 25},
            "scope": "c",
            "salt": "s",
            "id_value": "a",
            "hits": 200,
            "at_ms": BASE,
            "expect_allowed": 25,
        }
    }


def contract() -> dict[str, dict]:
    limit_for_day = (MAX_SAFE - 1) // (2 * 86_400_000)
    policies = [
        ({"limit": 1, "window_ms": 1000, "burst": 1}, True),
        ({"limit": 5, "window_ms": 60_000, "burst": 5}, True),
        ({"limit": limit_for_day, "window_ms": 86_400_000, "burst": limit_for_day}, 2 * limit_for_day * 86_400_000 + limit_for_day <= MAX_SAFE),
        ({"limit": limit_for_day + 1000, "window_ms": 86_400_000, "burst": limit_for_day + 1000}, False),
        ({"limit": 0, "window_ms": 1000, "burst": 1}, False),
        ({"limit": 1, "window_ms": 999, "burst": 1}, False),
        ({"limit": 1, "window_ms": 1000, "burst": 0}, False),
        ({"limit": 1.5, "window_ms": 1000, "burst": 1}, False),
        ({"limit": 1, "window_ms": 1000.5, "burst": 1}, False),
        ({"limit": -1, "window_ms": 1000, "burst": 1}, False),
    ]
    return {
        "contract/policy-validation": {
            "spec": SPEC,
            "id": "contract/policy-validation",
            "kind": "contract",
            "subject": "policy",
            "cases": [{"policy": p, "valid": v} for p, v in policies],
        },
        "contract/cost-validation": {
            "spec": SPEC,
            "id": "contract/cost-validation",
            "kind": "contract",
            "subject": "cost",
            "policy": {"limit": 10, "window_ms": 60_000, "burst": 4},
            "cases": [
                {"cost": 1, "valid": True},
                {"cost": 2, "valid": True},
                {"cost": 4, "valid": True},
                {"cost": 5, "valid": False},
                {"cost": 10, "valid": False},
                {"cost": 0, "valid": False},
                {"cost": -1, "valid": False},
                {"cost": 1.5, "valid": False},
                {"cost": "2", "valid": False},
            ],
        },
        "contract/scope-validation": {
            "spec": SPEC,
            "id": "contract/scope-validation",
            "kind": "contract",
            "subject": "scope",
            "cases": [
                {"scope": "login", "valid": True},
                {"scope": "api.v1_x-y", "valid": True},
                {"scope": "", "valid": False},
                {"scope": "a:b", "valid": False},
                {"scope": 'a"b', "valid": False},
                {"scope": "a b", "valid": False},
                {"scope": "x" * 65, "valid": False},
            ],
        },
        "contract/approximate-decision": {
            "spec": SPEC,
            "id": "contract/approximate-decision",
            "kind": "contract",
            "subject": "approximate",
            "policy": {"limit": 5, "window_ms": 60_000, "burst": 5},
            "period_sec": 60,
            "cases": [
                {"store_allows": True, "expect": {"allowed": True, "reason": "ok", "status": None, "remaining": None, "reset_ms": None, "retry_after_ms": None, "headers": {}, "tier": "approximate"}},
                {"store_allows": False, "expect": {"allowed": False, "reason": "limited", "status": 429, "remaining": None, "reset_ms": None, "retry_after_ms": 60000, "headers": {"retry-after": "60"}, "tier": "approximate"}},
            ],
        },
    }


SHORTHAND_UNITS = {"second": 1000, "minute": 60_000, "hour": 3_600_000, "day": 86_400_000}


def shorthand(text: object) -> dict | None:
    """The independent model of the policy shorthand: characters walked by hand, no regex."""
    if not isinstance(text, str) or "/" not in text:
        return None
    head, _, tail = text.partition("/")
    unit = next((u for u in SHORTHAND_UNITS if tail.endswith(u)), None)
    if unit is None:
        return None
    count = tail[: len(tail) - len(unit)] or "1"

    def digits(value: str) -> bool:
        return 1 <= len(value) <= 15 and all(c in "0123456789" for c in value) and value[0] != "0"

    if not digits(head) or not digits(count):
        return None
    limit, window_ms = int(head), int(count) * SHORTHAND_UNITS[unit]
    if 2 * limit * window_ms + limit > MAX_SAFE:
        return None
    return {"limit": limit, "window_ms": window_ms}


def shorthands() -> dict[str, dict]:
    texts = [
        "1/second", "10/minute", "5/15minute", "100/hour", "1000/day", "5/2second", "3/90second",
        "1/1second", "20/10minute", "7/7day", "999999999999999/second",
        "", "/", "10", "10/", "/minute", "10/min", "10/minutes", "10 / minute", "10/ minute",
        " 10/minute", "10/minute ", "10/Minute", "10 per minute", "0/minute", "010/minute",
        "10/0minute", "10/01minute", "-1/minute", "1.5/minute", "10/1.5minute", "1e3/minute",
        "+5/minute", "10/minute/hour", "10/week", "10/ms", "٣/minute", "1000000000000000/second",
        "999999999999999/999999999999999day", "9007199254740991/day",
    ]
    cases = [{"text": t, "policy": shorthand(t)} for t in texts]
    assert sum(c["policy"] is not None for c in cases) == 10, "shorthand model drifted"
    return {
        "contract/policy-shorthand": {
            "spec": SPEC,
            "id": "contract/policy-shorthand",
            "kind": "contract",
            "subject": "shorthand",
            "cases": cases,
        }
    }


OPEN_FAILURE = {"allowed": True, "reason": "store_error", "status": None, "remaining": None, "reset_ms": None, "retry_after_ms": None}
CLOSED_FAILURE = {"allowed": False, "reason": "store_error", "status": 503, "remaining": None, "reset_ms": None, "retry_after_ms": 1000}


def short(r: dict) -> dict:
    return {
        "allowed": r["allowed"],
        "reason": "ok" if r["allowed"] else "limited",
        "status": None if r["allowed"] else 429,
        "remaining": r["remaining"],
        "reset_ms": r["reset_ms"],
        "retry_after_ms": r["retry_after_ms"],
    }


def governing(decisions: list[dict]) -> int:
    """Rule 3, written as a ranking rather than a running minimum like the ports."""
    assert decisions, "a combined result always has a decision"
    ranked = list(enumerate(decisions))
    denied = [(i, d) for i, d in ranked if not d["allowed"]]
    if denied:
        # Largest wait first, then lowest index: sort on (-wait, index) and take the head.
        return sorted(denied, key=lambda p: (-(p[1]["retry_after_ms"] or 0), p[0]))[0][0]
    counted = sorted((d["remaining"], i) for i, d in ranked if d["remaining"] is not None)
    return counted[0][1] if counted else 0


class RecipeError(Exception):
    def __init__(self, kind: str) -> None:
        self.kind = kind


class RecipeModel:
    """The recipes over Meters, one per limiter and id, with a log of every store call."""

    OPS = ("check_all", "peek_all", "reserve_all", "commit", "cancel", "attempt", "check", "peek", "reset", "inspect", "fault", "heal")

    def __init__(self, limiters: dict, lockouts: dict | None = None) -> None:
        scopes = [cfg["scope"] for cfg in limiters.values()]
        assert len(set(scopes)) == len(scopes), "limiters with one scope would share buckets"
        self.limiters, self.lockouts = limiters, lockouts or {}
        self.meters: dict[tuple[str, str], Meter] = {}
        self.faults: dict[str, tuple[str, set[str]]] = {}
        self.now = BASE
        self.calls: list[dict] = []
        # Pending checks by handle name: what each holds and how it settled.
        self.handles: dict[str, dict] = {}

    def meter(self, name: str, who: str) -> Meter:
        p = self.limiters[name]["policy"]
        return self.meters.setdefault((name, who), Meter(p["limit"], p["window_ms"], p["burst"]))

    def store(self, op: str, name: str, who: str, cost: int = 1, since: int | None = None) -> dict | None:
        """One store call: its result, or None when the armed fault answers it."""
        fault = self.faults.get(name)
        failed = fault is not None and op in fault[1]
        self.calls.append({"op": op, "limiter": name, "id": who, **({"failed": True} if failed else {})})
        if failed:
            return None
        m = self.meter(name, who)
        if op == "reset":
            m.reset()
            return {}
        if op == "refund":
            assert since is not None
            return m.refund(self.now, since, cost)
        return m.hit(self.now, cost) if op == "hit" else m.peek(self.now, cost)

    def decide(self, op: str, name: str, who: str, cost: int = 1) -> dict:
        r = self.store(op, name, who, cost)
        if r is None:
            return dict(OPEN_FAILURE if self.limiters[name]["on_store_error"] == "open" else CLOSED_FAILURE)
        return short(r)

    def validate(self, entries: list[dict], peeks: bool, reserves: bool = False) -> None:
        if not entries:
            raise RecipeError("value")
        approximate = [e for e in entries if self.limiters[e["limiter"]].get("tier") == "approximate"]
        burst = lambda e: self.limiters[e["limiter"]]["policy"]["burst"]  # noqa: E731
        bad_cost = [e for e in entries if not (isinstance(e.get("cost", 1), int) and 1 <= e.get("cost", 1) <= burst(e))]
        assert not (approximate and bad_cost), "which error wins is unspecified: keep vectors to one"
        # An approximate store can neither peek nor refund: the only stores of the vectors that cannot.
        if (peeks or reserves) and approximate:
            raise RecipeError("type")
        if bad_cost:
            raise RecipeError("value")

    def check_all(self, entries: list[dict]) -> dict:
        # Self-check: the same step as a pending opened in peek mode and committed at once.
        twin = copy.deepcopy(self)
        try:
            twin.open_pending("peek", "\0check_all", entries)
            twin_result: dict | str = twin.settle("commit", "\0check_all")
        except RecipeError as error:
            twin_result = error.kind
        try:
            result = self.checked_all(entries)
        except RecipeError as error:
            assert twin_result == error.kind and twin.calls == self.calls == []
            raise
        assert twin_result == result and twin.calls == self.calls
        assert {k: (m.level, m.ts) for k, m in twin.meters.items()} == {k: (m.level, m.ts) for k, m in self.meters.items()}
        return result

    def checked_all(self, entries: list[dict]) -> dict:
        self.validate(entries, peeks=True)
        cost = lambda e: e.get("cost", 1)  # noqa: E731
        peeks = [self.decide("peek", e["limiter"], e["id"], cost(e)) for e in entries]
        if not all(d["allowed"] for d in peeks):
            result = {"allowed": False, "governing": governing(peeks), "decisions": peeks}
        else:
            counted: list[dict] = []
            for e in entries:
                counted.append(self.decide("hit", e["limiter"], e["id"], cost(e)))
                if not counted[-1]["allowed"]:
                    break
            last = counted[-1]
            index = len(counted) - 1 if not last["allowed"] else governing(counted)
            result = {"allowed": last["allowed"], "governing": index, "decisions": counted}
            # A denial while counting is the only one in the list, so rule 3 picks it too.
            assert index == governing(counted)
        # Self-checks: the rules the ports are tested against, restated.
        g = result["decisions"][result["governing"]]
        assert result["allowed"] == g["allowed"] == all(d["allowed"] for d in result["decisions"])
        assert len(result["decisions"]) <= len(entries)
        if not result["allowed"]:
            assert (g["retry_after_ms"] or 0) == max(d["retry_after_ms"] or 0 for d in result["decisions"] if not d["allowed"])
            assert result["decisions"] is peeks or sum(not d["allowed"] for d in result["decisions"]) == 1
        elif any(d["remaining"] is not None for d in result["decisions"]):
            assert g["remaining"] == min(d["remaining"] for d in result["decisions"] if d["remaining"] is not None)
        return result

    def give_back(self, held: dict | None) -> bool | None:
        """Refund one reservation: None when there is none, else whether the store applied it."""
        if held is None:
            return None
        return self.store("refund", held["limiter"], held["id"], held["cost"], held["since"]) is not None

    def open_pending(self, mode: str, handle: str, entries: list[dict]) -> dict:
        """`peek_all` or `reserve_all`: the first phase of a combined check."""
        assert handle not in self.handles, "a handle names one pending"
        self.validate(entries, peeks=mode == "peek", reserves=mode == "reserve")
        cost = lambda e: e.get("cost", 1)  # noqa: E731
        held: list[dict | None] = []
        if mode == "peek":
            decisions = [self.decide("peek", e["limiter"], e["id"], cost(e)) for e in entries]
            held = [None] * len(decisions)
            refunded: list[bool | None] = [None] * len(decisions)
            index = governing(decisions)
        else:
            before = copy.deepcopy(self.meters)
            decisions = []
            for e in entries:
                r = self.store("hit", e["limiter"], e["id"], cost(e))
                if r is None:
                    on = self.limiters[e["limiter"]]["on_store_error"]
                    decisions.append(dict(OPEN_FAILURE if on == "open" else CLOSED_FAILURE))
                    held.append(None)
                else:
                    decisions.append(short(r))
                    held.append({"limiter": e["limiter"], "id": e["id"], "cost": cost(e), "since": r["state"]["ts"]} if r["allowed"] else None)
                if not decisions[-1]["allowed"]:
                    break
            if decisions[-1]["allowed"]:
                refunded = [None] * len(decisions)
                index = governing(decisions)
            else:
                # Compensate in entry order; the denial itself held nothing.
                refunded = [self.give_back(h) for h in held]
                index = len(decisions) - 1
                assert index == governing(decisions)
                # Self-check: compensation leaves every bucket where the request had never come,
                # or above it where a refund failed. Never below: that would over-admit.
                for k, m in self.meters.items():
                    was = before.get(k)
                    level, then = m.level_at(self.now), (was.level_at(self.now) if was else 0)
                    failed = any(done is False for h, done in zip(held, refunded) if h and (h["limiter"], h["id"]) == k)
                    assert level >= then and (failed or level == then)
        allowed = decisions[index]["allowed"]
        assert allowed == all(d["allowed"] for d in decisions)
        self.handles[handle] = {
            "mode": mode, "entries": entries, "allowed": allowed, "governing": index,
            "decisions": decisions, "held": held, "refunded": refunded, "settled": None, "result": None,
        }
        return {"allowed": allowed, "governing": index, "decisions": decisions, "refunded": refunded}

    def settle(self, op: str, handle: str) -> dict:
        """`commit` or `cancel`, under rule 8: once, repeatable, and the other one refused."""
        h = self.handles[handle]
        if h["settled"] is not None:
            assert self.calls == []
            if h["settled"] != op:
                raise RecipeError("state")
            return h["result"]
        h["settled"] = op
        own = {"allowed": h["allowed"], "governing": h["governing"], "decisions": h["decisions"]}
        if op == "commit":
            if h["mode"] == "peek" and h["allowed"]:
                counted: list[dict] = []
                for e in h["entries"]:
                    counted.append(self.decide("hit", e["limiter"], e["id"], e.get("cost", 1)))
                    if not counted[-1]["allowed"]:
                        break
                denied = not counted[-1]["allowed"]
                result = {"allowed": not denied, "governing": len(counted) - 1 if denied else governing(counted), "decisions": counted}
            else:
                result = own
        elif h["mode"] == "reserve" and h["allowed"]:
            result = {"refunded": [self.give_back(held) for held in h["held"]]}
        else:
            result = {"refunded": h["refunded"]}
        h["result"] = result
        return result

    def attempt(self, lockout: str, ids: list[str], verify: object) -> dict:
        limits = self.lockouts[lockout]
        assert len(limits) == len(ids) == 1, "lockouts take one limit in this spec version"
        (limit,), (who,) = limits, ids
        self.validate([{"limiter": limit["limiter"], "id": who}], peeks=True)
        gate = self.decide("peek", limit["limiter"], who)
        if not gate["allowed"]:
            return {"allowed": False, "verified": None, "verify_ran": False, "governing": 0, "decisions": [gate]}
        if verify == "raise":
            raise RecipeError("verify")
        if verify:
            if limit.get("reset_on_success", True):
                self.store("reset", limit["limiter"], who)
            return {"allowed": True, "verified": True, "verify_ran": True, "governing": 0, "decisions": [gate]}
        counted = self.decide("hit", limit["limiter"], who)
        return {"allowed": True, "verified": False, "verify_ran": True, "governing": 0, "decisions": [counted]}

    def step(self, step: dict) -> dict:
        op = step["op"]
        assert op in self.OPS
        if "at_ms" in step:
            self.now = step["at_ms"]
        if op == "fault":
            self.faults[step["limiter"]] = (step["fault"], set(step["on"]))
            return step
        if op == "heal":
            self.faults.pop(step["limiter"], None)
            return step
        if op == "inspect":
            r = self.meter(step["limiter"], step["id"]).peek(self.now)
            return {**step, "expect": {"state": r["state"]}}
        self.calls = []
        expect: dict = {}
        try:
            if op == "check_all":
                expect["result"] = self.check_all(step["entries"])
            elif op in ("peek_all", "reserve_all"):
                expect["result"] = self.open_pending(op[: -len("_all")], step["handle"], step["entries"])
            elif op in ("commit", "cancel"):
                expect["result"] = self.settle(op, step["handle"])
            elif op == "attempt":
                expect["result"] = self.attempt(step["lockout"], step["ids"], step["verify"])
            elif op == "reset":
                expect["result"] = self.store("reset", step["limiter"], step["id"]) is not None
            else:
                entry = {"limiter": step["limiter"], "id": step["id"], **({"cost": step["cost"]} if "cost" in step else {})}
                if op == "peek":
                    self.validate([entry], peeks=True)
                expect["result"] = self.decide("hit" if op == "check" else "peek", step["limiter"], step["id"], step.get("cost", 1))
        except RecipeError as error:
            expect["error"] = error.kind
        return {**step, "expect": {"calls": self.calls, **expect}}


def recipe(vector_id: str, limiters: dict, timeline: list[dict], lockouts: dict | None = None) -> dict:
    model = RecipeModel(limiters, lockouts)
    steps = [model.step(step) for step in timeline]
    vector = {"spec": SPEC, "id": vector_id, "kind": "recipe", "tiers": SEQ_TIERS, "salt": "s", "limiters": limiters}
    if lockouts:
        vector["lockouts"] = lockouts
    vector["timeline"] = steps
    return vector


def lim(scope: str, limit: int, window_ms: int, burst: int | None = None, on: str = "closed", tier: str | None = None) -> dict:
    out = {"scope": scope, "policy": {"limit": limit, "window_ms": window_ms, "burst": burst or limit}, "on_store_error": on}
    if tier:
        out["tier"] = tier
    return out


def recipes() -> dict[str, dict]:
    b = BASE

    def ca(*entries: tuple, at: int | None = None) -> dict:
        step: dict = {"op": "check_all"}
        if at is not None:
            step["at_ms"] = at
        step["entries"] = [{"limiter": e[0], "id": e[1], **({"cost": e[2]} if len(e) > 2 else {})} for e in entries]
        return step

    def one(op: str, name: str, who: str, cost: int | None = None, at: int | None = None) -> dict:
        step: dict = {"op": op}
        if at is not None:
            step["at_ms"] = at
        step.update({"limiter": name, "id": who})
        if cost is not None:
            step["cost"] = cost
        return step

    def att(who: str, verify: object, lockout: str = "login", at: int | None = None) -> dict:
        step: dict = {"op": "attempt"}
        if at is not None:
            step["at_ms"] = at
        step.update({"lockout": lockout, "ids": [who], "verify": verify})
        return step

    def fault(name: str, kind: str, on: list[str]) -> dict:
        return {"op": "fault", "limiter": name, "fault": kind, "on": on}

    def pending(op: str, handle: str, *entries: tuple, at: int | None = None) -> dict:
        step = ca(*entries, at=at)
        return {"op": op, **({"at_ms": at} if at is not None else {}), "handle": handle, "entries": step["entries"]}

    def pa(handle: str, *entries: tuple, at: int | None = None) -> dict:
        return pending("peek_all", handle, *entries, at=at)

    def ra(handle: str, *entries: tuple, at: int | None = None) -> dict:
        return pending("reserve_all", handle, *entries, at=at)

    def settle(op: str, handle: str, at: int | None = None) -> dict:
        return {"op": op, **({"at_ms": at} if at is not None else {}), "handle": handle}

    def heal(name: str) -> dict:
        return {"op": "heal", "limiter": name}

    two = {"ip": lim("ip", 2, 60_000), "user": lim("user", 5, 60_000)}
    named: dict[str, tuple] = {
        "all-allowed-counts-every-limit": (
            {"ip": lim("ip", 10, 60_000), "user": lim("user", 3, 60_000)},
            [ca(("ip", "a"), ("user", "u"), at=b), one("inspect", "ip", "a"), one("inspect", "user", "u")],
        ),
        "peek-reports-every-denial": (
            {"a": lim("a", 1, 60_000), "b": lim("b", 1, 10_000), "c": lim("c", 5, 60_000)},
            [one("check", "a", "x", at=b), one("check", "b", "x"), ca(("a", "x"), ("c", "x"), ("b", "x"), at=b + 1000), one("inspect", "c", "x")],
        ),
        "governing-tie-picks-the-first": (
            {"a": lim("a", 1, 60_000), "b": lim("b", 1, 60_000), "c": lim("c", 4, 60_000), "d": lim("d", 4, 60_000)},
            [
                one("check", "a", "x", at=b), one("check", "b", "x"),
                ca(("c", "x"), ("a", "x"), ("b", "x"), at=b + 5),
                ca(("c", "y"), ("d", "y")),
                ca(("d", "y"), ("c", "y")),
            ],
        ),
        "null-remaining-never-governs": (
            {"down": lim("down", 3, 60_000, on="open"), "ip": lim("ip", 2, 60_000)},
            [
                fault("down", "error", ["peek", "hit"]),
                ca(("down", "x"), ("ip", "a"), at=b),
                ca(("down", "x"), ("down", "y")),
                ca(("ip", "a"), ("down", "x")),
                ca(("ip", "a"), ("down", "x")),
            ],
        ),
        "same-entry-twice": (
            two,
            [ca(("ip", "a"), ("ip", "a"), at=b), one("check", "user", "u"), ca(("user", "u"), ("ip", "a"), ("ip", "a"), at=b + 30_000), one("inspect", "user", "u"), one("inspect", "ip", "a")],
        ),
        "check-all-partial-count-when-a-later-entry-denies": (
            {"ip": lim("ip", 3, 60_000), "user": lim("user", 3, 60_000), "last": lim("last", 3, 60_000)},
            [
                ca(("ip", "a"), ("user", "u"), ("ip", "a", 3), ("last", "z"), at=b),
                one("inspect", "ip", "a"), one("inspect", "user", "u"), one("inspect", "last", "z"),
            ],
        ),
        "check-all-partial-count-after-closed-failure": (
            {"ip": lim("ip", 3, 60_000), "user": lim("user", 3, 60_000), "last": lim("last", 3, 60_000)},
            [
                fault("user", "error", ["hit"]),
                ca(("ip", "a"), ("user", "u"), ("last", "z"), at=b),
                one("inspect", "ip", "a"), one("inspect", "user", "u"), one("inspect", "last", "z"),
                heal("user"),
                ca(("ip", "a"), ("user", "u"), ("last", "z")),
            ],
        ),
        "open-and-closed-failures": (
            {"open": lim("open", 2, 60_000, on="open"), "closed": lim("closed", 2, 60_000), "ip": lim("ip", 1, 1000)},
            [
                one("check", "ip", "a", at=b),
                fault("open", "error", ["peek", "hit", "reset"]),
                fault("closed", "error", ["peek"]),
                ca(("open", "x"), ("ip", "a"), at=b + 1),
                ca(("closed", "x"), ("open", "x"), ("ip", "a")),
                ca(("ip", "a"), ("closed", "x")),
                ca(("open", "x"), ("ip", "a"), at=b + 1000),
                heal("closed"),
                ca(("closed", "x"), ("open", "x")),
                one("reset", "open", "x"),
                one("reset", "closed", "x"),
            ],
        ),
        "store-timeout-follows-each-mode": (
            {"open": lim("open", 2, 60_000, on="open"), "closed": lim("closed", 2, 60_000)},
            [
                fault("open", "timeout", ["peek"]),
                ca(("open", "x"), ("closed", "x"), at=b),
                fault("closed", "timeout", ["hit"]),
                ca(("closed", "x"), ("open", "x")),
                heal("open"), heal("closed"),
                ca(("open", "x"), ("closed", "x")),
            ],
        ),
        "cost-validated-before-any-store-call": (
            {"ip": lim("ip", 10, 60_000, 4), "user": lim("user", 5, 60_000)},
            [
                ca(("ip", "a"), ("user", "u", 6), at=b),
                ca(("ip", "a"), ("user", "u", 0)),
                ca(("ip", "a", 5), ("user", "u")),
                ca(("ip", "a", 4), ("user", "u", 5)),
                one("inspect", "ip", "a"),
            ],
        ),
        "empty-list-is-a-value-error": (
            {"ip": lim("ip", 1, 1000)},
            [ca(at=b), ca(("ip", "a"))],
        ),
        "approximate-rejected-before-any-store-call": (
            {"ip": lim("ip", 5, 60_000), "edge": lim("edge", 5, 60_000, on="open", tier="approximate")},
            [ca(("ip", "a"), ("edge", "a"), at=b), ca(("edge", "a")), one("inspect", "ip", "a")],
        ),
        "lockout-counts-failures-and-resets-on-success": (
            {"fail": lim("fail", 2, 60_000)},
            [
                att("u", False, at=b), att("u", True), att("u", False), att("u", False),
                att("u", True, at=b + 1), one("inspect", "fail", "u"),
                att("u", True, at=b + 30_000), one("inspect", "fail", "u"),
                att("v", False), att("u", False),
            ],
            {"login": [{"limiter": "fail"}]},
        ),
        "lockout-without-reset-keeps-the-failures": (
            {"fail": lim("fail", 2, 60_000)},
            [att("u", False, at=b), att("u", True), one("inspect", "fail", "u"), att("u", False), att("u", True), att("u", True, at=b + 30_000)],
            {"login": [{"limiter": "fail", "reset_on_success": False}]},
        ),
        "lockout-verify-raises-counts-nothing": (
            {"fail": lim("fail", 1, 60_000)},
            [att("u", "raise", at=b), one("inspect", "fail", "u"), att("u", False), att("u", "raise"), one("inspect", "fail", "u")],
            {"login": [{"limiter": "fail"}]},
        ),
        "lockout-store-failures": (
            {"closed": lim("closed", 2, 60_000), "open": lim("open", 2, 60_000, on="open")},
            [
                fault("closed", "error", ["peek"]), att("u", True, "closed-login", at=b), heal("closed"),
                fault("closed", "error", ["hit"]), att("u", False, "closed-login"), heal("closed"),
                fault("open", "error", ["peek", "hit"]), att("u", False, "open-login"), att("u", True, "open-login"),
                fault("open", "error", ["reset"]), att("u", True, "open-login"),
                fault("closed", "error", ["reset"]), att("u", True, "closed-login"),
            ],
            {"closed-login": [{"limiter": "closed"}], "open-login": [{"limiter": "open"}]},
        ),
        "lockout-on-approximate-is-a-type-error": (
            {"edge": lim("edge", 5, 60_000, on="open", tier="approximate")},
            [att("u", False, "edge-login", at=b)],
            {"edge-login": [{"limiter": "edge"}]},
        ),
    }
    named.update({
        "pending-peek-commit-counts-every-limit": (
            {"ip": lim("ip", 10, 60_000), "user": lim("user", 3, 60_000)},
            [pa("p", ("ip", "a"), ("user", "u"), at=b), one("inspect", "user", "u"), settle("commit", "p"), one("inspect", "ip", "a"), one("inspect", "user", "u")],
        ),
        # Another request takes the last unit between the peek and the commit.
        "pending-commit-denied-after-a-racing-check": (
            {"ip": lim("ip", 1, 60_000), "user": lim("user", 5, 60_000)},
            [
                pa("p", ("user", "u"), ("ip", "a"), at=b),
                one("check", "ip", "a"),
                settle("commit", "p"),
                one("inspect", "user", "u"), one("inspect", "ip", "a"),
            ],
        ),
        "pending-commit-twice-returns-the-first-result": (
            {"ip": lim("ip", 2, 60_000)},
            [pa("p", ("ip", "a"), at=b), settle("commit", "p"), settle("commit", "p", at=b + 30_000), one("inspect", "ip", "a")],
        ),
        "pending-other-settlement-is-a-state-error": (
            {"ip": lim("ip", 2, 60_000), "user": lim("user", 2, 60_000)},
            [
                pa("p", ("ip", "a"), at=b), settle("cancel", "p"), settle("commit", "p"), settle("cancel", "p"), one("inspect", "ip", "a"),
                ra("r", ("ip", "a"), ("user", "u")), settle("commit", "r"), settle("cancel", "r"), settle("commit", "r"),
                one("inspect", "ip", "a"), one("inspect", "user", "u"),
            ],
        ),
        "pending-peek-denied-settles-without-calls": (
            {"ip": lim("ip", 1, 60_000), "user": lim("user", 5, 60_000)},
            [
                one("check", "ip", "a", at=b),
                pa("p", ("user", "u"), ("ip", "a")), settle("commit", "p"), settle("cancel", "p"),
                pa("q", ("ip", "a"), ("user", "u")), settle("cancel", "q"), settle("cancel", "q"),
                one("inspect", "user", "u"),
            ],
        ),
        "reserve-all-allowed-commit-makes-no-call": (
            {"ip": lim("ip", 10, 60_000), "user": lim("user", 3, 60_000)},
            [ra("r", ("ip", "a"), ("user", "u", 2), at=b), settle("commit", "r"), one("inspect", "ip", "a"), one("inspect", "user", "u")],
        ),
        "reserve-all-denied-on-the-second-entry-refunds-the-first": (
            {"ip": lim("ip", 5, 60_000), "user": lim("user", 1, 60_000)},
            [
                one("check", "user", "u", at=b),
                ra("r", ("ip", "a"), ("user", "u"), ("ip", "c"), at=b + 1000),
                one("inspect", "ip", "a"), one("inspect", "ip", "c"),
                settle("commit", "r"), settle("commit", "r"),
                ra("s", ("ip", "a"), ("user", "u")), settle("cancel", "s"),
                one("inspect", "ip", "a"),
            ],
        ),
        "reserve-all-cancel-refunds-every-entry": (
            {"ip": lim("ip", 2, 1000), "user": lim("user", 5, 60_000)},
            [
                one("check", "ip", "a", at=b),
                ra("r", ("ip", "a"), ("user", "u", 2), at=b + 100),
                one("inspect", "ip", "a"), one("inspect", "user", "u"),
                settle("cancel", "r", at=b + 300),
                one("inspect", "ip", "a"), one("inspect", "user", "u"),
                settle("cancel", "r"),
                ra("s", ("ip", "a"), ("user", "u")), settle("cancel", "s", at=b + 5000),
                one("inspect", "ip", "a"), one("inspect", "user", "u"),
            ],
        ),
        "reserve-all-needs-limiters-that-refund": (
            {"ip": lim("ip", 5, 60_000), "edge": lim("edge", 5, 60_000, on="open", tier="approximate")},
            [ra("r", ("ip", "a"), ("edge", "a"), at=b), ra("s", ("edge", "a")), one("inspect", "ip", "a")],
        ),
        "reserve-all-validates-costs-before-any-store-call": (
            {"ip": lim("ip", 10, 60_000, 4), "user": lim("user", 5, 60_000)},
            [ra("r", ("ip", "a"), ("user", "u", 6), at=b), ra("s", ("ip", "a", 5)), ra("t", at=b), one("inspect", "ip", "a")],
        ),
        "reserve-all-failed-compensation-stays-counted": (
            {"ip": lim("ip", 5, 60_000), "acct": lim("acct", 5, 60_000), "user": lim("user", 1, 60_000)},
            [
                one("check", "user", "u", at=b),
                fault("ip", "error", ["refund"]),
                ra("r", ("ip", "a"), ("acct", "x"), ("user", "u")),
                one("inspect", "ip", "a"), one("inspect", "acct", "x"),
                ra("s", ("acct", "x"), ("ip", "a")),
                settle("cancel", "s"),
                one("inspect", "ip", "a"), one("inspect", "acct", "x"),
                heal("ip"),
                settle("cancel", "s"),
            ],
        ),
        "reserve-all-refund-timeout-stays-counted": (
            {"ip": lim("ip", 5, 60_000), "user": lim("user", 1, 60_000)},
            [
                one("check", "user", "u", at=b),
                fault("ip", "timeout", ["refund"]),
                ra("r", ("ip", "a"), ("user", "u")),
                heal("ip"),
                one("inspect", "ip", "a"),
            ],
        ),
        "reserve-all-same-entry-twice": (
            {"ip": lim("ip", 3, 60_000)},
            [
                ra("r", ("ip", "a"), ("ip", "a"), at=b), one("inspect", "ip", "a"),
                ra("s", ("ip", "a"), ("ip", "a")), one("inspect", "ip", "a"),
                settle("cancel", "r"), one("inspect", "ip", "a"),
            ],
        ),
        "reserve-all-store-failures": (
            {"open": lim("open", 2, 60_000, on="open"), "closed": lim("closed", 2, 60_000), "ip": lim("ip", 1, 60_000)},
            [
                fault("open", "error", ["hit"]),
                ra("r", ("open", "x"), ("ip", "a"), at=b),
                settle("cancel", "r"),
                one("check", "ip", "a"),
                ra("s", ("open", "x"), ("ip", "a")),
                heal("open"),
                fault("closed", "error", ["hit"]),
                ra("t", ("open", "x"), ("closed", "x"), at=b + 60_000),
                one("inspect", "open", "x"),
            ],
        ),
        "check-all-is-peek-all-then-commit": (
            {"ip": lim("ip", 2, 60_000), "user": lim("user", 3, 60_000)},
            [
                ca(("ip", "a"), ("user", "u"), at=b), pa("p", ("ip", "a"), ("user", "u")), settle("commit", "p"),
                ca(("ip", "a"), ("user", "u")), pa("q", ("ip", "a"), ("user", "u")), settle("commit", "q"),
                one("inspect", "ip", "a"), one("inspect", "user", "u"),
            ],
        ),
    })
    out = {f"recipe/{k}": recipe(f"recipe/{k}", *v) for k, v in named.items()}

    rng = random.Random(20261120)
    for n in range(24):
        limiters = {}
        for name in ("a", "b", "c")[: rng.randrange(1, 4)]:
            limit = rng.choice([1, 2, 3, 5, 10])
            window = rng.choice([1000, 1009, 10_000, 60_000])
            burst = rng.choice([1, 2, 3, limit, limit + 1])
            limiters[name] = lim(name, limit, window, burst, on=rng.choice(["open", "closed"]))
        names = list(limiters)
        lockouts = {"login": [{"limiter": names[0], **({"reset_on_success": False} if rng.random() < 0.3 else {})}]}
        at = BASE + rng.randrange(1000)
        timeline: list[dict] = []
        faulted: set[str] = set()
        for i in range(rng.randrange(10, 25)):
            dt = rng.choice([0, 0, 0, 1, 17, 333, 1000, 5000])
            at += dt
            when = at if (i == 0 or dt) else None
            kind = rng.choice(["check_all"] * 6 + ["check", "peek", "attempt", "attempt", "reset", "inspect", "fault"])
            if kind == "fault":
                if faulted and rng.random() < 0.5:
                    name = rng.choice(sorted(faulted))
                    faulted.discard(name)
                    timeline.append(heal(name))
                else:
                    name = rng.choice(names)
                    faulted.add(name)
                    timeline.append(fault(name, "error", rng.sample(["peek", "hit", "reset"], rng.randrange(1, 4))))
                continue
            if kind == "check_all":
                entries = []
                for _ in range(rng.randrange(1, 4)):
                    name = rng.choice(names)
                    burst = limiters[name]["policy"]["burst"]
                    cost = rng.choice([1, 1, 1, 2, burst])
                    entries.append((name, rng.choice("xxy"), *([min(cost, burst)] if cost != 1 else [])))
                timeline.append(ca(*entries, at=when))
            elif kind == "attempt":
                timeline.append(att(rng.choice("uuv"), rng.choice([True, False, False, False, "raise"]), at=when))
            else:
                timeline.append(one(kind, rng.choice(names), rng.choice("xxy"), at=when))
        out[f"recipe/random-{n:03d}"] = recipe(f"recipe/random-{n:03d}", limiters, timeline, lockouts)

    # Pending checks in both modes, settled, repeated, crossed and abandoned, among plain calls.
    rng = random.Random(20261121)
    for n in range(24):
        limiters = {}
        for name in ("a", "b", "c")[: rng.randrange(1, 4)]:
            limit = rng.choice([1, 2, 3, 5, 10])
            window = rng.choice([1000, 1009, 10_000, 60_000])
            burst = rng.choice([1, 2, 3, limit, limit + 1])
            limiters[name] = lim(name, limit, window, burst, on=rng.choice(["open", "closed"]))
        names = list(limiters)
        at = BASE + rng.randrange(1000)
        timeline = []
        faulted = set()
        handles: list[str] = []
        for i in range(rng.randrange(12, 30)):
            dt = rng.choice([0, 0, 0, 1, 17, 333, 1000, 5000])
            at += dt
            when = at if (i == 0 or dt) else None
            kind = rng.choice(["peek_all", "reserve_all", "reserve_all", "settle", "settle", "settle", "check_all", "check", "reset", "inspect", "fault"])
            if kind == "settle" and not handles:
                kind = "reserve_all"
            if kind == "fault":
                if faulted and rng.random() < 0.5:
                    name = rng.choice(sorted(faulted))
                    faulted.discard(name)
                    timeline.append(heal(name))
                else:
                    name = rng.choice(names)
                    faulted.add(name)
                    timeline.append(fault(name, "error", rng.sample(["peek", "hit", "reset", "refund"], rng.randrange(1, 5))))
                continue
            if kind in ("peek_all", "reserve_all", "check_all"):
                entries = []
                for _ in range(rng.randrange(1, 4)):
                    name = rng.choice(names)
                    burst = limiters[name]["policy"]["burst"]
                    cost = min(burst, rng.choice([1, 1, 1, 2, burst]))
                    entries.append((name, rng.choice("xxy"), *([cost] if cost != 1 else [])))
                if kind == "check_all":
                    timeline.append(ca(*entries, at=when))
                else:
                    handle = f"h{len(handles)}"
                    handles.append(handle)
                    timeline.append(pending(kind, handle, *entries, at=when))
            elif kind == "settle":
                # Mostly the newest pending, sometimes an older one, settled or not.
                handle = handles[-1] if rng.random() < 0.6 else rng.choice(handles)
                timeline.append(settle(rng.choice(["commit", "cancel"]), handle, at=when))
            else:
                timeline.append(one(kind, rng.choice(names), rng.choice("xxy"), at=when))
        out[f"recipe/pending-random-{n:03d}"] = recipe(f"recipe/pending-random-{n:03d}", limiters, timeline)
    return out


def build() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for part in (sequences(), keys(), ips(), failures(), concurrency(), contract(), shorthands(), recipes()):
        out.update(part)
    return out


def write(target: Path) -> None:
    for vector_id, vector in build().items():
        path = target / f"{vector_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(vector, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def snapshot(base: Path) -> dict[str, bytes]:
    return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*.json"))}


def main() -> int:
    if "--check" in sys.argv[1:]:
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp))
            if snapshot(Path(tmp)) != snapshot(OUT):
                print("spec/vectors is stale: run tools/gen_vectors.py", file=sys.stderr)
                return 1
        print("vectors in sync")
        return 0
    shutil.rmtree(OUT, ignore_errors=True)
    write(OUT)
    print(f"wrote {len(build())} vectors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
