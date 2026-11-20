"""Never run: mypy checks that every store fits the types users will write against."""

from typing import Any

from omnilimit import (
    AsyncLimiter,
    AsyncPending,
    AsyncStore,
    Cancelled,
    CombinedDecision,
    Decision,
    Limiter,
    MemoryStore,
    Pending,
    Reservation,
    Reserved,
    Store,
    peek_all,
    peek_all_async,
    reserve_all,
    reserve_all_async,
)
from omnilimit.stores.postgres import AsyncPostgresStore, PostgresStore
from omnilimit.stores.redis import AsyncRedisStore, RedisStore

client: Any = None
stores: list[Store] = [MemoryStore(), RedisStore(client), PostgresStore(client)]
async_stores: list[AsyncStore] = [AsyncRedisStore(client), AsyncPostgresStore(client)]

for store in stores:
    limiter = Limiter(store=store, scope="s", limit=1, window_ms=1000, on_store_error="open")
    peeked: Decision = limiter.peek("id")
    was_reset: bool = limiter.reset("id")
    reserved: Reserved = limiter.reserve("id", cost=1)
    reservation: Reservation | None = reserved.reservation
    was_refunded: bool = limiter.refund(reservation)
    pending: Pending = peek_all([(limiter, "ip"), (limiter, "user", 2)])
    committed: CombinedDecision = pending.commit()
    held: Pending = reserve_all([(limiter, "ip")])
    cancelled: Cancelled = held.cancel()
    refunded: tuple[bool | None, ...] = cancelled.refunded
either: list[AsyncStore | Store] = [*async_stores, MemoryStore()]
for async_store in either:
    AsyncLimiter(store=async_store, scope="s", limit=1, window_ms=1000, on_store_error="open")


async def pending_async(limiter: AsyncLimiter) -> None:
    opened: AsyncPending = await peek_all_async([(limiter, "ip")])
    committed: CombinedDecision = await opened.commit()
    held: AsyncPending = await reserve_all_async([(limiter, "ip", 1)])
    cancelled: Cancelled = await held.cancel()
