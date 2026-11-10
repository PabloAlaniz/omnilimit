"""Never run: mypy checks that every store fits the types users will write against."""

from typing import Any

from omnilimit import AsyncLimiter, AsyncStore, Decision, Limiter, MemoryStore, Store
from omnilimit.stores.postgres import AsyncPostgresStore, PostgresStore
from omnilimit.stores.redis import AsyncRedisStore, RedisStore

client: Any = None
stores: list[Store] = [MemoryStore(), RedisStore(client), PostgresStore(client)]
async_stores: list[AsyncStore] = [AsyncRedisStore(client), AsyncPostgresStore(client)]

for store in stores:
    limiter = Limiter(store=store, scope="s", limit=1, window_ms=1000, on_store_error="open")
    peeked: Decision = limiter.peek("id")
    was_reset: bool = limiter.reset("id")
either: list[AsyncStore | Store] = [*async_stores, MemoryStore()]
for async_store in either:
    AsyncLimiter(store=async_store, scope="s", limit=1, window_ms=1000, on_store_error="open")
