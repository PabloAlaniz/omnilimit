"""Python-specific behaviour the shared vectors cannot reach."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from omnilimit import AsyncLimiter, HitParams, Limiter, LimiterEvent, MemoryStore, RawHit
from omnilimit._gcra import resolve_policy
from omnilimit._key import derive_key
from omnilimit.ip import canonical_ip, mask_ip, resolve_client_ip
from omnilimit.stores import memory

BASE: dict[str, Any] = {"scope": "t", "limit": 2, "window_ms": 1000, "on_store_error": "closed"}


class Blocking:
    tier = "exact"

    def __init__(self) -> None:
        self.release = threading.Event()
        self.calls = 0

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        self.calls += 1
        self.release.wait(5)
        return RawHit(allowed=True, level=params.window_ms, ts=0)


class AsyncOk:
    tier = "exact"

    async def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        return RawHit(allowed=True, level=params.window_ms, ts=0)


@pytest.mark.parametrize("limit", [True, 1.0, "1", None])
def test_only_real_integers_are_a_policy(limit: Any) -> None:
    with pytest.raises(ValueError, match="limit"):
        resolve_policy(limit, 1000)


def test_burst_true_is_rejected() -> None:
    with pytest.raises(ValueError, match="burst"):
        resolve_policy(1, 1000, True)


def test_burst_defaults_to_limit() -> None:
    assert resolve_policy(7, 1000).burst == 7


def test_on_store_error_has_no_default() -> None:
    with pytest.raises(TypeError, match="on_store_error"):
        Limiter(store=MemoryStore(), scope="t", limit=1, window_ms=1000)  # type: ignore[call-arg]


def test_on_store_error_must_be_known() -> None:
    with pytest.raises(ValueError, match="on_store_error"):
        Limiter(**{**BASE, "on_store_error": "maybe"}, store=MemoryStore())


@pytest.mark.parametrize("field", ["store_timeout_ms", "max_store_threads"])
def test_limits_on_the_limiter_must_be_positive(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        Limiter(**BASE, store=MemoryStore(), **{field: 0})


def test_async_store_needs_async_limiter() -> None:
    with pytest.raises(TypeError, match="AsyncLimiter"):
        Limiter(**BASE, store=AsyncOk())  # type: ignore[arg-type]


def test_blocking_store_is_refused_by_async_limiter() -> None:
    with pytest.raises(TypeError, match="block the event loop"):
        AsyncLimiter(**BASE, store=Blocking())


def test_properties() -> None:
    for limiter in (
        Limiter(**BASE, store=MemoryStore()),
        AsyncLimiter(**BASE, store=MemoryStore()),
    ):
        assert (limiter.scope, limiter.tier) == ("t", "local")


def test_events_report_each_outcome_and_cannot_break_a_decision() -> None:
    events: list[LimiterEvent] = []

    def hook(event: LimiterEvent) -> None:
        events.append(event)
        raise RuntimeError("broken hook")

    limiter = Limiter(**{**BASE, "limit": 1}, store=MemoryStore(), on_event=hook)
    assert limiter.check("a", now_ms=1000).allowed
    assert not limiter.check("a", now_ms=1000).allowed
    assert [event.type for event in events] == ["allowed", "limited"]


def test_store_error_event_carries_the_error() -> None:
    events: list[LimiterEvent] = []
    boom = ConnectionError("down")

    class Failing:
        tier = "exact"

        def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
            raise boom

    Limiter(**BASE, store=Failing(), on_event=events.append).check("a")
    assert events[0].type == "store_error"
    assert events[0].error is boom


def test_a_hung_store_only_delays_a_request_by_the_timeout() -> None:
    store = Blocking()
    limiter = Limiter(**BASE, store=store, store_timeout_ms=30)
    started = time.monotonic()
    decision = limiter.check("a")
    store.release.set()
    assert decision.reason == "store_error"
    assert time.monotonic() - started < 2


def test_threads_are_capped_while_a_store_hangs() -> None:
    store = Blocking()
    limiter = Limiter(**BASE, store=store, store_timeout_ms=20, max_store_threads=1)
    assert limiter.check("a").reason == "store_error"
    # The only slot is still held by the first call, so this one never reaches the store.
    assert limiter.check("b").reason == "store_error"
    assert store.calls == 1
    store.release.set()


def test_the_store_call_slot_comes_back() -> None:
    store = Blocking()
    store.release.set()
    limiter = Limiter(**BASE, store=store, store_timeout_ms=1000, max_store_threads=1)
    for _ in range(3):
        assert limiter.check("a").reason == "ok"


def test_failing_to_start_a_thread_is_a_store_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    store = Blocking()
    limiter = Limiter(**BASE, store=store, store_timeout_ms=20, max_store_threads=1)
    monkeypatch.setattr(threading.Thread, "start", refuse)
    assert limiter.check("a").reason == "store_error"
    monkeypatch.undo()
    store.release.set()
    assert limiter.check("a").reason == "ok"  # the slot was not lost


def test_async_limiter_accepts_async_stores() -> None:
    limiter = AsyncLimiter(**BASE, store=AsyncOk())
    assert asyncio.run(limiter.check("a")).allowed


def test_custom_header_function_is_used() -> None:
    limiter = Limiter(**BASE, store=MemoryStore(), headers=lambda data: {"x-scope": data.scope})
    assert limiter.check("a", now_ms=1000).headers == {"x-scope": "t"}


def test_none_profile_only_sends_retry_after() -> None:
    limiter = Limiter(**{**BASE, "limit": 1}, store=MemoryStore(), headers="none")
    assert limiter.check("a", now_ms=1000).headers == {}
    assert limiter.check("a", now_ms=1000).headers == {"retry-after": "1"}


def test_memory_store_rejects_a_bad_cap() -> None:
    with pytest.raises(ValueError, match="max_keys"):
        MemoryStore(max_keys=0)


def test_memory_store_uses_the_real_clock() -> None:
    limiter = Limiter(**{**BASE, "limit": 1}, store=MemoryStore())
    assert limiter.check("a").allowed
    denied = limiter.check("a")
    assert not denied.allowed
    assert denied.retry_after_ms is not None
    assert 0 < denied.retry_after_ms <= 1000


def test_memory_store_evicts_expired_buckets_first() -> None:
    params = HitParams(limit=1, window_ms=1000, capacity=1000, drain_ms=1000)
    store = MemoryStore(max_keys=2)
    store.hit("expired", HitParams(1, 1000, 1000, 1), now_ms=1000)
    store.hit("live", params, now_ms=1000)
    store.hit("new", params, now_ms=1500)
    # "live" kept its quota used; a re-created bucket would allow this hit.
    assert not store.hit("live", params, now_ms=1500).allowed


def test_memory_store_evicts_the_oldest_when_nothing_expired() -> None:
    params = HitParams(limit=1, window_ms=1000, capacity=1000, drain_ms=1000)
    store = MemoryStore(max_keys=2)
    for key in ("a", "b", "c"):
        store.hit(key, params, now_ms=1000)
    assert store.hit("a", params, now_ms=1000).allowed  # forgotten
    assert not store.hit("c", params, now_ms=1000).allowed  # still counted


def test_memory_store_checks_and_writes_atomically(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pause between reading and writing a bucket, so a missing lock always shows up."""
    real_apply_hit = memory.apply_hit

    def slow_apply_hit(*args: Any) -> Any:
        time.sleep(0.002)
        return real_apply_hit(*args)

    monkeypatch.setattr(memory, "apply_hit", slow_apply_hit)
    params = HitParams(limit=1, window_ms=1000, capacity=5000, drain_ms=5000)
    store = MemoryStore()
    with ThreadPoolExecutor(max_workers=16) as pool:
        hits = list(pool.map(lambda _: store.hit("a", params, now_ms=1000), range(16)))
    assert sum(hit.allowed for hit in hits) == 5


