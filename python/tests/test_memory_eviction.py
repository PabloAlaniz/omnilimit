"""What a full `MemoryStore` pushes out, and what that costs."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
from typing import Any

import pytest

from omnilimit import HitParams, Limiter, MemoryStore, RawHit

# One unit per second, one unit of burst: a hit fills the bucket, which expires a second later.
ONE = HitParams(limit=1, window_ms=1000, capacity=1000, drain_ms=1000)
# The same, expiring a millisecond after its hit.
BRIEF = HitParams(limit=1, window_ms=1000, capacity=1000, drain_ms=1)


def counted(store: MemoryStore, key: str, now_ms: int) -> bool:
    """Whether the store still holds the bucket, asked without touching it."""
    return not store.peek(key, ONE, now_ms=now_ms).allowed


def test_the_least_recently_hit_bucket_goes_when_nothing_expired() -> None:
    store = MemoryStore(max_keys=3)
    for key in ("a", "b", "c"):
        store.hit(key, ONE, now_ms=1000)
    store.hit("d", ONE, now_ms=1000)
    assert [counted(store, key, 1000) for key in "abcd"] == [False, True, True, True]
    store.hit("e", ONE, now_ms=1000)
    assert [counted(store, key, 1000) for key in "bcde"] == [False, True, True, True]


def test_an_allowed_hit_makes_its_bucket_the_most_recent() -> None:
    params = HitParams(limit=1, window_ms=1000, capacity=2000, drain_ms=2000)
    store = MemoryStore(max_keys=2)
    store.hit("a", params, now_ms=1000)
    store.hit("b", params, now_ms=1000)
    assert store.hit("a", params, now_ms=1000).allowed
    store.hit("c", params, now_ms=1000)
    # "b" went: "a" still holds both of its hits.
    assert store.peek("a", params, now_ms=1000) == RawHit(False, 2000, 1000)
    assert store.peek("b", params, now_ms=1000) == RawHit(True, 0, 1000)


def test_a_denied_hit_makes_its_bucket_the_most_recent_and_writes_nothing() -> None:
    store = MemoryStore(max_keys=2)
    store.hit("a", ONE, now_ms=1000)
    store.hit("b", ONE, now_ms=1000)
    before = store.peek("a", ONE, now_ms=1400)
    assert store.hit("a", ONE, now_ms=1400) == RawHit(False, 600, 1000)
    # The meter is where it was: the denial neither counted nor moved the timestamp.
    assert store.peek("a", ONE, now_ms=1400) == before == RawHit(False, 600, 1000)
    store.hit("c", ONE, now_ms=1400)
    assert counted(store, "a", 1400)
    assert not counted(store, "b", 1400)
    # Nor did it push the expiry back: a full drain after its one counted hit, "a" is free.
    assert store.hit("a", ONE, now_ms=2000) == RawHit(True, 1000, 2000)


def test_a_denied_bucket_that_expired_since_is_still_the_first_to_go() -> None:
    store = MemoryStore(max_keys=3)
    store.hit("a", BRIEF, now_ms=1000)
    store.hit("b", ONE, now_ms=1000)
    store.hit("c", ONE, now_ms=1000)
    # Denied at the same instant, so "a" is the most recent bucket, and it expires at 1001.
    assert not store.hit("a", BRIEF, now_ms=1000).allowed
    store.hit("d", ONE, now_ms=1500)
    assert [counted(store, key, 1500) for key in "bcd"] == [True, True, True]


def test_an_expired_bucket_goes_before_live_ones_that_are_older() -> None:
    store = MemoryStore(max_keys=3)
    store.hit("live-1", ONE, now_ms=1000)
    store.hit("expired", BRIEF, now_ms=1000)
    store.hit("live-2", ONE, now_ms=1000)
    store.hit("new", ONE, now_ms=1500)
    assert [counted(store, key, 1500) for key in ("live-1", "live-2", "new")] == [True] * 3


def test_of_two_expired_buckets_the_least_recently_hit_goes() -> None:
    store = MemoryStore(max_keys=3)
    store.hit("old", BRIEF, now_ms=1000)
    store.hit("live", ONE, now_ms=1000)
    store.hit("newer", BRIEF, now_ms=1001)
    store.hit("x", ONE, now_ms=1500)
    # "newer" is expired too, but still there: giving it a longer life shows its old state.
    assert store.peek("newer", ONE, now_ms=1001) == RawHit(False, 1000, 1001)
    assert store.peek("old", ONE, now_ms=1000) == RawHit(True, 0, 1000)


def test_an_expired_bucket_is_found_behind_any_number_of_live_ones() -> None:
    store = MemoryStore(max_keys=41)
    for index in range(40):
        store.hit(f"live-{index}", ONE, now_ms=1000)
    store.hit("expired", BRIEF, now_ms=1000)
    store.hit("new", ONE, now_ms=1500)
    assert all(counted(store, f"live-{index}", 1500) for index in range(40))
    assert store.peek("expired", ONE, now_ms=1000) == RawHit(True, 0, 1000)


def test_with_several_drain_times_the_least_recently_hit_of_all_goes() -> None:
    slow = HitParams(limit=1, window_ms=1000, capacity=1000, drain_ms=5000)
    store = MemoryStore(max_keys=4)
    store.hit("a", ONE, now_ms=1000)
    store.hit("b", slow, now_ms=1000)
    store.hit("c", ONE, now_ms=1000)
    store.hit("d", slow, now_ms=1000)
    # Nothing has expired. A hit, allowed or not, makes a bucket the newest of its drain time.
    assert not store.hit("a", ONE, now_ms=1000).allowed
    order = []
    for index, gone in enumerate("bcda"):
        store.hit(f"new-{index}", slow, now_ms=1000)
        order.append(store.peek(gone, ONE, now_ms=1000).allowed)
        assert len(store._entries) == 4
    assert order == [True, True, True, True]
    assert [counted(store, f"new-{index}", 1000) for index in range(4)] == [True] * 4


def test_of_the_expired_buckets_of_several_drain_times_the_least_recently_hit_goes() -> None:
    brief = HitParams(limit=1, window_ms=1000, capacity=1000, drain_ms=2)
    store = MemoryStore(max_keys=4)
    store.hit("live", ONE, now_ms=1000)
    store.hit("one-1", BRIEF, now_ms=1000)
    store.hit("two-1", brief, now_ms=1000)
    store.hit("one-2", BRIEF, now_ms=1001)
    # All three brief buckets have expired: they go in the order of their hits, then "live".
    for key, gone in (("w", "one-1"), ("x", "two-1"), ("y", "one-2"), ("z", "live")):
        store.hit(key, ONE, now_ms=1500)
        assert store.peek(gone, ONE, now_ms=1000) == RawHit(True, 0, 1000), gone
        assert len(store._entries) == 4


def test_reset_and_refund_keep_the_orders_of_several_drain_times() -> None:
    slow = HitParams(limit=1, window_ms=1000, capacity=2000, drain_ms=2000)
    store = MemoryStore(max_keys=3)
    store.hit("a", ONE, now_ms=1000)
    store.hit("b", slow, now_ms=1000)
    store.hit("b", slow, now_ms=1000)
    store.hit("c", ONE, now_ms=1000)
    store.reset("a")
    store.hit("d", ONE, now_ms=1000)
    # A refund is not a hit: "b" stays the least recently hit, and goes first.
    store.refund("b", slow, 1000, now_ms=1001)
    store.hit("e", ONE, now_ms=1001)
    assert store.peek("b", slow, now_ms=1001) == RawHit(True, 0, 1001)
    assert [counted(store, key, 1001) for key in "cde"] == [True, True, True]
    # A bucket refunded under another drain time is still found when its turn comes.
    store.refund("c", slow, 1000, now_ms=1001)
    store.hit("f", ONE, now_ms=1001)
    store.hit("g", ONE, now_ms=1001)
    assert [counted(store, key, 1001) for key in "cdefg"] == [False, False, True, True, True]
    assert len(store._entries) == 3


def test_a_store_goes_back_to_work_after_being_emptied_of_one_drain_time() -> None:
    store = MemoryStore(max_keys=2)
    store.hit("a", BRIEF, now_ms=1000)
    store.reset("a")
    store.hit("b", ONE, now_ms=1000)
    store.hit("c", ONE, now_ms=1000)
    store.hit("d", ONE, now_ms=1000)
    assert [counted(store, key, 1000) for key in "bcd"] == [False, True, True]


def test_a_denial_with_no_bucket_makes_none() -> None:
    store = MemoryStore(max_keys=1)
    store.hit("a", ONE, now_ms=1000)
    too_heavy = HitParams(limit=1, window_ms=3000, capacity=1000, drain_ms=7)
    assert store.hit("b", too_heavy, now_ms=1000) == RawHit(False, 0, 1000)
    assert list(store._entries) == ["a"]
    assert not store._queues


def test_a_store_never_holds_more_than_max_keys() -> None:
    store = MemoryStore(max_keys=5)
    for index in range(50):
        store.hit(f"k{index}", BRIEF if index % 3 else ONE, now_ms=1000 + index)
        store.hit(f"k{index // 2}", ONE, now_ms=1000 + index)
        assert len(store._entries) <= 5


class Watched(OrderedDict[str, Any]):
    """The store's dict, counting every entry anything walks over."""

    walked = 0

    def _count(self, entries: Iterator[Any]) -> Iterator[Any]:
        for entry in entries:
            self.walked += 1
            yield entry

    def __iter__(self) -> Iterator[str]:
        return self._count(super().__iter__())

    def keys(self) -> Any:
        return self._count(iter(super().keys()))

    def values(self) -> Any:
        return self._count(iter(super().values()))

    def items(self) -> Any:
        return self._count(iter(super().items()))

    def copy(self) -> Any:
        self.walked += len(self)
        return super().copy()


