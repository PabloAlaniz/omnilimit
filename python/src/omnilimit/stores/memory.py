"""In-process store."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Literal

from omnilimit._gcra import MeterState, apply_hit
from omnilimit._types import HitParams, RawHit


class MemoryStore:
    """Exact within one process and independent per instance: N instances allow about N times
    the limit. It is thread-safe, and `max_keys` stops rotating ids from exhausting memory.
    """

    tier: Literal["local"] = "local"

    def __init__(self, max_keys: int = 100_000) -> None:
        if not isinstance(max_keys, int) or isinstance(max_keys, bool) or max_keys < 1:
            raise ValueError("omnilimit: max_keys must be a positive integer")
        self._max_keys = max_keys
        self._lock = threading.Lock()
        # key -> (state, expires_at); insertion order tracks recency
        self._entries: OrderedDict[str, tuple[MeterState, int]] = OrderedDict()

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        now = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        with self._lock:
            entry = self._entries.get(key)
            hit, write = apply_hit(entry[0] if entry else None, params, now)
            if write is not None:
                self._entries[key] = (write, write.ts + params.drain_ms)
                self._entries.move_to_end(key)
                self._evict(now)
            return hit

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