def test_memory_store_counts_denials_as_no_write() -> None:
    params = HitParams(limit=1, window_ms=1000, capacity=1000, drain_ms=1000)
    store = MemoryStore()
    store.hit("a", params, now_ms=1000)
    assert store.hit("a", params, now_ms=1500).ts == 1000


def test_lone_surrogates_hash_like_javascript() -> None:
    policy = resolve_policy(1, 1000)
    assert derive_key("t", policy, "", "\ud800") == derive_key("t", policy, "", "�")


class TestClientIp:
    def test_a_client_supplied_header_line_cannot_pass_for_the_trusted_one(self) -> None:
        class Lines:
            def getlist(self, name: str) -> list[str]:
                return ["6.6.6.6", "203.0.113.7"]

        assert resolve_client_ip(Lines(), xff_hops=0) == "203.0.113.7"

    def test_a_plain_mapping_may_hold_a_list(self) -> None:
        headers = {"x-forwarded-for": ["6.6.6.6", "203.0.113.7"]}
        assert resolve_client_ip(headers, xff_hops=0) == "203.0.113.7"

    def test_an_empty_list_is_a_missing_header(self) -> None:
        assert resolve_client_ip({"x-forwarded-for": []}, xff_hops=0) is None

    @pytest.mark.parametrize(
        "kwargs",
        [{}, {"preset": "direct", "xff_hops": 0}, {"use_peer": True, "header": "x-real-ip"}],
    )
    def test_exactly_one_source_must_be_chosen(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            resolve_client_ip({}, "1.2.3.4", **kwargs)

    def test_unknown_preset(self) -> None:
        with pytest.raises(ValueError, match="preset"):
            resolve_client_ip({}, preset="nope")  # type: ignore[arg-type]

    @pytest.mark.parametrize("hops", [-1, True, 1.0])
    def test_bad_hops(self, hops: Any) -> None:
        with pytest.raises(ValueError, match="hops"):
            resolve_client_ip({}, xff_hops=hops)

    def test_custom_header_is_lowercased_and_trimmed(self) -> None:
        assert resolve_client_ip({"x-real-ip": " 1.2.3.4 "}, header="X-Real-IP") == "1.2.3.4"

    def test_missing_custom_header(self) -> None:
        assert resolve_client_ip({}, header="x-real-ip") is None

    def test_peer(self) -> None:
        assert resolve_client_ip({}, "1.2.3.4", use_peer=True) == "1.2.3.4"

    @pytest.mark.parametrize(
        "raw",
        ["01.2.3.4", "1.2.3", "1.2.3.256", "1.2.3.4\n", "\u0661.2.3.4", "fe80::1%eth0", "::1%1"],
    )
    def test_rejected(self, raw: str) -> None:
        assert canonical_ip(raw) is None

    def test_mapped_becomes_ipv4(self) -> None:
        assert canonical_ip("::ffff:1.2.3.4") == "1.2.3.4"

    def test_mask_rejects_garbage_and_keeps_ipv4(self) -> None:
        assert mask_ip("junk", 64) is None
        assert mask_ip("1.2.3.4", 64) == "1.2.3.4"

    @pytest.mark.parametrize("prefix", [-1, 129, True, 64.0])
    def test_mask_prefix_is_validated(self, prefix: Any) -> None:
        with pytest.raises(ValueError, match="prefix"):
            mask_ip("2001:db8::1", prefix)


class TestPeekAndReset:
    def test_peek_does_not_consume(self) -> None:
        limiter = Limiter(**{**BASE, "limit": 1}, store=MemoryStore())
        for _ in range(3):
            peeked = limiter.peek("a", now_ms=1000)
            assert (peeked.allowed, peeked.remaining, peeked.reason) == (True, 1, "ok")
        assert limiter.check("a", now_ms=1000).allowed
        denied = limiter.peek("a", now_ms=1000)
        assert (denied.allowed, denied.status, denied.retry_after_ms) == (False, 429, 1000)
        assert denied.headers["retry-after"] == "1"

    def test_peek_on_memory_writes_nothing_and_keeps_the_eviction_order(self) -> None:
        params = HitParams(limit=1, window_ms=1000, capacity=1000, drain_ms=1000)
        store = MemoryStore(max_keys=2)
        store.hit("a", params, now_ms=1000)
        store.hit("b", params, now_ms=1000)
        assert store.peek("missing", params, now_ms=1000) == RawHit(True, 0, 1000)
        assert store.peek("a", params, now_ms=1500) == RawHit(False, 500, 1000)
        store.hit("c", params, now_ms=1000)
        # "a" stayed the oldest: a peek is not a use.
        assert store.hit("a", params, now_ms=1000).allowed
        assert not store.hit("c", params, now_ms=1000).allowed

    def test_reset_forgets_only_its_id(self) -> None:
        events: list[LimiterEvent] = []
        limiter = Limiter(**{**BASE, "limit": 1}, store=MemoryStore(), on_event=events.append)
        assert limiter.check("a", now_ms=1000).allowed
        assert limiter.check("b", now_ms=1000).allowed
        assert limiter.reset("a") is True
        assert limiter.reset("never-seen") is True
        assert limiter.check("a", now_ms=1000).allowed
        assert not limiter.check("b", now_ms=1000).allowed
        assert events[2] == LimiterEvent("reset", "t", "local", op="reset")

    def test_events_say_which_operation_they_come_from(self) -> None:
        events: list[LimiterEvent] = []
        limiter = Limiter(**{**BASE, "limit": 1}, store=MemoryStore(), on_event=events.append)
        limiter.check("a", now_ms=1000)
        limiter.peek("a", now_ms=1000)
        limiter.reset("a")
        assert [(e.type, e.op) for e in events] == [
            ("allowed", "check"),
            ("limited", "peek"),
            ("reset", "reset"),
        ]

    def test_a_store_without_peek_or_reset_is_a_configuration_error(self) -> None:
        events: list[LimiterEvent] = []
        limiter = Limiter(**BASE, store=Blocking(), on_event=events.append)
        with pytest.raises(TypeError, match="does not support peek"):
            limiter.peek("a")
        with pytest.raises(TypeError, match="does not support reset"):
            limiter.reset("a")
        async_limiter = AsyncLimiter(**BASE, store=AsyncOk())
        with pytest.raises(TypeError, match="does not support peek"):
            asyncio.run(async_limiter.peek("a"))
        with pytest.raises(TypeError, match="does not support reset"):
            asyncio.run(async_limiter.reset("a"))
        assert events == []

    def test_peek_failure_follows_on_store_error(self) -> None:
        events: list[LimiterEvent] = []
        boom = ConnectionError("down")

        class Failing(Blocking):
            def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
                raise boom

        for mode, status in (("closed", 503), ("open", None)):
            limiter = Limiter(
                **{**BASE, "on_store_error": mode}, store=Failing(), on_event=events.append
            )
            decision = limiter.peek("a")
            assert (decision.reason, decision.status) == ("store_error", status)
        assert [(e.type, e.op, e.error) for e in events] == [("store_error", "peek", boom)] * 2

    def test_reset_with_a_failing_store_returns_false_and_reports_it(self) -> None:
        events: list[LimiterEvent] = []
        boom = ConnectionError("down")

        class Failing(Blocking):
            def reset(self, key: str) -> None:
                raise boom

        limiter = Limiter(**BASE, store=Failing(), on_event=events.append)
        assert limiter.reset("a") is False
        assert [(e.type, e.op, e.error) for e in events] == [("store_error", "reset", boom)]

    def test_reset_with_a_hung_store_times_out_to_false(self) -> None:
        events: list[LimiterEvent] = []
        release = threading.Event()

        class Hanging(Blocking):
            def reset(self, key: str) -> None:
                release.wait(5)

        limiter = Limiter(**BASE, store=Hanging(), store_timeout_ms=20, on_event=events.append)
        try:
            assert limiter.reset("a") is False
        finally:
            release.set()
        assert events[0].type == "store_error"
        assert isinstance(events[0].error, TimeoutError)

    def test_async_peek_and_reset(self) -> None:
        events: list[LimiterEvent] = []
        reset_keys: list[str] = []

        class Async(AsyncOk):
            async def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
                return RawHit(allowed=True, level=0, ts=0)

            async def reset(self, key: str) -> None:
                reset_keys.append(key)

        limiter = AsyncLimiter(**BASE, store=Async(), on_event=events.append)
        assert asyncio.run(limiter.peek("a")).remaining == 2
        assert asyncio.run(limiter.reset("a")) is True
        assert len(reset_keys) == 1
        assert [(e.type, e.op) for e in events] == [("allowed", "peek"), ("reset", "reset")]

    def test_async_reset_failure_and_timeout(self) -> None:
        events: list[LimiterEvent] = []

        class Broken(AsyncOk):
            async def reset(self, key: str) -> None:
                raise ConnectionError("down")

        class Hung(AsyncOk):
            async def reset(self, key: str) -> None:
                await asyncio.sleep(3600)

        for store in (Broken(), Hung()):
            limiter = AsyncLimiter(**BASE, store=store, store_timeout_ms=20, on_event=events.append)
            assert asyncio.run(limiter.reset("a")) is False
        assert [(e.type, e.op) for e in events] == [("store_error", "reset")] * 2
        assert isinstance(events[1].error, TimeoutError)