def entries_walked_per_new_key(max_keys: int, new_keys: int = 50) -> float:
    store = MemoryStore(max_keys=max_keys)
    watched = Watched()
    store._entries = watched
    for index in range(max_keys):
        store.hit(f"live-{index}", ONE, now_ms=1000)
    assert len(watched) == max_keys
    watched.walked = 0
    for index in range(new_keys):
        assert store.hit(f"new-{index}", ONE, now_ms=1000).allowed
    assert len(watched) == max_keys
    return watched.walked / new_keys


def test_a_new_key_in_a_full_store_costs_the_same_whatever_the_cap() -> None:
    small = entries_walked_per_new_key(100)
    large = entries_walked_per_new_key(20_000)
    assert small == large <= 1


def test_so_it_does_with_several_drain_times() -> None:
    walked = []
    for max_keys in (100, 20_000):
        store = MemoryStore(max_keys=max_keys)
        slow = HitParams(limit=1, window_ms=1000, capacity=1000, drain_ms=5000)
        for index in range(max_keys):
            store.hit(f"live-{index}", slow if index % 2 else ONE, now_ms=1000)
        watched = [Watched(store._entries), *(Watched(queue) for queue in store._queues.values())]
        assert len(watched) == 3
        store._entries = watched[0]
        store._queues = dict(zip(list(store._queues), watched[1:], strict=True))
        for index in range(50):
            assert store.hit(f"new-{index}", ONE, now_ms=1000).allowed
        assert len(store._entries) == max_keys
        walked.append(sum(order.walked for order in watched) / 50)
    # The first bucket of each drain time, then the first of all.
    assert walked[0] == walked[1] <= 3


