# Stores

What each store can do besides `check`:

| Store | Tier | `peek` | `reset` | `reserve` and `refund` |
|---|---|---|---|---|
| Redis, Valkey | `exact` | ✓ | ✓ | ✓ |
| Postgres | `exact` | ✓ | ✓ | ✓, after reapplying `MIGRATION_SQL` from 0.4 |
| Durable Objects | `exact` | ✓ | ✓ | ✓ |
| Memory | `local` | ✓ | ✓ | ✓ |
| Cloudflare rate limit binding | `approximate` | | | |

A missing operation raises a `TypeError`, which is a configuration mistake rather than a store failure.

## Redis and Valkey

```ts
import { fromIoredis, fromNodeRedis, redisStore } from 'omnilimit/redis';
const store = redisStore(fromIoredis(client));   // ioredis: see "When the connection drops"
const store = redisStore(fromNodeRedis(client)); // node-redis
```

The store runs one Lua script per hit, using `EVALSHA` and loading the script on `NOSCRIPT`. It uses the Redis server clock and sets a TTL so idle buckets disappear. `peek` runs a second, read-only script, `refund` a third, and `reset` is a `DEL`, so a hand-written `RedisEval` needs `del` for it.

Other Redis-compatible services work if they support `EVALSHA`, `TIME` inside scripts and `PEXPIRE`, but only Redis and Valkey are in the test matrix. To connect another client, implement `RedisEval`.

**Redis Cluster** works with the cluster client of ioredis, node-redis or redis-py: every script takes exactly one key. In Python, give the async cluster client enough connections for your concurrency (`max_connections`): past its limit it fails at once instead of waiting, and the limiter reports a store failure.

### When the connection drops

A store call whose reply is lost is a store failure: the limiter answers with your `onStoreError` choice at the timeout, and does not try again. The store may still have counted the hit, once.

**ioredis sends commands again by default.** After it reconnects, it resends every command that had no reply yet, and the store runs it a second time. A hit is then counted twice, which only makes the limit stricter. A refund is applied twice, which gives back a unit the reservation never held. Turn it off on the connection the limiter uses:

```ts
const client = new Redis(process.env.REDIS_URL, { autoResendUnfulfilledCommands: false });
```

With that option, and with node-redis, pg, redis-py, valkey-py and psycopg as they come, a call that lost its connection is applied at most once. This is tested against real stores, by cutting the connection after the store ran the call.

## Postgres

```ts
import { MIGRATION_SQL, postgresStore } from 'omnilimit/postgres';
const store = postgresStore(pool); // any object with query(sql, params), such as a pg Pool
```

1. Apply `MIGRATION_SQL` once with your migration tool. omnilimit never runs it for you, and it is safe to reapply. Upgrading, reapply it to add what is new: `omnilimit_peek_v1` (0.2), which `peek` needs, and `omnilimit_refund_v1` (0.4), which `refund` needs. Until then, a refund fails as a store error and returns `false`.
2. Schedule `store.cleanup()`, which deletes expired buckets in batches.

A client whose query function resolves to the rows themselves, instead of `{ rows }`, goes through `fromRowQuery`:

```ts
import { fromRowQuery, postgresStore } from 'omnilimit/postgres';

const store = postgresStore(fromRowQuery((sql, params) => prisma.$queryRawUnsafe(sql, ...params)));
```

That covers Prisma and serverless HTTP drivers. Only `pg` is in the test matrix; the adapter itself is tested against real Postgres with a rows-only wrapper. Whatever you pass must run each statement on its own, outside any transaction of yours.

In Python, besides psycopg 3 there are stores for the engine or pool an application already has:

| You have | Import | Use with |
|---|---|---|
| A SQLAlchemy `Engine` | `omnilimit.stores.sqlalchemy.SqlAlchemyStore` | `Limiter` |
| A SQLAlchemy `AsyncEngine` | `omnilimit.stores.sqlalchemy.AsyncSqlAlchemyStore` | `AsyncLimiter` |
| An asyncpg pool | `omnilimit.stores.asyncpg.AsyncpgStore` | `AsyncLimiter` |

The SQLAlchemy stores borrow connections from the engine's own pool and run in AUTOCOMMIT, whichever driver the engine uses. They are tested with psycopg, psycopg2, pg8000 and asyncpg. They take the engine, not a session: a hit must commit on its own, so that rolling back your work does not undo the count.

Each hit takes a row lock and writes a row, so busy buckets contend and create dead tuples. It is exact, and convenient when Postgres is already there, but it has not been benchmarked: use Redis for high traffic.

## Durable Objects

```ts
import { durableObjectStore, OmnilimitDurableObject } from 'omnilimit/cloudflare/do';
export { OmnilimitDurableObject };

const store = durableObjectStore(env.RATE_LIMITER);
```

```jsonc
// wrangler.jsonc
{
  "durable_objects": { "bindings": [{ "name": "RATE_LIMITER", "class_name": "OmnilimitDurableObject" }] },
  "migrations": [{ "tag": "v1", "new_sqlite_classes": ["OmnilimitDurableObject"] }]
}
```

There is one object per bucket, and an alarm deletes its storage after it drains. `reset` deletes the storage and the alarm. Requests incur Durable Object billing, and every check, peek, reset and refund is one round trip to the object.

The first call to an object that is not in memory, as after a deploy, can take over a second. This store therefore declares a default timeout of 3000 ms, which a limiter uses unless you set `storeTimeoutMs`.

## Cloudflare rate limit binding

```ts
import { cloudflareBindingStore } from 'omnilimit/cloudflare';
const store = cloudflareBindingStore({ binding: env.MY_LIMITER, periodSec: 60 });
```

Cloudflare enforces its own configured limit per location and only reports allowed or denied. So:

- The limit in your Wrangler configuration is the real one. Keep the limiter's `limit` and `windowMs` in step with it.
- `periodSec` must be 10 or 60.
- Decisions have `remaining: null` and `resetMs: null`, and only send `Retry-After`.
- A missing binding is a store failure, handled by `onStoreError`. It does not silently allow traffic.
- `peek`, `reset`, `reserve` and `refund` are not available: the binding cannot answer them.

## Memory

```ts
import { memoryStore } from 'omnilimit';
const store = memoryStore({ maxKeys: 100_000 });
```

It works within one process. Several instances each keep their own count, and serverless platforms may start from zero often. At `maxKeys`, expired buckets go first, then the least recently hit. A denied request counts as a hit for this, although it writes nothing: a client that keeps being denied stays in the store, so a flood of new ids cannot give it a fresh burst. An attacker rotating ids can still push out the buckets of clients that have gone quiet.

In tests, pass `now` to move time yourself instead of faking timers: `memoryStore({ now: () => clock })` (`MemoryStore(now=...)` in Python).

## Workers KV

Not supported. KV is not atomic and changes can take 60 seconds or more to reach other locations, so it cannot keep the promises above. The `Store` interface is public if you accept that trade-off in your own code.
