"""In-process store."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Literal

from omnilimit._gcra import MeterState, apply_hit, apply_peek, apply_refund
from omnilimit._types import HitParams, RawHit, RawRefund

# In place of the one drain time of a store, once its buckets have several.
_SEVERAL = -1

_Entry = tuple[MeterState, int]


def _wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


class MemoryStore:
    """Exact within one process and independent per instance: N instances allow about N times
    the limit. It is thread-safe, and `max_keys` stops rotating ids from exhausting memory.

    A new bucket in a full store pushes one out, at a cost that does not depend on `max_keys`:
    an expired one if the least recently hit bucket of some drain time has expired, or else the
    least recently hit of all. A denied hit counts as a hit here, although it writes nothing: a
    client that keeps being denied must not age out and come back with an empty meter.

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
        # key -> (state, expires_at); the order is that of the last hit, oldest first
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        # Buckets that drain in the same time expire in the order of their counted hits, so with
        # one drain time the first entry is the first to expire, unless a denial has since moved
        # an older one behind it. With several, `_queues` keeps that order for each: drain time
        # -> key -> the number of its last hit, oldest hit first.
        self._drain_ms: int | None = None
        self._queues: dict[int, OrderedDict[str, int]] = {}
        self._hits = 0

    def hit(self, key: str, params: HitParams, now_ms: int | None = None) -> RawHit:
        now = now_ms if now_ms is not None else self._now()
        with self._lock:
            entries = self._entries
            entry = entries.get(key)
            hit, write = apply_hit(entry[0] if entry else None, params, now)
            if write is not None:
                entries[key] = (write, write.ts + params.drain_ms)
            if entry is not None:
                # Allowed or denied, the bucket is in use. A denial leaves its state alone.
                entries.move_to_end(key)
            elif write is None:
                return hit
            if params.drain_ms != self._drain_ms:
                self._queue(key, entry)
            if entry is None and len(entries) > self._max_keys:
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
            entry = self._entries.pop(key, None)
            if entry is not None and self._queues:
                self._unqueue(key, entry)

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

    def _queue(self, key: str, before: _Entry | None) -> None:
        """Make `key` the newest of its drain time, if the store has to tell drain times apart."""
        entries, queues = self._entries, self._queues
        state, expires_at = entries[key]
        drain_ms = expires_at - state.ts
        if not queues:
            if self._drain_ms is None:
                self._drain_ms = drain_ms
            if drain_ms == self._drain_ms:
                return
            # A second drain time. Once: every bucket goes to the queue of its own.
            self._drain_ms = _SEVERAL
            for other, (state, expires_at) in entries.items():
                self._hits += 1
                queues.setdefault(expires_at - state.ts, OrderedDict())[other] = self._hits
            return
        if before is not None:
            self._unqueue(key, before)
        queue = queues.get(drain_ms)
        if queue is None:
            queue = queues[drain_ms] = OrderedDict()
        self._hits += 1
        queue[key] = self._hits

    def _unqueue(self, key: str, entry: _Entry) -> None:
        queue = self._queues.get(entry[1] - entry[0].ts)
        if queue is not None and key in queue:
            del queue[key]
            return
        # Its drain time is not the one it was queued under: a refund with other parameters.
        for queue in self._queues.values():
            queue.pop(key, None)

    def _evict(self, now: int) -> None:
        entries, queues = self._entries, self._queues
        while len(entries) > self._max_keys:
            if not queues:
                # One drain time: the least recently hit bucket goes, expired or not.
                entries.popitem(last=False)
                continue
            # An expired bucket if a queue starts with one, the least recently hit of those if
            # several do, or else the least recently hit of all.
            expired: tuple[int, str] | None = None
            for drain_ms, queue in tuple(queues.items()):
                if not queue:
                    del queues[drain_ms]
                    continue
                key, hit = next(iter(queue.items()))
                if entries[key][1] <= now and (expired is None or hit < expired[0]):
                    expired = (hit, key)
            if expired is None:
                key, entry = entries.popitem(last=False)
            else:
                key = expired[1]
                entry = entries.pop(key)
            self._unqueue(key, entry)
