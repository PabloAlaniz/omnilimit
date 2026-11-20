"""Never run: mypy checks that every store fits the types users will write against."""

from typing import Any

from omnilimit import (
    AsyncLimiter,
    AsyncLockout,
    AsyncLockoutGate,
    AsyncPending,
    Attempt,
    AsyncStore,
    Cancelled,
    CombinedDecision,
    Decision,
    Limiter,
    Lockout,
    LockoutGate,
    LockoutLimit,
    MemoryStore,
    Pending,
    Reservation,
    Reserved,
    Store,
    Succeeded,
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


def lockouts(limiter: Limiter, async_limiter: AsyncLimiter) -> None:
    earlier: Lockout[str] = Lockout(limiter, reset_on_success=False)
    attempt: Attempt = earlier.attempt("id", lambda: True)
    every: tuple[Decision, ...] = attempt.decisions
    by_subject: Lockout[tuple[str, str]] = Lockout(
        limits=[
            LockoutLimit(limiter, id_of=lambda s: s[0]),
            LockoutLimit(limiter, id_of=lambda s: s[1], reset_on_success=False),
        ],
        mode="reserve",
    )
    gate: LockoutGate = by_subject.begin(("subject", "ip"))
    succeeded: Succeeded = gate.succeed()
    resets: tuple[bool | None, ...] = succeeded.resets
    failed: CombinedDecision = by_subject.begin(("s", "i")).fail()
    AsyncLockout(async_limiter)
    AsyncLockout(limits=[LockoutLimit(async_limiter)], mode="advisory")


async def async_gate(lockout: AsyncLockout[str]) -> None:
    gate: AsyncLockoutGate = await lockout.begin("id")
    cancelled: Cancelled = await gate.cancel()
