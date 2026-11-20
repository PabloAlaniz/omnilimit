"""In-process store."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Literal

from omnilimit._gcra import MeterState, apply_hit, apply_peek, apply_refund
from omnilimit._types import HitParams, RawHit, RawRefund


def _wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


class MemoryStore:
    """Exact within one process and independent per instance: N instances allow about N times
    the limit. It is thread-safe, and `max_keys` stops rotating ids from exhausting memory.

    `now` is the clock, in milliseconds since the epoch. Pass your own to move time in tests
    without patching `time`.
    """

    tier: Literal["local"] = "local"

    def __init__(self, max_keys: int = 100_000, *, now: Callable[[], int] | None = None) -> None:
        if not isinstance(max_keys, int) or isinstance(max_keys, bool) or max_keys < 1:
            raise ValueError("omnilimit: max_keys must be a positive integer")
        self._max_keys = max_keys
        self._now = now if now is not None else _wall_clock_ms
        self._lock = threading.Lock()
        # key -> (state, expires_at); insertion order tracks recency
        self._entries: OrderedDict[str, tuple[MeterState, int]] = OrderedDict()

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        now = now_ms if now_ms is not None else self._now()
        with self._lock:
            entry = self._entries.get(key)
            hit, write = apply_hit(entry[0] if entry else None, params, now)
            if write is not None:
                self._entries[key] = (write, write.ts + params.drain_ms)
                self._entries.move_to_end(key)
                self._evict(now)
            return hit

    def peek(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        """What `hit` would answer now. It writes nothing and leaves the eviction order alone."""
        now = now_ms if now_ms is not None else self._now()
        with self._lock:
            entry = self._entries.get(key)
            return apply_peek(entry[0] if entry else None, params, now)

    def reset(self, key: str) -> None:
        """Forget the bucket, so the next hit starts from an empty meter."""
        with self._lock:
            self._entries.pop(key, None)

    def refund(
        self, key: str, params: HitParams, since: int, now_ms: int | None = None
    ) -> RawRefund:
        """Give back a hit counted at `since`, as far as it has not drained.

        The bucket keeps its place in the eviction order: giving a request back does not make
        it more recent.
        """
        now = now_ms if now_ms is not None else self._now()
        with self._lock:
            entry = self._entries.get(key)
            refund, write = apply_refund(entry[0] if entry else None, params, since, now)
            if write is not None:
                self._entries[key] = (write, write.ts + params.drain_ms)
            return refund

    def _evict(self, now: int) -> None:
        if len(self._entries) <= self._max_keys:
            return
        for key, (_, expires_at) in list(self._entries.items()):
            if expires_at <= now:
                del self._entries[key]
            if len(self._entries) <= self._max_keys:
                return
        while len(self._entries) > self._max_keys:
            self._entries.popitem(last=False)