def test_recurring_keys_in_a_full_store_walk_nothing() -> None:
    store = MemoryStore(max_keys=100)
    watched = Watched()
    store._entries = watched
    for index in range(100):
        store.hit(f"k{index}", ONE, now_ms=1000)
    watched.walked = 0
    for index in range(100):
        store.hit(f"k{index}", ONE, now_ms=1000)  # denied
        store.hit(f"k{index}", ONE, now_ms=5000)  # allowed
    assert watched.walked == 0


@pytest.mark.parametrize("flood", [0, 250])
def test_a_key_flood_does_not_reopen_the_limit_of_a_client_being_denied(flood: int) -> None:
    """One client at 10 times its limit for 3 windows, while never-seen keys arrive at 12.5
    times the cap per window. It gets what it gets with no flood: its burst, then the refill."""
    limit, window_ms, cap = 5, 60_000, 1000
    clock = 1_800_000_000_000
    store = MemoryStore(max_keys=cap, now=lambda: clock)
    limiter = Limiter(
        store=store, scope="t", limit=limit, window_ms=window_ms, on_store_error="closed"
    )
    gap = window_ms // (10 * limit)
    start, admitted, others = clock, 0, 0
    for index in range(3 * 10 * limit):
        clock = start + index * gap
        admitted += limiter.check("abuser").allowed
        for extra in range(flood):
            clock = start + index * gap + 1 + extra * 4
            assert limiter.check(f"flood:{others}").allowed
            others += 1
    assert len(store._entries) <= cap
    # 5 of burst, then one every 12 s for the 178.8 s that follow: 5 + 14.
    assert admitted == 19
