#!/usr/bin/env python3
"""Every interleaving of a recipe run by a few callers at once, in scopes small enough to list.

The concurrency vectors replay each recipe in 300 random interleavings of the model, and a port
runs them under real concurrency, where the order is whatever the runtime gives. Here the model
is run in every order there is, one store call at a time. Two things come out of it:

- The bounds of "Recipes under concurrency" in the spec are checked in all of them.
- Each order is written down with the result the model got, so that a port can be driven through
  the same order, call by call, and compared.

The files are large and are not committed: `fuzz_vectors.py` writes them next to its own.

Usage: explore_interleavings.py [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gen_vectors as gv  # noqa: E402


class Scripted:
    """Stands in for the random schedule: replays `prefix`, then always picks the first request
    still running, and records every pick with how many there were to choose from."""

    def __init__(self, prefix: list[int]) -> None:
        self.prefix = prefix
        self.taken: list[tuple[int, int]] = []

    def randrange(self, n: int) -> int:
        at = len(self.taken)
        pick = self.prefix[at] if at < len(self.prefix) else 0
        self.taken.append((pick, n))
        return pick


def every_schedule(limiters: dict, recipe: dict, requests: int):
    """Yield `(picks, summary)` for each order the store calls of the requests can take."""
    prefix: list[int] = []
    while True:
        schedule = Scripted(prefix)
        summary = gv.concurrent_requests(limiters, recipe, requests, schedule)  # type: ignore[arg-type]
        yield [pick for pick, _ in schedule.taken], summary
        # The next order: move the last pick that still has another choice, drop what follows.
        taken = schedule.taken
        while taken and taken[-1][0] == taken[-1][1] - 1:
            taken.pop()
        if not taken:
            return
        prefix = [pick for pick, _ in taken[:-1]] + [taken[-1][0] + 1]


def check_bounds(name: str, limiters: dict, recipe: dict, summary: dict) -> None:
    """What the spec promises of every interleaving, as `concurrency_recipe` asserts it."""
    window = {n: c["policy"]["window_ms"] for n, c in limiters.items()}
    capacity = {n: c["policy"]["burst"] * window[n] for n, c in limiters.items()}
    op = recipe["op"]
    advisory = op == "gate" and recipe["lockout"].get("mode", "advisory") == "advisory"
    weights = [(e["limiter"], e.get("cost", 1)) for e in recipe.get("entries", [])]
    weights += [(limit["limiter"], 1) for limit in recipe.get("lockout", {}).get("limits", [])]
    smallest = min(capacity[n] // (window[n] * cost) for n, cost in weights)
    assert advisory or op == "check_all" or summary["allowed"] <= smallest, f"{name}: more allowed than the smallest limit holds"
    assert summary["verify_ran"] <= smallest, f"{name}: more verifications than the smallest limit holds"
    assert all(level <= capacity[n] for n, level in summary["levels"].items()), f"{name}: a level above its capacity"
    if op == "check_all" or advisory:
        assert all(summary["levels"][n] == summary["counted"][n] * window[n] for n in summary["levels"]), f"{name}: levels differ from what was counted"


def lockout(mode: str | None, *names: str) -> dict:
    return {"limits": [{"limiter": n} for n in names], **({"mode": mode} if mode else {})}


def scopes() -> list[tuple[str, dict, dict, int]]:
    """Small on purpose: the number of orders grows with the factorial of the store calls."""
    lim = gv.lim
    two = lambda a, b: {"a": lim("a", 1, 60_000, a), "b": lim("b", 1, 60_000, b)}  # noqa: E731
    entries = [{"limiter": "a", "id": "x"}, {"limiter": "b", "id": "y"}]
    return [
        # Peek mode: the advisory window between the peeks and the hits, in every order.
        ("check-all-two-limits-two-callers", two(1, 2), {"op": "check_all", "entries": entries}, 2),
        # The race the spec describes: both callers pass their peeks, and the one the second
        # limit denies has already spent the first.
        ("check-all-spends-the-first-limit-when-the-second-denies", two(2, 1), {"op": "check_all", "entries": entries}, 2),
        ("check-all-three-limits", {**two(2, 2), "c": lim("c", 1, 60_000, 1)}, {"op": "check_all", "entries": [*entries, {"limiter": "c", "id": "z"}]}, 2),
        ("check-all-one-limit-three-callers", {"a": lim("a", 1, 60_000, 2)}, {"op": "check_all", "entries": entries[:1]}, 3),
        ("check-all-with-cost", {"a": lim("a", 1, 60_000, 3)}, {"op": "check_all", "entries": [{"limiter": "a", "id": "x", "cost": 2}]}, 3),
        # Reserve mode: a later limit denies, and the earlier one is refunded while others run.
        ("reserve-all-later-limit-denies", two(2, 1), {"op": "reserve_all", "entries": entries, "commit": 1}, 2),
        ("reserve-all-earlier-limit-denies", two(1, 2), {"op": "reserve_all", "entries": entries, "commit": 1}, 2),
        ("reserve-all-every-caller-cancels", two(2, 2), {"op": "reserve_all", "entries": entries, "commit": 0}, 2),
        ("reserve-all-three-callers", {"a": lim("a", 1, 60_000, 2)}, {"op": "reserve_all", "entries": entries[:1], "commit": 1}, 3),
        ("reserve-all-three-callers-two-limits", two(2, 1), {"op": "reserve_all", "entries": entries, "commit": 1}, 3),
        # Lockouts: gates opened by every caller, then failed.
        ("lockout-advisory-two-limits", two(2, 1), {"op": "gate", "lockout": lockout(None, "a", "b"), "ids": ["u", "v"], "settle": "fail"}, 2),
        ("lockout-advisory-three-callers", {"a": lim("a", 1, 60_000, 2)}, {"op": "gate", "lockout": lockout(None, "a"), "ids": ["u"], "settle": "fail"}, 3),
        ("lockout-reserve-two-limits", two(2, 1), {"op": "gate", "lockout": lockout("reserve", "a", "b"), "ids": ["u", "v"], "settle": "fail"}, 2),
        ("lockout-reserve-three-callers", two(2, 1), {"op": "gate", "lockout": lockout("reserve", "a", "b"), "ids": ["u", "v"], "settle": "fail"}, 3),
        ("lockout-reserve-attempts", two(1, 2), {"op": "attempt", "lockout": lockout("reserve", "a", "b"), "ids": ["u", "v"], "verify": False}, 3),
    ]


def build() -> dict[str, dict]:
    out = {}
    for name, limiters, recipe, requests in scopes():
        runs = []
        for picks, summary in every_schedule(limiters, recipe, requests):
            check_bounds(name, limiters, recipe, summary)
            runs.append([picks, summary["allowed"], summary["verify_ran"], [summary["levels"][n] for n in limiters]])
        vector_id = f"interleaving/{name}"
        out[vector_id] = {
            "spec": gv.SPEC,
            "id": vector_id,
            "kind": "interleaving",
            "salt": "s",
            "at_ms": gv.BASE,
            "limiters": limiters,
            "requests": requests,
            "recipe": recipe,
            # Each run: the picks, then `allowed`, `verify_ran` and the level of each limiter in
            # the order of `limiters`. A pick is an index into the requests still running.
            "runs": runs,
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=None, help="write the orders here; without it, only check the bounds")
    args = parser.parse_args()
    vectors = build()
    for vector_id, vector in vectors.items():
        outcomes = {json.dumps(run[1:]) for run in vector["runs"]}
        print(f"{vector_id}: {len(vector['runs'])} orders, {len(outcomes)} distinct outcomes")
        if args.out:
            path = args.out / f"{vector_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(vector, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{sum(len(v['runs']) for v in vectors.values())} orders, every bound holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
