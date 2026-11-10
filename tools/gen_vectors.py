#!/usr/bin/env python3
"""Generate the conformance vectors in spec/vectors.

This is deliberately an independent implementation written in Python with only
the standard library: if the TypeScript port produced its own expectations it
would only be validating itself. Expected IPs come from the `ipaddress` module.

Usage: gen_vectors.py [--check]   (--check fails if the committed vectors differ)
"""

from __future__ import annotations

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

    def hit(self, now: int) -> dict:
        ts = now if self.ts is None else self.ts
        t = max(now, ts)
        elapsed = min(t - ts, self.drain)
        drained = max(0, self.level - elapsed * self.limit)
        rate = Fraction(self.limit, self.window)
        exact = max(Fraction(0), self.exact - elapsed * rate)
        assert Fraction(drained, self.window) == exact, "integer meter diverged from the rational model"
        if drained + self.window <= self.capacity:
            self.level, self.ts = drained + self.window, t
            self.exact = exact + 1
            allowed = True
            level = self.level
            retry = None
        else:
            allowed = False
            level = drained
            retry = ceil_div(drained + self.window - self.capacity, self.limit)
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

    def peek(self, now: int) -> dict:
        """The decision a hit would get now. Nothing is written."""
        ts = now if self.ts is None else self.ts
        t = max(now, ts)
        drained = max(0, self.level - min(t - ts, self.drain) * self.limit)
        allowed = drained + self.window <= self.capacity
        retry = None if allowed else ceil_div(drained + self.window - self.capacity, self.limit)
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
    """`moves` are `(at_ms, id)` hits, or `(at_ms, id, op)` with op `hit`, `peek` or `reset`."""
    meters: dict[str, Meter] = {}
    steps = []
    for move in moves:
        at, who = move[0], move[1]
        op = move[2] if len(move) > 2 else "hit"
        meter = meters.setdefault(who, Meter(limit, window_ms, burst))
        if op == "reset":
            meter.reset()
            steps.append({"at_ms": at, "id": who, "op": "reset"})
            continue
        r = meter.peek(at) if op == "peek" else meter.hit(at)
        # `op` is only written when it is not a hit, so version-1 vectors keep their bytes.
        tag = {"op": op} if op != "hit" else {}
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


def build() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for part in (sequences(), keys(), ips(), failures(), concurrency(), contract()):
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
