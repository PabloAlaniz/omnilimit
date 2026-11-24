#!/usr/bin/env python3
"""Fresh random vectors from the independent model, for one run. Nothing here is committed.

The vectors in `spec/vectors` are fixed: they pin the behaviour, and include random ones from a
few seeds chosen once. This tool draws new ones from any seed, over wider ranges, so a port is
compared with the model on inputs nobody picked. Every port runs them through the runner it
already has, when `OMNILIMIT_FUZZ_VECTORS` names the directory.

A failure prints the seed. To keep it fixed, add the case to `gen_vectors.py` as a named vector.

Usage: fuzz_vectors.py [--seed N] [--count N] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import explore_interleavings  # noqa: E402
import gen_vectors as gv  # noqa: E402

BASE = gv.BASE
MAX_SAFE = gv.MAX_SAFE
LATEST = 2**52


def valid_policy(limit: object, window_ms: object, burst: object) -> bool:
    """The rule of the spec, written from its text."""
    for value in (limit, window_ms, burst):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return False
    return window_ms >= 1000 and 2 * burst * window_ms + limit <= MAX_SAFE


def log_uniform(rng: random.Random, low: int, high: int) -> int:
    """An integer from `low` to `high` where every order of magnitude is as likely."""
    bits = rng.uniform(low.bit_length() - 1, high.bit_length())
    return max(low, min(high, int(2**bits)))


def policy(rng: random.Random, limit_max: int = 10**6, window_max: int = 10**10, burst_max: int = 10**4) -> tuple[int, int, int]:
    while True:
        limit = rng.choice([1, 2, 3, 5, 7, 10, 60, 100, 997, log_uniform(rng, 1, limit_max)])
        window = rng.choice([1000, 1001, 1009, 1500, 10_000, 60_000, 900_000, 3_600_000, 86_400_000, log_uniform(rng, 1000, window_max)])
        burst = rng.choice([1, 2, 3, limit, limit + 1, limit * 2, log_uniform(rng, 1, burst_max)])
        if valid_policy(limit, window, burst):
            return limit, window, burst


def edge_policy(rng: random.Random) -> tuple[int, int, int]:
    """A policy close to the largest the spec allows, where intermediate values are near 2^53."""
    while True:
        window = log_uniform(rng, 1000, 10**13)
        burst = log_uniform(rng, 1, 10**6)
        limit = log_uniform(rng, 1, 10**9)
        room = (MAX_SAFE - limit) // (2 * window)
        if room < 1:
            continue
        burst = min(burst, room) if rng.random() < 0.5 else room
        if valid_policy(limit, window, burst):
            return limit, window, burst


def steps_in_time(rng: random.Random, limit: int, window: int, burst: int) -> list[int]:
    drain = gv.ceil_div(burst * window, limit)
    unit = max(1, window // limit)
    return [0, 0, 0, 1, 2, 17, 333, 1000, unit - 1, unit, unit + 1, window, drain - 1, drain, drain + 1, rng.randrange(1, drain + 2)]


def sequences(rng: random.Random, count: int) -> dict[str, dict]:
    out = {}
    refund_rng = random.Random(rng.random())

    def pick(fresh: list[int], spent: list[int]) -> int | None:
        if spent and refund_rng.random() < 0.15:
            return refund_rng.choice(spent)
        return refund_rng.choice(fresh) if fresh else None

    for n in range(count):
        limit, window, burst = edge_policy(rng) if n % 5 == 4 else policy(rng)
        deltas = steps_in_time(rng, limit, window, burst)
        regress = rng.random() < 0.25
        at = BASE + rng.randrange(1000)
        ids = rng.choice(["a", "aab", "abcdef"])
        moves = []
        for _ in range(rng.randrange(5, 60)):
            if regress and rng.random() < 0.15:
                at = max(1, at - rng.choice([1, 1000, window, 5 * window]))
            else:
                # Times stay where a double holds every millisecond, as real clocks do.
                at = min(at + rng.choice(deltas), LATEST)
            op = rng.choice(["hit"] * 6 + ["peek", "peek", "reset", "refund", "refund"])
            who = rng.choice(ids)
            if op == "refund":
                moves.append((at, who, "refund", pick))
            else:
                cost = min(burst, rng.choice([1, 1, 1, 2, 3, burst, max(1, burst // 2), rng.randrange(1, burst + 1)]))
                moves.append((at, who, op, cost))
        vector_id = f"sequence/fuzz-{n:04d}"
        out[vector_id] = gv.sequence(vector_id, limit, window, burst, moves, shadow=rng.random() < 0.15)
    return out


def ipv4(rng: random.Random) -> str:
    return ".".join(str(rng.choice([0, 1, 9, 10, 99, 100, 127, 192, 203, 255, rng.randrange(256)])) for _ in range(4))


def ipv6(rng: random.Random) -> str:
    """A valid IPv6 address in one of its many spellings."""
    groups = [rng.choice([0, 0, 0, 1, 0xFFFF, 0xDB8, 0x2001, rng.randrange(0x10000)]) for _ in range(8)]
    if rng.random() < 0.2:
        groups[:6] = [0, 0, 0, 0, 0, 0xFFFF]
    text = [format(g, rng.choice(["x", "X", "04x", "x", "x"])) for g in groups]
    tail = None
    if rng.random() < 0.25:
        tail = f"{groups[6] >> 8}.{groups[6] & 255}.{groups[7] >> 8}.{groups[7] & 255}"
        text = text[:6]
    if rng.random() < 0.6:
        # Compress one run of zero groups: any run, whether or not it is the canonical one.
        runs = []
        i = 0
        while i < len(text):
            if groups[i] == 0:
                j = i
                while j < len(text) and groups[j] == 0:
                    j += 1
                runs.append((i, rng.randrange(i + 1, j + 1)))
                i = j
            else:
                i += 1
        if runs:
            start, end = rng.choice(runs)
            left, right = ":".join(text[:start]), ":".join(text[end:])
            joined = f"{left}::{right}"
            return f"{joined}:{tail}" if tail and right else f"{joined}{tail}" if tail else joined
    joined = ":".join(text)
    return f"{joined}:{tail}" if tail else joined


SPACES = [" ", "  ", "\t", " \t ", " ", "\u0085", "\u000b", "\u000c", "\u001f", " ", "﻿", "​"]
JUNK = ["", " ", "unknown", "_hidden", "[::1]", "[2001:db8::1]:443", "1.2.3.4:80", "localhost", "0x7f.1", "1.2.3", "1.2.3.4.5", "::", ":", ":::", "1::2::3"]


def mutate(rng: random.Random, ip: str) -> str:
    kind = rng.randrange(14)
    at = rng.randrange(len(ip) + 1)
    if kind == 0:
        return ip[:at] + rng.choice("0123456789abcdefABCDEFg.:%/+- xX") + ip[at:]
    if kind == 1 and ip:
        return ip[: max(0, at - 1)] + ip[at:]
    if kind == 2:
        return ip + rng.choice(["%eth0", "%1", "/64", "/32", ":443", ".", ":", "::", " 1", "\n", "\x00"])
    if kind == 3:
        return rng.choice(["0", "00", "+", "-", "[", " ", "0x", "::", ":"]) + ip
    if kind == 4:
        return ip.replace("1", rng.choice(["01", "١", "１", "¹", "l"]), 1)
    if kind == 5:
        return ip.replace(".", rng.choice(["..", ",", " . ", "。"]), 1)
    if kind == 6:
        return ip.replace(":", rng.choice([":::", ": ", ";"]), 1)
    if kind == 7:
        return ip.upper() if rng.random() < 0.5 else ip.swapcase()
    if kind == 8:
        return ip.replace("::", "::0::" if rng.random() < 0.5 else ":0:0:0:0:0:0:0:0:", 1)
    if kind == 9:
        return rng.choice(JUNK)
    if kind == 10:
        return ip + ":" + ipv4(rng)
    if kind == 11:
        return ipv4(rng) + ":" + ip
    if kind == 12:
        return str(rng.choice([256, 300, 999, 1000, 65536])) + ip[ip.find(".") :] if "." in ip else "10000" + ip[ip.find(":") :]
    return "".join(rng.choice(ip + ".:0f ") for _ in range(rng.randrange(1, 20)))


def address(rng: random.Random) -> str:
    ip = ipv4(rng) if rng.random() < 0.5 else ipv6(rng)
    for _ in range(rng.choice([0, 0, 0, 1, 1, 2])):
        ip = mutate(rng, ip)
    return ip


def ips(rng: random.Random, count: int) -> dict[str, dict]:
    cases = []
    for n in range(count * 20):
        hops = rng.choice([0, 0, 0, 1, 1, 2, 3, 5])
        entries = [address(rng) for _ in range(rng.choice([1, 1, 2, 3, 4, 6]))]
        pad = lambda: rng.choice(["", "", "", " ", " "] + SPACES)  # noqa: E731
        value = ",".join(f"{pad()}{entry}{pad()}" for entry in entries)
        # A header value cannot carry a line break or a NUL: no server would deliver one.
        value = value.replace("\n", "").replace("\x00", "")
        cases.append(
            {
                "name": f"xff-{n:05d}",
                "headers": {"x-forwarded-for": value},
                "peer": "10.0.0.1",
                "strategy": {"xff": {"hops": hops}},
                "expect": gv.from_xff(value, hops),
            }
        )
    for n in range(count * 10):
        peer = address(rng)
        cases.append({"name": f"peer-{n:05d}", "headers": {}, "peer": peer, "strategy": {"preset": "direct"}, "expect": gv.canonical(peer)})
    masks = []
    for _ in range(count * 10):
        ip = gv.canonical(ipv4(rng) if rng.random() < 0.2 else ipv6(rng))
        prefix = rng.choice([0, 1, 15, 16, 17, 32, 48, 56, 63, 64, 65, 96, 127, 128, rng.randrange(129)])
        masks.append({"ip": ip, "prefix": prefix, "expect": gv.masked(ip, prefix)})
    return {"ip/fuzz": {"spec": gv.SPEC, "id": "ip/fuzz", "kind": "ip", "cases": cases, "masks": masks}}


def shorthands(rng: random.Random, count: int) -> dict[str, dict]:
    units = [*gv.SHORTHAND_UNITS, "seconds", "min", "sec", "m", "h", "Second", "MINUTE", "week", "", "ms"]
    numbers = ["1", "5", "10", "100", "1000", "0", "00", "01", "007", "-1", "+1", "1.0", "1e3", " 1", "1 ", "", "١", "１", "9007199254740991", "999999999999999", "1000000000000000"]
    texts = []
    for _ in range(count * 30):
        head = rng.choice(numbers) if rng.random() < 0.6 else str(log_uniform(rng, 1, 10**16))
        tail_count = rng.choice(["", "", "", *numbers]) if rng.random() < 0.7 else str(log_uniform(rng, 1, 10**12))
        text = f"{head}{rng.choice(['/', '/', '/', '/', ' / ', '//', '', ':', ' per '])}{tail_count}{rng.choice(units)}"
        if rng.random() < 0.1:
            text = rng.choice([" ", "\t", "\n"]) + text if rng.random() < 0.5 else text + rng.choice([" ", "\n", "s", "/"])
        texts.append(text)
    cases = []
    for text in dict.fromkeys(texts):
        parsed = gv.shorthand(text)
        cases.append({"text": text, "policy": parsed})
    return {"contract/fuzz-shorthand": {"spec": gv.SPEC, "id": "contract/fuzz-shorthand", "kind": "contract", "subject": "shorthand", "cases": cases}}


def policies(rng: random.Random, count: int) -> dict[str, dict]:
    cases = []
    for _ in range(count * 30):
        limit, window, burst = edge_policy(rng) if rng.random() < 0.6 else policy(rng)
        shift = rng.choice([0, 0, 1, 2, 10, 1000])
        which = rng.randrange(4)
        candidate = {
            "limit": limit + (shift if which == 0 else 0),
            "window_ms": window + (shift if which == 1 else 0),
            "burst": burst + (shift if which == 2 else 0),
        }
        if which == 3:
            field = rng.choice(list(candidate))
            candidate[field] = rng.choice([0, -1, 999, 1.5, candidate[field] + 0.5, MAX_SAFE, MAX_SAFE + 1, 2**60])
        cases.append({"policy": candidate, "valid": valid_policy(candidate["limit"], candidate["window_ms"], candidate["burst"])})
    return {"contract/fuzz-policy": {"spec": gv.SPEC, "id": "contract/fuzz-policy", "kind": "contract", "subject": "policy", "cases": cases}}


def keys(rng: random.Random, count: int) -> dict[str, dict]:
    alphabet = "abcXYZ019 :@._-/\\\"'\x00\t\néñü中文\U0001f600́‍"
    text = lambda: "".join(rng.choice(alphabet) for _ in range(rng.choice([0, 1, 2, 5, 20, 200])))  # noqa: E731
    scope_alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    cases = []
    for _ in range(count * 20):
        limit, window, burst = policy(rng)
        scope = "".join(rng.choice(scope_alphabet) for _ in range(rng.choice([1, 2, 8, 64])))
        salt, who = text(), text()
        cases.append(
            {
                "scope": scope,
                "policy": {"limit": limit, "window_ms": window, "burst": burst},
                "salt": salt,
                "id": who,
                "expect": gv.key_for(scope, limit, window, burst, salt, who),
            }
        )
    return {"key/fuzz": {"spec": gv.SPEC, "id": "key/fuzz", "kind": "key", "cases": cases}}


def recipes(rng: random.Random, count: int) -> dict[str, dict]:
    """Combined checks, pending checks and lockouts in one timeline, with faults."""

    def stamp(step: dict, at: int | None) -> dict:
        return {"op": step.pop("op"), **({"at_ms": at} if at is not None else {}), **step}

    out = {}
    for n in range(count):
        limiters = {}
        for name in ("a", "b", "c", "d")[: rng.randrange(1, 5)]:
            limit, window, burst = policy(rng, limit_max=50, window_max=10**6, burst_max=8)
            limiters[name] = gv.lim(name, limit, window, burst, on=rng.choice(["open", "closed"]), shadow=rng.random() < 0.1)
        names = list(limiters)
        limits = [{"limiter": name, **({"reset_on_success": False} if rng.random() < 0.4 else {})} for name in names[: rng.randrange(1, len(names) + 1)]]
        lockouts = {"login": {"limits": limits, "mode": rng.choice(["advisory", "reserve"])}}
        at = BASE + rng.randrange(1000)
        timeline: list[dict] = []
        faulted: set[str] = set()
        pendings: list[str] = []
        gates: list[str] = []
        stamped = False
        for _ in range(rng.randrange(8, 40)):
            dt = rng.choice([0, 0, 0, 1, 17, 333, 1000, 5000, 60_000])
            at += dt
            kind = rng.choice(
                ["check_all", "check_all", "peek_all", "reserve_all", "reserve_all", "settle", "settle", "attempt", "attempt", "gate", "settle_gate", "settle_gate", "check", "peek", "reset", "inspect", "fault"]
            )
            if kind == "settle" and not pendings:
                kind = "reserve_all"
            if kind == "settle_gate" and not gates:
                kind = "gate"
            if kind == "fault":
                if faulted and rng.random() < 0.5:
                    name = rng.choice(sorted(faulted))
                    faulted.discard(name)
                    timeline.append({"op": "heal", "limiter": name})
                else:
                    name = rng.choice(names)
                    faulted.add(name)
                    timeline.append({"op": "fault", "limiter": name, "fault": "error", "on": rng.sample(["peek", "hit", "reset", "refund"], rng.randrange(1, 5))})
                continue
            # The first step that reaches a store carries the time, and so does each one after
            # the clock moved.
            when = at if (dt or not stamped) else None
            stamped = True
            if kind in ("check_all", "peek_all", "reserve_all"):
                entries = []
                for _ in range(rng.randrange(1, 5)):
                    name = rng.choice(names)
                    burst = limiters[name]["policy"]["burst"]
                    cost = min(burst, rng.choice([1, 1, 1, 2, burst]))
                    entries.append({"limiter": name, "id": rng.choice("xxy"), **({"cost": cost} if cost != 1 else {})})
                step: dict = {"op": kind}
                if kind != "check_all":
                    step["handle"] = f"h{len(pendings)}"
                    pendings.append(step["handle"])
                step["entries"] = entries
                timeline.append(stamp(step, when))
            elif kind == "settle":
                handle = pendings[-1] if rng.random() < 0.6 else rng.choice(pendings)
                timeline.append(stamp({"op": rng.choice(["commit", "cancel"]), "handle": handle}, when))
            elif kind in ("attempt", "gate"):
                ids = [rng.choice("uuv") if j == 0 else rng.choice("aab") for j in range(len(limits))]
                if kind == "attempt":
                    timeline.append(stamp({"op": "attempt", "lockout": "login", "ids": ids, "verify": rng.choice([True, False, False, "raise"])}, when))
                else:
                    handle = f"g{len(gates)}"
                    gates.append(handle)
                    timeline.append(stamp({"op": "gate", "lockout": "login", "ids": ids, "handle": handle}, when))
            elif kind == "settle_gate":
                handle = gates[-1] if rng.random() < 0.6 else rng.choice(gates)
                timeline.append(stamp({"op": rng.choice(["fail", "fail", "succeed", "succeed", "cancel"]), "handle": handle}, when))
            else:
                timeline.append(stamp({"op": kind, "limiter": rng.choice(names), "id": rng.choice("uvabxy")}, when))
        vector_id = f"recipe/fuzz-{n:04d}"
        out[vector_id] = gv.recipe(vector_id, limiters, timeline, lockouts)
    return out


def build(seed: int, count: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    # One stream per kind, so that a change to one generator does not move the others.
    for index, part in enumerate((sequences, recipes, ips, shorthands, policies, keys)):
        out.update(part(random.Random(seed * 1000 + index), count))
    # Not random, and here because a port finds them in the same place: every interleaving of
    # the recipes in a few small scopes.
    out.update(explore_interleavings.build())
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seed", type=int, default=random.SystemRandom().randrange(10**9))
    parser.add_argument("--count", type=int, default=200, help="sequence and recipe vectors; the other kinds scale with it")
    parser.add_argument("--out", type=Path, default=gv.ROOT / ".fuzz")
    args = parser.parse_args()
    vectors = build(args.seed, args.count)
    shutil.rmtree(args.out, ignore_errors=True)
    for vector_id, vector in vectors.items():
        path = args.out / f"{vector_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(vector, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    (args.out / "SEED").write_text(f"{args.seed}\n", encoding="utf-8")
    print(f"seed {args.seed}: wrote {len(vectors)} vector files to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
