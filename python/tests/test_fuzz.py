"""The port against the independent model on vectors nobody picked.

`tools/fuzz_vectors.py` draws them from a new seed, and OMNILIMIT_FUZZ_VECTORS names their
directory. Without it there is nothing to run, and every test here is skipped.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from schedule import run_order, run_order_async
from support import (
    FUZZ_SEED,
    FUZZ_VECTORS,
    fuzz_of,
    run_recipe,
    run_recipe_async,
    run_sequence,
    run_sequence_async,
)

from omnilimit import MemoryStore
from omnilimit._gcra import parse_policy, resolve_policy
from omnilimit._key import derive_key
from omnilimit.ip import mask_ip, resolve_client_ip

SEED = FUZZ_SEED

pytestmark = pytest.mark.skipif(SEED is None, reason="OMNILIMIT_FUZZ_VECTORS is not set")


def _by_id(kind: str) -> Any:
    return pytest.mark.parametrize("vector", fuzz_of(kind), ids=lambda v: f"seed-{SEED}:{v['id']}")


def test_fuzz_has_every_kind() -> None:
    assert sorted({v["kind"] for v in FUZZ_VECTORS}) == [
        "contract",
        "interleaving",
        "ip",
        "key",
        "recipe",
        "sequence",
    ]


@_by_id("sequence")
def test_fuzz_sequence(vector: dict[str, Any]) -> None:
    run_sequence(vector, MemoryStore())


@_by_id("sequence")
def test_fuzz_sequence_async(vector: dict[str, Any]) -> None:
    asyncio.run(run_sequence_async(vector, MemoryStore()))


@_by_id("recipe")
def test_fuzz_recipe(vector: dict[str, Any]) -> None:
    run_recipe(vector, MemoryStore())


@_by_id("recipe")
def test_fuzz_recipe_async(vector: dict[str, Any]) -> None:
    asyncio.run(run_recipe_async(vector, MemoryStore()))


# Not random: every order of the store calls of a few concurrent callers, each with the result
# the model got when it ran in that order.
@_by_id("interleaving")
def test_every_interleaving(vector: dict[str, Any]) -> None:
    assert len(vector["runs"]) > 10
    for picks, allowed, verify_ran, levels in vector["runs"]:
        assert run_order(vector, picks) == (allowed, verify_ran, levels), f"order {picks}"


@_by_id("interleaving")
def test_every_interleaving_async(vector: dict[str, Any]) -> None:
    async def run() -> None:
        for picks, allowed, verify_ran, levels in vector["runs"]:
            got = await run_order_async(vector, picks)
            assert got == (allowed, verify_ran, levels), f"order {picks}"

    asyncio.run(run())


def test_fuzz_client_ips() -> None:
    for vector in fuzz_of("ip"):
        for case in vector["cases"]:
            strategy = case["strategy"]
            kwargs: dict[str, Any] = (
                {"preset": strategy["preset"]}
                if "preset" in strategy
                else {"xff_hops": strategy["xff"]["hops"]}
            )
            got = resolve_client_ip(case["headers"], case.get("peer"), **kwargs)
            assert got == case["expect"], f"seed {SEED}: {case!r}"
        for mask in vector["masks"]:
            assert mask_ip(mask["ip"], mask["prefix"]) == mask["expect"], f"seed {SEED}: {mask!r}"


def test_fuzz_keys() -> None:
    for vector in fuzz_of("key"):
        for case in vector["cases"]:
            policy = resolve_policy(**case["policy"])
            key = derive_key(case["scope"], policy, case["salt"], case["id"])
            assert key == case["expect"], f"seed {SEED}: {case!r}"


def test_fuzz_policies() -> None:
    for vector in fuzz_of("contract", "policy"):
        for case in vector["cases"]:
            if case["valid"]:
                resolve_policy(**case["policy"])
            else:
                with pytest.raises(ValueError):
                    resolve_policy(**case["policy"])
                    pytest.fail(f"seed {SEED}: accepted {case!r}")


def test_fuzz_policy_shorthand() -> None:
    for vector in fuzz_of("contract", "shorthand"):
        for case in vector["cases"]:
            if case["policy"] is not None:
                assert parse_policy(case["text"]) == case["policy"], f"seed {SEED}: {case!r}"
            else:
                with pytest.raises(ValueError):
                    parse_policy(case["text"])
                    pytest.fail(f"seed {SEED}: accepted {case['text']!r}")
